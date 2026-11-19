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

Walks the file system inside a partition and writes out its files. Each file is read by following its chain of 4 KB blocks through the partition's chain table, the way the boot ROM's loader reads it; reading a file's blocks as one contiguous run, as the public research scripts do, gets three files on partition 0 wrong, `GAME.EXE` among them. The whole directory and every file's chain are validated first: a damaged partition stops with an error and leaves the output folder empty. A partition without the home-block magic (0x34011280 at byte 1024) is refused as not a CarnEvil partition, and a block that two files share, or two names that differ only in case, stop the extraction. File names with a `..` component are refused, and `/` or `\` in a name becomes `_`. Each file (and each partition written by `split`) is written under a `.partial` name and renamed when it is complete, so an interrupted run leaves no file that looks whole. Errors from the file system, such as a full disk or a folder you cannot write to, are reported as `error: …` with exit status 1.

The layout of the partition table and the file system is described in [analysis/disk-format.md](analysis/disk-format.md).

## verify

```
python3 -m carnevil_tools verify <partition file>
```

Reads every file of a partition in memory, following the block chains, and compares its size and checksum with the game's own table `FILESYS.CHK`. It prints how many listed files match, how many listed files are not on the partition, how many files on the partition the table does not list (those are not checked; on partition 0 they are 17, `GAME.EXE` and `DIAG.EXE` among them, which the EXE gate checks instead), and the name of each file that does not match; it exits with an error when any file does not match. Nothing is written.

## census

```
python3 -m carnevil_tools census <GAME.EXE>
```

Decodes every word of the image as a MIPS instruction and prints how many times each one appears, along with the addresses of the first words that are not valid instructions. Because the image holds data as well as code, some counts are inflated by data that happens to look like instructions. The census restricted to real code is in [analysis/functions.md](analysis/functions.md).

## functions

```
python3 -m carnevil_tools functions <GAME.EXE> -o <output.toml> [--manual <manual.toml>]
```

Finds the functions in the game: it follows the code from the entry point through every call and branch, resolves jump tables, picks up functions that are only referenced through pointers, and reports the ranges it could not reach. The output file starts with `format = 2` and the SHA-256 of the EXE it was made from, then lists the code range, the stub blocks, each function (address, size, name, how it was found), each jump table, the functions left to the interpreter and the unreached ranges. This file is what the recompiler reads; a file in any other format (an earlier `format = 1` file included) is refused with a message saying how to regenerate it.

What the analysis cannot find by itself comes from a hand-maintained manual file. Every entry has a `reason` that cites the evidence:

- `[[manual]]` (`addr`): an extra function start.
- `[[stub_block]]` (`start`, `end` exclusive): a range the program fills at run time. Stub-block words are never decoded; each call or jump target inside one becomes an 8-byte function with the source `stub`.
- `[[jump_table]]` (`jr_addr`, `targets`, case k being `targets[k]`): the table of a `jr` the analysis cannot resolve. Its targets are followed like those of a table the analysis found, and it is recorded with `table_addr = 0`. The command stops with an error naming the entry when `jr_addr` is not a `jr` on a register other than `ra` in a function's reached code, or a target is not a word inside that function.
- `[[interpreted]]` (`addr`): a function the recompiler leaves to the interpreter. It must be a function start; it stays in the function list, so calls to it still resolve.

Without `--manual`, the command reads the file named like the output with `.functions.toml` replaced by `.manual.toml`, if it exists: `-o config/game.functions.toml` reads `config/game.manual.toml`. Any other output name gets no manual input.

The copy produced from the supported revision, with `config/game.manual.toml`, is committed as `config/game.functions.toml`; a game-data test checks that it is still what the code produces.

## recompile

```
python3 -m carnevil_tools recompile <GAME.EXE> -o <output folder> [--functions config/game.functions.toml]
```

Turns every function of the functions file into C. It refuses an EXE whose SHA-256 is not the one the functions file was made from, naming both. The output folder gets `funcs.h` (a prototype of each function), `funcs_000.c`, `funcs_001.c` and so on (64 functions each, in address order), `func_table.c` (`recomp_lookup`, which finds the recompiled function at an address: every function, loader slots and interpreted functions included) and, last, `manifest.toml` (the EXE's SHA-256 and the counts of functions, files, loader stubs, interpreted functions, jump tables and `jr`s left to `rt_jump`). A loader slot becomes a call of `rt_loader`, an interpreted function a call of `interp_run`.

Everything is written to a working folder the command creates next to the output folder under a unique hidden name, and renamed into place when it is complete, so a folder holding `manifest.toml` is always a whole output. An instruction the recompiler does not handle stops it with the address and the mnemonic, and leaves no output. An existing output folder is replaced only if it holds a `manifest.toml`; anything else at that path is left alone and the command stops. Nothing else is ever deleted: only the folders a run creates are removed, on success or failure, and a failed run leaves an existing output as it was. The output is game code: it goes to the share (`make recomp` writes `$CARNEVIL_WORK/generated`), never into this repository.

## pure-functions

```
python3 -m carnevil_tools pure-functions [<GAME.EXE>] [--functions config/game.functions.toml]
```

Prints, one address per line, the functions that are statically pure by the census: nothing in their call closure (direct calls, branches and jumps out of the function, jump-table entries, running off its end) calls a loader stub or an interpreted function, holds a `break`, a `jalr` or an unresolved `jr`, or makes a hardware access `hwcensus` can attribute. The counts go to stderr. The EXE defaults to `$CARNEVIL_WORK/part0_files/GAME.EXE`. The list is a superset of what is pure at run time: hardware reached through pointers held in memory or passed as arguments is invisible to the census, so some listed functions do touch hardware (25 of the 152 captured in MAME: the Voodoo, its texture memory, the CMOS). `tools/mame/funcreplay.lua` captures listed functions in MAME, and `test_against_mame` sorts each capture into a clean replay (127 of 152: those functions match MAME) or impure at run time (25: these show only that the recompiled C and the interpreter agree up to the first hardware access, not a match with MAME) (`tools/mame/README.md`).

## make

```
make recomp       # recompile $CARNEVIL_WORK/part0_files/GAME.EXE into $CARNEVIL_WORK/generated
make linkcheck    # compile the generated C and link it into $BUILD/game_stub
make smoke        # ... with runtime/stub/smoke.c into $BUILD/smoke
make replay       # ... with runtime/stub/replay.c into $BUILD/replay
make test         # the unit tests
```

`smoke RAM_IN RAM_OUT [NAME=VALUE ...]` runs GAME.EXE's entry on MAME's RAM dump at the entry, with the registers given (ABI names and `fcsr`), to the first loader call, and prints the slot and the state there and writes the RAM. `replay [--interp] CAPTURE_DIR` runs one captured function and compares its result with MAME's; it stops at the first bus access, prints it and the state and exits 3; `--interp` runs every call in the interpreter instead. Both are described at the top of their source files.

`make linkcheck` compiles the generated files and the runtime (`runtime/core`, `runtime/interp` and the stub runtime `runtime/stub/stub.c`) in parallel, one job per core, and links them into `game_stub`. `BUILD` defaults to `$CARNEVIL_WORK/build/<uname -s>-<uname -m>`; `GENERATED`, `BUILD`, `CARNEVIL_WORK`, `JOBS` and `CC` can be set on the command line. It stops with an error when the generated folder has no `manifest.toml`, and compiles or links nothing that is up to date. The whole image builds in about 4 s on the Mac (Apple clang, 10 cores) and about 25 s with GCC 14 in a 4-core container.

`game_stub` with no argument does nothing: the link is the check. `game_stub <entry> [<EXE>]` loads the EXE into RAM, calls the function at the entry address with every register 0 but sp (just below the top of RAM) and prints the general registers. Its runtime is the smallest one: the bus reads 0 and logs each address the first time it sees it; the loader, `break` and interrupt entry points log their arguments and return; and each address the interpreter is entered at is logged once.

## disasm

```
python3 -m carnevil_tools disasm <file> --base <addr> --start <addr> --count <n>
```

Decodes `n` words of any file, starting at address `start`, and prints them to the terminal; nothing is written. `--base` is the address of the file's first byte: `0xBFC00000` for `boot.u32`, `0x800C3FF8` for `GAME.EXE` and `0x800FFFF8` for `DIAG.EXE` (their 8-byte header comes first). The analysis notes name addresses only; this is how to look at the code behind them on your own files.

## hwcensus

```
python3 -m carnevil_tools hwcensus <GAME.EXE> [--ioctl]
python3 -m carnevil_tools hwcensus --loader <boot.u32> [--reach]
```

Lists the hardware registers the code touches, grouped by region (Voodoo, frame buffer, texture memory, IOASIC, CMOS, gun, system controller and so on), with read and write counts and the functions that make the accesses. It follows register values through each function and across calls to work out the addresses.

- With an EXE it analyses `GAME.EXE`, using the function list in `config/game.functions.toml`. `--ioctl` also lists the game's ioctl calls into the boot ROM's loader: the device and command number of each call.
- With `--loader` it analyses the boot ROM's resident loader instead, and labels each access with what reaches it: the services the game calls, the exception vectors, the loader's own start-up or one of its device drivers. `--reach` also prints, for each of the 72 service slots, how many loader functions it reaches by direct calls and tail jumps, how many of those make an indirect call (so may reach more), and the hardware regions those functions access.

The static census cannot see every access; [analysis/hardware-usage.md](analysis/hardware-usage.md) says where it falls short and gives the measurements taken in MAME.

## The modules behind them

- `disk.py`: partition table, block chains and file system, and the `FILESYS.CHK` check.
- `cksum.py`: the POSIX `cksum` CRC that the EXE header and `FILESYS.CHK` use (also importable as `exe.posix_cksum`).
- `exe.py`: the EXE header, its checksum, loading the image, and the revision check.
- `mips.py`: the MIPS IV instruction decoder, the instruction semantics the analyses share (`dest_reg`, `reads`, `always_taken`, `ends_path`, `is_prologue`) and the opcode census (`tally`, `census`).
- `functions.py` and `naming.py`: function discovery, the TOML it writes and reads, the manual file (`read_manual`), `reached_words` (the words of each function that discovery reached), and naming functions from the game's own debug messages.
- `emit.py` and `recomp.py`: one instruction to C statements, one function to a C function (`function_c`), and the whole image to files (`recompile`).
- `board.py`: the board's physical memory map (`REGIONS`, `classify`, `region_of`) and the register names.
- `hwcensus.py`: the register-value propagation behind `hwcensus` (`propagate`), the hardware accesses it attributes and the ioctl list. `hwcensus.REGIONS` and `hwcensus.classify` are the ones in `board.py`.
- `loader.py`: the boot ROM's resident loader: its image, service slots, device-driver table, call graph and what each slot reaches, and `game_slots`, the slots `GAME.EXE` calls according to `config/boot_state.toml`.
- `cli.py`: the command line.
