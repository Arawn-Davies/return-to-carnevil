# reincarnevilation

A native port of CarnEvil (Midway, 1998, Seattle arcade hardware) by static recompilation:
the game's own MIPS code is translated to C ahead of time and linked against a runtime that
stands in for the arcade board. Builds come in this order: macOS, then Linux (including WSL2),
then PlayStation 4 Linux, the machine the project exists for, where emulation is too slow for
full speed.

Status: early. Milestone M0 (extraction and analysis tooling) is in progress. See
`docs/superpowers/specs/` for the design and `docs/superpowers/plans/` for the plan.

## BYOCHD: bring your own CHD

This repository contains tools, notes and (later) the recompiler and runtime. It contains no
game code, assets, ROMs or disk images, and none will be added. Everything game-specific is
produced on your machine from your own `carnevil.chd`:

```
chdman extractraw -i carnevil.chd -o work/carnevil.raw
python3 -m carnevil_tools split work/carnevil.raw -o work/parts
python3 -m carnevil_tools extract work/parts/part0 -o work/part0_files
```

Point the tools and tests at that folder with `CARNEVIL_WORK=/path/to/work`. One revision is
supported for now: the `GAME.EXE` whose SHA-256 is
`47aea64a3e5d15c15f28b3907294e706f078a4e9a47c422f551f420d9ba7da9e`. Other revisions are refused
with a message showing both hashes.

## Running the tests

Python 3, standard library only.

```
python3 -m unittest discover -s tests -v
```

Tests that need game files skip when they are not there, so the suite passes on a clean checkout.

## Documentation

Start at [`docs/README.md`](docs/README.md): how it works, getting started, the tools, the arcade hardware, and the roadmap.

## Layout

- `carnevil_tools/`: disk image, EXE, MIPS IV decoding and analysis tools
- `tests/`: unit tests on synthetic fixtures, plus checks against your own game files
- `docs/analysis/`: what has been learned about the formats and the binary (addresses, counts,
  layouts; no code listings)
- `docs/`: guides for people, plus `docs/superpowers/`, the design and plan written to drive AI coding agents

## Credits

- The disk format was learned from [xclusivor/CarnEvil-Research](https://github.com/xclusivor/CarnEvil-Research),
  itself based on daemon1's tools from the XenTaX forums. See `docs/analysis/disk-format.md`.
- MAME's `seattle` driver is the hardware reference.
- The recompiler will build on [N64Recomp](https://github.com/N64Recomp/N64Recomp).

## Licence

MIT, see `LICENSE`. It covers the code and notes in this repository, not the game.
