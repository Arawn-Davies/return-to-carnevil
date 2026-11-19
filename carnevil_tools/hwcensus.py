"""Hardware usage census: constant hardware addresses in the reached code of an image.

Results and method are documented in docs/analysis/hardware-usage.md. In short:

- The board's memory map and register names live in `carnevil_tools.board`; `REGIONS`
  and `classify` stay importable from here.
- `propagate` runs a forward constant propagation over each function's reached words: one
  `lui` feeds every later `addiu`/`ori` and every load/store that uses the register,
  across branches, merged at joins and reset at calls for the caller-saved registers. It
  records each memory access whose base register holds a known constant (or constant +
  register), every constant a register is given, and accesses through words loaded from
  fixed RAM locations; `resolve` turns the latter into hardware accesses where every store
  seen into the location holds the same hardware base. Constant arguments and return
  values are carried between functions (`_summaries`).
- What it does not follow is listed in the document.
"""
from dataclasses import dataclass, field
from typing import NamedTuple

from carnevil_tools import functions, mips
from carnevil_tools.board import REGIONS, classify, is_kseg01, region_of  # noqa: F401

_M32 = 0xFFFFFFFF


# Loader services with a constant result. Slot 0x110 (stub 0x800C419C in GAME.EXE, loader
# routine 0x8000E80C) finds the PCI device, writes its BAR0 = 0x08000000 and returns
# 0xA8000000 (0 when no device matches): that is the Voodoo base Glide keeps.
GAME_RETURNS = {0x800C419C: 0xA8000000}

# Constants in reached code at or above physical 0x08000000 that classify as unknown and
# are never used as an address (no load/store base, no jump target). Each is explained in
# docs/analysis/hardware-usage.md, "Constants that are not addresses".
NOT_ADDRESSES = {
    "GAME.EXE": [
        0x8D0D0000, 0x8D0D2CAD, 0x8FE30000, 0x8FE3297E, 0x9AA40000, 0x9AA47BF0,
        0x9C0A0000, 0x9C0A4D5B, 0x9D530000, 0x9D535756, 0x9F4B0000, 0x9F4BA339,
        0x9FAF0000, 0x9FAF1308, 0xAC710000, 0xAC71C590, 0xAD160000, 0xAD161652,
        0xAD510000, 0xAD517F33, 0xAF270000, 0xAF2798CC, 0xAFB00000, 0xAFB0E9BD,
        0xBA930000, 0xBA93A92D, 0xBADB0000, 0xBADBEEF1, 0xBC230000, 0xBC23D70A,
        0xBD270000, 0xBD276B66, 0xBD4C0000, 0xBD4CCCCD, 0xBE190000, 0xBE19999A,
        0xBE4C0000, 0xBE4CCCCD, 0xBEB00000, 0xBEB0C6F7, 0xBF100000, 0xBF10F977,
        0xBFFF0000, 0xBFFFF001, 0xBFFFFFEF,
    ],
    "loader": [],
}


# --- propagation ----------------------------------------------------------------------

@dataclass
class Access:
    site: int       # address of the load/store
    function: int   # start of the function holding it
    addr: int       # effective address (virtual, as formed)
    op: str         # "read", "write" or "cache"
    size: int       # bytes (0 for cache/pref)
    value: int | None = None  # constant stored, when the stored register holds one
    indexed: bool = False  # base was constant + a register: addr is the constant part
    via: tuple | None = None  # base loaded from this memory path (see `resolve`)


class Idx(NamedTuple):
    """A constant plus an unknown register (an indexed address)."""
    base: int


class Ptr(NamedTuple):
    """A word loaded from memory location `path`, plus `delta`. path is (A,) for the word
    at constant address A, or (A, k) for the word at offset k of the pointer stored at A.
    A location indexed by a register (table + i) is taken at entry 0.
    indexed: an unknown register was added to the loaded word."""
    path: tuple
    delta: int
    indexed: bool


@dataclass
class Indirect:
    """A load/store through a base loaded from memory: address = *path + offset."""
    site: int
    function: int
    path: tuple
    offset: int
    op: str
    size: int
    indexed: bool


class _Unknown:
    """The value of a store the census cannot value."""

    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()


@dataclass
class Store:
    """A store into memory location `path` of a constant, an Idx, a Ptr, or UNKNOWN."""
    site: int
    function: int
    path: tuple
    value: object


@dataclass
class Formed:
    sites: set = field(default_factory=set)  # instructions giving a register this value
    roles: set = field(default_factory=set)  # "base", "target", "store_value", "operand"
    consumers: set = field(default_factory=set)  # mnemonics that read it
    functions: set = field(default_factory=set)


@dataclass
class Census:
    accesses: list  # Access through a constant (or constant + register) base
    formed: dict    # constant value -> Formed
    indirect: list = field(default_factory=list)  # Indirect
    stores: list = field(default_factory=list)    # Store
    seeds: dict = field(default_factory=dict)     # function -> {arg register: value}
    returns: dict = field(default_factory=dict)   # function or call target -> v0 value
    initial: dict = field(default_factory=dict)   # path (A,) -> the image's word at A


_LOADS = {"lb": 1, "lbu": 1, "lh": 2, "lhu": 2, "lw": 4, "lwu": 4, "lwl": 4, "lwr": 4,
          "ll": 4, "lwc1": 4, "ld": 8, "ldl": 8, "ldr": 8, "lld": 8, "ldc1": 8}
_STORES = {"sb": 1, "sh": 2, "sw": 4, "swl": 4, "swr": 4, "sc": 4, "swc1": 4, "sd": 8,
           "sdl": 8, "sdr": 8, "scd": 8, "sdc1": 8}
_CACHE = frozenset(("cache", "pref"))
_FP_DATA = frozenset(("lwc1", "ldc1", "swc1", "sdc1"))  # rt is an FPR, not a GPR
# Registers a call may change (o32): at, v0-v1, a0-a3, t0-t9, k0-k1, ra.
_CALLER_SAVED = frozenset([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25,
                           26, 27, 31])
_CTI = frozenset(("branch", "branch_likely", "call", "call_reg", "jump", "jump_reg"))


def _value(state, reg):
    return 0 if reg == 0 else state.get(reg)


def _offset(value, imm):
    """value + imm for a known value (constant, Idx or Ptr); None stays None."""
    if isinstance(value, int):
        return (value + imm) & _M32
    if isinstance(value, Idx):
        return Idx((value.base + imm) & _M32)
    if isinstance(value, Ptr):
        return Ptr(value.path, (value.delta + imm) & _M32, value.indexed)
    return None


def _add(a, b):
    """a + b where either may be unknown (None): constant + register is an Idx when the
    constant is a KSEG0/KSEG1 address, a Ptr plus anything stays that Ptr, indexed."""
    if a == 0:
        return b
    if b == 0:
        return a
    if isinstance(a, int) and isinstance(b, int):
        return (a + b) & _M32
    for x, y in ((a, b), (b, a)):
        if isinstance(x, Ptr):
            return _offset(x, y) if isinstance(y, int) else Ptr(x.path, x.delta, True)
        if isinstance(x, Idx):
            return Idx((x.base + y) & _M32) if isinstance(y, int) else x
        if isinstance(x, int) and is_kseg01(x) and y is None:
            return Idx(x)
    return None


def _location(base, imm):
    """Memory path of the word at base + imm, or None: (A,) for a constant (or indexed)
    RAM address A, (A, k) for offset k from a pointer loaded from (A,)."""
    if isinstance(base, int) or isinstance(base, Idx):
        addr = ((base if isinstance(base, int) else base.base) + imm) & _M32
        return (addr,) if is_kseg01(addr) and classify(addr) == "ram" else None
    if isinstance(base, Ptr) and len(base.path) == 1:
        return base.path + (((base.delta + imm) & _M32),)
    return None


_POINTER_LOADS = frozenset(("lw", "ld"))


def _transfer(state, insn):
    """State after insn. Constants go through lui, addiu/daddiu, ori, xori, andi, addu and
    moves; constant + register gives an Idx; a word load from a RAM location (or from a
    pointer loaded from one) gives a Ptr; any other write forgets the register."""
    name = insn.mnemonic
    dest = mips.dest_reg(insn)
    if dest is None or dest == 0 or name in _FP_DATA:
        return state
    out = dict(state)
    rs, rt = _value(state, insn.rs), _value(state, insn.rt)
    value = None
    if name == "lui":
        value = (insn.imm << 16) & _M32
    elif name in ("addiu", "daddiu", "addi", "daddi"):
        value = _offset(rs, insn.imm)
    elif name in ("ori", "xori", "andi") and isinstance(rs, int):
        value = {"ori": rs | insn.imm, "xori": rs ^ insn.imm, "andi": rs & insn.imm}[name]
    elif name in ("addu", "daddu"):
        value = _add(rs, rt)
    elif name == "or" and 0 in (insn.rs, insn.rt):
        value = rt if insn.rs == 0 else rs
    elif name == "or" and isinstance(rs, int) and isinstance(rt, int):
        value = rs | rt
    elif name in _POINTER_LOADS:
        path = _location(rs, insn.imm)
        if path is not None:
            value = Ptr(path, 0, False)
    if value is None:
        out.pop(dest, None)
    else:
        out[dest] = value
    return out


def _base(value):
    return value.base if isinstance(value, Idx) else value


def _stepping(x, y):
    """Two addresses (constant or table + register) in the same hardware region."""
    if not (isinstance(x, (int, Idx)) and isinstance(y, (int, Idx))):
        return False
    bx, by = _base(x), _base(y)
    return _hardware(bx) and region_of(bx) == region_of(by)


def _meet(a, b):
    """Registers both states agree on. A hardware base (or memory pointer) on one path and
    null on the other keeps the base: dereferencing the null would fault, so every access
    that executes sees the base. A constant C on one path and C + register on the other
    (a loop over a table, first with index 0) is C + register. Two addresses in the same
    hardware region, or two offsets from the same loaded pointer, widen to the lower one
    plus a register (a loop stepping through registers or a buffer)."""
    out = {}
    for r in a.keys() | b.keys():
        x, y = a.get(r), b.get(r)
        if x == y:
            out[r] = x
        elif isinstance(x, Idx) and y == x.base or isinstance(y, Idx) and x == y.base:
            out[r] = x if isinstance(x, Idx) else y  # table + i, met first with i = 0
        elif isinstance(x, Ptr) and isinstance(y, Ptr) and x.path == y.path:
            low = min(x.delta, y.delta)  # a pointer stepping on; widen once, not again
            if not (x.indexed and low != x.delta):
                out[r] = Ptr(x.path, low, True)
        elif _stepping(x, y):
            low = min(_base(x), _base(y))  # a hardware address stepping on; widen once
            if not (isinstance(x, Idx) and low != x.base):
                out[r] = Idx(low)
        elif x == 0 and _summarisable(y):
            out[r] = y
        elif y == 0 and _summarisable(x):
            out[r] = x
    return out


class _Function:
    def __init__(self, image, start, words, tables, returns):
        self.image, self.start, self.words, self.tables = image, start, words, tables
        self.returns = returns
        self.insn = mips.Decoder(image).insn

    def successors(self, addr, state):
        """(successor address, state on entry) pairs of the instruction at addr. A control
        transfer's delay slot is applied here, so its successors are the targets."""
        insn = self.insn(addr)
        kind = insn.kind
        if kind == "unknown" or insn.mnemonic == "eret":
            return []
        if kind not in _CTI:
            return [(addr + 4, _transfer(state, insn))]
        after_branch = _transfer(state, insn)
        if addr + 4 not in self.words:
            return []
        taken = _transfer(after_branch, self.insn(addr + 4))
        if kind in ("branch", "branch_likely"):
            out = [(insn.target, taken)]
            if not mips.always_taken(insn):
                out.append((addr + 8, taken if kind == "branch" else after_branch))
            return out
        if kind in ("call", "call_reg"):
            after = {r: v for r, v in taken.items() if r not in _CALLER_SAVED}
            if kind == "call" and insn.target in self.returns:
                after[2] = self.returns[insn.target]  # v0
            return [(addr + 8, after)]
        if kind == "jump":
            return [(insn.target, taken)]
        table = self.tables.get(addr)
        return [(t, taken) for t in table.targets] if table and insn.rs != 31 else []

    def solve(self, entry):
        states = {self.start: dict(entry)}
        pending = [self.start]
        while pending:
            addr = pending.pop()
            for succ, state in self.successors(addr, states[addr]):
                if succ not in self.words:
                    continue
                old = states.get(succ)
                new = state if old is None else _meet(old, state)
                if old is None or new != old:
                    states[succ] = new
                    pending.append(succ)
        return states


def _reads(insn):
    reads = mips.reads(insn)
    return set() if reads is None else reads - {0}


def _record(fn, addr, insn, state, out):
    """Note what insn does with the known values in state (the state before it)."""
    name = insn.mnemonic
    base = _value(state, insn.rs)
    memory = name in _LOADS or name in _STORES or name in _CACHE
    if base is not None and memory:
        if name in _LOADS:
            op, size = "read", _LOADS[name]
        elif name in _STORES:
            op, size = "write", _STORES[name]
        else:
            op, size = "cache", 0
        stored = None if op != "write" or name in _FP_DATA else _value(state, insn.rt)
        if isinstance(base, Ptr):
            out.indirect[(addr, base)] = Indirect(
                addr, fn.start, base.path, (base.delta + insn.imm) & _M32, op, size,
                base.indexed)
        else:
            indexed = isinstance(base, Idx)
            ea = ((base.base if indexed else base) + insn.imm) & _M32
            value = stored if isinstance(stored, int) else None
            out.accesses[(addr, ea)] = Access(addr, fn.start, ea, op, size, value, indexed)
        path = _location(base, insn.imm) if op == "write" else None
        if path is not None:
            if size < 4 or not isinstance(stored, (int, Ptr, Idx)) or (
                    isinstance(stored, Ptr) and stored.indexed):
                stored = UNKNOWN  # a store the census cannot value: it disqualifies the path
            out.stores[(addr, path, stored)] = Store(addr, fn.start, path, stored)
    formed = out.formed
    for reg in _reads(insn):
        v = _value(state, reg)
        if not isinstance(v, int):
            continue
        f = formed.setdefault(v, Formed())
        f.consumers.add(name)
        if reg == insn.rs and (name in _LOADS or name in _STORES or name in _CACHE):
            f.roles.add("base")
        elif name in _STORES and reg == insn.rt:
            f.roles.add("store_value")
        elif insn.kind in ("jump_reg", "call_reg"):
            f.roles.add("target")
        else:
            f.roles.add("operand")
    dest = mips.dest_reg(insn)
    move = name in ("addu", "daddu", "or") and 0 in (insn.rs, insn.rt)
    if dest and not move and name not in _FP_DATA:
        after = _transfer(state, insn)
        if isinstance(after.get(dest), int):
            f = formed.setdefault(after[dest], Formed())
            f.sites.add(addr)
            f.functions.add(fn.start)


class _Collect:
    def __init__(self):
        self.accesses, self.formed, self.indirect, self.stores = {}, {}, {}, {}


_ARGS = (4, 5, 6, 7)  # a0-a3
SUMMARY_ROUNDS = 8


def _summarisable(value):
    """Values carried across calls: hardware constants and pointers loaded from memory."""
    if isinstance(value, int):
        return _hardware(value)
    return isinstance(value, Ptr) and not value.indexed


def _agree(values):
    """The one value all of `values` share (nulls aside for returns), or None."""
    distinct = set(values)
    if len(distinct) == 1:
        value = next(iter(distinct))
        return value if _summarisable(value) else None
    return None


def _summaries(image, analysis, tables, returns):
    """Entry states (constant arguments) and return values, to a fixpoint.

    A function gets an argument register at entry when every direct call and tail jump to
    it passes the same hardware constant or memory pointer in that register, and its
    address is never taken (no "pointer" source), so no unseen caller exists. A function
    gets a return value when every `jr ra` returns the same such value in v0, or null."""
    funcs = list(functions.reached_words(analysis).items())
    starts = {f.addr for f in analysis.functions}
    taken = {f.addr for f in analysis.functions if "pointer" in f.sources}
    seeds, rets = {}, dict(returns)
    for _round in range(SUMMARY_ROUNDS):
        calls, results = {}, {}
        for start, words in funcs:
            if start not in words:
                continue
            fn = _Function(image, start, words, tables, rets)
            states = fn.solve(seeds.get(start, {}))
            for addr, state in states.items():
                insn = fn.insn(addr)
                if insn.kind not in ("call", "jump", "jump_reg") or addr + 4 not in words:
                    continue
                after = _transfer(_transfer(state, insn), fn.insn(addr + 4))
                if insn.kind == "jump_reg":
                    if insn.rs == 31:
                        results.setdefault(start, []).append(after.get(2))
                    continue
                if insn.target in starts and not (insn.kind == "jump" and insn.target in words):
                    calls.setdefault(insn.target, []).append(after)
        new_seeds = {}
        for target, sites in calls.items():
            if target in taken:
                continue
            entry = {}
            for reg in _ARGS:
                value = _agree(site.get(reg) for site in sites)
                if value is not None:
                    entry[reg] = value
            if entry:
                new_seeds[target] = entry
        new_rets = dict(returns)
        for start, values in results.items():
            value = _agree(v for v in values if v != 0)
            if value is not None:
                new_rets[start] = value
        if new_seeds == seeds and new_rets == rets:
            break
        seeds, rets = new_seeds, new_rets
    return seeds, rets


def propagate(image, analysis, returns=None, interprocedural=True):
    """Accesses through known bases, and constants formed, over the reached code.
    returns: call target -> the value it is known to return in v0 (for targets outside
    the image). With interprocedural, constant arguments and return values found by
    `_summaries` are used too."""
    tables = {t.jr_addr: t for t in analysis.jump_tables}
    returns = dict(returns or {})
    seeds, rets = (_summaries(image, analysis, tables, returns) if interprocedural
                   else ({}, returns))
    out = _Collect()
    for start, words in functions.reached_words(analysis).items():
        if start not in words:
            continue
        fn = _Function(image, start, words, tables, rets)
        states = fn.solve(seeds.get(start, {}))
        for addr in sorted(states):
            state = states[addr]
            insn = fn.insn(addr)
            _record(fn, addr, insn, state, out)
            if insn.kind in _CTI and addr + 4 in words:
                _record(fn, addr + 4, fn.insn(addr + 4), _transfer(state, insn), out)
    indirect = [out.indirect[k] for k in sorted(out.indirect, key=lambda k: k[0])]
    stores = [out.stores[k] for k in sorted(out.stores, key=lambda k: (k[0], k[1]))]
    words = {ia.path[0] for ia in indirect} | {s.path[0] for s in stores}
    words |= {s.value.path[0] for s in stores if isinstance(s.value, Ptr)}
    initial = {(a,): image.word(a) for a in words if a % 4 == 0 and image.contains(a)}
    return Census([out.accesses[k] for k in sorted(out.accesses)], out.formed, indirect,
                  stores, seeds, rets, initial)


def _single(values, accept):
    """The one non-null value of a set, if there is exactly one and accept() takes it."""
    rest = values - {0}
    if len(rest) == 1:
        value = next(iter(rest))
        if accept(value):
            return value
    return None


def ram_pointers(c):
    """Memory path -> the RAM address it holds, when every store the census sees into the
    path stores that address (a table + register counts as the table, entry 0) or null.
    A store of anything else (UNKNOWN, a loaded pointer) disqualifies the path."""
    seen = {}
    for s in c.stores:
        value = s.value.base if isinstance(s.value, Idx) else s.value
        seen.setdefault(s.path, set()).add(value if isinstance(value, int) else UNKNOWN)
    out = {}
    for path, values in seen.items():
        if UNKNOWN in values:
            continue
        value = _single(values, lambda v: is_kseg01(v) and classify(v) == "ram")
        if value is not None:
            out[path] = value
    return out


def _canonical(path, rams):
    """(A, k) -> (P + k,) when (A,) holds the RAM address P."""
    if len(path) == 2 and (path[0],) in rams:
        return (((rams[(path[0],)] + path[1]) & _M32),)
    return path


def holders(c):
    """(holders, ram_pointers). holders: memory path -> the hardware base it holds, when
    every store this census sees into the path, and the image's initial word there, is the
    same non-RAM KSEG0/KSEG1 constant (or constant + register, or null), directly or by
    copying a word loaded from another holder. Any store into the path the census cannot
    value (UNKNOWN: a register it does not know, a partial-word store, an FPU store) or a
    copy from a location that is not a holder disqualifies the path. A path through a
    pointer to a RAM structure is taken as the structure's own address (`ram_pointers`).
    Stores through bases the census does not know cannot be seen at all and are not
    accounted for."""
    rams = ram_pointers(c)
    direct = {path: {value} for path, value in c.initial.items()}  # initialised data
    copies = []
    for s in c.stores:
        dst = _canonical(s.path, rams)
        if isinstance(s.value, Ptr):
            copies.append((_canonical(s.value.path, rams), s.value.delta, dst))
            direct.setdefault(dst, set())
        else:
            direct.setdefault(dst, set()).add(s.value)

    def accept(value):
        return _hardware(value.base if isinstance(value, Idx) else value)

    known = {}
    for _round in range(64):  # least fixpoint: a copy counts only from a proven holder
        seen = {path: set(values) for path, values in direct.items()}
        for src, delta, dst in copies:
            seen[dst].add(_offset(known[src], delta) if src in known else UNKNOWN)
        new = {}
        for path, values in seen.items():
            if UNKNOWN in values:
                continue
            value = _single(values, accept)
            if value is not None:
                new[path] = value
        if new == known:
            break
        known = new
    return known, rams


def _hardware(value):
    return is_kseg01(value) and classify(value) not in ("ram", "unknown")


def resolve(c):
    """Indirect accesses whose base path holds a known hardware base, as Accesses with
    `via` set to the path."""
    known, rams = holders(c)
    out = []
    for ia in c.indirect:
        base = known.get(_canonical(ia.path, rams))
        if base is not None:
            indexed = ia.indexed or isinstance(base, Idx)
            addr = (base.base if isinstance(base, Idx) else base) + ia.offset
            out.append(Access(ia.site, ia.function, addr & _M32, ia.op, ia.size, None,
                              indexed, ia.path))
    return out


def all_accesses(c):
    """Direct accesses and resolved indirect ones, by site."""
    return sorted(c.accesses + resolve(c), key=lambda a: (a.site, a.addr))


def constant_addresses(image, analysis):
    """KSEG0/KSEG1 address -> the code addresses forming it, from constants in code only:
    each value an `addiu`/`ori` (or other constant arithmetic) builds on a `lui`, each
    effective address of a load/store/cache through such a constant base (constant +
    register counts at its constant part), and each such constant used as a jump target.
    Only KSEG0/KSEG1 values are returned: no value at or above 0x80000000 can be built
    without a `lui`, so these are the lui-formed ones; small integers are left out.

    Not included: addresses reached through bases held in memory, passed between
    functions, or returned by loader services. These are not constants in the code.
    `hardware_accesses` is the function to size the hardware from."""
    c = propagate(image, analysis, interprocedural=False)
    out = {}
    for a in c.accesses:
        if is_kseg01(a.addr):
            out.setdefault(a.addr, set()).add(a.site)
    for value, f in c.formed.items():
        if not is_kseg01(value):
            continue
        sites = {s for s in f.sites if mips.decode(image.word(s), s).mnemonic != "lui"}
        if "target" in f.roles:
            sites |= f.sites
        if sites:
            out.setdefault(value, set()).update(sites)
    return {v: sorted(s) for v, s in sorted(out.items())}


def hardware_accesses(image, analysis, returns=None):
    """Every load/store/cache access to a hardware region (KSEG0/KSEG1, neither RAM nor
    unknown) that the census can attribute, as Access records sorted by site. These are
    direct (constant, or constant + register) accesses and accesses through bases held
    in memory under the `holders` rule, with interprocedural summaries applied. returns:
    known results of calls out of the image; for GAME.EXE pass `GAME_RETURNS`. The
    document's tables are built from this."""
    c = propagate(image, analysis, returns)
    return [a for a in all_accesses(c) if _hardware(a.addr)]


@dataclass
class CallArgs:
    site: int       # the call instruction
    function: int   # the function holding it
    target: int
    args: dict      # a0..a3 register number -> known value (int, Idx or Ptr)


def call_arguments(image, analysis, targets, returns=None):
    """The known argument registers at each direct call to one of `targets`, after the
    delay slot, with the census's interprocedural summaries applied."""
    tables = {t.jr_addr: t for t in analysis.jump_tables}
    seeds, rets = _summaries(image, analysis, tables, dict(returns or {}))
    targets = set(targets)
    out = []
    for start, words in functions.reached_words(analysis).items():
        if start not in words:
            continue
        fn = _Function(image, start, words, tables, rets)
        states = fn.solve(seeds.get(start, {}))
        for addr in sorted(states):
            insn = fn.insn(addr)
            if insn.kind != "call" or insn.target not in targets or addr + 4 not in words:
                continue
            after = _transfer(_transfer(states[addr], insn), fn.insn(addr + 4))
            out.append(CallArgs(addr, start, insn.target,
                                {r: after[r] for r in _ARGS if r in after}))
    return out


IOCTL_STUB = 0x800C4154  # GAME.EXE's stub-block entry for loader slot 0x0C8, ioctl(fd, cmd, arg)


def ioctl_calls(image, analysis, returns=None):
    """(fd, cmd) -> count over the ioctl calls with a constant descriptor and command, and
    the list of CallArgs whose descriptor or command is not a constant."""
    counts, other = {}, []
    for call in call_arguments(image, analysis, [IOCTL_STUB], returns):
        fd, cmd = call.args.get(4), call.args.get(5)
        if isinstance(fd, int) and isinstance(cmd, int):
            counts[(fd, cmd)] = counts.get((fd, cmd), 0) + 1
        else:
            other.append(call)
    return counts, other
