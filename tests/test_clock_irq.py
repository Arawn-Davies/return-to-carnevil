"""runtime/core's virtual clock, interrupt and exception dispatch (runtime/include/machine.h),
against docs/analysis/loader-services.md section 6.

The C runs in tests/harness/machine_driver.c (its top comment lists the commands), built with
the global C flags, -Wall -Wextra -Werror and the sanitizer flags, with the interpreter
running synthetic guest code assembled here, into a cache under the system temp dir. No game
data is needed.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

from carnevil_tools import vectors
from tests import charness

REPO = charness.REPO
SOURCES = [os.path.join(REPO, "runtime", "core", f)
           for f in ("call.c", "fpu.c", "clock.c", "irq.c", "exc.c")] + \
          [os.path.join(REPO, "runtime", "interp", "interp.c")]
DRIVER = os.path.join(REPO, "tests", "harness", "machine_driver.c")
HEADERS = [os.path.join(charness.INCLUDE, h) for h in ("recomp.h", "machine.h")]
CACHE = os.path.join(tempfile.gettempdir(), "carnevil-machine-driver")

A = vectors.assemble
NOP = 0
AT, V0, A0, A1, A2, T0, T1, T2, T3, S7, K0, SP, RA = 1, 2, 4, 5, 6, 8, 9, 10, 11, 23, 26, 29, 31

MAIN = 0x80100000
H = 0x80110000
H2 = 0x80112000
CNT = 0x80200000
FLAG = 0x80200004
COPY = 0x80201000
PROBE = 0x80300000
FRAME = 0x80014328
FRAME_SIZE = 0x320
EXC_SP = 0x800C1400
NS_PER_CHECK = 100
P = 100_000  # the tests' VBLANK period: 1,000 check points
STATUS = 0x30010401
EXL = 2
MARK = 0xA5A5A5A5
FCSR = 0x00800001  # condition code 0 and RM = 1, so both kinds of FCSR bit are carried


def build_driver(generated=None):
    """Path of the machine driver binary. Raises unittest.SkipTest without `cc`. generated:
    C text of recompiled functions defining extra_lookup (machine_driver.c), linked in and
    built with the generated code's warning flags."""
    cc = charness._cc()
    flags = charness.CFLAGS + charness.RUNTIME_WARNINGS + charness.SANITIZE
    gen_flags = charness.CFLAGS + charness.GENERATED_WARNINGS + charness.SANITIZE
    h = hashlib.sha256()
    st = os.stat(os.path.realpath(cc))
    h.update(f"{os.path.realpath(cc)}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    h.update("\0".join(flags).encode())
    for path in SOURCES + [DRIVER] + HEADERS:
        with open(path, "rb") as f:
            h.update(path.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    if generated is not None:
        h.update(b"generated\0" + generated.encode())
    out = os.path.join(CACHE, h.hexdigest()[:32])
    exe = os.path.join(out, "machine_driver")
    if os.path.exists(exe):
        return exe
    os.makedirs(CACHE, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=CACHE, prefix="build-")
    try:
        objs = []
        for i, src in enumerate(SOURCES + [DRIVER]):
            obj = os.path.join(tmp, f"{i}-{os.path.basename(src)}.o")
            charness._cc_run([cc, *flags, "-I", charness.INCLUDE, "-c", src, "-o", obj])
            objs.append(obj)
        if generated is not None:
            src = os.path.join(tmp, "generated.c")
            with open(src, "w") as f:
                f.write(generated)
            charness._cc_run([cc, *gen_flags, "-I", charness.INCLUDE, "-c", src, "-o",
                              src + ".o"])
            objs.append(src + ".o")
        charness._cc_run([cc, *charness.SANITIZE, *objs, "-lm",
                          "-o", os.path.join(tmp, "machine_driver")])
        try:
            os.rename(tmp, out)
        except OSError:  # another process built the same key first
            if not os.path.exists(exe):
                raise
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    return exe


# --- guest code --------------------------------------------------------------------------

def li(reg, value):
    return [A("lui", rt=reg, imm=value >> 16 & 0xFFFF), A("ori", rt=reg, rs=reg, imm=value & 0xFFFF)]


def asm(*items):
    """Words of a program: ints are words, lists are spliced, a str is a label, and a tuple
    (mnemonic, rs, rt, label) is a branch to a label."""
    labels, words, fix = {}, [], []
    for item in items:
        if isinstance(item, str):
            labels[item] = len(words)
        elif isinstance(item, tuple):
            fix.append((len(words), item))
            words.append(0)
        elif isinstance(item, list):
            words += item
        else:
            words.append(item)
    for i, (mnemonic, rs, rt, label) in fix:
        words[i] = A(mnemonic, rs=rs, rt=rt, imm=labels[label] - (i + 1))
    return words


def call_probe(tag):
    """ra saved in s7, a2 = tag, jal PROBE, ra restored."""
    return [A("or", rd=S7, rs=RA, rt=0), A("ori", rt=A2, rs=0, imm=tag),
            A("jal", target=PROBE), NOP, A("or", rd=RA, rs=S7, rt=0)]


def ret(v0):
    return [A("jr", rs=RA), A("ori", rt=V0, rs=0, imm=v0)]


# The guest's main loop: t0 counts down to 0, one check point per iteration.
LOOP = asm("loop", A("addiu", rt=T0, rs=T0, imm=-1), ("bne", T0, 0, "loop"), NOP,
           A("jr", rs=RA), NOP)
LOOP_EPC = MAIN  # the pc the loop's check point stores: the back edge's target


def place(addr, words):
    return [f"mem {addr + 4 * i:08X} {w:08X}" for i, w in enumerate(words)]


def pattern():
    """Distinct register values, half of them with bit 31 set (sign extension shows)."""
    r = [0] + [(0x80000000 if n % 2 else 0) | n << 16 | (0x100 + n) for n in range(1, 32)]
    fpr = [0x3F000000 + 0x10101 * k for k in range(32)]
    return r, 0x11223344, 0x8899AABB, fpr


def set_state(r, hi, lo, fpr, fcsr=FCSR):
    cmds = [f"r {n:X} {v:08X}" for n, v in enumerate(r)]
    cmds += [f"fpr {n:X} {v:08X}" for n, v in enumerate(fpr)]
    return cmds + [f"hilo {hi:08X} {lo:08X}", f"fcsr {fcsr:08X}"]


def run_main(n, entry=MAIN):
    return [f"r {T0:X} {n:X}", f"run {entry:08X}"]


class Out:
    """The driver's output, parsed."""

    def __init__(self, text):
        self.lines = [line.split() for line in text.splitlines()]

    def all(self, key):
        return [line[1:] for line in self.lines if line[0] == key]

    def last(self, key):
        found = self.all(key)
        if not found:
            raise AssertionError(f"no {key!r} line in the driver's output")
        return found[-1]

    def probes(self):
        return [dict(zip(("a0", "a1", "a2", "sp", "status"), (int(x, 16) for x in p[:5])),
                     now=int(p[5]), resume=int(p[6], 16), epc=int(p[7], 16))
                for p in self.all("probe")]

    def regs(self):
        return [int(x, 16) for x in self.last("r")]

    def fpr(self):
        return [int(x, 16) for x in self.last("fpr")]

    def hilo(self):
        return tuple(int(x, 16) for x in self.last("hilo"))

    def fcsr(self):
        return int(self.last("fcsr")[0], 16)

    def m(self):
        f = self.last("m")
        return dict(status=int(f[0], 16), cause=int(f[1], 16), intctl=int(f[2], 16),
                    gtcause=int(f[3], 16), now=int(f[4]), slice=int(f[5]), pc=int(f[6], 16))

    def dump(self, i=-1):
        return [int(x, 16) for x in self.all("dump")[i]]


class ClockIrqTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_driver()

    def drive(self, cmds, status=0):
        res = subprocess.run([self.exe], input="\n".join(["reset"] + cmds) + "\n",
                             capture_output=True, text=True, timeout=120)
        if res.returncode != status:
            self.fail(f"driver status {res.returncode}, want {status}: {res.stderr}")
        if status == 0 and res.stderr:
            self.fail(f"driver wrote to stderr: {res.stderr}")
        return Out(res.stdout), res.stderr

    def stops(self, cmds, *needles):
        """The driver stops through rt_stop: status 3, one "carnevil: stop:" line holding
        every needle."""
        out, err = self.drive(cmds, status=3)
        lines = [line for line in err.splitlines() if line.startswith("carnevil: stop: ")]
        self.assertEqual(len(lines), 1, err)
        for needle in needles:
            self.assertIn(needle, lines[0])
        return out, err

    # --- delivery ------------------------------------------------------------------------

    def test_vblank_delivery(self):
        clobber = [A("ori", rt=n, rs=0, imm=0xB000 + n) for n in range(1, 31) if n != S7]
        handler = asm(call_probe(1), li(T0, CNT), A("lw", rt=T1, base=T0, imm=0),
                      A("addiu", rt=T1, rs=T1, imm=1), A("sw", rt=T1, base=T0, imm=0),
                      A("mult", rs=T0, rt=T1), li(T0, 0x40490FDB),
                      [A("mtc1", rt=T0, fs=k) for k in range(32)],
                      A("ori", rt=T1, rs=0, imm=3), A("ctc1", rt=T1, fs=31), clobber,
                      A("ori", rt=RA, rs=0, imm=0xB01F), A("or", rd=RA, rs=S7, rt=0),
                      A("ori", rt=S7, rs=0, imm=0xB017), ret(1))
        r, hi, lo, fpr = pattern()
        # 10,210 check points (main's entry, the 10,199 taken backward branches of 10,200
        # iterations, ten handler entries): ten periods and a fraction
        out, _ = self.drive(place(MAIN, LOOP) + place(H, handler) + set_state(r, hi, lo, fpr)
                            + [f"line 0 {H:08X}", f"vblank {P}"] + run_main(10200)
                            + [f"dump {CNT:08X} 1"])
        probes = out.probes()
        self.assertEqual(len(probes), 10)
        self.assertEqual(out.dump(), [10])
        for k, p in enumerate(probes):
            self.assertEqual((p["a0"], p["a1"], p["sp"]), (0x4000, FRAME, EXC_SP))
            self.assertEqual(p["status"], STATUS | 0x4000 | 1 | EXL)
            self.assertGreaterEqual(p["now"], (k + 1) * P)
            self.assertLess(p["now"], (k + 1) * P + 10 * NS_PER_CHECK)
        want = list(r)
        want[T0] = 0
        got = out.regs()
        got[K0] = want[K0]  # k0 is not restored
        self.assertEqual(got, want)
        self.assertEqual(out.hilo(), (hi, lo))
        self.assertEqual(out.fpr(), fpr)
        self.assertEqual(out.fcsr(), FCSR)
        m = out.m()
        self.assertEqual(m["status"], STATUS | 0x4000 | 1)
        self.assertEqual(m["cause"], 0)
        self.assertTrue(m["intctl"] & 0x4000)
        self.assertEqual(m["now"], 10210 * NS_PER_CHECK)

    def test_no_nesting(self):
        # The first call loops for 3 periods and returns 0 (no default work, so the VBLANK
        # stays pending); later calls return 1 at once (the default work acknowledges).
        handler = asm(call_probe(1), li(T0, FLAG), A("lw", rt=T1, base=T0, imm=0),
                      ("bne", T1, 0, "quick"), NOP,
                      A("ori", rt=T1, rs=0, imm=1), A("sw", rt=T1, base=T0, imm=0),
                      A("ori", rt=T2, rs=0, imm=3 * P // NS_PER_CHECK + 1),  # 3,000 checks
                      "busy", A("addiu", rt=T2, rs=T2, imm=-1), ("bne", T2, 0, "busy"), NOP,
                      call_probe(2), ret(0),
                      "quick", call_probe(2), ret(1))
        out, _ = self.drive(place(MAIN, LOOP) + place(H, handler)
                            + [f"line 0 {H:08X}", f"vblank {P}"] + run_main(10200))
        probes = out.probes()
        self.assertEqual([p["a2"] for p in probes], [1, 2] * (len(probes) // 2))
        entries = [p["now"] for p in probes if p["a2"] == 1]
        exits = [p["now"] for p in probes if p["a2"] == 2]
        self.assertTrue(P <= entries[0] < P + 1000, entries)
        self.assertGreaterEqual(exits[0] - entries[0], 3 * P)
        # the VBLANKs due during the long call are one pending bit, delivered once at once
        self.assertLess(entries[1] - exits[0], 1000)
        self.assertGreater(entries[1], 4 * P)
        # then one per period again, starting with the first VBLANK after the second call
        for k, t in enumerate(entries[2:]):
            self.assertEqual(t // P, 5 + k)
            self.assertLess(t % P, 1000)
        self.assertEqual(len(entries), 2 + out.m()["now"] // P - 4)
        for p in probes:
            self.assertTrue(p["status"] & EXL)

    def test_masked_until_registered(self):
        r, hi, lo, fpr = pattern()
        out, _ = self.drive(place(MAIN, LOOP) + place(H, asm(call_probe(1), ret(1)))
                            + set_state(r, hi, lo, fpr) + [f"vblank {P}"] + run_main(10200))
        self.assertEqual(out.probes(), [])
        m = out.m()
        self.assertGreaterEqual(m["now"], 10 * P)
        self.assertEqual(m["status"], STATUS)
        # not even pending: the board's VBLANK enable (interrupt enable bit 7) is set only by
        # registering line 0 (slot 0x200), so the retrace never reaches IP6
        self.assertEqual(m["cause"], 0)
        # INTCTL as machine_reset leaves it: the value measured at the game's entry
        # (loader-services.md, open question 1)
        self.assertEqual(m["intctl"], 0x4000)
        want = list(r)
        want[T0] = 0
        self.assertEqual(out.regs(), want)

    def test_masked_pending_not_delivered(self):
        # IP6 raised directly (as a device would) while Status IM6 is clear, with line 0
        # never registered: it stays pending in Cause and is never delivered
        r, hi, lo, fpr = pattern()
        out, _ = self.drive(place(MAIN, LOOP) + place(H, asm(call_probe(1), ret(1)))
                            + set_state(r, hi, lo, fpr) + ["irqraise 6"] + run_main(10200))
        self.assertEqual(out.probes(), [])
        m = out.m()
        self.assertEqual(m["status"], STATUS)
        self.assertEqual(m["cause"], 0x4000)  # pending, masked

    def test_frame_layout(self):
        handler = asm(li(T0, FRAME), li(T1, COPY), A("addiu", rt=T2, rs=T0, imm=FRAME_SIZE),
                      "copy", A("lw", rt=T3, base=T0, imm=0), A("sw", rt=T3, base=T1, imm=0),
                      A("addiu", rt=T0, rs=T0, imm=4), A("addiu", rt=T1, rs=T1, imm=4),
                      ("bne", T0, T2, "copy"), NOP, ret(1))
        r, hi, lo, fpr = pattern()
        marks = place(FRAME, [MARK] * (FRAME_SIZE // 4))
        out, _ = self.drive(place(MAIN, LOOP) + place(H, handler) + marks
                            + set_state(r, hi, lo, fpr) + [f"line 0 {H:08X}", f"vblank {P}"]
                            + run_main(P // NS_PER_CHECK + 50)
                            + [f"dump {COPY:08X} {FRAME_SIZE // 4:X}",
                               f"dump {FRAME:08X} {FRAME_SIZE // 4:X}"])
        w = out.dump(0)

        def slot(off):
            return w[off // 4], w[off // 4 + 1]

        def sext(v):
            return v, 0xFFFFFFFF if v & 0x80000000 else 0

        self.assertEqual(slot(0x000), (MARK, MARK))  # r0 is not written
        for n in range(1, 32):
            if n == K0:
                self.assertEqual(slot(0xD0), (MARK, MARK))  # k0 is not saved
            elif n == T0:
                self.assertEqual(slot(8 * n), sext(slot(8 * n)[0]))  # the loop counter
            else:
                self.assertEqual(slot(8 * n), sext(r[n]), f"GPR {n}")
        self.assertEqual(slot(0x100), sext(lo))
        self.assertEqual(slot(0x108), sext(hi))
        self.assertEqual(slot(0x110), (LOOP_EPC, MARK))  # the resume pc is a 32-bit sw
        self.assertEqual(slot(0x118 + 8 * 12), (STATUS | 0x4000 | 1 | EXL, 0))
        self.assertEqual(slot(0x180), (0x4000, 0))  # Cause: IP6, code 0
        self.assertEqual(slot(0x188), sext(LOOP_EPC))  # EPC
        for k in range(16):
            self.assertEqual(w[(0x218 + 16 * k) // 4], fpr[2 * k])
            self.assertEqual(w[(0x21C + 16 * k) // 4], fpr[2 * k + 1])
            self.assertEqual(slot(0x220 + 16 * k), (MARK, MARK))
        self.assertEqual(w[0x31C // 4], FCSR)
        # after the loop, the IP bit is cleared in the saved Cause
        self.assertEqual(out.dump(1)[0x180 // 4], 0)

    def test_handler_edits_frame(self):
        handler = asm(call_probe(3), A("lw", rt=T0, base=A1, imm=0x110),
                      A("addiu", rt=T0, rs=T0, imm=4), A("sw", rt=T0, base=A1, imm=0x110),
                      A("ori", rt=T1, rs=0, imm=0x5678), A("sw", rt=T1, base=A1, imm=0x10),
                      A("sw", rt=0, base=A1, imm=0x14), A("ori", rt=T2, rs=0, imm=0xDEAD),
                      ret(0))
        r, hi, lo, fpr = pattern()
        r[V0] = 0x1234
        epc = MAIN + 0x40
        out, _ = self.drive(place(H, handler) + set_state(r, hi, lo, fpr)
                            + [f"exc D {H:08X}", f"raise D {epc:08X}"])
        self.assertEqual(out.last("resume"), [f"{epc + 4:08X}"])
        [p] = out.probes()
        self.assertEqual((p["a0"], p["a1"], p["sp"]), (13 << 2, FRAME, EXC_SP))
        self.assertTrue(p["status"] & EXL)
        want = list(r)
        want[V0] = 0x5678
        got = out.regs()
        got[K0] = want[K0]
        self.assertEqual(got, want)
        self.assertEqual(out.fpr(), fpr)
        m = out.m()
        self.assertEqual(m["status"], STATUS)
        self.assertTrue(m["intctl"] & 0x4000)
        # the pc follows the resume pc, so a later trap does not record the handler's
        self.assertEqual(m["pc"], epc + 4)

    def test_gt_cause(self):
        out, _ = self.drive(place(MAIN, LOOP) + place(H, asm(call_probe(4), ret(7)))
                            + [f"gt 4 {H:08X}", "gtraise 4"] + run_main(10))
        [p] = out.probes()
        self.assertEqual((p["a0"], p["sp"], p["a2"]), (1, EXC_SP, 4))
        m = out.m()
        self.assertEqual((m["gtcause"], m["cause"]), (0, 0))

    def test_raise_outside_event_delivered_next_check(self):
        # A line raised outside a clock event (as a loader slot or a bus access raises one),
        # followed by vclock_reload, is delivered at the very next check point, not when the
        # current slice of up to 1,000,000 checks runs out.
        out, _ = self.drive(place(MAIN, LOOP) + place(H, asm(call_probe(1), ret(1)))
                            + [f"line 0 {H:08X}", "tick", "irqraise 6", "reload"]
                            + run_main(10))
        [p] = out.probes()
        # delivered at main's entry check; the probe also sees the handler's entry check
        self.assertEqual(p["now"], 2 * NS_PER_CHECK)

    def test_pending_after_exception_delivered_next_check(self):
        # A VBLANK that comes due while a break hook runs (EXL set) is delivered at the first
        # check after the exception returns: exc_raise reloads the slice once EXL is clear.
        hook = asm(A("lw", rt=T0, base=A1, imm=0x110), A("addiu", rt=T0, rs=T0, imm=4),
                   A("sw", rt=T0, base=A1, imm=0x110),
                   A("ori", rt=T2, rs=0, imm=3 * P // NS_PER_CHECK // 2),  # 1,500 checks
                   "busy", A("addiu", rt=T2, rs=T2, imm=-1), ("bne", T2, 0, "busy"), NOP,
                   call_probe(2), ret(0))
        main = [A("break", code=0x1C00)] + LOOP
        out, _ = self.drive(place(MAIN, main) + place(H, asm(call_probe(1), ret(1)))
                            + place(H2, hook)
                            + [f"line 0 {H:08X}", f"hook 7 {H2:08X}", f"vblank {P}"]
                            + run_main(100))
        probes = out.probes()
        self.assertEqual([p["a2"] for p in probes], [2, 1])
        self.assertGreater(probes[0]["now"], P)  # the VBLANK came due inside the hook
        self.assertLess(probes[1]["now"] - probes[0]["now"], 10 * NS_PER_CHECK)

    def test_ip3_default_lowers_the_line(self):
        # the default IP3 work clears INTCTL bit 0 and the pending IP3 bit, so one raise is
        # one delivery
        out, _ = self.drive(place(MAIN, LOOP) + place(H, asm(call_probe(3), ret(1)))
                            + [f"line 3 {H:08X}", "intctl 4001", "irqraise 3"]
                            + run_main(100))
        self.assertEqual(len(out.probes()), 1)
        m = out.m()
        self.assertEqual(m["cause"], 0)
        self.assertEqual(m["intctl"], 0x4000)

    def test_ip_order(self):
        # VBLANK (IP6) and a GT64010 cause bit (IP2) pending at once: IP2's work first
        out, _ = self.drive(place(MAIN, LOOP) + place(H, asm(call_probe(1), ret(1)))
                            + place(H2, asm(call_probe(4), ret(0)))
                            + [f"line 0 {H:08X}", f"gt 4 {H2:08X}", "gtraise 4", "irqraise 6"]
                            + run_main(10))
        self.assertEqual([p["a2"] for p in out.probes()], [4, 1])
        self.assertEqual(out.m()["cause"], 0)

    def test_long_vblank_handler_absorbs(self):
        # The board's VBLANK path: the game's line-0 handler returns 1, so the default work
        # acknowledges. A first call that runs for three periods is entered once; the VBLANKs
        # that came due during it are absorbed by its acknowledgement, and the next delivery
        # is the first VBLANK that comes due after it returned.
        handler = asm(call_probe(1), li(T0, FLAG), A("lw", rt=T1, base=T0, imm=0),
                      ("bne", T1, 0, "quick"), NOP,
                      A("ori", rt=T1, rs=0, imm=1), A("sw", rt=T1, base=T0, imm=0),
                      A("ori", rt=T2, rs=0, imm=3 * P // NS_PER_CHECK + 1),  # 3,001 checks
                      "busy", A("addiu", rt=T2, rs=T2, imm=-1), ("bne", T2, 0, "busy"), NOP,
                      "quick", call_probe(2), ret(1))
        out, _ = self.drive(place(MAIN, LOOP) + place(H, handler)
                            + [f"line 0 {H:08X}", f"vblank {P}"] + run_main(10200))
        probes = out.probes()
        self.assertEqual([p["a2"] for p in probes], [1, 2] * (len(probes) // 2))
        entries = [p["now"] for p in probes if p["a2"] == 1]
        exits = [p["now"] for p in probes if p["a2"] == 2]
        self.assertTrue(P <= entries[0] < P + 1000, entries)
        self.assertGreaterEqual(exits[0] - entries[0], 3 * P)
        self.assertEqual(entries[1] // P, exits[0] // P + 1)
        self.assertLess(entries[1] % P, 1000)
        for k, t in enumerate(entries[1:]):
            self.assertEqual(t // P, entries[1] // P + k)

    # --- break and FP exceptions -----------------------------------------------------------

    def test_break_services(self):
        r, hi, lo, fpr = pattern()
        # break 0x409 installs (a0 code, a1 handler); a code above 15 is ignored
        out, _ = self.drive(place(H, asm(call_probe(5), ret(0))) + set_state(r, hi, lo, fpr)
                            + [f"r {A0:X} D", f"r {A1:X} {H:08X}", "break 80100020 409",
                               f"r {A0:X} 10", f"r {A1:X} {H2:08X}", "break 80100024 409",
                               "handlers"])
        want = list(r)
        want[A0], want[A1] = 0x10, H2
        self.assertEqual(out.regs(), want)
        self.assertEqual([int(x, 16) for x in out.last("handlers")],
                         [H if code == 13 else 0 for code in range(16)])
        self.assertEqual(out.m()["status"], STATUS)
        # break 0x407 (an assert) stops, naming the pc
        self.stops(["break 80100024 407"], "0x80100024")
        # break 7 with a hook: hook(Cause, frame); the hook leaving the pc stops naming it
        out, _ = self.stops(place(H, asm(call_probe(6), ret(0)))
                            + [f"hook 7 {H:08X}", "break 80100028 1C00"], "0x80100028")
        [p] = out.probes()
        self.assertEqual((p["a0"], p["a1"], p["sp"]), (9 << 2, FRAME, EXC_SP))
        # a hook that moves the resume pc past the break returns to the next instruction
        skip = asm(A("lw", rt=T0, base=A1, imm=0x110), A("addiu", rt=T0, rs=T0, imm=4),
                   A("sw", rt=T0, base=A1, imm=0x110), ret(0))
        out, _ = self.drive(place(H, skip) + set_state(r, hi, lo, fpr)
                            + [f"hook 6 {H:08X}", "break 80100028 1800"])
        self.assertEqual(out.all("returned"), [[]])
        got = out.regs()
        got[K0] = r[K0]
        self.assertEqual(got, r)

    def test_fp_trap_reaches_catch_all(self):
        catch_all = asm(li(T0, 0xBFC00000), A("jr", rs=T0), NOP)
        main = asm(A("ori", rt=A0, rs=0, imm=15), li(A1, H), A("break", code=0x409),
                   A("div.s", fd=0, fs=2, ft=4), A("jr", rs=RA), NOP)
        self.stops(place(MAIN, main) + place(H, catch_all)
                   + ["fpr 2 3F800000", "fpr 4 0", "fcsr 400", f"run {MAIN:08X}"],
                   "reset vector", "0xBFC00000")

    def test_fpe_handler_advances(self):
        # no code-15 handler: the default calls the FPU handler of each FCSR cause bit with
        # a0 = frame and clears the bit in the saved FCSR; this one skips the instruction
        skip = asm(call_probe(7), A("lw", rt=T0, base=A0, imm=0x110),
                   A("addiu", rt=T0, rs=T0, imm=4), A("sw", rt=T0, base=A0, imm=0x110), ret(9))
        # (break 0x409 with a0 = 0, a1 = 0 only hands the runtime its RAM: it installs no
        # handler)
        out, _ = self.drive(place(H, skip) + [f"fpe 3 {H:08X}", "fcsr 400",
                                              "break 80100000 409", f"pc {MAIN + 0x10:08X}",
                                              "fptrap 8000"])
        [p] = out.probes()
        self.assertEqual((p["a0"], p["sp"]), (FRAME, EXC_SP))
        self.assertEqual(len(out.all("returned")), 2)  # the break, then the trap
        self.assertEqual(out.fcsr(), 0x400)  # the cause bit is cleared in the saved FCSR

    # --- the clock -------------------------------------------------------------------------

    def test_timer_far_future(self):
        t = 1 << 40
        out, _ = self.drive([f"event 1 {t}", f"event 2 {t}", f"event 3 {t + 1}",
                             f"advance {t + 1000}", "checks 5", "checks 0"])
        events = [(int(i), int(now)) for i, now in out.all("event")]
        self.assertEqual([i for i, _ in events], [1, 2, 3])
        for _i, now in events:
            self.assertTrue(t <= now < t + NS_PER_CHECK, now)
        now, ticks, max_slice, min_slice, backwards = map(int, out.last("advanced"))
        self.assertGreaterEqual(now, t + 1000)
        self.assertEqual(backwards, 0)
        self.assertTrue(1 <= min_slice <= max_slice <= 1_000_000, (min_slice, max_slice))
        self.assertGreater(ticks, t // NS_PER_CHECK // 1_000_000)
        later, same = (int(x[0]) for x in out.all("now"))
        self.assertEqual(later, now + 5 * NS_PER_CHECK)
        self.assertEqual(same, later)

    def test_event_order(self):
        # time order; equal times in scheduling order; an event may schedule one at the
        # current time, which runs in the same tick; a cancelled event does not run
        out, _ = self.drive(["event 5 3000", "chain 1 2000", "event 4 2000", "event 9 2500",
                             "cancel 9", "advance 4000"])
        self.assertEqual(out.all("event"), [["1", "2000"], ["4", "2000"], ["2", "2000"],
                                            ["5", "3000"]])

    def test_vclock_advance(self):
        """vclock_advance(c, ns): the checks used so far are accounted, time moves by exactly
        ns, the events due inside the interval run once each, in time order, each at its own
        time (one an event schedules at its time too), a later one waits, and the slice is
        reloaded for it."""
        out, _ = self.drive(["checks 5", "event 1 1000", "chain 2 1500", "event 4 5000",
                             "advanceby 2000", "checks 3", "advance 6000"])
        self.assertEqual(out.all("now"), [["500"], ["2800"]])
        self.assertEqual(out.all("event"), [["1", "1000"], ["2", "1500"], ["3", "1500"],
                                            ["4", "5000"]])
        self.assertEqual(out.all("advancedby"), [["2500", "25"]])

    def test_vclock_advance_delivers_nothing(self):
        """An interrupt raised by an event inside vclock_advance is pending afterwards, with
        the slice at 1, and is delivered by the next check point's tick, not inside."""
        out, _ = self.drive([f"line 0 {PROBE:08X}", "vblank 1000",
                             "advanceby 3500", "state", "tick", "state"])
        keys = [line[0] for line in out.lines]
        self.assertLess(keys.index("m"), keys.index("probe"), keys)  # none inside the advance
        self.assertEqual(out.all("advancedby"), [["3500", "1"]])
        self.assertTrue(int(out.all("m")[0][1], 16) & 0x4000)  # IP6 pending after it
        probes = out.probes()
        self.assertEqual(len(probes), 1)
        self.assertEqual((probes[0]["a0"], probes[0]["now"]), (0x4000, 3600))

    # --- stops -----------------------------------------------------------------------------

    def test_stop_paths(self):
        nested = asm(A("break", code=0x409), ret(0))
        moves = asm(A("lw", rt=T0, base=A1, imm=0x110), A("addiu", rt=T0, rs=T0, imm=8),
                    A("sw", rt=T0, base=A1, imm=0x110), ret(1))
        cases = [
            (["break 80100024 402"], ["0x80100024", "0x402"]),
            (["break 80100024 1234"], ["0x80100024", "0x1234"]),
            (["break 80100024 1C00"], ["0x80100024", "0x1C00"]),  # no hook
            (place(H, moves) + [f"hook 7 {H:08X}", "break 80100024 1C00"],
             ["0x80100024", "0x8010002C"]),
            (place(H, nested) + [f"exc D {H:08X}", "raise D 80100040"],
             ["exception inside a handler", f"0x{H:08X}", "code 9"]),
            (["raise E 80100040"], ["0x80100040", "code 14"]),
            (place(H, ret(1)) + [f"exc D {H:08X}", "raise D 80100040"],
             ["0x80100040", "code 13"]),
            (["raise 9 80100040"], ["0x80100040", "code 9"]),
            (["break 80100000 409", "pc 80100050", "fptrap 8000"],
             ["0x80100050", "(the dividing instruction)", "0x8000"]),
            (place(H, ret(0)) + [f"fpe 3 {H:08X}", "break 80100000 409", "pc 80100050",
                                 "fptrap 8000"],
             ["0x80100050", "(the dividing instruction)", "0x8000"]),
            (["badtarget 9FC00000"], ["reset vector", "0x9FC00000"]),
            (["badtarget 10000000"], ["0x10000000"]),
            (place(MAIN, LOOP) + place(H, moves) + [f"line 0 {H:08X}", f"vblank {P}"]
             + run_main(P // NS_PER_CHECK + 50),
             [f"0x{LOOP_EPC:08X}", f"0x{LOOP_EPC + 8:08X}"]),
            (place(MAIN, LOOP) + place(H, ret(1)) + [f"line 2 {H:08X}", "irqraise 4"]
             + run_main(10), ["IP4"]),
            # the interrupted pc is named: the frame's resume pc
            (["irqraise 4", "raise 0 80100040"], ["IP4", "0x80100040"]),
            (["cause 100", "raise 0 80100040"], ["IP0", "0x80100040"]),
            (["cause 200", "raise 0 80100040"], ["IP1", "0x80100040"]),
            (["cause 8000", "raise 0 80100040"], ["IP7", "0x80100040"]),
            (["irqraise 7"], ["IP7"]),
            (["irqraise 1"], ["IP1"]),
            ([f"event {i:X} 1000" for i in range(65)], ["events"]),
            # before the runtime was given RAM
            (["fptrap 8000"], ["0x8000", "(the dividing instruction)", "given RAM"]),
            (["slice 5", "checks 0"], ["slice"]),
            ([f"hook 5 {H:08X}"], ["break hook 5"]),
        ]
        for cmds, needles in cases:
            with self.subTest(cmds=cmds[-1], needles=needles):
                self.stops(cmds, *needles)

    def test_at_stop_order(self):
        _out, err = self.stops(["atstop", "break 80100024 407"], "0x80100024")
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("carnevil: stop: "), err)
        self.assertEqual(lines[1:], ["at_stop 1", "at_stop 2"])

    def test_stop_hook_gets_the_message(self):
        """rt_set_stop_hook: the hook gets the status and the message rt_stop prints, after
        the message and before the at-stop functions."""
        _out, err = self.stops(["stophook", "atstop", "break 80100024 407"], "0x80100024")
        lines = err.splitlines()
        message = lines[0][len("carnevil: stop: "):]
        self.assertEqual(lines[1:], [f"stop_hook 3 {message}", "at_stop 1", "at_stop 2"])
        # machine_reset forgets the hook
        _out, err = self.stops(["stophook", "reset", "break 80100024 407"], "0x80100024")
        self.assertEqual(len(err.splitlines()), 1, err)

    def test_exit_runs_the_at_stop_functions(self):
        """rt_exit: the at-stop functions in order, then the status, with no stop message and
        no stop hook call."""
        res = subprocess.run([self.exe], input="reset\nstophook\natstop\nexit 0\n",
                             capture_output=True, text=True, timeout=120)
        self.assertEqual((res.returncode, res.stderr), (0, "at_stop 1\nat_stop 2\n"))
        res = subprocess.run([self.exe], input="reset\nexit 5\n", capture_output=True,
                             text=True, timeout=120)
        self.assertEqual((res.returncode, res.stderr), (5, ""))

    def test_failed_at_stop_function_does_not_skip_the_rest(self):
        """A stop inside an at-stop function (a failed NVRAM save, final review M10) is
        reported, the remaining at-stop functions still run, and the program exits with the
        stop status, whatever ended the run; the stop hook hears every stop."""
        fail = "carnevil: stop: at_stop fail: the save failed"
        res = subprocess.run([self.exe], input="reset\natfail\natstop\nexit 0\n",
                             capture_output=True, text=True, timeout=120)
        self.assertEqual((res.returncode, res.stderr.splitlines()),
                         (3, ["at_stop fail", fail, "at_stop 1", "at_stop 2"]))
        res = subprocess.run([self.exe], input="reset\nstophook\natfail\natstop\n"
                             "break 80100024 407\n", capture_output=True, text=True,
                             timeout=120)
        lines = res.stderr.splitlines()
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertTrue(lines[0].startswith("carnevil: stop: break 0x407"), lines)
        message = lines[0][len("carnevil: stop: "):]
        self.assertEqual(lines[1:], [f"stop_hook 3 {message}", "at_stop fail", fail,
                                     "stop_hook 3 at_stop fail: the save failed",
                                     "at_stop 1", "at_stop 2"])

    def test_signal_ends_the_run_at_the_next_check_point(self):
        """rt_catch_signals (final review I1): SIGTERM, SIGINT and SIGHUP only set a flag; the
        next rt_tick calls the end function, which ends the run through rt_exit with status
        130 (the at-stop functions run once); with no tick, nothing happens."""
        for sig in (15, 2, 1):
            with self.subTest(sig=sig):
                res = subprocess.run([self.exe], input=f"reset\natstop\nsignals\nkill {sig}\n"
                                     "state\ntick\nstate\n", capture_output=True, text=True,
                                     timeout=120)
                self.assertEqual(res.returncode, 130, res.stderr)
                self.assertEqual(res.stderr.splitlines(),
                                 [f"interrupted {sig}", "at_stop 1", "at_stop 2"])
                out = Out(res.stdout)
                self.assertEqual(out.all("killed"), [[]])
                self.assertEqual(len(out.all("m")), 1)  # the state after the tick never came
        # without a tick the flag is only set
        res = subprocess.run([self.exe], input="reset\natstop\nsignals\nkill 15\nstate\n",
                             capture_output=True, text=True, timeout=120)
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_vblank_hook_counts_raised_vblanks(self):
        """irq_set_vblank_hook: called at each vblank_raise that raises IP6 (the VBLANK
        enable set by line 0's handler), not at a retrace with the enable clear."""
        handler = asm(ret(1))
        out, _ = self.drive(place(MAIN, LOOP) + place(H, handler) + ["vbhook", f"vblank {P}"]
                            + run_main(2 * P // NS_PER_CHECK + 50) + [f"line 0 {H:08X}"]
                            + run_main(3 * P // NS_PER_CHECK))
        # the first two retraces (at 0.1 and 0.2 ms) come before line 0 is installed, the
        # next three (0.3 to 0.5 ms) after it
        hooks = [int(t[0]) for t in out.all("vblank_hook")]
        self.assertEqual(len(hooks), 3, out.lines)
        for k, t in enumerate(hooks):
            self.assertGreaterEqual(t, (k + 3) * P)
            self.assertLess(t, (k + 3) * P + 10 * NS_PER_CHECK)


# Recompiled guest code (final review I3): a loop whose back edge targets an address other
# than the function's own, and an FP divide that traps, at addresses the generated C knows.
GEN = 0x80120000
GEN_LOOP = GEN + 4
GEN_DIV = GEN + 0x40
GEN_DIV_AT = GEN_DIV + 4


def generated_program():
    """(the words from GEN, the C of both functions with extra_lookup)."""
    from carnevil_tools import exe, functions, recomp
    import struct
    loop = asm(A("ori", rt=T1, rs=0, imm=1), "loop", A("addiu", rt=T0, rs=T0, imm=-1),
               ("bne", T0, 0, "loop"), NOP, A("jr", rs=RA), NOP)
    div = [NOP, A("div.s", fd=0, fs=2, ft=4), A("jr", rs=RA), NOP]
    words = loop + [0] * ((GEN_DIV - GEN) // 4 - len(loop)) + div
    image = exe.ExeImage(0, GEN, struct.pack("<%dI" % len(words), *words), "")
    analysis = functions.analyse(image, GEN, manual=[GEN_DIV])
    fns = sorted(analysis.functions, key=lambda f: f.addr)
    assert [f.addr for f in fns] == [GEN, GEN_DIV], [hex(f.addr) for f in fns]
    text = '#include "recomp.h"\n\n' + "\n".join(
        recomp.function_c(image, analysis, f) for f in fns)
    text += ("\nrecomp_fn extra_lookup(uint32_t addr);\n"
             "recomp_fn extra_lookup(uint32_t addr)\n{\n"
             f"    return addr == 0x{GEN:08X}u ? {recomp.c_name(GEN)} : addr == 0x{GEN_DIV:08X}u "
             f"? {recomp.c_name(GEN_DIV)} : NULL;\n}}\n")
    return words, text


class CheckPointPcTest(unittest.TestCase):
    """The resume pc and EPC an interrupt frame records, and the pc an FP trap raises with,
    are the same in generated C ("call") and in the interpreter ("run"): the address the
    interrupted check point goes on at, and the dividing instruction's address."""

    @classmethod
    def setUpClass(cls):
        cls.words, generated = generated_program()
        cls.exe = build_driver(generated)

    drive = ClockIrqTest.drive

    def both(self, cmds_for):
        """The driver's output for cmds_for(entry_command) in each mode."""
        return {mode: self.drive(place(GEN, self.words) + cmds_for(mode))[0]
                for mode in ("run", "call")}

    def test_interrupt_at_a_function_entry(self):
        # IP6 pending and deliverable with one check left: the entry check delivers it
        handler = asm(call_probe(1), ret(1))
        for mode, out in self.both(lambda m: place(H, handler) + [
                f"line 0 {H:08X}", "irqraise 6", "reload", f"r {T0:X} 3",
                f"{m} {GEN:08X}"]).items():
            probes = out.probes()
            self.assertEqual(len(probes), 1, mode)
            self.assertEqual((probes[0]["resume"], probes[0]["epc"]), (GEN, GEN), mode)
            self.assertEqual(out.regs()[T0], 0, mode)

    def test_interrupt_at_a_backward_branch(self):
        # VBLANKs every P from time 0: the entry check is long past, each lands on the loop's
        # back edge, whose target is GEN + 4
        handler = asm(call_probe(1), ret(1))
        for mode, out in self.both(lambda m: place(H, handler) + [
                f"line 0 {H:08X}", f"vblank {P}", f"r {T0:X} {5 * P // (2 * NS_PER_CHECK):X}",
                f"{m} {GEN:08X}"]).items():
            probes = out.probes()
            self.assertEqual(len(probes), 2, mode)
            for p in probes:
                self.assertEqual((p["resume"], p["epc"]), (GEN_LOOP, GEN_LOOP), mode)

    def test_fp_trap_names_the_dividing_instruction(self):
        # the FPU handler for divide by zero sees the frame of the trap: EPC and the resume pc
        # are the div.s (GEN_DIV + 4), which it steps past
        skip = asm(call_probe(7), A("lw", rt=T0, base=A0, imm=0x110),
                   A("addiu", rt=T0, rs=T0, imm=4), A("sw", rt=T0, base=A0, imm=0x110), ret(9))
        for mode, out in self.both(lambda m: place(H, skip) + [
                f"fpe 3 {H:08X}", "fpr 2 3F800000", "fpr 4 0", "fcsr 400",
                "break 80100000 409",
                f"{m} {GEN_DIV:08X}"]).items():
            [p] = out.probes()
            self.assertEqual((p["resume"], p["epc"]), (GEN_DIV_AT, GEN_DIV_AT), mode)
            self.assertEqual(out.all("ran"), [[]], mode)


if __name__ == "__main__":
    unittest.main()
