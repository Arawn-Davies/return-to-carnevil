"""Emit C for one MIPS instruction that is not a control transfer.

`statement(insn, addr)` returns the C statements, over the names `ram` (uint8_t *) and `c`
(cpu_t *) of runtime/include/recomp.h, that do what the instruction does. Everything that is
not a plain C operator on uint32_t goes through a helper of recomp.h (`rc_*`, `mem_*`,
`rt_set_fcsr`), the helpers the reference interpreter uses, so the two share one definition.

Rules: a write to register 0 is dropped, but a load into register 0 is still performed (the
bus may have a side effect); `addi` is emitted as `addiu` (MAME takes no overflow trap);
`cache` becomes a comment; `ctc1` calls `rt_set_fcsr`.

An instruction in `CONTROL` (left to the function emitter), outside the reached census, or
encoded as the interpreter would reject raises `UnsupportedInstruction`, never a silent no-op.
"""
from carnevil_tools import mips

# Every census mnemonic that transfers control: handled when whole functions are emitted.
CONTROL = frozenset("""
beq bne blez bgtz bltz bgez beql bnel blezl bgtzl bltzl bgezl bc1f bc1t bc1fl bc1tl
j jal jr jalr break
""".split())

_M32 = 0xFFFFFFFF


class UnsupportedInstruction(Exception):
    def __init__(self, addr, mnemonic):
        super().__init__(f"unsupported instruction {mnemonic} at {addr:08X}")
        self.addr = addr
        self.mnemonic = mnemonic


def _u(value):
    """A uint32_t constant."""
    return f"0x{value & _M32:08X}u"


def _reg(n):
    return "0u" if n == 0 else f"c->r[{n}]"


def _set(n, expr):
    """Assign to a general register; a write to register 0 is dropped."""
    return [] if n == 0 else [f"c->r[{n}] = {expr};"]


def _ea(base, offset, plus=0):
    return f"({_reg(base)} + {_u(offset + plus)})"


def _fpr(n):
    return f"c->fpr[{n}]"


def _signed(expr):
    return f"(int32_t){expr}"


# --- integer ----------------------------------------------------------------------------------

def _alu3(expr):
    """rd = expr(rs, rt)."""
    return lambda i: _set(i.rd, expr(_reg(i.rs), _reg(i.rt)))


def _alu_imm(expr):
    """rt = expr(rs, imm), the immediate as the instruction's own (already extended) value."""
    return lambda i: _set(i.rt, expr(_reg(i.rs), i.imm))


def _shift(expr):
    return lambda i: _set(i.rd, expr(_reg(i.rt), i.sa))


def _hilo(helper):
    return lambda i: [f"{helper}(c, {_reg(i.rs)}, {_reg(i.rt)});"]


def _slt(expr):
    """rd = rs < rt; of a register with itself that is 0, written out because the compiler
    rejects the self-comparison (-Wtautological-compare)."""
    return lambda i: _set(i.rd, "0u" if i.rs == i.rt else expr(_reg(i.rs), _reg(i.rt)))


def _movz(i):
    return _cond_move(i, f"{_reg(i.rt)} == 0")


def _movn(i):
    return _cond_move(i, f"{_reg(i.rt)} != 0")


def _cond_move(i, cond):
    if i.rd == 0:
        return []
    return [f"if ({cond}) c->r[{i.rd}] = {_reg(i.rs)};"]


def _movcc(i):
    """movf / movt: the rt field is cc << 2 | tf."""
    if i.rd == 0:
        return []
    cc = mips.fp_condition_code(i)
    return [f"if (rc_cc(c, {cc}) == {i.rt & 1}) c->r[{i.rd}] = {_reg(i.rs)};"]


_INTEGER = {
    "sll": _shift(lambda t, sa: f"{t} << {sa}"),
    "srl": _shift(lambda t, sa: f"{t} >> {sa}"),
    "sra": _shift(lambda t, sa: f"(uint32_t)({_signed(t)} >> {sa})"),
    "sllv": _alu3(lambda s, t: f"{t} << ({s} & 31u)"),
    "movz": _movz,
    "movn": _movn,
    "movf": _movcc,
    "movt": _movcc,
    "mfhi": lambda i: _set(i.rd, "c->hi"),
    "mflo": lambda i: _set(i.rd, "c->lo"),
    "mult": _hilo("rc_mult"),
    "multu": _hilo("rc_multu"),
    "div": _hilo("rc_div"),
    "divu": _hilo("rc_divu"),
    "addu": _alu3(lambda s, t: f"{s} + {t}"),
    "subu": _alu3(lambda s, t: f"{s} - {t}"),
    "and": _alu3(lambda s, t: f"{s} & {t}"),
    "or": _alu3(lambda s, t: f"{s} | {t}"),
    "xor": _alu3(lambda s, t: f"{s} ^ {t}"),
    "nor": _alu3(lambda s, t: f"~({s} | {t})"),
    "slt": _slt(lambda s, t: f"(uint32_t)({_signed(s)} < {_signed(t)})"),
    "sltu": _slt(lambda s, t: f"(uint32_t)({s} < {t})"),
    "addiu": _alu_imm(lambda s, imm: f"{s} + {_u(imm)}"),
    "addi": _alu_imm(lambda s, imm: f"{s} + {_u(imm)}"),
    "slti": _alu_imm(lambda s, imm: f"(uint32_t)({_signed(s)} < {imm})"),
    "sltiu": _alu_imm(lambda s, imm: f"(uint32_t)({s} < {_u(imm)})"),
    "andi": _alu_imm(lambda s, imm: f"{s} & {_u(imm)}"),
    "ori": _alu_imm(lambda s, imm: f"{s} | {_u(imm)}"),
    "xori": _alu_imm(lambda s, imm: f"{s} ^ {_u(imm)}"),
    "lui": lambda i: _set(i.rt, _u(i.imm << 16)),
}


# --- memory -----------------------------------------------------------------------------------

def _load(read, extend="{}"):
    def emit(i):
        value = f"{read}(ram, c, {_ea(i.rs, i.imm)})"
        if i.rt == 0:
            return [f"(void){value};"]  # still read: the bus may have a side effect
        return _set(i.rt, extend.format(value))
    return emit


def _store(write):
    return lambda i: [f"{write}(ram, c, {_ea(i.rs, i.imm)}, {_reg(i.rt)});"]


def _lwc1(i):
    return [f"{_fpr(i.rt)} = mem_r32(ram, c, {_ea(i.rs, i.imm)});"]


def _swc1(i):
    return [f"mem_w32(ram, c, {_ea(i.rs, i.imm)}, {_fpr(i.rt)});"]


def _ldc1(i):
    lo = i.rt & 30
    return [f"{_fpr(lo)} = mem_r32(ram, c, {_ea(i.rs, i.imm)});",
            f"{_fpr(lo + 1)} = mem_r32(ram, c, {_ea(i.rs, i.imm, 4)});"]


def _sdc1(i):
    lo = i.rt & 30
    return [f"mem_w32(ram, c, {_ea(i.rs, i.imm)}, {_fpr(lo)});",
            f"mem_w32(ram, c, {_ea(i.rs, i.imm, 4)}, {_fpr(lo + 1)});"]


def _cache(i):
    return [f"/* cache op {i.rt}: no caches are modelled */"]


_MEMORY = {
    "lb": _load("mem_r8", "(uint32_t)(int32_t)(int8_t){}"),
    "lh": _load("mem_r16", "(uint32_t)(int32_t)(int16_t){}"),
    "lw": _load("mem_r32"),
    "lbu": _load("mem_r8"),
    "lhu": _load("mem_r16"),
    "sb": _store("mem_w8"),
    "sh": _store("mem_w16"),
    "sw": _store("mem_w32"),
    "lwc1": _lwc1,
    "swc1": _swc1,
    "ldc1": _ldc1,
    "sdc1": _sdc1,
    "cache": _cache,
}


# --- floating point ---------------------------------------------------------------------------
# COP1 fields: fs = rd, ft = rt, fd = sa. COP1X: fr = rs.

def _fs(n):
    return f"rc_fs(c, {n})"


def _fd(n):
    return f"rc_fd(c, {n})"


def _fp_binary(op, fmt):
    """fd = fs op ft, a plain C operator on the format's host type."""
    get, put = (_fs, "rc_set_fs") if fmt == "s" else (_fd, "rc_set_fd")
    return lambda i: [f"{put}(c, {i.sa}, {get(i.rd)} {op} {get(i.rt)});"]


def _fp_unary(fmt, expr):
    """fd = expr(fs) in format fmt."""
    get, put = (_fs, "rc_set_fs") if fmt == "s" else (_fd, "rc_set_fd")
    return lambda i: [f"{put}(c, {i.sa}, {expr(get(i.rd))});"]


def _compare(helper, fmt):
    get = _fs if fmt == "s" else _fd
    return lambda i: [f"{helper}(c, {mips.fp_condition_code(i)}, {get(i.rd)}, {get(i.rt)});"]


def _madd(helper, fmt):
    get, put = (_fs, "rc_set_fs") if fmt == "s" else (_fd, "rc_set_fd")
    return lambda i: [f"{put}(c, {i.sa}, {helper}({get(i.rs)}, {get(i.rd)}, {get(i.rt)}));"]


def _mov_s(i):
    return [f"{_fpr(i.sa)} = {_fpr(i.rd)};"]


def _mov_d(i):
    fd, fs = i.sa & 30, i.rd & 30
    return [f"{_fpr(fd)} = {_fpr(fs)};", f"{_fpr(fd + 1)} = {_fpr(fs + 1)};"]


def _trunc(fmt):
    helper = f"rc_trunc_w_{fmt}"
    get = _fs if fmt == "s" else _fd
    return lambda i: [f"{_fpr(i.sa)} = {helper}({get(i.rd)});"]


def _cvt_w(put):
    return lambda i: [f"{put}(c, {i.sa}, {'(float)' if put == 'rc_set_fs' else '(double)'}"
                      f"(int32_t){_fpr(i.rd)});"]


def _mfc1(i):
    return _set(i.rt, _fpr(i.rd))


def _mtc1(i):
    return [f"{_fpr(i.rd)} = {_reg(i.rt)};"]


_FLOAT = {
    "add.s": _fp_binary("+", "s"), "sub.s": _fp_binary("-", "s"),
    "mul.s": _fp_binary("*", "s"), "div.s": _fp_binary("/", "s"),
    "add.d": _fp_binary("+", "d"), "sub.d": _fp_binary("-", "d"),
    "mul.d": _fp_binary("*", "d"), "div.d": _fp_binary("/", "d"),
    "sqrt.s": _fp_unary("s", lambda a: f"rc_sqrt_s({a})"),
    "neg.s": _fp_unary("s", lambda a: f"-{a}"),
    "neg.d": _fp_unary("d", lambda a: f"-{a}"),
    "recip.s": _fp_unary("s", lambda a: f"1.0f / {a}"),
    "mov.s": _mov_s,
    "mov.d": _mov_d,
    "trunc.w.s": _trunc("s"),
    "trunc.w.d": _trunc("d"),
    "cvt.d.s": lambda i: [f"rc_set_fd(c, {i.sa}, (double){_fs(i.rd)});"],
    "cvt.s.d": lambda i: [f"rc_set_fs(c, {i.sa}, (float){_fd(i.rd)});"],
    "cvt.s.w": _cvt_w("rc_set_fs"),
    "cvt.d.w": _cvt_w("rc_set_fd"),
    "c.eq.s": _compare("rc_c_eq_s", "s"), "c.lt.s": _compare("rc_c_lt_s", "s"),
    "c.le.s": _compare("rc_c_le_s", "s"),
    "c.eq.d": _compare("rc_c_eq_d", "d"), "c.lt.d": _compare("rc_c_lt_d", "d"),
    "c.le.d": _compare("rc_c_le_d", "d"),
    "madd.s": _madd("rc_madd_s", "s"), "msub.s": _madd("rc_msub_s", "s"),
    "nmadd.s": _madd("rc_nmadd_s", "s"), "nmsub.s": _madd("rc_nmsub_s", "s"),
    "madd.d": _madd("rc_madd_d", "d"),
    "mfc1": _mfc1,
    "mtc1": _mtc1,
}


def _ctc1(i):
    return [f"rt_set_fcsr(c, {_reg(i.rt)});"]


_STRAIGHT = {**_INTEGER, **_MEMORY, **_FLOAT, "ctc1": _ctc1}


def statement(insn, addr):
    """The C statements for `insn` (a mips.Insn) at `addr`; raises UnsupportedInstruction for
    a control transfer, an instruction outside the census, or a `ctc1` of anything but FCSR."""
    emit = _STRAIGHT.get(insn.mnemonic)
    if emit is None or (insn.mnemonic == "ctc1" and insn.rd != 31):
        raise UnsupportedInstruction(addr, insn.mnemonic)
    return emit(insn)


# Every census mnemonic `statement` emits; with CONTROL, the whole census.
STRAIGHT = frozenset(_STRAIGHT)
