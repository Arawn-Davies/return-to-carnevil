import io
import json
import math
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction

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


def _round(x, fmt):
    """An exact Fraction rounded to nearest-even single ("s") or double ("d"), normal range."""
    p, emin = (24, -126) if fmt == "s" else (53, -1022)
    if x == 0:
        return Fraction(0)
    a = abs(x)
    e = a.numerator.bit_length() - a.denominator.bit_length()
    e += (Fraction(2) ** (e + 1) <= a) - (Fraction(2) ** e > a)
    assert e >= emin, "subnormal"
    q = a / Fraction(2) ** (e - p + 1)
    n, rem = divmod(q.numerator, q.denominator)
    if 2 * rem > q.denominator or 2 * rem == q.denominator and n & 1:
        n += 1
    return (1 if x > 0 else -1) * n * Fraction(2) ** (e - p + 1)


def _fp(fpr, reg, fmt):
    return Fraction(_f32(fpr[reg]) if fmt == "s" else _f64(fpr[reg], fpr[reg + 1]))


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
            # condition bits only, and FS in the FS cases: rounding mode 0, no enables
            fs = vectors.FCSR_FS if ".edge.fs1." in case.name else 0
            self.assertEqual(b.fcsr & ~vectors.FCSR_CC_MASK, fs, case.name)
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
        edge = {c.name for c in self.cases if ".edge." in c.name}
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

    def test_fs_cases(self):
        """Each FS case runs with FCSR.FS = 1 and has a control with FS = 0, the same program
        and the same state otherwise; the denormal-result ones have normal operands whose
        exact result is denormal, the denormal-operand ones a denormal operand."""
        fs = {c.name: c for c in self.cases if ".edge.fs" in c.name}
        self.assertEqual(len(fs), 32)
        self.assertEqual(list(fs), [c.name for c in self.cases[-32:]])  # appended last
        kinds = set()
        for name, case in fs.items():
            if ".edge.fs0." in name:
                continue
            control = fs[name.replace(".edge.fs1.", ".edge.fs0.")]
            self.assertEqual(case.before.fcsr, control.before.fcsr | vectors.FCSR_FS, name)
            self.assertEqual((case.words, case.before.r, case.before.fpr),
                             (control.words, control.before.r, control.before.fpr), name)
            insn = _first(case)
            fmt = insn.mnemonic[-1]
            regs = [insn.rd] + ([insn.rt] if insn.mnemonic[:4] in ("mul.", "div.", "add.",
                                                                    "sub.", "c.eq", "c.lt")
                                else [])
            values = [_fp(case.before.fpr, r, fmt) for r in regs]
            tiny = Fraction(2) ** (-126 if fmt == "s" else -1022)
            denormal = [v != 0 and abs(v) < tiny for v in values]
            label = name.rsplit(".", 1)[1]
            kinds.add((insn.mnemonic, label))
            if label == "denormal-result":
                self.assertFalse(any(denormal), name)
                op = insn.mnemonic.split(".")[0]
                exact = {"mul": lambda a, b: a * b, "div": lambda a, b: a / b,
                         "add": lambda a, b: a + b, "sub": lambda a, b: a - b,
                         "cvt": lambda a: a}[op](*values)
                out = "s" if insn.mnemonic == "cvt.s.d" else fmt
                low, limit = ((Fraction(2) ** -149, Fraction(2) ** -126) if out == "s"
                              else (Fraction(2) ** -1074, Fraction(2) ** -1022))
                self.assertTrue(low <= abs(exact) < limit, name)  # in the denormal range
            else:
                self.assertEqual(label, "denormal-operand", name)
                self.assertTrue(any(denormal), name)
        self.assertEqual(len(kinds), 16)
        # MAME ignores FS, so every FS = 1 result is MAME's, not the R5000's
        fs1 = {n for n in fs if ".edge.fs1." in n}
        self.assertEqual({n for n, why in vectors.MAME_SPECIFIC.items()
                          if "MAME does not model FS" in why}, fs1)
        self.assertEqual({m for m, _ in kinds},
                         {"mul.s", "div.s", "add.s", "sub.s", "cvt.s.d", "mul.d", "div.d",
                          "add.d", "sub.d", "cvt.d.s", "c.eq.s", "c.lt.s"})

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


    def test_conflicting_flags_are_refused(self):
        # --seed only means something with --write-cases, -o only with --merge
        for argv in (["vectors", "--merge", "c.json", "r.jsonl", "-o", "x.json", "--seed", "2"],
                     ["vectors", "--write-cases", "c.json", "-o", "x.json"],
                     ["vectors", "--merge", "c.json", "r.jsonl"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as err:
                    self._run(*argv)
                self.assertEqual(err.exception.code, 2)

    def test_merge_refuses_extra_and_duplicate_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = os.path.join(tmp, "cases.json")
            wanted = [c for c in vectors.cases(1) if not c.no_mame][:3]
            vectors.write_cases(wanted, cases_path)
            with open(cases_path) as f:
                lines = [json.dumps(dict(c, words=None)) for c in json.load(f)["cases"]]
            out = os.path.join(tmp, "insn.json")
            results = os.path.join(tmp, "results.jsonl")
            extra = json.loads(lines[0])
            extra["name"] = "not.a.case"
            for bad, why in ((lines + [lines[1]], wanted[1].name + ": more than one result"),
                             (lines + [json.dumps(extra)], "not.a.case: not a case")):
                with self.subTest(why=why):
                    with open(results, "w") as f:
                        f.write("\n".join(bad) + "\n")
                    with self.assertRaises(ValueError) as err:
                        vectors.merge(cases_path, results, out)
                    self.assertIn(why, str(err.exception))
                    self.assertFalse(os.path.exists(out))
            with open(results, "w") as f:
                f.write("\n".join(lines) + "\n")
            self.assertEqual(vectors.merge(cases_path, results, out), 3)


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

    def test_madd_family_is_unfused(self):
        # fd = +-(fs * ft +- fr): MAME rounds the product, then the sum, then negates. Every
        # case where a single rounding gives other bits is listed as MAME-specific.
        differ, seen = set(), 0
        for case, after in self.recorded:
            insn = _first(case)
            op, _, fmt = insn.mnemonic.partition(".")
            if op not in ("madd", "msub", "nmadd", "nmsub"):
                continue
            seen += 1
            fpr = case.before.fpr
            fr, fs, ft = (_fp(fpr, reg, fmt) for reg in (insn.rs, insn.rd, insn.rt))
            if op in ("msub", "nmsub"):
                fr = -fr
            fused, unfused = _round(fs * ft + fr, fmt), _round(_round(fs * ft, fmt) + fr, fmt)
            if op.startswith("n"):
                fused, unfused = -fused, -unfused
            self.assertEqual(_fp(after.fpr, insn.sa, fmt), unfused, case.name)
            if fused != unfused:
                differ.add(case.name)
        self.assertEqual(seen, 5 * vectors.RANDOM_PER_MNEMONIC)
        self.assertTrue(differ)
        listed = {n for n in vectors.MAME_SPECIFIC if n.split(".")[0] in
                  ("madd", "msub", "nmadd", "nmsub")}
        self.assertEqual(differ, listed)

    def test_recip_and_sqrt_are_correctly_rounded(self):
        # MAME's recip.s is 1.0f / x; the R5000's accuracy for recip is not verified, so every
        # recip.s case is listed as MAME-specific
        recip = {c.name for c, _a in self._of("recip.s")}
        self.assertEqual(len(recip), 12)
        self.assertLessEqual(recip, set(vectors.MAME_SPECIFIC))
        for case, after in self.recorded:
            insn = _first(case)
            if insn.mnemonic not in ("recip.s", "sqrt.s"):
                continue
            x, got = _fp(case.before.fpr, insn.rd, "s"), after.fpr[insn.sa]
            if insn.mnemonic == "recip.s":
                self.assertEqual(Fraction(_f32(got)), _round(1 / x, "s"), case.name)
            else:  # got is the nearest single to sqrt(x): x lies between the midpoints squared
                below, at, above = (Fraction(_f32(got + d)) for d in (-1, 0, 1))
                self.assertTrue(((below + at) / 2) ** 2 <= x <= ((at + above) / 2) ** 2,
                                case.name)


if __name__ == "__main__":
    unittest.main()
