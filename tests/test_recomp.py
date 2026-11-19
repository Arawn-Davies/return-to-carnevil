"""Whole functions emitted by recomp.function_c, compiled into the test harness and run
against the reference interpreter on the same image (and, for the recorded branch and jump
cases, against MAME).

Each test assembles a small image at vectors.CODE_BASE, analyses it with functions.analyse,
emits every function and builds two harness binaries from the same C: one for "recomp" mode
with every emitted function registered by address (the entry also by name), one for
"interp" mode with only the stub thunks (and any callee a test wants run natively)
registered. The image is loaded into RAM in both modes.
"""
import dataclasses
import random
import re
import struct
import unittest

from carnevil_tools import emit, exe, functions, mips, recomp, vectors
from tests import charness

CODE = vectors.CODE_BASE
A = vectors.assemble
JR_RA = A("jr", rs=31)
RANDOM_STATES = 100
_M32 = 0xFFFFFFFF

AT, V0, V1, A0, A1, A2 = 1, 2, 3, 4, 5, 6
T0, T1, T2, T3, T4, T5 = 8, 9, 10, 11, 12, 13
S0, T9, SP, RA = 16, 25, 29, 31


def _s32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def _f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _cc(fcsr, n):
    return fcsr >> vectors._FCC_SHIFT[n] & 1


class Asm:
    """Words from CODE upwards; items are words or callables (labels, addr) -> word."""

    def __init__(self):
        self.items = []
        self.labels = {}

    def here(self):
        return CODE + 4 * len(self.items)

    def label(self, name):
        self.labels[name] = self.here()

    def __call__(self, *words):
        self.items.extend(words)

    def b(self, mnemonic, label, **fields):
        """A branch to a label."""
        self.items.append(lambda labels, addr: A(
            mnemonic, imm=(labels[label] - addr - 4) >> 2, **fields))

    def j(self, mnemonic, label):
        """j or jal to a label."""
        self.items.append(lambda labels, addr: A(mnemonic, target=labels[label]))

    def ptr(self, label):
        """A data word holding the address of a label."""
        self.items.append(lambda labels, addr: labels[label])

    def pad_to(self, addr):
        while self.here() < addr:
            self.items.append(0)

    def words(self):
        return [item(self.labels, CODE + 4 * i) if callable(item) else item
                for i, item in enumerate(self.items)]


def image_of(words):
    return exe.ExeImage(0, CODE, struct.pack("<%dI" % len(words), *words), "")


def _registration(fns, entry_name):
    lines = "".join(f"    harness_register(0x{f.addr:08X}u, {recomp.c_name(f.addr)});\n"
                    for f in fns)
    return ("static void __attribute__((constructor)) register_addresses(void)\n{\n"
            f"{lines}}}\n\n" + charness.register_named([entry_name]))


class Program:
    """An analysed, emitted and built image."""

    def __init__(self, asm, manual=(), stub_blocks=(), native=()):
        self.labels = dict(asm.labels)
        self.words = asm.words()
        self.image = image_of(self.words)
        self.analysis = functions.analyse(self.image, CODE, manual, stub_blocks)
        self.fns = sorted(self.analysis.functions, key=lambda f: f.addr)
        self.c = {f.addr: recomp.function_c(self.image, self.analysis, f) for f in self.fns}
        source = "".join(self.c[f.addr] + "\n" for f in self.fns)
        entry = recomp.c_name(CODE)
        in_interp = [f for f in self.fns if f.sources == {"stub"} or f.addr in native]
        self.recomp_exe = charness.build(
            [charness.write_generated(source + _registration(self.fns, entry))])
        self.interp_exe = charness.build(
            [charness.write_generated(source + _registration(in_interp, entry))])

    def at(self, label):
        return self.labels[label]

    def cases(self, name, setup, n=RANDOM_STATES, irq=0):
        """n seeded random before-states, each changed by setup(state, rng, i)."""
        rng = random.Random(f"recomp.{name}")
        out = []
        for i in range(n):
            s = vectors._state(rng)
            setup(s, rng, i)
            case = vectors.Case(f"{name}.{i}", self.words, s)
            if irq:
                case.irq_pending = irq
            out.append(case)
        return out

    def run(self, mode, cases):
        if mode == "recomp":
            return charness.run(self.recomp_exe, "recomp", cases,
                                {c.name: recomp.c_name(CODE) for c in cases})
        return charness.run(self.interp_exe, "interp", cases)


class RecompTestCase(unittest.TestCase):
    def assertSameAsInterpreter(self, prog, cases, unrecompiled=()):
        """Run cases in both modes; fail on any state or runtime-call difference, or if
        the recompiled run entered the interpreter anywhere but `unrecompiled`. Returns the
        recompiled after-states."""
        cases = list(cases)
        want, got = prog.run("interp", cases), prog.run("recomp", cases)
        failures = []
        for case, u, v in zip(cases, want, got):
            diff = charness.first_difference(u, v)
            if diff is None and u.calls != v.calls:
                diff = f"runtime calls: interp {u.calls} recomp {v.calls}"
            if diff is None and v.unrecompiled != list(unrecompiled):
                diff = f"recomp entered the interpreter at {[hex(a) for a in v.unrecompiled]}"
            if diff:
                failures.append(f"{case.name}: {diff}")
        self.assertEqual(failures, [], f"{len(failures)} cases differ:\n" + "\n".join(failures))
        return got


class StaticTest(unittest.TestCase):
    """What needs no C compiler."""

    def test_name_and_signature(self):
        p = Asm()
        p(JR_RA, 0)
        image = image_of(p.words())
        analysis = functions.analyse(image, CODE)
        fn, = analysis.functions
        text = recomp.function_c(image, analysis, fn)
        self.assertIn("void fn_80100000(uint8_t *ram, cpu_t *c)\n{", text)
        self.assertIn("/* _start */", text)

    def test_control_transfer_in_delay_slot_raises(self):
        p = Asm()
        p.b("beq", "end", rs=A0, rt=0)
        p.j("j", "end")  # in the delay slot
        p(0)
        p.label("end")
        p(JR_RA, 0)
        image = image_of(p.words())
        analysis = functions.analyse(image, CODE)
        fn = next(f for f in analysis.functions if f.addr == CODE)
        with self.assertRaises(recomp.RecompileError) as err:
            recomp.function_c(image, analysis, fn)
        self.assertEqual(err.exception.addr, CODE + 4)

    def test_output_is_deterministic(self):
        p = jump_table_asm()
        words = p.words()
        texts = []
        for _ in range(2):
            image = image_of(words)
            analysis = functions.analyse(image, CODE, manual=[p.labels["handler"]])
            texts.append([recomp.function_c(image, analysis, f)
                          for f in sorted(analysis.functions, key=lambda f: f.addr)])
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(len(texts[0]), 2)


def jump_table_asm():
    """sltiu bound 5; table of 5 cases, entries 2 and 3 sharing a target, then a sixth word
    (read for index 5, out of range) holding the address of a separate handler function."""
    p = Asm()
    p(A("sltiu", rt=AT, rs=A0, imm=5))
    p(A("sll", rd=T0, rt=A0, sa=2))
    p.items.append(lambda labels, addr: A("lui", rt=T1, imm=labels["table"] >> 16))
    p(A("addu", rd=T1, rs=T1, rt=T0))
    p.items.append(lambda labels, addr: A("lw", rt=T2, rs=T1, imm=labels["table"] & 0xFFFF))
    p(A("jr", rs=T2))
    p(A("ori", rt=T2, rs=0, imm=0x77))  # slot overwrites the jump register
    for k, value in ((0, 10), (1, 11), (2, 12)):
        p.label(f"case{k}")
        p(A("ori", rt=V0, rs=0, imm=value))
        p.j("j", "end")
        p(0)
    p.label("case4")
    p(A("ori", rt=V0, rs=0, imm=14))
    p.label("end")
    p(JR_RA, 0)
    p.label("handler")
    p(A("addiu", rt=SP, rs=SP, imm=-8), A("ori", rt=V0, rs=0, imm=99), JR_RA,
      A("addiu", rt=SP, rs=SP, imm=8))
    p.label("table")
    for name in ("case0", "case1", "case2", "case2", "case4", "handler"):
        p.ptr(name)
    return p


class FunctionTest(RecompTestCase):
    def test_loop_with_backward_branch(self):
        p = Asm()
        p(A("addu", rd=T1, rs=0, rt=0))
        p.label("loop")
        p(A("addu", rd=T1, rs=T1, rt=A0))
        p(A("addiu", rt=A0, rs=A0, imm=-1))
        p.b("bgtz", "loop", rs=A0)
        p(A("addiu", rt=T2, rs=T2, imm=1))
        p(JR_RA, 0)
        prog = Program(p)
        self.assertIn("goto L_80100004;", prog.c[CODE])

        def setup(s, rng, i):
            s.r[A0] = rng.randrange(-2, 12) & _M32
        cases = prog.cases("loop", setup)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            n = _s32(case.before.r[A0])
            self.assertEqual(state.r[T2], (case.before.r[T2] + max(n, 1)) & _M32)
            self.assertEqual(state.r[T1], sum(range(max(n, 1), 0, -1)) if n > 0 else n & _M32)

    def test_if_else(self):
        p = Asm()
        p(A("slt", rd=T0, rs=A0, rt=A1))
        p.b("beq", "else", rs=T0, rt=0)
        p(0)
        p(A("ori", rt=V0, rs=0, imm=1))
        p.b("beq", "end", rs=0, rt=0)  # b end
        p(A("ori", rt=V1, rs=0, imm=1))
        p.label("else")
        p(A("ori", rt=V0, rs=0, imm=2))
        p.label("end")
        p(JR_RA, 0)
        prog = Program(p)
        cases = prog.cases("if", lambda s, rng, i: None)
        got = self.assertSameAsInterpreter(prog, cases)
        for state, case in zip(got, cases):
            less = _s32(case.before.r[A0]) < _s32(case.before.r[A1])
            self.assertEqual(state.r[V0], 1 if less else 2)

    LIKELY = (("beql", {"rs": A0, "rt": A1}), ("bnel", {"rs": A0, "rt": A1}),
              ("blezl", {"rs": A2}), ("bgtzl", {"rs": A2}), ("bltzl", {"rs": A2}),
              ("bgezl", {"rs": A2}), ("bc1fl", {"cc": 3}), ("bc1tl", {"cc": 3}))

    @staticmethod
    def likely_taken(name, s):
        a, b, x = s.r[A0], s.r[A1], _s32(s.r[A2])
        cc3 = _cc(s.fcsr, 3)
        return {"beql": a == b, "bnel": a != b, "blezl": x <= 0, "bgtzl": x > 0,
                "bltzl": x < 0, "bgezl": x >= 0, "bc1fl": cc3 == 0, "bc1tl": cc3 == 1}[name]

    def test_branch_likely_skips_slot_when_not_taken(self):
        census_likely = {n for n in vectors.CENSUS
                         if mips.decode(A(n), CODE).kind == "branch_likely"}
        self.assertEqual({n for n, _ in self.LIKELY}, census_likely)
        p = Asm()
        for k, (name, fields) in enumerate(self.LIKELY):
            p.b(name, f"L{k}", **fields)
            p(A("addiu", rt=T0 + k, rs=T0 + k, imm=1))  # slot: t0..t7
            p(A("ori", rt=S0 + k, rs=0, imm=1))         # not taken: s0..s7
            p.label(f"L{k}")
        p(JR_RA, 0)
        prog = Program(p)

        def setup(s, rng, i):
            if i % 2 == 0:
                s.r[A1] = s.r[A0]
            s.r[A2] = vectors._SIGN_CYCLE[i % 5] if i < 10 else s.r[A2]
        cases = prog.cases("likely", setup)
        got = self.assertSameAsInterpreter(prog, cases)
        seen = set()
        for case, state in zip(cases, got):
            for k, (name, _) in enumerate(self.LIKELY):
                taken = self.likely_taken(name, case.before)
                seen.add((name, taken))
                self.assertEqual(state.r[T0 + k], (case.before.r[T0 + k] + taken) & _M32,
                                 f"{case.name} {name}: slot ran {not taken}")
                self.assertEqual(state.r[S0 + k], case.before.r[S0 + k] if taken else 1)
        self.assertEqual(len(seen), 2 * len(self.LIKELY))  # each taken and not taken

    def test_delay_slot_changes_branch_operand(self):
        p = Asm()
        p.b("beq", "end", rs=A0, rt=0)
        p(A("addiu", rt=A0, rs=A0, imm=-1))
        p(A("ori", rt=V0, rs=0, imm=1))
        p.label("end")
        p(JR_RA, 0)
        prog = Program(p)

        def setup(s, rng, i):
            s.r[A0] = (0, 1)[i % 2] if i < 20 else s.r[A0]
        cases = prog.cases("operand", setup)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            taken = case.before.r[A0] == 0
            self.assertEqual(state.r[V0], case.before.r[V0] if taken else 1)
            self.assertEqual(state.r[A0], (case.before.r[A0] - 1) & _M32)

    def test_jal_slot_reads_and_writes_ra(self):
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.label("jal1")
        p.j("jal", "callee")
        p(A("addu", rd=T0, rs=RA, rt=0))  # slot reads the link
        p.label("jal2")
        p.j("jal", "callee")
        p(A("ori", rt=RA, rs=0, imm=0x1234))  # slot overwrites the link
        p(A("addu", rd=RA, rs=S0, rt=0))
        p(JR_RA, 0)
        p.label("callee")
        p(A("addu", rd=T1, rs=RA, rt=0))
        p(A("addiu", rt=V0, rs=V0, imm=1))
        p(JR_RA, 0)
        prog = Program(p)
        got = self.assertSameAsInterpreter(prog, prog.cases("jal", lambda s, rng, i: None))
        for state in got:
            self.assertEqual(state.r[T0], prog.at("jal1") + 8)
            self.assertEqual(state.r[T1], 0x1234)
            self.assertEqual(state.r[RA], vectors.RA)

    def test_jalr_slot_overwrites_target_register(self):
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p(A("jalr", rd=RA, rs=T9))
        p(A("addiu", rt=T9, rs=T9, imm=12))  # slot moves t9 from callee to callee2
        p.label("jalr2")
        p(A("jalr", rd=T9, rs=T9))  # link into the target register itself
        p(A("addu", rd=T5, rs=T9, rt=0))  # slot reads that link
        p(A("addu", rd=RA, rs=S0, rt=0))
        p(JR_RA, 0)
        p.label("callee")
        p(A("addiu", rt=V0, rs=V0, imm=1), JR_RA, 0)
        p.label("callee2")
        p(A("addiu", rt=V1, rs=V1, imm=1), JR_RA, 0)
        manual = [CODE + 4 * 8, CODE + 4 * 11]
        prog = Program(p, manual=manual)
        self.assertEqual(manual, [prog.at("callee"), prog.at("callee2")])

        def setup(s, rng, i):
            s.r[T9] = prog.at("callee")
        cases = prog.cases("jalr", setup)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            self.assertEqual(state.r[V0], (case.before.r[V0] + 1) & _M32)
            self.assertEqual(state.r[V1], (case.before.r[V1] + 1) & _M32)
            self.assertEqual(state.r[T5], prog.at("jalr2") + 8)
            self.assertEqual(state.r[T9], prog.at("jalr2") + 8)

    def test_branch_into_delay_slot(self):
        p = Asm()
        p.b("bne", "a", rs=A0, rt=0)
        p.label("slot")
        p(A("addiu", rt=V0, rs=V0, imm=1))  # delay slot, and a branch target
        p(A("ori", rt=T0, rs=0, imm=5))
        p.label("a")
        p(A("addiu", rt=A1, rs=A1, imm=-1))
        p.b("bgtz", "slot", rs=A1)
        p(A("addiu", rt=V1, rs=V1, imm=1))
        p(JR_RA, 0)
        prog = Program(p)
        text = prog.c[CODE]
        self.assertEqual(text.count(f"c->r[{V0}] = c->r[{V0}] + 0x00000001u;"), 2)
        self.assertIn("L_80100004:", text)

        def setup(s, rng, i):
            s.r[A0] = (0, 1)[i % 2] if i < 50 else s.r[A0]
            s.r[A1] = rng.randrange(-1, 6) & _M32
        cases = prog.cases("into-slot", setup)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            back = max(_s32(case.before.r[A1]) - 1, 0)
            self.assertEqual(state.r[V0], (case.before.r[V0] + 1 + back) & _M32)

    def test_jump_table_becomes_switch(self):
        p = jump_table_asm()
        prog = Program(p, manual=[p.labels["handler"]])
        table, = prog.analysis.jump_tables
        self.assertEqual(len(table.targets), 5)
        text = prog.c[CODE]
        self.assertIn("switch (", text)
        self.assertEqual(len(re.findall(r"^\s*case 0x", text, re.M)), 4)
        self.assertIn("default:", text)
        values = {0: 10, 1: 11, 2: 12, 3: 12, 4: 14, 5: 99}

        def setup(s, rng, i):
            s.r[A0] = i % 6
        cases = prog.cases("table", setup)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            self.assertEqual(state.r[V0], values[case.before.r[A0]], case.name)
            self.assertEqual(state.r[T2], 0x77)

    def test_unresolved_jr_calls_rt_jump(self):
        p = Asm()
        p(A("jr", rs=T3))
        p(A("ori", rt=T3, rs=0, imm=0))  # slot overwrites the jump register
        p.label("away")
        p(A("ori", rt=V0, rs=0, imm=3), JR_RA, 0)
        prog = Program(p)
        self.assertEqual(prog.analysis.jump_tables, [])
        self.assertIn("rt_jump(ram, c, ", prog.c[CODE])

        def setup(s, rng, i):
            s.r[T3] = prog.at("away")
        got = self.assertSameAsInterpreter(prog, prog.cases("jr", setup),
                                           unrecompiled=[prog.at("away")])
        for state in got:
            self.assertEqual(state.r[V0], 3)

    def test_tail_jump_to_function(self):
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.j("jal", "f")
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p.j("j", "f")
        p(A("ori", rt=T1, rs=0, imm=2))
        p.label("f")
        p(A("addiu", rt=V0, rs=V0, imm=1), JR_RA, 0)
        prog = Program(p)
        f = prog.at("f")
        self.assertIn(f, {fn.addr for fn in prog.fns})
        self.assertRegex(prog.c[CODE], rf"fn_{f:08X}\(ram, c\);\n\s*return;")
        cases = prog.cases("tail", lambda s, rng, i: None)
        got = self.assertSameAsInterpreter(prog, cases)
        for state, case in zip(got, cases):
            self.assertEqual(state.r[V0], (case.before.r[V0] + 2) & _M32)
            self.assertEqual(state.r[T1], 2)

    def test_jump_outside_function_to_non_start_raises(self):
        for mnemonic, fields in (("j", {}), ("bne", {"rs": A0, "rt": 0})):
            p = Asm()
            if mnemonic == "j":
                p.j("j", "x")
            else:
                p.b("bne", "x", **fields)
            p(0)
            if mnemonic != "j":
                p(JR_RA, 0)
            p.label("g")
            p(JR_RA, 0)
            p.label("x")
            p(A("ori", rt=V0, rs=0, imm=1), JR_RA, 0)
            image = image_of(p.words())
            analysis = functions.analyse(image, CODE, manual=[p.labels["g"]])
            starts = {f.addr for f in analysis.functions}
            self.assertNotIn(p.labels["x"], starts)
            main = next(f for f in analysis.functions if f.addr == CODE)
            with self.assertRaises(recomp.RecompileError) as err:
                recomp.function_c(image, analysis, main)
            self.assertEqual(err.exception.addr, CODE, mnemonic)
            self.assertIn(f"{p.labels['x']:08X}", str(err.exception))

    def test_fall_through_into_next_function(self):
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.j("jal", "next")
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p(A("addiu", rt=V0, rs=V0, imm=1))  # runs on into `next`
        p.label("next")
        p(A("addiu", rt=V1, rs=V1, imm=1), JR_RA, 0)
        prog = Program(p)
        nxt = prog.at("next")
        self.assertRegex(prog.c[CODE], rf"fn_{nxt:08X}\(ram, c\);\n\s*return;\n}}")
        cases = prog.cases("fall", lambda s, rng, i: None)
        got = self.assertSameAsInterpreter(prog, cases)
        for state, case in zip(got, cases):
            self.assertEqual(state.r[V1], (case.before.r[V1] + 2) & _M32)
        # with no function starting there: an error
        main = dataclasses.replace(prog.fns[0], size=nxt - CODE)
        analysis = dataclasses.replace(prog.analysis, functions=[main])
        with self.assertRaises(recomp.RecompileError) as err:
            recomp.function_c(prog.image, analysis, main)
        self.assertEqual(err.exception.addr, nxt - 4)

    def test_stub_call_becomes_loader_call(self):
        block = CODE + 0x400
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p(A("jal", target=block + 0x1A8))
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p(A("j", target=block + 0x10))
        p(A("ori", rt=V0, rs=0, imm=1))
        p.pad_to(block + 0x200)
        prog = Program(p, stub_blocks=[(block, block + 0x200)])
        stubs = {f.addr for f in prog.fns if f.sources == {"stub"}}
        self.assertEqual(stubs, {block + 0x10, block + 0x1A8})
        self.assertIn("rt_loader(ram, c, 0x000001A8u);", prog.c[block + 0x1A8])
        self.assertIn("rt_loader(ram, c, 0x00000010u);", prog.c[block + 0x10])
        self.assertIn("rt_loader(ram, c, 0x000001A8u);", prog.c[CODE])
        self.assertNotIn(f"fn_{block + 0x1A8:08X}(ram", prog.c[CODE])
        got = self.assertSameAsInterpreter(prog, prog.cases("stub", lambda s, rng, i: None))
        for state in got:
            self.assertEqual(state.calls, [("loader", 0x1A8), ("loader", 0x10)])
            self.assertEqual(state.r[V0], 1)

    def test_break_passes_code_and_address(self):
        self.assertEqual(A("break", code=0x1C00), 0x0007000D)
        p = Asm()
        p(A("ori", rt=T0, rs=0, imm=1))
        p(0x0007000D)
        p(A("ori", rt=T1, rs=0, imm=1))
        p.b("bne", "end", rs=A0, rt=0)
        p(A("break", code=5))  # break is not a control transfer: allowed in a slot
        p(A("ori", rt=T2, rs=0, imm=1))
        p.label("end")
        p(JR_RA, 0)
        prog = Program(p)
        self.assertIn("rt_break(ram, c, 0x80100004u, 0x00001C00u);", prog.c[CODE])
        got = self.assertSameAsInterpreter(prog, prog.cases("break", lambda s, rng, i: None))
        for state in got:
            self.assertEqual(state.calls, [("break", CODE + 4, 0x1C00), ("break", CODE + 16, 5)])
            self.assertEqual((state.r[T0], state.r[T1]), (1, 1))

    def test_rt_check_at_entry_and_back_edge(self):
        p = Asm()
        p.b("bne", "loop", rs=A1, rt=0)  # forward: no check
        p(0)
        p(A("addiu", rt=V0, rs=V0, imm=1))
        p.label("loop")
        p(A("addiu", rt=A0, rs=A0, imm=-1))
        p.b("bgtz", "loop", rs=A0)
        p(0)
        p(JR_RA, 0)
        prog = Program(p)

        def setup(s, rng, i):
            s.r[A0] = rng.randrange(-1, 8) & _M32
        cases = prog.cases("irq", setup, irq=1)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            n = max(_s32(case.before.r[A0]), 1)
            self.assertEqual(state.calls, [("interrupt",)] * n, case.name)
        quiet = prog.cases("quiet", setup)
        for state in self.assertSameAsInterpreter(prog, quiet):
            self.assertEqual(state.calls, [])

    def test_bc1_with_condition_code_2(self):
        p = Asm()
        p(A("c.lt.s", fs=2, ft=4, cc=2))
        p.b("bc1t", "t", cc=2)
        p(0)
        p(A("ori", rt=V0, rs=0, imm=1))
        p.label("t")
        p.b("bc1f", "f", cc=2)
        p(0)
        p(A("ori", rt=V1, rs=0, imm=1))
        p.label("f")
        p(JR_RA, 0)
        prog = Program(p)
        self.assertIn("rc_cc(c, 2)", prog.c[CODE])
        cases = prog.cases("cc2", lambda s, rng, i: None)
        got = self.assertSameAsInterpreter(prog, cases)
        outcomes = set()
        for case, state in zip(cases, got):
            less = _f32(case.before.fpr[2]) < _f32(case.before.fpr[4])
            outcomes.add(less)
            self.assertEqual(state.r[V0], case.before.r[V0] if less else 1)
            self.assertEqual(state.r[V1], 1 if less else case.before.r[V1])
        self.assertEqual(outcomes, {False, True})

    def test_fp_condition_survives_a_call(self):
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p(A("c.lt.s", fs=2, ft=4, cc=0))
        p.j("jal", "f")
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p.b("bc1t", "end", cc=0)
        p(0)
        p(A("ori", rt=V0, rs=0, imm=1))
        p.label("end")
        p(JR_RA, 0)
        p.label("f")
        p(A("add.s", fd=6, fs=2, ft=4), A("ori", rt=T0, rs=0, imm=1), JR_RA, 0)
        prog = Program(p)
        cases = prog.cases("fcc", lambda s, rng, i: None)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            less = _f32(case.before.fpr[2]) < _f32(case.before.fpr[4])
            self.assertEqual(state.r[V0], case.before.r[V0] if less else 1)
            self.assertEqual(state.r[T0], 1)


class VectorTest(unittest.TestCase):
    def test_branch_cases_match_mame_vectors(self):
        """Each recorded branch, jump and call case analysed with functions.analyse, emitted
        with its functions renamed per case (every case program sits at CODE_BASE), and run
        by name. No function is registered by address: the only rt_call / rt_jump targets of
        these programs (jalr's callee, jr's target) are words the analysis did not reach."""
        recorded = [(c, after) for c, after in vectors.load(charness.REPO + "/tests/vectors/insn.json")
                    if mips.decode(c.words[0], CODE).mnemonic in emit.CONTROL]
        breaks = [c for c in vectors.cases(1) if mips.decode(c.words[0], CODE).mnemonic == "break"]
        self.assertEqual(len(recorded), 240)
        self.assertTrue(breaks)
        sources, names = [], {}
        for k, case in enumerate([c for c, _ in recorded] + breaks):
            image = image_of(case.words)
            analysis = functions.analyse(image, CODE)
            prefix = f"v{k}_"
            for f in sorted(analysis.functions, key=lambda f: f.addr):
                text = recomp.function_c(image, analysis, f)
                sources.append(re.sub(r"\bfn_(?=[0-9A-F]{8}\b)", prefix, text))
            names[case.name] = f"{prefix}{CODE:08X}"
        source = "\n".join(sources) + "\n" + charness.register_named(sorted(set(names.values())))
        exe_path = charness.build([charness.write_generated(source)])
        cases = [c for c, _ in recorded]
        got = charness.run(exe_path, "recomp", cases, names)
        failures = []
        for case, (_, want), state in zip(cases, recorded, got):
            diff = charness.first_difference(want, state)
            if diff:
                failures.append(f"{case.name}: {diff}")
        self.assertEqual(failures, [], f"{len(failures)} cases differ:\n" + "\n".join(failures))
        # break has no MAME record: the two modes agree
        want = charness.run(exe_path, "interp", breaks)
        got = charness.run(exe_path, "recomp", breaks, names)
        for case, u, v in zip(breaks, want, got):
            self.assertIsNone(charness.first_difference(u, v), case.name)
            self.assertEqual(u.calls, v.calls, case.name)
            self.assertEqual(v.calls[0][0], "break")


if __name__ == "__main__":
    unittest.main()
