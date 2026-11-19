import io
import json
import math
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from carnevil_tools import cli, mips, vectors

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDED = os.path.join(REPO, "tests", "vectors", "insn.json")

# The reached census of docs/analysis/functions.md ("Census of reached code"), copied.
FUNCTIONS_MD_CENSUS = """
addiu lw lui addu sw sll jal lwc1 beq swc1 j bne sh jr ori lhu mul.s andi slti add.s lh
sub.s sra ldc1 mtc1 sdc1 or mfc1 lbu bc1f c.lt.s subu and slt cvt.s.w mov.s beql bnel
blez sltiu div.s sb srl c.le.s bc1t sltu c.eq.s bgez neg.s cvt.d.s trunc.w.s bc1tl
cvt.s.d movz mult xor bc1fl mflo madd.s jalr lb add.d cvt.d.w bltz mul.d break bgtz
movn nor mfhi xori c.lt.d sllv c.le.d bltzl sub.d cache mov.d c.eq.d div msub.s div.d
bgezl addi trunc.w.d madd.d nmsub.s movf blezl divu movt bgtzl ctc1 multu neg.d nmadd.s
recip.s sqrt.s
""".split()

M32 = 0xFFFFFFFF
INT_EDGES = {0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF}


def _first(case):
    return mips.decode(case.words[0], vectors.CODE_BASE)


def _f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _f64(lo, hi):
    return struct.unpack("<d", struct.pack("<II", lo, hi))[0]


def _signed(v):
    return v - (1 << 32) if v & 0x80000000 else v


class AssembleTest(unittest.TestCase):
    def test_round_trips_through_decoder(self):
        for case in vectors.cases(1):
            for i, word in enumerate(case.words):
                insn = mips.decode(word, vectors.CODE_BASE + 4 * i)
                again = vectors.assemble(insn.mnemonic, rs=insn.rs, rt=insn.rt, rd=insn.rd,
                                         sa=insn.sa, imm=insn.imm)
                self.assertEqual(again, word, f"{case.name} word {i} {insn.mnemonic}")

    def test_named_fields(self):
        self.assertEqual(vectors.assemble("addu", rd=2, rs=4, rt=5), 0x00851021)
        self.assertEqual(vectors.assemble("jr", rs=31), 0x03E00008)
        self.assertEqual(vectors.assemble("lui", rt=4, imm=0x8020), 0x3C048020)
        self.assertEqual(vectors.assemble("addiu", rt=2, rs=2, imm=-1), 0x2442FFFF)
        self.assertEqual(vectors.assemble("break", code=0x1C00), 0x0007000D)
        insn = mips.decode(vectors.assemble("c.lt.s", cc=2, fs=4, ft=6), 0)
        self.assertEqual((insn.mnemonic, mips.fp_condition_code(insn), insn.rd, insn.rt),
                         ("c.lt.s", 2, 4, 6))
        insn = mips.decode(vectors.assemble("bc1fl", cc=1, imm=3), 0)
        self.assertEqual((insn.mnemonic, mips.fp_condition_code(insn), insn.imm),
                         ("bc1fl", 1, 3))
        insn = mips.decode(vectors.assemble("madd.s", fd=1, fr=2, fs=3, ft=4), 0)
        self.assertEqual((insn.mnemonic, insn.sa, insn.rs, insn.rd, insn.rt),
                         ("madd.s", 1, 2, 3, 4))

    def test_unknown_mnemonic(self):
        with self.assertRaises(ValueError):
            vectors.assemble("dadd", rd=1, rs=2, rt=3)


class CasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = vectors.cases(1)
        cls.by_mnemonic = {}
        for case in cls.cases:
            cls.by_mnemonic.setdefault(_first(case).mnemonic, []).append(case)

    def test_census_constant_matches_functions_md(self):
        # 98 names: their counts in functions.md sum to the 165,638 reached words. (The M1
        # plan says 105; that figure is a miscount.)
        self.assertEqual(len(FUNCTIONS_MD_CENSUS), 98)
        self.assertEqual(list(vectors.CENSUS), FUNCTIONS_MD_CENSUS)

    def test_every_census_mnemonic_has_cases(self):
        seen = {mips.decode(w, vectors.CODE_BASE + 4 * i).mnemonic
                for case in self.cases for i, w in enumerate(case.words)}
        self.assertLessEqual(set(vectors.CENSUS), seen)
        for name in vectors.CENSUS:
            self.assertGreaterEqual(len(self.by_mnemonic.get(name, [])), 12, name)

    def test_cases_are_deterministic(self):
        self.assertEqual(vectors.cases(1), vectors.cases(1))
        self.assertNotEqual(vectors.cases(2), vectors.cases(1))

    def test_case_shape(self):
        self.assertLess(len(self.cases), 3000)
        self.assertEqual(len({c.name for c in self.cases}), len(self.cases))
        for case in self.cases:
            b = case.before
            self.assertEqual((len(b.r), len(b.fpr)), (32, 32), case.name)
            self.assertEqual((b.r[0], b.r[31]), (0, vectors.RA), case.name)
            self.assertEqual(case.words[-2:], [vectors.assemble("jr", rs=31), 0], case.name)
            self.assertLessEqual(len(case.words) * 4, vectors.RA - vectors.CODE_BASE)
            for addr, value in b.mem.items():
                self.assertTrue(vectors.SCRATCH <= addr < vectors.SCRATCH + 0x1000, case.name)
                self.assertEqual((addr % 4, value != 0), (0, True), case.name)
            self.assertEqual(b.fcsr & ~vectors.FCSR_CC_MASK, 0, case.name)
        no_mame = {c.name for c in self.cases if c.no_mame}
        breaks = {c.name for c in self.cases if _first(c).mnemonic == "break"}
        self.assertEqual(no_mame, breaks | {"div.edge.80000000.FFFFFFFF"})

    def test_edge_operands_present(self):
        for name in ("div", "divu", "sllv", "addiu", "mult"):
            values = set()
            for case in self.by_mnemonic[name]:
                insn = _first(case)
                values.add(case.before.r[insn.rs])
                if name != "addiu":
                    values.add(case.before.r[insn.rt])
            self.assertLessEqual(INT_EDGES, values, name)
        f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]
        for name, conv in (("trunc.w.s", f32), ("trunc.w.d", float)):
            values, nan = set(), False
            for case in self.by_mnemonic[name]:
                fs, fpr = _first(case).rd, case.before.fpr
                v = _f32(fpr[fs]) if name.endswith(".s") else _f64(fpr[fs], fpr[fs + 1])
                if math.isnan(v):
                    nan = True
                else:
                    values.add(v)
            wanted = {conv(float(e)) for e in INT_EDGES} | {conv(3e9), conv(-3e9)}
            self.assertLessEqual(wanted, values, name)
            self.assertTrue(nan, name)

    def test_fp_operands_are_finite_normal(self):
        edge = set(vectors.MAME_SPECIFIC) | {c.name for c in self.cases if ".edge." in c.name}
        for case in self.cases:
            insn = _first(case)
            if case.name in edge or not insn.mnemonic[-2:] in (".s", ".d") \
                    or insn.mnemonic.startswith("cvt.") and insn.mnemonic.endswith(".w"):
                continue
            unary = insn.mnemonic.split(".")[0] in ("mov", "neg", "sqrt", "recip", "cvt", "trunc")
            regs = {insn.rd} | (set() if unary else {insn.rt})
            if insn.word >> 26 == 0x13:
                regs.add(insn.rs)
            for reg in regs:
                if insn.mnemonic.endswith(".s"):
                    exp = (case.before.fpr[reg] >> 23) & 0xFF
                    self.assertTrue(0 < exp < 0xFF, case.name)
                else:
                    exp = (case.before.fpr[reg & ~1 | 1] >> 20) & 0x7FF
                    self.assertTrue(0 < exp < 0x7FF, case.name)

    def test_fp_cc_cases(self):
        def ccs(name):
            return {mips.fp_condition_code(_first(c)) for c in self.by_mnemonic[name]}
        for name in ("c.lt.s", "bc1f", "bc1fl"):
            self.assertLessEqual({1, 2}, ccs(name), name)
        self.assertTrue(self.by_mnemonic["movf"] and self.by_mnemonic["movt"])

    def test_mame_specific_names_exist(self):
        names = {c.name for c in self.cases}
        self.assertLessEqual(set(vectors.MAME_SPECIFIC), names)
        for reason in vectors.MAME_SPECIFIC.values():
            self.assertTrue(reason and "\n" not in reason)


class FilesTest(unittest.TestCase):
    def _run(self, *argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            status = cli.main(list(argv))
        return status, err.getvalue()

    def test_write_merge_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = os.path.join(tmp, "cases.json")
            self.assertEqual(self._run("vectors", "--write-cases", cases_path)[0], 0)
            with open(cases_path) as f:
                data = json.load(f)
            wanted = [c for c in vectors.cases(1) if not c.no_mame]
            self.assertEqual([c["name"] for c in data["cases"]], [c.name for c in wanted])
            # results in the script's format: v0 changed and one word stored
            lines = []
            for c in data["cases"]:
                r = list(c["r"])
                r[2] = 0x12345678
                mem = [m for m in c["mem"] if m[0] != vectors.SCRATCH] + [[vectors.SCRATCH, 7]]
                lines.append(json.dumps(dict(c, r=r, mem=mem, words=None)))
            results = os.path.join(tmp, "results.jsonl")
            with open(results, "w") as f:
                f.write("\n".join(lines) + "\n")
            out = os.path.join(tmp, "insn.json")
            self.assertEqual(self._run("vectors", "--merge", cases_path, results, "-o", out)[0], 0)
            loaded = vectors.load(out)
            self.assertEqual([c for c, _a in loaded], wanted)
            for case, after in loaded:
                self.assertEqual(after.r[2], 0x12345678)
                self.assertEqual(after.r[:2] + after.r[3:], case.before.r[:2] + case.before.r[3:])
                self.assertEqual(after.mem, {**case.before.mem, vectors.SCRATCH: 7})
                self.assertEqual((after.hi, after.lo, after.fpr, after.fcsr),
                                 (case.before.hi, case.before.lo, case.before.fpr,
                                  case.before.fcsr))
            # a failed case and a missing one are both reported, and nothing is written
            with open(results, "w") as f:
                f.write(json.dumps({"name": wanted[0].name, "error": "exception"}) + "\n")
            os.remove(out)
            status, err = self._run("vectors", "--merge", cases_path, results, "-o", out)
            self.assertEqual(status, 1)
            self.assertIn(f"{wanted[0].name}: exception", err)
            self.assertIn(f"{wanted[1].name}: no result", err)
            self.assertFalse(os.path.exists(out))


class RecordedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recorded = vectors.load(RECORDED)

    def test_recorded_vectors_match_cases(self):
        wanted = [c for c in vectors.cases(1) if not c.no_mame]
        self.assertEqual([c for c, _after in self.recorded], wanted)

    def test_file_is_small(self):
        self.assertLess(os.path.getsize(RECORDED), 1_500_000)

    def _of(self, mnemonic):
        return [(c, a) for c, a in self.recorded if _first(c).mnemonic == mnemonic]

    def test_known_results(self):
        addu = [(c, a) for c, a in self._of("addu") if _first(c).rd]
        self.assertTrue(addu)
        for case, after in addu:
            insn = _first(case)
            self.assertEqual(after.r[insn.rd],
                             (case.before.r[insn.rs] + case.before.r[insn.rt]) & M32, case.name)
        mult = self._of("mult")
        self.assertTrue(mult)
        for case, after in mult:
            insn = _first(case)
            product = _signed(case.before.r[insn.rs]) * _signed(case.before.r[insn.rt])
            self.assertEqual((after.hi << 32) | after.lo, product & (1 << 64) - 1, case.name)
        ldc1 = self._of("ldc1")
        self.assertTrue(ldc1)
        for case, after in ldc1:
            insn = _first(case)
            addr = (case.before.r[insn.rs] + insn.imm) & M32
            self.assertEqual((after.fpr[insn.rt], after.fpr[insn.rt + 1]),
                             (case.before.mem.get(addr, 0), case.before.mem.get(addr + 4, 0)),
                             case.name)


if __name__ == "__main__":
    unittest.main()
