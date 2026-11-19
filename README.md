# reincarnevilation

A native port of CarnEvil (Midway, 1998, Seattle arcade hardware) by static recompilation:
the game's own MIPS code is translated to C ahead of time and linked against a runtime that
stands in for the arcade board. The first target is PlayStation 4 Linux, where emulation is
too slow for full speed.

## Status

Early. Milestone M0 (tools that extract the game's files and analyse its executable) is in
progress on the [`m0-analysis`](../../tree/m0-analysis) branch, which holds the code, tests and
analysis notes. It merges here when M0 is complete. Nothing is playable yet; the
[roadmap](docs/roadmap.md) has the details.

## BYOCHD: bring your own CHD

This repository contains tools, notes and (later) the recompiler and runtime. It contains no
game code, assets, ROMs or disk images, and none will be added. Everything game-specific is
produced on your machine from your own `carnevil.chd`.

## Documentation

- [How it works](docs/how-it-works.md): static recompilation and how the port is put together.
- [The arcade hardware](docs/hardware.md): what is on the Seattle board and how each part is handled.
- [Roadmap and status](docs/roadmap.md): milestones, progress and findings so far.
- [Getting started](docs/getting-started.md) and the [tools reference](docs/tools.md): using the
  analysis tools (on the `m0-analysis` branch for now).
- [All documentation](docs/README.md)

## Credits

- The disk format was learned from [xclusivor/CarnEvil-Research](https://github.com/xclusivor/CarnEvil-Research),
  itself based on daemon1's tools from the XenTaX forums.
- MAME's `seattle` driver is the hardware reference.
- The recompiler will build on [N64Recomp](https://github.com/N64Recomp/N64Recomp).

## Licence

MIT, see `LICENSE`. It covers the code and notes in this repository, not the game.
