import dataclasses
import io
import struct
import unittest
from contextlib import redirect_stderr, redirect_stdout

from carnevil_tools import cli, exe, mips
from tests.gamedata import requires_file, work_path

BASE = 0x80010000


def enc_r(op, rs, rt, rd, sa, funct):
    return (op << 26) | (rs << 21) | (rt << 16) | (rd << 11) | (sa << 6) | funct


def enc_i(op, rs, rt, imm):
    return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def enc_j(op, index):
    return (op << 26) | index


def enc_cop1(fmt, ft, fs, fd, funct):
    return enc_r(0x11, fmt, ft, fs, fd, funct)


def enc_cop1x(base_or_fr, ft, fs, fd, funct):
    return enc_r(0x13, base_or_fr, ft, fs, fd, funct)


S, D, W, L = 16, 17, 20, 21


class DecodeBase(unittest.TestCase):
    def check(self, word, mnemonic, kind="normal", addr=BASE):
        insn = mips.decode(word, addr)
        self.assertEqual((insn.mnemonic, insn.kind), (mnemonic, kind), "0x%08X" % word)
        return insn

    def check_table(self, rows, addr=BASE):
        for word, mnemonic, kind in rows:
            with self.subTest(word="0x%08X" % word, mnemonic=mnemonic):
                self.check(word, mnemonic, kind, addr)


class BriefCases(DecodeBase):
    def test_lui(self):
        for addr in (0, 0x800C4000, 0x80123454):
            insn = self.check(0x3C1D8080, "lui", addr=addr)
            self.assertEqual(insn.rt, 29)
            self.assertEqual(insn.imm, 0x8080)
            self.assertIsNone(insn.target)

    def test_jr_ra(self):
        insn = self.check(0x03E00008, "jr", "jump_reg")
        self.assertEqual(insn.rs, 31)
        self.assertIsNone(insn.target)

    def test_jal(self):
        insn = self.check(0x0C031000, "jal", "call", addr=0x800C4000)
        self.assertEqual(insn.target, 0x800C4000)

    def test_unconditional_branch_stays_beq(self):
        insn = self.check(0x1000FFFF, "beq", "branch", addr=0x80010000)
        self.assertEqual(insn.target, 0x80010000)
        self.assertEqual(insn.imm, -1)

    def test_movn(self):
        insn = self.check(0x0085100B, "movn")
        self.assertEqual((insn.rd, insn.rs, insn.rt), (2, 4, 5))

    def test_madd_s(self):
        word = enc_cop1x(6, 5, 4, 3, 0x20)
        self.assertEqual(word >> 26, 0x13)
        insn = self.check(word, "madd.s")
        self.assertEqual((insn.rs, insn.rt, insn.rd, insn.sa), (6, 5, 4, 3))

    def test_reserved_word_is_unknown(self):
        # The brief's 0xFFFFFFFF is a valid `sd` (opcode 0x3F) on a 64-bit CPU,
        # so the reserved-word case uses opcode 0x1F instead.
        insn = self.check(0x7FFFFFFF, "unknown", "unknown")
        self.assertIsNone(insn.target)
        self.check(0xFFFFFFFF, "sd")

    def test_nop_is_sll(self):
        self.check(0x00000000, "sll")


class InsnShape(unittest.TestCase):
    def test_fields_and_frozen(self):
        names = [f.name for f in dataclasses.fields(mips.Insn)]
        self.assertEqual(
            names, ["word", "mnemonic", "rs", "rt", "rd", "sa", "imm", "target", "kind"]
        )
        insn = mips.decode(0x3C1D8080, 0)
        self.assertEqual(insn.word, 0x3C1D8080)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            insn.rs = 1

    def test_immediate_signedness(self):
        self.assertEqual(mips.decode(enc_i(0x09, 1, 2, 0xFFFF), 0).imm, -1)  # addiu
        self.assertEqual(mips.decode(enc_i(0x0A, 1, 2, 0x8000), 0).imm, -0x8000)  # slti
        self.assertEqual(mips.decode(enc_i(0x23, 29, 4, 0xFFFC), 0).imm, -4)  # lw
        self.assertEqual(mips.decode(enc_i(0x0C, 1, 2, 0xFFFF), 0).imm, 0xFFFF)  # andi
        self.assertEqual(mips.decode(enc_i(0x0D, 1, 2, 0x8001), 0).imm, 0x8001)  # ori
        self.assertEqual(mips.decode(enc_i(0x0E, 1, 2, 0x8001), 0).imm, 0x8001)  # xori
        self.assertEqual(mips.decode(enc_i(0x0F, 0, 2, 0xFFFF), 0).imm, 0xFFFF)  # lui

    def test_shift_amount_and_registers(self):
        insn = mips.decode(enc_r(0, 0, 9, 8, 5, 0x00), 0)  # sll t0,t1,5
        self.assertEqual((insn.mnemonic, insn.rt, insn.rd, insn.sa), ("sll", 9, 8, 5))


class IntegerAlu(DecodeBase):
    def test_special_alu(self):
        rows = [
            (enc_r(0, 0, 2, 3, 4, 0x00), "sll", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x02), "srl", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x03), "sra", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x04), "sllv", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x06), "srlv", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x07), "srav", "normal"),
            (enc_r(0, 0, 0, 3, 0, 0x10), "mfhi", "normal"),
            (enc_r(0, 3, 0, 0, 0, 0x11), "mthi", "normal"),
            (enc_r(0, 0, 0, 3, 0, 0x12), "mflo", "normal"),
            (enc_r(0, 3, 0, 0, 0, 0x13), "mtlo", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x18), "mult", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x19), "multu", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x1A), "div", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x1B), "divu", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x20), "add", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x21), "addu", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x22), "sub", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x23), "subu", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x24), "and", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x25), "or", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x26), "xor", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x27), "nor", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x2A), "slt", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x2B), "sltu", "normal"),
        ]
        self.check_table(rows)

    def test_move_is_not_a_pseudo(self):
        self.check(enc_r(0, 4, 0, 5, 0, 0x25), "or")
        self.check(enc_i(0x09, 0, 5, 7), "addiu")

    def test_immediates(self):
        rows = [
            (enc_i(0x08, 1, 2, 5), "addi", "normal"),
            (enc_i(0x09, 1, 2, 5), "addiu", "normal"),
            (enc_i(0x0A, 1, 2, 5), "slti", "normal"),
            (enc_i(0x0B, 1, 2, 5), "sltiu", "normal"),
            (enc_i(0x0C, 1, 2, 5), "andi", "normal"),
            (enc_i(0x0D, 1, 2, 5), "ori", "normal"),
            (enc_i(0x0E, 1, 2, 5), "xori", "normal"),
            (enc_i(0x0F, 0, 2, 5), "lui", "normal"),
        ]
        self.check_table(rows)

    def test_64bit_alu(self):
        rows = [
            (enc_r(0, 0, 2, 3, 4, 0x38), "dsll", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x3A), "dsrl", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x3B), "dsra", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x3C), "dsll32", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x3E), "dsrl32", "normal"),
            (enc_r(0, 0, 2, 3, 4, 0x3F), "dsra32", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x14), "dsllv", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x16), "dsrlv", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x17), "dsrav", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x1C), "dmult", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x1D), "dmultu", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x1E), "ddiv", "normal"),
            (enc_r(0, 1, 2, 0, 0, 0x1F), "ddivu", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x2C), "dadd", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x2D), "daddu", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x2E), "dsub", "normal"),
            (enc_r(0, 1, 2, 3, 0, 0x2F), "dsubu", "normal"),
            (enc_i(0x18, 1, 2, 5), "daddi", "normal"),
            (enc_i(0x19, 1, 2, 5), "daddiu", "normal"),
        ]
        self.check_table(rows)

    def test_conditional_moves(self):
        self.check(enc_r(0, 4, 5, 2, 0, 0x0A), "movz")
        self.check(enc_r(0, 4, 5, 2, 0, 0x0B), "movn")

    def test_movf_movt_with_condition_codes(self):
        for cc in range(8):
            for tf, name in ((0, "movf"), (1, "movt")):
                with self.subTest(cc=cc, tf=tf):
                    word = enc_r(0, 4, (cc << 2) | tf, 2, 0, 0x01)
                    insn = self.check(word, name)
                    self.assertEqual((insn.rs, insn.rd), (4, 2))
                    self.assertEqual(insn.rt >> 2, cc)
                    self.assertEqual(mips.fp_condition_code(insn), cc)
        # the nd bit (bit 17) must be zero
        self.check(enc_r(0, 4, 2, 2, 0, 0x01), "unknown", "unknown")


class LoadStore(DecodeBase):
    def test_loads_stores(self):
        names = {
            0x20: "lb", 0x21: "lh", 0x22: "lwl", 0x23: "lw", 0x24: "lbu",
            0x25: "lhu", 0x26: "lwr", 0x27: "lwu", 0x28: "sb", 0x29: "sh",
            0x2A: "swl", 0x2B: "sw", 0x2C: "sdl", 0x2D: "sdr", 0x2E: "swr",
            0x30: "ll", 0x34: "lld", 0x37: "ld", 0x38: "sc", 0x3C: "scd",
            0x3F: "sd", 0x1A: "ldl", 0x1B: "ldr",
            0x31: "lwc1", 0x35: "ldc1", 0x39: "swc1", 0x3D: "sdc1",
        }
        for op, name in names.items():
            with self.subTest(name=name):
                insn = self.check(enc_i(op, 29, 8, 0xFFF8), name)
                self.assertEqual((insn.rs, insn.rt, insn.imm), (29, 8, -8))
                self.assertIsNone(insn.target)

    def test_cache_pref_sync(self):
        insn = self.check(enc_i(0x2F, 4, 0x15, 0x20), "cache")
        self.assertEqual((insn.rs, insn.rt, insn.imm), (4, 0x15, 0x20))
        insn = self.check(enc_i(0x33, 4, 1, 0x40), "pref")
        self.assertEqual((insn.rs, insn.rt, insn.imm), (4, 1, 0x40))
        self.check(enc_r(0, 0, 0, 0, 0, 0x0F), "sync")
        self.check(enc_r(0, 0, 0, 0, 3, 0x0F), "sync")


class ControlFlow(DecodeBase):
    def test_branch_targets(self):
        addr = 0x80020000
        insn = self.check(enc_i(0x05, 2, 3, 4), "bne", "branch", addr)
        self.assertEqual(insn.target, addr + 4 + 16)
        insn = self.check(enc_i(0x04, 2, 3, 0xFFF0), "beq", "branch", addr)
        self.assertEqual(insn.target, addr + 4 - 64)

    def test_branch_kinds(self):
        rows = [
            (enc_i(0x04, 1, 2, 3), "beq", "branch"),
            (enc_i(0x05, 1, 2, 3), "bne", "branch"),
            (enc_i(0x06, 1, 0, 3), "blez", "branch"),
            (enc_i(0x07, 1, 0, 3), "bgtz", "branch"),
            (enc_i(0x01, 1, 0, 3), "bltz", "branch"),
            (enc_i(0x01, 1, 1, 3), "bgez", "branch"),
            (enc_i(0x14, 1, 2, 3), "beql", "branch_likely"),
            (enc_i(0x15, 1, 2, 3), "bnel", "branch_likely"),
            (enc_i(0x16, 1, 0, 3), "blezl", "branch_likely"),
            (enc_i(0x17, 1, 0, 3), "bgtzl", "branch_likely"),
            (enc_i(0x01, 1, 2, 3), "bltzl", "branch_likely"),
            (enc_i(0x01, 1, 3, 3), "bgezl", "branch_likely"),
            (enc_i(0x01, 1, 0x10, 3), "bltzal", "call"),
            (enc_i(0x01, 1, 0x11, 3), "bgezal", "call"),
            (enc_i(0x01, 1, 0x12, 3), "bltzall", "call"),
            (enc_i(0x01, 1, 0x13, 3), "bgezall", "call"),
        ]
        for word, name, kind in rows:
            with self.subTest(name=name):
                insn = self.check(word, name, kind)
                self.assertEqual(insn.target, BASE + 4 + 12)

    def test_bgezal_zero_is_bgezal(self):
        insn = self.check(enc_i(0x01, 0, 0x11, 0x10), "bgezal", "call")
        self.assertEqual(insn.target, BASE + 4 + 0x40)

    def test_bc1_condition_codes(self):
        for cc in range(8):
            for nd_tf, name, kind in (
                (0, "bc1f", "branch"),
                (1, "bc1t", "branch"),
                (2, "bc1fl", "branch_likely"),
                (3, "bc1tl", "branch_likely"),
            ):
                with self.subTest(cc=cc, name=name):
                    word = enc_cop1(8, (cc << 2) | nd_tf, 0, 0, 0) | 0x0005
                    insn = self.check(word, name, kind)
                    self.assertEqual(insn.target, BASE + 4 + 20)
                    self.assertEqual(mips.fp_condition_code(insn), cc)

    def test_jumps(self):
        insn = self.check(enc_j(0x02, 0x0012345), "j", "jump", 0x800C4000)
        self.assertEqual(insn.target, 0x80000000 | (0x0012345 << 2))
        # top nibble comes from the delay-slot address
        insn = self.check(enc_j(0x03, 0x0000010), "jal", "call", 0x8FFFFFF8)
        self.assertEqual(insn.target, 0x80000040)
        insn = self.check(enc_j(0x03, 0x0000010), "jal", "call", 0x7FFFFFFC)
        self.assertEqual(insn.target, 0x80000040)

    def test_register_jumps(self):
        insn = self.check(enc_r(0, 25, 0, 31, 0, 0x09), "jalr", "call_reg")
        self.assertEqual((insn.rs, insn.rd), (25, 31))
        self.assertIsNone(insn.target)
        insn = self.check(enc_r(0, 8, 0, 0, 0, 0x08), "jr", "jump_reg")
        self.assertEqual(insn.rs, 8)

    def test_traps(self):
        self.check(enc_r(0, 0, 0, 0, 0, 0x0C) | (0x123 << 6), "syscall", "trap")
        self.check(enc_r(0, 0, 0, 0, 0, 0x0D) | (0x7 << 6), "break", "trap")
        for funct, name in ((0x30, "tge"), (0x31, "tgeu"), (0x32, "tlt"),
                            (0x33, "tltu"), (0x34, "teq"), (0x36, "tne")):
            with self.subTest(name=name):
                self.check(enc_r(0, 1, 2, 0, 0, funct), name, "trap")
        for rt, name in ((8, "tgei"), (9, "tgeiu"), (10, "tlti"),
                         (11, "tltiu"), (12, "teqi"), (14, "tnei")):
            with self.subTest(name=name):
                insn = self.check(enc_i(0x01, 1, rt, 0xFFFF), name, "trap")
                self.assertEqual(insn.imm, -1)
                self.assertIsNone(insn.target)


class Cop0(DecodeBase):
    def test_moves(self):
        for rs, name in ((0, "mfc0"), (1, "dmfc0"), (4, "mtc0"), (5, "dmtc0")):
            with self.subTest(name=name):
                insn = self.check(enc_r(0x10, rs, 7, 12, 0, 0), name)
                self.assertEqual((insn.rt, insn.rd), (7, 12))

    def test_co_instructions(self):
        co = 1 << 25
        rows = [
            (0x10 << 26 | co | 0x01, "tlbr", "normal"),
            (0x10 << 26 | co | 0x02, "tlbwi", "normal"),
            (0x10 << 26 | co | 0x06, "tlbwr", "normal"),
            (0x10 << 26 | co | 0x08, "tlbp", "normal"),
            (0x10 << 26 | co | 0x18, "eret", "jump_reg"),
            (0x10 << 26 | co | 0x20, "wait", "normal"),
        ]
        self.check_table(rows)
        self.assertEqual(mips.decode(0x42000018, 0).mnemonic, "eret")

    def test_reserved(self):
        co = 1 << 25
        self.check(enc_r(0x10, 2, 7, 12, 0, 0), "unknown", "unknown")  # cfc0
        self.check(0x10 << 26 | co | 0x03, "unknown", "unknown")
        self.check(0x10 << 26 | co | 0x3F, "unknown", "unknown")

    def test_bc0(self):
        for rt, name, kind in ((0, "bc0f", "branch"), (1, "bc0t", "branch"),
                               (2, "bc0fl", "branch_likely"), (3, "bc0tl", "branch_likely")):
            with self.subTest(name=name):
                insn = self.check(enc_i(0x10, 8, rt, 2), name, kind)
                self.assertEqual(insn.target, BASE + 4 + 8)


class Cop1(DecodeBase):
    def test_moves(self):
        for fmt, name in ((0, "mfc1"), (1, "dmfc1"), (2, "cfc1"),
                          (4, "mtc1"), (5, "dmtc1"), (6, "ctc1")):
            with self.subTest(name=name):
                insn = self.check(enc_cop1(fmt, 9, 12, 0, 0), name)
                self.assertEqual((insn.rt, insn.rd), (9, 12))

    def test_arithmetic_s_and_d(self):
        ops = {0: "add", 1: "sub", 2: "mul", 3: "div"}
        for funct, base in ops.items():
            for fmt, suffix in ((S, "s"), (D, "d")):
                with self.subTest(op=base, fmt=suffix):
                    insn = self.check(enc_cop1(fmt, 3, 4, 5, funct), "%s.%s" % (base, suffix))
                    self.assertEqual((insn.rs, insn.rt, insn.rd, insn.sa), (fmt, 3, 4, 5))

    def test_unary_s_and_d(self):
        ops = {4: "sqrt", 5: "abs", 6: "mov", 7: "neg", 8: "round.l", 9: "trunc.l",
               10: "ceil.l", 11: "floor.l", 12: "round.w", 13: "trunc.w",
               14: "ceil.w", 15: "floor.w", 21: "recip", 22: "rsqrt"}
        for funct, base in ops.items():
            for fmt, suffix in ((S, "s"), (D, "d")):
                with self.subTest(op=base, fmt=suffix):
                    self.check(enc_cop1(fmt, 0, 4, 5, funct), "%s.%s" % (base, suffix))

    def test_conversions(self):
        rows = [
            (enc_cop1(D, 0, 4, 5, 0x20), "cvt.s.d"),
            (enc_cop1(W, 0, 4, 5, 0x20), "cvt.s.w"),
            (enc_cop1(L, 0, 4, 5, 0x20), "cvt.s.l"),
            (enc_cop1(S, 0, 4, 5, 0x21), "cvt.d.s"),
            (enc_cop1(W, 0, 4, 5, 0x21), "cvt.d.w"),
            (enc_cop1(L, 0, 4, 5, 0x21), "cvt.d.l"),
            (enc_cop1(S, 0, 4, 5, 0x24), "cvt.w.s"),
            (enc_cop1(D, 0, 4, 5, 0x24), "cvt.w.d"),
            (enc_cop1(S, 0, 4, 5, 0x25), "cvt.l.s"),
            (enc_cop1(D, 0, 4, 5, 0x25), "cvt.l.d"),
        ]
        self.check_table([(w, m, "normal") for w, m in rows])

    def test_compares_with_condition_codes(self):
        conds = ["f", "un", "eq", "ueq", "olt", "ult", "ole", "ule",
                 "sf", "ngle", "seq", "ngl", "lt", "nge", "le", "ngt"]
        for i, cond in enumerate(conds):
            for fmt, suffix in ((S, "s"), (D, "d")):
                with self.subTest(cond=cond, fmt=suffix):
                    insn = self.check(
                        enc_cop1(fmt, 3, 4, 0, 0x30 + i), "c.%s.%s" % (cond, suffix)
                    )
                    self.assertEqual(mips.fp_condition_code(insn), 0)
        for cc in range(8):
            with self.subTest(cc=cc):
                word = enc_cop1(D, 3, 4, cc << 2, 0x32)
                insn = self.check(word, "c.eq.d")
                self.assertEqual(mips.fp_condition_code(insn), cc)
        # bits 7:6 of a compare must be zero
        self.check(enc_cop1(S, 3, 4, 1, 0x32), "unknown", "unknown")

    def test_fp_conditional_moves(self):
        for cc in range(8):
            for tf, name in ((0, "movf"), (1, "movt")):
                for fmt, suffix in ((S, "s"), (D, "d")):
                    with self.subTest(cc=cc, name=name, fmt=suffix):
                        word = enc_cop1(fmt, (cc << 2) | tf, 4, 5, 0x11)
                        insn = self.check(word, "%s.%s" % (name, suffix))
                        self.assertEqual(mips.fp_condition_code(insn), cc)
        for fmt, suffix in ((S, "s"), (D, "d")):
            self.check(enc_cop1(fmt, 3, 4, 5, 0x12), "movz.%s" % suffix)
            self.check(enc_cop1(fmt, 3, 4, 5, 0x13), "movn.%s" % suffix)
        self.check(enc_cop1(S, 2, 4, 5, 0x11), "unknown", "unknown")  # nd bit set

    def test_fp_condition_code_none_elsewhere(self):
        self.assertIsNone(mips.fp_condition_code(mips.decode(0x00000000, 0)))
        self.assertIsNone(mips.fp_condition_code(mips.decode(enc_cop1(S, 3, 4, 5, 0), 0)))

    def test_reserved(self):
        rows = [
            enc_cop1(3, 0, 0, 0, 0),             # fmt 3 does not exist
            enc_cop1(7, 0, 0, 0, 0),
            enc_cop1(18, 3, 4, 5, 0),            # fmt 18
            enc_cop1(22, 3, 4, 5, 0),            # fmt 22 (ps) is not in the R5000
            enc_cop1(31, 3, 4, 5, 0),
            enc_cop1(9, 0, 0, 0, 0),             # rs 9..15 undefined
            enc_cop1(S, 3, 4, 5, 0x20),          # cvt.s.s
            enc_cop1(D, 3, 4, 5, 0x21),          # cvt.d.d
            enc_cop1(S, 0, 4, 5, 0x23),          # reserved
            enc_cop1(S, 3, 4, 5, 0x10),          # reserved funct
            enc_cop1(S, 3, 4, 5, 0x14),
            enc_cop1(S, 3, 4, 5, 0x17),
            enc_cop1(W, 3, 4, 5, 0x00),          # add.w does not exist
            enc_cop1(L, 3, 4, 5, 0x02),
            enc_cop1(W, 0, 4, 5, 0x24),          # cvt.w.w
            enc_cop1(W, 3, 4, 5, 0x30),          # compare on W
            enc_cop1(S, 3, 4, 5, 0x18),
            enc_cop1(S, 1, 4, 5, 0x04),          # sqrt.s with ft != 0
        ]
        for word in rows:
            with self.subTest(word="0x%08X" % word):
                self.check(word, "unknown", "unknown")
        self.check(enc_cop1(0, 0, 0, 0, 1), "unknown", "unknown")  # mfc1 with funct bits


class Cop1X(DecodeBase):
    def test_indexed_loads_stores(self):
        insn = self.check(enc_cop1x(4, 5, 0, 6, 0x00), "lwxc1")
        self.assertEqual((insn.rs, insn.rt, insn.sa), (4, 5, 6))
        self.check(enc_cop1x(4, 5, 0, 6, 0x01), "ldxc1")
        insn = self.check(enc_cop1x(4, 5, 6, 0, 0x08), "swxc1")
        self.assertEqual((insn.rs, insn.rt, insn.rd), (4, 5, 6))
        self.check(enc_cop1x(4, 5, 6, 0, 0x09), "sdxc1")
        insn = self.check(enc_cop1x(4, 5, 1, 0, 0x0F), "prefx")
        self.assertEqual((insn.rs, insn.rt, insn.rd), (4, 5, 1))

    def test_multiply_add_family(self):
        for base, name in ((0x20, "madd"), (0x28, "msub"), (0x30, "nmadd"), (0x38, "nmsub")):
            for fmt, suffix in ((0, "s"), (1, "d")):
                with self.subTest(name=name, fmt=suffix):
                    self.check(enc_cop1x(6, 5, 4, 3, base | fmt), "%s.%s" % (name, suffix))

    def test_reserved(self):
        rows = [
            enc_cop1x(6, 5, 4, 3, 0x22),  # madd.ps is not in the R5000
            enc_cop1x(6, 5, 4, 3, 0x23),
            enc_cop1x(6, 5, 4, 3, 0x2A),
            enc_cop1x(6, 5, 4, 3, 0x02),
            enc_cop1x(6, 5, 4, 3, 0x10),
            enc_cop1x(6, 5, 4, 3, 0x3F),
            enc_cop1x(6, 5, 4, 3, 0x00),  # lwxc1 with nonzero bits 15:11
            enc_cop1x(6, 5, 4, 3, 0x0F),  # prefx with nonzero bits 10:6
        ]
        for word in rows:
            with self.subTest(word="0x%08X" % word):
                self.check(word, "unknown", "unknown")


class Reserved(DecodeBase):
    def test_unassigned_primary_opcodes(self):
        for op in (0x12, 0x1C, 0x1D, 0x1E, 0x1F, 0x32, 0x36, 0x3A, 0x3B, 0x3E):
            with self.subTest(op=op):
                self.check(enc_i(op, 1, 2, 3), "unknown", "unknown")

    def test_cop2_cop3_family(self):
        # COP2 (0x12), LWC2/SWC2/LDC2/SDC2, and the old COP3 slot other than COP1X
        for op in (0x12, 0x32, 0x3A, 0x36, 0x3E):
            self.check(enc_i(op, 0, 0, 0), "unknown", "unknown")

    def test_unassigned_special_functs(self):
        for funct in (0x05, 0x0E, 0x15, 0x28, 0x29, 0x35, 0x37, 0x39, 0x3D):
            with self.subTest(funct=funct):
                self.check(enc_r(0, 1, 2, 3, 0, funct), "unknown", "unknown")

    def test_unassigned_regimm(self):
        for rt in (4, 5, 6, 7, 13, 15, 20, 21, 31):
            with self.subTest(rt=rt):
                self.check(enc_i(0x01, 1, rt, 0), "unknown", "unknown")

    def test_nonzero_reserved_fields(self):
        self.check(enc_r(0, 1, 0, 0, 0, 0x08) | (3 << 16), "unknown", "unknown")  # jr, rt != 0
        self.check(enc_r(0, 1, 2, 0, 0, 0x00), "unknown", "unknown")  # sll with rs != 0

    def test_unknown_has_no_target(self):
        for word in (0xFFFFFFFF, enc_j(0x1F, 5), enc_i(0x1C, 0, 0, 4)):
            self.assertIsNone(mips.decode(word, BASE).target)


class CensusTests(unittest.TestCase):
    def make_image(self, words, load=0x80010000):
        data = b"".join(struct.pack("<I", w) for w in words)
        return exe.ExeImage(0, load, data, "x")

    def test_counts_mnemonics(self):
        words = [0x3C1D8080, 0x03E00008, 0x00000000, 0x00000000, 0x7FFFFFFF, 0x0085100B]
        image = self.make_image(words)
        counts = mips.census(image, 0x80010000, 0x80010000 + 4 * len(words))
        self.assertEqual(
            counts, {"lui": 1, "jr": 1, "sll": 2, "unknown": 1, "movn": 1}
        )

    def test_range_is_half_open(self):
        image = self.make_image([0x3C1D8080, 0x03E00008, 0x00000000])
        counts = mips.census(image, 0x80010004, 0x80010008)
        self.assertEqual(counts, {"jr": 1})
        self.assertEqual(mips.census(image, 0x80010004, 0x80010004), {})

    def test_branch_target_uses_address(self):
        word = enc_i(0x04, 0, 0, 3)
        image = self.make_image([0, word], load=0x80020000)
        found = [(a, i.target) for a, i in mips.walk(image, 0x80020000, 0x80020008) if i.kind == "branch"]
        self.assertEqual(found, [(0x80020004, 0x80020004 + 4 + 12)])

    def test_mips_iv_and_cop0_sets(self):
        for name in ("movn", "movz", "movf", "movt", "movf.s", "movt.d", "movn.d", "movz.s",
                     "lwxc1", "ldxc1", "swxc1", "sdxc1", "madd.s", "msub.d", "nmadd.s",
                     "nmsub.d", "prefx", "pref", "recip.s", "rsqrt.d"):
            self.assertIn(name, mips.MIPS_IV_MNEMONICS)
        for name in ("mfc0", "mtc0", "dmfc0", "dmtc0", "tlbr", "tlbwi", "tlbwr", "tlbp",
                     "eret", "wait", "cache"):
            self.assertIn(name, mips.COP0_MNEMONICS)
        self.assertNotIn("addu", mips.MIPS_IV_MNEMONICS)


class GameExeTests(unittest.TestCase):
    @requires_file("part0_files", "GAME.EXE")
    def test_census_of_game_exe(self):
        image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        end = image.end_addr() & ~3
        counts = mips.census(image, image.load_addr, end)
        self.assertEqual(sum(counts.values()), (end - image.load_addr) // 4)
        self.assertGreater(counts["lw"], 1000)
        self.assertGreater(counts["jal"], 100)

    @requires_file("part0_files", "GAME.EXE")
    def test_cli_census(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["census", work_path("part0_files", "GAME.EXE")])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("unknown", out)
        self.assertIn("lw", out)

    def test_cli_census_missing_file(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main(["census", "/nonexistent/GAME.EXE"])
        self.assertEqual(rc, 2)
        self.assertIn("not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
