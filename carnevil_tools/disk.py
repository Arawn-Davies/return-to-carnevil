"""Read the CarnEvil hard-disk image: partition table and per-partition file system.

The layout is documented in docs/analysis/disk-format.md.
"""
import os
import struct
from dataclasses import dataclass

from carnevil_tools.cksum import posix_cksum

SECTOR = 512
RAW_MAGIC = b"TRAP"
HOME_BLOCK = 2 * SECTOR  # byte offset of a partition's home block
HOME_MAGIC = 0x34011280  # its first word, checked by the loader's mount routine
MAX_PARTITIONS = 256

PART_BASE = 70656  # byte offset of "block 0" inside a partition
BLOCK = 4096
FIRST_DIR_BLOCK = 3
FIRST_BLOCK = 3  # lowest block number that holds data; chain links below it end a chain
FAT_OFFSET = 67 * SECTOR  # chain table: u16 link of block b at FAT_OFFSET + 2 * (b - 3)
FAT_END = PART_BASE + FIRST_BLOCK * BLOCK  # the table fills the space up to block 3
NAME_LEN = 12
ENTRY = struct.Struct("<iii")  # length in words, unused, data block
MAX_DIR_BLOCKS = 4096
MAX_ENTRIES = 1_000_000
COPY_CHUNK = 1 << 20

CHECKSUM_TABLE = "FILESYS.CHK"
CHK_RECORD = 0x126
CHK_SIZE = 0x1A  # u32 file size in bytes
CHK_NAME = 0x1E  # NUL-terminated name, up to the checksum
CHK_SUM = 0x122  # u32 POSIX cksum of the file


class DiskFormatError(Exception):
    """The image or partition does not match the expected on-disk layout."""


@dataclass(frozen=True)
class Partition:
    index: int
    offset: int  # bytes from start of raw image
    size: int  # bytes


@dataclass(frozen=True)
class FileEntry:
    name: str
    offset: int  # bytes from start of partition file
    size: int  # bytes


def _read_exact(f, n, what):
    data = f.read(n)
    if len(data) != n:
        raise DiskFormatError(f"truncated while reading {what}")
    return data


def read_partition_table(raw_path):
    """Return the partitions listed in the raw image header (reads only the header)."""
    with open(raw_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        image_size = f.tell()
        f.seek(0)
        head = _read_exact(f, 16, "image header")
        if head[:4] != RAW_MAGIC:
            raise DiskFormatError("not a CarnEvil raw image (missing TRAP magic)")
        (count,) = struct.unpack_from("<i", head, 4)
        if not 0 < count <= MAX_PARTITIONS:
            raise DiskFormatError(f"implausible partition count {count}")
        table = _read_exact(f, 12 * count, "partition table")
    parts = []
    for i in range(count):
        start, length = struct.unpack_from("<ii", table, 12 * i + 4)
        if start < 0 or length <= 0:
            raise DiskFormatError(f"partition {i} has invalid start/size")
        part = Partition(i, start * SECTOR, length * SECTOR)
        if part.offset + part.size > image_size:
            raise DiskFormatError(f"partition {i} extends beyond the end of the image")
        parts.append(part)
    return parts


class _Output:
    """Write dest through dest + ".partial", renamed into place only when the write
    completes; on any exception the partial file is removed and dest is untouched."""

    def __init__(self, dest):
        self.dest, self.partial = dest, dest + ".partial"

    def __enter__(self):
        self.f = open(self.partial, "wb")
        return self.f

    def __exit__(self, exc_type, exc, tb):
        try:
            self.f.close()
        finally:
            if exc_type is None:
                os.replace(self.partial, self.dest)
            else:
                try:
                    os.remove(self.partial)
                except OSError:
                    pass
        return False


def split_partitions(raw_path, out_dir):
    """Copy each partition to out_dir/part<N>; return the paths."""
    parts = read_partition_table(raw_path)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with open(raw_path, "rb") as src:
        for part in parts:
            dest = os.path.join(out_dir, f"part{part.index}")
            src.seek(part.offset)
            remaining = part.size
            with _Output(dest) as dst:
                while remaining:
                    chunk = src.read(min(COPY_CHUNK, remaining))
                    if not chunk:
                        raise DiskFormatError(f"truncated while copying partition {part.index}")
                    dst.write(chunk)
                    remaining -= len(chunk)
            paths.append(dest)
    return paths


def _decode_name(raw):
    # Each 4-byte group is stored byte-reversed.
    fixed = b"".join(raw[i:i + 4][::-1] for i in range(0, NAME_LEN, 4))
    fixed = fixed.split(b"\x00", 1)[0]
    try:
        return fixed.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiskFormatError("file name is not valid UTF-8") from exc


def list_files(partition_path):
    """Return every FileEntry in a partition, each file's block chain validated."""
    return [entry for entry, _blocks in _scan(partition_path)]


def _scan(partition_path):
    """[(FileEntry, [block, ...])] for every file; raises DiskFormatError on a bad chain,
    a block two files share, or a partition without the home-block magic."""
    with open(partition_path, "rb") as f:
        f.seek(HOME_BLOCK)
        home = f.read(4)
        if len(home) != 4 or struct.unpack("<I", home)[0] != HOME_MAGIC:
            raise DiskFormatError(
                f"not a CarnEvil partition (no home-block magic 0x{HOME_MAGIC:08X} at byte "
                f"{HOME_BLOCK})")
        entries, part_size = _read_directory(f)
        f.seek(FAT_OFFSET)
        fat = f.read(max(0, min(part_size, FAT_END) - FAT_OFFSET))
    owner = {}  # block -> index of the file holding it
    out = []
    for i, entry in enumerate(entries):
        blocks = _chain(entry, fat, part_size)
        for block in blocks:
            k = owner.setdefault(block, i)
            if k != i:
                raise DiskFormatError(
                    f"block {block} belongs to both {entries[k].name!r} and {entry.name!r}")
        out.append((entry, blocks))
    return out


def _chain(entry, fat, part_size):
    """The blocks holding entry's data, in file order, following the chain table."""
    blocks = []
    block = (entry.offset - PART_BASE) // BLOCK
    seen = set()
    remaining = entry.size
    while remaining > 0:
        if block in seen:
            raise DiskFormatError(f"block chain of {entry.name!r} loops at block {block}")
        start = PART_BASE + block * BLOCK
        if block < FIRST_BLOCK or start + min(BLOCK, remaining) > part_size:
            raise DiskFormatError(
                f"block chain of {entry.name!r} reaches block {block}, "
                f"not a data block inside the partition")
        seen.add(block)
        blocks.append(block)
        remaining -= BLOCK
        if remaining <= 0:
            break
        at = 2 * (block - FIRST_BLOCK)
        if at + 2 > len(fat):
            raise DiskFormatError(f"chain table entry of block {block} is outside the partition")
        (block,) = struct.unpack_from("<H", fat, at)
        if block < FIRST_BLOCK:
            raise DiskFormatError(
                f"block chain of {entry.name!r} ends after {len(blocks)} blocks, "
                f"short of its {entry.size} bytes")
    return blocks


def _read_blocks(f, entry, blocks):
    parts = []
    remaining = entry.size
    for block in blocks:
        f.seek(PART_BASE + block * BLOCK)
        n = min(BLOCK, remaining)
        parts.append(_read_exact(f, n, f"data of {entry.name!r}"))
        remaining -= n
    return b"".join(parts)


def _read_directory(f):
    """(entries, partition size) from the directory chain starting at block 3."""
    entries = []
    seen_blocks = set()
    f.seek(0, os.SEEK_END)
    part_size = f.tell()
    block = FIRST_DIR_BLOCK
    while True:
        if block in seen_blocks or len(seen_blocks) >= MAX_DIR_BLOCKS:
            raise DiskFormatError("directory chain loops or is too long")
        seen_blocks.add(block)
        if block < 0:
            raise DiskFormatError("negative directory block")
        f.seek(PART_BASE + block * BLOCK)
        while True:
            name = _decode_name(_read_exact(f, NAME_LEN, "directory entry"))
            length, _unused, data_block = ENTRY.unpack(_read_exact(f, ENTRY.size, "directory entry"))
            if name == "":
                if length == 0:
                    return entries, part_size
                # Blank name: the length field links to the next directory block.
                # A link past the end of the partition ends the listing (the
                # real image terminates its last directory this way).
                if PART_BASE + length * BLOCK >= part_size:
                    return entries, part_size
                block = length
                break
            if length < 0 or data_block < 0:
                raise DiskFormatError(f"entry {name!r} has negative length or block")
            # Where the data lies is checked block by block when the chain is followed.
            entries.append(FileEntry(name, PART_BASE + data_block * BLOCK, length * 4))
            if len(entries) > MAX_ENTRIES:
                raise DiskFormatError("too many directory entries")


def _safe_name(name):
    """The name with path separators replaced by "_"; a name with a ".." component, or
    one that is empty or "." after the replacement, is refused."""
    if ".." in name.replace("\\", "/").split("/"):
        raise DiskFormatError(f"unsafe file name {name!r}")
    safe = name.replace("/", "_").replace("\\", "_")
    if safe in ("", ".") or "\x00" in safe:
        raise DiskFormatError(f"unsafe file name {name!r}")
    return safe


def extract_files(partition_path, out_dir):
    """Extract every file into out_dir; return the written paths.

    The whole directory, and every file's block chain, is validated before anything
    is written.
    """
    scanned = _scan(partition_path)
    targets = []
    used = set()
    for entry, _blocks in scanned:
        safe = _safe_name(entry.name)
        if safe.casefold() in used:  # macOS and Windows file systems ignore case
            raise DiskFormatError(f"duplicate file name {safe!r}")
        used.add(safe.casefold())
        targets.append(safe)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with open(partition_path, "rb") as src:
        for (entry, blocks), safe in zip(scanned, targets):
            dest = os.path.join(out_dir, safe)
            data = _read_blocks(src, entry, blocks)
            with _Output(dest) as dst:
                dst.write(data)
            paths.append(dest)
    return paths


@dataclass(frozen=True)
class VerifyResult:
    listed: int  # records in FILESYS.CHK
    matched: list  # names whose size and checksum agree
    mismatched: list  # names on the partition whose size or checksum differ
    missing: list  # listed names not on the partition


def verify_files(partition_path):
    """Check every file listed in the partition's FILESYS.CHK against its recorded size
    and POSIX cksum, reading the files in memory by following their block chains."""
    scanned = _scan(partition_path)
    by_name = {}
    for entry, blocks in scanned:
        if entry.name in by_name:
            raise DiskFormatError(f"duplicate file name {entry.name!r}")
        by_name[entry.name] = (entry, blocks)
    with open(partition_path, "rb") as src:
        if CHECKSUM_TABLE not in by_name:
            raise DiskFormatError(f"no {CHECKSUM_TABLE} on the partition")
        table = _read_blocks(src, *by_name[CHECKSUM_TABLE])
        records = _parse_checksum_table(table)
        matched, mismatched, missing = [], [], []
        for name, size, checksum in records:
            if name not in by_name:
                missing.append(name)
                continue
            data = _read_blocks(src, *by_name[name])
            if len(data) == size and posix_cksum(data) == checksum:
                matched.append(name)
            else:
                mismatched.append(name)
    return VerifyResult(len(records), matched, mismatched, missing)


def _parse_checksum_table(table):
    """[(name, size, checksum)] from FILESYS.CHK: records, then the cksum of the records."""
    body, trailer = table[:-4], table[-4:]
    if len(table) < 4 or len(body) % CHK_RECORD:
        raise DiskFormatError(f"{CHECKSUM_TABLE} is not a whole number of records")
    if posix_cksum(body) != struct.unpack("<I", trailer)[0]:
        raise DiskFormatError(f"{CHECKSUM_TABLE} fails its own checksum")
    records = []
    for at in range(0, len(body), CHK_RECORD):
        rec = body[at:at + CHK_RECORD]
        name = rec[CHK_NAME:CHK_SUM].split(b"\x00", 1)[0].decode("ascii", "replace")
        (size,) = struct.unpack_from("<I", rec, CHK_SIZE)
        (checksum,) = struct.unpack_from("<I", rec, CHK_SUM)
        records.append((name, size, checksum))
    return records
