"""Emitted C for straight-line instructions, compiled into the test harness and run against
MAME's recorded results and against the reference interpreter.

One harness binary holds every function this class runs: each distinct straight-line program
(the words of a case before its final `jr ra; nop`) becomes `void fn_<hash>(ram, c)` made of
the statements `emit.statement` returns, and cases select their function by name.
"""
import hashlib
import os
import random
import struct
import unittest
from fractions import Fraction

from carnevil_tools import emit, mips, vectors
from tests import charness

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDED = os.path.join(REPO, "tests", "vectors", "insn.json")

A = vectors.assemble
JR_RA = A("jr", rs=31)
T0, T1, T2, T3, T4 = 8, 9, 10, 11, 12
RANDOM_STATES = 200
SPECIAL_BITS = (0, 0x80000000, 0x7F800000, 0xFF800000, 0x7FC00000, 0x7FA00000, 0xFFC12345,
                0x00000001, 0x007FFFFF, 0x80000001, 0x3F800000, 0xBF800000, 0xFFFFFFFF)


def straight_body(words):
    """The words before the final `jr ra; nop` if no one of them transfers control, else
    None."""
    if len(words) < 2 or words[-2:] != [JR_RA, 0]:
        return None
    body = words[:-2]
    for i, word in enumerate(body):
        if mips.decode(word, vectors.CODE_BASE + 4 * i).mnemonic in emit.CONTROL:
            return None
    return body


def statements(body):
    out = []
    for i, word in enumerate(body):
        addr = vectors.CODE_BASE + 4 * i
        out += emit.statement(mips.decode(word, addr), addr)
    return out


def function_name(body):
    digest = hashlib.sha256(",".join(f"{w:08X}" for w in body).encode()).hexdigest()
    return "fn_" + digest[:16]


def _f32(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _from_f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def random_state_cases():
    """RANDOM_STATES seeded cases per straight-line mnemonic, from the vector generators, with
    FP registers sometimes set to special values and random FS and rounding mode in FCSR."""
    out = []
    for name in vectors.CENSUS:
        if name in emit.CONTROL:
            continue
        rng = random.Random(f"emit.{name}")
        for i in range(RANDOM_STATES):
            words, before = vectors._GENERATORS[name](rng, i)
            assert straight_body(words) is not None, name
            for reg in range(32):
                if rng.random() < 0.1:
                    before.fpr[reg] = rng.choice(SPECIAL_BITS)
            before.fcsr = (before.fcsr & vectors.FCSR_CC_MASK | rng.getrandbits(1) << 24
                           | rng.getrandbits(2))
            out.append(vectors.Case(f"rand.{name}.{i}", words, before))
    return out


def unfused_and_fused_madd():
    """Single operands (fr, fs, ft bits) of fs * ft + fr whose result differs between two
    roundings (what the R5000 and MAME do) and one: (operands, unfused bits, fused bits)."""
    rng = random.Random("madd")
    while True:
        a, b = _f32(rng.uniform(1, 2)), _f32(rng.uniform(1, 2))
        product = _from_f32(a) * _from_f32(b)  # exact: 48 significant bits
        c = _f32(-rng.uniform(1, 4))
        total = Fraction(product) + Fraction(_from_f32(c))
        fused = _f32(float(total))  # one rounding of the exact sum (53 bits is enough here)
        unfused = _f32(_from_f32(_f32(product)) + _from_f32(c))
        if fused != unfused:
            return (c, a, b), unfused, fused


class EmitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        recorded = vectors.load(RECORDED)
        cls.recorded = [(c, after) for c, after in recorded if straight_body(c.words)]
        cls.vector_cases = [c for c in vectors.cases(1) if straight_body(c.words)]
        cls.random_cases = random_state_cases()
        (cls.madd_operands, cls.madd_unfused, cls.madd_fused) = unfused_and_fused_madd()
        cls.extra = cls.extra_cases()

        bodies = {}
        for case in [c for c, _ in cls.recorded] + cls.vector_cases + cls.random_cases \
                + list(cls.extra.values()):
            body = straight_body(case.words)
            bodies[function_name(body)] = body
        source = "".join(charness.wrap_function(n, statements(b)) + "\n"
                         for n, b in sorted(bodies.items()))
        # a deliberately undefined shift, to prove the build traps undefined behaviour
        source += charness.wrap_function("ub_shift", ["c->r[2] = 1u << c->r[1];"]) + "\n"
        source += charness.register_named(sorted(bodies) + ["ub_shift"])
        cls.exe = charness.build([charness.write_generated(source)])

    @classmethod
    def extra_cases(cls):
        """Hand-made cases, by the name the tests use."""
        c, a, b = cls.madd_operands  # fr, fs, ft
        return {
            "madd": charness.make_case(
                "madd-unfused", A("madd.s", fd=4, fr=1, fs=2, ft=3), fpr={1: c, 2: a, 3: b}),
            "zero": charness.make_case(
                "zero-writes", A("addu", rd=0, rs=T0, rt=T1), A("lui", rt=0, imm=0x1234),
                A("addiu", rt=0, rs=T0, imm=7), A("sll", rd=0, rt=T0, sa=3),
                A("mfc1", rt=0, fs=2), A("mfhi", rd=0), A("mflo", rd=0),
                A("slt", rd=0, rs=T0, rt=T1), A("movn", rd=0, rs=T0, rt=T1),
                A("movt", rd=0, rs=T0, cc=0), A("ori", rt=0, rs=T0, imm=0xFFFF),
                r={T0: 0x12345678, T1: 5}, fpr={2: 0x99}),
            "slt-same": charness.make_case(
                "slt-same", A("slt", rd=T2, rs=T0, rt=T0), A("sltu", rd=T3, rs=T1, rt=T1),
                A("sltu", rd=T4, rs=0, rt=0), r={T0: 5, T1: 0xFFFFFFFF, T2: 9, T3: 9, T4: 9}),
            "load-zero": charness.make_case(
                "load-zero", A("lui", rt=T0, imm=0xA800), A("lw", rt=0, rs=T0, imm=0),
                A("lb", rt=0, rs=T0, imm=4), A("lhu", rt=0, rs=T0, imm=8)),
            "bus-load": charness.make_case(
                "lw-zero-t0", A("lw", rt=0, rs=T0, imm=0), r={T0: 0xA8000000}),
        }

    def run_recomp(self, cases):
        cases = list(cases)
        functions = {c.name: function_name(straight_body(c.words)) for c in cases}
        return charness.run(self.exe, "recomp", cases, functions)

    def run_interp(self, cases):
        cases = list(cases)
        return charness.run(self.exe, "interp", cases)

    def compare(self, cases, wanted):
        """Fail listing the cases whose after-state differs from `wanted` (a list of states)."""
        got = self.run_recomp(cases)
        failures = []
        for case, want, state in zip(cases, wanted, got):
            diff = charness.first_difference(want, state)
            if diff:
                failures.append(f"{case.name}: {diff}")
        self.assertEqual(failures, [], f"{len(failures)} cases differ:\n" + "\n".join(failures))

    def test_recompiled_matches_mame_vectors(self):
        # 1320 recorded cases (break has none) less the 240 of the 20 branches and jumps
        self.assertEqual(len(self.recorded), 1080)
        self.compare([c for c, _ in self.recorded], [after for _, after in self.recorded])

    def assertSameAsInterpreter(self, cases):
        cases = list(cases)
        want, got = self.run_interp(cases), self.run_recomp(cases)
        failures = []
        for case, u, v in zip(cases, want, got):
            diff = charness.first_difference(u, v)
            if diff is None and (u.calls != v.calls or v.unrecompiled):
                diff = f"runtime calls: want {u.calls} got {v.calls}"
            if diff:
                failures.append(f"{case.name}: {diff}")
        self.assertEqual(failures, [], f"{len(failures)} cases differ:\n" + "\n".join(failures))

    def test_recompiled_matches_interpreter_on_random_states(self):
        straight = [n for n in vectors.CENSUS if n not in emit.CONTROL]
        self.assertEqual(len(straight), 77)
        self.assertEqual(len(self.random_cases), RANDOM_STATES * 77)
        self.assertSameAsInterpreter(self.random_cases)

    def test_recompiled_matches_interpreter_on_every_straight_line_vector(self):
        names = {c.name for c in self.vector_cases}
        # no MAME vector: INT_MIN / -1 (MAME's host division faults)
        self.assertIn("div.edge.80000000.FFFFFFFF", names)
        self.assertSameAsInterpreter(self.vector_cases)
        int_min, = [c for c in self.vector_cases if c.name == "div.edge.80000000.FFFFFFFF"]
        got, = self.run_recomp([int_min])
        self.assertEqual((got.lo, got.hi), (0x80000000, 0))

    def test_every_census_mnemonic_is_straight_line_or_control(self):
        self.assertEqual(len(emit.CONTROL), 21)
        for name in vectors.CENSUS:
            kwargs = {"fs": 31} if name == "ctc1" else {}
            insn = mips.decode(A(name, **kwargs), vectors.CODE_BASE)
            self.assertEqual(insn.mnemonic, name)
            if name in emit.CONTROL:
                with self.assertRaises(emit.UnsupportedInstruction) as err:
                    emit.statement(insn, 0x80123456)
                self.assertEqual((err.exception.addr, err.exception.mnemonic),
                                 (0x80123456, name))
            else:
                self.assertIsInstance(emit.statement(insn, vectors.CODE_BASE), list)

    def test_unknown_mnemonic_raises(self):
        mfc0 = 0x40086000  # mfc0 t0, $12
        insn = mips.decode(mfc0, 0x80100010)
        self.assertEqual(insn.mnemonic, "mfc0")
        with self.assertRaises(emit.UnsupportedInstruction) as err:
            emit.statement(insn, 0x80100010)
        self.assertEqual(err.exception.addr, 0x80100010)
        self.assertEqual(err.exception.mnemonic, "mfc0")
        self.assertIn("80100010", str(err.exception))
        self.assertIn("mfc0", str(err.exception))
        # not in the census although the interpreter runs it; ctc1 of anything but FCSR
        for word in (A("cfc1", rt=T0, fs=31), A("ctc1", rt=T0, fs=30), 0x7C000000):
            with self.assertRaises(emit.UnsupportedInstruction):
                emit.statement(mips.decode(word, 0x80100000), 0x80100000)

    def test_addi_is_addiu_and_cache_is_a_comment(self):
        word = A("addi", rt=T0, rs=T1, imm=-5)
        addi = emit.statement(mips.decode(word, 0), 0)
        addiu = emit.statement(mips.decode(A("addiu", rt=T0, rs=T1, imm=-5), 0), 0)
        self.assertEqual(addi, addiu)
        cache = emit.statement(mips.decode(A("cache", op=0x15, base=T0, imm=4), 0), 0)
        self.assertEqual(len(cache), 1)
        self.assertTrue(cache[0].startswith("/*") and cache[0].endswith("*/"))
        ctc1 = emit.statement(mips.decode(A("ctc1", rt=T0, fs=31), 0), 0)
        self.assertEqual(ctc1, [f"rt_set_fcsr(c, c->r[{T0}]);"])

    def test_write_to_zero_is_dropped(self):
        case = self.extra["zero"]
        for word in case.words[:-2]:
            self.assertEqual(emit.statement(mips.decode(word, 0), 0), [], hex(word))
        got, = self.run_recomp([case])
        self.assertEqual(got.r[0], 0)
        self.assertIsNone(charness.first_difference(case.before, got))

    def test_compare_of_a_register_with_itself(self):
        # the compiler rejects `x < x` under -Werror, so the emitter writes the 0 out
        got, = self.run_recomp([self.extra["slt-same"]])
        self.assertEqual((got.r[T2], got.r[T3], got.r[T4]), (0, 0, 0))

    def test_load_into_zero_still_reads(self):
        got, = self.run_recomp([self.extra["bus-load"]])
        self.assertEqual(got.calls, [("bus_read", 0xA8000000, 4)])
        self.assertEqual(got.r[0], 0)
        got, = self.run_recomp([self.extra["load-zero"]])
        self.assertEqual(got.calls, [("bus_read", 0xA8000000, 4), ("bus_read", 0xA8000004, 1),
                                     ("bus_read", 0xA8000008, 2)])
        self.assertEqual(got.r[0], 0)

    def test_madd_is_not_fused(self):
        self.assertNotEqual(self.madd_unfused, self.madd_fused)
        case = self.extra["madd"]
        got, = self.run_recomp([case])
        self.assertEqual(got.fpr[4], self.madd_unfused,
                         f"fused would be {self.madd_fused:08X}")
        self.assertEqual(self.run_interp([case])[0].fpr[4], self.madd_unfused)

    def test_no_undefined_behaviour(self):
        # The harness is built with the sanitizer: the vector and random runs above pass in it.
        # Prove it traps: a function that shifts by 32 is reported and fails the run.
        self.assertIn("-fsanitize=undefined", charness.SANITIZE)
        shift = charness.make_case("shift32", r={1: 32})
        with self.assertRaises(charness.HarnessError):
            charness.run(self.exe, "recomp", [shift], {"shift32": "ub_shift"})
        shift.before.r[1] = 31
        got, = charness.run(self.exe, "recomp", [shift], {"shift32": "ub_shift"})
        self.assertEqual(got.r[2], 0x80000000)
        self.compare([c for c, _ in self.recorded], [after for _, after in self.recorded])


if __name__ == "__main__":
    unittest.main()
