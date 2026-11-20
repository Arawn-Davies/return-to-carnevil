"""The `carnevil` program (runtime/main/carnevil.c) and the boot to attract mode.

Two groups of tests:

- MainTest needs no game data: a synthetic GAME.EXE (a console write, then a loop) is
  recompiled into a temporary folder, `make carnevil` builds the program from it, and a
  synthetic disk image holding that EXE (a sparse file of the supported image's size) runs it.
  It checks the options, the file errors, the hash refusal, the end of a timed run, the
  trace's format and the default overlay and NVRAM paths.
- BootTest needs the share: GAME.EXE recompiled into a temporary folder, the program built
  from it, and the user's raw image run read-only, with the overlay, the NVRAM file and the
  trace in a temporary folder. One run of --seconds 150 is shared by the tests that compare
  it with MAME's measured facts (mame/m2-facts on the share, described in
  tools/mame/README.md, "M2 facts"). The comparisons are of order and values, never of times:
  MAME's timing shifts under its debugger, the runtime's clock counts check points, and its
  sound-board responder is never slower than the board.

The trace has one line per event, "TIME KIND FIELDS...", TIME the virtual time in ns since
GAME.EXE's entry (carnevil.c's top comment lists the kinds).
"""
import collections
import dataclasses
import hashlib
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import tomllib
import unittest

from carnevil_tools import exe, functions, recomp, vectors
from tests import charness
from tests.gamedata import requires_file, work_path
from tests.test_disk_c import fs_image, fs_partition
from tests.test_recomp import _make, write_exe

REPO = charness.REPO
EXPECTED = os.path.join(REPO, "config", "runtime.expected.toml")
FUNCTIONS = os.path.join(REPO, "config", "game.functions.toml")
FACTS = ("mame", "m2-facts")
DISK_BYTES = 4_310_046_720  # the supported disk: 0x0080730C sectors (loader-services.md, s. 7)
ENTRY = 0x800C4000
STUB, STUB_END = 0x800C408C, 0x800C42CC
SUPPORTED_SHA256 = "f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6"
ATTRACT = 0x800CB084  # the `attract` task's function (task-switch.md, section 1)
RUN_SECONDS, SHORT_SECONDS = 150, 20
RUN_TIMEOUT, SHORT_TIMEOUT = 3600, 1200
EXCEPTION_STACK = (0x800C0000, 0x800C1400)  # the loader's exception stack (loader-calls.md)
A = vectors.assemble
A0, A1, A2 = 4, 5, 6

# Per slot: the indexes of the arguments (a0 = 0 .. a3 = 3) its signature has, and whether
# it returns a value (loader-services.md section 1). Unused arguments and the fifth
# argument on the stack (slot 0x110) are left out; so is v0 for the slots section 1 gives no
# return value for (it is the caller's leftover).
SIGNATURES = {
    0x018: ((0,), True), 0x028: ((0, 1, 2), True), 0x038: ((0, 1, 2), True),
    0x088: ((0, 1), True), 0x090: ((0, 1, 2), True), 0x0C0: ((0, 1, 2), True),
    0x0C8: ((0, 1, 2), True), 0x0D8: ((0,), False), 0x0E0: ((0,), True),
    0x0E8: ((), False), 0x0F0: ((0, 1, 2), True), 0x0F8: ((0, 1, 2), True),
    0x108: ((0, 1, 3), True), 0x110: ((0, 1, 3), True), 0x118: ((0,), True),
    0x128: ((), True), 0x158: ((0, 1, 2), True), 0x190: ((0,), True),
    0x198: ((0, 1, 2), True), 0x1A8: ((), True), 0x1B8: ((0, 1), True),
    0x1C0: ((0, 1, 2), True), 0x1D8: ((0,), False), 0x1E0: ((0, 1), True),
    0x1E8: ((0, 1), False), 0x1F0: ((0,), False), 0x1F8: ((0,), False),
    0x200: ((0, 1), True), 0x220: ((0, 1, 2), False),
}


# --- the trace -----------------------------------------------------------------------------

class Trace:
    """A trace file, parsed: console (fd, text), opens (name, mode, fd), loader calls (n,
    slot, in_irq, a0, a1, a2, a3, sp, v0), switches (kind, record, function), task events
    (event, record, function), interpreter entries, tolerated requests, the loader calls
    running at a stop (as loader calls, without v0), stops, counts
    ({name or (name, key): value}), the tasks alive at the end and the end line."""

    def __init__(self, path):
        self.console, self.opens, self.loader, self.switches, self.tasks = [], [], [], [], []
        self.interp, self.tolerated, self.stops, self.alive = [], [], [], []
        self.pending, self.dma, self.spins = [], [], []
        self.counts, self.end, self.kinds = {}, None, []
        with open(path, encoding="latin-1") as f:
            for line in f:
                self.add(line.rstrip("\n"))
        self.loader.sort()

    def add(self, line):
        t, kind, rest = (line.split(" ", 2) + [""])[:3]
        int(t)
        self.kinds.append(kind)
        if kind == "console":
            fd, n, text = rest.split(" ", 2)
            self.console.append((int(fd, 16), text))
        elif kind == "open":
            mode, fd, name = rest.split(" ", 2)
            self.opens.append((name, int(mode, 16), int(fd, 16)))
        elif kind == "loader":
            f = rest.split()
            self.loader.append((int(f[0]), int(f[1], 16), int(f[2])) +
                               tuple(int(x, 16) for x in f[3:]))
        elif kind == "switch":
            k, record, fn = rest.split()
            self.switches.append((k, int(record, 16), int(fn, 16)))
        elif kind == "task":
            event, record, fn = rest.split()
            self.tasks.append((event, int(record, 16), int(fn, 16)))
        elif kind == "interp":
            self.interp.append(int(rest, 16))
        elif kind == "tolerated":
            self.tolerated.append(rest)
        elif kind == "pending":
            f = rest.split()
            self.pending.append((int(f[0]), int(f[1], 16), int(f[2])) +
                                tuple(int(x, 16) for x in f[3:]))
        elif kind == "stop":
            self.stops.append(rest)
        elif kind == "alive":
            record, fn = rest.split()
            self.alive.append((int(record, 16), int(fn, 16)))
        elif kind == "count":
            f = rest.split()
            if f[0] in ("voodoo_reg", "voodoo_alt", "dropped", "slot", "ioctl"):  # NAME KEY N
                self.counts[(f[0], int(f[1], 0))] = int(f[2])
            else:
                self.counts[f[0]] = int(f[1])
        elif kind == "dma":
            self.dma.append(tuple(int(x) for x in rest.split()))
        elif kind == "spin":
            self.spins.append(int(rest))
        elif kind == "end":
            self.end = rest
        else:
            raise AssertionError(f"trace: unknown kind in {line!r}")


def facts(name):
    """The rows of a fact file under mame/m2-facts, without its # header."""
    with open(work_path(*FACTS, name), encoding="latin-1") as f:
        return [line.rstrip("\n") for line in f if line.strip() and not line.startswith("#")]


def mame_console():
    out = []
    for row in facts("console.txt"):
        _t, fd, _n, text = row.split(" ", 3)
        out.append((int(fd, 16), text))
    return out


def mame_opens():
    out = []
    for row in facts("opens.txt"):
        _t, mode, _a2, fd, _ra, name = row.split(" ", 5)
        out.append((name, int(mode, 16), int(fd, 16)))
    return out


def mame_tasks():
    return [(f[1], int(f[3], 16)) for f in (row.split() for row in facts("tasks.txt"))]


def mame_loader_first():
    return [(int(f[0]), int(f[1], 16)) + tuple(int(x, 16) for x in f[2:])
            for f in (row.split() for row in facts("loader-first.txt"))]


def compared(slot, args, v0):
    """The fields of a loader call that its signature has: (slot, args..., v0 or None)."""
    which, returns = SIGNATURES[slot]
    return (slot,) + tuple(args[i] for i in which) + (v0 if returns else None,)


# --- building and running ------------------------------------------------------------------

def build(generated, build_dir, target="carnevil"):
    """`make TARGET` for a generated folder; the program's path."""
    res = subprocess.run([_make(), "-C", REPO, target, f"GENERATED={generated}",
                          f"BUILD={build_dir}"], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"make {target} failed:\n" + res.stdout[-4000:]
                             + res.stderr[-4000:])
    return os.path.join(build_dir, "ubsan" if target == "carnevil-ubsan" else "", "carnevil")


def run(program, args, timeout):
    return subprocess.run([program, *args], capture_output=True, text=True, timeout=timeout)


def interrupted(program, args, sig, after, timeout=120):
    """The program run with args and sent sig after `after` seconds of wall time; a
    CompletedProcess with stdout and stderr as bytes."""
    proc = subprocess.Popen([program, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(after)
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def files_in(folder, raw=None, trace=True, quiet=True):
    """The options for an overlay, an NVRAM file and (if trace) a trace in folder."""
    args = ["--overlay", os.path.join(folder, "carnevil.overlay"),
            "--nvram", os.path.join(folder, "carnevil.nvram")]
    if raw:
        args = ["--disk", raw] + args
    if trace:
        args += ["--trace", os.path.join(folder, "trace.txt")]
    return args + (["--quiet"] if quiet else [])


# --- without game data ---------------------------------------------------------------------

MSG = b"hello\r\n\0"


def synthetic_exe(path, message=MSG, then=0x0C0, then_fd=5, then_len=0, writes_file=False):
    """GAME.EXE at the entry: write(1, message, len) through the stub block's slot 0x0C0,
    then a call of the stub block at offset `then` with a0 = then_fd, a1 = the message and
    a2 = then_len (by default another write, of 0 bytes to fd 5; with then_fd 3, a write of
    the message's first then_len bytes to the CMOS), then a loop for ever; the image runs past
    the stub block, with the message after it. With writes_file, instead of the call at
    `then`: open("NEW.BIN", 1) (slot 0x088) and a write of the message's 8 bytes to the
    descriptor it returns, a file written to the disk through the overlay."""
    msg = STUB_END
    name = msg + len(message)
    words = [A("lui", rt=A1, imm=msg >> 16), A("ori", rt=A1, rs=A1, imm=msg & 0xFFFF),
             A("addiu", rt=A0, rs=0, imm=1), A("addiu", rt=A2, rs=0, imm=len(message) - 1),
             A("jal", target=STUB + 0x0C0), 0]
    if writes_file:
        words += [A("lui", rt=A0, imm=name >> 16), A("ori", rt=A0, rs=A0, imm=name & 0xFFFF),
                  A("addiu", rt=A1, rs=0, imm=1), A("jal", target=STUB + 0x088), 0,
                  A("or", rd=A0, rs=2, rt=0),
                  A("lui", rt=A1, imm=msg >> 16), A("ori", rt=A1, rs=A1, imm=msg & 0xFFFF),
                  A("addiu", rt=A2, rs=0, imm=8), A("jal", target=STUB + 0x0C0), 0]
    else:
        words += [A("addiu", rt=A0, rs=0, imm=then_fd), A("addiu", rt=A2, rs=0, imm=then_len),
                  A("jal", target=STUB + then), 0]
    words += [A("beq", rs=0, rt=0, imm=-1), 0]
    words += [0] * ((msg - ENTRY) // 4 - len(words))
    words += list(struct.unpack("<%dI" % (len(message) // 4), message))
    words += list(struct.unpack("<2I", b"NEW.BIN\0"))
    return write_exe(path, words, load=ENTRY)


def sha256_file(path):
    """The SHA-256 of a whole file, read in 16 MB pieces."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for piece in iter(lambda: f.read(1 << 24), b""):
            h.update(piece)
    return h.hexdigest()


def synthetic_disk(path, exe_path, size=DISK_BYTES):
    """A disk image whose partition 1 holds GAME.EXE (the file at exe_path), as a sparse file
    of `size` bytes."""
    with open(exe_path, "rb") as f:
        raw = f.read()
    with open(path, "wb") as f:
        f.write(fs_image(fs_partition([("GAME.EXE", len(raw), 4)], [([4], raw)])))
        f.truncate(size)
    return path


class MainTest(unittest.TestCase):
    """The program on a synthetic GAME.EXE and disk (no game data)."""

    @classmethod
    def synthetic(cls, name, then=0x0C0, **kw):
        """(program, disk image, SHA-256) for synthetic_exe(then, **kw), under the class's
        folder."""
        folder = os.path.join(cls.dir, name)
        os.makedirs(os.path.join(folder, "disk"))
        image = synthetic_exe(os.path.join(folder, "GAME.EXE"), then=then, **kw)
        analysis = functions.analyse(image, ENTRY, (), [(STUB, STUB_END)])
        analysis = dataclasses.replace(analysis, exe_sha256=image.sha256)
        gen = os.path.join(folder, "generated")
        recomp.recompile(image, analysis, gen)
        program = build(gen, os.path.join(folder, "build"))
        raw = synthetic_disk(os.path.join(folder, "disk", "carnevil.raw"),
                             os.path.join(folder, "GAME.EXE"))
        return program, raw, image.sha256

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.program, cls.raw, cls.sha = cls.synthetic("main")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=self.dir)

    def refused(self, args, *needles):
        """Exit 2 with exactly one line on stderr holding every needle."""
        res = run(self.program, args, 60)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(len(res.stderr.splitlines()), 1, res.stderr)
        for needle in needles:
            self.assertIn(needle, res.stderr)
        return res

    def test_usage_errors(self):
        good = files_in(self.tmp, self.raw, trace=False)
        for args in ([], good[2:], good + ["--seconds", "0"], good + ["--seconds", "x"],
                     good + ["--seconds", "1", "--frames", "1"], good + ["--frames", "-1"],
                     good + ["--bogus"], good + ["--disk"], good + ["extra"]):
            with self.subTest(args=args):
                res = self.refused(args, "carnevil")
                self.assertIn("130 when interrupted by a signal", res.stderr)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_seconds_bound(self):
        """--seconds is bounded by what every time base of the runtime can represent (final
        review M3): the card's picosecond clock, power-on based, is the narrowest, so the
        bound is 18,000,000 s; one more is a usage error naming the bound."""
        res = self.refused(files_in(self.tmp, self.raw, trace=False) + ["--seconds", "18000001"],
                           "--seconds 18000001", "from 1 to 18000000")
        self.assertIn("usage:", res.stderr)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_file_errors(self):
        missing = os.path.join(self.tmp, "nonexistent.raw")
        self.refused(files_in(self.tmp, missing, trace=False) + ["--seconds", "1"], missing)
        short = synthetic_disk(os.path.join(self.tmp, "short.raw"),
                               os.path.join(self.dir, "main", "GAME.EXE"), DISK_BYTES - 512)
        self.refused(files_in(self.tmp, short, trace=False) + ["--seconds", "1"], short,
                     str(DISK_BYTES - 512))
        nvram = os.path.join(self.tmp, "carnevil.nvram")
        with open(nvram, "wb") as f:
            f.write(b"\xFF" * 100)
        # the size it has and the size it needs (final review M4)
        self.refused(files_in(self.tmp, self.raw, trace=False) + ["--seconds", "1"], nvram,
                     "100 bytes, not the CMOS's 32768")
        os.remove(nvram)
        os.mkdir(nvram)
        self.refused(files_in(self.tmp, self.raw, trace=False) + ["--seconds", "1"], nvram,
                     "not a regular file")
        os.rmdir(nvram)
        trace = os.path.join(self.tmp, "no", "such", "folder", "trace.txt")
        self.refused(files_in(self.tmp, self.raw, trace=False) + ["--trace", trace], trace)
        # nothing was created next to the image, and no overlay before the checks passed
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.raw))), ["carnevil.raw"])
        self.assertNotIn("carnevil.overlay", os.listdir(self.tmp))

    def test_another_hash_is_refused(self):
        other = os.path.join(self.tmp, "OTHER.EXE")
        image = synthetic_exe(other, b"howdy\r\n\0")
        raw = synthetic_disk(os.path.join(self.tmp, "other.raw"), other)
        self.refused(files_in(self.tmp, raw, trace=False) + ["--seconds", "1"], raw,
                     image.sha256, self.sha)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["OTHER.EXE", "other.raw"])

    def test_timed_run(self):
        """--seconds 1: the console write reaches standard output (not with --quiet) and the
        trace, the run ends at 1 s of virtual time with status 0, and the image is not
        changed."""
        before = os.stat(self.raw)
        res = subprocess.run([self.program, *files_in(self.tmp, self.raw, quiet=False),
                              "--seconds", "1"], capture_output=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, b"hello\r\n")
        self.assertRegex(res.stderr, rb"^carnevil: ran 1000000000 ns of guest time; RAM "
                                     rb"SHA-256 [0-9a-f]{64}\n$")
        after = os.stat(self.raw)
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        trace = Trace(os.path.join(self.tmp, "trace.txt"))
        self.assertEqual(trace.console, [(1, "hello\\x0D\\x0A")])
        self.assertEqual(len(trace.loader), 2)
        n, slot, irq, a0, a1, a2, _a3, sp, v0 = trace.loader[0]
        self.assertEqual((n, slot, irq, a0, a1, a2, v0), (1, 0x0C0, 0, 1, STUB_END, 7, 7))
        self.assertEqual(sp, 0x807FFFA8)  # the entry's sp, from config/boot_state.toml
        self.assertEqual(trace.loader[1][:6], (2, 0x0C0, 0, 5, STUB_END, 0))
        self.assertEqual(trace.loader[1][-1], 0)  # write to timer: returns 0
        self.assertEqual(trace.end, "seconds 1")
        self.assertEqual((trace.interp, trace.stops, trace.tasks), ([], [], []))
        self.assertEqual(trace.counts["vblank"], 0)
        self.assertEqual(trace.counts["sndfifo"], 0)
        self.assertEqual(os.path.getsize(os.path.join(self.tmp, "carnevil.nvram")), 0x8000)
        with open(os.path.join(self.tmp, "trace.txt"), encoding="latin-1") as f:
            first = f.readline()
        self.assertRegex(first, r"^[0-9]+ console 0x1 0x7 hello")
        # --quiet: nothing on stdout or stderr
        res = run(self.program, files_in(self.tmp, self.raw) + ["--seconds", "1"], 120)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_stop_is_traced(self):
        """A stop (here a call of 0x048, not one of the loader's slots) exits 3 with the
        message on stderr; the trace holds the call still running and the stop, then the
        end-of-run counts. The NVRAM is saved and the overlay kept."""
        program, raw, _sha = self.synthetic("stop", then=0x048)
        res = run(program, files_in(self.tmp, raw) + ["--seconds", "1"], 120)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertRegex(res.stderr, r"^carnevil: stop: loader slot 0x048: not a loader slot")
        trace = Trace(os.path.join(self.tmp, "trace.txt"))
        self.assertEqual(len(trace.loader), 1)
        # a3 is the entry's leftover (config/boot_state.toml)
        self.assertEqual(trace.pending, [(2, 0x048, 0, 5, STUB_END, 0, 0x000E0228, 0x807FFFA8)])
        self.assertEqual(len(trace.stops), 1)
        self.assertTrue(trace.stops[0].startswith("3 loader slot 0x048: not a loader slot"))
        self.assertIsNone(trace.end)
        self.assertEqual(trace.counts["loader_calls"], 2)
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["carnevil.nvram", "carnevil.overlay", "trace.txt"])

    def test_default_files_next_to_the_image(self):
        """Without --overlay and --nvram, both are next to the raw image."""
        folder = os.path.join(self.tmp, "own")
        os.makedirs(folder)
        raw = os.path.join(folder, "carnevil.raw")
        shutil.copyfile(self.raw, raw)
        res = run(self.program, ["--disk", raw, "--seconds", "1", "--quiet"], 120)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sorted(os.listdir(folder)),
                         ["carnevil.nvram", "carnevil.overlay", "carnevil.raw"])

    def test_interrupted_run_saves_and_closes(self):
        """SIGTERM, SIGINT and SIGHUP part-way through a run without a limit (final review
        I1): exit status 130; the NVRAM file holds the CMOS write the program made before the
        signal; the trace is complete to the interruption and ends with the "end
        interrupted" line and the totals; a following run on the same files starts
        normally."""
        program, raw, _sha = self.synthetic("cmos", then_fd=3, then_len=7)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig.name):
                folder = tempfile.mkdtemp(dir=self.dir)
                res = interrupted(program, files_in(folder, raw, quiet=False), sig, 1.0)
                self.assertEqual(res.returncode, 130, res.stderr)
                self.assertEqual(res.stdout, b"hello\r\n")
                self.assertRegex(res.stderr, rb"^carnevil: interrupted by " + sig.name.encode()
                                 + rb" at [0-9]+ ns of guest time\n$")
                with open(os.path.join(folder, "carnevil.nvram"), "rb") as f:
                    self.assertEqual(f.read(), MSG[:7] + b"\xFF" * (0x8000 - 7))
                trace = Trace(os.path.join(folder, "trace.txt"))
                self.assertEqual(trace.console, [(1, "hello\\x0D\\x0A")])
                self.assertEqual([t[1] for t in trace.loader], [0x0C0, 0x0C0])
                self.assertEqual(trace.end, f"interrupted {sig.name}")
                self.assertEqual(trace.counts["loader_calls"], 2)
                after_end = trace.kinds[trace.kinds.index("end") + 1:]
                self.assertIn("count", after_end)
                self.assertLessEqual(set(after_end), {"count", "alive"})
                # the next run on the same overlay and NVRAM starts and ends normally
                again = run(program, files_in(folder, raw) + ["--seconds", "1"], 120)
                self.assertEqual((again.returncode, again.stderr), (0, ""))

    def test_nvram_folder_not_writable(self):
        """An NVRAM file in a folder the program cannot write (final review M1) is refused
        before the run starts: exit 2, one line naming the file, and nothing is created (no
        overlay, no trace), whether the NVRAM file exists yet or not."""
        folder = os.path.join(self.tmp, "ro")
        os.mkdir(folder)
        args = ["--disk", self.raw, "--overlay", os.path.join(self.tmp, "o"),
                "--nvram", os.path.join(folder, "carnevil.nvram"),
                "--trace", os.path.join(self.tmp, "t"), "--seconds", "1"]
        os.chmod(folder, 0o555)
        self.addCleanup(os.chmod, folder, 0o755)
        if os.access(folder, os.W_OK):
            self.skipTest("this user can write a read-only folder (running as root?)")
        self.refused(args, os.path.join(folder, "carnevil.nvram"), "cannot write its folder")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["ro"])
        os.chmod(folder, 0o755)
        with open(os.path.join(folder, "carnevil.nvram"), "wb") as f:
            f.write(b"\xFF" * 0x8000)
        os.chmod(folder, 0o555)
        self.refused(args, os.path.join(folder, "carnevil.nvram"), "cannot write its folder")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["ro"])

    def test_raw_image_unchanged_by_a_run_that_writes(self):
        """The raw image is byte-identical before and after a run whose game writes a file
        through the overlay (final review M13): the synthetic image, all 4,310,046,720 bytes
        of it, hashed in full both times. (For the user's real image the tests keep to its
        size and modification time, test_user_files: hashing 4.3 GB per run would dominate
        the suite.)"""
        program, raw, _sha = self.synthetic("writer", then=0x088, writes_file=True)
        before = sha256_file(raw)
        res = run(program, files_in(self.tmp, raw) + ["--seconds", "1"], 120)
        self.assertEqual(res.returncode, 0, res.stderr)
        trace = Trace(os.path.join(self.tmp, "trace.txt"))
        [(name, mode, fd)] = trace.opens
        self.assertEqual((name, mode), ("NEW.BIN", 1))
        self.assertGreaterEqual(fd, 0)
        self.assertEqual(trace.loader[-1][1], 0x0C0)
        self.assertEqual(trace.loader[-1][-1], 8)  # the write wrote its 8 bytes
        self.assertGreater(os.path.getsize(os.path.join(self.tmp, "carnevil.overlay")), 512)
        self.assertEqual(sha256_file(raw), before)

    def test_failed_nvram_save_keeps_the_rest(self):
        """The NVRAM save failing at the end of a run (its temporary file's name taken by a
        folder) stops with a message naming the file (final review M10): the overlay is still
        flushed and the trace still gets its stop line and its totals; exit 3."""
        good = files_in(self.tmp, self.raw)
        self.assertEqual(run(self.program, good + ["--seconds", "1"], 120).returncode, 0)
        os.mkdir(os.path.join(self.tmp, "carnevil.nvram.tmp"))
        res = run(self.program, good + ["--seconds", "1"], 120)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertRegex(res.stderr, r"^carnevil: stop: cmos_save: cannot write \S*"
                                     r"carnevil.nvram.tmp: ")
        trace = Trace(os.path.join(self.tmp, "trace.txt"))
        self.assertEqual(trace.end, "seconds 1")
        self.assertEqual(len(trace.stops), 1)
        self.assertTrue(trace.stops[0].startswith("3 cmos_save: cannot write"), trace.stops)
        self.assertEqual(trace.counts["loader_calls"], 2)


# --- with the game -------------------------------------------------------------------------

_game = {}


def game_program(target="carnevil"):
    """The program built from GAME.EXE, recompiled once per module into a temporary folder."""
    if "generated" not in _game:
        tmp = tempfile.mkdtemp()
        _game["tmp"] = tmp
        gen = os.path.join(tmp, "generated")
        recomp.recompile(exe.load_exe(work_path("part0_files", "GAME.EXE")),
                         functions.read_toml(FUNCTIONS), gen)
        _game["generated"] = gen
    if target not in _game:
        _game[target] = build(_game["generated"], os.path.join(_game["tmp"], "build"), target)
    return _game[target]


def game_run(seconds, name, extra=(), quiet=True, files_from=None):
    """A run of the game for `seconds` with its overlay, NVRAM and trace in a fresh folder
    (the files of an earlier run copied in first, if files_from names its folder); (the
    CompletedProcess, the folder)."""
    folder = os.path.join(_game["tmp"], name)
    os.makedirs(folder)
    if files_from:
        for f in ("carnevil.overlay", "carnevil.nvram"):
            shutil.copyfile(os.path.join(files_from, f), os.path.join(folder, f))
    res = run(game_program(), files_in(folder, work_path("carnevil.raw"), quiet=quiet)
              + ["--seconds", str(seconds), *extra],
              RUN_TIMEOUT if seconds > SHORT_SECONDS else SHORT_TIMEOUT)
    return res, folder


def long_run():
    """The shared run of RUN_SECONDS: (CompletedProcess, Trace, folder, raw stat before,
    raw stat after)."""
    if "long" not in _game:
        game_program()
        raw = work_path("carnevil.raw")
        before = os.stat(raw)
        res, folder = game_run(RUN_SECONDS, "long")
        after = os.stat(raw)
        _game["long"] = (res, Trace(os.path.join(folder, "trace.txt")), folder, before, after)
    return _game["long"]


def tearDownModule():
    if "tmp" in _game:
        shutil.rmtree(_game["tmp"])
        _game.clear()


@requires_file("carnevil.raw")
@requires_file("part0_files", "GAME.EXE")
@requires_file(*FACTS, "console.txt")
class BootTest(unittest.TestCase):
    """GAME.EXE from the user's image, run headless, against MAME's run A (attract mode, blank
    NVRAM, no input)."""

    def trace(self):
        return long_run()[1]

    def test_console_matches_mame(self):
        self.assertEqual(self.trace().console, mame_console())

    def test_opens_match_mame(self):
        port, mame = self.trace().opens, mame_opens()
        common = min(len(port), len(mame))
        self.assertEqual(port[:common], mame[:common])
        self.assertGreaterEqual(len(port), 180, f"{len(port)} of MAME's {len(mame)} opens")

    def test_tasks_match_mame(self):
        trace = self.trace()
        self.assertEqual([(e, fn) for e, _rec, fn in trace.tasks], mame_tasks())
        self.assertLessEqual(trace.counts["tasks_max"], 6)

    def test_first_loader_calls_match_mame(self):
        """The first 400 loader calls of each side, those outside interrupts (in_irq 0 here;
        on MAME's side sp outside the loader's exception stack), agree in order on the slot,
        the arguments its signature has and v0. The shorter list is compared whole."""
        port = [compared(slot, (a0, a1, a2, a3), v0)
                for _n, slot, irq, a0, a1, a2, a3, _sp, v0 in self.trace().loader[:400]
                if not irq]
        mame = [compared(slot, (a0, a1, a2, a3), v0)
                for _n, slot, a0, a1, a2, a3, sp, v0 in mame_loader_first()[:400]
                if not EXCEPTION_STACK[0] <= sp < EXCEPTION_STACK[1]]
        common = min(len(port), len(mame))
        self.assertGreater(common, 300, (len(port), len(mame)))
        for i, (p, m) in enumerate(zip(port[:common], mame[:common])):
            self.assertEqual(p, m, f"call {i} outside interrupts (slot, args, v0)")

    def test_reaches_and_stays_in_attract(self):
        res, trace = long_run()[:2]
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self.assertEqual(trace.end, f"seconds {RUN_SECONDS}")
        self.assertGreaterEqual(trace.counts["vblank"], 8000)
        self.assertIn(ATTRACT, [fn for _rec, fn in trace.alive])

    def test_nothing_unexplained(self):
        """Every interpreter entry and tolerated request of the run is listed in
        config/runtime.expected.toml with a reason, and the file lists nothing the run did
        not use."""
        with open(EXPECTED, "rb") as f:
            expected = tomllib.load(f)
        self.assertLessEqual(set(expected), {"interp", "tolerated"})
        listed_interp = {e["addr"]: e["reason"] for e in expected.get("interp", [])}
        listed_tolerated = {e["request"]: e["reason"] for e in expected.get("tolerated", [])}
        for reason in list(listed_interp.values()) + list(listed_tolerated.values()):
            self.assertTrue(reason.strip(), "an entry without a reason")
        trace = self.trace()
        self.assertEqual(set(trace.interp) - set(listed_interp), set(),
                         "interpreter entries not in runtime.expected.toml")
        self.assertEqual(set(trace.tolerated) - set(listed_tolerated), set(),
                         "tolerated requests not in runtime.expected.toml")
        self.assertEqual(set(listed_interp) - set(trace.interp), set(), "unused entries")
        self.assertEqual(set(listed_tolerated) - set(trace.tolerated), set(), "unused entries")

    def test_deterministic(self):
        """Two traced runs of --seconds 20 give byte-identical traces; with an untraced run,
        all three print the same console output and end with the same time and RAM hash, so
        tracing changes nothing the guest does."""
        game_program()
        runs = [game_run(SHORT_SECONDS, f"det{i}", quiet=False) for i in (1, 2)]
        res, folder = game_run(SHORT_SECONDS, "det3", quiet=False)
        os.remove(os.path.join(folder, "carnevil.overlay"))
        os.remove(os.path.join(folder, "carnevil.nvram"))
        untraced = run(game_program(), ["--disk", work_path("carnevil.raw")]
                       + files_in(folder, trace=False, quiet=False)
                       + ["--seconds", str(SHORT_SECONDS)], SHORT_TIMEOUT)
        traces = []
        for r, f in runs:
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            with open(os.path.join(f, "trace.txt"), "rb") as t:
                traces.append(t.read())
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(untraced.returncode, 0, untraced.stderr[-2000:])
        for r, _f in runs:
            self.assertEqual(r.stdout, untraced.stdout)
            self.assertEqual(r.stderr, untraced.stderr)
        self.assertRegex(untraced.stderr, r"carnevil: ran [0-9]+ ns of guest time; RAM "
                                          r"SHA-256 [0-9a-f]{64}\n$")

    def test_dma_completion_matches_mame(self):
        """Every DMA channel 0 chain of the run completes (cause bit 4 due, from the enable
        write) within 5 ns of every completion MAME measured for its shape (records not
        writing the frame buffer, bytes of those that do: dma0.txt), held chains included:
        the card's hold of the channel never pushes a chain past the measured time."""
        mame, cur = collections.defaultdict(set), None
        for row in facts("dma0.txt"):
            f = row.split()
            if f[1] == "chain":
                cur = [0, 0]
            elif f[1] == "rec":
                count, dest = int(f[3], 16), int(f[5], 16)
                if 0x08400000 <= dest < 0x08800000:
                    cur[1] += count
                else:
                    cur[0] += 1
            elif f[1] == "b4" and f[2] != "-":
                mame[tuple(cur)].add(int(f[2]))
        port = collections.defaultdict(set)
        for _records, n_other, lfb, delay in self.trace().dma:
            port[(n_other, lfb)].add(delay)
        self.assertTrue(port)
        self.assertLessEqual(set(port), set(mame), "chain shapes MAME did not measure")
        for shape, delays in sorted(port.items()):
            worst = max(abs(d - m) for d in delays for m in mame[shape])
            with self.subTest(shape=shape):
                self.assertLessEqual(worst, 5, (sorted(delays), sorted(mame[shape])))

    def test_frames(self):
        """--frames 100 ends the run at the 100th VBLANK interrupt with status 0."""
        game_program()
        frames_dir = os.path.join(_game["tmp"], "frames100")
        os.makedirs(frames_dir)
        res = run(game_program(), files_in(frames_dir, work_path("carnevil.raw"))
                  + ["--frames", "100"], SHORT_TIMEOUT)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        trace = Trace(os.path.join(frames_dir, "trace.txt"))
        self.assertEqual(trace.end, "frames 100")
        self.assertEqual(trace.counts["vblank"], 100)

    def test_host_write_error_stops(self):
        """A host write to the overlay that fails (the file-size limit RLIMIT_FSIZE reached
        part-way through the first boot's 1,426 records) stops the run with a message naming
        the overlay and the system's error (final review M2), instead of handing the game the
        loader's -1; the overlay still opens afterwards."""
        from tests.test_disk_c import _limit_file_size
        game_program()
        folder = os.path.join(_game["tmp"], "fsize")
        os.makedirs(folder)
        overlay = os.path.join(folder, "carnevil.overlay")
        res = subprocess.run([game_program(), *files_in(folder, work_path("carnevil.raw"),
                                                        trace=False), "--seconds", "30"],
                             capture_output=True, text=True, timeout=SHORT_TIMEOUT,
                             preexec_fn=_limit_file_size(600_000))
        self.assertEqual(res.returncode, 3, res.stderr[-2000:])
        self.assertRegex(res.stderr, r"(?m)^carnevil: stop: overlay " + re.escape(overlay)
                         + r": cannot write sector [0-9]+: File too large$")
        r = run(game_program(), files_in(folder, work_path("carnevil.raw"), trace=False)
                + ["--seconds", "1"], SHORT_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

    def test_interrupted_boot(self):
        """A run without a limit sent SIGTERM after 4 s of wall time (final review I1): exit
        130; its trace up to the "end interrupted SIGTERM" line is the shared 150 s run's
        trace up to there, and the totals follow; its NVRAM is what the game wrote up to the
        signal: the NVRAM of a run ended by --frames at the VBLANK before the signal or the
        one after (the game rewrites a running count in the CMOS about once a second, so the
        state between two VBLANKs is one of the two); a second run on the same files starts
        as a second boot."""
        long_trace = os.path.join(long_run()[2], "trace.txt")
        folder = os.path.join(_game["tmp"], "sigterm")
        os.makedirs(folder)
        res = interrupted(game_program(), files_in(folder, work_path("carnevil.raw")),
                          signal.SIGTERM, 4.0)
        self.assertEqual(res.returncode, 130, res.stderr[-2000:])
        with open(os.path.join(folder, "trace.txt"), encoding="latin-1") as f:
            lines = f.read().splitlines()
        trace = Trace(os.path.join(folder, "trace.txt"))
        self.assertEqual(trace.end, "interrupted SIGTERM")
        end = trace.kinds.index("end")
        self.assertLessEqual(set(trace.kinds[end + 1:]), {"count", "alive"})
        with open(long_trace, encoding="latin-1") as f:
            reference = [next(f).rstrip("\n") for _ in range(end)]
        self.assertEqual(lines[:end], reference)
        self.assertEqual(trace.console, mame_console())  # all 25 come within 3 s of game time
        frames = trace.counts["vblank"]
        nvrams = []
        for n in (frames, frames + 1):
            ref = os.path.join(_game["tmp"], f"sigterm-frames{n}")
            os.makedirs(ref)
            r = run(game_program(), files_in(ref, work_path("carnevil.raw"), trace=False)
                    + ["--frames", str(n)], SHORT_TIMEOUT)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            with open(os.path.join(ref, "carnevil.nvram"), "rb") as f:
                nvrams.append(f.read())
        with open(os.path.join(folder, "carnevil.nvram"), "rb") as f:
            self.assertIn(f.read(), nvrams)
        self.assertNotEqual(nvrams[0], b"\xFF" * 0x8000)
        r, second = game_run(SHORT_SECONDS, "sigterm-second", files_from=folder)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        console = Trace(os.path.join(second, "trace.txt")).console
        self.assertNotIn("reseting high scores", " ".join(text for _fd, text in console))

    def test_spin_counter_sane(self):
        spin = self.trace().counts["spin_max"]
        self.assertGreater(spin, 0)
        self.assertLess(spin, 10_000_000)

    def test_user_files(self):
        """A missing image, an image of the wrong size and a GAME.EXE of another hash each
        exit 2 with one line naming the file; the raw image is unchanged by a run; a second
        run on the first run's overlay and NVRAM prints what a non-blank NVRAM gives."""
        program = game_program()
        raw = work_path("carnevil.raw")
        res, _trace, folder, before, after = long_run()
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        tmp = tempfile.mkdtemp(dir=_game["tmp"])
        missing = os.path.join(tmp, "carnevil.raw")
        r = run(program, files_in(tmp, missing, trace=False) + ["--seconds", "1"], 60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertIn(missing, r.stderr)
        # the disk header and partition 1 copied, with GAME.EXE's first block patched
        with open(raw, "rb") as f:
            head = f.read(512)
            start, size = struct.unpack_from("<ii", head, 0x10 + 4)
            f.seek(0)
            data = bytearray(f.read((start + size) * 512))
        with open(work_path("part0_files", "GAME.EXE"), "rb") as f:
            first = f.read(512)
        at = data.find(first)
        self.assertGreater(at, 0)
        while at >= 0:
            data[at + 0x100] ^= 0xFF
            at = data.find(first, at + 1)
        short = os.path.join(tmp, "short.raw")
        with open(short, "wb") as f:
            f.write(data)
        r = run(program, files_in(tmp, short, trace=False) + ["--seconds", "1"], 60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertIn(short, r.stderr)
        patched = os.path.join(tmp, "patched.raw")
        os.rename(short, patched)
        with open(patched, "r+b") as f:
            f.truncate(DISK_BYTES)
        r = run(program, files_in(tmp, patched, trace=False) + ["--seconds", "1"], 120)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertIn(patched, r.stderr)
        self.assertIn(SUPPORTED_SHA256, r.stderr)
        self.assertNotIn("carnevil.overlay", os.listdir(tmp))
        os.remove(patched)
        # the second run, on copies of the shared run's overlay and NVRAM
        r, second = game_run(SHORT_SECONDS, "second", files_from=folder)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        console = Trace(os.path.join(second, "trace.txt")).console
        self.assertEqual(console[:14], mame_console()[:14])
        self.assertNotIn("reseting high scores", " ".join(text for _fd, text in console))

    def test_ubsan_run(self):
        """--seconds 20 built with -fsanitize=undefined -fno-sanitize-recover=undefined (make
        carnevil-ubsan): no report, status 0."""
        from tests.test_against_mame import _require_ubsan
        _require_ubsan(_game.get("tmp") or tempfile.gettempdir())
        program = game_program("carnevil-ubsan")
        folder = os.path.join(_game["tmp"], "ubsan")
        os.makedirs(folder)
        res = run(program, files_in(folder, work_path("carnevil.raw"))
                  + ["--seconds", str(SHORT_SECONDS)], RUN_TIMEOUT)
        self.assertEqual((res.returncode, res.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
