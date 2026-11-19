import errno
import hashlib
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from carnevil_tools import disk, exe
from tests.gamedata import requires_file, work_path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Hashes of part0's files extracted by following the block chain (see docs/analysis/disk-format.md).
GAME_EXE_SHA256 = "f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6"
DIAG_EXE_SHA256 = "38b2810864c85d6971cedc3a6b0e507c8c6d593e4d7c355c880f91dbcaaaa2b9"

BASE = 70656  # partition data base, see docs/analysis/disk-format.md
BLOCK = 4096
FAT = 67 * 512  # block-chain table: u16 entry for block b at FAT + 2 * (b - 3)
HOME_MAGIC = 0x34011280  # first word of the home block, sector 2


def _encode_name(name):
    raw = name.encode("utf-8").ljust(12, b"\x00")
    out = bytearray()
    for i in range(0, 12, 4):
        out += raw[i:i + 4][::-1]
    return bytes(out)


def build_partition(files, fat_overrides=None):
    """Build a synthetic partition using the documented layout.

    `files` is [(name, bytes)] or [(name, bytes, [block, ...])]. Directory is
    block 3; without a block list a file gets the next free contiguous blocks
    from block 4. The chain table links each file's blocks in order and ends
    the chain with 0. `fat_overrides` ({block: link}) is applied last.
    """
    entries = b""
    placed = []
    used = {3}
    next_block = 4
    for item in files:
        name, payload = item[0], item[1]
        padded = payload + b"\x00" * (-len(payload) % 4)
        count = -(-len(padded) // BLOCK) or 1
        if len(item) > 2:
            blocks = list(item[2])
        else:
            while any(b in used for b in range(next_block, next_block + count)):
                next_block += 1
            blocks = list(range(next_block, next_block + count))
        used.update(blocks)
        entries += _encode_name(name) + struct.pack("<iii", len(padded) // 4, 0, blocks[0])
        placed.append((blocks, padded))
    entries += _encode_name("") + struct.pack("<iii", 0, 0, 0)  # terminator
    image = bytearray(BASE + (max(used) + 1) * BLOCK)
    struct.pack_into("<I", image, 2 * 512, HOME_MAGIC)
    image[BASE + 3 * BLOCK:BASE + 3 * BLOCK + len(entries)] = entries
    links = {}
    for blocks, padded in placed:
        for i, block in enumerate(blocks):
            start = BASE + block * BLOCK
            piece = padded[i * BLOCK:(i + 1) * BLOCK]
            image[start:start + len(piece)] = piece
            links[block] = blocks[i + 1] if i + 1 < len(blocks) else 0
    links.update(fat_overrides or {})
    for block, link in links.items():
        struct.pack_into("<H", image, FAT + 2 * (block - 3), link)
    return bytes(image)


def build_checksum_table(records):
    """Build a FILESYS.CHK from [(name, size, checksum)]: 0x126-byte records, then
    the POSIX cksum of all records. Use an even number of records: file sizes are
    whole words, and an odd count would leave the table 2 bytes short of one."""
    out = bytearray()
    for name, size, checksum in records:
        rec = bytearray(0x126)
        struct.pack_into("<I", rec, 0x1A, size)
        rec[0x1E:0x1E + len(name)] = name.encode("ascii")
        struct.pack_into("<I", rec, 0x122, checksum)
        out += rec
    return bytes(out) + struct.pack("<I", exe.posix_cksum(bytes(out)))


def build_raw(sizes_in_sectors):
    """Build a synthetic raw image header followed by zeroed partitions."""
    header = b"TRAP" + struct.pack("<i", len(sizes_in_sectors)) + b"\x00" * 8
    start = 8
    table = b""
    for size in sizes_in_sectors:
        table += b"\x00" * 4 + struct.pack("<ii", start, size)
        start += size
    body = bytearray(start * 512)
    body[:len(header) + len(table)] = header + table
    return bytes(body)


class SyntheticPartitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, *parts):
        return os.path.join(self.tmp.name, *parts)

    def write_partition(self, files, name="part"):
        p = self.path(name)
        with open(p, "wb") as f:
            f.write(build_partition(files))
        return p

    def test_roundtrip_synthetic(self):
        files = [
            ("ONE.BIN", bytes(range(256)) * 4),
            ("TWO.TXT", b"hello world!"),
            ("THREE.DAT", os.urandom(9000)),
        ]
        part = self.write_partition(files)
        entries = disk.list_files(part)
        self.assertEqual([e.name for e in entries], [n for n, _ in files])
        out = self.path("out")
        written = disk.extract_files(part, out)
        self.assertEqual(len(written), 3)
        for name, payload in files:
            with open(os.path.join(out, name), "rb") as f:
                self.assertEqual(f.read(), payload)

    def test_extract_rejects_path_escape(self):
        part = self.write_partition(
            [("../evil", b"EVIL"), ("a/b", b"ABAB"), ("ok.bin", b"OKOK")]
        )
        out = self.path("sandbox", "out")
        try:
            disk.extract_files(part, out)
        except disk.DiskFormatError:
            pass
        for root, _dirs, names in os.walk(self.tmp.name):
            for n in names:
                full = os.path.join(root, n)
                if full == part:
                    continue
                self.assertTrue(
                    os.path.realpath(full).startswith(os.path.realpath(out) + os.sep),
                    "file written outside out_dir: " + full,
                )
        self.assertFalse(os.path.exists(self.path("sandbox", "evil")))

    def test_separator_replaced(self):
        part = self.write_partition([("a/b", b"ABAB")])
        out = self.path("out")
        disk.extract_files(part, out)
        self.assertEqual(os.listdir(out), ["a_b"])

    def test_truncated_partition_raises(self):
        data = build_partition([("ONE.BIN", os.urandom(9000)), ("TWO.BIN", os.urandom(9000))])
        part = self.path("cut")
        with open(part, "wb") as f:
            f.write(data[: len(data) // 2])
        out = self.path("out")
        with self.assertRaises(disk.DiskFormatError):
            disk.extract_files(part, out)
        self.assertEqual(os.listdir(out) if os.path.isdir(out) else [], [])

    def assert_refused(self, files, fat_overrides):
        part = self.path("bad")
        with open(part, "wb") as f:
            f.write(build_partition(files, fat_overrides))
        out = self.path("out")
        with self.assertRaises(disk.DiskFormatError):
            disk.extract_files(part, out)
        self.assertEqual(os.listdir(out) if os.path.isdir(out) else [], [])

    def test_noncontiguous_file_follows_chain(self):
        a = [os.urandom(BLOCK) for _ in range(3)]
        b = [os.urandom(BLOCK) for _ in range(2)] + [os.urandom(100)]
        part = self.write_partition([
            ("A.BIN", b"".join(a), [9, 5, 7]),
            ("B.BIN", b"".join(b), [4, 8, 6]),
        ])
        out = self.path("out")
        disk.extract_files(part, out)
        with open(os.path.join(out, "A.BIN"), "rb") as f:
            self.assertEqual(f.read(), b"".join(a))
        with open(os.path.join(out, "B.BIN"), "rb") as f:
            self.assertEqual(f.read(), b"".join(b))
        entries = disk.list_files(part)
        self.assertEqual([e.offset for e in entries], [BASE + 9 * BLOCK, BASE + 4 * BLOCK])

    def test_chain_loop_raises(self):
        self.assert_refused([("A.BIN", os.urandom(3 * BLOCK), [4, 5, 6])], {5: 4})

    def test_chain_link_out_of_range_raises(self):
        self.assert_refused([("A.BIN", os.urandom(3 * BLOCK), [4, 5, 6])], {5: 5000})

    def test_chain_shorter_than_size_raises(self):
        self.assert_refused([("A.BIN", os.urandom(3 * BLOCK), [4, 5, 6])], {5: 0})

    def test_verify_against_checksum_table(self):
        a, b = os.urandom(5000), os.urandom(300)
        table = build_checksum_table([
            ("A.BIN", len(a), exe.posix_cksum(a)),
            ("B.BIN", len(b), exe.posix_cksum(b) ^ 1),
            ("C.BIN", 4, 0),
            ("D.BIN", 4, 0),
        ])
        part = self.write_partition([("A.BIN", a), ("B.BIN", b), ("FILESYS.CHK", table)])
        result = disk.verify_files(part)
        self.assertEqual(result.listed, 4)
        self.assertEqual(result.matched, ["A.BIN"])
        self.assertEqual(result.mismatched, ["B.BIN"])
        self.assertEqual(result.missing, ["C.BIN", "D.BIN"])

    def test_verify_size_mismatch_counts(self):
        a = os.urandom(400)
        table = build_checksum_table([("A.BIN", len(a) + 4, exe.posix_cksum(a)), ("C.BIN", 4, 0)])
        part = self.write_partition([("A.BIN", a), ("FILESYS.CHK", table)])
        self.assertEqual(disk.verify_files(part).mismatched, ["A.BIN"])

    def test_verify_rejects_damaged_table(self):
        table = bytearray(build_checksum_table([("A.BIN", 4, 0), ("C.BIN", 4, 0)]))
        table[0x40] ^= 1
        part = self.write_partition([("A.BIN", b"AAAA"), ("FILESYS.CHK", bytes(table))])
        with self.assertRaises(disk.DiskFormatError):
            disk.verify_files(part)
        part = self.write_partition([("A.BIN", b"AAAA")], name="nochk")
        with self.assertRaises(disk.DiskFormatError):
            disk.verify_files(part)

    def test_directory_loop_raises(self):
        # A blank-named entry pointing at its own block must not loop forever.
        entries = _encode_name("") + struct.pack("<iii", 3, 0, 0)
        image = bytearray(BASE + 4 * BLOCK)
        struct.pack_into("<I", image, 2 * 512, HOME_MAGIC)
        image[BASE + 3 * BLOCK:BASE + 3 * BLOCK + len(entries)] = entries
        part = self.path("loop")
        with open(part, "wb") as f:
            f.write(image)
        with self.assertRaises(disk.DiskFormatError):
            disk.list_files(part)

    def test_foreign_partition_refused(self):
        image = bytearray(build_partition([("A.BIN", b"AAAA")]))
        image[2 * 512:2 * 512 + 4] = b"\0\0\0\0"
        part = self.path("foreign")
        with open(part, "wb") as f:
            f.write(image)
        with self.assertRaisesRegex(disk.DiskFormatError, "not a CarnEvil partition"):
            disk.list_files(part)

    def test_block_shared_by_two_files_raises(self):
        self.assert_refused([("A.BIN", os.urandom(2 * BLOCK), [4, 5]),
                             ("B.BIN", os.urandom(2 * BLOCK), [6, 5])], {})

    def test_names_differing_only_in_case_are_duplicates(self):
        part = self.write_partition([("A.BIN", b"AAAA"), ("a.bin", b"BBBB")])
        out = self.path("out")
        with self.assertRaisesRegex(disk.DiskFormatError, "duplicate"):
            disk.extract_files(part, out)
        self.assertFalse(os.path.exists(out))

    def test_verify_rejects_duplicate_names(self):
        table = build_checksum_table([("A.BIN", 4, 0), ("C.BIN", 4, 0)])
        part = self.write_partition([("A.BIN", b"AAAA"), ("A.BIN", b"BBBB"),
                                     ("FILESYS.CHK", table)])
        with self.assertRaisesRegex(disk.DiskFormatError, "duplicate"):
            disk.verify_files(part)

    def test_only_a_dotdot_component_is_unsafe(self):
        part = self.write_partition([("A..B", b"ABAB")])
        out = self.path("out")
        disk.extract_files(part, out)
        self.assertEqual(os.listdir(out), ["A..B"])
        for name in ("..", "a/../b", "..\\x"):
            with self.subTest(name=name):
                part = self.write_partition([(name, b"EVIL")], name="bad")
                with self.assertRaises(disk.DiskFormatError):
                    disk.extract_files(part, self.path("out2"))

    def _failing_open(self):
        """open() whose binary-write files fail after their first 2 bytes (ENOSPC)."""
        real_open = open

        class Full:
            def __init__(self, f):
                self.f = f

            def write(self, data):
                self.f.write(data[:2])
                raise OSError(errno.ENOSPC, "No space left on device")

            def close(self):
                self.f.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()
                return False

        def fake(path, mode="r", *args, **kwargs):
            f = real_open(path, mode, *args, **kwargs)
            return Full(f) if mode == "wb" else f
        return mock.patch("carnevil_tools.disk.open", fake, create=True)

    def test_interrupted_extract_leaves_no_file(self):
        part = self.write_partition([("ONE.BIN", b"12345678")])
        out = self.path("out")
        with self._failing_open(), self.assertRaises(OSError):
            disk.extract_files(part, out)
        self.assertEqual(os.listdir(out), [])

    def test_interrupted_split_leaves_no_file(self):
        raw = self.path("img.raw")
        with open(raw, "wb") as f:
            f.write(build_raw([2, 3]))
        out = self.path("parts")
        with self._failing_open(), self.assertRaises(OSError):
            disk.split_partitions(raw, out)
        self.assertEqual(os.listdir(out), [])

    def test_partition_table_synthetic(self):
        raw = self.path("img.raw")
        with open(raw, "wb") as f:
            f.write(build_raw([2, 3]))
        table = disk.read_partition_table(raw)
        self.assertEqual(
            table,
            [disk.Partition(0, 8 * 512, 2 * 512), disk.Partition(1, 10 * 512, 3 * 512)],
        )

    def test_split_partitions_synthetic(self):
        raw = self.path("img.raw")
        image = bytearray(build_raw([2, 3]))
        image[8 * 512:8 * 512 + 4] = b"ABCD"
        with open(raw, "wb") as f:
            f.write(image)
        out = self.path("parts")
        paths = disk.split_partitions(raw, out)
        self.assertEqual([os.path.basename(p) for p in paths], ["part0", "part1"])
        with open(paths[0], "rb") as f:
            data = f.read()
        self.assertEqual(len(data), 1024)
        self.assertEqual(data[:4], b"ABCD")

    def test_bad_magic_raises(self):
        raw = self.path("bad.raw")
        with open(raw, "wb") as f:
            f.write(b"NOPE" + b"\x00" * 100)
        with self.assertRaises(disk.DiskFormatError):
            disk.read_partition_table(raw)

    def test_partition_beyond_image_raises(self):
        raw = self.path("short.raw")
        with open(raw, "wb") as f:
            f.write(build_raw([2, 3])[:-512])
        with self.assertRaises(disk.DiskFormatError):
            disk.read_partition_table(raw)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "carnevil_tools", *args],
            cwd=REPO_ROOT, capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if k != "CARNEVIL_WORK"},
        )

    def test_cli_without_data_explains(self):
        r = self.run_cli("split", "/nonexistent/carnevil.raw", "-o", "/nonexistent/out")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("/Volumes/archive/reincarnevilation/work", r.stdout + r.stderr)
        self.assertIn("CARNEVIL_WORK", r.stdout + r.stderr)

    def test_cli_unwritable_output_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = os.path.join(tmp, "part0")
            with open(part, "wb") as f:
                f.write(build_partition([("X.BIN", b"1234")]))
            out = os.path.join(tmp, "locked")
            os.mkdir(out)
            os.chmod(out, stat.S_IRUSR | stat.S_IXUSR)
            try:
                r = self.run_cli("extract", part, "-o", out)
            finally:
                os.chmod(out, stat.S_IRWXU)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("error:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(os.listdir(out), [])

    def test_cli_directory_is_not_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("extract", tmp, "-o", os.path.join(tmp, "out"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a file", r.stderr)

    def test_cli_split_counts_partitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "img.raw")
            with open(raw, "wb") as f:
                f.write(build_raw([2, 3]))
            r = self.run_cli("split", raw, "-o", os.path.join(tmp, "parts"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wrote 2 partitions to", r.stdout)

    def test_cli_extract_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = os.path.join(tmp, "part0")
            with open(part, "wb") as f:
                f.write(build_partition([("X.BIN", b"1234")]))
            out = os.path.join(tmp, "out")
            r = self.run_cli("extract", part, "-o", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(out, "X.BIN"), "rb") as f:
                self.assertEqual(f.read(), b"1234")

    def test_cli_verify_reports_mismatches(self):
        good, bad = b"GOOD", b"BAD!"
        table = build_checksum_table([
            ("GOOD.BIN", 4, exe.posix_cksum(good)),
            ("BAD.BIN", 4, exe.posix_cksum(bad) ^ 1),
            ("GONE.BIN", 4, 0),
            ("LOST.BIN", 4, 0),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            part = os.path.join(tmp, "part0")
            with open(part, "wb") as f:
                f.write(build_partition([("GOOD.BIN", good), ("BAD.BIN", bad),
                                         ("FILESYS.CHK", table)]))
            r = self.run_cli("verify", part)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 of 4 listed files match", r.stdout)
        self.assertIn("2 listed files are not on the partition", r.stdout)
        self.assertIn("mismatch: BAD.BIN", r.stdout)
        self.assertNotIn("GOOD.BIN", r.stdout)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class GameDataTests(unittest.TestCase):
    @requires_file("carnevil.raw")
    def test_partition_table(self):
        table = disk.read_partition_table(work_path("carnevil.raw"))
        self.assertEqual(len(table), 17)
        self.assertEqual(table[0].size, 98_816_000)

    @requires_file("parts", "part0")
    def test_part0_matches_reference(self):
        part0 = work_path("parts", "part0")
        self.assertEqual(len(disk.list_files(part0)), 1356)
        with tempfile.TemporaryDirectory() as out:
            disk.extract_files(part0, out)
            self.assertEqual(sha256(os.path.join(out, "GAME.EXE")), GAME_EXE_SHA256)
            self.assertEqual(sha256(os.path.join(out, "DIAG.EXE")), DIAG_EXE_SHA256)

    @requires_file("parts", "part0")
    @requires_file("mame", "dumps", "ram-at-game-entry.bin")
    def test_game_exe_matches_loader(self):
        # The image the boot ROM's loader placed at 0x800C4000 (physical 0xC4000)
        # is GAME.EXE from file offset 8 on.
        with tempfile.TemporaryDirectory() as out:
            disk.extract_files(work_path("parts", "part0"), out)
            with open(os.path.join(out, "GAME.EXE"), "rb") as f:
                image = f.read()[8:]
        with open(work_path("mame", "dumps", "ram-at-game-entry.bin"), "rb") as f:
            f.seek(0xC4000)
            loaded = f.read(len(image))
        self.assertEqual(len(image), 918052)
        self.assertTrue(image == loaded, "extracted GAME.EXE differs from the loaded image")

    @requires_file("parts", "part0")
    def test_part0_matches_checksum_table(self):
        result = disk.verify_files(work_path("parts", "part0"))
        self.assertEqual(result.listed, 1504)
        self.assertEqual(len(result.matched), 1339)
        self.assertEqual(result.mismatched, [])
        self.assertEqual(len(result.missing), 165)


class GameDataSkipTests(unittest.TestCase):
    def test_gamedata_tests_skip_when_files_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, CARNEVIL_WORK=empty)
            r = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                 "-p", "test_disk.py", "-k", "GameDataTests", "-v"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(out.count("skipped 'game data file not found"), 4, out)
        self.assertNotIn("ERROR", out)


if __name__ == "__main__":
    unittest.main()
