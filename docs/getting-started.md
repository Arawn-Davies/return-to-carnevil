# Getting started

Right now the project is at the analysis stage: you can extract the game's files from your own disk image and run the analysis tools. There is no playable build yet. See the [roadmap](roadmap.md).

## What you need

- Python 3.12 or newer. The tools use the standard library only: nothing to install.
- `chdman` (it ships with MAME) to unpack the CHD.
- Your own `carnevil.chd`.
- About 8 GB of free disk space: the raw image (4.3 GB), the 17 partitions (3.2 GB) and the extracted files (0.07 GB). After splitting, only `work/parts/part0` is needed, so the raw image and the other 16 partitions can be deleted.

## 1. Unpack your CHD

```
mkdir work
chdman extractraw -i carnevil.chd -o work/carnevil.raw
```

This gives a raw hard disk image of 4,310,046,720 bytes.

## 2. Split it into partitions, check partition 0 and extract its files

```
python3 -m carnevil_tools split work/carnevil.raw -o work/parts
python3 -m carnevil_tools verify work/parts/part0
python3 -m carnevil_tools extract work/parts/part0 -o work/part0_files
```

`verify` reads every file of partition 0 in memory and checks it against the game's own table `FILESYS.CHK`. On a good image it prints `1339 of 1504 listed files match`, `165 listed files are not on the partition` (the table lists more than this partition holds) and `17 files on the partition are not listed (not checked)`, and no mismatch. `GAME.EXE` and `DIAG.EXE` are among the 17: they are checked instead by the EXE gate (SHA-256 and header checksum, below) when `census` or `functions` first loads them.

The disk has 17 partitions. Partition 0 holds the 1,356 files the game runs from, including `GAME.EXE` (the game), `DIAG.EXE` (the operator diagnostics), textures (`.WMS`), models (`.ZM`), animations (`.ZA`) and sound banks (`.BNK`).

## 3. Tell the tools where the folder is

```
export CARNEVIL_WORK=/path/to/work
```

The built-in default is the author's own path, so set this. The boot ROM is not on the disk; it is `boot.u32` in MAME's `carnevil` set. The tests that read it run only when you name it:

```
export CARNEVIL_BOOT_ROM=/path/to/boot.u32
```

`hwcensus --loader` takes the path on its command line.

## 4. Run the analysis tools

```
python3 -m carnevil_tools census work/part0_files/GAME.EXE
python3 -m carnevil_tools functions work/part0_files/GAME.EXE -o my.functions.toml
```

The first counts the MIPS instructions in the game. The second finds its functions and jump tables. See the [tools reference](tools.md).

## 5. Run the tests

```
python3 -m unittest discover -s tests -v
```

The unit tests build their own small fixtures and need no game files. The tests that check your game files skip when the files are not there, so the suite passes on a clean checkout. `python3 -m unittest` and `python3 -m unittest tests.test_disk` (one module) work as well.

With your files in place, two tests still skip, because they read files only the developer has; their skips are expected:

- `test_contiguous_extraction_reported_as_mis_extracted` reads `part0_files_contiguous_wrong/GAME.EXE`, a copy extracted the wrong way (contiguously) on purpose.
- `test_game_exe_matches_loader` reads `mame/dumps/ram-at-game-entry.bin`, the RAM dump `tools/mame/handoff.lua` saves in MAME.

The boot-ROM tests skip unless `CARNEVIL_BOOT_ROM` is set (step 3).

## Supported revision

One revision of the game is supported for now, identified by the SHA-256 of `GAME.EXE`:

```
f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6
```

Loading a different `GAME.EXE` stops with one of two messages: an intact file of another revision gets "unknown EXE revision" with the expected hash and yours; a damaged or mis-extracted file (its header checksum fails, for example after extracting with a tool that reads files as contiguous blocks) gets a checksum error that names the expected hash and yours. Other revisions can be added later; each needs its own analysis output.

## Keeping game files out of git

The `.gitignore` excludes disk images, EXEs, ROMs and the usual work folders. If you contribute, please keep it that way: no game files, no extracted assets, no disassembly listings, and no generated C. Addresses, sizes, counts and format descriptions are fine.
