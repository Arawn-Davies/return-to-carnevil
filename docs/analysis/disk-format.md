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
holds the game files (1,523 of them). The other 16 partitions have no home-block magic and
hold no file system of this kind (measured: `list_files` refuses each of part1 to part16).

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
  allocator at 0x80007D40 takes the first entry equal to 1 at or after a search hint, sets it
  to 0 and moves the hint past it; the mount and unlink set the hint to 3; read. 5,504 entries
  on part0).

On part0 every entry other than 1 belongs to a file's chain or is a directory block, whose
entry is 0 (measured: the 18,591 blocks of the 1,523 files and the 9 directory blocks account
for all 18,600 used entries).

The loader's seek routine (0x80008F90) walks the chain one entry per 4096 bytes of file
offset and stops with an error at a link below 3; the read routine (0x80009040) takes the
next block the same way. The file size, from the directory, bounds the read. A file's blocks are therefore: the directory's data block, then
`entry(that block)`, and so on, as many blocks as `ceil(size / 4096)`.

### Directory

The root directory is block 3. A directory block holds 170 entries of 24 bytes (bytes 0 to
4079), then 12 unused bytes (zero on part0), then at byte 4092 a u32 link to the next
directory block (read: the lookup at 0x80008094 scans 0xAA entries per block and then loads
the word at byte 0xFFC of the block). Entry:

```
+0   12 bytes  name, NUL padded, with each 4-byte group stored byte-reversed
                (bytes 3,2,1,0, 7,6,5,4, 11,10,9,8); truncate at the first NUL
+12  int32     length of the file in 32-bit words (bytes = length * 4)
+16  u32       date and time: high half the DOS date, low half the DOS time (measured:
                `BTOP.ZM` has 0x254E7BEE, 14 October 1998 15:31:28; some files hold 0)
+20  int32     first data block of the file (the loader reads its low half, read)
```

- **Free entry.** An entry whose first name word is zero is free. The loader's unlink
  (0x8000884C) zeroes that word and frees the chain from the entry's first block, leaving the
  rest of the entry as it was (read). Its find-or-create routine (0x80008198) takes the
  first entry, in scan order, that is either free or holds the name; for a name it already
  holds it frees the old chain and reuses the entry (read). A free entry is not the end of
  the directory: the scan goes on past it.
- **End.** A link of 0 ends the directory. The lookup treats 0 as "back to block 3", since
  it scans circularly from the block of its last hit until it returns to where it started
  (read, 0x80008094: `movz` of 3 when the link is 0). When find-or-create reaches a link of 0
  with no free entry seen, it allocates a block (0x80007D40), stores its number in the link
  word, zeroes the new block and continues there (read, 0x80008298 to 0x80008308).
- Names are at most 12 bytes, so there are no subdirectories; files are flat.

On part0 the directory is 9 blocks: 3, 5541, 7453, 8691, 9957, 11188, 12473, 14313, 16909.
The first seven are full; block 14313 holds a free entry at slot 166 and block 16909 one at
slot 164 (both deleted files whose first name word is zero) and five never-used, all-zero
slots, 165 to 169 (measured).

**Correction (2026-11-19).** M0's reader and this document read the directory as a run of
entries in which any blank name was a link (its length field the next block), ending at a
blank entry of length 0 or at a link past the end of the partition. That misread the free
entry at block 14313, slot 166 (first name word zero, length 0x20000 words) as a link past
the end, and stopped there: it listed 1,356 files and missed 167. Shown by the entries after
it: `GLASS.WMS`, `NOBL1X1.WMS` and `NOBL1X2.WMS` in slots 167 to 169 have valid chains that
share no block with any other file and match `FILESYS.CHK` in size and checksum, and block
14313's link word at byte 4092 leads to a ninth directory block, 16909. With the rules above
the reader lists 1,523 files, and all 1,504 files `FILESYS.CHK` lists are present and match.
The rules were then confirmed in the loader's code (the addresses above). The extracted
folder made by M0 lacks the 167 files and needs regenerating.

### File data

File data is the file's blocks in chain order, truncated to `length * 4` bytes.

**Correction.** Our first extractor, like the public research scripts it followed, read
`length * 4` contiguous bytes from the first data block. That is right only when a file's
blocks are adjacent. On part0, 1,520 of the 1,523 files have adjacent blocks; three do not,
and their contiguous reading was wrong:

- `GAME.EXE`: 224 adjacent blocks from 16127, then block 18828 for the last 556 bytes
  (the contiguous reading took block 16351, which is DIAG.EXE's first block; 315 bytes
  differ);
- `HSTD02.WMS` and `HSTD05.WMS` (high-score tables, 65,576 bytes each; 8,394 and 51,969 bytes
  differ).

The three files total 1,049,212 bytes. The contiguous extraction of `GAME.EXE` had SHA-256
`47aea64a...da9e`; that value was pinned before the extractor followed the block chain, and is wrong.

## Validation done by our reader

- raw header magic, plausible partition count, every partition inside the image;
- the directory chain must not revisit a block, and each link must name a block inside the
  partition; an entry whose first name word is not zero but whose name is blank is refused;
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

- 1,504 files listed; all 1,504 are on the partition and all match (size and checksum);
  0 mismatches; none missing;
- 19 files on the partition are not listed: `ADJUST.FMT`, `AUDITS.FMT`, `CREDITS.RAW`,
  `CREDITSB.RAW`, `DIAG.EXE`, `FILESYS.CHK`, `GAME.EXE`, `GAMEINF.REV`, `HSTD01.WMS` to
  `HSTD10.WMS`, `J.PRC`.

Before the directory correction above, M0 reported 1,339 matches, 165 listed files missing
and 17 unlisted; the 165 and `GAMEINF.REV` and `J.PRC` are the 167 files it had missed.

None of the three non-adjacent files is listed, so this check does not itself exercise the
chain fix; it confirms that the listed files,
all with adjacent blocks, are read correctly. GAME.EXE and DIAG.EXE are covered by their
header checksums instead, and GAME.EXE by the RAM image the loader produced in MAME
(`tests/test_disk.py`, `test_game_exe_matches_loader`). The two high-score tables are covered
only by the chain itself.

## Verified values (part0)

- 1,523 files in 9 directory blocks, 2 free (deleted) entries and 5 never-used slots
- `GAME.EXE` SHA-256 `f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6`
- `DIAG.EXE` SHA-256 `38b2810864c85d6971cedc3a6b0e507c8c6d593e4d7c355c880f91dbcaaaa2b9`
  (unchanged: its blocks are adjacent)
