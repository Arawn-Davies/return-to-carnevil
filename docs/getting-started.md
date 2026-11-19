# Getting started

Right now the project is at the analysis stage: you can extract the game's files from your own disk image and run the analysis tools. There is no playable build yet. See the [roadmap](roadmap.md).

## What you need

- Python 3.12 or newer. The tools use the standard library only: nothing to install.
- `chdman` (it ships with MAME) to unpack the CHD.
- Your own `carnevil.chd`.
- About 6 GB of free disk space for the unpacked image and extracted files.

## 1. Unpack your CHD

```
mkdir work
chdman extractraw -i carnevil.chd -o work/carnevil.raw
```

This gives a raw hard disk image of 4,310,046,720 bytes.

## 2. Split it into partitions and extract the files

```
python3 -m carnevil_tools split work/carnevil.raw -o work/parts
python3 -m carnevil_tools extract work/parts/part0 -o work/part0_files
```

The disk has 17 partitions. Partition 0 holds the 1,356 files the game runs from, including `GAME.EXE` (the game), `DIAG.EXE` (the operator diagnostics), textures (`.WMS`), models (`.ZM`), animations (`.ZA`) and sound banks (`.BNK`).

## 3. Tell the tools where the folder is

```
export CARNEVIL_WORK=/path/to/work
```

The built-in default is the author's own path, so set this.

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

The unit tests build their own small fixtures and need no game files. The tests that check your game files skip when the files are not there, so the suite passes on a clean checkout.

## Supported revision

One revision of the game is supported for now, identified by the SHA-256 of `GAME.EXE`:

```
f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6
```

Loading a different `GAME.EXE` stops with a message that shows the expected hash and yours. Other revisions can be added later; each needs its own analysis output.

## Keeping game files out of git

The `.gitignore` excludes disk images, EXEs, ROMs and the usual work folders. If you contribute, please keep it that way: no game files, no extracted assets, no disassembly listings, and no generated C. Addresses, sizes, counts and format descriptions are fine.
