"""runtime/core/tasks.c: the host contexts of runtime/core/tasks.h and the game glue
rt_host_switch that runs the game's cooperative tasks on them (docs/analysis/task-switch.md,
"The mechanism (read)").

The C runs in tests/harness/tasks_driver.c (its top comment lists the scenarios), built with
the global C flags, -Wall -Wextra -Werror, -pthread and the sanitizer flags, into a cache
under the system temp dir; `test_under_thread_sanitizer` builds it again with
-fsanitize=thread. The guest scheduler of `test_guest_scheduler` is assembled here, emitted as
C by carnevil_tools.recomp and run both recompiled and in the interpreter. No game data is
needed. Every run has a timeout, so that a hand-over deadlock fails the test instead of
hanging the suite.

`test_first_tasks_match_mame`, the comparison of the game's first tasks with MAME, needs game
data and is built in a later task.
"""
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from carnevil_tools import exe, functions, recomp, vectors
from tests import charness

REPO = charness.REPO
CORE = os.path.join(REPO, "runtime", "core")
RUNTIME = [os.path.join(CORE, f) for f in ("call.c", "fpu.c", "clock.c", "irq.c", "exc.c",
                                           "tasks.c")] + \
          [os.path.join(REPO, "runtime", "interp", "interp.c")]
HARNESS_DIR = os.path.join(REPO, "tests", "harness")
DRIVER = os.path.join(HARNESS_DIR, "tasks_driver.c")
HEADERS = [os.path.join(charness.INCLUDE, h) for h in ("recomp.h", "machine.h")] + \
          [os.path.join(CORE, "tasks.h"), os.path.join(HARNESS_DIR, "harness.h")]
CACHE = os.path.join(tempfile.gettempdir(), "carnevil-tasks-driver")
PTHREAD = ("-pthread",)
TSAN = ("-fsanitize=thread",)
TIMEOUT = 20  # seconds; a run that takes longer has deadlocked
STOP_STATUS = 3

A = vectors.assemble
NOP = 0
T0, T1, T2, T3, S0, S1, K1, SP, RA = 8, 9, 10, 11, 16, 17, 27, 29, 31

# The glue's guest addresses (docs/analysis/task-switch.md).
TRAMPOLINE = 0x8014A930
CURRENT_TASK = 0x801A3118
# The synthetic guest's records and stacks. A record: +0 next (0 ends the list), +8 saved
# sp, +0xC alive, +0x10 counter, +0x14 the count at which the task returns (0: never).
REC_A, REC_B, REC_C = 0x80180000, 0x80180040, 0x80180080
SCHED_STACK = 0x80200000
STACK_A, STACK_B, STACK_C = 0x80210000, 0x80220000, 0x80230000
FLAGS = 0x80190000  # +0: the VBLANK handler has linked REC_C; +4 INSERT: it may
PROBE = 0x80300000  # tasks_driver.c's guest-mode probe
A2, V0 = 6, 2
PASSES = 5
RZ_THIRD, RN_THIRD = 0x3EAAAAAA, 0x3EAAAAAB  # 1.0f / 3.0f toward zero and to nearest


def _key(cc, flags, sources):
    h = hashlib.sha256()
    st = os.stat(os.path.realpath(cc))
    h.update(f"{os.path.realpath(cc)}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    h.update("\0".join(flags).encode())
    for path in RUNTIME + [DRIVER] + HEADERS + list(sources):
        with open(path, "rb") as f:
            h.update(path.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return h.hexdigest()[:32]


def build_driver(generated=(), sanitize=charness.SANITIZE):
    """Path of the tasks driver binary, linked with the C files `generated` (built with the
    generated-code warnings) and the sanitizer flags `sanitize`. Raises unittest.SkipTest
    without `cc`."""
    cc = charness._cc()
    base = charness.CFLAGS + tuple(sanitize) + PTHREAD
    includes = ("-I", charness.INCLUDE, "-I", CORE, "-I", HARNESS_DIR)
    out = os.path.join(CACHE, _key(cc, base, generated))
    path = os.path.join(out, "tasks_driver")
    if os.path.exists(path):
        return path
    os.makedirs(CACHE, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=CACHE, prefix="build-")
    try:
        objs = []
        units = [(s, charness.RUNTIME_WARNINGS) for s in RUNTIME + [DRIVER]] + \
                [(s, charness.GENERATED_WARNINGS) for s in generated]
        for i, (src, warnings) in enumerate(units):
            obj = os.path.join(tmp, f"{i}-{os.path.basename(src)}.o")
            charness._cc_run([cc, *base, *warnings, *includes, "-c", src, "-o", obj])
            objs.append(obj)
        charness._cc_run([cc, *sanitize, *PTHREAD, *objs, "-lm",
                          "-o", os.path.join(tmp, "tasks_driver")])
        try:
            os.rename(tmp, out)
        except OSError:  # another process built the same key first
            if not os.path.exists(path):
                raise
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    return path


def tsan_available():
    """None when the host's compiler builds and runs a -fsanitize=thread program, else why
    not."""
    cc = charness._cc()
    tmp = tempfile.mkdtemp(prefix="carnevil-tsan-probe-")
    try:
        src = os.path.join(tmp, "probe.c")
        with open(src, "w") as f:
            f.write("#include <pthread.h>\nstatic void *run(void *p) { return p; }\n"
                    "int main(void) { pthread_t t; void *r;\n"
                    "    if (pthread_create(&t, 0, run, 0)) return 1;\n"
                    "    return pthread_join(t, &r); }\n")
        probe = os.path.join(tmp, "probe")
        res = subprocess.run([cc, *TSAN, *PTHREAD, src, "-o", probe], capture_output=True,
                             text=True)
        if res.returncode != 0:
            return "the host's compiler cannot build with -fsanitize=thread: " + \
                (res.stderr.strip().splitlines() or ["no message"])[-1]
        res = subprocess.run([probe], capture_output=True, text=True, timeout=TIMEOUT)
        if res.returncode != 0:
            return "a -fsanitize=thread program does not run on this host: " + \
                (res.stderr.strip().splitlines() or ["no message"])[-1]
        return None
    finally:
        shutil.rmtree(tmp)


# --- the synthetic guest scheduler --------------------------------------------------------

def asm(base, *items):
    """(words, labels) of a program at base: ints are words, a str is a label, a tuple
    ("b", mnemonic, rs, rt, label) a branch to a label and ("j", mnemonic, label) a j or
    jal to one."""
    labels, slots = {}, []
    for item in items:
        if isinstance(item, str):
            labels[item] = base + 4 * len(slots)
        else:
            slots.append(item)
    words = []
    for i, item in enumerate(slots):
        if isinstance(item, tuple) and item[0] == "b":
            _, mnemonic, rs, rt, label = item
            item = A(mnemonic, rs=rs, rt=rt, imm=(labels[label] - (base + 4 * i) - 4) >> 2)
        elif isinstance(item, tuple):
            item = A(item[1], target=labels[item[2]])
        words.append(item)
    return words, labels


def swap_sp_k1():
    """The first two of the three xors that swap sp and k1; the third is a delay slot."""
    return [A("xor", rd=SP, rs=SP, rt=K1), A("xor", rd=K1, rs=SP, rt=K1)]


LAST_XOR = A("xor", rd=SP, rs=SP, rt=K1)


def guest_program(irq=False):
    """(words, labels) of the guest program at TRAMPOLINE, the shapes of
    docs/analysis/task-switch.md reduced to two tasks. With irq, the task loop also calls
    PROBE (tag 2) at each run, and there is a VBLANK handler `vbl` (below, irq_items).

    - `tramp`, at the trampoline's address: calls the task, then jumps to the exit.
    - `sched`: PASSES passes over the record list from REC_A; for each live record: the
      current task word = the record, the scheduler's s0 and s1 saved below its sp, the
      task's ra and s0 loaded from its saved sp, sp and k1 swapped, `jr ra` (resume); the
      restore path after it reloads s0 and s1.
    - `task`: its record from the current task word, then: counter += 1; returns when the
      counter reaches the record's limit, otherwise `jal yield` and again.
    - `yield`: saves ra and s0 below sp, stores sp at record + 8, swaps sp and k1, `j` to the
      restore path (yield).
    - `exit`: clears the record's alive word and, as the game's free routine does, makes the
      exiting task's predecessor in the list current (0 for the head), swaps, `j` to the
      restore path (exit). So the current task word no longer names the exiting task when the
      glue sees the exit."""
    items = [
        "tramp",
        ("j", "jal", "task"), NOP,
        ("j", "j", "exit"), NOP,
        "sched",
        A("addiu", rt=SP, rs=SP, imm=-8), A("sw", rt=RA, rs=SP, imm=0),
        A("ori", rt=S1, rs=0, imm=PASSES),
        "pass",
        A("lui", rt=S0, imm=REC_A >> 16),
        "walk",
        A("lw", rt=T0, rs=S0, imm=0xC),
        ("b", "beq", T0, 0, "next"), A("lui", rt=T1, imm=CURRENT_TASK >> 16),
        A("sw", rt=S0, rs=T1, imm=CURRENT_TASK & 0xFFFF),
        A("addiu", rt=SP, rs=SP, imm=-8), A("sw", rt=S0, rs=SP, imm=0),
        A("sw", rt=S1, rs=SP, imm=4),
        A("lw", rt=K1, rs=S0, imm=8), A("lw", rt=RA, rs=K1, imm=0),
        A("lw", rt=S0, rs=K1, imm=4), A("addiu", rt=K1, rs=K1, imm=8),
        *swap_sp_k1(),
        "resume",
        A("jr", rs=RA), LAST_XOR,
        "restore",
        A("lw", rt=S0, rs=SP, imm=0), A("lw", rt=S1, rs=SP, imm=4),
        A("addiu", rt=SP, rs=SP, imm=8),
        "next",
        A("lw", rt=S0, rs=S0, imm=0),
        ("b", "bne", S0, 0, "walk"), NOP,
        A("addiu", rt=S1, rs=S1, imm=-1),
        ("b", "bne", S1, 0, "pass"), NOP,
        A("lw", rt=RA, rs=SP, imm=0), A("jr", rs=RA), A("addiu", rt=SP, rs=SP, imm=8),
        "task",
        A("addiu", rt=SP, rs=SP, imm=-8), A("sw", rt=RA, rs=SP, imm=0),
        A("lui", rt=S0, imm=CURRENT_TASK >> 16),
        A("lw", rt=S0, rs=S0, imm=CURRENT_TASK & 0xFFFF),
        "loop",
        *([A("ori", rt=A2, rs=0, imm=2), A("jal", target=PROBE), NOP] if irq else []),
        A("lw", rt=T0, rs=S0, imm=0x10), A("addiu", rt=T0, rs=T0, imm=1),
        A("sw", rt=T0, rs=S0, imm=0x10), A("lw", rt=T1, rs=S0, imm=0x14),
        ("b", "beq", T0, T1, "done"), NOP,
        ("j", "jal", "yield"), NOP,
        ("b", "beq", 0, 0, "loop"), NOP,
        "done",
        A("lw", rt=RA, rs=SP, imm=0), A("jr", rs=RA), A("addiu", rt=SP, rs=SP, imm=8),
        "yield",
        A("addiu", rt=SP, rs=SP, imm=-8), A("sw", rt=RA, rs=SP, imm=0),
        A("sw", rt=S0, rs=SP, imm=4),
        A("lui", rt=T0, imm=CURRENT_TASK >> 16),
        A("lw", rt=T0, rs=T0, imm=CURRENT_TASK & 0xFFFF),
        A("sw", rt=SP, rs=T0, imm=8),
        *swap_sp_k1(),
        "yield_j",
        ("j", "j", "restore"), LAST_XOR,
        "exit",
        A("lui", rt=T0, imm=CURRENT_TASK >> 16),
        A("lw", rt=T0, rs=T0, imm=CURRENT_TASK & 0xFFFF),
        A("sw", rt=0, rs=T0, imm=0xC),
        # t2 = the predecessor of t0 in the list from REC_A (0 for the head)
        A("or", rd=T2, rs=0, rt=0), A("lui", rt=T1, imm=REC_A >> 16),
        "pred",
        ("b", "beq", T1, T0, "pred_found"), NOP,
        A("or", rd=T2, rs=T1, rt=0), A("lw", rt=T1, rs=T1, imm=0),
        ("b", "bne", T1, 0, "pred"), NOP,
        "pred_found",
        A("lui", rt=T3, imm=CURRENT_TASK >> 16),
        A("sw", rt=T2, rs=T3, imm=CURRENT_TASK & 0xFFFF),
        *swap_sp_k1(),
        "exit_j",
        ("j", "j", "restore"), LAST_XOR,
    ]
    if irq:
        items += irq_items()
    return asm(TRAMPOLINE, *items)


def irq_items():
    """The VBLANK handler `vbl`: PROBE (tag 1); the first time it runs with the word INSERT
    non-zero it links task record REC_C after REC_A (C.next = A.next, A.next = C), as the
    game's creation routine links a new record that the scheduler then first-runs through the
    trampoline, and calls PROBE (tag 3); it returns 1, so the loader's default work
    acknowledges the VBLANK."""
    return [
        "vbl",
        A("addiu", rt=SP, rs=SP, imm=-8), A("sw", rt=RA, rs=SP, imm=0),
        A("ori", rt=A2, rs=0, imm=1), A("jal", target=PROBE), NOP,
        A("lui", rt=T0, imm=FLAGS >> 16),
        A("lw", rt=T1, rs=T0, imm=4),            # INSERT: 0, nothing is linked
        ("b", "beq", T1, 0, "vbl_done"), NOP,
        A("lw", rt=T1, rs=T0, imm=0),            # done already
        ("b", "bne", T1, 0, "vbl_done"), NOP,
        A("ori", rt=T1, rs=0, imm=1), A("sw", rt=T1, rs=T0, imm=0),
        A("lui", rt=T2, imm=REC_A >> 16), A("lw", rt=T3, rs=T2, imm=0),
        A("lui", rt=T1, imm=REC_C >> 16), A("ori", rt=T1, rs=T1, imm=REC_C & 0xFFFF),
        A("sw", rt=T3, rs=T1, imm=0), A("sw", rt=T1, rs=T2, imm=0),
        A("ori", rt=A2, rs=0, imm=3), A("jal", target=PROBE), NOP,
        "vbl_done",
        A("lw", rt=RA, rs=SP, imm=0), A("addiu", rt=SP, rs=SP, imm=8),
        A("jr", rs=RA), A("ori", rt=V0, rs=0, imm=1),
    ]


def guest_records(irq=False):
    """RAM words of the two records and of the first frames on their stacks: saved sp is the
    stack top - 8, holding the trampoline as ra (as the creation routines plant it) and 0 as
    s0. Task A never returns; task B returns at its third run. With irq, also record REC_C
    (unlinked until the VBLANK handler links it; it returns at its second run)."""
    mem = {}
    recs = [(REC_A, REC_B, STACK_A, 0), (REC_B, 0, STACK_B, 3)]
    if irq:
        recs.append((REC_C, 0, STACK_C, 2))
    for rec, nxt, stack, limit in recs:
        mem.update({rec: nxt, rec + 8: stack - 8, rec + 0xC: 1, rec + 0x10: 0,
                    rec + 0x14: limit, stack - 8: TRAMPOLINE, stack - 4: 0})
    return mem


def guest_c(words, labels):
    """The generated C of the guest program, with a constructor registering every function
    and host-switch point with the driver; returns (path, analysis)."""
    image = exe.ExeImage(0, TRAMPOLINE, struct.pack("<%dI" % len(words), *words), "")
    switches = {labels["resume"]: "resume", labels["yield_j"]: "yield",
                labels["exit_j"]: "exit"}
    # the trampoline is entered only through the ra a record holds, the exit only through the
    # trampoline's tail jump: both are named starts, as config/game.manual.toml names them
    # (and so is the VBLANK handler, entered only through the dispatcher)
    manual = [TRAMPOLINE, labels["exit"]] + ([labels["vbl"]] if "vbl" in labels else [])
    analysis = functions.analyse(image, labels["sched"], manual=manual,
                                 host_switches=switches)
    fns = sorted(analysis.functions, key=lambda f: f.addr)
    source = "".join(recomp.function_c(image, analysis, f) + "\n" for f in fns)
    source += "static void __attribute__((constructor)) register_all(void)\n{\n"
    source += "".join(f"    harness_register(0x{f.addr:08X}u, {recomp.c_name(f.addr)});\n"
                      for f in fns)
    source += "".join(f"    harness_register_switch(0x{a:08X}u, {recomp.SWITCH_KINDS[k]});\n"
                      for a, k in sorted(switches.items()))
    source += "}\n"
    return charness.write_generated(source), analysis


class TasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_driver()

    def drive(self, args, status=0, stdin="", exe=None):
        """(stdout, stderr) of one driver run; fails on another exit status, on stderr output
        from a run that should succeed, or when the run takes over TIMEOUT seconds."""
        try:
            res = subprocess.run([exe or self.exe, *args], input=stdin, capture_output=True,
                                 text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            self.fail(f"tasks_driver {' '.join(args)}: no exit within {TIMEOUT} s (a hang)")
        if res.returncode != status:
            self.fail(f"tasks_driver {' '.join(args)}: status {res.returncode}, want {status}: "
                      f"{res.stderr}")
        if status == 0 and res.stderr:
            self.fail(f"tasks_driver {' '.join(args)} wrote to stderr: {res.stderr}")
        return res.stdout, res.stderr

    # --- the generic contexts ------------------------------------------------------------

    def check_ping_pong(self, exe=None):
        out, _ = self.drive(["ping"], exe=exe)
        # 1,000 parks of each context, and the switch that ends each
        self.assertEqual(out, "ping switches 3003 violations 0 parks 1000 1000 1000 "
                              "ended 1 1 1 roles 1 0\n")

    def test_ping_pong(self):
        """Three contexts each park 1,000 times and end (one by returning from its entry);
        every switch runs the context the scheduler chose and no other, and
        ctx_is_scheduler is true only on the scheduler's thread."""
        self.check_ping_pong()

    def check_retire_parked(self, exe=None):
        out, _ = self.drive(["retire"], exe=exe)
        # context 0 ran twice and was retired while parked: it never got past that park
        self.assertEqual(out, "retire before 2 3 3 after 1 2 2\n")

    def test_retire_parked(self):
        """A parked context is retired without its entry running again, and the process exits
        with status 0 while two other contexts are still parked."""
        self.check_retire_parked()

    def check_stop_from_task(self, exe=None):
        out, err = self.drive(["stop"], status=STOP_STATUS, exe=exe)
        self.assertEqual(out, "")
        self.assertEqual(err, "carnevil: stop: stop from a task context\nat_stop ran\n")

    def test_stop_from_task(self):
        """rt_stop on a task context, with two others parked, stops the process promptly with
        status 3 and its message."""
        self.check_stop_from_task()

    # --- the game glue -------------------------------------------------------------------

    def check_fcsr_follows_cpu(self, exe=None):
        out, _ = self.drive(["fcsr"], exe=exe)
        self.assertEqual(out.splitlines(), [
            f"third scheduler {RZ_THIRD:08X}",  # A set toward zero and yielded
            f"third B {RZ_THIRD:08X}",          # a new thread, under the shared cpu_t's mode
            f"third scheduler {RN_THIRD:08X}",  # B set nearest and yielded
            f"third A {RN_THIRD:08X}",          # A's thread had set toward zero itself
            f"third scheduler {RN_THIRD:08X}"])

    def test_fcsr_follows_cpu(self):
        """The host rounding mode follows the shared cpu_t's FCSR across every switch, on the
        scheduler's side and on the task's, for a new context and a resumed one."""
        self.check_fcsr_follows_cpu()

    def check_misuse_stops(self, exe=None):
        cases = [
            ("yield", r"^carnevil: stop: rt_host_switch: yield on the scheduler's context "
                      r"\(record 0x80180000, pc as last stored 0x80151B48"),
            ("exit", r"^carnevil: stop: rt_host_switch: exit on the scheduler's context "
                     r"\(record 0x80180000, pc as last stored 0x80151B78"),
            ("exl", r"^carnevil: stop: rt_host_switch: resume inside an exception or interrupt "
                    r"handler \(Status EXL set\) \(record 0x80180000, pc as last stored "
                    r"0x80151E88"),
            ("resume-in-task", r"^carnevil: stop: rt_host_switch: resume on a task's context "
                               r"\(record 0x80180040, pc as last stored 0x80151E88"),
            ("no-context", r"^carnevil: stop: rt_host_switch: resume of a record with no "
                           r"context, and ra 0x80100000 is not the trampoline 0x8014A930 "
                           r"\(record 0x80180000, pc as last stored 0x80151E88"),
            ("full", r"^carnevil: stop: rt_host_switch: resume needs a 65th task context, "
                     r"more than the 64 the runtime keeps \(record 0x80181000, pc as last "
                     r"stored 0x80151E88"),
            ("reset", r"^carnevil: stop: rt_host_switch: resume of a record with no "
                      r"context, and ra 0x80100000 is not the trampoline 0x8014A930 "
                      r"\(record 0x80180000, pc as last stored 0x80151E88"),
            ("exit-unmapped", r"^carnevil: stop: rt_host_switch: exit on a task context the "
                              r"glue holds no record for \(record 0x80180000, pc as last "
                              r"stored 0x80151B78"),
        ]
        for kind, message in cases:
            with self.subTest(kind=kind):
                out, err = self.drive(["misuse", kind], status=STOP_STATUS, exe=exe)
                self.assertRegex(err, message)
                self.assertIn("exact only in a --trace-pc build", err)
                self.assertNotIn("returned", out)
                if kind == "full":
                    self.assertEqual(out.split()[-1], "64")

    def test_misuse_stops(self):
        """A YIELD on the scheduler's context stops with a message naming the kind, the record
        and the pc; so does every other misuse the glue refuses."""
        self.check_misuse_stops()

    def check_record_reuse(self, exe=None):
        out, _ = self.drive(["reuse"], exe=exe)
        self.assertEqual(out.splitlines(), [
            "run gen 1", "live 1",
            "run gen 2", "live 1",       # the first context was retired, not resumed
            "resumed gen 2", "live 1"])  # the normal resume runs the new context

    def test_record_reuse(self):
        """The game frees a suspended task and reuses its record: a resume at the trampoline
        while the record still holds a parked context retires that context (it never runs
        again), runs the new one, and leaves one live context."""
        self.check_record_reuse()

    # --- the guest scheduler -------------------------------------------------------------

    def check_guest_scheduler(self, sanitize=charness.SANITIZE):
        words, labels = guest_program()
        path, analysis = guest_c(words, labels)
        self.assertIn(TRAMPOLINE, {f.addr for f in analysis.functions})
        exe = build_driver([path], sanitize)
        load = [f"mem {TRAMPOLINE + 4 * i:08X} {w:08X}" for i, w in enumerate(words)]
        load += [f"mem {a:08X} {v:08X}" for a, v in sorted(guest_records().items())]
        load += [f"r {SP:X} {SCHED_STACK:08X}", f"r {RA:X} 80001000",
                 f"run {labels['sched']:08X}",
                 f"dump {REC_A:08X} 6", f"dump {REC_B:08X} 6"]
        # twice in one process: tasks_reset leaves nothing of the first run behind
        stdin = "\n".join(["reset"] + load + ["reset"] + load) + "\n"
        want_a = [REC_B, None, 1, PASSES, 0]  # next, (saved sp), alive, counter, limit
        want_b = [0, None, 0, 3, 3]           # exited at its third run
        for mode in ("recomp", "interp"):
            with self.subTest(mode=mode):
                out, _ = self.drive(["guest", mode], stdin=stdin, exe=exe)
                lines = [line.split() for line in out.splitlines()]
                self.assertEqual([line for line in lines if line[0] == "unrecompiled"], [])
                self.assertEqual([line[0] for line in lines], ["ran", "dump", "dump"] * 2)
                for dump_a, dump_b in ((lines[1], lines[2]), (lines[4], lines[5])):
                    a = [int(x, 16) for x in dump_a[1:]]
                    b = [int(x, 16) for x in dump_b[1:]]
                    self.assertEqual([a[0], a[3], a[4], a[5]], [want_a[0], *want_a[2:]])
                    self.assertEqual([b[0], b[3], b[4], b[5]], [want_b[0], *want_b[2:]])
                    # each task's last saved sp is its frame below the stack top
                    self.assertEqual(a[2], STACK_A - 16)

    def test_switch_hook(self):
        """tasks_set_switch_hook: the hook sees every host switch of the guest scheduler, its
        kind and the current task word, before the switch, and changes nothing: the records
        end as test_guest_scheduler's do. Every resume of a task ends in a yield of it or,
        once, B's exit."""
        words, labels = guest_program()
        path, _ = guest_c(words, labels)
        exe = build_driver([path])
        load = [f"mem {TRAMPOLINE + 4 * i:08X} {w:08X}" for i, w in enumerate(words)]
        load += [f"mem {a:08X} {v:08X}" for a, v in sorted(guest_records().items())]
        load += ["switchhook", f"r {SP:X} {SCHED_STACK:08X}", f"r {RA:X} 80001000",
                 f"run {labels['sched']:08X}", f"dump {REC_A:08X} 6", f"dump {REC_B:08X} 6"]
        out, _ = self.drive(["guest", "recomp"], stdin="\n".join(["reset"] + load) + "\n",
                            exe=exe)
        lines = [line.split() for line in out.splitlines()]
        switches = [(int(k), int(r, 16)) for key, k, r in
                    (line for line in lines if line[0] == "switch")]
        resume, yield_, exit_ = 0, 1, 2

        def count(kind, rec):
            return switches.count((kind, rec))
        # A never exits; B ends in the exit, which sees the exiting task's predecessor (A) as
        # the current task, as the game's free leaves it
        self.assertEqual(count(resume, REC_A), count(yield_, REC_A))
        self.assertEqual(count(resume, REC_B), count(yield_, REC_B) + 1)
        self.assertEqual([k for k, _ in switches].count(exit_), 1)
        self.assertIn((exit_, REC_A), switches)
        dumps = [[int(x, 16) for x in line[1:]] for line in lines if line[0] == "dump"]
        self.assertEqual([dumps[0][i] for i in (0, 3, 4, 5)], [REC_B, 1, PASSES, 0])
        self.assertEqual([dumps[1][i] for i in (0, 3, 4, 5)], [0, 0, 3, 3])

    # --- interrupts and task switches (final review I4) ----------------------------------

    def run_irq_guest(self, extra, insert):
        """The irq variant of the guest scheduler run in both modes with `extra` commands
        before the run (insert: whether the VBLANK handler links REC_C); {mode: (parsed
        probes, every line split, the dumps of REC_A, REC_B and REC_C)}."""
        words, labels = guest_program(irq=True)
        path, _ = guest_c(words, labels)
        exe = build_driver([path])
        load = [f"mem {TRAMPOLINE + 4 * i:08X} {w:08X}" for i, w in enumerate(words)]
        load += [f"mem {a:08X} {v:08X}" for a, v in sorted(guest_records(irq=True).items())]
        load += [f"mem {FLAGS + 4:08X} {1 if insert else 0:08X}",
                 f"line 0 {labels['vbl']:08X}"] + extra
        load += [f"r {SP:X} {SCHED_STACK:08X}", f"r {RA:X} 80001000",
                 f"run {labels['sched']:08X}",
                 f"dump {REC_A:08X} 6", f"dump {REC_B:08X} 6", f"dump {REC_C:08X} 6"]
        got = {}
        for mode in ("recomp", "interp"):
            out, _ = self.drive(["guest", mode], stdin="\n".join(["reset"] + load) + "\n",
                                exe=exe)
            lines = [line.split() for line in out.splitlines()]
            self.assertEqual([line for line in lines if line[0] == "unrecompiled"], [], mode)
            probes = [dict(tag=int(p[1]), thread=int(p[2]), sched=int(p[3]), exl=int(p[4]),
                           record=int(p[5], 16)) for p in lines if p[0] == "probe"]
            dumps = [[int(x, 16) for x in line[1:]] for line in lines if line[0] == "dump"]
            got[mode] = (probes, lines, dumps)
        return got

    def test_task_created_in_vblank_handler(self):
        """A task created from inside the VBLANK handler: VBLANKs every 400 ns across the whole
        run; the handler links a new record (REC_C) once, which the guest scheduler then
        first-runs through the trampoline. The handler runs at the exception level each time
        and returns normally (no switch can happen while EXL is set: rt_host_switch would
        stop); the new task runs on a context of its own, at task level, twice, then exits;
        the other tasks end as without interrupts. In emitted C and in the interpreter."""
        for mode, (probes, _lines, dumps) in self.run_irq_guest(["vblank 190"], True).items():
            with self.subTest(mode=mode):
                handler = [p for p in probes if p["tag"] == 1]
                inserted = [p for p in probes if p["tag"] == 3]
                runs = [p for p in probes if p["tag"] == 2]
                self.assertGreaterEqual(len(handler), 3)
                self.assertEqual({p["exl"] for p in handler + inserted}, {1})
                self.assertEqual(len(inserted), 1)
                self.assertEqual({p["exl"] for p in runs}, {0})
                self.assertEqual({p["sched"] for p in runs}, {0})
                threads = {rec: {p["thread"] for p in runs if p["record"] == rec}
                           for rec in (REC_A, REC_B, REC_C)}
                self.assertEqual([len(threads[r]) for r in (REC_A, REC_B, REC_C)], [1, 1, 1])
                self.assertEqual(len(threads[REC_A] | threads[REC_B] | threads[REC_C]), 3)
                self.assertEqual(len([p for p in runs if p["record"] == REC_C]), 2)
                a, b, c = dumps
                self.assertEqual([a[0], a[3], a[4], a[5]], [REC_C, 1, PASSES, 0])
                self.assertEqual([b[0], b[3], b[4], b[5]], [0, 0, 3, 3])
                self.assertEqual([c[0], c[3], c[4], c[5]], [REC_B, 0, 2, 2])

    def test_interrupt_due_at_a_host_switch(self):
        """An interrupt that falls due exactly at a host switch (IP6 raised by the switch
        hook, at the start of rt_host_switch, with the slice reloaded so the next check point
        delivers it): it is delivered once, on the context that runs after the switch has
        completed (the resumed task's after a resume, first runs and parked contexts alike;
        the scheduler's after a yield or an exit), never during the switch, and the tasks
        end as without the interrupt. In emitted C and in the interpreter."""
        resume, yield_, exit_ = 0, 1, 2
        for kind, n in ((resume, 1), (resume, 2), (resume, 4), (yield_, 1), (yield_, 3),
                        (exit_, 1)):
            for mode, (probes, lines, dumps) in self.run_irq_guest(
                    [f"raiseat {kind:X} {n:X}"], False).items():
                with self.subTest(kind=kind, n=n, mode=mode):
                    raised = [i for i, line in enumerate(lines) if line[0] == "raised"]
                    self.assertEqual(len(raised), 1)
                    record = int(lines[raised[0]][2], 16)
                    handler = [i for i, line in enumerate(lines)
                               if line[0] == "probe" and line[1] == "1"]
                    self.assertEqual(len(handler), 1)
                    self.assertGreater(handler[0], raised[0])
                    [h] = [p for p in probes if p["tag"] == 1]
                    self.assertEqual(h["exl"], 1)
                    if kind == resume:
                        task_threads = {p["thread"] for p in probes
                                        if p["tag"] == 2 and p["record"] == record}
                        self.assertEqual((h["sched"], h["record"]), (0, record))
                        self.assertEqual({h["thread"]}, task_threads)
                    else:
                        self.assertEqual(h["sched"], 1)
                    a, b, c = dumps
                    self.assertEqual([a[0], a[3], a[4], a[5]], [REC_B, 1, PASSES, 0])
                    self.assertEqual([b[0], b[3], b[4], b[5]], [0, 0, 3, 3])
                    self.assertEqual([c[0], c[3], c[4], c[5]], [0, 1, 0, 2])  # never linked

    def test_guest_scheduler(self):
        """A synthetic guest scheduler with the three host-switch shapes (a scheduler loop
        resuming two task records by `jr ra`, tasks that yield through a `j`, one that exits)
        and its current task word at 0x801A3118: each task's counter reaches the expected
        value, in emitted C and in the interpreter."""
        self.check_guest_scheduler()

    # --- the thread sanitizer ------------------------------------------------------------

    def test_under_thread_sanitizer(self):
        """Every scenario above again, built with -fsanitize=thread instead of the undefined
        behaviour sanitizer (a report makes a run that should succeed write to stderr, and a
        stop's stderr is compared whole). Skipped, saying so, where the host's compiler cannot
        build or run it."""
        why = tsan_available()
        if why:
            self.skipTest(why)
        exe = build_driver(sanitize=TSAN)
        self.check_ping_pong(exe)
        self.check_retire_parked(exe)
        self.check_stop_from_task(exe)
        self.check_fcsr_follows_cpu(exe)
        self.check_misuse_stops(exe)
        self.check_record_reuse(exe)
        self.check_guest_scheduler(TSAN)


if __name__ == "__main__":
    unittest.main()
