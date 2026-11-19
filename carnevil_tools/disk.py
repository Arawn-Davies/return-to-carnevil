"""Read the CarnEvil hard-disk image: partition table and per-partition file system.

The layout is documented in docs/analysis/disk-format.md.
"""
import os
import struct
from dataclasses import dataclass

SECTOR = 512
RAW_MAGIC = b"TRAP"
MAX_PARTITIONS = 256

PART_BASE = 70656  # byte offset of "block 0" inside a partition
BLOCK = 4096
FIRST_DIR_BLOCK = 3
NAME_LEN = 12
ENTRY = struct.Struct("<iii")  # length in words, unused, data block
MAX_DIR_BLOCKS = 4096
MAX_ENTRIES = 1_000_000
COPY_CHUNK = 1 << 20


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
            with open(dest, "wb") as dst:
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
    """Return every FileEntry in a partition, validated against the partition size."""
    entries = []
    seen_blocks = set()
    with open(partition_path, "rb") as f:
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
                        return entries
                    # Blank name: the length field links to the next directory block.
                    # A link past the end of the partition ends the listing (the
                    # real image terminates its last directory this way).
                    if PART_BASE + length * BLOCK >= part_size:
                        return entries
                    block = length
                    break
                if length < 0 or data_block < 0:
                    raise DiskFormatError(f"entry {name!r} has negative length or block")
                offset = PART_BASE + data_block * BLOCK
                size = length * 4
                if offset + size > part_size:
                    raise DiskFormatError(f"entry {name!r} extends beyond the end of the partition")
                entries.append(FileEntry(name, offset, size))
                if len(entries) > MAX_ENTRIES:
                    raise DiskFormatError("too many directory entries")


def _safe_name(name):
    if ".." in name:
        raise DiskFormatError(f"unsafe file name {name!r}")
    safe = name.replace("/", "_").replace("\\", "_")
    if safe in ("", ".") or "\x00" in safe:
        raise DiskFormatError(f"unsafe file name {name!r}")
    return safe


def extract_files(partition_path, out_dir):
    """Extract every file into out_dir; return the written paths.

    The whole directory is validated before anything is written.
    """
    entries = list_files(partition_path)
    targets = []
    used = set()
    for entry in entries:
        safe = _safe_name(entry.name)
        if safe in used:
            raise DiskFormatError(f"duplicate file name {safe!r}")
        used.add(safe)
        targets.append(safe)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with open(partition_path, "rb") as src:
        for entry, safe in zip(entries, targets):
            dest = os.path.join(out_dir, safe)
            src.seek(entry.offset)
            with open(dest, "wb") as dst:
                dst.write(_read_exact(src, entry.size, f"data of {entry.name!r}"))
            paths.append(dest)
    return paths
