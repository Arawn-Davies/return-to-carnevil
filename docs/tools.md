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

Walks the file system inside a partition and writes out its files. Each file is read by following its chain of 4 KB blocks through the partition's chain table, the way the boot ROM's loader reads it; reading a file's blocks as one contiguous run, as the public research scripts do, gets three files on partition 0 wrong, `GAME.EXE` among them. The whole directory and every file's chain are validated first: a damaged partition stops with an error and leaves the output folder empty. File names that would escape the output folder are refused.

The layout of the partition table and the file system is described in [analysis/disk-format.md](analysis/disk-format.md).

## verify

```
python3 -m carnevil_tools verify <partition file>
```

Reads every file of a partition in memory, following the block chains, and compares its size and checksum with the game's own table `FILESYS.CHK`. It prints how many listed files match, how many listed files are not on the partition, and the name of each file that does not match; it exits with an error when any file does not match. Nothing is written.

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

## disasm

```
python3 -m carnevil_tools disasm <file> --base <addr> --start <addr> --count <n>
```

Decodes `n` words of any file, starting at address `start`, and prints them to the terminal; nothing is written. `--base` is the address of the file's first byte: `0xBFC00000` for `boot.u32`, `0x800C3FF8` for `GAME.EXE` and `0x800FFFF8` for `DIAG.EXE` (their 8-byte header comes first). The analysis notes name addresses only; this is how to look at the code behind them on your own files.

## hwcensus

```
python3 -m carnevil_tools hwcensus <GAME.EXE> [--ioctl]
python3 -m carnevil_tools hwcensus --loader <boot.u32>
```

Lists the hardware registers the code touches, grouped by region (Voodoo, frame buffer, texture memory, IOASIC, CMOS, gun, system controller and so on), with read and write counts and the functions that make the accesses. It follows register values through each function and across calls to work out the addresses.

- With an EXE it analyses `GAME.EXE`, using the function list in `config/game.functions.toml`. `--ioctl` also lists the game's ioctl calls into the boot ROM's loader: the device and command number of each call.
- With `--loader` it analyses the boot ROM's resident loader instead, and labels each access with what reaches it: the services the game calls, the exception vectors, the loader's own start-up or one of its device drivers.

The static census cannot see every access; [analysis/hardware-usage.md](analysis/hardware-usage.md) says where it falls short and gives the measurements taken in MAME.

## The modules behind them

- `disk.py`: partition table, block chains and file system, and the `FILESYS.CHK` check.
- `exe.py`: the EXE header, its checksum, loading the image, and the revision check.
- `mips.py`: the MIPS IV instruction decoder.
- `functions.py` and `naming.py`: function discovery, and naming functions from the game's own debug messages.
- `hwcensus.py`: the hardware regions of the board, the register-value propagation behind `hwcensus`, the loader analysis and the ioctl list.
- `cli.py`: the command line.
