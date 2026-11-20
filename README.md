# Return to CarnEvil

A native port of CarnEvil (Midway, 1998, Seattle arcade hardware) by static recompilation:
the game's own MIPS code is translated to C ahead of time and linked against a runtime that
stands in for the arcade board. Builds come in this order: macOS, then Linux (including WSL2),
then PlayStation 4 Linux, the machine the project exists for, where emulation is too slow for
full speed.

Status: early, but the game boots. Milestones M0 (extraction and analysis), M1 (the
recompiler: all of `GAME.EXE` translated to C, checked against MAME) and M2 (the runtime:
the game runs headless into its attract mode) are done; see the
[M0 report](docs/analysis/M0-report.md), the [M1 report](docs/analysis/M1-report.md) and the
[M2 report](docs/analysis/M2-report.md). Nothing is playable yet: there is no picture, sound
or input, and pictures are next. See `docs/superpowers/specs/` for the design and
`docs/superpowers/plans/` for the plans.

M2 put a runtime under the translated code: native stand-ins for the boot ROM's loader
services, the hardware the game drives itself (including enough of the Voodoo to pass Glide's
start-up checks), interrupts, and the game's own cooperative tasks. The program `carnevil`
runs your `GAME.EXE` from your disk image, which it only reads, for 150 seconds of game time
into attract mode, and its console output, file activity and task activity match MAME's.

## BYOCHD: bring your own CHD

This repository contains tools, notes, the recompiler and the runtime. It contains no
game code, assets, ROMs or disk images, and none will be added. Everything game-specific is
produced on your machine from your own `carnevil.chd`:

```
chdman extractraw -i carnevil.chd -o work/carnevil.raw
python3 -m carnevil_tools split work/carnevil.raw -o work/parts
python3 -m carnevil_tools verify work/parts/part0
python3 -m carnevil_tools extract work/parts/part0 -o work/part0_files
```

`verify` checks every file the game's own table `FILESYS.CHK` lists against its recorded size
and checksum, and says how many files on the partition the table does not list. `GAME.EXE` and
`DIAG.EXE` are among those; they are checked instead by the EXE gate when `census` or
`functions` first loads them.

Point the tools and tests at that folder with `CARNEVIL_WORK=/path/to/work`, and the boot-ROM
tests at your `boot.u32` (from the MAME `carnevil` set) with `CARNEVIL_BOOT_ROM=/path/to/boot.u32`. One revision is
supported for now: the `GAME.EXE` whose SHA-256 is
`f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6`. Any other file is refused
with a message showing both hashes: an intact file of another revision as an unknown
revision, a damaged or mis-extracted one as a checksum error.

## Translating the game to C

With a C compiler (`cc`) and GNU `make`, and `CARNEVIL_WORK` set:

```
make recomp       # GAME.EXE to C, in $CARNEVIL_WORK/generated
make linkcheck    # compile and link it all against a stub runtime
```

The generated C is made from your own `GAME.EXE` and stays in your work folder; it is never
committed. See [getting started](docs/getting-started.md#5-translate-the-game-to-c).

## Running the game, headless

```
make carnevil     # the game and the runtime, in $CARNEVIL_WORK/build/<system>-<machine>
mkdir -p work/run
make run DISK=work/carnevil.raw SECONDS=20 OVERLAY=work/run/carnevil.overlay NVRAM=work/run/carnevil.nvram
```

It prints the game's console output and stops after 20 seconds of game time. No window, no
sound, no input yet. See [getting started](docs/getting-started.md#7-build-and-run-the-game-headless).

## Running the tests

Python 3, standard library only; the tests that compile C need `cc`.

```
python3 -m unittest discover -s tests -v
```

Tests that need game files skip when they are not there, so the suite passes on a clean checkout.
The tests that compare with MAME also skip until you make their captures with the scripts in
`tools/mame/`, and one always skips because it reads a file only the developer keeps; see
[getting started](docs/getting-started.md#8-run-the-tests).

## Documentation

Start at [`docs/README.md`](docs/README.md): how it works, getting started, the tools, the arcade hardware, and the roadmap.

## Layout

- `carnevil_tools/`: disk image, EXE, MIPS IV decoding and analysis tools, and the recompiler
- `runtime/`: the contract header the generated C uses, the reference interpreter, the stub
  runtimes, and the runtime and program `carnevil`
  ([analysis/runtime.md](docs/analysis/runtime.md))
- `config/`: the function list and boot state the recompiler and the runtime read
- `tools/mame/`: the MAME scripts behind the checks
- `tests/`: unit tests on synthetic fixtures, plus checks against your own game files
- `docs/analysis/`: what has been learned about the formats and the binary (addresses, counts,
  layouts; no code listings)
- `docs/`: guides for people, plus `docs/superpowers/`, the design and plan written to drive AI coding agents

## Credits

- The disk format was learned from [xclusivor/CarnEvil-Research](https://github.com/xclusivor/CarnEvil-Research),
  itself based on daemon1's tools from the XenTaX forums. See `docs/analysis/disk-format.md`.
- MAME's `seattle` driver is the hardware reference. The runtime's Voodoo model follows the
  rules of MAME's BSD-licensed Voodoo and GT64010 sources, with the code written fresh (see
  `docs/analysis/hardware-usage.md`, "The Voodoo model").
- The shape of the recompiler's output follows [N64Recomp](https://github.com/N64Recomp/N64Recomp).

## Licence

MIT, see `LICENSE`. It covers the code and notes in this repository, not the game.
