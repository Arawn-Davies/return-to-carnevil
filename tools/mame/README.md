# MAME scripts

Lua scripts that drove MAME's debugger and memory taps for the dynamic checks in
`docs/analysis/boot-handoff.md` and `docs/analysis/hardware-usage.md`. They contain logic and
addresses only. What they write (debugger logs, RAM dumps, tap counts) comes from the game and
stays out of git.

All runs used MAME 0.276 (Debian package) with the `carnevil` set and the `carnevil` CHD,
default DIP switches (except `diagpath.lua`), a blank NVRAM and no input.

## Output location

The scripts that write files write them to `$CARNEVIL_MAME_OUT`, or to the current directory
when it is unset. The debugger's `save` command takes the path unquoted, so use a folder whose
path has no spaces or commas. The breakpoint scripts log through the debugger, which writes
`debug.log` in the current directory when MAME runs with `-debuglog`.

These paths replace the share paths the scripts were run with; the scripts have not been run
again since (`os.getenv` is standard Lua, which MAME's scripting provides).

## Common options

The tap runs' options are recorded in `hardware-usage.md`. For the breakpoint runs only the
mode is recorded (headless, debugger on, `-autoboot_script`, since `-debugscript` with
`-debugger none` does not run a script in this build); the lines below for them are
reconstructed from that and from their logs. `<roms>` is the folder holding `carnevil` and
its CHD.

## The scripts

**`handoff.lua`** (run 1): the boot hand-off. Breakpoints at the loader's entry, its EXE
checksum compare and its jump to the EXE log the registers; at the first instruction of
`DIAG.EXE` or `GAME.EXE` it saves the 8 MB of RAM (`ram-at-game-entry.bin`, the dump some tests
read) and quits at `GAME.EXE`'s entry. About 18 s wall time (`debug.log` timestamps).

```
mame carnevil -rompath <roms> -debug -debugger none -debuglog -video none -sound none \
  -nothrottle -autoboot_script tools/mame/handoff.lua
```

**`diagpath.lua`** (run 2): the same with the Test Switch on, so the boot goes through
`DIAG.EXE`; the switch is released after 60 emulated seconds. It saves RAM at `DIAG.EXE`'s
entry and at `GAME.EXE`'s entry reached that way, then quits. About 20 s wall time (`debug.log`
timestamps).

```
mame carnevil -rompath <roms> -debug -debugger none -debuglog -video none -sound none \
  -nothrottle -autoboot_script tools/mame/diagpath.lua
```

**`gamerun.lua`** (run 3): lets `GAME.EXE` run and logs every handler it registers with the
loader (interrupt lines, GT64010 cause bits, FPU and break hooks, exception codes), the first
hit of each of the 10 probable callbacks and 16 unresolved jumps of `functions.md`, and the
first hit of each registered handler. Up to 150 emulated seconds (149 reached), about 12
minutes wall time (`debug.log` timestamps).

```
mame carnevil -rompath <roms> -debug -debugger none -debuglog -video none -sound none \
  -nothrottle -seconds_to_run 150 -autoboot_script tools/mame/gamerun.lua
```

**`voodootap.lua`**: read and write taps on physical 0x08000000 to 0x08FFFFFF (Voodoo
registers, frame buffer, texture memory), counted by offset, read or write, and whether the PC
is in `GAME.EXE`, with up to 12 PCs per offset; writes `voodoo-tap.txt`. 90 emulated seconds
at 160% speed, about 1 minute wall time.

```
timeout 2400 mame carnevil -rompath <roms> -video none -sound none -nothrottle \
  -seconds_to_run 90 -autoboot_script tools/mame/voodootap.lua
```

**`hwtap.lua`**: read and write taps on physical 0x0A000000 to 0x0C000FFF (PCI I/O, so IDE,
and the GT64010) and 0x10000000 to 0x17FFFFFF (the four chip selects: sound FIFO, CS2,
IOASIC, CMOS, gun, CS3 control registers), counted by address, read or write, and by who made
the access (game, stub block, loader, other), with up to 16 PCs per address for the game and
the stub block; writes `hw-tap.txt`. Under `-nodrc`, CURPC is the next PC to run: the
instruction after the access, or the branch target for an access in a delay slot. 90
emulated seconds at 28% speed, about 5.5 minutes wall time.

```
timeout 2400 mame carnevil -rompath <roms> -nodrc -video none -sound none -nothrottle \
  -seconds_to_run 90 -autoboot_script tools/mame/hwtap.lua
```

**`hwtap-split.lua`**: `hwtap.lua` plus a tap on the first word of the stub block; the first
non-zero write there (`_start` copying the slot table) marks the hand-off, and loader and
other accesses after it are counted apart. Writes `hw-tap-split.txt`. 90 emulated seconds at
28% speed, about 5.5 minutes wall time.

```
timeout 2400 mame carnevil -rompath <roms> -nodrc -video none -sound none -nothrottle \
  -seconds_to_run 90 -autoboot_script tools/mame/hwtap-split.lua
```

**`insnvec.lua`**: runs the instruction test cases of `carnevil_tools/vectors.py` on MAME's
R5000 core and writes `insnvec.jsonl` (one line per case: the registers and the non-zero
words of the scratch page after the case). Unlike the other scripts, its output is synthetic
(instruction words and register values chosen by `vectors.py`) and is merged into the committed
`tests/vectors/insn.json`. It boots to `GAME.EXE`'s entry, clears Status IE, EXL and ERL, and
from then on overwrites the pages 0x80100000 (code) and 0x80200000 (scratch) for each case; it
reads back nothing but the registers and the scratch page. It needs `-nodrc`, because it sets
the PC from inside the debugger's instruction hook. Input is `$CARNEVIL_VECTORS`. The run of
1,320 cases took 36 s wall time (`date` around the run), nearly all of it the boot: a run of
16 cases took 33 s.

```
python3 -m carnevil_tools vectors --write-cases <share>/mame/scripts/insnvec-cases.json
CARNEVIL_VECTORS=<share>/mame/scripts/insnvec-cases.json CARNEVIL_MAME_OUT=. \
  timeout 2400 mame carnevil -rompath <roms> -nodrc -debug -debugger none -debuglog \
  -video none -sound none -nothrottle -cfg_directory cfg -nvram_directory nvram \
  -snapshot_directory snap -diff_directory diff -autoboot_script tools/mame/insnvec.lua
python3 -m carnevil_tools vectors --merge <share>/mame/scripts/insnvec-cases.json \
  insnvec.jsonl -o tests/vectors/insn.json
```

**`firstcall.lua`**: GAME.EXE from its entry to its first loader call, for the smoke run of
`tests/test_against_mame.py` (`runtime/stub/smoke.c`). It boots to the entry, records the
registers there, sets a breakpoint on each of the stub block's 72 slots and stops at the first
one reached. There it writes `first-loader-call.txt` (the registers at the entry and at the
call, the slot, the number of exceptions taken) and `ram-at-first-loader-call.bin` (8 MB) to
`$CARNEVIL_MAME_OUT`, and quits. `config/boot_state.toml` [first_loader_call] and the loader's
leftover registers at the entry come from the text file; the test reads the RAM from
`mame/dumps/ram-at-first-loader-call.bin`. Breakpoints on the exception vectors log every
exception on the way; a write watchpoint on 0x800C4000-0x807FFFFF that fires only for code
below GAME.EXE logs whether an exception handler wrote there, and a control watchpoint with
the opposite condition on the first byte `_start` clears shows the mechanism works (it must
log one write). Watchpoints take physical addresses, and `pc` in a condition is the
sign-extended 64-bit value, so the conditions mask it. Two runs gave the same RAM byte for
byte. About 35 s wall time (`date` around the run), nearly all of it the boot.

```
CARNEVIL_MAME_OUT=<share>/mame/firstcall timeout 1200 mame carnevil -rompath <roms> -nodrc \
  -debug -debugger none -debuglog -video none -sound none -nothrottle -cfg_directory cfg \
  -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script tools/mame/firstcall.lua
cp <share>/mame/firstcall/ram-at-first-loader-call.bin <share>/mame/dumps/
grep -c "FIRSTCALL loader write" debug.log; grep -c "FIRSTCALL control write" debug.log
```

**`funcreplay.lua`**: captures of pure functions (`python3 -m carnevil_tools pure-functions`)
for `runtime/stub/replay.c`. From GAME.EXE's entry it sets a breakpoint on every listed
function (except the few whose first word follows a branch or jump, so may be a delay slot,
where setting the PC is not safe) and, for the first hit of each, saves the registers and RAM,
runs to `ra` with `sp` back at its entry value, and saves them again. An attempt during which
an exception vector is reached, or that has not returned within 1 emulated second, is dropped
and the function is tried again at a later hit, up to 3 attempts. MAME keeps FCSR's condition
codes outside its state entries (see below), so at both points the script runs one injected
`cfc1 k0,$31` at 0x807FFFF8 with Status IE cleared, then puts k0, Status, the two words and the
PC back before it records anything. Inputs and limits are environment variables, described at
the top of the script: `CARNEVIL_PURE` (the list), `CARNEVIL_MAME_OUT` (the capture folder),
`CARNEVIL_TMP` (a local folder for the RAM saves), `CARNEVIL_REPLAY_SECONDS` (90 emulated
seconds after the entry), `CARNEVIL_REPLAY_MAX` (200 captures; past that it only counts first
hits) and `CARNEVIL_REPLAY_TRIES` (3).

```
python3 -m carnevil_tools pure-functions > <share>/mame/scripts/pure-functions.txt
CARNEVIL_PURE=<share>/mame/scripts/pure-functions.txt CARNEVIL_MAME_OUT=<share>/mame/replay \
  CARNEVIL_TMP=/tmp/funcreplay timeout 7200 mame carnevil -rompath <roms> -nodrc -debug \
  -debugger none -debuglog -video none -sound none -nothrottle -cfg_directory cfg \
  -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script tools/mame/funcreplay.lua
```

The capture folder (`$CARNEVIL_WORK/mame/replay` for the tests; game RAM, so on the share
only) holds:

- `base-NNN.bin`: 8 MB RAM images. A capture's RAM before the call is stored as a diff
  against the current base; when that diff would exceed 1 MB, the RAM becomes a new base.
- `<ADDR>/capture.txt`, for the function at ADDR (8 upper-case hexadecimal digits): text
  lines `addr ADDR`, `base ../base-NNN.bin` (relative to the capture's folder), `attempt N`,
  `time T` (emulated seconds after GAME.EXE's entry), and for `before` and then `after` the
  five lines `<when> r R0 .. R31`, `<when> hi H`, `<when> lo L`, `<when> fpr F0 .. F31` (the 32
  FP halves) and `<when> fcsr X` (FCSR as `cfc1` reads it, condition codes included), every
  value 8 hexadecimal digits. Lines starting with `#` are comments.
- `<ADDR>/before.diff`: the RAM at the call against the base; `<ADDR>/after.diff`: the RAM at
  the return against the RAM at the call. A diff is a sequence of records, each a
  little-endian u32 byte offset into the 8 MB, a little-endian u32 length, then that many
  bytes, which replace RAM there. Offsets ascend and records do not overlap; the script
  compares 256-byte blocks and writes each run of differing blocks as one record, so a record
  can hold unchanged bytes too.
- `funcreplay-log.txt`: one line per capture and per dropped attempt, the functions hit but
  not captured, and a summary (candidates, hit, captured, dropped). Addresses and counts only.

`pure-functions` is pure by the census only, so some captured functions touch hardware
through pointers it cannot see. `replay` stops at its first bus access (exit 3, printing the
access and the state there); `test_against_mame` counts the capture impure at run time when
that address is a hardware region of `carnevil_tools.board` and `replay --interp` (every call
in the interpreter) makes the same first access, with the same written value, from the same
registers and RAM (replay prints an FNV-1a 64-bit hash of the 8 MB), and a failure otherwise.
In the run of 2026-11-19, 127 of the 152 captures replay clean: those 127 functions match
MAME. The other 25 are impure at run time; they show only that the recompiled C and the
interpreter agree up to the first hardware access, not that they match MAME.

How MAME exposes the state the script uses (MAME 0.276, `src/devices/cpu/mips/mips3.cpp`):
the general registers are the 64-bit state entries `zero`, `at`, ... `ra` (ABI names; `R<n>`
does not exist in this build), plus `HI`, `LO`, `PC`, `SR`, `Cause`, `EPC`. The FP registers
are the 64-bit entries `FPR0` to `FPR31`; under FR = 0, half n (even) is the low 32 bits of
`FPR<n>` and half n + 1 its high 32 bits, and `FPR<odd>` is unused. The case
`mtc1.edge.fr0-halves` confirms it: `mtc1` into `$f3`, then `sdc1 $f2` stores half 2 at the
address and the `mtc1` value at the address plus 4, `ldc1 $f4` fills halves 4 and 5 the same
way, and `mfc1` of `$f5` reads the `mtc1` value back. FCSR cannot be read or written through
the state: entry `CCR31` holds FCSR without its condition bits, which MAME keeps apart, so the
script sets FCSR with a `ctc1` and reads it with a `cfc1` (both through `k0`, which is
reloaded on the way into the case). The script reads registers through each entry's text
(`%016X`), because the value of a 64-bit entry with bit 63 set reaches Lua as a float, and it
copies `cpu.state` into a table first, because `cpu.state["at"]` is the container's `at`
method, not the register.

Not run in MAME (`vectors.NO_MAME`): `break` (it raises an exception, so a case cannot reach
`ra`), and `div` of 0x80000000 by 0xFFFFFFFF (MAME divides on the host, where x86 `idiv`
faults: the first run died of SIGFPE).

Results that are MAME's and not established hardware facts (`vectors.MAME_SPECIFIC`; what the
R5000 does in each is not verified here against its documentation):

- `div` and `divu` by zero leave `hi` and `lo` unchanged (MIPS leaves the result undefined).
- `trunc.w.s` and `trunc.w.d` of a NaN or of a value outside int32 give 0x80000000, the x86
  conversion result.
- `madd`, `msub`, `nmadd`, `nmsub` are unfused: MAME computes them in C as
  `fs * ft + fr` (`- fr` for msub), so the product is rounded to the format and then the sum,
  and nmadd/nmsub negate the rounded sum (`-(fs * ft + fr)`, `-(fs * ft - fr)`). All 60
  recorded cases equal that two-rounding result (exact arithmetic, `test_vectors`); in 3 of
  them (`nmadd.s.5`, `msub.s.9`, `nmsub.s.11`) a single rounding gives other bits, and those
  are the ones listed. Whether the R5000 rounds once or twice is not verified.

`recip.s` (`1.0f / x`) and `sqrt.s` (`sqrt` in double, then to single) are correctly rounded
in all 12 recorded cases each, so none of them is listed.

Also MAME-only, but invisible in the vectors: `cache` is a
no-op (MAME models no caches); MAME raises no FP exception except division by zero with its
enable set, and no `addi` overflow trap, so the cases keep FCSR's enables clear and `addi` from
overflowing.

The tap runs each kept MAME's state in a folder of their own (on the share, each run folder
holds `cfg`, `nvram`, `snap` and `diff`), which gives the blank NVRAM: add
`-cfg_directory`, `-nvram_directory`, `-snapshot_directory` and `-diff_directory` pointing
into an empty folder.

The taps are removed and reinstalled every 30 frames, because the GT64010 remaps the CPU
address space when its windows are written, and an access between a remap and the next
reinstall is lost (`hardware-usage.md`).
