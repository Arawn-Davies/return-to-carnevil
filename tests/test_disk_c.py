"""runtime/disk in C: the raw image with its overlay, and the file system on partition 1.

The C runs in tests/harness/disk_driver.c (its top comment lists the commands), built with
the global C flags, -Wall -Wextra -Werror and the sanitizer flags into a cache under the
system temp dir. Synthetic images and every overlay live in a TemporaryDirectory; the game
data is only ever opened read-only, and the tests check that the raw image is unchanged.
"""
import hashlib
import os
import random
import shutil
import struct
import subprocess
import tempfile
import unittest

from carnevil_tools import disk
from tests import charness
from tests.gamedata import requires_file, work_path

REPO = charness.REPO
DISK_DIR = os.path.join(REPO, "runtime", "disk")
SOURCES = [os.path.join(DISK_DIR, f) for f in ("image.c", "fs.c", "sha256.c")]
DRIVER = os.path.join(REPO, "tests", "harness", "disk_driver.c")
CACHE = os.path.join(tempfile.gettempdir(), "carnevil-disk-driver")

GAME_EXE_SHA256 = "f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6"
EOF = -9298  # 0xFFFFDBAE
ENOENT = -9289  # 0xFFFFDBB7
ESEEK = -9297  # 0xFFFFDBAF

SECTOR = 512
BLOCK = 4096
BASE_SECTOR = 138  # block b at partition sector 138 + 8 * b
FAT = 67 * SECTOR
HOME_MAGIC = 0x34011280
OVERLAY_HEADER = 512
OVERLAY_RECORD = 520


def build_driver():
    """Path of the disk driver binary. Raises unittest.SkipTest without `cc`."""
    cc = charness._cc()
    flags = charness.CFLAGS + charness.RUNTIME_WARNINGS + charness.SANITIZE
    h = hashlib.sha256()
    st = os.stat(os.path.realpath(cc))
    h.update(f"{os.path.realpath(cc)}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    h.update("\0".join(flags).encode())
    for path in SOURCES + [DRIVER, os.path.join(DISK_DIR, "disk.h")]:
        with open(path, "rb") as f:
            h.update(path.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    out = os.path.join(CACHE, h.hexdigest()[:32])
    exe = os.path.join(out, "disk_driver")
    if os.path.exists(exe):
        return exe
    os.makedirs(CACHE, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=CACHE, prefix="build-")
    try:
        objs = []
        for i, src in enumerate(SOURCES + [DRIVER]):
            obj = os.path.join(tmp, f"{i}-{os.path.basename(src)}.o")
            charness._cc_run([cc, *flags, "-I", DISK_DIR, "-c", src, "-o", obj])
            objs.append(obj)
        charness._cc_run([cc, *charness.SANITIZE, *objs, "-o", os.path.join(tmp, "disk_driver")])
        try:
            os.rename(tmp, out)
        except OSError:  # another process built the same key first
            if not os.path.exists(exe):
                raise
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    return exe


def run_driver(raw, overlay, commands=()):
    """(open result line, [answer lines]) of the driver on `commands`; on a failed open the
    answer list is [the message]."""
    res = subprocess.run([build_driver(), raw, overlay or "-"],
                         input="".join(c + "\n" for c in commands), capture_output=True,
                         text=True)
    lines = res.stdout.splitlines()
    if res.returncode not in (0, 1) or res.stderr or not lines:
        raise AssertionError(f"driver status {res.returncode}: {res.stderr}")
    return lines[0], lines[1:]


def pattern(n, seed):
    return bytes((seed + 7 * i) & 255 for i in range(n))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def raw_header(sizes, total):
    """Sector 0 of a raw image: TRAP, the partition count, and {0, start, size} entries;
    partitions are placed from sector 8 on."""
    head = bytearray(SECTOR)
    head[0:4] = b"TRAP"
    struct.pack_into("<i", head, 4, len(sizes))
    start = 8
    for i, size in enumerate(sizes):
        struct.pack_into("<ii", head, 0x10 + 12 * i + 4, start, size)
        start += size
    assert start <= total
    return bytes(head)


def synthetic_image(sectors=64, seed=1, part=56):
    data = bytearray(random.Random(seed).randbytes(sectors * SECTOR))
    data[:SECTOR] = raw_header([part], sectors)
    return bytes(data)


def encode_name(name):
    raw = name.encode().ljust(12, b"\0")
    return b"".join(raw[i:i + 4][::-1] for i in range(0, 12, 4))


def fs_partition(entries, files, nblocks=40):
    """A partition of `nblocks` blocks: `entries` is the root directory (block 3), a list of
    (name or None for a free entry, length in bytes, first block); `files` maps a block list
    to its bytes. Blocks no file and not the directory uses are free."""
    part = bytearray((BASE_SECTOR + 8 * nblocks) * SECTOR)
    struct.pack_into("<I", part, 2 * SECTOR, HOME_MAGIC)
    used = {3: 0}
    for blocks, payload in files:
        for i, b in enumerate(blocks):
            start = (BASE_SECTOR + 8 * b) * SECTOR
            piece = payload[i * BLOCK:(i + 1) * BLOCK]
            part[start:start + len(piece)] = piece
            used[b] = blocks[i + 1] if i + 1 < len(blocks) else 0
    for b in range(3, nblocks):
        struct.pack_into("<H", part, FAT + 2 * (b - 3), used.get(b, 1))
    d = (BASE_SECTOR + 8 * 3) * SECTOR
    for i, (name, size, first) in enumerate(entries):
        raw = encode_name(name or "X") + struct.pack("<iIi", size // 4, 0x254E7BEE, first)
        if name is None:
            raw = b"\0\0\0\0" + raw[4:]
        part[d + 24 * i:d + 24 * (i + 1)] = raw
    return bytes(part)


def fs_image(part):
    sectors = 8 + len(part) // SECTOR
    return raw_header([len(part) // SECTOR], sectors) + bytes(7 * SECTOR) + part


def fat_entry(part, b):
    return struct.unpack_from("<H", part, FAT + 2 * (b - 3))[0]


class DriverTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, name, data):
        with open(self.path(name), "wb") as f:
            f.write(data)
        return self.path(name)

    def opened(self, raw, overlay, commands=()):
        status, lines = run_driver(raw, overlay, commands)
        self.assertEqual(status, "open 0", lines)
        self.assertEqual(len(lines), len(commands) + commands.count("header"), lines)
        return lines

    def refused(self, raw, overlay, *fragments):
        status, lines = run_driver(raw, overlay, ["sectors"])
        self.assertEqual(status, "open -1")
        self.assertEqual(len(lines), 1, lines)
        for fragment in fragments:
            self.assertIn(fragment, lines[0])
        return lines[0]


class ImageTests(DriverTestCase):
    def test_overlay_created_on_first_open_and_holds_writes(self):
        image = synthetic_image()
        raw = self.write("img.raw", image)
        before = os.stat(raw)
        ovl = self.path("img.ovl")
        self.assertEqual(self.opened(raw, ovl, ["sectors"]), ["sectors 64"])
        with open(ovl, "rb") as f:
            head = f.read()
        self.assertEqual(len(head), OVERLAY_HEADER)
        self.assertEqual(head[:8], b"CEOVRLAY")
        self.assertEqual(struct.unpack_from("<IIQ", head, 8), (1, 0, len(image)))
        self.assertEqual(head[24:56], hashlib.sha256(image[:SECTOR]).digest())

        lines = self.opened(raw, ovl, ["write 10 2 171", "read 10 2", "read 9 1",
                                       "write 11 1 5", "read 10 2"])
        self.assertEqual(lines[0], "write 0")
        self.assertEqual(lines[1], "read 0 " + sha(b"\xab" * 1024))
        self.assertEqual(lines[2], "read 0 " + sha(image[9 * SECTOR:10 * SECTOR]))
        self.assertEqual(lines[4], "read 0 " + sha(b"\xab" * 512 + b"\x05" * 512))
        # a sector rewritten keeps its one record
        self.assertEqual(os.path.getsize(ovl), OVERLAY_HEADER + 2 * OVERLAY_RECORD)
        # a new process sees the overlay's sectors over the image's
        self.assertEqual(self.opened(raw, ovl, ["read 9 3"]),
                         ["read 0 " + sha(image[9 * SECTOR:10 * SECTOR] + b"\xab" * 512
                                          + b"\x05" * 512)])
        with open(raw, "rb") as f:
            self.assertEqual(f.read(), image)
        after = os.stat(raw)
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))

    def test_ranges_partitions_and_header(self):
        image = synthetic_image()
        raw = self.write("img.raw", image)
        lines = self.opened(raw, self.path("o"), [
            "read 64 1", "read 63 2", "read 63 1", "read 4294967295 2", "write 64 1 1",
            "write 4294967295 2 1", "part 0", "part 1", "part 2", "part -1", "header"])
        self.assertEqual(lines[:6], ["read -1 -", "read -1 -", "read 0 " + sha(image[-SECTOR:]),
                                     "read -1 -", "write -1", "write -1"])
        self.assertEqual(lines[6:10], ["part 0 0 64", "part 0 8 56", "part -1 0 0",
                                       "part -1 0 0"])
        patched = bytearray(image[:SECTOR])
        struct.pack_into("<I", patched, 0xC, 64)  # the sector count, as the loader patches it
        self.assertEqual(lines[10:], ["header " + sha(bytes(patched)), "word 64"])

    def test_without_an_overlay_writes_fail(self):
        raw = self.write("img.raw", synthetic_image())
        self.assertEqual(self.opened(raw, None, ["write 1 1 1", "read 1 1"])[0], "write -1")

    def test_overlay_for_another_image_is_refused(self):
        raw = self.write("a.raw", synthetic_image(seed=1))
        ovl = self.path("a.ovl")
        self.opened(raw, ovl, ["write 3 1 7"])
        a_hash = sha(synthetic_image(seed=1)[:SECTOR])

        other = bytearray(synthetic_image(seed=1))
        other[8] ^= 1  # same size, another first sector
        b = self.write("b.raw", bytes(other))
        self.refused(b, ovl, "made for an image of 32768 bytes", a_hash,
                     "this image of 32768 bytes", sha(bytes(other[:SECTOR])))

        bigger = synthetic_image(sectors=128, seed=1)
        c = self.write("c.raw", bigger)
        self.refused(c, ovl, "32768", "65536", a_hash, sha(bigger[:SECTOR]))

    def test_damaged_overlays_are_refused(self):
        raw = self.write("img.raw", synthetic_image())
        ovl = self.path("img.ovl")
        self.opened(raw, ovl, ["write 3 2 7"])
        with open(ovl, "rb") as f:
            good = f.read()

        def refused_with(data, *fragments):
            self.write("bad.ovl", data)
            return self.refused(raw, self.path("bad.ovl"), *fragments)

        refused_with(good[:-100], "truncated", "2 records")
        refused_with(good[:100], "truncated", "shorter than its header")
        refused_with(good + b"\0" * 520, "after its 2 records")
        refused_with(b"NOTOVRLY" + good[8:], "bad magic")
        refused_with(good[:8] + struct.pack("<I", 2) + good[12:], "format version 2")
        bad_lba = bytearray(good)
        struct.pack_into("<I", bad_lba, OVERLAY_HEADER, 64)
        refused_with(bytes(bad_lba), "record 0 names sector 64")

    def test_bad_raw_images_are_refused(self):
        self.refused(self.path("missing.raw"), self.path("o1"), "cannot open raw image",
                     "missing.raw")
        self.refused(self.write("odd.raw", b"\0" * 1000), self.path("o2"),
                     "1000 bytes, not a whole number of 512-byte sectors")
        self.refused(self.write("empty.raw", b""), self.path("o3"),
                     "too small to hold the disk header")
        self.refused(self.write("nomagic.raw", b"\0" * 1024), self.path("o4"), "no TRAP magic")
        raw = self.write("img.raw", synthetic_image())
        self.refused(raw, raw, "is the raw image itself")
        for name in ("o1", "o2", "o3", "o4"):
            self.assertFalse(os.path.exists(self.path(name)), name)

    def test_sha256(self):
        lines = self.opened(self.write("img.raw", synthetic_image()), None,
                            ["sha abc", "sha -", "shaa 1000000", "shaa 55", "shaa 56",
                             "shaa 64", "shaa 119"])
        self.assertEqual(lines[0], "sha " + sha(b"abc"))
        self.assertEqual(lines[0], "sha ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(lines[1], "sha " + sha(b""))
        self.assertEqual(lines[2], "shaa cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0")
        for line, n in zip(lines[3:], (55, 56, 64, 119)):
            self.assertEqual(line, "shaa " + sha(b"a" * n))


class SyntheticFileSystemTests(DriverTestCase):
    def setUp(self):
        super().setUp()
        self.a = random.Random(2).randbytes(5000)
        self.b = random.Random(3).randbytes(300)
        self.part = fs_partition(
            [("A.BIN", 5000, 4), (None, 0x2000, 6), ("B.BIN", 300, 7)],
            [([4, 5], self.a), ([7], self.b)])
        self.raw = self.write("fs.raw", fs_image(self.part))
        self.ovl = self.path("fs.ovl")

    def dump(self):
        out = self.path("dump")
        self.assertEqual(self.opened(self.raw, self.ovl, [f"dump 1 {out}"]), ["dump 0"])
        with open(out, "rb") as f:
            return out, f.read()

    def test_find_read_and_handles(self):
        cmds = ["stat A.BIN", "stat a.bin", "stat B.BIN", "stat game.env", "stat X",
                "stat ABCDEFGHIJKLM", "hash A.BIN", "hash B.BIN"]
        cmds += ["open B.BIN 3", "open B.BIN 4", "open B.BIN -1", "open NONE.BIN 0"]
        cmds += ["open A.BIN 0"] * 17 + ["close 99", "rd 99 4", "wr 0 4 1", "seek 99 0 0"]
        lines = self.opened(self.raw, self.ovl, cmds)
        self.assertEqual(lines[:6], ["stat 0 5000 0 31726 9550", "stat 0 5000 0 31726 9550",
                                     "stat 0 300 0 31726 9550", "stat -1 0 0 0 0",
                                     "stat -1 0 0 0 0", "stat -1 0 0 0 0"])
        self.assertEqual(lines[6:8], ["hash 0 5000 " + sha(self.a), "hash 0 300 " + sha(self.b)])
        self.assertEqual(lines[8:12], ["open -1"] * 4)
        self.assertEqual(lines[12:29], [f"open {i}" for i in range(16)] + ["open -1"])
        self.assertEqual(lines[29:], ["close -1", "rd -1 " + sha(b""), "wr -1", "seek 0"])

    def test_write_read_seek_unlink_rename(self):
        new = pattern(0x2084, 5)
        lines = self.opened(self.raw, self.ovl, [
            "open NEW.BIN 1", "wr 0 8324 5", "wr 0 3 5", "rd 0 4", "close 0",
            "open new.bin 0", "rd 0 8324", "rd 0 4", "rd 0 3", "wr 0 4 1",
            "seek 0 8 0", "seek 0 9 0", "seek 0 -4 1", "seek 0 100 2", "seek 0 0 3",
            "seek 0 -8 0", "seek 0 0 0", "rd 0 8", "seek 0 4096 0", "rd 0 12", "close 0",
            "stat NEW.BIN", "unlink A.BIN", "stat A.BIN", "unlink A.BIN", "rename B.BIN C.BIN",
            "stat B.BIN", "stat C.BIN", "rename B.BIN X", "open D.BIN 2", "wr 0 8 9",
            "seek 0 0 0", "rd 0 8", "close 0"])
        self.assertEqual(lines[:5], ["open 0", "wr 8324", "wr 0", "rd -1 " + sha(b""), "close 0"])
        self.assertEqual(lines[5:10], ["open 0", "rd 8324 " + sha(new), f"rd {EOF} " + sha(b""),
                                       "rd 0 " + sha(b""), "wr -1"])
        self.assertEqual(lines[10:17], ["seek 8", "seek 8", "seek 4", "seek 8324", "seek 0",
                                        f"seek {ESEEK}", "seek 0"])
        self.assertEqual(lines[17:21], ["rd 8 " + sha(new[:8]), "seek 4096",
                                        "rd 12 " + sha(new[4096:4108]), "close 0"])
        self.assertEqual(lines[21], "stat 0 8324 0 0 9505")  # FS_DATE 0x2521, not the loader's
        self.assertEqual(lines[22:25], ["unlink 0", "stat -1 0 0 0 0", "unlink 0"])
        self.assertEqual(lines[25:29], ["rename 0", "stat -1 0 0 0 0", "stat 0 300 0 31726 9550",
                                        f"rename {ENOENT}"])
        self.assertEqual(lines[29:], ["open 0", "wr 8", "seek 0", "rd 8 " + sha(pattern(8, 9)),
                                      "close 0"])

        # The partition as written, read by the Python reader.
        path, part = self.dump()
        entries = disk.list_files(path)
        # NEW.BIN took the free entry at slot 1, D.BIN the one A.BIN left at slot 0
        self.assertEqual([(e.name, e.size) for e in entries],
                         [("D.BIN", 8), ("NEW.BIN", 0x2084), ("C.BIN", 300)])
        out = self.path("out")
        disk.extract_files(path, out)
        for name, want in (("D.BIN", pattern(8, 9)), ("NEW.BIN", new), ("C.BIN", self.b)):
            with open(os.path.join(out, name), "rb") as f:
                self.assertEqual(f.read(), want, name)
        # First fit: NEW.BIN took block 6 (free already) and 8, 9; A.BIN's blocks 4 and 5
        # were freed by the unlink, and D.BIN took 4.
        blocks = {e.name: disk._chain(e, part[FAT:disk.FAT_END], len(part)) for e in entries}
        self.assertEqual(blocks["NEW.BIN"], [6, 8, 9])
        self.assertEqual(blocks["D.BIN"], [4])
        self.assertEqual([fat_entry(part, b) for b in (4, 5, 6, 8, 9, 10)], [0, 1, 8, 9, 0, 1])

    def test_unlink_zeroes_the_first_name_word(self):
        self.assertEqual(self.opened(self.raw, self.ovl, ["unlink A.BIN"]), ["unlink 0"])
        path, part = self.dump()
        d = (BASE_SECTOR + 8 * 3) * SECTOR
        before = self.part[d:d + 24]
        self.assertEqual(part[d:d + 24], b"\0\0\0\0" + before[4:])
        self.assertEqual([e.name for e in disk.list_files(path)], ["B.BIN"])
        self.assertEqual([fat_entry(part, b) for b in (4, 5)], [1, 1])

    def test_truncating_open(self):
        lines = self.opened(self.raw, self.ovl, [
            "open A.BIN 1", "wr 0 4 1", "close 0", "stat A.BIN", "hash B.BIN"])
        self.assertEqual(lines, ["open 0", "wr 4", "close 0", "stat 0 4 0 0 9505",
                                 "hash 0 300 " + sha(self.b)])
        path, part = self.dump()
        self.assertEqual([(e.name, e.size) for e in disk.list_files(path)],
                         [("A.BIN", 4), ("B.BIN", 300)])
        # its second block went back to the free list; the first was freed and taken again
        self.assertEqual([fat_entry(part, b) for b in (4, 5)], [0, 1])

    def test_full_directory_is_extended(self):
        entries = [(f"F{i:03d}.BIN", 0, 0) for i in range(170)]
        raw = self.write("full.raw", fs_image(fs_partition(entries, [])))
        self.raw = raw
        lines = self.opened(raw, self.ovl, ["open NEWX.BIN 1", "wr 0 8 3", "close 0",
                                            "stat NEWX.BIN", "stat F169.BIN"])
        self.assertEqual(lines, ["open 0", "wr 8", "close 0", "stat 0 8 0 0 9505",
                                 "stat 0 0 0 31726 9550"])
        path, part = self.dump()
        names = [e.name for e in disk.list_files(path)]
        self.assertEqual(names, [e[0] for e in entries] + ["NEWX.BIN"])
        # block 4, the first free one, became the second directory block; block 5 the data
        d = (BASE_SECTOR + 8 * 3) * SECTOR
        self.assertEqual(struct.unpack_from("<I", part, d + 4092)[0], 4)
        self.assertEqual([fat_entry(part, b) for b in (4, 5, 6)], [0, 0, 1])


@requires_file("carnevil.raw")
@requires_file("parts", "part0")
class GameDataTests(unittest.TestCase):
    """Every file of partition 1 through C against disk.py reading parts/part0, then a write
    round trip on an overlay. One pass over the share for each side."""

    @classmethod
    def setUpClass(cls):
        build_driver()  # skips the class without `cc`
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = work_path("carnevil.raw")
        part0 = work_path("parts", "part0")
        cls.raw_before = os.stat(cls.raw)
        cls.ovl = os.path.join(cls.tmp.name, "game.ovl")

        scanned = disk._scan(part0)
        cls.files = {}
        with open(part0, "rb") as f:
            for entry, blocks in scanned:
                cls.files[entry.name] = (entry.size, sha(disk._read_blocks(f, entry, blocks)))

        names = list(cls.files)
        cls.listing = run_driver(cls.raw, cls.ovl,
                                 [c for n in names for c in (f"stat {n}", f"hash {n}")]
                                 + ["stat game.env", "stat GAME.ENV", "part 1"])
        cls.first = run_driver(cls.raw, cls.ovl, [
            "open TEST0001.WMS 1", "wr 0 8324 3", "close 0", "open TEST0001.WMS 0",
            "rd 0 8324", "rd 0 4", "close 0", "stat TEST0001.WMS"])
        cls.dump_path = os.path.join(cls.tmp.name, "part1")
        cls.second = run_driver(cls.raw, cls.ovl, [
            "hash TEST0001.WMS", "unlink TEST0001.WMS", "stat TEST0001.WMS",
            "open TEST0002.WMS 1", "wr 0 256 9", "close 0", "rename TEST0002.WMS TEST0003.WMS",
            "stat TEST0002.WMS", "stat TEST0003.WMS", "rename NOSUCH.FIL X.FIL",
            f"dump 1 {cls.dump_path}"])
        cls.raw_after = os.stat(cls.raw)

        # The raw image's bytes at every sector the overlay holds, against the split copy
        # of partition 0 taken before any of this.
        with open(cls.ovl, "rb") as f:
            ovl = f.read()
        (count,) = struct.unpack_from("<I", ovl, 12)
        cls.lbas = [struct.unpack_from("<I", ovl, OVERLAY_HEADER + OVERLAY_RECORD * i)[0]
                    for i in range(count)]
        cls.part_start = disk.read_partition_table(cls.raw)[0].offset // SECTOR
        cls.raw_sectors, cls.split_sectors, cls.overlay_sectors = [], [], []
        with open(cls.raw, "rb") as r, open(part0, "rb") as p:
            for i, lba in enumerate(cls.lbas):
                r.seek(lba * SECTOR)
                cls.raw_sectors.append(r.read(SECTOR))
                p.seek((lba - cls.part_start) * SECTOR)
                cls.split_sectors.append(p.read(SECTOR))
                at = OVERLAY_HEADER + OVERLAY_RECORD * i + 8
                cls.overlay_sectors.append(ovl[at:at + SECTOR])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_file_matches_the_python_reader(self):
        status, lines = self.listing
        self.assertEqual(status, "open 0")
        self.assertEqual(len(self.files), 1523)
        for i, (name, (size, digest)) in enumerate(self.files.items()):
            self.assertEqual(lines[2 * i].split()[:3], ["stat", "0", str(size)], name)
            # one handle at a time, so every open gets handle 0
            self.assertEqual(lines[2 * i + 1], f"hash 0 {size} {digest}", name)
        self.assertEqual(self.files["GAME.EXE"][1], GAME_EXE_SHA256)

    def test_game_env_is_absent(self):
        _status, lines = self.listing
        self.assertEqual(lines[-3:-1], ["stat -1 0 0 0 0", "stat -1 0 0 0 0"])
        self.assertEqual(lines[-1], "part 0 %d 193000" % self.part_start)

    def test_write_round_trip(self):
        data = pattern(0x2084, 3)
        self.assertEqual(self.first, ("open 0", [
            "open 0", "wr 8324", "close 0", "open 0", "rd 8324 " + sha(data),
            f"rd {EOF} " + sha(b""), "close 0", "stat 0 8324 0 0 9505"]))
        self.assertEqual(self.second, ("open 0", [
            "hash 0 8324 " + sha(data), "unlink 0", "stat -1 0 0 0 0", "open 0", "wr 256",
            "close 0", "rename 0", "stat -1 0 0 0 0", "stat 0 256 0 0 9505",
            f"rename {ENOENT}", "dump 0"]))

    def test_python_reads_the_written_partition(self):
        entries = disk.list_files(self.dump_path)
        got = {e.name: e.size for e in entries}
        want = {name: size for name, (size, _digest) in self.files.items()}
        want["TEST0003.WMS"] = 256
        self.assertEqual(got, want)
        result = disk.verify_files(self.dump_path)
        self.assertEqual(len(result.matched), 1504)
        self.assertEqual(result.mismatched, [])
        self.assertEqual(result.missing, [])
        with open(self.dump_path, "rb") as f:
            scanned = dict((e.name, (e, b)) for e, b in disk._scan(self.dump_path))
            self.assertEqual(sha(disk._read_blocks(f, *scanned["GAME.EXE"])), GAME_EXE_SHA256)
            self.assertEqual(disk._read_blocks(f, *scanned["TEST0003.WMS"]), pattern(256, 9))

    def test_raw_image_unchanged(self):
        self.assertEqual((self.raw_after.st_size, self.raw_after.st_mtime_ns),
                         (self.raw_before.st_size, self.raw_before.st_mtime_ns))
        self.assertEqual(self.raw_after.st_size, 4_310_046_720)
        self.assertTrue(self.lbas)
        self.assertTrue(all(self.part_start <= lba < self.part_start + 193000
                            for lba in self.lbas))
        self.assertEqual(self.raw_sectors, self.split_sectors)
        self.assertNotEqual(self.raw_sectors, self.overlay_sectors)


if __name__ == "__main__":
    unittest.main()
