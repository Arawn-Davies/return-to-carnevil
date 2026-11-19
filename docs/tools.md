# Tools reference

All tools live in the `carnevil_tools` Python package and run as `python3 -m carnevil_tools <command>`. They use the standard library only. Add `-h` to any command for its options.

## split

```
python3 -m carnevil_tools split <raw image> -o <output folder>
```

Reads the partition table of the raw disk image and writes each partition to its own file (`part0` to `part16`). It checks the table before writing anything, so a truncated image is refused rather than half-split.

## extract

```
python3 -m carnevil_tools extract <partition file> -o <output folder>
```

Walks the file system inside a partition and writes out its files. The whole directory is validated first: a damaged partition stops with an error and leaves the output folder empty. File names that would escape the output folder are refused.

The layout of the partition table and the file system is described in [analysis/disk-format.md](analysis/disk-format.md).

## census

```
python3 -m carnevil_tools census <GAME.EXE>
```

Decodes every word of the image as a MIPS instruction and prints how many times each one appears, along with the addresses of the first words that are not valid instructions. Because the image holds data as well as code, some counts are inflated by data that happens to look like instructions. The census restricted to real code is in [analysis/functions.md](analysis/functions.md).

## functions

```
python3 -m carnevil_tools functions <GAME.EXE> -o <output.toml>
```

Finds the functions in the game: it follows the code from the entry point through every call and branch, resolves jump tables, picks up functions that are only referenced through pointers, and reports the ranges it could not reach. The output file lists the code range, each function (address, size, name, how it was found), each jump table and the unreached ranges. This file is what the recompiler will read.

The copy produced from the supported revision is committed as `config/game.functions.toml`.

## The modules behind them

- `disk.py`: partition table and file system.
- `exe.py`: the EXE header, loading the image, and the revision check.
- `mips.py`: the MIPS IV instruction decoder.
- `functions.py` and `naming.py`: function discovery, and naming functions from the game's own debug messages.
- `cli.py`: the command line.
