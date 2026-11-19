"""The POSIX `cksum` CRC, used by the EXE header and by FILESYS.CHK."""
import zlib

_BIT_REVERSE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


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
