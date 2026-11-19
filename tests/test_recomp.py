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
import io
import os
import random
import re
import shutil
import struct
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout

from carnevil_tools import cli, emit, exe, functions, mips, recomp, vectors
from tests import charness

CODE = vectors.CODE_BASE
A = vectors.assemble
JR_RA = A("jr", rs=31)
RANDOM_STATES = 100
_M32 = 0xFFFFFFFF

AT, V0, V1, A0, A1, A2, A3 = 1, 2, 3, 4, 5, 6, 7
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

    def __init__(self, asm, manual=(), stub_blocks=(), native=(), jump_tables=(),
                 interpreted=()):
        self.labels = dict(asm.labels)
        self.words = asm.words()
        self.image = image_of(self.words)
        self.analysis = functions.analyse(self.image, CODE, manual, stub_blocks, jump_tables,
                                          interpreted)
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

    def test_kuseg_ram_mapping(self):
        """The loader's TLB maps kuseg 0x00040000-0x007FFFFF onto RAM at the same physical
        address (MAME: GAME.EXE stores through such addresses, task 6); the first 256 KB of
        kuseg and kuseg from 8 MB up are not RAM and go to the bus, as does an access that
        straddles either edge. Every edge in both modes."""
        p = Asm()
        p(A("lui", rt=T0, imm=0x0020))           # 0x00200000: kuseg view of the scratch page
        p(A("sw", rt=A0, rs=T0, imm=0x10))
        p(A("lui", rt=T1, imm=0x8020))
        p(A("lw", rt=V0, rs=T1, imm=0x10))       # the same word through kseg0
        p(A("sh", rt=A1, rs=T1, imm=0x20))
        p(A("lhu", rt=V1, rs=T0, imm=0x20))      # and back through kuseg
        p(A("lui", rt=T2, imm=0x0004))           # 0x00040000, the first mapped byte
        p(A("sw", rt=A0, rs=T2, imm=-4))         # 0x0003FFFC: the last unmapped word, bus
        p(A("sb", rt=A0, rs=T2, imm=-1))         # 0x0003FFFF: the last unmapped byte, bus
        p(A("lw", rt=A2, rs=T2, imm=-2))         # 0x0003FFFE size 4: straddles, bus
        p(A("sw", rt=A1, rs=T2, imm=0))          # 0x00040000: the first mapped word, RAM
        p(A("lui", rt=T3, imm=0x8004))
        p(A("lw", rt=A3, rs=T3, imm=0))          # read back through kseg0
        p(A("lui", rt=T4, imm=0x0080))           # 0x00800000, the first byte past RAM
        p(A("sw", rt=A1, rs=T4, imm=-4))         # 0x007FFFFC: the last mapped word, RAM
        p(A("sb", rt=A0, rs=T4, imm=-1))         # 0x007FFFFF: the last mapped byte, RAM
        p(A("lui", rt=T5, imm=0x8080))
        p(A("lw", rt=S0, rs=T5, imm=-4))         # 0x807FFFFC: both stores, through kseg0
        p(A("lw", rt=T9, rs=T4, imm=-2))         # 0x007FFFFE size 4: straddles, bus
        p(A("lw", rt=T1, rs=T4, imm=0))          # 0x00800000: past RAM, bus
        p(JR_RA, 0)
        prog = Program(p)

        def setup(s, rng, i):
            s.r[A0] = rng.getrandbits(32)
            s.r[A1] = rng.getrandbits(32)
        cases = prog.cases("kuseg", setup, n=8)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            a0, a1 = case.before.r[A0], case.before.r[A1]
            self.assertEqual(state.r[V0], a0)
            self.assertEqual(state.r[V1], a1 & 0xFFFF)
            self.assertEqual(state.mem.get(vectors.SCRATCH + 0x10), a0)
            self.assertEqual(state.r[A3], a1)
            self.assertEqual(state.r[S0], (a1 & 0x00FFFFFF) | (a0 & 0xFF) << 24)
            self.assertEqual(state.calls, [("bus_write", 0x0003FFFC, 4, a0),
                                           ("bus_write", 0x0003FFFF, 1, a0 & 0xFF),
                                           ("bus_read", 0x0003FFFE, 4),
                                           ("bus_read", 0x007FFFFE, 4),
                                           ("bus_read", 0x00800000, 4)])
            for reg in (A2, T9, T1):
                self.assertEqual(state.r[reg], charness.BUS_READ_VALUE)

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

    def test_rt_check_not_at_transfers_that_call(self):
        """Tail j (backward and forward) and an unresolved backward jr to a function that
        both modes have registered: one check, the callee's entry, none at the transfer."""
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.j("jal", "b1")
        p(0)
        p.j("jal", "b2")
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p.j("j", "f")  # forward tail jump
        p(0)
        p.label("f")
        p(A("addiu", rt=V0, rs=V0, imm=1), JR_RA, 0)
        p.label("b1")
        p.j("j", "f")  # backward tail jump
        p(0)
        p.label("b2")
        p(A("jr", rs=T3))  # backward, unresolved
        p(0)
        f = CODE + 4 * 8
        prog = Program(p, manual=[f], native=[f])
        self.assertEqual(prog.at("f"), f)
        self.assertEqual(prog.analysis.jump_tables, [])
        for label in ("b1", "b2"):
            self.assertEqual(prog.c[prog.at(label)].count("RT_CHECK"), 1, label)
        self.assertEqual(prog.c[CODE].count("RT_CHECK"), 1)

        def setup(s, rng, i):
            s.r[T3] = f
        cases = prog.cases("calls", setup, irq=1)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            # entries: main, b1, f, b2, f, f
            self.assertEqual(state.calls, [("interrupt",)] * 6, case.name)
            self.assertEqual(state.r[V0], (case.before.r[V0] + 3) & _M32)

    def test_rt_check_at_backward_jump_table_case(self):
        p = Asm()
        p.label("top")
        p(A("addiu", rt=A0, rs=A0, imm=-1))
        p(A("slt", rd=T3, rs=0, rt=A0))  # index: 1 while a0 > 0
        p(A("sltiu", rt=AT, rs=T3, imm=2))
        p(A("sll", rd=T0, rt=T3, sa=2))
        p.items.append(lambda labels, addr: A("lui", rt=T1, imm=labels["table"] >> 16))
        p(A("addu", rd=T1, rs=T1, rt=T0))
        p.items.append(lambda labels, addr: A("lw", rt=T2, rs=T1, imm=labels["table"] & 0xFFFF))
        p(A("jr", rs=T2))
        p(A("addiu", rt=V0, rs=V0, imm=1))
        p.label("exit")
        p(JR_RA, 0)
        p.label("table")
        p.ptr("exit")
        p.ptr("top")
        prog = Program(p)
        self.assertEqual(len(prog.analysis.jump_tables), 1)

        def setup(s, rng, i):
            s.r[A0] = rng.randrange(-1, 8) & _M32
        cases = prog.cases("table-loop", setup, irq=1)
        got = self.assertSameAsInterpreter(prog, cases)
        for case, state in zip(cases, got):
            n = max(_s32(case.before.r[A0]), 1)
            self.assertEqual(state.calls, [("interrupt",)] * n, case.name)

    def test_rt_check_documented_exceptions(self):
        """A conditional branch to another function's start, and running off the end into
        one: the C checks only at the callee's entry. The interpreter runs on inline there
        (no check), so only the states are compared with it."""
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.j("jal", "g")
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p.b("bne", "g", rs=A0, rt=0)
        p(0)
        p(A("addiu", rt=V0, rs=V0, imm=1))  # runs on into g
        p.label("g")
        p(A("addiu", rt=V1, rs=V1, imm=1), JR_RA, 0)
        prog = Program(p)
        self.assertEqual(prog.c[CODE].count("RT_CHECK"), 1)

        def setup(s, rng, i):
            s.r[A0] = i % 2 if i < 50 else s.r[A0]
        cases = prog.cases("exceptions", setup, irq=1)
        want, got = prog.run("interp", cases), prog.run("recomp", cases)
        for case, u, v in zip(cases, want, got):
            self.assertIsNone(charness.first_difference(u, v), case.name)
            # entries: main, g (the call), g (the branch or the run-on)
            self.assertEqual(v.calls, [("interrupt",)] * 3, case.name)

    def test_branch_into_stub_block_raises(self):
        block = CODE + 0x100
        p = Asm()
        p(A("beq", rs=A0, rt=0, imm=(block + 0x10 - CODE - 4) >> 2))
        p(0)
        p(JR_RA, 0)
        p.pad_to(block + 0x40)
        image = image_of(p.words())
        analysis = functions.analyse(image, CODE, stub_blocks=[(block, block + 0x40)])
        main = next(f for f in analysis.functions if f.addr == CODE)
        with self.assertRaises(recomp.RecompileError) as err:
            recomp.function_c(image, analysis, main)
        self.assertEqual(err.exception.addr, CODE)
        self.assertIn("stub block", str(err.exception))

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


MFC0 = 0x40086000  # mfc0 t0,$12: outside the reached census


def dispatch_asm():
    """`lw v0,0(a0); jr v0` over two cases the analysis cannot find (a manual jump table),
    an interpreted function `g` and a function `h` holding mfc0, which only `g` calls."""
    p = Asm()
    p(A("addu", rd=S0, rs=RA, rt=0))
    p.j("jal", "g")
    p(0)
    p(A("addu", rd=RA, rs=S0, rt=0))
    p(A("lw", rt=V0, rs=A0, imm=0))
    p.label("jr")
    p(A("jr", rs=V0), 0)
    p.label("case0")
    p(A("ori", rt=V0, rs=0, imm=10))
    p.j("j", "end")
    p(0)
    p.label("case1")
    p(A("ori", rt=V0, rs=0, imm=11))
    p.label("end")
    p(JR_RA, 0)
    p.label("g")
    p(A("addiu", rt=SP, rs=SP, imm=-8), A("ori", rt=V1, rs=0, imm=7), JR_RA,
      A("addiu", rt=SP, rs=SP, imm=8))
    p.label("h")
    p(A("addiu", rt=SP, rs=SP, imm=-8), MFC0, JR_RA, A("addiu", rt=SP, rs=SP, imm=8))
    p.label("table")
    p.ptr("case0")
    p.ptr("case1")
    return p


def dispatch_analysis(p, interpreted=True):
    """The image of `dispatch_asm` and its analysis with the manual jump table, `h` as a
    manual start and `g` (and `h`) interpreted."""
    L = p.labels
    image = image_of(p.words())
    table = functions.JumpTable(L["jr"], 0, [L["case0"], L["case1"]])
    return image, functions.analyse(image, CODE, [L["h"]], (), [table],
                                    [L["g"], L["h"]] if interpreted else [])


def write_exe(path, words, load=CODE):
    """An EXE file of `words` at `load` (header: checksum, load address); its SHA-256."""
    body = struct.pack("<I", load) + struct.pack("<%dI" % len(words), *words)
    raw = struct.pack("<I", exe.posix_cksum(body)) + body
    with open(path, "wb") as f:
        f.write(raw)
    return exe.load_exe(path, expect_sha256=None)


class RecompileTest(unittest.TestCase):
    """recomp.recompile: the files, the manifest and the output folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_recompile_writes_manifest_last_and_no_partial_dir(self):
        image, analysis = dispatch_analysis(dispatch_asm())
        out = os.path.join(self.tmp, "gen")
        written = []
        real = recomp._write

        def spy(path, text):
            written.append(os.path.relpath(path, self.tmp))
            real(path, text)
        recomp._write = spy
        self.addCleanup(setattr, recomp, "_write", real)
        manifest = recomp.recompile(image, analysis, out)
        # everything goes to a working folder of this run, next to out; the manifest last
        work = {os.path.dirname(w) for w in written}
        self.assertEqual(len(work), 1)
        self.assertEqual(os.path.dirname(work.pop()), "")
        self.assertEqual(os.path.basename(written[-1]), "manifest.toml")
        self.assertEqual(sorted(os.listdir(out)), ["func_table.c", "funcs.h", "funcs_000.c",
                                                   "manifest.toml"])
        self.assertEqual(os.listdir(self.tmp), ["gen"])  # the working folder is gone
        with open(os.path.join(out, "manifest.toml"), "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data, {"format": 1, "exe_sha256": "", "functions": 3, "files": 1,
                                "stubs": 0, "interpreted": 2, "jump_tables": 1,
                                "unresolved_jr": 0})
        self.assertEqual(dataclasses.asdict(manifest), data)
        # a complete output is replaced; anything else is refused and left alone
        with open(os.path.join(out, "stale.c"), "w") as f:
            f.write("/* from an earlier run */\n")
        recomp.recompile(image, analysis, out)
        self.assertNotIn("stale.c", os.listdir(out))
        os.remove(os.path.join(out, "manifest.toml"))
        with self.assertRaisesRegex(ValueError, "manifest.toml"):
            recomp.recompile(image, analysis, out)
        self.assertEqual(sorted(os.listdir(out)), ["func_table.c", "funcs.h", "funcs_000.c"])

    def test_paths_next_to_the_output_are_never_deleted(self):
        """Only out_dir is ever replaced: `<out>.old` and `<out>.partial`, whatever they hold,
        survive a successful and a failing run, and a failing run leaves nothing of its
        own behind (an existing output stays as it was)."""
        image, analysis = dispatch_analysis(dispatch_asm())
        out = os.path.join(self.tmp, "gen")
        mine = {}
        for name in ("gen.old", "gen.partial"):
            os.mkdir(os.path.join(self.tmp, name))
            path = os.path.join(self.tmp, name, "keep.txt")
            with open(path, "w") as f:
                f.write(name)
            mine[path] = name

        def untouched():
            for path, text in mine.items():
                with open(path) as f:
                    self.assertEqual(f.read(), text)
            self.assertEqual(sorted(os.listdir(self.tmp)), ["gen", "gen.old", "gen.partial"])
            for name in ("gen.old", "gen.partial"):
                self.assertEqual(os.listdir(os.path.join(self.tmp, name)), ["keep.txt"])
        for _ in range(2):  # first run creates out, second replaces it
            recomp.recompile(image, analysis, out)
            untouched()
        def contents():
            out_files = {}
            for n in os.listdir(out):
                with open(os.path.join(out, n)) as f:
                    out_files[n] = f.read()
            return out_files
        before = contents()
        real = recomp._write

        def failing(path, text):
            if os.path.basename(path) == "manifest.toml":
                raise OSError(28, "No space left on device")
            real(path, text)
        recomp._write = failing
        self.addCleanup(setattr, recomp, "_write", real)
        with self.assertRaisesRegex(OSError, "No space left"):
            recomp.recompile(image, analysis, out)
        untouched()
        self.assertEqual(contents(), before)
        # a failure after the old output was set aside puts it back
        real_rename = os.rename
        calls = []

        def rename(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:  # the new output into place
                raise OSError(5, "Input/output error")
            real_rename(src, dst)
        recomp._write = real
        recomp.os.rename = rename
        self.addCleanup(setattr, recomp.os, "rename", real_rename)
        with self.assertRaisesRegex(OSError, "Input/output"):
            recomp.recompile(image, analysis, out)
        recomp.os.rename = real_rename
        self.assertEqual(len(calls), 3)  # aside, into place (failed), back
        untouched()
        self.assertEqual(contents(), before)

    def test_files_hold_64_functions_in_address_order(self):
        p = Asm()
        for k in range(70):
            p.j("jal", f"f{k}")
            p(0)
        p(JR_RA, 0)
        for k in range(70):
            p.label(f"f{k}")
            p(A("ori", rt=V0, rs=0, imm=k), JR_RA, 0)
        image = image_of(p.words())
        analysis = functions.analyse(image, CODE)
        out = os.path.join(self.tmp, "gen")
        self.assertEqual(recomp.recompile(image, analysis, out).files, 2)
        names = []
        for name in ("funcs_000.c", "funcs_001.c"):
            with open(os.path.join(out, name)) as f:
                text = f.read()
            self.assertIn('#include "recomp.h"\n#include "funcs.h"\n', text)
            # the prototypes are in funcs.h only, not repeated in the function files
            self.assertNotRegex(text, r"(?m)^void fn_[0-9A-F]{8}\(uint8_t \*ram, cpu_t \*c\);$")
            names.append(re.findall(r"^void (fn_[0-9A-F]{8})\(uint8_t \*ram, cpu_t \*c\)$", text,
                                    re.M))
        self.assertEqual([len(n) for n in names], [64, 7])
        flat = names[0] + names[1]
        self.assertEqual(flat, sorted(flat))
        with open(os.path.join(out, "funcs.h")) as f:
            header = f.read()
        self.assertEqual(re.findall(r"^void (fn_[0-9A-F]{8})\(uint8_t \*ram, cpu_t \*c\);$",
                                    header, re.M), flat)

    def test_unsupported_instruction_leaves_no_output(self):
        image, analysis = dispatch_analysis(dispatch_asm(), interpreted=False)
        h = dispatch_asm().labels["h"]
        out = os.path.join(self.tmp, "gen")
        self.assertEqual(mips.decode(MFC0, h + 4).mnemonic, "mfc0")
        with self.assertRaises(recomp.RecompileError) as err:
            recomp.recompile(image, analysis, out)
        self.assertEqual(err.exception.addr, h + 4)
        self.assertIn(f"{h + 4:08X}", str(err.exception))
        self.assertIn("mfc0", str(err.exception))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_wrong_exe_hash_is_refused(self):
        p = dispatch_asm()
        exe_path = os.path.join(self.tmp, "GAME.EXE")
        image = write_exe(exe_path, p.words())
        _, analysis = dispatch_analysis(p)
        analysis.exe_sha256 = "ab" * 32
        toml = os.path.join(self.tmp, "game.functions.toml")
        functions.write_toml(analysis, toml)
        out = os.path.join(self.tmp, "gen")
        err = io.StringIO()
        with redirect_stderr(err):
            status = cli.main(["recompile", exe_path, "-o", out, "--functions", toml])
        self.assertEqual(status, 1)
        self.assertIn(image.sha256, err.getvalue())
        self.assertIn("ab" * 32, err.getvalue())
        self.assertFalse(os.path.exists(out))
        # with the right hash the CLI writes the output and says what it wrote
        analysis.exe_sha256 = image.sha256
        functions.write_toml(analysis, toml)
        with redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main(["recompile", exe_path, "-o", out, "--functions", toml]),
                             0)
        self.assertIn("wrote 3 functions in 1 files", printed.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(out, "manifest.toml")))

    def test_lookup_table_is_sorted_and_complete(self):
        block = CODE + 0x400
        p = dispatch_asm()
        p.label("k")  # a manual start that calls a loader slot
        p(A("jal", target=block + 0x10), 0, JR_RA, 0)
        p.pad_to(block + 0x40)
        L = p.labels
        image = image_of(p.words())
        analysis = functions.analyse(
            image, CODE, [L["h"], L["k"]], [(block, block + 0x40)],
            [functions.JumpTable(L["jr"], 0, [L["case0"], L["case1"]])], [L["g"], L["h"]])
        starts = sorted(f.addr for f in analysis.functions)
        self.assertIn(block + 0x10, starts)  # the stub slot
        out = os.path.join(self.tmp, "gen")
        manifest = recomp.recompile(image, analysis, out)
        self.assertEqual((manifest.stubs, manifest.interpreted), (1, 2))
        with open(os.path.join(out, "func_table.c")) as f:
            text = f.read()
        entries = re.findall(r"\{0x([0-9A-F]{8})u, (fn_[0-9A-F]{8})\}", text)
        self.assertEqual([int(a, 16) for a, _ in entries], starts)
        self.assertEqual([n for _, n in entries], [recomp.c_name(a) for a in starts])
        self.assertIn("recomp_fn recomp_lookup(uint32_t addr)", text)

    def test_interpreted_function_is_a_thunk(self):
        p = dispatch_asm()
        image, analysis = dispatch_analysis(p)
        for label in ("g", "h"):  # h holds mfc0: its body is never emitted or checked
            fn = next(f for f in analysis.functions if f.addr == p.labels[label])
            text = recomp.function_c(image, analysis, fn)
            body = text.split("{\n", 1)[1]
            self.assertEqual(body, f"    interp_run(ram, c, 0x{fn.addr:08X}u);\n}}\n")
        main = recomp.function_c(image, analysis, analysis.functions[0])
        self.assertIn(f"fn_{p.labels['g']:08X}(ram, c);", main)

    def test_function_text_without_prototypes(self):
        p = dispatch_asm()
        image, analysis = dispatch_analysis(p)
        main = analysis.functions[0]
        g = f"void fn_{p.labels['g']:08X}(uint8_t *ram, cpu_t *c);\n"
        self.assertTrue(recomp.function_c(image, analysis, main).startswith(g))
        self.assertTrue(recomp.function_c(image, analysis, main, prototypes=False)
                        .startswith("/* _start */\n"))


class ManualEntriesTest(RecompTestCase):
    def test_manual_jump_table_is_used(self):
        p = dispatch_asm()
        L = p.labels
        table = functions.JumpTable(L["jr"], 0, [L["case0"], L["case1"], L["case0"]])
        prog = Program(p, manual=[L["h"]], jump_tables=[table], interpreted=[L["g"], L["h"]])
        text = prog.c[CODE]
        self.assertIn("switch (target) {", text)
        self.assertEqual(sorted(re.findall(r"^\s*case 0x([0-9A-F]{8})u:", text, re.M)),
                         [f"{L['case0']:08X}", f"{L['case1']:08X}"])
        self.assertNotIn(f"rt_jump(ram, c, target);\n        return;\n    }}\n}}", text)

        def setup(s, rng, i):
            s.r[A0] = 0x80200000  # the harness's scratch page holds the case address
            s.mem[0x80200000] = (L["case0"], L["case1"])[i % 2]
        cases = prog.cases("manual-table", setup)
        got = self.assertSameAsInterpreter(prog, cases, unrecompiled=[L["g"]])
        for i, state in enumerate(got):
            self.assertEqual(state.r[V0], 10 + i % 2)
            self.assertEqual(state.r[V1], 7)  # g ran, in the interpreter


def _make():
    make = shutil.which("make")
    if make is None:
        raise unittest.SkipTest("no `make` on PATH")
    charness._cc()  # skips without `cc`
    return make


def linkcheck(make, generated, build):
    """Run `make linkcheck` on a generated folder; the CompletedProcess."""
    return subprocess.run([make, "-C", charness.REPO, "linkcheck", f"GENERATED={generated}",
                           f"BUILD={build}"], capture_output=True, text=True)


class LinkTest(unittest.TestCase):
    """The generated files, compiled with the stub runtime by `make linkcheck`."""

    def setUp(self):
        self.make = _make()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_synthetic_image_links_and_runs(self):
        block = CODE + 0x400
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.j("jal", "f")  # recompiled
        p(0)
        p.j("jal", "g")  # interpreted
        p(0)
        p(A("lui", rt=T0, imm=0xB000))
        p(A("lw", rt=T1, rs=T0, imm=0))  # the bus, twice: logged once
        p(A("lw", rt=T2, rs=T0, imm=0))
        p(A("sw", rt=T1, rs=T0, imm=4))
        p(A("jal", target=block + 0x18))  # a loader slot
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p(JR_RA, 0)
        p.label("f")
        p(A("ori", rt=V0, rs=0, imm=0x1234), JR_RA, 0)
        p.label("g")
        p(A("addiu", rt=SP, rs=SP, imm=-8), A("ori", rt=V1, rs=0, imm=7), JR_RA,
          A("addiu", rt=SP, rs=SP, imm=8))
        p.pad_to(block + 0x40)
        exe_path = os.path.join(self.tmp, "GAME.EXE")
        image = write_exe(exe_path, p.words())
        analysis = functions.analyse(image, CODE, (), [(block, block + 0x40)], (),
                                     [p.labels["g"]])
        gen, build = os.path.join(self.tmp, "gen"), os.path.join(self.tmp, "build")
        recomp.recompile(image, analysis, gen)
        res = linkcheck(self.make, gen, build)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        stub = os.path.join(build, "game_stub")
        run = subprocess.run([stub, f"0x{CODE:08X}", exe_path], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        regs = [int(v, 16) for v in run.stdout.split("\n")[0].split()[1:]]
        self.assertEqual(len(regs), 32)
        self.assertEqual((regs[V0], regs[V1], regs[T1], regs[T2]), (0x1234, 7, 0, 0))
        log = run.stderr.splitlines()
        self.assertEqual(log, [f"stub: interp_run entered at 0x{p.labels['g']:08X}",
                               "stub: bus_read 0xB0000000 size 4",
                               "stub: bus_write 0xB0000004 size 4 value 0x00000000",
                               "stub: rt_loader slot 0x018"])
        # nothing changed: nothing is compiled or linked again
        again = linkcheck(self.make, gen, build)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn(" -c ", again.stdout)
        self.assertNotIn(" -o ", again.stdout)

    def test_lookup_hits_and_misses_in_the_linked_program(self):
        """game_stub enters through rt_call, so recomp_lookup: the first and the last table
        entries run natively; an address below the first, above the last and inside a
        function miss and run in the interpreter, which logs the address."""
        p = Asm()
        p(A("addu", rd=S0, rs=RA, rt=0))
        p.j("jal", "last")
        p(0)
        p(A("addu", rd=RA, rs=S0, rt=0))
        p(JR_RA, A("ori", rt=V1, rs=0, imm=1))
        p.label("last")
        p(A("ori", rt=V0, rs=0, imm=2), JR_RA, 0)
        p(0x7FFFFFFF)  # data: the code after it is not reached
        p.label("leaf")
        p(A("ori", rt=A0, rs=0, imm=3), JR_RA, 0)
        exe_path = os.path.join(self.tmp, "GAME.EXE")
        image = write_exe(exe_path, p.words())
        analysis = functions.analyse(image, CODE)
        L = p.labels
        self.assertEqual([f.addr for f in analysis.functions], [CODE, L["last"]])
        gen, build = os.path.join(self.tmp, "gen"), os.path.join(self.tmp, "build")
        recomp.recompile(image, analysis, gen)
        res = linkcheck(self.make, gen, build)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        stub = os.path.join(build, "game_stub")
        runs = [  # entry, interpreted?, (v0, v1, a0)
            (CODE, False, (2, 1, 0)),
            (L["last"], False, (2, 0, 0)),
            (CODE - 8, True, (2, 1, 0)),  # zero words (nops) run on into the first function
            (L["leaf"], True, (0, 0, 3)),
            (L["last"] + 4, True, (0, 0, 0)),
        ]
        for entry, interpreted, want in runs:
            with self.subTest(entry=hex(entry)):
                run = subprocess.run([stub, f"{entry:08X}", exe_path], capture_output=True,
                                     text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                regs = [int(v, 16) for v in run.stdout.split("\n")[0].split()[1:]]
                self.assertEqual((regs[V0], regs[V1], regs[A0]), want)
                log = [f"stub: interp_run entered at 0x{entry:08X}"] if interpreted else []
                self.assertEqual(run.stderr.splitlines(), log)

    def test_linkcheck_refuses_a_folder_without_manifest(self):
        image, analysis = dispatch_analysis(dispatch_asm())
        gen, build = os.path.join(self.tmp, "gen"), os.path.join(self.tmp, "build")
        recomp.recompile(image, analysis, gen)
        os.remove(os.path.join(gen, "manifest.toml"))
        res = linkcheck(self.make, gen, build)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"{gen}/manifest.toml not found", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(build, "game_stub")))


if __name__ == "__main__":
    unittest.main()
