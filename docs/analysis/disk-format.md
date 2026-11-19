# CarnEvil disk image format

The arcade hard disk (CHD converted to a raw image) holds a small header, then a number of
partitions. Each partition holds a flat custom file system. All integers are little-endian
32-bit signed. The layout below was learned from the two scripts in
https://github.com/xclusivor/CarnEvil-Research (`scripts/split_raw_img.py`,
`scripts/split_partition.py`, themselves a rewrite of daemon1's `carnevilraw.exe` /
`carnevilpart.exe`) and verified against the real image. Our implementation is
`carnevil_tools/disk.py`.

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

Everything is addressed in 4096-byte blocks relative to a base of `70656` (0x11400) bytes
into the partition: block `b` lives at `70656 + b*4096`.

The root directory is block 3. A directory is a run of 24-byte entries:

```
+0   12 bytes  name, NUL padded, with each 4-byte group stored byte-reversed
                (bytes 3,2,1,0, 7,6,5,4, 11,10,9,8); truncate at the first NUL
+12  int32     length of the file in 32-bit words (bytes = length * 4)
+16  int32     unknown (ignored)
+20  int32     data block of the file
```

Entries are read sequentially. An entry with a blank name is not a file: its length field is
the block number of the next directory block, and reading continues there. In the real image
these link entries appear at offset 4080 of each directory block (the last 24-byte slot);
their other two fields are garbage. The chain ends at a blank entry whose length is 0, or,
as observed on part0, whose link points past the end of the partition. Names are at most
12 bytes, so there are no subdirectories; files are flat.

File data is `length * 4` bytes starting at `70656 + data_block * 4096`.

## Validation done by our reader

- raw header magic, plausible partition count, every partition inside the image;
- every file lies inside the partition (a truncated partition raises `DiskFormatError`
  before anything is written);
- the directory chain must not revisit a block;
- names containing `..` are rejected; `/` and `\` in names are replaced with `_`; duplicate
  names after that are rejected, so nothing is written outside the output directory.

## Verified values (part0)

- 1,356 files
- `GAME.EXE` SHA-256 `47aea64a...da9e`, `DIAG.EXE` SHA-256 `38b28108...a2b9`
