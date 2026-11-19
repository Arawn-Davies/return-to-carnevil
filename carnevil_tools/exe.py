"""Load a CarnEvil EXE: an 8-byte header followed by the raw memory image.

The layout is documented in docs/analysis/exe-format.md.
"""
import hashlib
import struct
from dataclasses import dataclass

GAME_EXE_SHA256 = "47aea64a3e5d15c15f28b3907294e706f078a4e9a47c422f551f420d9ba7da9e"
DIAG_EXE_SHA256 = "38b2810864c85d6971cedc3a6b0e507c8c6d593e4d7c355c880f91dbcaaaa2b9"

HEADER = struct.Struct("<II")  # header word 0, load address


class UnknownRevisionError(Exception):
    """The file's SHA-256 is not the revision this toolchain was built against."""


@dataclass(frozen=True)
class ExeImage:
    header_word0: int
    load_addr: int
    data: bytes
    sha256: str

    def end_addr(self):
        return self.load_addr + len(self.data)

    def contains(self, addr):
        return self.load_addr <= addr < self.end_addr()

    def word(self, addr):
        """Little-endian u32 at a 4-byte-aligned address inside the image."""
        if addr % 4 != 0:
            raise IndexError("unaligned address 0x%08X" % addr)
        if not (self.load_addr <= addr and addr + 4 <= self.end_addr()):
            raise IndexError("address 0x%08X outside image" % addr)
        return struct.unpack_from("<I", self.data, addr - self.load_addr)[0]


def load_exe(path, expect_sha256=GAME_EXE_SHA256):
    """Read an EXE, refusing it if its SHA-256 differs from expect_sha256 (None skips the check)."""
    with open(path, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    if expect_sha256 is not None and digest != expect_sha256:
        raise UnknownRevisionError(
            "unknown EXE revision: expected sha256 %s, got %s" % (expect_sha256, digest)
        )
    if len(raw) < HEADER.size:
        raise ValueError("EXE shorter than its %d-byte header" % HEADER.size)
    word0, load_addr = HEADER.unpack_from(raw)
    return ExeImage(word0, load_addr, raw[HEADER.size:], digest)
