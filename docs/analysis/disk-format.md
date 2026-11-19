# CarnEvil disk image format

The arcade hard disk (CHD converted to a raw image) holds a small header, then a number of
partitions. Each partition holds a flat custom file system. All integers are little-endian.
The raw-image and directory layout below was first learned from the two scripts in
https://github.com/xclusivor/CarnEvil-Research (`scripts/split_raw_img.py`,
`scripts/split_partition.py`, themselves a rewrite of daemon1's `carnevilraw.exe` /
`carnevilpart.exe`); the block chain was established from the boot ROM's loader
(`boot.u32`, loader body ROM 0xBFC00F20 to 0xBFC16B60, running at 0x80000000) and checked
against the disk. Our implementation is `carnevil_tools/disk.py`.

## Raw image

```
0x00  4 bytes   magic "TRAP"
0x04  int32     partition count N (17 for CarnEvil)
0x08  8 bytes   unknown (ignored)
0x10  N x 12    partition table entries:
                  +0  4 bytes  unknown (ignored)
                  +4  int32    start sector (512-byte sectors, from image start)
                  +8  int32    size in sectors
```

Only the first `0x10 + 12*N` bytes are needed to read the table. Partition `i` is the byte
range `[start*512, (start+size)*512)`. Partition 0 is 193,000 sectors = 98,816,000 bytes and
holds the game files (1,356 of them).

## Partition file system

Sector offsets below are from the start of the partition.

### Home block (sector 2)

The loader's mount routine (0x80008368) reads sector 2 and checks it:

```
+0x00  u32  magic 0x34011280
+0x04  u32  sectors per block, 8 (4096-byte blocks)
+0x08  u32  first sector of the chain table, 67 (0x43)
+0x0C  u32  162 (0xA2): the first sector of block 3
+0x24  u32  a checksum of the nine words before it (computed by 0x800087D4, not read)
```

The loader computes the first sector of block 3 as the table's first sector plus its size in
sectors (0x800098D4; 67 + 95 = 162 on part0) rather than reading +0x0C. The other words were
not read. Our reader does not parse the home block; it uses the values
seen on part0 as constants (`FAT_OFFSET`, `PART_BASE`).

### Blocks

Data is addressed in 4096-byte blocks. Block `b` (b >= 3) starts at sector `162 + (b-3)*8`,
which is byte `70656 + b*4096` (`PART_BASE` is 70656 = 0x11400). The loader computes it this
way at 0x80009714. Block numbers 0 to 2 are not data: the space they would occupy is the
tail of the chain table.

### Chain table

A table of 16-bit entries, one per block, starts at sector 67 (byte 34,304 = 0x8600). The
entry of block `b` is the u16 at `34304 + 2*(b-3)`; the loader keeps 1,024 entries (4 sectors)
cached and reads the entry with 0x80007CCC (index `b - first`, word `index/2`, low half for an
even index, high half for an odd one: that is the little-endian u16 array). The table ends
before block 3 (part0 has 24,107 blocks, so it needs 48,208 bytes, ending at 0x14250).

An entry holds the number of the block that follows `b` in its file. Values below 3 are not
block numbers:

- 0: end of the chain (every file on part0 ends with 0);
- 1: free (the usage routine at 0x8000972C counts every entry other than 1 as used, and the
  allocator at 0x80007D40 takes the first entry equal to 1; 5,504 entries on part0).

The loader's seek routine (0x80008F90) walks the chain one entry per 4096 bytes of file
offset and stops with an error at a link below 3; the read routine (0x80009040) takes the
next block the same way. The file size, from the directory, bounds the read. A file's blocks are therefore: the directory's data block, then
`entry(that block)`, and so on, as many blocks as `ceil(size / 4096)`.

### Directory

The root directory is block 3. A directory is a run of 24-byte entries:

```
+0   12 bytes  name, NUL padded, with each 4-byte group stored byte-reversed
                (bytes 3,2,1,0, 7,6,5,4, 11,10,9,8); truncate at the first NUL
+12  int32     length of the file in 32-bit words (bytes = length * 4)
+16  int32     unknown (ignored)
+20  int32     first data block of the file
```

Entries are read sequentially. An entry with a blank name is not a file: its length field is
the block number of the next directory block, and reading continues there. In the real image
these link entries appear at offset 4080 of each directory block (the last 24-byte slot);
their other two fields are garbage. The chain ends at a blank entry whose length is 0, or,
as observed on part0, whose link points past the end of the partition. Names are at most
12 bytes, so there are no subdirectories; files are flat.

### File data

File data is the file's blocks in chain order, truncated to `length * 4` bytes.

**Correction.** Our first extractor, like the public research scripts it followed, read
`length * 4` contiguous bytes from the first data block. That is right only when a file's
blocks are adjacent. On part0, 1,353 of the 1,356 files have adjacent blocks; three do not,
and their contiguous reading was wrong:

- `GAME.EXE`: 224 adjacent blocks from 16127, then block 18828 for the last 556 bytes
  (the contiguous reading took block 16351, which is DIAG.EXE's first block; 315 bytes
  differ);
- `HSTD02.WMS` and `HSTD05.WMS` (high-score tables, 65,576 bytes each; 8,394 and 51,969 bytes
  differ).

The three files total 1,049,212 bytes. The contiguous extraction of `GAME.EXE` had SHA-256
`47aea64a...da9e`; that value was pinned until Task 5b and is wrong.

## Validation done by our reader

- raw header magic, plausible partition count, every partition inside the image;
- the directory chain must not revisit a block;
- every file's block chain is followed before anything is written: a link that revisits a
  block, a block outside the partition, or a chain that ends (link below 3) before the file's
  size is covered raises `DiskFormatError`;
- names containing `..` are rejected; `/` and `\` in names are replaced with `_`; duplicate
  names after that are rejected, so nothing is written outside the output directory.

## The game's own check: FILESYS.CHK

`FILESYS.CHK` (442,180 bytes on part0) is a table of per-file checksums. Verified layout:

```
records of 0x126 bytes, 1,504 of them, then
u32  POSIX cksum of all the records (0x144B243B on part0)

record:
+0x00  4 bytes   "LFN3" in every record
+0x04  22 bytes  unknown (some bytes vary between records)
+0x1A  u32       file size in bytes
+0x1E  ...       file name, NUL terminated
+0x122 u32       POSIX cksum of the file
```

The public research notes put the size at 0x1B and the checksum at 0x123; both are one byte
off. The checksum is the POSIX `cksum` CRC (the algorithm of `exe.posix_cksum`, the same as
the EXE header checksum, see `exe-format.md`); zlib CRC-32 does not match.

`python3 -m carnevil_tools verify <partition>` reads every file in memory by following the
chains and compares it with the table. Result on part0:

- 1,504 files listed; 1,339 are on the partition and all 1,339 match (size and checksum);
  0 mismatches;
- 165 listed files are not on the partition;
- 17 files on the partition are not listed: `ADJUST.FMT`, `AUDITS.FMT`, `CREDITS.RAW`,
  `CREDITSB.RAW`, `DIAG.EXE`, `FILESYS.CHK`, `GAME.EXE`, `HSTD01.WMS` to `HSTD10.WMS`.

None of the three non-adjacent files is listed, so this check does not itself exercise the
chain fix (the contiguous reading also matched all 1,339); it confirms that the listed files,
all with adjacent blocks, are read correctly. GAME.EXE and DIAG.EXE are covered by their
header checksums instead, and GAME.EXE by the RAM image the loader produced in MAME
(`tests/test_disk.py`, `test_game_exe_matches_loader`). The two high-score tables are covered
only by the chain itself.

## Verified values (part0)

- 1,356 files
- `GAME.EXE` SHA-256 `f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6`
- `DIAG.EXE` SHA-256 `38b2810864c85d6971cedc3a6b0e507c8c6d593e4d7c355c880f91dbcaaaa2b9`
  (unchanged: its blocks are adjacent)
