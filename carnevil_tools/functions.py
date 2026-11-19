"""Function discovery over a flat MIPS image: code/data boundary, functions, jump tables.

The algorithm and its results on GAME.EXE are documented in
docs/analysis/functions.md. In short, `analyse` runs these passes to a
fixpoint:

1. recursive descent from the entry (and manual starts): branches (both arms),
   delay slots, call targets; a path ends at `jr`, `eret`, `j` or an
   unconditional branch;
2. jump tables at `jr` on a register other than ra (sltiu bound + lui-based
   table address within the 12 preceding instructions);
3. text_end = end of the last code word reached, unless that code is a small
   island far out in the data (see `_Descent.bounds`);
4. pointers: aligned data words in [text_end, image end) and lui/addiu (or
   lui/ori) pairs in reached code whose value is in the text range and lands
   on a stack-adjust prologue or a known function start;
5. tail calls: a `j` to another function start, or out of the function it is
   in, makes its target a function start;
6. prologue scan: every `addiu sp,sp,-n` in a gap of the text range starts a
   function, moved back over instructions GCC scheduled above it.

Names come from debug strings (carnevil_tools/naming.py). What stays unreached
in the text range is listed in `Analysis.unreached`; a function's size stops
short of unreached words between it and the next function.
"""
import bisect
import tomllib
from dataclasses import dataclass, field

from carnevil_tools import mips, naming

SOURCES = ("entry", "call", "pointer", "prologue", "manual")
SP, RA = 29, 31
JUMP_TABLE_LOOKBACK = 12  # instructions searched back from a `jr reg`
MAX_JUMP_TABLE = 4096  # entries; a larger sltiu bound is not taken for a table
PAIR_WINDOW = 32  # instructions searched forward from a `lui` for its low half
HOISTED = 8  # instructions a function may run before its stack adjustment
_M32 = 0xFFFFFFFF


@dataclass
class Function:
    addr: int
    size: int
    name: str
    sources: set[str]


@dataclass
class JumpTable:
    jr_addr: int
    table_addr: int
    targets: list[int]


@dataclass
class Analysis:
    text_start: int
    text_end: int
    functions: list[Function]
    jump_tables: list[JumpTable]
    unreached: list[tuple[int, int]]


@dataclass
class Notes:
    """Diagnostics of one run, for the CLI summary and docs/analysis/functions.md."""

    unknown: list[int] = field(default_factory=list)  # unknown words reached as code
    unresolved_jr: list[int] = field(default_factory=list)  # jr reg with no table found
    bad_targets: list[int] = field(default_factory=list)  # control transfers outside the image
    external_calls: list[int] = field(default_factory=list)  # call targets outside the image
    cti_in_delay_slot: list[int] = field(default_factory=list)
    code_pointer_functions: int = 0  # starts first found by a lui/addiu pair in code
    named: int = 0  # functions renamed from debug strings
    # Pointer values (data words or lui/addiu pairs) refused by the prologue-or-known-start
    # rule that land anywhere inside an unreached range holding no undecodable word:
    # candidate leaf functions or case labels.
    rejected_pointers: list[int] = field(default_factory=list)
    # (address, target) of each `j` whose target is neither in its own function nor a
    # known function start; not taken as a tail call.
    jumps_out: list[tuple[int, int]] = field(default_factory=list)
    # prologue starts withdrawn because reached code runs straight into them
    withdrawn: list[int] = field(default_factory=list)


# Instructions that write their rt field; everything else on SPECIAL writes rd
# unless listed in _SPECIAL_NO_RD.
_WRITES_RT = frozenset(
    "addi addiu slti sltiu andi ori xori lui daddi daddiu lb lh lw lbu lhu lwu ld "
    "lwl lwr ldl ldr ll lld sc scd mfc0 dmfc0 mfc1 dmfc1 cfc1".split()
)
_SPECIAL_NO_RD = frozenset(
    "jr mult multu div divu dmult dmultu ddiv ddivu mthi mtlo sync syscall break "
    "tge tgeu tlt tltu teq tne".split()
)
_LINKING = frozenset("jal bltzal bgezal bltzall bgezall".split())


def _dest(insn):
    """The integer register an instruction writes, or None."""
    name = insn.mnemonic
    if name in _WRITES_RT:
        return insn.rt
    if name in _LINKING:
        return RA
    if insn.word >> 26 == 0 and name not in _SPECIAL_NO_RD and insn.kind != "unknown":
        return insn.rd
    return None


_SHIFT_IMM = frozenset("sll srl sra dsll dsrl dsra dsll32 dsrl32 dsra32".split())
_STORES = frozenset("sb sh sw swl swr sd sdl sdr sc scd".split())
_FROM_RT = frozenset("mtc1 dmtc1 ctc1 mtc0 dmtc0".split())
# Integer registers holding a value at a function's first instruction: zero, a0-a3, gp, sp, ra
_LIVE_AT_ENTRY = frozenset((0, 4, 5, 6, 7, 28, 29, 31))


def _reads(insn):
    """Integer registers an instruction reads; None if it reads state undefined at entry."""
    name, op = insn.mnemonic, insn.word >> 26
    if name in ("mfhi", "mflo", "mfc0", "dmfc0", "cfc1"):
        return None
    if op == 0:
        return {insn.rt} if name in _SHIFT_IMM else {insn.rs, insn.rt}
    if name == "lui":
        return set()
    if name in _STORES:
        return {insn.rs, insn.rt}
    if name in _FROM_RT:
        return {insn.rt}
    if op in (0x10, 0x11):  # other COP0/COP1 work on their own registers
        return set()
    if op == 0x13:  # COP1X: integer base and index for the indexed loads/stores
        return {insn.rs, insn.rt} if insn.mnemonic[-2:] == "c1" else set()
    return {insn.rs}


def _entry_run_ok(run):
    """True if a run of instructions can execute at a function entry. A write to
    $zero other than the canonical nop (word 0) is data, not compiled code."""
    live = set(_LIVE_AT_ENTRY)
    for insn in run:
        reads = _reads(insn)
        if insn.kind != "normal" or reads is None or not reads <= live:
            return False
        dest = _dest(insn)
        if dest == 0 and insn.word != 0:
            return False
        if dest is not None:
            live.add(dest)
    return True


def _always_taken(insn):
    """beq r,r and bgez zero (and their likely forms) are unconditional."""
    if insn.mnemonic in ("beq", "beql"):
        return insn.rs == insn.rt
    if insn.mnemonic in ("bgez", "bgezl"):
        return insn.rs == 0
    return False


def _is_prologue(insn):
    return insn.mnemonic == "addiu" and insn.rs == SP and insn.rt == SP and insn.imm < 0


class _Descent:
    def __init__(self, image):
        self.image = image
        self.code = set()  # every word known to be an instruction
        self.walked = set()  # words a path has continued from (delay slots may not be)
        self.starts = {}  # function start -> set of sources
        self.tables = {}  # jr address -> JumpTable
        self.jumps = []  # (address, target) of every `j`
        self.falls = set()  # words a path reaches by running on from the word before
        self.returns = set()  # words a path reaches as the return point of a call
        self.pending = []
        self.notes = Notes()
        self._decoded = {}

    def insn(self, addr):
        insn = self._decoded.get(addr)
        if insn is None:
            insn = self._decoded[addr] = mips.decode(self.image.word(addr), addr)
        return insn

    def valid(self, addr):
        return addr % 4 == 0 and self.image.contains(addr)

    def add_start(self, addr, source):
        if self.valid(addr):
            self.starts.setdefault(addr, set()).add(source)
            self.pending.append(addr)

    def run(self):
        while self.pending:
            self._path(self.pending.pop())

    def _mark(self, addr):
        """Record a reached word as code; False if it cannot be (outside the image)."""
        if not self.valid(addr):
            self.notes.bad_targets.append(addr)
            return False
        self.code.add(addr)
        return True

    def _path(self, addr):
        while addr not in self.walked and self._mark(addr):
            self.walked.add(addr)
            insn = self.insn(addr)
            kind = insn.kind
            if kind == "unknown":
                self.notes.unknown.append(addr)
                return
            if kind in ("normal", "trap"):
                addr += 4
                self.falls.add(addr)
                continue
            if insn.mnemonic == "eret":  # no delay slot
                return
            self._delay_slot(addr + 4)
            if kind in ("branch", "branch_likely"):
                self.pending.append(insn.target)
                if _always_taken(insn):
                    return
            elif kind == "call":
                if self.image.contains(insn.target):
                    self.add_start(insn.target, "call")
                else:
                    self.notes.external_calls.append(insn.target)
            elif kind == "jump":
                self.jumps.append((addr, insn.target))
                self.pending.append(insn.target)
                return
            elif kind == "jump_reg":
                if insn.rs != RA:
                    self._jump_table(addr, insn.rs)
                return
            addr += 8  # fall-through, or the return point of a call
            (self.returns if kind in ("call", "call_reg") else self.falls).add(addr)

    def _delay_slot(self, addr):
        if not self._mark(addr):
            return
        insn = self.insn(addr)
        if insn.kind == "unknown":
            self.notes.unknown.append(addr)
        elif insn.kind not in ("normal", "trap"):
            self.notes.cti_in_delay_slot.append(addr)

    def _window(self, addr, count):
        """Decoded instructions before addr, nearest first, stopping at non-image words."""
        seq = []
        for a in range(addr - 4, addr - 4 * (count + 1), -4):
            if not self.valid(a):
                break
            seq.append(self.insn(a))
        return seq

    def _jump_table(self, jr_addr, reg):
        if jr_addr in self.tables:
            return
        seq = self._window(jr_addr, JUMP_TABLE_LOOKBACK)
        table = _table_address(seq, reg)
        bound = next((i.imm for i in seq if i.mnemonic == "sltiu"), None)
        if table is None or bound is None or not 0 < bound <= MAX_JUMP_TABLE:
            self.notes.unresolved_jr.append(jr_addr)
            return
        if table % 4 or not (self.image.contains(table) and self.image.contains(table + 4 * bound - 1)):
            self.notes.unresolved_jr.append(jr_addr)
            return
        targets = []
        for k in range(bound):
            target = self.image.word(table + 4 * k)
            if self.valid(target):
                targets.append(target)
                self.pending.append(target)
        self.tables[jr_addr] = JumpTable(jr_addr, table, targets)

    def bounds(self):
        """(text_start, text_end). text_end is the end of the last code word, except
        that code beyond the widest gap between code words is an island in the data,
        not part of the text, when the gap holds data (a word that does not decode)
        and is wider than all the code beyond it."""
        ordered = sorted(self.code)
        end = ordered[-1] + 4
        widest, at = 0, None
        for i in range(len(ordered) - 1):
            gap = ordered[i + 1] - ordered[i] - 4
            if gap > widest:
                widest, at = gap, i
        if at is not None and widest > 4 * (len(ordered) - at - 1):
            lo = ordered[at] + 4
            if any(self.insn(a).kind == "unknown" for a in range(lo, lo + widest, 4)):
                end = lo
        return ordered[0], end

    def gaps(self, text_start, text_end):
        """Maximal runs of non-code words in [text_start, text_end)."""
        out = []
        lo = None
        for addr in range(text_start, text_end, 4):
            if addr in self.code:
                if lo is not None:
                    out.append((lo, addr))
                    lo = None
            elif lo is None:
                lo = addr
        if lo is not None:
            out.append((lo, text_end))
        return out

    def _function_like(self, value, text_start, text_end):
        if value % 4 or not text_start <= value < text_end:
            return False
        return value in self.starts or _is_prologue(self.insn(value))

    def data_pointers(self, text_start, text_end):
        for addr in range(text_end, self.image.end_addr() - 3, 4):
            value = self.image.word(addr)
            if self._function_like(value, text_start, text_end):
                self.add_start(value, "pointer")

    def code_pointers(self, text_start, text_end):
        for _addr, value in self.address_pairs():
            if self._function_like(value, text_start, text_end):
                if value not in self.starts:
                    self.notes.code_pointer_functions += 1
                self.add_start(value, "pointer")

    def address_pairs(self):
        """(address of the low half, value) for each lui + addiu/ori pair in reached code."""
        for addr in sorted(self.code):
            insn = self.insn(addr)
            if insn.mnemonic != "lui" or insn.rt == 0:
                continue
            reg, high = insn.rt, insn.imm << 16
            for a in range(addr + 4, addr + 4 * (PAIR_WINDOW + 1), 4):
                if a not in self.code or (a >= addr + 8 and _ends_path(self.insn(a - 8))):
                    break  # left reached code, or passed the delay slot of a path end
                nxt = self.insn(a)
                if nxt.mnemonic == "addiu" and nxt.rs == reg:
                    yield a, (high + nxt.imm) & _M32
                elif nxt.mnemonic == "ori" and nxt.rs == reg:
                    yield a, high | nxt.imm
                if _dest(nxt) == reg:
                    break

    def tail_calls(self, text_end):
        """A `j` to another known function start, or out of its own function to a
        stack-adjust prologue, is a tail call. A `j` out of its own function to
        anywhere else is only reported (Notes.jumps_out): the owner comes from the
        current starts, so taking it as a start would let one doubtful start cascade."""
        ordered = sorted(self.starts)
        out = []
        for addr, target in self.jumps:
            i = bisect.bisect_right(ordered, addr) - 1
            if i < 0:
                continue
            owner = ordered[i]
            end = ordered[i + 1] if i + 1 < len(ordered) else text_end
            if target in self.starts:
                if target != owner:
                    self.add_start(target, "call")
            elif not owner <= target < end:
                if self.valid(target) and _is_prologue(self.insn(target)):
                    self.add_start(target, "call")
                else:
                    out.append((addr, target))
        self.notes.jumps_out = sorted(set(out))

    def withdraw_fallen_into(self):
        """Drop prologue-only starts that a later pass proved wrong:
        - reached code runs straight into the start: it is inside the function
          before it (whose own start lies further back than HOISTED, or was found
          by another pass later);
        - its hoisted run runs straight into another start: the hoist overshot
          that start, and the words before it are unreached again."""
        ordered = sorted(self.starts)
        for i, addr in enumerate(ordered):
            if self.starts[addr] != {"prologue"}:
                continue
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if addr in self.falls:
                del self.starts[addr]
                self.notes.withdrawn.append(addr)
            elif nxt is not None and nxt - addr <= 4 * HOISTED and all(
                    self.insn(a).kind == "normal" for a in range(addr, nxt, 4)):
                del self.starts[addr]
                self.notes.withdrawn.append(addr)
                for a in range(addr, nxt, 4):
                    self.code.discard(a)
                    self.walked.discard(a)
                self.falls.discard(nxt)

    def prologues(self, text_start, text_end):
        """Each `addiu sp,sp,-n` in a gap starts a function. GCC schedules a few
        instructions above the stack adjustment, so the start moves back over them."""
        for lo, hi in self.gaps(text_start, text_end):
            for addr in range(lo, hi, 4):
                if _is_prologue(self.insn(addr)):
                    start = self._hoisted_start(lo, addr)
                    if start not in self.notes.withdrawn:  # never re-propose a refuted start
                        self.add_start(start, "prologue")

    def _hoisted_start(self, lo, addr):
        """Earliest q in [lo, addr) such that [q, addr) can open a function: at most
        HOISTED instructions, none a control transfer or a delay slot, each reading
        only registers live at entry or written earlier in the run. Zero words
        (padding) at the front are left out."""
        q = addr
        while q - 4 >= max(lo, addr - 4 * HOISTED):
            prev = self.insn(q - 4)
            if prev.kind != "normal" or (q - 8 >= lo and self.insn(q - 8).kind not in ("normal", "trap", "unknown")):
                break
            q -= 4
        while q < addr and not _entry_run_ok([self.insn(a) for a in range(q, addr, 4)]):
            q += 4
        while q < addr and self.image.word(q) == 0:
            q += 4
        return q


def _ends_path(insn):
    return insn.kind in ("jump", "jump_reg") or (
        insn.kind in ("branch", "branch_likely") and _always_taken(insn)
    )


def _constant(seq, start, reg, depth=0):
    """Value of `reg` built by lui [+ addiu/ori] in seq[start:] (nearest first), or None."""
    for k in range(start, len(seq)):
        insn = seq[k]
        if _dest(insn) != reg:
            continue
        if insn.mnemonic == "lui":
            return insn.imm << 16
        if insn.mnemonic in ("addiu", "ori") and depth < 2:
            base = _constant(seq, k + 1, insn.rs, depth + 1)
            if base is None:
                return None
            return (base + insn.imm) & _M32 if insn.mnemonic == "addiu" else base | insn.imm
        return None
    return None


def _table_address(seq, reg):
    """Table address for `jr reg`: lw reg,off(base) where base = constant + index."""
    for k, insn in enumerate(seq):
        if _dest(insn) != reg:
            continue
        if insn.mnemonic != "lw":
            return None
        offset, base = insn.imm, insn.rs
        for m in range(k + 1, len(seq)):
            add = seq[m]
            if _dest(add) != base:
                continue
            if add.mnemonic not in ("addu", "daddu"):
                return None
            for operand in (add.rs, add.rt):
                value = _constant(seq, m + 1, operand)
                if value is not None:
                    return (value + offset) & _M32
            return None
        return None
    return None


def analyse(image, entry, manual=()):
    """Discover the functions, jump tables and text/data boundary of an ExeImage."""
    return discover(image, entry, manual)[0]


def discover(image, entry, manual=()):
    """As `analyse`, also returning the run's Notes."""
    d = _Descent(image)
    d.add_start(entry, "entry")
    for addr in manual:
        d.add_start(addr, "manual")
    d.run()
    def state():
        return len(d.code), sum(len(s) for s in d.starts.values()), len(d.notes.withdrawn)

    while True:
        before = state()
        text_start, text_end = d.bounds()
        d.data_pointers(text_start, text_end)
        d.code_pointers(text_start, text_end)
        d.run()
        d.tail_calls(text_end)
        d.run()
        d.prologues(text_start, text_end)
        d.run()
        d.withdraw_fallen_into()
        if state() == before:
            break
    text_start, text_end = d.bounds()
    d.tail_calls(text_end)  # jumps_out against the final starts
    unreached = d.gaps(text_start, text_end)
    gap_at_end = {hi: lo for lo, hi in unreached}
    ordered = sorted(d.starts)
    funcs = []
    for i, addr in enumerate(ordered):
        if addr >= text_end:  # a code island in the data: just the code reached
            end = addr
            while end in d.code and (i + 1 == len(ordered) or end < ordered[i + 1]):
                end += 4
        else:
            end = min(ordered[i + 1], text_end) if i + 1 < len(ordered) else text_end
            gap = gap_at_end.get(end)
            if gap is not None and gap > addr:
                end = gap  # trailing data before the next function is not part of this one
        name = "_start" if addr == entry else "func_%08X" % addr
        funcs.append(Function(addr, end - addr, name, set(d.starts[addr])))
    analysis = Analysis(text_start, text_end, funcs,
                        [d.tables[a] for a in sorted(d.tables)], unreached)
    pairs = list(d.address_pairs())
    d.notes.named = naming.name_from_strings(image, analysis, pairs)
    values = {v for _a, v in pairs}
    values.update(image.word(a) for a in range(text_end, image.end_addr() - 3, 4))
    # Ranges holding an undecodable word are data; pointers into them are data pointers.
    code_like = [(lo, hi) for lo, hi in unreached
                 if all(d.insn(a).kind != "unknown" for a in range(lo, hi, 4))]
    gap_lo = [lo for lo, _hi in code_like]
    refused = []
    for value in sorted(values):
        i = bisect.bisect_right(gap_lo, value) - 1
        if value % 4 == 0 and i >= 0 and value < code_like[i][1]:
            refused.append(value)
    d.notes.rejected_pointers = refused
    return analysis, d.notes


def _hex(value):
    return "0x%08X" % value


def _toml_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_toml(analysis, path):
    """Write an Analysis as TOML: sorted by address, one stable layout."""
    lines = [
        "# Function discovery: python3 -m carnevil_tools functions <exe> -o <this file>",
        "# Addresses, sizes, names and sources only. See docs/analysis/functions.md.",
        "text_start = " + _hex(analysis.text_start),
        "text_end = " + _hex(analysis.text_end),
    ]
    for f in sorted(analysis.functions, key=lambda f: f.addr):
        lines += ["", "[[function]]", "addr = " + _hex(f.addr), "size = %d" % f.size,
                  "name = " + _toml_string(f.name),
                  "sources = [%s]" % ", ".join(_toml_string(s) for s in sorted(f.sources))]
    for t in sorted(analysis.jump_tables, key=lambda t: t.jr_addr):
        lines += ["", "[[jump_table]]", "jr_addr = " + _hex(t.jr_addr),
                  "table_addr = " + _hex(t.table_addr), "targets = ["]
        for k in range(0, len(t.targets), 4):
            lines.append("  " + " ".join(_hex(v) + "," for v in t.targets[k:k + 4]))
        lines.append("]")
    for lo, hi in sorted(analysis.unreached):
        lines += ["", "[[unreached]]", "start = " + _hex(lo), "end = " + _hex(hi)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_toml(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Analysis(
        data["text_start"],
        data["text_end"],
        [Function(f["addr"], f["size"], f["name"], set(f["sources"]))
         for f in data.get("function", [])],
        [JumpTable(t["jr_addr"], t["table_addr"], list(t["targets"]))
         for t in data.get("jump_table", [])],
        [(u["start"], u["end"]) for u in data.get("unreached", [])],
    )
