# EXE format

`GAME.EXE` and `DIAG.EXE` (in `part0_files/`) share one layout: an 8-byte
little-endian header followed by the raw memory image, with no sections,
relocations or symbols.

    offset 0   u32  header word 0: POSIX cksum of the file from offset 4 (below)
    offset 4   u32  load address (KSEG0 virtual address)
    offset 8   ...  image, copied to the load address
                    end address = load address + (file size - 8)

Loader: `carnevil_tools/exe.py` (`load_exe`). A file whose header word 0 is
not the checksum of the rest is refused with `ChecksumError` (a `ValueError`:
the file is damaged or was mis-extracted, for example by a tool that reads
files as contiguous blocks); when the SHA-256 also differs from the pinned
value, the message names the supported revision's hash. An intact file (checksum
holds) whose SHA-256 differs is refused with `UnknownRevisionError`: another
revision. The checksum is verified even when the SHA-256 check is skipped
(`expect_sha256=None`).

## Observed values

GAME.EXE (sha256 f9455ce8...d3a6, extracted by following the block chain;
see `disk-format.md`)
- header word 0: 0x2CAE3F54
- load address: 0x800C4000
- end address: 0x801A4224 (image 918,052 bytes)
- first word at the load address: 0x3C1D8080

DIAG.EXE (sha256 38b28108...a2b9)
- header word 0: 0x5799F785
- load address: 0x80100000
- end address: 0x801725CC (image 468,428 bytes)

Both images are a multiple of 4 bytes long.

## Header word 0

Header word 0 is the POSIX `cksum` CRC of the file's bytes from offset 4 to the
end, that is the load-address word followed by the image: CRC-32 with
polynomial 0x04C11DB7, MSB first, initial value 0, over the data followed by
its byte length as a minimal little-endian byte string, then complemented.
`exe.posix_cksum` implements it (check value: `123456789` gives 0x377A6011).
The boot ROM's loader computes it before starting an EXE and refuses to run one
that does not match (`boot-handoff.md`, load-and-run step 5).

- GAME.EXE: 0x2CAE3F54
- DIAG.EXE: 0x5799F785

The same algorithm covers the game's per-file table `FILESYS.CHK`
(`disk-format.md`).

### Ruled out first

Before the loader was read, these were checked against the image bytes
(everything after the 8-byte header) and did not match. The GAME values below
were computed from the contiguously extracted file, whose last 556 bytes were
wrong (`disk-format.md`), so they are not the values of the correct file; DIAG
is unaffected. The checks were over the wrong range anyway (the image, not
offset 4 onwards).

- 32-bit sum of image words: no match (GAME 0x5BE871F4, DIAG 0xDB9BBC9C)
- sum of bytes, mod 2^32: no match (GAME 0x034AD22D, DIAG 0x01AF01D1)
- CRC-32 (zlib): no match (GAME 0x553CF19D, DIAG 0x99750A74)
- negated word sum: no match (GAME 0xA4178E0C, DIAG 0x24644364)
- XOR of all words: no match (GAME 0x885B64D6, DIAG 0x0A22B774)
