"""Load a CarnEvil EXE: an 8-byte header followed by the raw memory image.

The layout is documented in docs/analysis/exe-format.md.
"""
import hashlib
import struct
import zlib
from dataclasses import dataclass

GAME_EXE_SHA256 = "f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6"
DIAG_EXE_SHA256 = "38b2810864c85d6971cedc3a6b0e507c8c6d593e4d7c355c880f91dbcaaaa2b9"

HEADER = struct.Struct("<II")  # header word 0 (checksum), load address

_BIT_REVERSE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


class UnknownRevisionError(Exception):
    """The file's SHA-256 is not the revision this toolchain was built against."""


class ChecksumError(ValueError):
    """Header word 0 is not the checksum of the rest of the file: damaged or mis-extracted."""


def posix_cksum(data):
    """The POSIX `cksum` CRC of data.

    CRC-32 with polynomial 0x04C11DB7, MSB first, initial value 0, over the data
    followed by its length as a minimal little-endian byte string, complemented.
    Computed with zlib's reflected CRC-32 on bit-reversed bytes: passing 0xFFFFFFFF
    as zlib's running value starts the register at 0, and reversing the 32-bit
    result turns the reflected register (already complemented by zlib) back.
    """
    length = len(data)
    tail = bytearray()
    while length:
        tail.append(length & 0xFF)
        length >>= 8
    reflected = zlib.crc32((bytes(data) + bytes(tail)).translate(_BIT_REVERSE), 0xFFFFFFFF)
    return int(f"{reflected:032b}"[::-1], 2)


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
    """Read an EXE, refusing it if its SHA-256 differs from expect_sha256 (None skips that
    check) or if header word 0 is not the POSIX cksum of the file from byte 4 (always checked).
    """
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
    computed = posix_cksum(raw[4:])
    if word0 != computed:
        raise ChecksumError(
            "EXE header checksum 0x%08X does not match the file's checksum 0x%08X: "
            "the file is damaged or was mis-extracted" % (word0, computed)
        )
    return ExeImage(word0, load_addr, raw[HEADER.size:], digest)
