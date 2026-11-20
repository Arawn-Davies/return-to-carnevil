"""The loader stand-ins (runtime/loader/loader.h) against docs/analysis/loader-services.md:
section 1 (the slots), section 2 (descriptors, the name table, the drivers), section 3 (the
ioctls), section 7 (start-up state) and section 8 (what each stand-in must do).

The C runs in tests/harness/loader_driver.c (its top comment lists the commands), built with
the global C flags, -Wall -Wextra -Werror and the sanitizer flags, with the runtime core, the
interpreter and the disk library, into a cache under the system temp dir. Synthetic disk
images, overlays and NVRAM files live in a TemporaryDirectory. The tests that need the game's
disk or the measured MAME facts skip without them; the game data is opened read-only, with an
overlay in a temporary directory.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest

from carnevil_tools import disk, vectors
from tests import charness
from tests.gamedata import requires_file, work_path
from tests.test_clock_irq import LOOP, asm, call_probe, li, place, ret
from tests.test_disk_c import fs_image, fs_partition, pattern

REPO = charness.REPO
CORE = os.path.join(REPO, "runtime", "core")
DISK_DIR = os.path.join(REPO, "runtime", "disk")
LOADER_DIR = os.path.join(REPO, "runtime", "loader")
HW_DIR = os.path.join(REPO, "runtime", "hw")
LOADER_SOURCES = [os.path.join(LOADER_DIR, f) for f in (
    "slots.c", "fd.c", "dev_file.c", "dev_con.c", "dev_cmos.c", "dev_pic.c", "dev_timer.c",
    "dev_ioasic.c", "dev_sysctrl.c", "diskslots.c")]
HW_SOURCES = [os.path.join(CORE, "bus.c")] + \
             [os.path.join(HW_DIR, f) for f in ("voodoo_null.c", "gun.c", "misc.c")]
SOURCES = [os.path.join(CORE, f) for f in ("call.c", "fpu.c", "clock.c", "irq.c", "exc.c")] + \
          [os.path.join(REPO, "runtime", "interp", "interp.c")] + \
          [os.path.join(DISK_DIR, f) for f in ("image.c", "fs.c", "sha256.c")] + \
          LOADER_SOURCES + HW_SOURCES
DRIVER = os.path.join(REPO, "tests", "harness", "loader_driver.c")
HEADERS = [os.path.join(charness.INCLUDE, h) for h in ("recomp.h", "machine.h")] + \
          [os.path.join(DISK_DIR, "disk.h"), os.path.join(LOADER_DIR, "loader.h"),
           os.path.join(HW_DIR, "hw.h")]
CACHE = os.path.join(tempfile.gettempdir(), "carnevil-loader-driver")
TIMEOUT = 120

A = vectors.assemble
NOP = 0
V0, A0, A1, A2, T0, SP, RA = 2, 4, 5, 6, 8, 29, 31

STATUS = 0x30010401
IM3, IM6 = 0x800, 0x4000
VBLANK_PERIOD_NS = 18467220
NS_PER_CHECK = 100
EOF = 0xFFFFDBAE
ENOENT = 0xFFFFDBB7
M1 = 0xFFFFFFFF
SAT = 0x13FFFFEC  # 0xFFFFFF counts of 20 ns

STUB = 0x800C408C
MAIN = 0x80100000
H = 0x80110000
FN = 0x80150000
CALLER_RA = 0x80123458
NAME = 0x80201000
BUF = 0x80202000
BUF2 = 0x80203000
DATA = 0x80204000
FDP = 0x80205000
MARK = 0x5EED5EED

# Synthetic partition 1: A.BIN, 0x1008 bytes over blocks 4 and 5.
A_SIZE = 0x1008
A_DATA = pattern(A_SIZE, 5)
A_TIME, A_DATE = 0x7BEE, 0x254E  # fs_partition's date/time word 0x254E7BEE


def build_driver(driver=DRIVER, name="loader_driver"):
    """Path of the binary of `driver` (the loader driver by default) linked with SOURCES.
    Raises unittest.SkipTest without `cc`."""
    cc = charness._cc()
    flags = charness.CFLAGS + charness.RUNTIME_WARNINGS + charness.SANITIZE
    includes = ("-I", charness.INCLUDE, "-I", DISK_DIR, "-I", LOADER_DIR, "-I", HW_DIR)
    h = hashlib.sha256()
    st = os.stat(os.path.realpath(cc))
    h.update(f"{os.path.realpath(cc)}\0{st.st_size}\0{st.st_mtime_ns}\0{name}\0".encode())
    h.update("\0".join(flags).encode())
    for path in SOURCES + [driver] + HEADERS:
        with open(path, "rb") as f:
            h.update(path.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    out = os.path.join(CACHE, h.hexdigest()[:32])
    exe = os.path.join(out, name)
    if os.path.exists(exe):
        return exe
    os.makedirs(CACHE, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=CACHE, prefix="build-")
    try:
        objs = []
        for i, src in enumerate(SOURCES + [driver]):
            obj = os.path.join(tmp, f"{i}-{os.path.basename(src)}.o")
            charness._cc_run([cc, *flags, *includes, "-c", src, "-o", obj])
            objs.append(obj)
        charness._cc_run([cc, *charness.SANITIZE, *objs, "-lm",
                          "-o", os.path.join(tmp, name)])
        try:
            os.rename(tmp, out)
        except OSError:  # another process built the same key first
            if not os.path.exists(exe):
                raise
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    return exe


def call(slot, a0=0, a1=0, a2=0, a3=0):
    return f"call {slot:X} {a0 & M1:X} {a1 & M1:X} {a2 & M1:X} {a3 & M1:X}"


def ioctl(fd, cmd, arg=0):
    return call(0x0C8, fd, cmd, arg)


def string(addr, text):
    """RAM commands for the bytes of text and a NUL at addr (text may hold any byte)."""
    raw = text.encode("latin-1") + b"\0"
    raw += b"\0" * (-len(raw) % 4)
    return [f"mem {addr + i:08X} {int.from_bytes(raw[i:i + 4], 'little'):08X}"
            for i in range(0, len(raw), 4)]


def data(addr, raw):
    raw = bytes(raw) + b"\0" * (-len(raw) % 4)
    return [f"mem {addr + i:08X} {int.from_bytes(raw[i:i + 4], 'little'):08X}"
            for i in range(0, len(raw), 4)]


def open_(name, mode, addr=NAME):
    return string(addr, name) + [call(0x088, addr, mode)]


class Out:
    """The driver's output, parsed."""

    def __init__(self, text):
        self.text = text
        self.lines = [line.split() for line in text.splitlines() if line.split()]

    def all(self, key):
        return [line[1:] for line in self.lines if line[0] == key]

    def rets(self):
        return [(int(v0, 16), int(v1, 16)) for v0, v1 in self.all("ret")]

    def v0s(self):
        return [v0 for v0, _ in self.rets()]

    def dumps(self):
        return [[int(x, 16) for x in d] for d in self.all("dump")]

    def peeks(self):
        return [bytes.fromhex(p[0]) if p else b"" for p in self.all("peek")]

    def m(self, i=-1):
        f = self.all("m")[i]
        return dict(status=int(f[0], 16), cause=int(f[1], 16), intctl=int(f[2], 16),
                    now=int(f[3]), fcsr=int(f[4], 16), vblank=int(f[5]))

    def handlers(self, i=-1):
        f = [int(x, 16) for x in self.all("handlers")[i]]
        return dict(line=f[0:8], gt4=f[8], fpe=f[9:15], break7=f[15], break6=f[16],
                    gtmask=f[17])

    def probes(self):
        return [dict(zip(("a0", "a1", "a2", "sp", "status"), (int(x, 16) for x in p[:5])),
                     now=int(p[5])) for p in self.all("probe")]


class LoaderTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_driver()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def drive(self, cmds, status=0, stderr=False):
        """Out of one driver run of `cmds` after a reset (ra set to CALLER_RA); fails on
        another exit status, or on stderr output from a run that should succeed unless
        `stderr`."""
        res = subprocess.run([self.exe], capture_output=True, text=True, timeout=TIMEOUT,
                             input="\n".join(["reset", f"r {RA:X} {CALLER_RA:08X}"] + cmds)
                             + "\n")
        if res.returncode != status:
            self.fail(f"driver status {res.returncode}, want {status}: {res.stderr}")
        if status == 0 and res.stderr and not stderr:
            self.fail(f"driver wrote to stderr: {res.stderr}")
        return Out(res.stdout), res.stderr

    def stops(self, cmds, *needles):
        """The run stops through rt_stop: status 3, one "carnevil: stop:" line holding every
        needle, and no "ret" line after the last command (the stopping call never
        returns)."""
        out, err = self.drive(cmds, status=3)
        lines = [line for line in err.splitlines() if line.startswith("carnevil: stop: ")]
        self.assertEqual(len(lines), 1, err)
        for needle in needles:
            self.assertIn(needle, lines[0])
        calls = sum(1 for c in cmds if c.startswith("call "))
        stopper = 1 if cmds and cmds[-1].startswith("call ") else 0
        self.assertEqual(len(out.all("ret")), max(calls - stopper, 0), out.text)
        return out, lines[0]

    def image(self, entries=None, files=None, overlay=True):
        """Commands opening a synthetic image (A.BIN by default) with an overlay in the
        temporary directory."""
        if entries is None:
            entries, files = [("A.BIN", A_SIZE, 4)], [([4, 5], A_DATA)]
        raw = self.path("disk.raw")
        with open(raw, "wb") as f:
            f.write(fs_image(fs_partition(entries, files)))
        return [f"img {raw} {self.path('disk.ovl') if overlay else '-'}"]


class LoaderTest(LoaderTestCase):
    # --- section 8's named tests --------------------------------------------------------

    def test_close_then_ioctl(self):
        """close(3) returns 0 and a following ioctl(3, 0x6301) returns -1; close(16) is -1;
        a second close of 3 is -1 (section 8, slot 0x018)."""
        out, _ = self.drive([call(0x018, 3), ioctl(3, 0x6301, BUF), call(0x018, 16),
                             call(0x018, 3), call(0x018, 0xFFFFFFFF)])
        self.assertEqual(out.v0s(), [0, M1, M1, M1, M1])

    def test_open_pci_is_6(self):
        """open("pci:", 2) at the hand-off returns 6 with v1 = 4 * fd (sections 1 and 8,
        measured in loader-calls.md); its ioctls are tests/test_hw.py's."""
        out, _ = self.drive(open_("pci:", 2) + ["desc 6"])
        self.assertEqual(out.rets(), [(6, 24)])
        self.assertEqual(out.all("desc"), [["03"]])

    def test_open_nosuchfile(self):
        """open("nosuchfile", 0) returns -1, leaves the descriptor free, and upper-cases the
        name in place (section 1, slot 0x088), with or without a disk image."""
        for img in (self.image(), []):
            with self.subTest(image=bool(img)):
                out, _ = self.drive(img + open_("nosuchfile", 0) + [f"peek {NAME:08X} B",
                                                                     "desc 6"])
                self.assertEqual(out.v0s(), [M1])
                self.assertEqual(out.peeks(), [b"NOSUCHFILE\0"])
                self.assertEqual(out.all("desc"), [["00"]])

    def test_fpe_set_handler(self):
        """fpe_set_handler (slot 0x1E8): cause 0 to 4 set (fn non-zero) or clear the FCSR
        enable bit 0x80 << cause through rt_set_fcsr; cause 5 has none; the handler table
        is filled; v0 is left; cause 6 stops."""
        out, _ = self.drive(["fcsr 01000000", f"r {V0:X} {MARK:08X}", call(0x1E8, 3, FN),
                             "state", call(0x1E8, 5, FN + 4), "state", call(0x1E8, 0, FN + 8),
                             "state", call(0x1E8, 3, 0), "state", "handlers"])
        self.assertEqual([m[4] for m in out.all("m")],
                         ["01000400", "01000400", "01000480", "01000080"])
        self.assertEqual(out.v0s(), [MARK] * 4)
        self.assertEqual(out.handlers()["fpe"], [FN + 8, 0, 0, 0, 0, FN + 4])
        self.stops([call(0x1E8, 6, FN)], "0x1E8", "cause 6")

    def test_findfirst(self):
        """find-first (slot 0x038): game.env, absent, returns -1 and leaves the buffer; a
        present file returns 0 with the attribute byte at +21, time +22, date +24, the size
        (u32) at +26 and the name at +30 (section 1); a wildcard pattern stops."""
        out, _ = self.drive(self.image() + [f"fill {BUF:08X} 40 AA"]
                            + string(NAME, "game.env") + [call(0x038, NAME, 0, BUF),
                                                          f"peek {BUF:08X} 40"]
                            + string(NAME, "a.bin") + [call(0x038, NAME, 0, BUF),
                                                       f"peek {BUF:08X} 40"])
        self.assertEqual(out.v0s(), [M1, 0])
        before, after = out.peeks()
        self.assertEqual(before, b"\xAA" * 0x40)
        want = bytearray(b"\xAA" * 0x40)
        want[21] = 0
        want[22:26] = A_TIME.to_bytes(2, "little") + A_DATE.to_bytes(2, "little")
        want[26:30] = A_SIZE.to_bytes(4, "little")
        want[30:36] = b"A.BIN\0"
        self.assertEqual(after, bytes(want))
        self.stops(self.image() + string(NAME, "*.BIN") + [call(0x038, NAME, 0, BUF)],
                   "0x038", "*.BIN")

    def test_read_rules(self):
        """read (slot 0x090) on a file: 0 when n % 4 != 0, the bytes otherwise (clamped to
        the file), 0xFFFFDBAE at end of file; -1 for a bad or closed descriptor."""
        out, _ = self.drive(self.image() + open_("A.BIN", 0)
                            + [call(0x090, 6, BUF, 6), call(0x090, 6, BUF, 0x1000),
                               f"sha {BUF:08X} 1000", call(0x090, 6, BUF2, 0x100),
                               f"peek {BUF2:08X} 8", call(0x090, 6, BUF, 4),
                               call(0x090, 16, BUF, 4), call(0x090, 7, BUF, 4),
                               call(0x018, 6), call(0x090, 6, BUF, 4)])
        self.assertEqual(out.v0s(), [6, 0, 0x1000, 8, EOF, M1, M1, 0, M1])
        self.assertEqual(out.all("sha"), [[hashlib.sha256(A_DATA[:0x1000]).hexdigest()]])
        self.assertEqual(out.peeks(), [A_DATA[0x1000:]])

    def test_lseek_words(self):
        """lseek (slot 0x1C0) positions in words: SET offset / 4, CUR current + offset / 4
        (an arithmetic shift), END the length (offset ignored); the new position x 4; a bad
        fd or whence 3 returns 0 (sections 1 and 8)."""
        out, _ = self.drive(self.image() + open_("A.BIN", 0)
                            + [call(0x1C0, 6, 0x10, 0), call(0x090, 6, BUF, 4),
                               f"peek {BUF:08X} 4", call(0x1C0, 6, 7, 0),
                               call(0x1C0, 6, 0x13, 1), call(0x1C0, 6, -4, 1),
                               call(0x1C0, 6, 0x99, 2), call(0x1C0, 6, -1, 1),
                               call(0x1C0, 6, 0, 3), call(0x1C0, 16, 0, 0),
                               call(0x1C0, 6, 0, 2), call(0x090, 6, BUF, 4)])
        self.assertEqual(out.v0s(), [6, 0x10, 4, 4, 0x14, 0x10, A_SIZE, A_SIZE - 4, 0, 0,
                                     A_SIZE, EOF])
        self.assertEqual(out.peeks(), [A_DATA[0x10:0x14]])
        # what the loader returns for these is not documented (section 1, slot 0x1C0)
        self.stops(self.image() + open_("A.BIN", 0) + [call(0x1C0, 6, 0x2000, 0)],
                   "0x1C0", "outside the file")
        self.stops([call(0x1C0, 3, 0, 0)], "0x1C0", "cmos:")
        self.stops([call(0x1C0, 7, 0, 0)], "0x1C0", "not open")

    def test_cmos(self):
        """cmos: (sections 2 and 3): 0x6301 base and 0x6302 size; 0x6300 positions at
        0xB6100000 + 4 x offset, out of range -2 keeping the old position; a write then read
        of 4 bytes returns them without moving the position; transfers are clamped at the
        end; the file holds the bytes after cmos_save; a missing NVRAM file is created blank,
        a truncated one stops."""
        nv = self.path("nvram")
        pos = 0xB6100000 + 4 * 0x123
        out, _ = self.drive([f"cmosload {nv}", ioctl(3, 0x6301, BUF), ioctl(3, 0x6302, BUF2),
                             f"dump {BUF:08X} 1", f"dump {BUF2:08X} 1",
                             call(0x090, 3, BUF, 4), f"peek {BUF:08X} 4",
                             ioctl(3, 0x6300, pos)] + data(DATA, b"\x11\x22\x33\x44")
                            + [call(0x0C0, 3, DATA, 4), call(0x090, 3, BUF, 4),
                               f"peek {BUF:08X} 4", call(0x090, 3, BUF, 4), f"peek {BUF:08X} 4",
                               ioctl(3, 0x6300, 0xB6120000), ioctl(3, 0x6300, 0xB60FFFFC),
                               call(0x090, 3, BUF2, 2), f"peek {BUF2:08X} 2",
                               "cmosbyte 123", "cmosbyte 126",
                               ioctl(3, 0x6300, 0xB6100000 + 4 * 0x7FFE),
                               call(0x090, 3, BUF, 8), call(0x0C0, 3, DATA, 8), "cmossave"])
        self.assertEqual(out.v0s(), [0, 0, 4, 0, 4, 4, 4, 0xFFFFFFFE, 0xFFFFFFFE, 2, 0, 2, 2])
        self.assertEqual(out.dumps(), [[0xB6100000], [0x8000]])
        self.assertEqual(out.peeks(), [b"\xFF" * 4, b"\x11\x22\x33\x44", b"\x11\x22\x33\x44",
                                       b"\x11\x22"])
        self.assertEqual(out.all("cmosbyte"), [["11"], ["44"]])
        with open(nv, "rb") as f:
            saved = f.read()
        want = bytearray(b"\xFF" * 0x8000)
        want[0x123:0x127] = b"\x11\x22\x33\x44"
        want[0x7FFE:0x8000] = b"\x11\x22"
        self.assertEqual(saved, bytes(want))
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n != "nvram"], [])
        # a second run loads the file as saved
        out, _ = self.drive([f"cmosload {nv}", "cmosbyte 124", "cmosbyte 7FFF"])
        self.assertEqual(out.all("cmosbyte"), [["22"], ["22"]])
        # a missing file is created blank, without a write
        fresh = self.path("fresh")
        self.drive([f"cmosload {fresh}"])
        with open(fresh, "rb") as f:
            self.assertEqual(f.read(), b"\xFF" * 0x8000)
        # a truncated file stops, naming the path and both sizes
        short = self.path("short")
        with open(short, "wb") as f:
            f.write(b"\0" * 100)
        self.stops([f"cmosload {short}"], short, "100", "32768")
        # without a file the device works in memory only
        out, _ = self.drive([ioctl(3, 0x6300, pos)] + data(DATA, b"\x55\x66\x77\x88")
                            + [call(0x0C0, 3, DATA, 4), "cmosbyte 123", "cmossave"])
        self.assertEqual(out.all("cmosbyte"), [["55"]])

    def test_cmos_saved_at_stop(self):
        """cmos_load registers cmos_save with rt_at_stop: a stop leaves the file holding what
        the game wrote."""
        nv = self.path("nvram")
        self.stops([f"cmosload {nv}", ioctl(3, 0x6300, 0xB6100000)]
                   + data(DATA, b"\x01\x02\x03\x04") + [call(0x0C0, 3, DATA, 4), call(0x048)],
                   "0x048")
        with open(nv, "rb") as f:
            self.assertEqual(f.read(8), b"\x01\x02\x03\x04" + b"\xFF" * 4)

    def test_cmos_stops(self):
        """An unaligned CMOS position (accepted by 0x6300, section 3) stops at the transfer;
        cmos_byte outside the 32 KB stops; 0x6303 is not a command the game issues."""
        self.stops([ioctl(3, 0x6300, 0xB6100001), call(0x090, 3, BUF, 4)], "0xB6100001",
                   "aligned")
        self.stops(["cmosbyte 8000"], "0x8000")
        self.stops([ioctl(3, 0x6303, BUF)], "0x6303", f"ra 0x{CALLER_RA:08X}")

    def test_timer_oneshot(self):
        """timer: one-shot (section 3): after 0x7403 with arg 0, 0x7407 gives the elapsed
        checks x 100 ns rounded down to 20 ns (v1 too); after 335.6 ms exactly 0x13FFFFEC,
        still after 10 s more; after 0x740B and a restart it starts from 0 again."""
        out, _ = self.drive([ioctl(5, 0x7403, 0), "checks 7", ioctl(5, 0x7407, BUF),
                             f"dump {BUF:08X} 1", "advance 335600000", ioctl(5, 0x7407, BUF),
                             f"dump {BUF:08X} 1", "advance 10335600000", ioctl(5, 0x7407, BUF),
                             f"dump {BUF:08X} 1", ioctl(5, 0x740B), ioctl(5, 0x7403, 0),
                             ioctl(5, 0x7407, BUF), f"dump {BUF:08X} 1", "checks 3",
                             ioctl(5, 0x7407, BUF), f"dump {BUF:08X} 1"])
        self.assertEqual(out.v0s(), [0] * 8)
        # v1 is the elapsed value for the reads (section 1, leftovers)
        self.assertEqual([out.rets()[i][1] for i in (1, 2, 3, 6, 7)], [700, SAT, SAT, 0, 300])
        self.assertEqual([d[0] for d in out.dumps()], [700, SAT, SAT, 0, 300])

    def test_timer_periodic(self):
        """timer: timer mode (arg n): elapsed wraps with the period of n counts; timers 1
        and 2 through their own commands (0x7401/0x7405/0x7409, 0x7402/0x7406/0x740A)."""
        out, _ = self.drive([ioctl(5, 0x7401, 1000), "checks FA", ioctl(5, 0x7405, BUF),
                             f"dump {BUF:08X} 1", ioctl(5, 0x7402, 0), "checks 3",
                             ioctl(5, 0x7406, BUF2), f"dump {BUF2:08X} 1",
                             ioctl(5, 0x7405, BUF), f"dump {BUF:08X} 1",
                             ioctl(5, 0x7409), ioctl(5, 0x740A)])
        # 250 checks = 25,000 ns = 1,250 counts: 250 counts into the second period
        self.assertEqual([d[0] for d in out.dumps()], [5000, 300, 5300])
        self.assertEqual(out.v0s(), [0] * 7)
        self.assertEqual([out.rets()[i][1] for i in (1, 3, 4)], [5000, 300, 5300])

    def test_timer_stops(self):
        """Requests whose result is not documented stop: a read of a timer never started
        (since loader_reset), a period above the 24-bit counter, timer 0 (0x7400) and
        0x7404/0x7408."""
        self.stops([ioctl(5, 0x7405, BUF)], "0x7405", "never started")
        self.stops([ioctl(5, 0x7401, 0), ioctl(5, 0x7409), "reset", ioctl(5, 0x7405, BUF)],
                   "0x7405", "never started")
        self.stops([ioctl(5, 0x7401, 0x1000000)], "0x7401", "0x1000000")
        for cmd in (0x7400, 0x7404, 0x7408, 0x740C):
            with self.subTest(cmd=hex(cmd)):
                self.stops([ioctl(5, cmd, BUF)], f"0x{cmd:04X}", f"ra 0x{CALLER_RA:08X}")

    def test_console(self):
        """The console (section 2, tty, and the brief): write(2, "abc\\n", 4) returns 4 and
        the bytes are on the host's standard output and passed to the hook with the fd;
        isatty(1) is 1, isatty(3) 0 (slot 0x0E0); a console read returns 0 (no input)."""
        out, _ = self.drive(["hook"] + data(DATA, b"abc\n") + [call(0x0C0, 2, DATA, 4)]
                            + data(DATA + 8, b"xyz\n") + [call(0x0C0, 1, DATA + 8, 4),
                                                          call(0x0E0, 1), call(0x0E0, 3),
                                                          call(0x0E0, 0), call(0x0E0, 16),
                                                          call(0x0E0, 6),
                                                          call(0x090, 0, BUF, 4),
                                                          call(0x018, 0), call(0x0E0, 0)])
        self.assertEqual(out.v0s(), [4, 4, 1, 0, 1, 0, 0, 0, 0, 1])
        lines = out.text.splitlines()
        self.assertEqual(lines[:2], ["abc", "hook 2 6162630A"])
        self.assertEqual(lines[3:5], ["xyz", "hook 1 78797A0A"])
        self.stops([ioctl(0, 0x6602)], "0x6602", f"ra 0x{CALLER_RA:08X}")

    def test_unknown_requests_stop(self):
        """Slot 0x048 (not one of the 29), ioctl(4, 0x690B) and ioctl(5, 0x7400) each stop
        with a message naming the slot or command, the arguments and ra; none returns."""
        _, msg = self.stops([call(0x048, 1, 2, 3, 4)], "0x048", "a0 0x00000001",
                            "a3 0x00000004", f"ra 0x{CALLER_RA:08X}")
        self.assertIn("not a loader slot", msg)
        self.stops([ioctl(4, 0x690B, BUF)], "0x690B", "fd 4", "a1 0x0000690B",
                   f"ra 0x{CALLER_RA:08X}")
        self.stops([ioctl(5, 0x7400, BUF)], "0x7400", "fd 5", f"ra 0x{CALLER_RA:08X}")
        self.stops([call(0x0C9)], "0x0C9", "not a loader slot")
        self.stops([call(0x240)], "0x240", "not a loader slot")

    def test_exit_exec(self):
        """_exit (slot 0x0D8) stops naming the status; exec (0x198) stops naming the file."""
        self.stops([call(0x0D8, 7)], "0x0D8", "_exit(7)", f"ra 0x{CALLER_RA:08X}")
        self.stops(string(NAME, "diag.exe") + [call(0x198, NAME, 0, 0)], "0x198",
                   '"diag.exe"', f"ra 0x{CALLER_RA:08X}")

    def test_slots_from_handler(self):
        """An ioctl issued while EXL is set, from a synthetic line-0 handler registered
        through slot 0x200, works and delivers nothing: one VBLANK, one handler call; the
        INTCTL read inside sees bit 14 cleared (the dispatcher clears it while handling)."""
        handler = asm(A("addiu", rt=SP, rs=SP, imm=-8), A("sw", rt=RA, base=SP, imm=0),
                      A("ori", rt=A0, rs=0, imm=4), A("ori", rt=A1, rs=0, imm=0x6910),
                      li(A2, BUF), A("jal", target=STUB + 0x0C8), NOP,
                      li(T0, BUF2), A("sw", rt=V0, base=T0, imm=0),
                      A("ori", rt=A0, rs=0, imm=5), A("ori", rt=A1, rs=0, imm=0x7403),
                      A("jal", target=STUB + 0x0C8), A("or", rd=A2, rs=0, rt=0),
                      A("ori", rt=A0, rs=0, imm=5), A("ori", rt=A1, rs=0, imm=0x7407),
                      li(A2, BUF + 4), A("jal", target=STUB + 0x0C8), NOP,
                      call_probe(1),
                      A("lw", rt=RA, base=SP, imm=0), A("addiu", rt=SP, rs=SP, imm=8),
                      ret(1))
        loops = VBLANK_PERIOD_NS // NS_PER_CHECK + 20000
        out, _ = self.drive(place(MAIN, LOOP) + place(H, handler)
                            + [f"r {SP:X} 80700000", call(0x200, 0, H), f"r {T0:X} {loops:X}",
                               f"run {MAIN:08X}", f"dump {BUF:08X} 2", f"dump {BUF2:08X} 1",
                               "state"])
        self.assertEqual(len(out.probes()), 1)
        [p] = out.probes()
        self.assertTrue(p["status"] & 2)  # EXL
        self.assertTrue(VBLANK_PERIOD_NS <= p["now"] < VBLANK_PERIOD_NS + 1000)
        intctl, elapsed = out.dumps()[0]
        self.assertEqual(intctl, 0)
        self.assertLess(elapsed, 1000)
        self.assertEqual(out.dumps()[1], [0])
        self.assertEqual(out.m()["intctl"], 0x4000)

    # --- the other slots and commands ---------------------------------------------------

    def test_open_resolution(self):
        """open (slot 0x088, section 2): the name table in its order with the prefix match
        in both directions; the lowest free descriptor; the console's flags carry 0x08;
        the high bits of the mode are ignored; with no descriptor free, -1."""
        cmds = (open_("con:", 1) + ["desc 6", call(0x0E0, 6)] + open_("c", 0)
                + ["desc 7", call(0x0E0, 7)] + open_("timer:", 2) + [ioctl(8, 0x7403, 0)]
                + open_("pic:", 0) + [ioctl(9, 0x7000, BUF), f"dump {BUF:08X} 1"]
                + open_("ioasic:", 2) + open_("cmos:", 0x12) + ["desc B"] + open_("pci:", 2)
                + [call(0x018, 7)] + open_("con:", 0) + open_("con:", 0) + open_("con:", 0)
                + open_("con:", 0) + open_("con:", 0))
        out, _ = self.drive(cmds)
        self.assertEqual(out.v0s(), [6, 1, 7, 1, 8, 0, 9, 0, 10, 11, 12, 0, 7, 13, 14, 15, M1])
        self.assertEqual(out.all("desc"), [["0A"], ["09"], ["03"]])
        self.assertEqual(out.dumps(), [[0x0001E240]])
        for name in ("led:", "", "mdiag:", "c:", "file:x", "scsi:", "wdog:", "snd:"):
            with self.subTest(name=name):
                self.stops(open_(name, 0), "0x088", f'"{name}"')
        self.stops(open_("con:", 3), "0x088", "mode 0x3")

    def test_creat_write_unlink_rename(self):
        """creat (slot 0x028) opens for writing and stores the fd; write (0x0C0) on a file
        returns n, or 0 when n % 4 != 0; the file reads back; unlink (0x190) returns 0, also
        for an absent name; rename (0x1B8) returns 0xFFFFDBB7 for an absent name, else 0."""
        out, _ = self.drive(self.image() + string(NAME, "new.dat")
                            + [call(0x028, NAME, 0x20, FDP), f"dump {FDP:08X} 1",
                               f"peek {NAME:08X} 8"]
                            + data(DATA, b"12345678")
                            + [call(0x0C0, 6, DATA, 8), call(0x0C0, 6, DATA, 3),
                               call(0x018, 6)]
                            + open_("NEW.DAT", 0) + [call(0x090, 6, BUF, 0x100),
                                                     f"peek {BUF:08X} 8", call(0x018, 6)]
                            + string(NAME, "new.dat") + [call(0x190, NAME)]
                            + open_("NEW.DAT", 0) + [call(0x190, NAME)]
                            + string(NAME, "nosuch.dat") + string(NAME + 0x20, "x.dat")
                            + [call(0x1B8, NAME, NAME + 0x20)]
                            + string(NAME, "a.bin") + string(NAME + 0x20, "c.bin")
                            + [call(0x1B8, NAME, NAME + 0x20)] + open_("C.BIN", 0, NAME + 0x40))
        self.assertEqual(out.rets()[0], (0, 0))
        self.assertEqual(out.v0s()[1:], [8, 0, 0, 6, 8, 0, 0, M1, 0, ENOENT, 0, 6])
        self.assertEqual(out.dumps(), [[6]])
        self.assertEqual(out.peeks(), [b"NEW.DAT\0", b"12345678"])
        # creat without a disk: the fd word gets -1 and the slot returns -1 (v1 too)
        out, _ = self.drive(string(NAME, "new.dat") + [call(0x028, NAME, 0, FDP),
                                                       f"dump {FDP:08X} 1"])
        self.assertEqual(out.rets(), [(M1, M1)])
        self.assertEqual(out.dumps(), [[M1]])

    def test_misc_slots(self):
        """dcache flush (slot 0x0E8) does nothing and leaves v0; the argument block (0x1A8)
        is 0x800B6E30 with its 4 words zero; the break hooks (0x1F0, 0x1F8) are stored."""
        out, _ = self.drive([f"r {V0:X} {MARK:08X}", call(0x0E8), "fill 800B6E30 10 77",
                             call(0x1A8), "dump 800B6E30 4", f"r {V0:X} {MARK:08X}",
                             call(0x1F0, FN), call(0x1F8, FN + 4), "handlers"])
        self.assertEqual(out.v0s(), [MARK, 0x800B6E30, MARK, MARK])
        self.assertEqual(out.dumps(), [[0, 0, 0, 0]])
        h = out.handlers()
        self.assertEqual((h["break7"], h["break6"]), (FN, FN + 4))

    def test_irq_set_handler(self):
        """irq_set_handler (slot 0x200): line 0 starts VBLANK once (a second registration
        does not restart it) and ORs IM6 and IE into Status, line 3 IM3; v0 is 8; fn 0 masks
        the line again; a line no IP maps to stops."""
        out, _ = self.drive(place(H, asm(call_probe(1), ret(1)))
                            + [call(0x200, 0, H), "state", "checks 3E8",
                               call(0x200, 0, H), call(0x200, 3, H + 0x40), "state",
                               "handlers", f"advance {VBLANK_PERIOD_NS + 5000}",
                               call(0x200, 3, 0), "state"])
        self.assertEqual(out.v0s(), [8, 8, 8, 8])
        ms = out.all("m")
        self.assertEqual(int(ms[0][0], 16), STATUS | IM6)
        self.assertEqual(int(ms[0][5]), VBLANK_PERIOD_NS)
        self.assertEqual(int(ms[1][0], 16), STATUS | IM6 | IM3)
        self.assertEqual(int(ms[2][0], 16), STATUS | IM6)
        self.assertEqual(out.handlers()["line"][:4], [H, 0, 0, H + 0x40])
        # (advance runs whole slices, so it may pass more than one period)
        probes = out.probes()
        self.assertTrue(probes)
        for k, p in enumerate(probes):
            self.assertEqual(p["a0"], IM6)
            self.assertEqual(p["now"] // VBLANK_PERIOD_NS, k + 1)
            self.assertLess(p["now"] % VBLANK_PERIOD_NS, 1000)
        for line in (5, 8):
            with self.subTest(line=line):
                self.stops([call(0x200, line, H)], "0x200", f"line {line}")

    def test_gt_set_handler(self):
        """gt_set_handler (slot 0x1E0): the handler is stored and the bit enabled in
        CPU_MASK; v0 is bit x 4; fn 0 disables it; a bit above 31 stops."""
        out, _ = self.drive([call(0x1E0, 4, FN), "handlers", call(0x1E0, 4, 0), "handlers"])
        self.assertEqual(out.v0s(), [0x10, 0x10])
        first, second = out.handlers(0), out.handlers(1)
        self.assertEqual((first["gt4"], first["gtmask"] & 0x10), (FN, 0x10))
        self.assertEqual((second["gt4"], second["gtmask"] & 0x10), (0, 0))
        self.stops([call(0x1E0, 32, FN)], "0x1E0", "bit 32")

    def test_pic(self):
        """pic: (section 3): 0x7000 to 0x7002 store the three words of the dump; read and
        write are the shared return-0 routine (section 2: only init and ioctl); other
        commands stop."""
        out, _ = self.drive(open_("pic:", 0) + [ioctl(6, 0x7000, BUF), ioctl(6, 0x7001, BUF + 4),
                                                ioctl(6, 0x7002, BUF + 8),
                                                f"dump {BUF:08X} 3", call(0x090, 6, BUF2, 4),
                                                call(0x0C0, 6, BUF2, 4), call(0x018, 6)])
        self.assertEqual(out.v0s(), [6, 0, 0, 0, 0, 0, 0])
        self.assertEqual(out.dumps(), [[0x0001E240, 0x000001D5, 0x00001B88]])
        self.stops(open_("pic:", 0) + [ioctl(6, 0x7003, BUF)], "0x7003")

    def test_timer_stopped_holds(self):
        """A stopped timer holds the elapsed value of the moment it was stopped (section 3,
        inferred): one-shot and timer mode, read twice; a restart reads from 0 again."""
        out, _ = self.drive([ioctl(5, 0x7403, 0), "checks 7", ioctl(5, 0x740B), "checks 64",
                             ioctl(5, 0x7407, BUF), ioctl(5, 0x7407, BUF + 4),
                             ioctl(5, 0x7403, 0), ioctl(5, 0x7407, BUF + 8),
                             # timer mode: 1,250 counts into a 1,000-count period
                             ioctl(5, 0x7401, 1000), "checks FA", ioctl(5, 0x7409),
                             "checks 3E8", ioctl(5, 0x7405, BUF + 0xC),
                             # a run-out one-shot stopped later holds the saturated value
                             ioctl(5, 0x7402, 0), "advance 400000000", ioctl(5, 0x740A),
                             "checks 3E8", ioctl(5, 0x7406, BUF + 0x10),
                             f"dump {BUF:08X} 5"])
        self.assertEqual(out.dumps(), [[700, 700, 0, 5000, SAT]])
        self.assertEqual(out.rets()[3][1], 700)  # v1 too

    def test_timer_read_write(self):
        """timer: read and write are the shared return-0 routine (section 2: only ioctl)."""
        out, _ = self.drive([call(0x090, 5, BUF, 4), call(0x0C0, 5, BUF, 4)])
        self.assertEqual(out.v0s(), [0, 0])

    def test_pci_read_stops(self):
        """pci: (sysctrl) read and write are not documented: they stop."""
        self.stops(open_("pci:", 2) + [call(0x090, 6, BUF, 4)], "0x090", "pci:")

    def test_ioasic(self):
        """ioasic: (section 3): 0x6900 to 0x6903 store the input ports (bus words 3, 4, 2,
        1); 0x6905 is accepted and logged; 0x690E stores INTSTAT, 0x690F writes INTCTL and
        0x6910 reads it back; read and write return 0."""
        out, err = self.drive(["inputs 1111 2222 3333 4444"]
                              + [ioctl(4, 0x6900 + i, BUF + 4 * i) for i in range(4)]
                              + [f"dump {BUF:08X} 4", ioctl(4, 0x6905, 0x10),
                                 ioctl(4, 0x690E, BUF2), ioctl(4, 0x690F, 0x12345),
                                 ioctl(4, 0x6910, BUF2 + 4), f"dump {BUF2:08X} 2", "state",
                                 call(0x090, 4, BUF, 4), call(0x0C0, 4, BUF, 4)],
                              stderr=True)
        self.assertEqual(out.v0s(), [0] * 8 + [0, 0])
        self.assertEqual(out.dumps(), [[0x1111, 0x2222, 0x3333, 0x4444], [0x20C9, 0x2345]])
        self.assertEqual(out.m()["intctl"], 0x2345)
        self.assertEqual(err, f"carnevil: ioasic: ioctl 0x6905 (COIN register write) 0x10, "
                              f"ra 0x{CALLER_RA:08X}\n")
        for cmd in (0x6906, 0x6907, 0x6908, 0x6909, 0x690A, 0x6911):
            with self.subTest(cmd=hex(cmd)):
                self.stops([ioctl(4, cmd, BUF)], f"sound board ioctl 0x{cmd:04X} is not "
                           "implemented yet")
        for cmd in (0x6904, 0x690B, 0x690C, 0x690D, 0x6912):
            with self.subTest(cmd=hex(cmd)):
                self.stops([ioctl(4, cmd, BUF)], f"0x{cmd:04X}", f"ra 0x{CALLER_RA:08X}")

    def test_input_defaults(self):
        """With no ioasic_set_inputs, 0x6900 to 0x6903 give the measured no-input values
        (section 3: 0xBDFE, then 0xFFFF three times); ioasic_set_inputs overrides them and
        loader_reset brings them back."""
        reads = [ioctl(4, 0x6900 + i, BUF + 4 * i) for i in range(4)] + [f"dump {BUF:08X} 4"]
        out, _ = self.drive(reads + ["inputs 1 2 3 4"] + reads + ["reset"] + reads)
        defaults = [0xBDFE, 0xFFFF, 0xFFFF, 0xFFFF]
        self.assertEqual(out.dumps(), [defaults, [1, 2, 3, 4], defaults])

    def test_generic_ioctls(self):
        """0x6601 sets (arg non-zero) or clears flag 0x04 and returns 0 without the driver;
        0x6603 and 0x6604 change flag 0x20 and go on to the driver, and no driver here has
        them (section 3 documents none), so they stop; a free descriptor gives -1."""
        out, _ = self.drive([ioctl(0, 0x6601, 1), "desc 0", ioctl(0, 0x6601, 0), "desc 0",
                             ioctl(7, 0x6601, 1), ioctl(16, 0x6601, 1)])
        self.assertEqual(out.v0s(), [0, 0, M1, M1])
        self.assertEqual(out.all("desc"), [["0D"], ["09"]])
        self.stops([ioctl(4, 0x6603, 0)], "0x6603", "ioasic:")
        self.stops([ioctl(3, 0x6604, 0)], "0x6604", "cmos:")

    def test_bad_guest_memory(self):
        """A guest pointer outside RAM reaches the bus, which stops; an unterminated string
        stops."""
        self.stops([call(0x090, 3, 0x10000000, 4)], "bus write", "0x10000000")
        self.stops([call(0x0C0, 1, 0x1F000000, 4)], "bus read", "0x1F000000")
        self.stops([call(0x088, 0x1F000000, 0)], "bus read", "0x1F000000")
        self.stops([f"fill {NAME:08X} 100 41", call(0x088, NAME, 0)], "0x088",
                   "unterminated")
        # a result pointer the loader's sw could not store through
        self.stops([ioctl(3, 0x6301, BUF + 1)], "0x0C8", f"0x{BUF + 1:08X}", "not word aligned")
        # a console write longer than RAM
        self.stops([call(0x0C0, 1, BUF, 0x900000)], "0x0C0", "more bytes than RAM")

    def test_long_file_names_stop(self):
        """A file name longer than the directory's 12 characters stops (its 8.3 form is not
        documented), for each slot that takes one."""
        name = "LONGFILENAME.BIN"
        for cmds in (open_(name, 0), string(NAME, name) + [call(0x038, NAME, 0, BUF)],
                     string(NAME, name) + [call(0x190, NAME)],
                     string(NAME, "A.BIN") + string(NAME + 0x40, name)
                     + [call(0x1B8, NAME, NAME + 0x40)]):
            with self.subTest(cmd=cmds[-1]):
                self.stops(self.image() + cmds, f'"{name}"', "12 characters")

    def test_file_access_modes(self):
        """A file opened for reading refuses writes with -1 (section 2: "-1 on failure"); a
        read on a file opened for writing is not documented and stops; ioctl on a file stops
        (the game issues none)."""
        out, _ = self.drive(self.image() + open_("A.BIN", 0) + [call(0x0C0, 6, BUF, 4)])
        self.assertEqual(out.v0s(), [6, M1])
        self.stops(self.image() + open_("B.BIN", 1) + [call(0x090, 6, BUF, 4)], "0x090",
                   "fd 6", "not open for reading", f"ra 0x{CALLER_RA:08X}")
        self.stops(self.image() + open_("A.BIN", 0) + [ioctl(6, 0x99F6, 0)], "0x99F6",
                   "file:")

    def test_loader_reset(self):
        """loader_reset returns descriptors, positions, timers, inputs, the CMOS and the hook
        to their start-up values."""
        out, _ = self.drive(self.image() + ["hook", "inputs 1 2 3 4", call(0x018, 3)]
                            + open_("A.BIN", 0) + open_("pci:", 2) + [ioctl(5, 0x7401, 0)]
                            + ["reset"] + self.image()
                            + [ioctl(3, 0x6301, BUF), f"dump {BUF:08X} 1"]
                            + open_("pci:", 2) + data(DATA, b"ok\n\0")
                            + [call(0x0C0, 1, DATA, 3), "cmosbyte 0"])
        # (fd 3, closed, is the lowest free descriptor before the reset)
        self.assertEqual(out.v0s(), [0, 3, 6, 0, 0, 6, 3])
        self.assertEqual(out.dumps(), [[0xB6100000]])
        self.assertEqual(out.all("hook"), [])
        self.assertEqual(out.all("cmosbyte"), [["FF"]])
        self.stops(["inputs 1 2 3 4", ioctl(5, 0x7401, 0), "reset", ioctl(5, 0x7405, BUF)],
                   "never started")

    def test_cmos_reload_rejected(self):
        """A second cmos_load of a file of the wrong size stops before anything changes: the
        rejected file keeps its bytes and modification time, and the stop's save writes the
        cells to the file loaded first."""
        a, b = self.path("a.nv"), self.path("b.nv")
        with open(b, "wb") as f:
            f.write(b"\x5A" * 100)
        os.utime(b, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        before = os.stat(b)
        self.stops([f"cmosload {a}", ioctl(3, 0x6300, 0xB6100000)]
                   + data(DATA, b"\x01\x02\x03\x04") + [call(0x0C0, 3, DATA, 4),
                                                        f"cmosload {b}"], b, "100")
        with open(b, "rb") as f:
            self.assertEqual(f.read(), b"\x5A" * 100)
        self.assertEqual(os.stat(b).st_mtime_ns, before.st_mtime_ns)
        with open(a, "rb") as f:
            self.assertEqual(f.read(), b"\x01\x02\x03\x04" + b"\xFF" * (0x8000 - 4))

    def test_cmos_file_errors(self):
        """An NVRAM file that cannot be created stops, naming it."""
        nv = os.path.join(self.path("no-such-dir"), "nvram")
        self.stops([f"cmosload {nv}"], nv, "cannot write")


@requires_file("carnevil.raw")
@requires_file("parts", "part0")
class GameDiskTest(LoaderTestCase):
    """The game's own disk: FONT0001.WMS, which the game finds and reads in one call
    (loader-calls.md), against the Python extractor reading parts/part0."""

    def disk_cmds(self):
        return [f"img {work_path('carnevil.raw')} {self.path('game.ovl')}"]

    def test_findfirst(self):
        out, _ = self.drive(self.disk_cmds() + [f"fill {BUF:08X} 40 AA"]
                            + string(NAME, "game.env") + [call(0x038, NAME, 0, BUF)]
                            + string(NAME, "FONT0001.WMS") + [call(0x038, NAME, 0, BUF),
                                                              f"peek {BUF:08X} 40"])
        self.assertEqual(out.v0s(), [M1, 0])
        [buf] = out.peeks()
        self.assertEqual(int.from_bytes(buf[26:30], "little"), 0x2AAD4)
        self.assertEqual(buf[30:43], b"FONT0001.WMS\0")

    def test_read_rules(self):
        part0 = work_path("parts", "part0")
        scanned = {e.name: (e, b) for e, b in disk._scan(part0)}
        with open(part0, "rb") as f:
            want = disk._read_blocks(f, *scanned["FONT0001.WMS"])
        self.assertEqual(len(want), 0x2AAD4)
        out, _ = self.drive(self.disk_cmds() + open_("FONT0001.WMS", 0)
                            + [call(0x090, 6, 0x80300000, 0x2AAD4),
                               f"sha 80300000 {0x2AAD4:X}", call(0x090, 6, BUF, 4),
                               call(0x018, 6)])
        self.assertEqual(out.v0s(), [6, 0x2AAD4, EOF, 0])
        self.assertEqual(out.all("sha"), [[hashlib.sha256(want).hexdigest()]])


@requires_file("mame", "m2-facts", "inputs.txt")
@requires_file("mame", "m2-facts", "entry-hw.txt")
class MeasuredFactsTest(LoaderTestCase):
    """The measured start-up values (tools/mame/m2facts.lua's facts, on the share only)."""

    def test_input_ports(self):
        """ioctls 0x6900 to 0x6903 return the ports ioasic_set_inputs gives, here the values
        the game read with no input in MAME."""
        values = {}
        with open(work_path("mame", "m2-facts", "inputs.txt")) as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("#"))
        for line in lines[1:]:
            source, value = line.split()[:2]
            if int(source, 16) in range(0x6900, 0x6904):
                self.assertNotIn(source, values, "one value per port with no input")
                values[int(source, 16)] = int(value, 16) & 0xFFFF
        ports = [values[0x6900 + i] for i in range(4)]
        out, _ = self.drive(["inputs " + " ".join(f"{p:X}" for p in ports)]
                            + [ioctl(4, 0x6900 + i, BUF + 4 * i) for i in range(4)]
                            + [f"dump {BUF:08X} 4"])
        self.assertEqual(out.dumps(), [ports])

    def test_entry_hardware(self):
        """INTCTL at reset, the idle INTSTAT of ioctl 0x690E and the blank CMOS byte are the
        values measured at the game's entry."""
        facts = {}
        with open(work_path("mame", "m2-facts", "entry-hw.txt")) as f:
            for line in f.read().splitlines()[1:]:
                fields = line.split()
                facts[fields[0]] = int(fields[1], 16)
        out, _ = self.drive([ioctl(4, 0x6910, BUF), ioctl(4, 0x690E, BUF + 4),
                             f"dump {BUF:08X} 2", "cmosbyte 0", "cmosbyte 7FFF"])
        self.assertEqual(out.dumps(), [[facts["0xB6000020"], facts["0xB600001C"]]])
        blank = f"{facts['0xB6100000']:02X}"
        self.assertEqual(out.all("cmosbyte"), [[blank], [blank]])


if __name__ == "__main__":
    unittest.main()
