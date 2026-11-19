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

    def _synthetic(self):
        return struct.pack("<II", 0x11223344, 0x80010000) + bytes(range(16))

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
        self.assertEqual(image.header_word0, 0x11223344)
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
        self.assertGreater(len(image.data), 0)
        self.assertEqual(image.end_addr(), image.load_addr + len(image.data))


if __name__ == "__main__":
    unittest.main()
