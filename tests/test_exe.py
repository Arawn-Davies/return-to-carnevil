import hashlib
import os
import struct
import tempfile
import unittest

from carnevil_tools import exe
from tests.gamedata import requires_file, work_path


class ExeTests(unittest.TestCase):
    def _write(self, payload):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, tmp.name)
        tmp.write(payload)
        tmp.close()
        return tmp.name

    def _synthetic(self, word0=None):
        body = struct.pack("<I", 0x80010000) + bytes(range(16))
        if word0 is None:
            word0 = exe.posix_cksum(body)
        return struct.pack("<I", word0) + body

    def test_posix_cksum_check_value(self):
        self.assertEqual(exe.posix_cksum(b"123456789"), 0x377A6011)
        self.assertEqual(exe.posix_cksum(b""), 0xFFFFFFFF)

    def test_posix_cksum_matches_bitwise_definition(self):
        def reference(data):
            crc = 0
            length = len(data)
            tail = b""
            while length:
                tail += bytes([length & 0xFF])
                length >>= 8
            for byte in data + tail:
                crc ^= byte << 24
                for _ in range(8):
                    crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
                    crc &= 0xFFFFFFFF
            return crc ^ 0xFFFFFFFF

        for data in (b"\x00", b"\xff" * 3, bytes(range(256)), os.urandom(1000)):
            self.assertEqual(exe.posix_cksum(data), reference(data), data[:8].hex())

    def test_bad_header_checksum_refused(self):
        good = self._synthetic()
        right = struct.unpack_from("<I", good)[0]
        wrong = right ^ 0x1
        with self.assertRaises(exe.ChecksumError) as ctx:
            exe.load_exe(self._write(self._synthetic(wrong)), expect_sha256=None)
        self.assertIsInstance(ctx.exception, ValueError)
        message = str(ctx.exception)
        self.assertIn("0x%08X" % wrong, message)
        self.assertIn("0x%08X" % right, message)
        image = exe.load_exe(self._write(good), expect_sha256=None)
        self.assertEqual(image.header_word0, right)

    def test_unknown_hash_refused(self):
        path = self._write(self._synthetic())
        with self.assertRaises(exe.UnknownRevisionError) as ctx:
            exe.load_exe(path)
        message = str(ctx.exception)
        self.assertIn(exe.GAME_EXE_SHA256, message)
        self.assertIn(hashlib.sha256(self._synthetic()).hexdigest(), message)

    def test_synthetic_header(self):
        path = self._write(self._synthetic())
        image = exe.load_exe(path, expect_sha256=None)
        self.assertEqual(image.header_word0, exe.posix_cksum(self._synthetic()[4:]))
        self.assertEqual(image.load_addr, 0x80010000)
        self.assertEqual(image.end_addr(), 0x80010010)
        self.assertEqual(image.word(0x80010000), 0x03020100)
        self.assertEqual(image.word(0x8001000C), 0x0F0E0D0C)
        self.assertTrue(image.contains(0x80010000))
        self.assertTrue(image.contains(0x8001000F))
        self.assertFalse(image.contains(0x80010010))
        self.assertFalse(image.contains(0x8000FFFF))

    def test_word_out_of_range_or_unaligned(self):
        image = exe.load_exe(self._write(self._synthetic()), expect_sha256=None)
        for addr in (0x8000FFFC, 0x80010010, 0x80010001, 0x80010002):
            with self.assertRaises(IndexError, msg=hex(addr)):
                image.word(addr)

    def test_truncated_file(self):
        path = self._write(b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            exe.load_exe(path, expect_sha256=None)

    @requires_file("part0_files", "GAME.EXE")
    def test_game_exe_layout(self):
        image = exe.load_exe(work_path("part0_files", "GAME.EXE"))
        self.assertEqual(image.header_word0, 0x2CAE3F54)
        self.assertEqual(image.load_addr, 0x800C4000)
        self.assertEqual(image.end_addr(), 0x801A4224)
        self.assertEqual(image.word(0x800C4000), 0x3C1D8080)
        self.assertEqual(image.sha256, exe.GAME_EXE_SHA256)

    @requires_file("part0_files", "DIAG.EXE")
    def test_diag_exe_loads(self):
        image = exe.load_exe(
            work_path("part0_files", "DIAG.EXE"), expect_sha256=exe.DIAG_EXE_SHA256
        )
        self.assertEqual(image.sha256, exe.DIAG_EXE_SHA256)
        self.assertEqual(image.header_word0, 0x5799F785)
        self.assertEqual(image.load_addr, 0x80100000)
        self.assertEqual(image.end_addr(), 0x801725CC)
        self.assertEqual(image.end_addr(), image.load_addr + len(image.data))


if __name__ == "__main__":
    unittest.main()
