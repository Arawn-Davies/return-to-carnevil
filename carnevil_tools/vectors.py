"""Instruction test cases for the recompiler, and the results MAME recorded for them.

A case is a small program, not one word. It is placed at CODE_BASE, entered with
ra = RA, and is finished when the PC reaches RA. Its memory operands lie in the scratch
page SCRATCH .. SCRATCH + 0xFFF. Every program ends in `jr ra` and `nop`. A branch case
sets a marker register differently on the two paths (and another in the delay slot), so no
PC needs comparing. Calls (`jal`, `jalr`, `j`, `jr`) go to a callee or label inside the
same program.

State: 32 general registers (low 32 bits), hi, lo, 32 FP register halves of 32 bits under
FR = 0 (a double in even register n is half n, low word, and half n + 1, high word), FCSR as
`cfc1 $31` reads it (condition bits in place), and the scratch page as aligned address ->
word. Words absent from `mem` are 0, and zero words are not stored.

`tools/mame/insnvec.lua` runs the cases in MAME (`write_cases` makes its input); `merge`
writes `tests/vectors/insn.json` from its output, storing for each case only the fields of
the after-state that differ from the before-state; `load` reads that file back.
"""
import json
import random
import struct
from dataclasses import dataclass, field

from carnevil_tools import mips

CODE_BASE = 0x80100000
RA = 0x80100F00
SCRATCH = 0x80200000
SCRATCH_SIZE = 0x1000
FCSR_CC_MASK = 0xFE800000  # FCC0 is bit 23, FCC1..7 are bits 25..31
_FCC_SHIFT = (23, 25, 26, 27, 28, 29, 30, 31)

_M32 = 0xFFFFFFFF

# The reached census of docs/analysis/functions.md ("Census of reached code"), by count.
CENSUS = tuple("""
addiu lw lui addu sw sll jal lwc1 beq swc1 j bne sh jr ori lhu mul.s andi slti add.s lh
sub.s sra ldc1 mtc1 sdc1 or mfc1 lbu bc1f c.lt.s subu and slt cvt.s.w mov.s beql bnel
blez sltiu div.s sb srl c.le.s bc1t sltu c.eq.s bgez neg.s cvt.d.s trunc.w.s bc1tl
cvt.s.d movz mult xor bc1fl mflo madd.s jalr lb add.d cvt.d.w bltz mul.d break bgtz
movn nor mfhi xori c.lt.d sllv c.le.d bltzl sub.d cache mov.d c.eq.d div msub.s div.d
bgezl addi trunc.w.d madd.d nmsub.s movf blezl divu movt bgtzl ctc1 multu neg.d nmadd.s
recip.s sqrt.s
""".split())

# Results MAME gives that are not a hardware fact. Later tasks still match them, so the port
# and its oracle agree; this list says where a real board might differ.
# What the R5000 does in these cases is not verified here against its documentation.
_DIV0 = ("division by zero: MAME leaves hi and lo unchanged; the R5000's result is not "
         "verified (MIPS leaves it undefined)")
_TRUNC = ("trunc of a NaN or of a value outside int32: MAME returns the x86 conversion result "
          "0x80000000; what the R5000 does (result or FPE) is not verified")
_MADD = ("madd family: MAME rounds the product to the format, then the sum (two roundings), "
         "and for nmadd/nmsub negates the rounded sum; a single rounding gives other bits here; "
         "whether the R5000 rounds once or twice is not verified")
MAME_SPECIFIC = {
    "div.edge.00000000.00000000": _DIV0,
    "div.edge.00000001.00000000": _DIV0,
    "div.edge.FFFFFFFF.00000000": _DIV0,
    "div.edge.80000000.00000000": _DIV0,
    "div.edge.7FFFFFFF.00000000": _DIV0,
    "divu.edge.00000000.00000000": _DIV0,
    "divu.edge.00000001.00000000": _DIV0,
    "divu.edge.FFFFFFFF.00000000": _DIV0,
    "divu.edge.80000000.00000000": _DIV0,
    "divu.edge.7FFFFFFF.00000000": _DIV0,
    "trunc.w.s.edge.FFFFFFFF": _TRUNC,  # 4294967295 rounds to 2^32 as a single
    "trunc.w.s.edge.80000000": _TRUNC,  # 2^31
    "trunc.w.s.edge.7FFFFFFF": _TRUNC,  # rounds to 2^31 as a single
    "trunc.w.s.edge.3e9": _TRUNC,
    "trunc.w.s.edge.-3e9": _TRUNC,
    "trunc.w.s.edge.nan": _TRUNC,
    "trunc.w.d.edge.FFFFFFFF": _TRUNC,
    "trunc.w.d.edge.80000000": _TRUNC,
    "trunc.w.d.edge.3e9": _TRUNC,
    "trunc.w.d.edge.-3e9": _TRUNC,
    "trunc.w.d.edge.nan": _TRUNC,
    # the recorded madd-family cases whose fused and unfused results differ (MAME: unfused)
    "nmadd.s.5": _MADD,
    "msub.s.9": _MADD,
    "nmsub.s.11": _MADD,
}
_RECIP = ("recip.s: MAME computes 1.0f / x, correctly rounded; the R5000's accuracy for recip "
          "is not verified")
MAME_SPECIFIC.update({f"recip.s.{i}": _RECIP for i in range(12)})
_FS = ("FCSR.FS = 1: MAME does not model FS (it keeps denormal results and operands); the "
       "R5000 documents flushing them to zero (runtime/core/fpu.c, RT_HONOUR_FS)")
# MAME_SPECIFIC gains the FS = 1 cases below, after _FS_CASES


# Cases that are not run in MAME (Case.no_mame), so have no recorded result, and why.
NO_MAME = {
    "break": "break raises an exception in MAME; it cannot run to ra",
    "div.edge.80000000.FFFFFFFF": "INT_MIN / -1: MAME divides on the host, where x86 idiv "
                                  "faults and SIGFPE kills MAME",
}


@dataclass
class State:
    r: list
    hi: int
    lo: int
    fpr: list
    fcsr: int
    mem: dict = field(default_factory=dict)


@dataclass
class Case:
    name: str
    words: list
    before: State
    no_mame: bool = False  # not run in MAME: see NO_MAME


# --- assembler ----------------------------------------------------------------------------

def _templates():
    """Census mnemonic -> (fixed bits, form); form "I" 16-bit immediate, "J" 26-bit index,
    "R" no immediate. Built from the decoder's own tables, so the two cannot disagree."""
    t = {}
    for op, (name, _kind, form) in mips._PRIMARY.items():
        t[name] = (op << 26, "J" if form == "J" else "I")
    for funct, (name, _kind, _zero) in mips._SPECIAL.items():
        t[name] = (funct, "R")
    t["movf"] = (mips._MOVCI, "R")
    t["movt"] = (mips._MOVCI | 1 << 16, "R")
    for rt, (name, _kind, _form) in mips._REGIMM.items():
        t[name] = (1 << 26 | rt << 16, "I")
    for rs, name in mips._COP1_MOVES.items():
        t[name] = (0x11 << 26 | rs << 21, "R")
    for bits, (name, _kind) in mips._COP1_BC.items():
        t[name] = (0x11 << 26 | 8 << 21 | bits << 16, "I")
    for code, fmt in mips._FMT.items():
        cop1 = 0x11 << 26 | code << 21
        if fmt in "sd":
            for funct, op in list(mips._FP_BINARY.items()) + list(mips._FP_UNARY.items()):
                t[f"{op}.{fmt}"] = (cop1 | funct, "R")
            for i, cond in enumerate(mips._FP_COND):
                t[f"c.{cond}.{fmt}"] = (cop1 | 0x30 + i, "R")
        if fmt != "s":
            t["cvt.s." + fmt] = (cop1 | 0x20, "R")
        if fmt != "d":
            t["cvt.d." + fmt] = (cop1 | 0x21, "R")
    for funct, op in mips._COP1X_MADD.items():
        for i, fmt in enumerate("sd"):
            t[f"{op}.{fmt}"] = (0x13 << 26 | funct | i, "R")
    return {name: t[name] for name in CENSUS + ("cfc1",)}


_TEMPLATES = _templates()
_FIELDS = {"rs": 21, "rt": 16, "rd": 11, "sa": 6}
_ALIASES = {"fs": "rd", "ft": "rt", "fd": "sa", "fr": "rs", "base": "rs", "op": "rt"}


def assemble(mnemonic, **fields):
    """The word for a census mnemonic (or `cfc1`, which reads FCSR back). Raw fields as `mips.Insn` holds them (rs, rt, rd, sa,
    imm: for branches the signed word offset, for j/jal the 26-bit index), or the names
    fs/ft/fd/fr (COP1 and COP1X), base, op (cache), cc (FP condition code), code (the 20-bit
    break code), target (j/jal: the address jumped to). The inverse of `mips.decode`."""
    if mnemonic not in _TEMPLATES:
        raise ValueError(f"{mnemonic} is not in the reached census")
    word, form = _TEMPLATES[mnemonic]
    raw = {"rs": 0, "rt": 0, "rd": 0, "sa": 0}
    imm = fields.pop("imm", 0)
    if "target" in fields:
        imm = (fields.pop("target") >> 2) & 0x03FFFFFF
    if "code" in fields:
        word |= (fields.pop("code") & 0xFFFFF) << 6
    cc = fields.pop("cc", None)
    for name, value in fields.items():
        name = _ALIASES.get(name, name)
        if name not in raw:
            raise ValueError(f"{mnemonic}: unknown field {name}")
        if not 0 <= value < 32:
            raise ValueError(f"{mnemonic}: {name}={value} is not a 5-bit field")
        raw[name] |= value
    if cc is not None:
        if mnemonic.startswith("c."):
            raw["sa"] |= cc << 2
        else:
            raw["rt"] |= cc << 2
    for name, shift in _FIELDS.items():
        word |= (raw[name] & 31) << shift
    if form == "I":
        if not -0x8000 <= imm <= 0xFFFF:
            raise ValueError(f"{mnemonic}: imm {imm} is not 16 bits")
        word |= imm & 0xFFFF
    elif form == "J":
        word |= imm & 0x03FFFFFF
    elif imm:
        raise ValueError(f"{mnemonic} takes no immediate")
    return word


_JR_RA = assemble("jr", rs=31)
_NOP = 0


def _prog(*words):
    return list(words) + [_JR_RA, _NOP]


# --- operand values -----------------------------------------------------------------------

_INT_EDGES = (0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF)
_MORE_EDGES = _INT_EDGES + (0xFFFF, 0x8000, 0x7FFF, 0x10000, 0xFFFF8000)
_POOL = tuple(range(1, 31))  # registers a case may use: not zero, not ra


def _int(rng):
    return rng.choice(_MORE_EDGES) if rng.random() < 0.15 else rng.getrandbits(32)


def _nonzero(rng):
    while True:
        v = _int(rng)
        if v:
            return v


def _single(rng, positive=False):
    """A finite normal single (bits) with a moderate exponent, so results stay normal."""
    sign = 0 if positive else rng.getrandbits(1)
    return sign << 31 | (127 + rng.randint(-30, 30)) << 23 | rng.getrandbits(23)


def _double(rng):
    """A finite normal double as (low word, high word), moderate exponent."""
    bits = rng.getrandbits(1) << 63 | (1023 + rng.randint(-100, 100)) << 52 | rng.getrandbits(52)
    return bits & _M32, bits >> 32


def _f32_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _f64_words(value):
    return struct.unpack("<II", struct.pack("<d", value))


def _fcsr_cc(rng):
    bits = rng.getrandbits(8)
    return sum(1 << s for i, s in enumerate(_FCC_SHIFT) if bits >> i & 1)


def _state(rng):
    r = [0] + [_int(rng) for _ in range(30)] + [RA]
    fpr = [_single(rng) for _ in range(32)]
    return State(r, rng.getrandbits(32), rng.getrandbits(32), fpr, _fcsr_cc(rng))


def _regs(rng, n, pool=_POOL):
    return rng.sample(pool, n)


def _set_double(state, reg, words):
    state.fpr[reg], state.fpr[reg + 1] = words


# --- case generators ----------------------------------------------------------------------
# Each takes (rng, i) and returns (words, before); i is the index of the random case.

def _alu3(name):
    def gen(rng, i):
        s = _state(rng)
        rd, = _regs(rng, 1)
        rs, rt = rng.choice((0,) + _POOL), rng.choice((0,) + _POOL)
        if name in ("movz", "movn") and i % 2 == 0:
            s.r[rt] = 0 if rt else s.r[rt]
        return _prog(assemble(name, rd=rd, rs=rs, rt=rt)), s
    return gen


def _shift(name):
    def gen(rng, i):
        s = _state(rng)
        rd, rt = _regs(rng, 2)
        sa = {0: 0, 1: 31}.get(i, rng.randrange(32))
        return _prog(assemble(name, rd=rd, rt=rt, sa=sa)), s
    return gen


def _sllv(rng, i):
    s = _state(rng)
    rd, rs, rt = _regs(rng, 3)
    return _prog(assemble("sllv", rd=rd, rs=rs, rt=rt)), s


def _imm(name):
    def gen(rng, i):
        s = _state(rng)
        rt, rs = _regs(rng, 2)
        if name in ("andi", "ori", "xori"):
            imm = rng.getrandbits(16)
        else:
            imm = rng.choice((0, 1, -1, 0x7FFF, -0x8000)) if i < 3 else rng.randint(-0x8000, 0x7FFF)
        if name == "addi":  # keep it from overflowing: the R5000 traps, MAME does not
            while not -0x80000000 <= _signed(s.r[rs]) + imm <= 0x7FFFFFFF:
                s.r[rs] = _int(rng)
        return _prog(assemble(name, rt=rt, rs=rs, imm=imm)), s
    return gen


def _lui(rng, i):
    s = _state(rng)
    rt, = _regs(rng, 1)
    return _prog(assemble("lui", rt=rt, imm=rng.getrandbits(16))), s


def _muldiv(name):
    def gen(rng, i):
        s = _state(rng)
        rs, rt = _regs(rng, 2)
        if name in ("div", "divu"):
            s.r[rt] = _nonzero(rng)
        return _prog(assemble(name, rs=rs, rt=rt)), s
    return gen


def _mfhilo(name):
    def gen(rng, i):
        s = _state(rng)
        rd, = _regs(rng, 1)
        return _prog(assemble(name, rd=rd)), s
    return gen


def _movcc(name):
    def gen(rng, i):
        s = _state(rng)
        rd, rs = _regs(rng, 2)
        return _prog(assemble(name, rd=rd, rs=rs, cc=i % 8)), s
    return gen


_MEM_SIZE = {"lb": 1, "lbu": 1, "sb": 1, "lh": 2, "lhu": 2, "sh": 2, "lw": 4, "sw": 4,
             "lwc1": 4, "swc1": 4, "ldc1": 8, "sdc1": 8, "cache": 4}


def _mem(name):
    """Loads, stores and cache: an address in the scratch page, the words around it set."""
    size = _MEM_SIZE[name]

    def gen(rng, i):
        s = _state(rng)
        base, rt = _regs(rng, 2)
        if name in ("ldc1", "sdc1"):
            rt = rng.randrange(0, 32, 2)
        elif name in ("lwc1", "swc1"):
            rt = rng.randrange(32)
        elif name == "cache":
            rt = rng.choice((0x00, 0x01, 0x10, 0x11, 0x15, 0x19))
        ea = SCRATCH + 8 + rng.randrange(0, SCRATCH_SIZE - 16, size)
        imm = rng.randrange(-0x400, 0x400)
        s.r[base] = (ea - imm) & _M32
        for addr in range(ea & ~3, (ea & ~3) + max(size, 4), 4):
            s.mem[addr] = rng.getrandbits(32) | 1
        for _ in range(3):
            s.mem[SCRATCH + rng.randrange(0, SCRATCH_SIZE, 4)] = rng.getrandbits(32) | 1
        return _prog(assemble(name, rs=base, rt=rt, imm=imm)), s
    return gen


# Branch layout, in words: 0 branch to 5; 1 delay slot: ori A,zero,1; 2 not taken: ori
# M,zero,2; 3 jr ra; 4 nop; 5 taken: ori M,zero,3; 6 jr ra; 7 nop.
def _branch_body(branch, a, m):
    return [branch, assemble("ori", rt=a, imm=1), assemble("ori", rt=m, imm=2),
            _JR_RA, _NOP, assemble("ori", rt=m, imm=3), _JR_RA, _NOP]


_SIGN_CYCLE = (0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF)


def _branch(name):
    def gen(rng, i):
        s = _state(rng)
        rs, rt, a, m = _regs(rng, 4)
        if name.startswith("bc1"):
            word = assemble(name, cc=i % 8, imm=4)
        elif name in ("beq", "bne", "beql", "bnel"):
            if i % 2 == 0:
                s.r[rt] = s.r[rs]
            word = assemble(name, rs=rs, rt=rt, imm=4)
        else:
            if i < len(_SIGN_CYCLE):
                s.r[rs] = _SIGN_CYCLE[i]
            word = assemble(name, rs=rs, imm=4)
        return _branch_body(word, a, m), s
    return gen


def _j(rng, i):
    s = _state(rng)
    a, m = _regs(rng, 2)
    return _branch_body(assemble("j", target=CODE_BASE + 20), a, m), s


def _jr(rng, i):
    s = _state(rng)
    x, a, m = _regs(rng, 3)
    s.r[x] = CODE_BASE + 20
    return _branch_body(assemble("jr", rs=x), a, m), s


# Call layout: 0 the call to 6; 1 delay slot: ori A,zero,1; 2 addu L,ra,zero (the link);
# 3 addu ra,T,zero (T holds RA); 4 jr ra; 5 nop; 6 callee: ori M,zero,3; 7 jr ra; 8 nop.
def _call(name):
    def gen(rng, i):
        s = _state(rng)
        t, a, l, m, x = _regs(rng, 5)
        s.r[t] = RA
        if name == "jal":
            call = assemble("jal", target=CODE_BASE + 24)
        else:
            s.r[x] = CODE_BASE + 24
            call = assemble("jalr", rd=31, rs=x)
        return [call, assemble("ori", rt=a, imm=1), assemble("addu", rd=l, rs=31),
                assemble("addu", rd=31, rs=t), _JR_RA, _NOP,
                assemble("ori", rt=m, imm=3), _JR_RA, _NOP], s
    return gen


def _fp_operand(rng, s, reg, fmt, positive=False):
    if fmt == "s":
        s.fpr[reg] = _single(rng, positive)
    else:
        _set_double(s, reg, _double(rng))


def _fp_reg(rng, fmt):
    return rng.randrange(32) if fmt == "s" else rng.randrange(0, 32, 2)


def _fp_binary(name):
    fmt = name[-1]

    def gen(rng, i):
        s = _state(rng)
        fd, fs, ft = (_fp_reg(rng, fmt) for _ in range(3))
        _fp_operand(rng, s, fs, fmt)
        _fp_operand(rng, s, ft, fmt)
        return _prog(assemble(name, fd=fd, fs=fs, ft=ft)), s
    return gen


def _fp_unary(name):
    fmt = name[-1]

    def gen(rng, i):
        s = _state(rng)
        fd, fs = _fp_reg(rng, fmt), _fp_reg(rng, fmt)
        _fp_operand(rng, s, fs, fmt, positive=name.startswith("sqrt"))
        return _prog(assemble(name, fd=fd, fs=fs)), s
    return gen


def _fp_compare(name):
    fmt = name[-1]

    def gen(rng, i):
        s = _state(rng)
        fs, ft = _fp_reg(rng, fmt), _fp_reg(rng, fmt)
        _fp_operand(rng, s, fs, fmt)
        if i % 3 == 0:  # equal operands
            s.fpr[ft], s.fpr[ft | (fmt == "d")] = s.fpr[fs], s.fpr[fs | (fmt == "d")]
        else:
            _fp_operand(rng, s, ft, fmt)
        return _prog(assemble(name, cc=i % 8, fs=fs, ft=ft)), s
    return gen


def _fp_madd(name):
    fmt = name[-1]

    def gen(rng, i):
        s = _state(rng)
        fd, fr, fs, ft = (_fp_reg(rng, fmt) for _ in range(4))
        for reg in (fr, fs, ft):
            _fp_operand(rng, s, reg, fmt)
        return _prog(assemble(name, fd=fd, fr=fr, fs=fs, ft=ft)), s
    return gen


def _cvt(name):
    to, src = name[4], name[6]

    def gen(rng, i):
        s = _state(rng)
        fd, fs = _fp_reg(rng, to), _fp_reg(rng, src)
        if src == "w":
            s.fpr[fs] = _int(rng)
        else:
            _fp_operand(rng, s, fs, src)
        return _prog(assemble(name, fd=fd, fs=fs)), s
    return gen


def _trunc(name):
    fmt = name[-1]

    def gen(rng, i):
        s = _state(rng)
        fd, fs = _fp_reg(rng, "s"), _fp_reg(rng, fmt)
        value = rng.choice((-1, 1)) * 2 ** rng.uniform(-3, 30.9)
        if fmt == "s":
            s.fpr[fs] = _f32_bits(value)
        else:
            _set_double(s, fs, _f64_words(value))
        return _prog(assemble(name, fd=fd, fs=fs)), s
    return gen


def _mtc1(rng, i):
    s = _state(rng)
    rt, = _regs(rng, 1)
    return _prog(assemble("mtc1", rt=rt, fs=rng.randrange(32))), s


def _mfc1(rng, i):
    s = _state(rng)
    rt, = _regs(rng, 1)
    return _prog(assemble("mfc1", rt=rt, fs=rng.randrange(32))), s


def _ctc1(rng, i):
    s = _state(rng)
    rt, = _regs(rng, 1)
    # condition bits, FS, flags and rounding mode; no enables or causes, which would trap
    s.r[rt] = _fcsr_cc(rng) | rng.getrandbits(1) << 24 | rng.getrandbits(5) << 2 | i % 4
    return _prog(assemble("ctc1", rt=rt, fs=31)), s


def _break(rng, i):
    return _prog(assemble("break", code=rng.getrandbits(20))), _state(rng)


def _signed(v):
    return v - (1 << 32) if v & 0x80000000 else v


_GENERATORS = {}
for _n in ("addu", "subu", "and", "or", "xor", "nor", "slt", "sltu", "movz", "movn"):
    _GENERATORS[_n] = _alu3(_n)
for _n in ("sll", "srl", "sra"):
    _GENERATORS[_n] = _shift(_n)
for _n in ("addiu", "addi", "slti", "sltiu", "andi", "ori", "xori"):
    _GENERATORS[_n] = _imm(_n)
for _n in ("mult", "multu", "div", "divu"):
    _GENERATORS[_n] = _muldiv(_n)
for _n in ("mfhi", "mflo"):
    _GENERATORS[_n] = _mfhilo(_n)
for _n in ("movf", "movt"):
    _GENERATORS[_n] = _movcc(_n)
for _n in _MEM_SIZE:
    _GENERATORS[_n] = _mem(_n)
for _n in ("beq", "bne", "blez", "bgtz", "bltz", "bgez", "beql", "bnel", "blezl", "bgtzl",
           "bltzl", "bgezl", "bc1f", "bc1t", "bc1fl", "bc1tl"):
    _GENERATORS[_n] = _branch(_n)
for _n in ("jal", "jalr"):
    _GENERATORS[_n] = _call(_n)
for _n in ("add.s", "sub.s", "mul.s", "div.s", "add.d", "sub.d", "mul.d", "div.d"):
    _GENERATORS[_n] = _fp_binary(_n)
for _n in ("mov.s", "neg.s", "sqrt.s", "recip.s", "mov.d", "neg.d"):
    _GENERATORS[_n] = _fp_unary(_n)
for _n in ("c.lt.s", "c.le.s", "c.eq.s", "c.lt.d", "c.le.d", "c.eq.d"):
    _GENERATORS[_n] = _fp_compare(_n)
for _n in ("madd.s", "msub.s", "nmadd.s", "nmsub.s", "madd.d"):
    _GENERATORS[_n] = _fp_madd(_n)
for _n in ("cvt.s.w", "cvt.d.w", "cvt.d.s", "cvt.s.d"):
    _GENERATORS[_n] = _cvt(_n)
for _n in ("trunc.w.s", "trunc.w.d"):
    _GENERATORS[_n] = _trunc(_n)
_GENERATORS.update(lui=_lui, sllv=_sllv, j=_j, jr=_jr, mtc1=_mtc1, mfc1=_mfc1, ctc1=_ctc1,
                   **{"break": _break})
assert set(_GENERATORS) == set(CENSUS), set(CENSUS) ^ set(_GENERATORS)

RANDOM_PER_MNEMONIC = 12


# --- edge cases ---------------------------------------------------------------------------

def _edges(rng):
    """(name, words, before) of the named edge cases."""
    out = []
    for name in ("div", "divu", "mult"):
        for a in _INT_EDGES:
            for b in _INT_EDGES:
                s = _state(rng)
                rs, rt = _regs(rng, 2)
                s.r[rs], s.r[rt] = a, b
                out.append((f"{name}.edge.{a:08X}.{b:08X}",
                            _prog(assemble(name, rs=rs, rt=rt)), s))
    for a in _INT_EDGES:
        for b in _INT_EDGES + (31, 32, 33):
            s = _state(rng)
            rd, rs, rt = _regs(rng, 3)
            s.r[rt], s.r[rs] = a, b
            out.append((f"sllv.edge.{a:08X}.{b:08X}",
                        _prog(assemble("sllv", rd=rd, rs=rs, rt=rt)), s))
        for imm in (0, 1, -1, 0x7FFF, -0x8000):
            s = _state(rng)
            rt, rs = _regs(rng, 2)
            s.r[rs] = a
            out.append((f"addiu.edge.{a:08X}.{imm & 0xFFFF:04X}",
                        _prog(assemble("addiu", rt=rt, rs=rs, imm=imm)), s))
    trunc = [(f"{e:08X}", float(e)) for e in _INT_EDGES] + \
        [("3e9", 3e9), ("-3e9", -3e9), ("nan", float("nan"))]
    for name in ("trunc.w.s", "trunc.w.d"):
        for label, value in trunc:
            s = _state(rng)
            fd, fs = _fp_reg(rng, "s"), _fp_reg(rng, "d")
            if name.endswith(".s"):
                s.fpr[fs] = 0x7FC00000 if label == "nan" else _f32_bits(value)
            else:
                _set_double(s, fs, (0, 0x7FF80000) if label == "nan" else _f64_words(value))
            out.append((f"{name}.edge.{label}", _prog(assemble(name, fd=fd, fs=fs)), s))
    # The game's own break words (divide by zero, overflow, and the two other codes).
    for word in (0x0007000D, 0x0006000D, 0x000101CD, 0x0001024D):
        out.append((f"break.edge.{word:08X}", _prog(word), _state(rng)))
    # FR = 0 halves: mtc1 into odd $f3, sdc1/ldc1 of the even pairs, mfc1 of the odd half.
    s = _state(rng)
    s.r[8], s.r[4] = 0x13579BDF, SCRATCH + 0x100
    s.mem = {}
    out.append(("mtc1.edge.fr0-halves", _prog(
        assemble("mtc1", rt=8, fs=3), assemble("sdc1", base=4, ft=2, imm=0),
        assemble("ldc1", base=4, ft=4, imm=0), assemble("mfc1", rt=9, fs=5)), s))
    return out


# FCSR.FS (flush to zero), which the game sets in _start (ctc1 at 0x800C4064) and keeps.
FCSR_FS = 1 << 24


# The FS cases: (mnemonic, label, operand bits by role). Singles are bits, doubles (low,
# high) words. "denormal-result": normal operands whose exact result is denormal;
# "denormal-operand": a denormal operand whose result, unflushed, is normal (or a compare
# that a flushed operand would decide the other way).
_DEN_S = 0x00400000  # 2^-127
_DEN_D = _f64_words(2.0 ** -1023)
_FS_CASES = (
    ("mul.s", "denormal-result", {"fs": _f32_bits(2.0 ** -70), "ft": _f32_bits(1.5 * 2.0 ** -70)}),
    ("div.s", "denormal-result", {"fs": _f32_bits(2.0 ** -100), "ft": _f32_bits(2.0 ** 30)}),
    ("sub.s", "denormal-result", {"fs": 0x00C00000, "ft": 0x00800000}),
    ("add.s", "denormal-result", {"fs": 0x00C00000, "ft": 0x80800000}),
    ("cvt.s.d", "denormal-result", {"fs": _f64_words(1.5 * 2.0 ** -140)}),
    ("mul.d", "denormal-result", {"fs": _f64_words(2.0 ** -520),
                                  "ft": _f64_words(1.5 * 2.0 ** -520)}),
    ("div.d", "denormal-result", {"fs": _f64_words(2.0 ** -1000), "ft": _f64_words(2.0 ** 60)}),
    ("sub.d", "denormal-result", {"fs": _f64_words(1.5 * 2.0 ** -1022),
                                  "ft": _f64_words(2.0 ** -1022)}),
    ("add.d", "denormal-result", {"fs": _f64_words(1.5 * 2.0 ** -1022),
                                  "ft": _f64_words(-(2.0 ** -1022))}),
    ("mul.s", "denormal-operand", {"fs": _DEN_S, "ft": _f32_bits(2.0 ** 30)}),
    ("add.s", "denormal-operand", {"fs": _DEN_S, "ft": 0x00800000}),
    ("cvt.d.s", "denormal-operand", {"fs": _DEN_S}),
    ("c.eq.s", "denormal-operand", {"fs": _DEN_S, "ft": 0}),
    ("c.lt.s", "denormal-operand", {"fs": 0, "ft": _DEN_S}),
    ("mul.d", "denormal-operand", {"fs": _DEN_D, "ft": _f64_words(2.0 ** 100)}),
    ("add.d", "denormal-operand", {"fs": _DEN_D, "ft": _f64_words(2.0 ** -1022)}),
)
MAME_SPECIFIC.update({f"{name}.edge.fs1.{label}": _FS for name, label, _ops in _FS_CASES})


def _fs_edges(rng):
    """(name, words, before) of the FS cases: each of _FS_CASES with FCSR.FS = 1, then the same
    program and state with FS = 0 as its control. Rounding mode 0, no enables."""
    out = []
    for name, label, operands in _FS_CASES:
        s = _state(rng)
        # even registers throughout, so a double (source or destination) is a valid pair
        fields = dict(zip(("fd", "fs", "ft"), rng.sample(range(0, 32, 2), 3)))
        for role, bits in operands.items():
            if name.endswith(".d"):  # the source format
                _set_double(s, fields[role], bits)
            else:
                s.fpr[fields[role]] = bits
        if name.startswith("c."):
            # cc0 starts opposite to the unflushed result (c.eq false, c.lt true), so that
            # result shows as a change and a flushed operand as none
            s.fcsr = s.fcsr & ~(1 << 23) | (name == "c.eq.s") << 23
            word = assemble(name, cc=0, fs=fields["fs"], ft=fields["ft"])
        elif "ft" in operands:
            word = assemble(name, **fields)
        else:
            word = assemble(name, fd=fields["fd"], fs=fields["fs"])
        for fs in (1, 0):
            before = State(list(s.r), s.hi, s.lo, list(s.fpr), s.fcsr | fs * FCSR_FS)
            out.append((f"{name}.edge.fs{fs}.{label}", _prog(word), before))
    return out


def _clean(state):
    state.mem = {a: v for a, v in state.mem.items() if v}
    return state


def cases(seed=1):
    """The instruction test cases: RANDOM_PER_MNEMONIC random ones per census mnemonic,
    then the named edge cases, then the FS cases (appended last, so the cases before them
    are the same as before they existed). Deterministic for a given seed."""
    rng = random.Random(seed)
    out = []
    for name in CENSUS:
        for i in range(RANDOM_PER_MNEMONIC):
            words, before = _GENERATORS[name](rng, i)
            out.append(Case(f"{name}.{i}", words, _clean(before), name in NO_MAME))
    for name, words, before in _edges(rng) + _fs_edges(rng):
        no_mame = name in NO_MAME or name.split(".edge.")[0] in NO_MAME
        out.append(Case(name, words, _clean(before), no_mame))
    return out


# --- files --------------------------------------------------------------------------------

def write_cases(case_list, path):
    """The input of tools/mame/insnvec.lua: the MAME-run cases (not `no_mame`) with their
    before-states, and the stub words the script places at RA and at RA + 0x80."""
    k0 = 26
    stub = {
        "code_base": CODE_BASE, "ra": RA, "scratch": SCRATCH, "scratch_size": SCRATCH_SIZE,
        # at RA: cfc1 k0,$31; nop -- the script reads FCSR from k0 at RA + 8
        "cfc1_k0": assemble("cfc1", rt=k0, fs=31),
        # at RA + 0x80: ctc1 k0,$31; lui k0,hi; j CODE_BASE; ori k0,k0,lo
        "ctc1_k0": assemble("ctc1", rt=k0, fs=31),
        "lui_k0": assemble("lui", rt=k0, imm=0),
        "j_code": assemble("j", target=CODE_BASE),
        "ori_k0": assemble("ori", rt=k0, rs=k0, imm=0),
    }
    lines = []
    for c in case_list:
        if c.no_mame:
            continue
        b = c.before
        lines.append(json.dumps({
            "name": c.name, "words": c.words, "r": b.r, "hi": b.hi, "lo": b.lo, "fpr": b.fpr,
            "fcsr": b.fcsr, "mem": sorted([a, v] for a, v in b.mem.items())},
            separators=(",", ":")))
    with open(path, "w") as f:
        f.write('{"stub":' + json.dumps(stub, separators=(",", ":")) + ',"cases":[\n')
        f.write(",\n".join(lines))
        f.write("\n]}\n")


def _read_cases(path):
    with open(path) as f:
        data = json.load(f)
    return [Case(c["name"], c["words"],
                 State(c["r"], c["hi"], c["lo"], c["fpr"], c["fcsr"],
                       {a: v for a, v in c["mem"]})) for c in data["cases"]]


def _hex(v):
    return f"{v:08X}"


def _pack(values):
    return "".join(_hex(v) for v in values)


def _unpack(text):
    return [int(text[i:i + 8], 16) for i in range(0, len(text), 8)]


def _diff(before, after):
    d = {}
    r = {str(i): _hex(v) for i, (u, v) in enumerate(zip(before.r, after.r)) if u != v}
    fpr = {str(i): _hex(v) for i, (u, v) in enumerate(zip(before.fpr, after.fpr)) if u != v}
    mem = {_hex(a): _hex(after.mem.get(a, 0))
           for a in sorted(set(before.mem) | set(after.mem))
           if before.mem.get(a, 0) != after.mem.get(a, 0)}
    if r:
        d["r"] = r
    for name in ("hi", "lo", "fcsr"):
        if getattr(before, name) != getattr(after, name):
            d[name] = _hex(getattr(after, name))
    if fpr:
        d["fpr"] = fpr
    if mem:
        d["mem"] = mem
    return d


def merge(cases_path, results_path, out_path):
    """Write the committed vector file from the script's input and its output. Raises
    ValueError naming the cases MAME failed or did not run."""
    case_list = _read_cases(cases_path)
    results = {}
    with open(results_path) as f:
        for line in f:
            if line.strip():
                res = json.loads(line)
                results[res["name"]] = res
    failed = [f"{n}: {r['error']}" for n, r in results.items() if "error" in r]
    missing = [c.name for c in case_list if c.name not in results]
    if failed or missing:
        raise ValueError("MAME did not record every case:\n  " +
                         "\n  ".join(failed + [f"{n}: no result" for n in missing]))
    lines = []
    for c in case_list:
        res = results[c.name]
        after = State([v & _M32 for v in res["r"]], res["hi"] & _M32, res["lo"] & _M32,
                      [v & _M32 for v in res["fpr"]], res["fcsr"] & _M32,
                      {a: v & _M32 for a, v in res["mem"] if v})
        b = c.before
        lines.append(json.dumps({
            "name": c.name, "words": [_hex(w) for w in c.words],
            "before": {"r": _pack(b.r), "hi": _hex(b.hi), "lo": _hex(b.lo),
                       "fpr": _pack(b.fpr), "fcsr": _hex(b.fcsr),
                       "mem": {_hex(a): _hex(v) for a, v in sorted(b.mem.items())}},
            "after": _diff(b, after)}, separators=(",", ":")))
    with open(out_path, "w") as f:
        f.write('{"format":1,"cases":[\n' + ",\n".join(lines) + "\n]}\n")
    return len(lines)


def load(path):
    """[(case, state after)] of a vector file written by `merge`."""
    with open(path) as f:
        data = json.load(f)
    out = []
    for c in data["cases"]:
        b = c["before"]
        before = State(_unpack(b["r"]), int(b["hi"], 16), int(b["lo"], 16), _unpack(b["fpr"]),
                       int(b["fcsr"], 16), {int(a, 16): int(v, 16) for a, v in b["mem"].items()})
        a = c["after"]
        after = State(list(before.r), before.hi, before.lo, list(before.fpr), before.fcsr,
                      dict(before.mem))
        for i, v in a.get("r", {}).items():
            after.r[int(i)] = int(v, 16)
        for i, v in a.get("fpr", {}).items():
            after.fpr[int(i)] = int(v, 16)
        for name in ("hi", "lo", "fcsr"):
            if name in a:
                setattr(after, name, int(a[name], 16))
        for addr, v in a.get("mem", {}).items():
            after.mem[int(addr, 16)] = int(v, 16)
        after.mem = {k: v for k, v in after.mem.items() if v}
        out.append((Case(c["name"], [int(w, 16) for w in c["words"]], before), after))
    return out
