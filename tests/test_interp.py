"""The reference interpreter (runtime/interp) against MAME's recorded results, and the
contract of runtime/include/recomp.h that the harness exercises: memory routing, calls,
FCSR and the host FP environment."""
import os
import struct
import unittest

from carnevil_tools import vectors
from tests import charness

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDED = os.path.join(REPO, "tests", "vectors", "insn.json")

A = vectors.assemble
NOP = 0
JR_RA = A("jr", rs=31)
T0, T1, T2, T3, V0 = 8, 9, 10, 11, 2


def _f32(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


_case = charness.make_case
first_difference = charness.first_difference


class InterpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = charness.build([])
        cls.recorded = vectors.load(RECORDED)

    def run_cases(self, cases):
        return charness.run(self.exe, "interp", cases)

    def run_one(self, case):
        return self.run_cases([case])[0]

    def assertMatches(self, pairs):
        results = self.run_cases([case for case, _after in pairs])
        self.assertEqual(len(results), len(pairs))
        failures = []
        for (case, want), got in zip(pairs, results):
            diff = first_difference(want, got)
            if diff:
                failures.append(f"{case.name}: {diff}")
        self.assertEqual(failures, [], f"{len(failures)} cases differ:\n" + "\n".join(failures))

    def test_interp_matches_mame_vectors(self):
        self.assertEqual(len(self.recorded), 1352)
        self.assertMatches(self.recorded)

    def test_division_edges_do_not_trap(self):
        # The harness is always the sanitizer build: a trap or a UB report fails the run.
        edges = [(c, a) for c, a in self.recorded
                 if c.name.startswith(("div.edge.", "divu.edge."))
                 and (c.name.endswith(".00000000") or c.name == "div.edge.80000000.FFFFFFFF")]
        self.assertEqual(len(edges), 10)  # by zero; INT_MIN / -1 has no MAME vector
        self.assertMatches(edges)
        int_min, = [c for c in vectors.cases(1) if c.name == "div.edge.80000000.FFFFFFFF"]
        got = self.run_one(int_min)
        self.assertEqual((got.lo, got.hi), (0x80000000, 0))
        self.assertEqual(got.calls, [])

    def test_load_outside_ram_goes_to_the_bus(self):
        got = self.run_one(_case(
            "bus", A("lui", rt=T0, imm=0xA800), A("lw", rt=T1, rs=T0, imm=0),
            A("lw", rt=T2, rs=0, imm=0x10), A("lui", rt=T3, imm=0xB610),
            A("sw", rt=T1, rs=T3, imm=0)))
        bus = charness.BUS_READ_VALUE
        self.assertEqual(got.calls, [("bus_read", 0xA8000000, 4), ("bus_read", 0x00000010, 4),
                                     ("bus_write", 0xB6100000, 4, bus)])
        self.assertEqual((got.r[T1], got.r[T2]), (bus, bus))

    def test_byte_and_halfword_bus_accesses(self):
        # size 1 and 2 reach the bus with their size (the CMOS is read a byte at a time); the
        # read is masked to the size, then extended as the load says; a store passes its value
        # masked to the size
        got = self.run_one(_case(
            "bus-small", A("lui", rt=T0, imm=0xB610),
            A("lb", rt=T1, rs=T0, imm=1), A("lbu", rt=T2, rs=T0, imm=2),
            A("lh", rt=T3, rs=T0, imm=4), A("lhu", rt=V0, rs=T0, imm=6),
            A("sb", rt=T1, rs=T0, imm=9), A("sh", rt=T3, rs=T0, imm=10)))
        self.assertEqual(got.calls, [
            ("bus_read", 0xB6100001, 1), ("bus_read", 0xB6100002, 1),
            ("bus_read", 0xB6100004, 2), ("bus_read", 0xB6100006, 2),
            ("bus_write", 0xB6100009, 1, 0xEF), ("bus_write", 0xB610000A, 2, 0xBEEF)])
        self.assertEqual((got.r[T1], got.r[T2], got.r[T3], got.r[V0]),
                         (0xFFFFFFEF, 0xEF, 0xFFFFBEEF, 0xBEEF))

    def test_ram_aliases(self):
        got = self.run_one(_case(
            "alias", A("lui", rt=T0, imm=0xA020), A("sw", rt=T1, rs=T0, imm=0),
            A("lui", rt=T2, imm=0x8020), A("lw", rt=T3, rs=T2, imm=0),
            r={T1: 0x12345678}))
        self.assertEqual(got.calls, [])
        self.assertEqual(got.r[T3], 0x12345678)
        self.assertEqual(got.mem, {vectors.SCRATCH: 0x12345678})

    def test_access_straddling_the_end_of_ram(self):
        got = self.run_one(_case(
            "straddle", A("lui", rt=T0, imm=0x8080), A("lw", rt=T1, rs=T0, imm=-2),
            A("lhu", rt=T2, rs=T0, imm=-2)))
        # the word does not fit below the end of RAM; the halfword at the same address does
        self.assertEqual(got.calls, [("bus_read", 0x807FFFFE, 4)])
        self.assertEqual(got.r[T2], 0)

    def test_call_to_unrecompiled_address_is_interpreted_and_logged(self):
        callee = vectors.CODE_BASE + 0x40
        words = [A("jal", target=callee), NOP, A("jal", target=callee), NOP, JR_RA, NOP]
        words += [NOP] * (0x40 // 4 - len(words))
        words += [A("addiu", rt=V0, rs=V0, imm=1)]  # callee: v0 += 1; jr ra; nop
        got = self.run_one(_case("calls", *words, r={V0: 5}))
        self.assertEqual(got.r[V0], 7)
        self.assertEqual(got.unrecompiled, [vectors.CODE_BASE, callee])
        self.assertEqual(got.calls, [])

    def test_unsupported_instruction_stops_with_its_address(self):
        with self.assertRaises(charness.HarnessError) as err:
            self.run_one(_case("unsupported", NOP, 0x7C000000))
        self.assertIn("7C000000", str(err.exception))
        self.assertIn("80100004", str(err.exception))

    def test_break_calls_rt_break_and_continues(self):
        got = self.run_one(_case("break", 0x0007000D, A("ori", rt=T0, imm=1)))
        self.assertEqual(got.calls, [("break", vectors.CODE_BASE, 0x1C00)])
        self.assertEqual(got.r[T0], 1)

    def test_fcsr_round_trip_and_rounding(self):
        written = 0x00800001 | 1 << 26 | 0x7C  # cc0, cc2, flags, rounding mode 1 (to zero)

        def program(name, fcsr):
            return _case(
                name, A("ctc1", rt=T0, fs=31), A("cvt.s.w", fd=0, fs=2),
                A("cvt.s.w", fd=1, fs=3), A("cfc1", rt=T1, fs=31),
                r={T0: fcsr}, fpr={2: 0x01000001, 3: 0x01000003})

        to_zero, nearest = self.run_cases([program("rz", written), program("rn", 0)])
        self.assertEqual((to_zero.fcsr, to_zero.r[T1]), (written, written))
        self.assertEqual(to_zero.fpr[0], _f32(16777216.0))
        self.assertEqual(to_zero.fpr[1], _f32(16777218.0))  # toward zero
        self.assertEqual(nearest.fpr[1], _f32(16777220.0))  # nearest even, the case after

    def test_nan_results(self):
        # A generated NaN's sign and payload are the host's (recomp.h): only NaN-ness is
        # pinned. A NaN operand's payload propagates.
        def is_nan(bits):
            return bits >> 23 & 0xFF == 0xFF and bits & 0x7FFFFF != 0

        inf, payload = 0x7F800000, 0x7FC12345
        got = self.run_one(_case(
            "nan", A("div.s", fd=0, fs=10, ft=10), A("sub.s", fd=1, fs=11, ft=11),
            A("sqrt.s", fd=2, fs=12), A("add.s", fd=3, fs=13, ft=14),
            fpr={0: 1, 1: 1, 2: 1, 3: 1, 10: 0, 11: inf, 12: _f32(-1.0), 13: payload,
                 14: _f32(1.0)}))
        for reg in (0, 1, 2):
            self.assertTrue(is_nan(got.fpr[reg]), f"fpr[{reg}] = {got.fpr[reg]:08X}")
        self.assertEqual(got.fpr[3], payload)

    @staticmethod
    def flush_programs():
        tiny = 57 << 23  # 2^-70: the exact product 2^-140 is denormal
        product = A("mul.s", fd=0, fs=2, ft=4)

        def program(name, fcsr):
            return _case(name, product, fcsr=fcsr, fpr={0: 0xFFFFFFFF, 2: tiny, 4: tiny})

        return [program("fs", 1 << 24), program("no-fs", 0), program("fs-again", 1 << 24)]

    def test_fs_is_not_honoured_by_default(self):
        # RT_HONOUR_FS = 0 (fpu.c), as MAME: FS = 1 is kept in FCSR, but the denormal result
        # is not flushed (the FS vectors pin the same against MAME's results)
        fs, no_fs, again = self.run_cases(self.flush_programs())
        self.assertEqual((fs.fpr[0], no_fs.fpr[0], again.fpr[0]), (1 << 9, 1 << 9, 1 << 9))
        self.assertEqual(fs.fcsr, 1 << 24)

    def test_flush_to_zero_when_fs_is_honoured(self):
        # built with RT_HONOUR_FS = 1: FS = 1 flushes, as the R5000 documents
        exe = charness.build([], extra_flags=("-DRT_HONOUR_FS=1",))
        flushed, denormal, again = charness.run(exe, "interp", self.flush_programs())
        self.assertEqual(flushed.fpr[0], 0)
        self.assertEqual(flushed.fcsr, 1 << 24)
        self.assertEqual(denormal.fpr[0], 1 << 9)
        self.assertEqual(again.fpr[0], 0)


if __name__ == "__main__":
    unittest.main()
