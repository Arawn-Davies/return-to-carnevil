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

What the analysis cannot discover comes from a hand-maintained file
(config/game.manual.toml, `read_manual`): manual starts; stub blocks, ranges
the program fills at run time; jump tables for a `jr` the analysis cannot
resolve; functions left to the interpreter; and host-switch points, the
transfers of the game's task switch that the runtime takes over. Stub-block words are never
decoded; each call or jump target inside one is a function of one slot with
source "stub". A jump table is recorded only when every entry lies in the text,
so targets[k] is always case k. A manual jump table has table_addr 0; its
targets are descended into like a found table's, and it is checked against the
result (the `jr` is reached code of a function and every target lies inside that
function). An interpreted function must be a function start; it stays in the
function list (`Analysis.interpreted` names it) so calls to it resolve. A
host-switch point is a `j` (kinds yield and exit) or a `jr ra` (kind resume) in
reached code of a function; after a resume, execution goes on at the
instruction after its delay slot, so the descent continues there.
"""
import bisect
import tomllib
from dataclasses import dataclass, field

from carnevil_tools import mips, naming

SOURCES = ("entry", "call", "pointer", "prologue", "manual", "stub")
FORMAT = 3  # version of the TOML layout written by write_toml
# kinds of [[host_switch]] entry, in the order of recomp.h's RT_SWITCH_* values
HOST_SWITCH_KINDS = ("resume", "yield", "exit")
STUB_SLOT = 8  # bytes per slot of a stub block (a `j` and its delay slot)
RA = mips.RA
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
    table_addr: int  # 0 for a table from the manual file
    targets: list[int]


@dataclass
class Analysis:
    text_start: int
    text_end: int
    functions: list[Function]
    jump_tables: list[JumpTable]
    unreached: list[tuple[int, int]]
    # [start, end) ranges filled at run time, not code in the file (game.manual.toml)
    stub_blocks: list[tuple[int, int]] = field(default_factory=list)
    exe_sha256: str = ""  # SHA-256 of the file the analysis was made from
    # function starts left to the interpreter (game.manual.toml), ascending
    interpreted: list[int] = field(default_factory=list)
    # host-switch points (game.manual.toml): address -> kind (HOST_SWITCH_KINDS)
    host_switches: dict[int, str] = field(default_factory=dict)


@dataclass
class Manual:
    """The hand-maintained input of `analyse` (`read_manual`)."""

    starts: list[int] = field(default_factory=list)
    stub_blocks: list[tuple[int, int]] = field(default_factory=list)
    jump_tables: list[JumpTable] = field(default_factory=list)  # table_addr 0
    interpreted: list[int] = field(default_factory=list)
    host_switches: dict[int, str] = field(default_factory=dict)  # address -> kind


@dataclass
class Notes:
    """Diagnostics of one run, for the CLI summary and docs/analysis/functions.md."""

    unknown: list[int] = field(default_factory=list)  # unknown words reached as code
    # jr reg with no table found, or whose table has an entry outside the text
    unresolved_jr: list[int] = field(default_factory=list)
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


# Integer registers holding a value at a function's first instruction: zero, a0-a3, gp, sp, ra
_LIVE_AT_ENTRY = frozenset((0, 4, 5, 6, 7, 28, 29, 31))


def _entry_run_ok(run):
    """True if a run of instructions can execute at a function entry. A write to
    $zero other than the canonical nop (word 0) is data, not compiled code."""
    live = set(_LIVE_AT_ENTRY)
    for insn in run:
        reads = mips.reads(insn)
        if insn.kind != "normal" or reads is None or not reads <= live:
            return False
        dest = mips.dest_reg(insn)
        if dest == 0 and insn.word != 0:
            return False
        if dest is not None:
            live.add(dest)
    return True


class _Descent:
    def __init__(self, image, stub_blocks=(), refused_tables=(), manual_tables=(), resumes=()):
        self.image = image
        self.stub_blocks = sorted(stub_blocks)
        self.resumes = set(resumes)  # `jr ra` host-switch points: execution goes on after
        self.refused_tables = set(refused_tables)  # jr addresses whose table left the text
        self.manual_tables = {t.jr_addr: t for t in manual_tables}
        # jr address -> the table the analysis finds by itself at a manual table's jr
        self.found_under_manual = {}
        self.code = set()  # every word known to be an instruction
        self.walked = set()  # words a path has continued from (delay slots may not be)
        self.starts = {}  # function start -> set of sources
        self.tables = {}  # jr address -> JumpTable
        self.jumps = []  # (address, target) of every `j`
        self.falls = set()  # words a path reaches by running on from the word before
        self.delay_slots = set()  # words reached as the delay slot of a control transfer
        self.pending = []
        self.notes = Notes()
        self.insn = mips.Decoder(image).insn

    def valid(self, addr):
        return addr % 4 == 0 and self.image.contains(addr)

    def in_stub(self, addr):
        return any(lo <= addr < hi for lo, hi in self.stub_blocks)

    def add_start(self, addr, source):
        """A function start. Inside a stub block every start is a "stub" and is not
        descended into: its words are filled at run time."""
        if not self.valid(addr):
            return
        if self.in_stub(addr):
            self.starts.setdefault(addr, set()).add("stub")
            return
        self.starts.setdefault(addr, set()).add(source)
        self.pending.append(addr)

    def run(self):
        while self.pending:
            addr = self.pending.pop()
            if self.in_stub(addr):  # a branch or jump target in a stub block
                self.add_start(addr, "stub")
            else:
                self._path(addr)

    def _mark(self, addr):
        """Record a reached word as code; False if it cannot be (outside the image)."""
        if not self.valid(addr):
            self.notes.bad_targets.append(addr)
            return False
        self.code.add(addr)
        return True

    def _path(self, addr):
        while addr not in self.walked and not self.in_stub(addr) and self._mark(addr):
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
                if mips.always_taken(insn):
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
                if addr not in self.resumes:
                    return
            addr += 8  # fall-through, or the return point of a call
            if kind not in ("call", "call_reg"):
                self.falls.add(addr)

    def _delay_slot(self, addr):
        if self.in_stub(addr) or not self._mark(addr):
            return
        self.delay_slots.add(addr)
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
        """Record the table of `jr reg`, or list the jr as unresolved. Every entry must be
        a word address in the image (and, checked by `discover`, in the text), so that
        targets[k] is case k."""
        if jr_addr in self.tables:
            return
        manual = self.manual_tables.get(jr_addr)
        if manual is not None:  # checked against the result by `discover`
            found = self._found_table(jr_addr, reg)
            if found is not None:
                self.found_under_manual[jr_addr] = found
            self.pending.extend(t for t in manual.targets if self.valid(t))
            self.tables[jr_addr] = JumpTable(jr_addr, 0, list(manual.targets))
            return
        found = None if jr_addr in self.refused_tables else self._found_table(jr_addr, reg)
        if found is None:
            self.notes.unresolved_jr.append(jr_addr)
            return
        self.pending.extend(found.targets)
        self.tables[jr_addr] = found

    def _found_table(self, jr_addr, reg):
        """The JumpTable the analysis finds for `jr reg` by itself, or None."""
        seq = self._window(jr_addr, JUMP_TABLE_LOOKBACK)
        table = _table_address(seq, reg)
        bound = next((i.imm for i in seq if i.mnemonic == "sltiu"), None)
        if table is None or bound is None or not 0 < bound <= MAX_JUMP_TABLE:
            return None
        if table % 4 or not (self.image.contains(table) and self.image.contains(table + 4 * bound - 1)):
            return None
        targets = [self.image.word(table + 4 * k) for k in range(bound)]
        if not all(self.valid(t) and not self.in_stub(t) for t in targets):
            return None
        return JumpTable(jr_addr, table, targets)

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
        """Maximal runs of non-code words in [text_start, text_end), stub blocks aside
        (their words are neither code nor data in the file)."""
        out = []
        lo = None
        for addr in range(text_start, text_end, 4):
            if addr in self.code or self.in_stub(addr):
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
        if value in self.starts:
            return True
        return not self.in_stub(value) and mips.is_prologue(self.insn(value))

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
                if a not in self.code or (a >= addr + 8 and mips.ends_path(self.insn(a - 8))):
                    break  # left reached code, or passed the delay slot of a path end
                nxt = self.insn(a)
                if nxt.mnemonic == "addiu" and nxt.rs == reg:
                    yield a, (high + nxt.imm) & _M32
                elif nxt.mnemonic == "ori" and nxt.rs == reg:
                    yield a, high | nxt.imm
                if mips.dest_reg(nxt) == reg:
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
                if self.valid(target) and self.in_stub(target):
                    self.add_start(target, "stub")
                elif self.valid(target) and mips.is_prologue(self.insn(target)):
                    self.add_start(target, "call")
                else:
                    out.append((addr, target))
        self.notes.jumps_out = sorted(set(out))

    def withdraw_fallen_into(self):
        """Drop prologue-only starts that a later pass proved wrong:
        - reached code runs straight into the start, or reaches it as the delay
          slot of a branch: it is inside the function before it (whose own start
          lies further back than HOISTED, or was found by another pass later);
        - its hoisted run runs straight into another start: the hoist overshot
          that start, and the words before it are unreached again."""
        ordered = sorted(self.starts)
        for i, addr in enumerate(ordered):
            if self.starts[addr] != {"prologue"}:
                continue
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            if addr in self.falls or addr in self.delay_slots:
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
                if mips.is_prologue(self.insn(addr)):
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


def _constant(seq, start, reg, depth=0):
    """Value of `reg` built by lui [+ addiu/ori] in seq[start:] (nearest first), or None."""
    for k in range(start, len(seq)):
        insn = seq[k]
        if mips.dest_reg(insn) != reg:
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
        if mips.dest_reg(insn) != reg:
            continue
        if insn.mnemonic != "lw":
            return None
        offset, base = insn.imm, insn.rs
        for m in range(k + 1, len(seq)):
            add = seq[m]
            if mips.dest_reg(add) != base:
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


def analyse(image, entry, manual=(), stub_blocks=(), jump_tables=(), interpreted=(),
            host_switches=None):
    """Discover the functions, jump tables and text/data boundary of an ExeImage."""
    return discover(image, entry, manual, stub_blocks, jump_tables, interpreted,
                    host_switches)[0]


def _descend(image, entry, manual, stub_blocks, refused_tables, manual_tables, resumes):
    """Run the passes to a fixpoint; (descent, text_start, text_end)."""
    d = _Descent(image, stub_blocks, refused_tables, manual_tables, resumes)
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
    return d, text_start, text_end


def discover(image, entry, manual=(), stub_blocks=(), jump_tables=(), interpreted=(),
             host_switches=None):
    """As `analyse`, also returning the run's Notes.

    stub_blocks: [start, end) ranges whose words are filled at run time: never decoded,
    and each call or jump target inside one is a function of one slot (STUB_SLOT bytes)
    with source "stub". A jump table with an entry outside [text_start, text_end) is
    not recorded; its `jr` is listed as unresolved and the discovery is run again
    without it, so no case is lost or shifted.

    jump_tables: JumpTables (table_addr 0) for `jr`s the analysis cannot resolve; their
    targets are descended into. interpreted: function starts left to the interpreter.
    host_switches: {address: kind} of the host-switch points; the descent goes on after the
    delay slot of each resume.
    ValueError, naming the entry, if a jump table's `jr_addr` is not a `jr reg` (reg other
    than ra) in the reached code of a function, or one of its targets is not a word inside
    that function; if an interpreted address is not a function start; or if a host-switch
    kind is not one of HOST_SWITCH_KINDS, or its address is not a `j` (yield, exit) or a
    `jr ra` (resume) in the reached code of a function."""
    if entry % 4 or not image.contains(entry):
        raise ValueError("entry outside the image")
    host_switches = dict(host_switches or {})
    for addr, kind in sorted(host_switches.items()):
        if kind not in HOST_SWITCH_KINDS:
            raise ValueError("[[host_switch]] addr = %s: unknown kind %r (it is one of %s)"
                             % (_hex(addr), kind, ", ".join(HOST_SWITCH_KINDS)))
    resumes = {a for a, kind in host_switches.items() if kind == "resume"}
    stub_blocks = sorted((lo, hi) for lo, hi in stub_blocks)
    manual_jrs = {t.jr_addr for t in jump_tables}
    refused = set()
    while True:
        d, text_start, text_end = _descend(image, entry, manual, stub_blocks, refused,
                                           jump_tables, resumes)
        bad = {t.jr_addr for t in d.tables.values() if t.jr_addr not in manual_jrs
               and not all(text_start <= v < text_end for v in t.targets)}
        if not bad:
            break
        refused |= bad
    d.tail_calls(text_end)  # jumps_out against the final starts
    unreached = d.gaps(text_start, text_end)
    gap_at_end = {hi: lo for lo, hi in unreached}
    ordered = sorted(d.starts)
    funcs = []
    for i, addr in enumerate(ordered):
        block = next(((lo, hi) for lo, hi in stub_blocks if lo <= addr < hi), None)
        if block is not None:
            end = min(addr + STUB_SLOT, block[1])
        elif addr >= text_end:  # a code island in the data: just the code reached
            end = addr
            while end in d.code and (i + 1 == len(ordered) or end < ordered[i + 1]):
                end += 4
        else:
            end = min(ordered[i + 1], text_end) if i + 1 < len(ordered) else text_end
            end = min([end] + [lo for lo, _hi in stub_blocks if addr < lo])
            gap = gap_at_end.get(end)
            if gap is not None and gap > addr:
                end = gap  # trailing data before the next function is not part of this one
        name = "_start" if addr == entry else "func_%08X" % addr
        funcs.append(Function(addr, end - addr, name, set(d.starts[addr])))
    analysis = Analysis(text_start, text_end, funcs,
                        [d.tables[a] for a in sorted(d.tables)], unreached,
                        list(stub_blocks), image.sha256, sorted(set(interpreted)),
                        dict(sorted(host_switches.items())))
    _check_manual(analysis, d, jump_tables)
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


def _check_manual(analysis, d, jump_tables):
    """ValueError naming the first manual entry the result contradicts (see `discover`)."""
    starts = {f.addr for f in analysis.functions}
    for addr in analysis.interpreted:
        if addr not in starts:
            raise ValueError("[[interpreted]] addr = %s: not a function start" % _hex(addr))
    if not jump_tables and not analysis.host_switches:
        return
    by_addr = {f.addr: f for f in analysis.functions}
    owner = {addr: by_addr[start] for start, words in reached_words(analysis).items()
             for addr in words}
    for addr, kind in analysis.host_switches.items():
        entry = "[[host_switch]] addr = %s" % _hex(addr)
        if addr not in owner:
            raise ValueError(entry + ": not reached code of a function")
        insn = d.insn(addr)
        found = insn.mnemonic + (" ra" if insn.mnemonic == "jr" and insn.rs == RA else "")
        want = "jr ra" if kind == "resume" else "j"
        if found != want:
            raise ValueError(entry + ": the instruction there is %s, not a %s (kind %s)"
                             % (found, want, kind))
    for t in jump_tables:
        entry = "[[jump_table]] jr_addr = %s" % _hex(t.jr_addr)
        f = owner.get(t.jr_addr)
        if f is None:
            raise ValueError(entry + ": not reached code of a function")
        found = d.found_under_manual.get(t.jr_addr)
        if found is not None and all(analysis.text_start <= v < analysis.text_end
                                     for v in found.targets):
            raise ValueError(entry + ": the analysis resolves this jr by itself (table %s, %d "
                             "entries); a [[jump_table]] entry is only for a jr it cannot "
                             "resolve" % (_hex(found.table_addr), len(found.targets)))
        insn = d.insn(t.jr_addr)
        if insn.mnemonic != "jr" or insn.rs == RA:
            raise ValueError(entry + ": the instruction there is %s, not a jr on a register "
                             "other than ra" % (insn.mnemonic + (" ra" if insn.rs == RA else "")))
        for target in t.targets:
            if target % 4 or not f.addr <= target < f.addr + f.size:
                raise ValueError(entry + ": target %s is not a word inside its function "
                                 "%s-%s" % (_hex(target), _hex(f.addr), _hex(f.addr + f.size)))


def reached_words(analysis):
    """Function start -> the set of its reached words: the words of [addr, addr + size)
    outside every unreached range (a function's size spans interior unreached ranges) and
    every stub block (a stub function has no words in the file: the set is empty)."""
    unreached = sorted(analysis.unreached + analysis.stub_blocks)
    lows = [lo for lo, _hi in unreached]
    out = {}
    for f in sorted(analysis.functions, key=lambda f: f.addr):
        words = set()
        for a in range(f.addr, f.addr + f.size, 4):
            i = bisect.bisect_right(lows, a) - 1
            if i >= 0 and a < unreached[i][1]:
                continue
            words.add(a)
        out[f.addr] = words
    return out


def _hex(value):
    return "0x%08X" % value


def _toml_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_toml(analysis, path):
    """Write an Analysis as TOML: sorted by address, one stable layout."""
    lines = [
        "# Function discovery: python3 -m carnevil_tools functions <exe> -o <this file>",
        "# Addresses, sizes, names and sources only. See docs/analysis/functions.md.",
        "format = %d" % FORMAT,
        "exe_sha256 = " + _toml_string(analysis.exe_sha256),
        "text_start = " + _hex(analysis.text_start),
        "text_end = " + _hex(analysis.text_end),
    ]
    for lo, hi in sorted(analysis.stub_blocks):
        lines += ["", "[[stub_block]]", "start = " + _hex(lo), "end = " + _hex(hi)]
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
    for addr in sorted(analysis.interpreted):
        lines += ["", "[[interpreted]]", "addr = " + _hex(addr)]
    for addr, kind in sorted(analysis.host_switches.items()):
        lines += ["", "[[host_switch]]", "addr = " + _hex(addr), "kind = " + _toml_string(kind)]
    for lo, hi in sorted(analysis.unreached):
        lines += ["", "[[unreached]]", "start = " + _hex(lo), "end = " + _hex(hi)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_toml(path):
    """The Analysis in a TOML written by `write_toml`; ValueError if its `format` is
    missing or not FORMAT."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    found = data.get("format")
    if found != FORMAT:
        found = "with no format key" if found is None else "in format %r" % (found,)
        raise ValueError("%s: a functions TOML %s; only format = %d is read. Regenerate it: "
                         "python3 -m carnevil_tools functions <GAME.EXE> -o %s"
                         % (path, found, FORMAT, path))
    return Analysis(
        data["text_start"],
        data["text_end"],
        [Function(f["addr"], f["size"], f["name"], set(f["sources"]))
         for f in data.get("function", [])],
        [JumpTable(t["jr_addr"], t["table_addr"], list(t["targets"]))
         for t in data.get("jump_table", [])],
        [(u["start"], u["end"]) for u in data.get("unreached", [])],
        [(b["start"], b["end"]) for b in data.get("stub_block", [])],
        data["exe_sha256"],
        sorted(i["addr"] for i in data.get("interpreted", [])),
        {h["addr"]: h["kind"] for h in sorted(data.get("host_switch", []),
                                             key=lambda h: h["addr"])},
    )


# entry kind -> its keys (every entry also has a reason)
_MANUAL_KEYS = {"manual": ("addr",), "stub_block": ("start", "end"),
                "jump_table": ("jr_addr", "targets"), "interpreted": ("addr",),
                "host_switch": ("addr", "kind")}
_MANUAL_KINDS = tuple(_MANUAL_KEYS)


def _entry_text(kind, k, entry):
    """[[kind]] entry k (1-based), with its address keys, for an error message."""
    keys = ", ".join("%s = %s" % (key, _hex(v)) for key, v in entry.items()
                     if isinstance(v, int) and not isinstance(v, bool))
    return "[[%s]] entry %d%s" % (kind, k, " (%s)" % keys if keys else "")


def read_manual(path):
    """The Manual in a hand-maintained input such as config/game.manual.toml. Entries, each
    with a `reason` citing the evidence:
    - [[manual]] addr: a function start the analysis does not reach;
    - [[stub_block]] start, end (exclusive): words the program fills at run time;
    - [[jump_table]] jr_addr, targets (case k is targets[k]): the table of a `jr` the
      analysis cannot resolve (checked against the analysis by `discover`);
    - [[interpreted]] addr: a function start left to the interpreter;
    - [[host_switch]] addr, kind (one of HOST_SWITCH_KINDS): a transfer of the task switch
      the runtime takes over (checked against the analysis by `discover`)."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - set(_MANUAL_KINDS)
    if unknown:
        raise ValueError("%s: unknown keys %s" % (path, ", ".join(sorted(unknown))))
    for table, keys in _MANUAL_KEYS.items():
        for k, entry in enumerate(data.get(table, []), 1):
            if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                raise ValueError("%s: a [[%s]] entry has no reason (%s)"
                                 % (path, table, _entry_text(table, k, entry)))
            for key in keys:
                if key not in entry:
                    raise ValueError("%s: %s has no `%s`" % (path, _entry_text(table, k, entry),
                                                            key))
            extra = sorted(set(entry) - set(keys) - {"reason"})
            if extra:
                raise ValueError("%s: %s has unknown keys %s (it takes %s and reason)"
                                 % (path, _entry_text(table, k, entry), ", ".join(extra),
                                    ", ".join(keys)))
    blocks = [(b["start"], b["end"]) for b in data.get("stub_block", [])]
    for lo, hi in blocks:
        if lo % 4 or hi % 4 or lo >= hi:
            raise ValueError("%s: stub block 0x%08X-0x%08X is empty or unaligned"
                             % (path, lo, hi))
    tables = [JumpTable(t["jr_addr"], 0, list(t["targets"])) for t in data.get("jump_table", [])]
    for t in tables:
        if not t.targets:
            raise ValueError("%s: [[jump_table]] jr_addr = %s: no targets"
                             % (path, _hex(t.jr_addr)))
    if len({t.jr_addr for t in tables}) != len(tables):
        raise ValueError("%s: two [[jump_table]] entries for one jr_addr" % path)
    switches = {}
    for k, entry in enumerate(data.get("host_switch", []), 1):
        if entry["kind"] not in HOST_SWITCH_KINDS:
            raise ValueError("%s: %s: unknown kind %r (it is one of %s)"
                             % (path, _entry_text("host_switch", k, entry), entry["kind"],
                                ", ".join(HOST_SWITCH_KINDS)))
        if entry["addr"] in switches:
            raise ValueError("%s: two [[host_switch]] entries for addr = %s"
                             % (path, _hex(entry["addr"])))
        switches[entry["addr"]] = entry["kind"]
    return Manual([m["addr"] for m in data.get("manual", [])], blocks, tables,
                  [i["addr"] for i in data.get("interpreted", [])], switches)
