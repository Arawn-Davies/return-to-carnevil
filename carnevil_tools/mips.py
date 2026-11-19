"""MIPS IV decoder (IDT R5000, little-endian words) and opcode census.

`decode` is table-driven on the opcode / funct / fmt fields. Encodings that
MIPS IV on the R5000 leaves reserved decode as mnemonic "unknown", kind
"unknown" rather than being forced into a neighbouring instruction.

Insn fields are the raw instruction fields, whatever they mean for the
instruction in question:
    rs = bits 25..21, rt = bits 20..16, rd = bits 15..11, sa = bits 10..6
so for COP1 `fmt`, `ft`, `fs`, `fd` are rs, rt, rd, sa; for COP1X `base`/`fr`
is rs and `index`/`ft` is rt; for movf/movt the rt field is cc<<2 | tf; for
bc1* the rt field is cc<<2 | nd<<1 | tf. See `fp_condition_code`.

`imm` is the sign-extended 16-bit immediate, except for lui, andi, ori and
xori, where it is the raw zero-extended 16 bits. For j/jal it is the raw
26-bit index. For instructions without an immediate it is 0.

No pseudo-instructions: 0x00000000 is `sll zero,zero,0`, `b` is `beq`.
"""
from dataclasses import dataclass

_RS = 0x03E00000
_RT = 0x001F0000
_RD = 0x0000F800
_SA = 0x000007C0

_M32 = 0xFFFFFFFF


@dataclass(frozen=True)
class Insn:
    word: int
    mnemonic: str
    rs: int
    rt: int
    rd: int
    sa: int
    imm: int
    target: int | None
    kind: str


# Primary opcode -> (mnemonic, kind, form). Form: "I" sign-extended immediate,
# "U" raw 16-bit immediate, "B" PC-relative branch, "J" 26-bit jump.
_PRIMARY = {
    0x02: ("j", "jump", "J"),
    0x03: ("jal", "call", "J"),
    0x04: ("beq", "branch", "B"),
    0x05: ("bne", "branch", "B"),
    0x06: ("blez", "branch", "B"),
    0x07: ("bgtz", "branch", "B"),
    0x08: ("addi", "normal", "I"),
    0x09: ("addiu", "normal", "I"),
    0x0A: ("slti", "normal", "I"),
    0x0B: ("sltiu", "normal", "I"),
    0x0C: ("andi", "normal", "U"),
    0x0D: ("ori", "normal", "U"),
    0x0E: ("xori", "normal", "U"),
    0x0F: ("lui", "normal", "U"),
    0x14: ("beql", "branch_likely", "B"),
    0x15: ("bnel", "branch_likely", "B"),
    0x16: ("blezl", "branch_likely", "B"),
    0x17: ("bgtzl", "branch_likely", "B"),
    0x18: ("daddi", "normal", "I"),
    0x19: ("daddiu", "normal", "I"),
    0x1A: ("ldl", "normal", "I"),
    0x1B: ("ldr", "normal", "I"),
    0x20: ("lb", "normal", "I"),
    0x21: ("lh", "normal", "I"),
    0x22: ("lwl", "normal", "I"),
    0x23: ("lw", "normal", "I"),
    0x24: ("lbu", "normal", "I"),
    0x25: ("lhu", "normal", "I"),
    0x26: ("lwr", "normal", "I"),
    0x27: ("lwu", "normal", "I"),
    0x28: ("sb", "normal", "I"),
    0x29: ("sh", "normal", "I"),
    0x2A: ("swl", "normal", "I"),
    0x2B: ("sw", "normal", "I"),
    0x2C: ("sdl", "normal", "I"),
    0x2D: ("sdr", "normal", "I"),
    0x2E: ("swr", "normal", "I"),
    0x2F: ("cache", "normal", "I"),
    0x30: ("ll", "normal", "I"),
    0x31: ("lwc1", "normal", "I"),
    0x33: ("pref", "normal", "I"),
    0x34: ("lld", "normal", "I"),
    0x35: ("ldc1", "normal", "I"),
    0x37: ("ld", "normal", "I"),
    0x38: ("sc", "normal", "I"),
    0x39: ("swc1", "normal", "I"),
    0x3C: ("scd", "normal", "I"),
    0x3D: ("sdc1", "normal", "I"),
    0x3F: ("sd", "normal", "I"),
}

# SPECIAL funct -> (mnemonic, kind, bits that must be zero)
_SPECIAL = {
    0x00: ("sll", "normal", _RS),
    0x02: ("srl", "normal", _RS),
    0x03: ("sra", "normal", _RS),
    0x04: ("sllv", "normal", _SA),
    0x06: ("srlv", "normal", _SA),
    0x07: ("srav", "normal", _SA),
    0x08: ("jr", "jump_reg", _RT | _RD | _SA),
    0x09: ("jalr", "call_reg", _RT | _SA),
    0x0A: ("movz", "normal", _SA),
    0x0B: ("movn", "normal", _SA),
    0x0C: ("syscall", "trap", 0),
    0x0D: ("break", "trap", 0),
    0x0F: ("sync", "normal", _RS | _RT | _RD),
    0x10: ("mfhi", "normal", _RS | _RT | _SA),
    0x11: ("mthi", "normal", _RT | _RD | _SA),
    0x12: ("mflo", "normal", _RS | _RT | _SA),
    0x13: ("mtlo", "normal", _RT | _RD | _SA),
    0x14: ("dsllv", "normal", _SA),
    0x16: ("dsrlv", "normal", _SA),
    0x17: ("dsrav", "normal", _SA),
    0x18: ("mult", "normal", _RD | _SA),
    0x19: ("multu", "normal", _RD | _SA),
    0x1A: ("div", "normal", _RD | _SA),
    0x1B: ("divu", "normal", _RD | _SA),
    0x1C: ("dmult", "normal", _RD | _SA),
    0x1D: ("dmultu", "normal", _RD | _SA),
    0x1E: ("ddiv", "normal", _RD | _SA),
    0x1F: ("ddivu", "normal", _RD | _SA),
    0x20: ("add", "normal", _SA),
    0x21: ("addu", "normal", _SA),
    0x22: ("sub", "normal", _SA),
    0x23: ("subu", "normal", _SA),
    0x24: ("and", "normal", _SA),
    0x25: ("or", "normal", _SA),
    0x26: ("xor", "normal", _SA),
    0x27: ("nor", "normal", _SA),
    0x2A: ("slt", "normal", _SA),
    0x2B: ("sltu", "normal", _SA),
    0x2C: ("dadd", "normal", _SA),
    0x2D: ("daddu", "normal", _SA),
    0x2E: ("dsub", "normal", _SA),
    0x2F: ("dsubu", "normal", _SA),
    0x30: ("tge", "trap", 0),
    0x31: ("tgeu", "trap", 0),
    0x32: ("tlt", "trap", 0),
    0x33: ("tltu", "trap", 0),
    0x34: ("teq", "trap", 0),
    0x36: ("tne", "trap", 0),
    0x38: ("dsll", "normal", _RS),
    0x3A: ("dsrl", "normal", _RS),
    0x3B: ("dsra", "normal", _RS),
    0x3C: ("dsll32", "normal", _RS),
    0x3E: ("dsrl32", "normal", _RS),
    0x3F: ("dsra32", "normal", _RS),
}
_MOVCI = 0x01  # SPECIAL funct for movf / movt

# REGIMM rt -> (mnemonic, kind, form)
_REGIMM = {
    0x00: ("bltz", "branch", "B"),
    0x01: ("bgez", "branch", "B"),
    0x02: ("bltzl", "branch_likely", "B"),
    0x03: ("bgezl", "branch_likely", "B"),
    0x08: ("tgei", "trap", "I"),
    0x09: ("tgeiu", "trap", "I"),
    0x0A: ("tlti", "trap", "I"),
    0x0B: ("tltiu", "trap", "I"),
    0x0C: ("teqi", "trap", "I"),
    0x0E: ("tnei", "trap", "I"),
    0x10: ("bltzal", "call", "B"),
    0x11: ("bgezal", "call", "B"),
    0x12: ("bltzall", "call", "B"),
    0x13: ("bgezall", "call", "B"),
}

_COP0_MOVES = {0: "mfc0", 1: "dmfc0", 4: "mtc0", 5: "dmtc0"}
_COP0_BC = {0: ("bc0f", "branch"), 1: ("bc0t", "branch"),
            2: ("bc0fl", "branch_likely"), 3: ("bc0tl", "branch_likely")}
# COP0 CO funct -> (mnemonic, kind, bits that must be zero)
_COP0_CO_MASK = 0x01FFFFC0  # bits 24..6
_COP0_CO = {
    0x01: ("tlbr", "normal", _COP0_CO_MASK),
    0x02: ("tlbwi", "normal", _COP0_CO_MASK),
    0x06: ("tlbwr", "normal", _COP0_CO_MASK),
    0x08: ("tlbp", "normal", _COP0_CO_MASK),
    0x18: ("eret", "jump_reg", _COP0_CO_MASK),
    0x20: ("wait", "normal", 0),
}

_COP1_MOVES = {0: "mfc1", 1: "dmfc1", 2: "cfc1", 4: "mtc1", 5: "dmtc1", 6: "ctc1"}
_COP1_BC = {0: ("bc1f", "branch"), 1: ("bc1t", "branch"),
            2: ("bc1fl", "branch_likely"), 3: ("bc1tl", "branch_likely")}
_FMT = {16: "s", 17: "d", 20: "w", 21: "l"}
_FP_BINARY = {0x00: "add", 0x01: "sub", 0x02: "mul", 0x03: "div"}
_FP_UNARY = {
    0x04: "sqrt", 0x05: "abs", 0x06: "mov", 0x07: "neg",
    0x08: "round.l", 0x09: "trunc.l", 0x0A: "ceil.l", 0x0B: "floor.l",
    0x0C: "round.w", 0x0D: "trunc.w", 0x0E: "ceil.w", 0x0F: "floor.w",
    0x15: "recip", 0x16: "rsqrt",
}
_FP_COND = ["f", "un", "eq", "ueq", "olt", "ult", "ole", "ule",
            "sf", "ngle", "seq", "ngl", "lt", "nge", "le", "ngt"]

# COP1X funct -> mnemonic and the bits that must be zero
_COP1X_MEM = {
    0x00: ("lwxc1", _RD),
    0x01: ("ldxc1", _RD),
    0x08: ("swxc1", _SA),
    0x09: ("sdxc1", _SA),
    0x0F: ("prefx", _SA),
}
_COP1X_MADD = {0x20: "madd", 0x28: "msub", 0x30: "nmadd", 0x38: "nmsub"}

COP0_MNEMONICS = frozenset(
    list(_COP0_MOVES.values())
    + [name for name, _kind, _m in _COP0_CO.values()]
    + [name for name, _kind in _COP0_BC.values()]
    + ["cache"]
)

MIPS_IV_MNEMONICS = frozenset(
    ["movz", "movn", "movf", "movt", "pref", "prefx",
     "lwxc1", "ldxc1", "swxc1", "sdxc1"]
    + ["%s.%s" % (n, f) for n in ("movf", "movt", "movz", "movn", "recip", "rsqrt",
                                  "madd", "msub", "nmadd", "nmsub") for f in "sd"]
)


def _sext16(value):
    return value - 0x10000 if value & 0x8000 else value


def _fp_arith(rs, rt, rd, sa, funct, word):
    """COP1 arithmetic/convert/compare; returns (mnemonic, kind) or None."""
    fmt = _FMT.get(rs)
    if fmt is None:
        return None
    if fmt in "wl":
        # W and L sources exist only as conversion inputs.
        if funct == 0x20 and word & _RT == 0:
            return "cvt.s." + fmt, "normal"
        if funct == 0x21 and word & _RT == 0:
            return "cvt.d." + fmt, "normal"
        return None
    if funct in _FP_BINARY:
        return "%s.%s" % (_FP_BINARY[funct], fmt), "normal"
    if funct in _FP_UNARY:
        if word & _RT:
            return None
        return "%s.%s" % (_FP_UNARY[funct], fmt), "normal"
    if funct == 0x11:  # movf.fmt / movt.fmt: ft = cc<<2 | nd<<1 | tf
        if rt & 2:
            return None
        return ("movt." if rt & 1 else "movf.") + fmt, "normal"
    if funct == 0x12:
        return "movz." + fmt, "normal"
    if funct == 0x13:
        return "movn." + fmt, "normal"
    if funct in (0x20, 0x21, 0x24, 0x25):
        if word & _RT:
            return None
        if funct == 0x20 and fmt != "s":
            return "cvt.s." + fmt, "normal"
        if funct == 0x21 and fmt != "d":
            return "cvt.d." + fmt, "normal"
        if funct == 0x24:
            return "cvt.w." + fmt, "normal"
        if funct == 0x25:
            return "cvt.l." + fmt, "normal"
        return None
    if 0x30 <= funct <= 0x3F:
        if sa & 3:  # bits 7:6 are reserved zero; bits 10:8 are the cc
            return None
        return "c.%s.%s" % (_FP_COND[funct - 0x30], fmt), "normal"
    return None


def _lookup(word):
    """Resolve a word to (mnemonic, kind, form); form says how imm/target are computed."""
    op = word >> 26
    rs = (word >> 21) & 31
    rt = (word >> 16) & 31
    rd = (word >> 11) & 31
    sa = (word >> 6) & 31
    funct = word & 63

    if op == 0x00:
        if funct == _MOVCI:
            if rt & 2 or word & _SA:
                return None
            return ("movt" if rt & 1 else "movf"), "normal", "R"
        entry = _SPECIAL.get(funct)
        if entry is None or word & entry[2]:
            return None
        return entry[0], entry[1], "R"
    if op == 0x01:
        return _REGIMM.get(rt)
    if op == 0x10:
        if rs & 0x10:  # CO bit
            entry = _COP0_CO.get(funct)
            if entry is None or word & entry[2]:
                return None
            return entry[0], entry[1], "R"
        if rs == 8:
            entry = _COP0_BC.get(rt)
            return (entry[0], entry[1], "B") if entry else None
        name = _COP0_MOVES.get(rs)
        if name is None or word & 0x7FF:
            return None
        return name, "normal", "R"
    if op == 0x11:
        if rs == 8:
            entry = _COP1_BC.get(rt & 3)
            return (entry[0], entry[1], "B") if entry else None
        name = _COP1_MOVES.get(rs)
        if name is not None:
            return (name, "normal", "R") if not word & 0x7FF else None
        res = _fp_arith(rs, rt, rd, sa, funct, word)
        return (res[0], res[1], "R") if res else None
    if op == 0x13:
        mem = _COP1X_MEM.get(funct)
        if mem is not None:
            return (mem[0], "normal", "R") if not word & mem[1] else None
        base = _COP1X_MADD.get(funct & 0x38)
        if base is not None and funct & 7 in (0, 1):
            return "%s.%s" % (base, "sd"[funct & 1]), "normal", "R"
        return None
    return _PRIMARY.get(op)


def decode(word, addr):
    """Decode one instruction word fetched from address `addr`."""
    rs = (word >> 21) & 31
    rt = (word >> 16) & 31
    rd = (word >> 11) & 31
    sa = (word >> 6) & 31
    raw16 = word & 0xFFFF
    res = _lookup(word)
    if res is None:
        return Insn(word, "unknown", rs, rt, rd, sa, _sext16(raw16), None, "unknown")
    mnemonic, kind, form = res
    target = None
    if form == "R":
        imm = 0
    elif form == "U":
        imm = raw16
    elif form == "J":
        imm = word & 0x03FFFFFF
        target = (((addr + 4) & 0xF0000000) | (imm << 2)) & _M32
    else:
        imm = _sext16(raw16)
        if form == "B":
            target = (addr + 4 + (imm << 2)) & _M32
    return Insn(word, mnemonic, rs, rt, rd, sa, imm, target, kind)


def fp_condition_code(insn):
    """FP condition code (0..7) used by c.cond.fmt, bc1*, movf/movt and movf.fmt/movt.fmt; else None."""
    name = insn.mnemonic
    if name.startswith("c.") and insn.kind == "normal":
        return insn.sa >> 2
    if name.startswith("bc1") or name in ("movf", "movt") or name[:5] in ("movf.", "movt."):
        return insn.rt >> 2
    return None


def walk(image, start, end):
    """Yield (addr, Insn) for every word in [start, end) of an ExeImage."""
    for addr in range(start, end, 4):
        yield addr, decode(image.word(addr), addr)


def census(image, start, end):
    """Mnemonic -> count over the words in [start, end)."""
    counts = {}
    for _addr, insn in walk(image, start, end):
        counts[insn.mnemonic] = counts.get(insn.mnemonic, 0) + 1
    return counts
