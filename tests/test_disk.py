import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import unittest

from carnevil_tools import disk
from gamedata import requires_file, work_path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = 70656  # partition data base, see docs/analysis/disk-format.md
BLOCK = 4096


def _encode_name(name):
    raw = name.encode("utf-8").ljust(12, b"\x00")
    out = bytearray()
    for i in range(0, 12, 4):
        out += raw[i:i + 4][::-1]
    return bytes(out)


def build_partition(files):
    """Build a synthetic partition from [(name, bytes)] using the documented layout.

    Directory is block 3; file data follows from block 4, one block-aligned
    region per file.
    """
    entries = b""
    data_blocks = []
    next_block = 4
    for name, payload in files:
        padded = payload + b"\x00" * (-len(payload) % 4)
        entries += _encode_name(name) + struct.pack("<iii", len(padded) // 4, 0, next_block)
        data_blocks.append((next_block, padded))
        next_block += -(-len(padded) // BLOCK) or 1
    entries += _encode_name("") + struct.pack("<iii", 0, 0, 0)  # terminator
    image = bytearray(BASE + next_block * BLOCK)
    image[BASE + 3 * BLOCK:BASE + 3 * BLOCK + len(entries)] = entries
    for block, padded in data_blocks:
        start = BASE + block * BLOCK
        image[start:start + len(padded)] = padded
    return bytes(image)


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

    def test_directory_loop_raises(self):
        # A blank-named entry pointing at its own block must not loop forever.
        entries = _encode_name("") + struct.pack("<iii", 3, 0, 0)
        image = bytearray(BASE + 4 * BLOCK)
        image[BASE + 3 * BLOCK:BASE + 3 * BLOCK + len(entries)] = entries
        part = self.path("loop")
        with open(part, "wb") as f:
            f.write(image)
        with self.assertRaises(disk.DiskFormatError):
            disk.list_files(part)

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
            self.assertTrue(sha256(os.path.join(out, "GAME.EXE")).startswith("47aea64a"))
            self.assertTrue(sha256(os.path.join(out, "GAME.EXE")).endswith("da9e"))
            self.assertTrue(sha256(os.path.join(out, "DIAG.EXE")).startswith("38b28108"))
            self.assertTrue(sha256(os.path.join(out, "DIAG.EXE")).endswith("a2b9"))


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
        self.assertEqual(out.count("skipped 'game data file not found"), 2, out)
        self.assertNotIn("ERROR", out)


if __name__ == "__main__":
    unittest.main()
