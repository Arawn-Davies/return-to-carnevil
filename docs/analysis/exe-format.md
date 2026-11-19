# EXE format

`GAME.EXE` and `DIAG.EXE` (in `part0_files/`) share one layout: an 8-byte
little-endian header followed by the raw memory image, with no sections,
relocations or symbols.

    offset 0   u32  header word 0 (meaning: see below)
    offset 4   u32  load address (KSEG0 virtual address)
    offset 8   ...  image, copied to the load address
                    end address = load address + (file size - 8)

Loader: `carnevil_tools/exe.py` (`load_exe`). It refuses any file whose
SHA-256 differs from the pinned value (`UnknownRevisionError`).

## Observed values

GAME.EXE (sha256 47aea64a...da9e)
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

Checked against the image bytes (everything after the 8-byte header), for both
EXEs, in the order the plan specified:

- 32-bit sum of image words: no match (GAME 0x5BE871F4, DIAG 0xDB9BBC9C)
- sum of bytes, mod 2^32: no match (GAME 0x034AD22D, DIAG 0x01AF01D1)
- CRC-32 (zlib): no match (GAME 0x553CF19D, DIAG 0x99750A74)
- negated word sum: no match (GAME 0xA4178E0C, DIAG 0x24644364)

Also tried, not in the plan: XOR of all words, no match (GAME 0x885B64D6,
DIAG 0x0A22B774).

Result: header word 0 is not identified by these four checks (nor by the XOR
extra). It could be a checksum over a different range or with a different
algorithm, or not a checksum at all. Task 5 should confirm it from the boot
ROM's loader.
