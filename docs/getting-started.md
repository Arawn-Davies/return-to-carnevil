# Getting started

Right now you can extract the game's files from your own disk image, run the analysis tools, translate the game's code to C, and build and run the game headless: it boots and runs its attract mode with no picture, no sound and no input, printing its console output to your terminal. There is no playable build yet: pictures are the next milestone. See the [roadmap](roadmap.md).

## What you need

- Python 3.12 or newer. The tools use the standard library only: nothing to install.
- For the recompiled code (steps 5 to 7): a C compiler as `cc` (Apple clang on macOS and GCC on Linux are both tested) and GNU `make`. The tests that compile C skip, naming `cc`, when there is none.
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

`verify` reads every file of partition 0 in memory and checks it against the game's own table `FILESYS.CHK`. On a good image it prints `1504 of 1504 listed files match`, `0 listed files are not on the partition` and `19 files on the partition are not listed (not checked)`, and no mismatch. `GAME.EXE` and `DIAG.EXE` are among the 19: they are checked instead by the EXE gate (SHA-256 and header checksum, below) when `census` or `functions` first loads them.

The disk has 17 partitions. Partition 0 holds the 1,523 files the game runs from, including `GAME.EXE` (the game), `DIAG.EXE` (the operator diagnostics), textures (`.WMS`), models (`.ZM`), animations (`.ZA`) and sound banks (`.BNK`).

**If you extracted your files before 20 November 2026**, your folder lacks 167 of them: the earlier `extract` stopped reading the directory at a deleted entry ([analysis/disk-format.md](analysis/disk-format.md)). The partition files from `split` are not affected, only the extracted folder. To bring the folder up to date, run the `extract` line above again into the same folder with the current tools. It writes every file again, so the ones you have are rewritten with the same bytes and the 167 are added; `GAME.EXE` and `DIAG.EXE` do not change, so a translation made from the old folder stays valid. Nothing else needs redoing.

## 3. Tell the tools where the folder is

```
export CARNEVIL_WORK=/path/to/work
```

The built-in default is the author's own path, so set this; the tools, the tests and the `Makefile` all read it. Use a path without spaces: `make` cannot handle one, and stops with a one-line error if `CARNEVIL_WORK`, `GENERATED` or `BUILD` contains a space. The boot ROM is not on the disk; it is `boot.u32` in MAME's `carnevil` set. The tests that read it run only when you name it:

```
export CARNEVIL_BOOT_ROM=/path/to/boot.u32
```

`hwcensus --loader` takes the path on its command line.

## 4. Run the analysis tools

```
python3 -m carnevil_tools census work/part0_files/GAME.EXE
python3 -m carnevil_tools functions work/part0_files/GAME.EXE -o my.functions.toml
```

The first counts the MIPS instructions in the game. The second finds its functions and jump tables. With an output name like `my.functions.toml` it reads no manual input; `--manual config/game.manual.toml` (or `-o config/game.functions.toml`, which picks that file up by name) reproduces the committed `config/game.functions.toml` exactly. See the [tools reference](tools.md).

## 5. Translate the game to C

From the repository's top folder:

```
make recomp
```

This reads `$CARNEVIL_WORK/part0_files/GAME.EXE` and the committed `config/game.functions.toml`, and writes the C for all 2,093 functions to `$CARNEVIL_WORK/generated`: 33 files of functions, a header, a lookup table and `manifest.toml`, about 8 MB in all, in about a second. It refuses a `GAME.EXE` that is not the supported revision. The generated C is a translation of the game's code, so it stays in your work folder and is never committed. `python3 -m carnevil_tools recompile <GAME.EXE> -o <folder>` does the same into any folder ([tools reference](tools.md)).

## 6. Compile and link it

```
make linkcheck
```

This compiles the generated C and the runtime's core in parallel and links them with a stub runtime into `$CARNEVIL_WORK/build/<system>-<machine>/game_stub`. It succeeds only if every function links with no undefined symbol. It takes a few seconds on a recent Mac and under half a minute on a 4-core Linux machine. `game_stub` does nothing useful: the link is the check. `BUILD=…` puts the objects elsewhere.

## 7. Build and run the game, headless

```
make carnevil
```

This builds the program `carnevil` from the generated C (step 5 first) and the whole runtime into `$CARNEVIL_WORK/build/<system>-<machine>/carnevil`. It writes the start-up state header `boot_state.h` into the generated folder first. Then run it on your raw image (the file from step 1):

```
mkdir -p work/run
$CARNEVIL_WORK/build/$(uname -s)-$(uname -m)/carnevil --disk work/carnevil.raw \
    --overlay work/run/carnevil.overlay --nvram work/run/carnevil.nvram --seconds 20
```

The options:

- `--disk FILE` (required): your raw image. It is only ever opened for reading; the program checks its size and that its `GAME.EXE` is the supported revision before it creates any other file.
- `--overlay FILE`: where the game's writes to its disk go (audits, adjustments, high scores). Created on the first run, reused afterwards, and refused if it was made for another image.
- `--nvram FILE`: the game's settings memory (CMOS), 32 KB. Created blank on the first run. It is saved when the run ends, whether it reached its limit, stopped, or was interrupted (below); a run killed with `SIGKILL` (or by a crash of the machine) cannot save it.
- `--seconds N` or `--frames N`: stop after N seconds of game time (at most 18,000,000, about 208 days, the most the Voodoo model's picosecond clock can hold), or at the N-th frame (VBLANK interrupt). Without either, the game runs until something stops it.
- `--trace FILE`: write a log of what the game did, one line per event (see [analysis/runtime.md](analysis/runtime.md), "Reading a trace").
- `--quiet`: keep the game's console output out of the terminal.

Without `--overlay` and `--nvram`, the two files are created next to the raw image as `carnevil.overlay` and `carnevil.nvram`. The image itself is never written either way, but if you keep it on a shared or read-only drive, give both options. `make run DISK=work/carnevil.raw SECONDS=20 OVERLAY=… NVRAM=… TRACE=…` builds and runs in one step; set `OVERLAY` and `NVRAM` there too, for the same reason (the folder they go in must exist). Without `DISK`, `make run` uses `$CARNEVIL_WORK/carnevil.raw`, and without `SECONDS`, 150.

What a run prints: the game's own console lines, from the `Mother GOOSE - R5000` banner through `Setting 400x256 54Hz screen resolution`, `Starting process dispatcher`, the CMOS and high-score messages a blank NVRAM gives, to two blocks of `main_init_sound` lines, all within the first few seconds of game time. After that the game sits in its attract mode, printing nothing, until the time runs out; then one line on stderr, `carnevil: ran N ns of guest time; RAM SHA-256 …`, and exit status 0. On a Mac, 20 seconds of game time take a few seconds. A second run on the same NVRAM no longer prints `reseting high scores`, because the settings are no longer blank.

Game time is not real time: there is no window yet, so the program runs as fast as it can, and its clock is not the arcade board's pace either ([analysis/runtime.md](analysis/runtime.md), "The clock").

Exit status 2 means a problem with your files, named in one line on stderr. Exit status 3, with a line starting `carnevil: stop:`, means the game asked for something the port does not model yet, such as a hardware access or a loader service attract mode never uses; the message names it. The port never guesses an answer, so a stop is expected whenever the game goes somewhere it was not checked: anything outside attract mode with a blank NVRAM and no input. In attract mode, 150 seconds run without a stop.

Ending a run early: Ctrl-C (`SIGINT`), `kill` (`SIGTERM`) or closing the terminal (`SIGHUP`) ends the run cleanly at the next point where the game is between instructions, which comes within a fraction of a second: the NVRAM is saved, the overlay's writes are pushed to the disk, and the trace gets an `end interrupted SIGINT` line (or `SIGTERM`, `SIGHUP`) and its totals before it is closed. The program then prints `carnevil: interrupted by SIGINT at N ns of guest time` (not with `--quiet`) and exits with status 130, which is distinct from 0 (the limit was reached), 2 (a file or usage error) and 3 (a stop). The next run on the same files starts normally.

`SIGKILL` (`kill -9`) cannot be caught, so a run killed that way, or one that dies with the machine, saves nothing at its end. What it leaves: the overlay holds every sector write the game finished before the kill, and the next run reads it as it is; the NVRAM file is the one the previous run saved (or the blank one this run created), so every CMOS write of the killed run is lost; the trace ends wherever its last buffered megabyte was written out, without its totals. When a first run was killed this way in the milestone's review, the next run on its files behaved as a first boot again (it reset the high scores once more) and ran normally; other combinations have not been tried.

## 8. Run the tests

```
python3 -m unittest discover -s tests -v
```

The unit tests build their own small fixtures and need no game files. The tests that check your game files skip when the files are not there, so the suite passes on a clean checkout. `python3 -m unittest` and `python3 -m unittest tests.test_disk` (one module) work as well, and `make test` runs the suite with warnings turned into errors. With `cc` present, the suite compiles C into a temporary folder; nothing is written to the repository.

With your files in place, some tests still skip, and their skips are expected:

- `test_contiguous_extraction_reported_as_mis_extracted` reads `part0_files_contiguous_wrong/GAME.EXE`, a copy extracted the wrong way (contiguously) on purpose, which only the developer keeps.
- The boot tests (`tests/test_boot.py`) need your raw image at `$CARNEVIL_WORK/carnevil.raw` and the M2 captures under `$CARNEVIL_WORK/mame/m2-facts/`; they build `carnevil`, run it for 150 seconds of game time into a temporary folder (overlay, NVRAM and trace included) and compare its console output, file opens, tasks and first loader calls with MAME's.
- The tests that compare with MAME read captures made in MAME, under `$CARNEVIL_WORK/mame/`: `test_game_exe_matches_loader` and the smoke test read `mame/dumps/ram-at-game-entry.bin` (`tools/mame/handoff.lua`), the first-loader-call test reads `mame/dumps/ram-at-first-loader-call.bin` (`tools/mame/firstcall.lua`), and the replay test reads the function captures in `mame/replay/` (`tools/mame/funcreplay.lua`). Each skip message names the missing file. To make the captures, run those scripts in MAME 0.276 with the `carnevil` set and your CHD; [tools/mame/README.md](../tools/mame/README.md) has the command lines and where each output goes. The captures hold game RAM, so they stay in your work folder.

The boot-ROM tests skip unless `CARNEVIL_BOOT_ROM` is set (step 3).

## Supported revision

One revision of the game is supported for now, identified by the SHA-256 of `GAME.EXE`:

```
f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6
```

Loading a different `GAME.EXE` stops with one of two messages: an intact file of another revision gets "unknown EXE revision" with the expected hash and yours; a damaged or mis-extracted file (its header checksum fails, for example after extracting with a tool that reads files as contiguous blocks) gets a checksum error that names the expected hash and yours. Other revisions can be added later; each needs its own analysis output.

## Keeping game files out of git

The `.gitignore` excludes disk images, EXEs, ROMs and the usual work folders. If you contribute, please keep it that way: no game files, no extracted assets, no disassembly listings, no generated C and no MAME captures. Addresses, sizes, counts and format descriptions are fine.
