"""Emit one analysed MIPS function as one C function.

`function_c(image, analysis, fn)` returns `void fn_<ADDR>(uint8_t *ram, cpu_t *c)` (the
analysis name in a comment above it, preceded by prototypes of the functions it calls
directly). Straight-line instructions come from `emit.statement`; control transfers are
emitted here, with the order of the reference interpreter (runtime/interp/interp.c):

- only reached words (`functions.reached_words`) are emitted; every branch, jump or jump-table
  target inside the function gets a label `L_<ADDR>`;
- a branch decides before its delay slot runs: its condition goes into a local, then the
  slot, then the jump. A branch-likely runs its slot only when taken. A condition fixed by
  its operands (`beq r,r`, `bgez zero`, ...) is decided here;
- a delay slot that is also a branch target is emitted a second time under its own label,
  on the fall-through path (which the branch's own not-taken path jumps over);
- `jal`: ra = the address after the slot, the slot, a direct call (`rt_call` when the target
  is not a known function start); `jalr`: the target read into a local, the link, the slot,
  `rt_call`. Execution then continues after the slot;
- `jr ra`: the slot, `return`. `jr` with a jump table: the register read before the slot,
  then a `switch` with one `case` per distinct target and a `default` that calls `rt_jump`;
  `jr` without one: `rt_jump`, `return`;
- a branch or jump to another function's start: the slot, a direct call, `return`; a jump
  into a stub block: `rt_loader(ram, c, addr - block_start)`, `return` (a call: no
  `return`). A branch into a stub block, or a branch or jump to any other address outside
  the function: `RecompileError`;
- `break`: `rt_break(ram, c, addr, code)`, then the next instruction;
- `RT_CHECK(ram, c)` at entry and, after the slot, before every `goto` to an address at or
  below the transfer's own; never before a transfer that leaves through a call (the
  callee's entry checks; see recomp.h);
- a function whose code can run off its end calls the function starting there, then
  returns; `RecompileError` if none does;
- a function in a stub block is a thunk whose body is the `rt_loader` call; a function the
  analysis lists as interpreted is a thunk whose body is `interp_run(ram, c, addr)` (its
  own words are never emitted or checked).

A control transfer in a delay slot, or an instruction outside the reached census, is a
`RecompileError` naming its address.

`recompile(image, analysis, out_dir)` writes the whole image: `funcs.h` (one prototype per
function), `funcs_000.c` ... (FUNCTIONS_PER_FILE functions each, in address order, each file
including recomp.h and funcs.h), `func_table.c` (`recomp_lookup`, a binary search over every
function start, stub slots and interpreted functions included) and, last, `manifest.toml`.
Everything goes to a working folder this run creates next to out_dir, renamed to out_dir
when complete, so a folder with a manifest is always a complete output. The only path
`recompile` ever deletes that it did not create is out_dir itself, and only when it holds a
manifest.toml; on any failure the folders this run created are removed and an existing
output is left as it was.
"""
import dataclasses
import os
import shutil
import tempfile

from carnevil_tools import emit, functions, mips

FUNCTIONS_PER_FILE = 64
MANIFEST = "manifest.toml"
MANIFEST_FORMAT = 1

_M32 = 0xFFFFFFFF
_TRANSFERS = frozenset(("branch", "branch_likely", "jump", "jump_reg", "call", "call_reg"))
_SIGN_TESTS = {"blez": "<=", "bgtz": ">", "bltz": "<", "bgez": ">="}
_SIGN_OF_ZERO = {"blez": True, "bgtz": False, "bltz": False, "bgez": True}


class RecompileError(Exception):
    def __init__(self, addr, reason):
        super().__init__(f"{addr:08X}: {reason}")
        self.addr = addr
        self.reason = reason


def c_name(addr):
    """The C name of the recompiled function at addr."""
    return f"fn_{addr & _M32:08X}"


def _label(addr):
    return f"L_{addr:08X}"


def _u(value):
    return f"0x{value & _M32:08X}u"


def _reg(n):
    return "0u" if n == 0 else f"c->r[{n}]"


def _indent(lines, depth=1):
    return ["    " * depth + line for line in lines]


def _prototype(addr):
    return f"void {c_name(addr)}(uint8_t *ram, cpu_t *c)"


def _condition(insn):
    """The C condition of a branch, or True / False when its operands decide it."""
    name = insn.mnemonic[:-1] if insn.kind == "branch_likely" else insn.mnemonic
    if name in ("beq", "bne"):
        if insn.rs == insn.rt:
            return name == "beq"
        op = "==" if name == "beq" else "!="
        return f"{_reg(insn.rs)} {op} {_reg(insn.rt)}"
    if name in _SIGN_TESTS:
        if insn.rs == 0:
            return _SIGN_OF_ZERO[name]
        return f"(int32_t)c->r[{insn.rs}] {_SIGN_TESTS[name]} 0"
    # bc1f / bc1t
    return f"rc_cc(c, {mips.fp_condition_code(insn)}) == {1 if name == 'bc1t' else 0}"


class _Function:
    def __init__(self, image, analysis, fn):
        self.image = image
        self.analysis = analysis
        self.fn = fn
        self.insn = mips.Decoder(image).insn
        alone = dataclasses.replace(analysis, functions=[fn])  # reached_words of fn only
        self.words = sorted(functions.reached_words(alone)[fn.addr])
        self.wordset = set(self.words)
        self.starts = {f.addr for f in analysis.functions}
        self.tables = {t.jr_addr: t for t in analysis.jump_tables}
        self.unresolved = []  # each `jr` (not on ra) left to rt_jump
        self.callees = set()
        self.labels = set()
        self.lines = []

    # --- where control goes ------------------------------------------------------------

    def stub_slot(self, addr):
        for lo, hi in self.analysis.stub_blocks:
            if lo <= addr < hi:
                return addr - lo
        return None

    def transfer(self, target, at, what):
        """Lines that leave `at` for `target` for good (a goto or a call and return)."""
        if target in self.wordset:  # labelled by collect_labels
            check = ["RT_CHECK(ram, c);"] if target <= at else []
            return check + [f"goto {_label(target)};"]
        slot = self.stub_slot(target)
        if slot is not None:
            if what == "branch to":
                raise RecompileError(at, f"branch to {target:08X}, in a stub block")
            return [f"rt_loader(ram, c, {_u(slot)});", "return;"]
        if target in self.starts:
            self.callees.add(target)
            return [f"{c_name(target)}(ram, c);", "return;"]
        raise RecompileError(at, f"{what} {target:08X}: outside the function and not a "
                                 "function start")

    def call(self, target):
        slot = self.stub_slot(target)
        if slot is not None:
            return [f"rt_loader(ram, c, {_u(slot)});"]
        if target in self.starts:
            self.callees.add(target)
            return [f"{c_name(target)}(ram, c);"]
        return [f"rt_call(ram, c, {_u(target)});"]

    def dynamic_jump(self, var):
        return [f"rt_jump(ram, c, {var});", "return;"]

    # --- instructions ------------------------------------------------------------------

    def straight(self, insn, addr):
        if insn.mnemonic == "break":
            return [f"rt_break(ram, c, {_u(addr)}, {_u(insn.word >> 6 & 0xFFFFF)});"]
        try:
            return emit.statement(insn, addr)
        except emit.UnsupportedInstruction:
            raise RecompileError(addr, f"unsupported instruction {insn.mnemonic}") from None

    def delay_slot(self, addr):
        slot = addr + 4
        if not self.image.contains(slot) or self.stub_slot(slot) is not None:
            raise RecompileError(addr, "the delay slot is not in the file")
        insn = self.insn(slot)
        if insn.kind in _TRANSFERS:
            raise RecompileError(slot, f"control transfer {insn.mnemonic} in the delay slot "
                                       f"of {addr:08X}")
        return self.straight(insn, slot)

    def control(self, insn, addr):
        """Lines for a control transfer and its slot; whether execution can go on at
        addr + 8."""
        if insn.mnemonic not in emit.CONTROL:
            raise RecompileError(addr, f"unsupported instruction {insn.mnemonic}")
        name, kind, slot = insn.mnemonic, insn.kind, self.delay_slot(addr)
        if kind in ("branch", "branch_likely"):
            cond = _condition(insn)
            if cond is True:
                return slot + self.transfer(insn.target, addr, "branch to"), False
            if cond is False:
                return ([] if kind == "branch_likely" else slot), True
            taken = self.transfer(insn.target, addr, "branch to")
            if kind == "branch_likely":
                return [f"if ({cond}) {{", *_indent(slot + taken), "}"], True
            return ["{", f"    int taken = {cond};", *_indent(slot),
                    "    if (taken) {", *_indent(taken, 2), "    }", "}"], True
        if name == "j":
            return slot + self.transfer(insn.target, addr, "jump to"), False
        if name == "jal":
            return [f"c->r[31] = {_u(addr + 8)};", *slot, *self.call(insn.target)], True
        if name == "jalr":
            link = [f"c->r[{insn.rd}] = {_u(addr + 8)};"] if insn.rd else []
            return ["{", f"    uint32_t target = {_reg(insn.rs)};", *_indent(link + slot),
                    "    rt_call(ram, c, target);", "}"], True
        # jr
        if insn.rs == mips.RA:
            return slot + ["return;"], False
        head = ["{", f"    uint32_t target = {_reg(insn.rs)};", *_indent(slot)]
        table = self.tables.get(addr)
        if table is None:
            self.unresolved.append(addr)
            return head + _indent(self.dynamic_jump("target")) + ["}"], False
        cases = []
        for target in sorted(set(table.targets)):
            cases += [f"case {_u(target)}:", *_indent(self.transfer(target, addr, "jump-table "
                                                                    "entry"))]
        cases += ["default:", *_indent(self.dynamic_jump("target"))]
        return head + ["    switch (target) {", *_indent(cases), "    }", "}"], False

    # --- the function ------------------------------------------------------------------

    def collect_labels(self):
        for addr in self.words:
            insn = self.insn(addr)
            if insn.kind in ("branch", "branch_likely", "jump") and insn.target in self.wordset:
                self.labels.add(insn.target)
            table = self.tables.get(addr) if insn.kind == "jump_reg" else None
            if table is not None:
                self.labels.update(t for t in table.targets if t in self.wordset)

    def run_on(self, nxt, at):
        """Execution runs on from `at` to `nxt`, which is not a word of the function."""
        if nxt in self.labels:
            self.lines.append(f"{_label(nxt)}:;")
        self.lines += _indent(self.transfer(nxt, at, "runs off the end of the function into"))

    def body(self):
        if not self.words or self.words[0] != self.fn.addr:
            raise RecompileError(self.fn.addr, "the function's first word is not reached code")
        self.collect_labels()
        self.lines = ["    RT_CHECK(ram, c);"]
        nxt, last, i = None, None, 0
        while i < len(self.words):
            addr = self.words[i]
            if nxt is not None and nxt != addr:
                self.run_on(nxt, last)
            if addr in self.labels:
                self.lines.append(f"{_label(addr)}:;")
            insn = self.insn(addr)
            if insn.kind not in _TRANSFERS:
                self.lines += _indent(self.straight(insn, addr))
                nxt, last = addr + 4, addr
                i += 1
                continue
            lines, falls = self.control(insn, addr)
            self.lines += _indent(lines)
            nxt, last = (addr + 8 if falls else None), addr + 4
            if addr + 4 in self.labels:  # a branch target: again, under its own label
                if falls:
                    # a label added while emitting: safe because addr + 8 is emitted later in
                    # this loop (words ascend), where `addr in self.labels` places it; when
                    # addr + 8 is not a word of the function, run_on places it instead
                    self.labels.add(addr + 8)
                    self.lines.append(f"    goto {_label(addr + 8)};")
                self.lines.append(f"{_label(addr + 4)}:;")
                self.lines += _indent(self.delay_slot(addr))
                nxt = addr + 8
            i += 2 if i + 1 < len(self.words) and self.words[i + 1] == addr + 4 else 1
        if nxt is not None:
            self.run_on(nxt, last)
        return self.lines

    def text(self, prototypes=True):
        slot = self.stub_slot(self.fn.addr)
        if slot is not None:
            lines = [f"    rt_loader(ram, c, {_u(slot)});"]
        elif self.fn.addr in self.analysis.interpreted:
            lines = [f"    interp_run(ram, c, {_u(self.fn.addr)});"]
        else:
            lines = self.body()
        name = self.fn.name.replace("*/", "* /")
        protos = [_prototype(a) + ";" for a in sorted(self.callees - {self.fn.addr})
                  if prototypes]
        return "\n".join(protos + [f"/* {name} */", _prototype(self.fn.addr), "{", *lines,
                                   "}"]) + "\n"


def function_c(image, analysis, fn, prototypes=True):
    """The C for one function of `analysis` (a functions.Analysis of the ExeImage `image`).
    With prototypes, the text is preceded by a prototype of each function it calls, so it
    compiles on its own; without, it relies on a header declaring them (`recompile`'s
    funcs.h)."""
    return _Function(image, analysis, fn).text(prototypes)


@dataclasses.dataclass
class Manifest:
    """What `recompile` wrote (manifest.toml)."""

    format: int
    exe_sha256: str
    functions: int
    files: int
    stubs: int
    interpreted: int
    jump_tables: int
    unresolved_jr: int  # `jr`s (not on ra) in emitted code that become rt_jump


_HEAD = ("/* {name}: written by `python3 -m carnevil_tools recompile` from the EXE with SHA-256 "
         "{sha}.\n * Generated code: do not edit, and never commit it. */\n")


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _sources(image, analysis):
    """{file name: text} of every C file, and the Manifest. Raises RecompileError."""
    fns = sorted(analysis.functions, key=lambda f: f.addr)
    sha = analysis.exe_sha256
    texts, unresolved = [], 0
    for fn in fns:
        emitted = _Function(image, analysis, fn)
        texts.append(emitted.text(prototypes=False))
        unresolved += len(emitted.unresolved)
    files = {"funcs.h": _HEAD.format(name="funcs.h", sha=sha) + "\n".join(
        ["#ifndef FUNCS_H", "#define FUNCS_H", "", '#include "recomp.h"', ""]
        + [_prototype(f.addr) + ";" for f in fns] + ["", "#endif"]) + "\n"}
    for k in range(0, len(fns), FUNCTIONS_PER_FILE):
        name = "funcs_%03d.c" % (k // FUNCTIONS_PER_FILE)
        files[name] = (_HEAD.format(name=name, sha=sha) + '\n#include "recomp.h"\n'
                       '#include "funcs.h"\n\n' + "\n".join(texts[k:k + FUNCTIONS_PER_FILE]))
    entries = [f"    {{{_u(f.addr)}, {c_name(f.addr)}}}," for f in fns]
    files["func_table.c"] = _HEAD.format(name="func_table.c", sha=sha) + "\n".join(
        ["", "#include <stddef.h>", "", '#include "recomp.h"', '#include "funcs.h"', "",
         "/* every function start, ascending */",
         "static const struct {", "    uint32_t addr;", "    recomp_fn fn;", "} table[] = {",
         *entries, "};", "",
         "recomp_fn recomp_lookup(uint32_t addr)", "{",
         "    size_t lo = 0, hi = sizeof table / sizeof table[0];",
         "    while (lo < hi) {",
         "        size_t mid = lo + (hi - lo) / 2;",
         "        if (table[mid].addr < addr)",
         "            lo = mid + 1;",
         "        else if (table[mid].addr > addr)",
         "            hi = mid;",
         "        else",
         "            return table[mid].fn;",
         "    }",
         "    return NULL;",
         "}"]) + "\n"
    stubs = sum(any(lo <= f.addr < hi for lo, hi in analysis.stub_blocks) for f in fns)
    manifest = Manifest(MANIFEST_FORMAT, sha, len(fns),
                        -(-len(fns) // FUNCTIONS_PER_FILE), stubs, len(analysis.interpreted),
                        len(analysis.jump_tables), unresolved)
    return files, manifest


def _manifest_toml(m):
    return ("# Written last by `python3 -m carnevil_tools recompile`: a folder holding this file "
            "is a complete output.\n"
            + "".join(f"{k} = " + (f'"{v}"' if isinstance(v, str) else str(v)) + "\n"
                      for k, v in dataclasses.asdict(m).items()))


def recompile(image, analysis, out_dir):
    """Write the C of every function of `analysis` (made from the ExeImage `image`) to
    out_dir; the Manifest. Raises RecompileError (nothing is written), ValueError when the
    image is not the one the analysis was made from or out_dir exists without a
    manifest.toml (it is left as it is), OSError."""
    if image.sha256 != analysis.exe_sha256:
        raise ValueError(f"the EXE has SHA-256 {image.sha256 or '(none)'}, the functions were "
                         f"analysed from SHA-256 {analysis.exe_sha256 or '(none)'}")
    out_dir = os.path.normpath(out_dir)
    if os.path.lexists(out_dir) and not (
            os.path.isdir(out_dir) and os.path.isfile(os.path.join(out_dir, MANIFEST))):
        raise ValueError(f"{out_dir} exists and holds no {MANIFEST}: it is not a recompile "
                         "output, so it is not replaced")
    files, manifest = _sources(image, analysis)
    parent, base = os.path.split(out_dir)
    parent = parent or os.curdir
    os.makedirs(parent, exist_ok=True)
    # Folders of this run only, next to out_dir (one filesystem, so the renames are moves);
    # nothing else next to it is touched.
    work = tempfile.mkdtemp(dir=parent, prefix=f".{base}.partial-")
    aside = None
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(work, 0o777 & ~umask)  # mkdtemp makes it 0700
        for name in sorted(files):
            _write(os.path.join(work, name), files[name])
        _write(os.path.join(work, MANIFEST), _manifest_toml(manifest))
        if os.path.lexists(out_dir):
            aside = tempfile.mkdtemp(dir=parent, prefix=f".{base}.old-")
            os.rename(out_dir, os.path.join(aside, base))
        os.rename(work, out_dir)
    except BaseException:
        held = None if aside is None else os.path.join(aside, base)
        if held is not None and os.path.lexists(held) and not os.path.lexists(out_dir):
            os.rename(held, out_dir)
        for path in (work, aside):
            if path is not None and os.path.lexists(path):
                shutil.rmtree(path)
        raise
    if aside is not None:
        shutil.rmtree(aside)
    return manifest
