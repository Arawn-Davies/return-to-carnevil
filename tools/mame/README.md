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

## Where to run MAME

Run MAME from a scratch folder outside the repository, `<scratch>` below (for example `mkdir -p
/tmp/carnevil-mame` and `cd` into it), and give each script by its absolute path,
`<repo>/tools/mame/<script>.lua`: the absolute path of this folder on the machine that runs
MAME (copy the scripts there first if the repository is not on that machine). MAME writes into
the current folder: `debug.log` under `-debuglog`, the `cfg`, `nvram`, `snap` and `diff`
folders the commands name (`diff/carnevil.dif` is the CHD's write delta, disk-derived data),
and the scripts' own outputs when `$CARNEVIL_MAME_OUT` is unset. None of that belongs in git;
`.gitignore` also ignores those names, in case MAME is run from the repository anyway. The
`python3 -m carnevil_tools` commands run from `<repo>`. `<share>` is the work folder
(`$CARNEVIL_WORK`) as the machine running the command sees it.

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
cd <scratch> && mame carnevil -rompath <roms> -debug -debugger none -debuglog -video none -sound none \
  -nothrottle -autoboot_script <repo>/tools/mame/handoff.lua
```

**`diagpath.lua`** (run 2): the same with the Test Switch on, so the boot goes through
`DIAG.EXE`; the switch is released after 60 emulated seconds. It saves RAM at `DIAG.EXE`'s
entry and at `GAME.EXE`'s entry reached that way, then quits. About 20 s wall time (`debug.log`
timestamps).

```
cd <scratch> && mame carnevil -rompath <roms> -debug -debugger none -debuglog -video none -sound none \
  -nothrottle -autoboot_script <repo>/tools/mame/diagpath.lua
```

**`gamerun.lua`** (run 3): lets `GAME.EXE` run and logs every handler it registers with the
loader (interrupt lines, GT64010 cause bits, FPU and break hooks, exception codes), the first
hit of each of the 10 probable callbacks and 16 unresolved jumps of `functions.md`, and the
first hit of each registered handler. Up to 150 emulated seconds (149 reached), about 12
minutes wall time (`debug.log` timestamps).

```
cd <scratch> && mame carnevil -rompath <roms> -debug -debugger none -debuglog -video none -sound none \
  -nothrottle -seconds_to_run 150 -autoboot_script <repo>/tools/mame/gamerun.lua
```

**`voodootap.lua`**: read and write taps on physical 0x08000000 to 0x08FFFFFF (Voodoo
registers, frame buffer, texture memory), counted by offset, read or write, and whether the PC
is in `GAME.EXE`, with up to 12 PCs per offset; writes `voodoo-tap.txt`. 90 emulated seconds
at 160% speed, about 1 minute wall time.

```
cd <scratch> && timeout 2400 mame carnevil -rompath <roms> -video none -sound none -nothrottle \
  -seconds_to_run 90 -autoboot_script <repo>/tools/mame/voodootap.lua
```

**`hwtap.lua`**: read and write taps on physical 0x0A000000 to 0x0C000FFF (PCI I/O, so IDE,
and the GT64010) and 0x10000000 to 0x17FFFFFF (the four chip selects: sound FIFO, CS2,
IOASIC, CMOS, gun, CS3 control registers), counted by address, read or write, and by who made
the access (game, stub block, loader, other), with up to 16 PCs per address for the game and
the stub block; writes `hw-tap.txt`. Under `-nodrc`, CURPC is the next PC to run: the
instruction after the access, or the branch target for an access in a delay slot. 90
emulated seconds at 28% speed, about 5.5 minutes wall time.

```
cd <scratch> && timeout 2400 mame carnevil -rompath <roms> -nodrc -video none -sound none -nothrottle \
  -seconds_to_run 90 -autoboot_script <repo>/tools/mame/hwtap.lua
```

**`hwtap-split.lua`**: `hwtap.lua` plus a tap on the first word of the stub block; the first
non-zero write there (`_start` copying the slot table) marks the hand-off, and loader and
other accesses after it are counted apart. Writes `hw-tap-split.txt`. 90 emulated seconds at
28% speed, about 5.5 minutes wall time.

```
cd <scratch> && timeout 2400 mame carnevil -rompath <roms> -nodrc -video none -sound none -nothrottle \
  -seconds_to_run 90 -autoboot_script <repo>/tools/mame/hwtap-split.lua
```

**`insnvec.lua`**: runs the instruction test cases of `carnevil_tools/vectors.py` on MAME's
R5000 core and writes `insnvec.jsonl` (one line per case: the registers and the non-zero
words of the scratch page after the case). Unlike the other scripts, its output is synthetic
(instruction words and register values chosen by `vectors.py`) and is merged into the committed
`tests/vectors/insn.json`. It boots to `GAME.EXE`'s entry, clears Status IE, EXL and ERL, and
from then on overwrites the pages 0x80100000 (code) and 0x80200000 (scratch) for each case; it
reads back nothing but the registers and the scratch page. It needs `-nodrc`, because it sets
the PC from inside the debugger's instruction hook. Input is `$CARNEVIL_VECTORS`; FCSR,
FS included, is set from each case's before-state through `ctc1`. The run of 1,352 cases took
35 s wall time (`date` around the run), nearly all of it the boot: a run of 16 cases took 33
s. Every case is re-run when the cases change; `merge` writes the whole file again, and cases
appended at the end leave the earlier entries unchanged (the 1,320 entries recorded before the
FS cases were identical after the re-run that added them).

```
cd <repo> && python3 -m carnevil_tools vectors \
  --write-cases <share>/mame/scripts/insnvec-cases.json
cd <scratch> && CARNEVIL_VECTORS=<share>/mame/scripts/insnvec-cases.json \
  CARNEVIL_MAME_OUT=<scratch> timeout 2400 mame carnevil -rompath <roms> -nodrc -debug \
  -debugger none -debuglog -video none -sound none -nothrottle -cfg_directory cfg \
  -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script <repo>/tools/mame/insnvec.lua
cd <repo> && python3 -m carnevil_tools vectors \
  --merge <share>/mame/scripts/insnvec-cases.json <scratch>/insnvec.jsonl \
  -o tests/vectors/insn.json
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
cd <scratch> && CARNEVIL_MAME_OUT=<share>/mame/firstcall timeout 1200 mame carnevil \
  -rompath <roms> -nodrc -debug -debugger none -debuglog -video none -sound none -nothrottle \
  -cfg_directory cfg -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script <repo>/tools/mame/firstcall.lua
cp <share>/mame/firstcall/ram-at-first-loader-call.bin <share>/mame/dumps/
grep -c "FIRSTCALL loader write" <scratch>/debug.log
grep -c "FIRSTCALL control write" <scratch>/debug.log
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
cd <repo> && python3 -m carnevil_tools pure-functions > <share>/mame/scripts/pure-functions.txt
cd <scratch> && CARNEVIL_PURE=<share>/mame/scripts/pure-functions.txt \
  CARNEVIL_MAME_OUT=<share>/mame/replay CARNEVIL_TMP=<scratch>/funcreplay timeout 7200 \
  mame carnevil -rompath <roms> -nodrc -debug -debugger none -debuglog -video none \
  -sound none -nothrottle -cfg_directory cfg -nvram_directory nvram -snapshot_directory snap \
  -diff_directory diff -autoboot_script <repo>/tools/mame/funcreplay.lua
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

- `recip.s`: MAME computes `1.0f / x`, correctly rounded in all 12 recorded cases. The
  R5000's accuracy for `recip` is not verified, so all 12 are listed.
- FCSR.FS = 1 (the 16 `*.edge.fs1.*` cases): MAME does not model FS. Each case gives exactly
  the result of its FS = 0 control (`*.edge.fs0.*`, the same program and state): denormal
  results are kept and denormal operands used as they are, where the R5000 documents flushing
  both to zero. The game runs with FS = 1 throughout. The port follows MAME by default
  (`runtime/core/fpu.c`, `RT_HONOUR_FS`; [recompiler](../../docs/analysis/recompiler.md)).

`sqrt.s` (`sqrt` in double, then to single) is correctly rounded in all 12 recorded cases, so
none of them is listed.

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

## M2 measurements

Run in the Linux container like the others, each run in a folder of its own with a blank NVRAM. The
share holds the outputs under `mame/m2-measure/`.

**`tasksw.lua`** (M2 measurement 1, `docs/analysis/task-switch.md`): the cooperative task
switch. From GAME.EXE's entry it logs, through `printf` actions on breakpoints and watchpoints,
every yield (0x80151AC0), task exit (0x80151B50), scheduler resume (0x80151D4C), scheduler pass
(before and after its wait, 0x80151C04 and 0x80151C84, and the end of its task walk), the two
`timer:` ioctl calls of the scheduler, every task creation (0x80148FA4, 0x80149200) with its
return, every task kill and record free, every exception entry and eret, and every write to
0x801A3110-0x801A311B (task list head, free list, current task) and to 0x801A2FE8 (the vblank
count). At each creation's return and at each record free it stops, and Lua copies the new
stack or compares the freed one with its copy (the deepest word written). The script's header
lists every line and field. `$CARNEVIL_TASKSW_SECONDS` sets the emulated seconds after the
entry (150); `$CARNEVIL_PLAY` = 1 adds the scripted coin, start and trigger inputs of
`PLAY_STEPS` (MAME fields `:SYSTEM` "Coin 1" and "1 Player Start", `:FAKE` "P1 Trigger") and
snapshots into `snap/`; every input and snapshot is logged. From 75 s after the entry it
presses start every 3 s and inserts two coins every 10 s, to take the "Continue?" offers.
Watchpoints need `-nodrc`. Run A (attract, 150 s) took 1,283 s of wall time (12% speed, beside
two other runs on the Linux container's 4 cores; `wall.txt` in each run folder) and wrote 8.2 MB of
`debug.log`; run B (play, 230 s) took 2,150 s (11%) and wrote 14.4 MB. Run B1, an earlier play
run of 110 s whose schedule stopped at the triggers (no continues: one life, about 15 s of
play), took 1,043 s; it is kept and reported too.

```
cd <scratch>/tasksw-A && timeout 7200 mame carnevil -rompath <roms> -nodrc -debug \
  -debugger none -debuglog -video none -sound none -nothrottle -cfg_directory cfg \
  -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script <repo>/tools/mame/tasksw.lua
cd <scratch>/tasksw-B && CARNEVIL_PLAY=1 CARNEVIL_TASKSW_SECONDS=230 timeout 7200 mame carnevil \
  <the same options>
cd <repo> && python3 tools/mame/tasksw_report.py <scratch>/tasksw-A/debug.log
cd <repo> && python3 tools/mame/tasksw_report.py <scratch>/tasksw-B/debug.log --play-frame F
```

`tasksw_report.py` prints the numbers of `task-switch.md` (tasks alive, task functions,
yields per frame and per call site, stacks, scheduler passes, exceptions and `k1`, writes to
the task list). `--play-frame` adds per-frame figures from that frame on (run B: the frame of
the first gameplay snapshot). Outputs on the share: `mame/m2-measure/tasksw-A/`, `tasksw-B/`
and `tasksw-B1/` (`debug.log`, `report.txt`, `wall.txt`, `mame.out`, the play runs'
snapshots in `snap/`, named in order; the `TS SNAP` lines give their times).

**`loadercalls.lua`** (M2 measurement 2, `docs/analysis/loader-calls.md`): every call GAME.EXE
makes into the 72 slots of the stub block. The first `$CARNEVIL_LOADER_FIRST` calls (400) are
handled from Lua: arguments, the string at a0 when there is one, the bytes of a write to fd
0-2, and v0/v1 at the return (a one-off breakpoint at ra, conditioned on the call's sp); they
go to `loader-calls-first.txt` in `$CARNEVIL_MAME_OUT`. After them every call is a `LC` line
in `debug.log` (a `printf` action), except the opens, file-system calls and writes, which
stay in Lua and give `LS` lines at their return. A write watchpoint on physical 0x16000028
(IOASIC UARTOUT) logs every console byte from power-on. `$CARNEVIL_LOADER_SECONDS` (150) is
the run length after the entry. In `debug.log` the debugger's `printf` drops the backslash
of the `\xNN` escapes in `buf`, and Lua's lines have `?` for every `"` and `%`;
`loader-calls-first.txt` has them intact. Run A took 1,510 s of wall time (10% speed); its
`debug.log` is 670 MB, almost all of it the sound-data download between 10 and 20 emulated
seconds (5.9 million ioctl calls).

```
cd <scratch>/loader-A && CARNEVIL_MAME_OUT=. timeout 7200 mame carnevil -rompath <roms> -nodrc \
  -debug -debugger none -debuglog -video none -sound none -nothrottle -cfg_directory cfg \
  -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script <repo>/tools/mame/loadercalls.lua
cd <repo> && python3 tools/mame/loadercalls_report.py <scratch>/loader-A \
  --console <share>/mame/m2-measure/loader-A/console.txt
```

`loadercalls_report.py` prints the per-slot counts for the whole run, the ioctl (fd,
command) counts and hottest call sites, the opens with their paths and results, and the
console output as lines with the frame and emulated time of each line's first byte. Outputs
on the share: `mame/m2-measure/loader-A/` (`loader-calls-first.txt`, `report.txt`,
`console.txt`, `debug.log.gz`, 35 MB).

## M2 facts

The remaining open questions of `docs/analysis/loader-services.md` and the DCS exchange of
`docs/analysis/dcs-handshake.md`. Run in the Linux container like the M2 measurements, one MAME at a time,
each run in a folder of its own with a blank NVRAM. The share holds the outputs under
`mame/m2-facts/`. Both scripts' headers list every line they write and its fields. `c` is the
CPU's total cycles; the R5000 runs at 150 MHz, so the emulated time since power-on in
nanoseconds is c × 20 / 3 (the entry's cycle count, 750,535,450, against MAME's 5.003570 s
agrees).

**`dcstap.lua`**: the game's exchange with the DCS. Breakpoints in the loader's `ioasic:`
ioctl routine, on its one read path (0x80006CC8, just after the load) and its one write path
(0x80006CDC), log every SOUNDCTL, SOUNDOUT, SOUNDSTAT and SOUNDIN access the game makes through
ioctls 0x6906 to 0x6911, and INTSTAT/INTCTL; breakpoints on the `jr ra` of the seven DCS
routines that can return 0xEEEE log each such return with its caller; the general exception
vector logs every exception with Cause IP3 and the IOASIC interrupt registers then; the task
creation's return logs every task started; a watchpoint on UARTOUT logs the console from
power-on. `$CARNEVIL_DCS_SECONDS` (150) is the run length after the entry;
`$CARNEVIL_DCS_FORCE_CLEAR` = 1 clears bit 0x40 of every SOUNDSTAT read before the loader
stores it (a breakpoint action that rewrites v0 at 0x80006CC8), the DCS-less case of open
question 3; `$CARNEVIL_DCS_SNAP` = 1 takes a snapshot every 10 s from 20 s after the entry.

**`m2facts.lua`**: the rest. At the entry, debugger reads of the interrupt and timer
registers and a histogram of the CMOS bytes; then breakpoints and watchpoints for the input
ioctls, the gun registers and 0x17F00000, the four PCI slots (handled from Lua, with their
results), the Voodoo reads of GAME.EXE code for the first `$CARNEVIL_M2_VOODOO_SECONDS` (15)
after the entry, the `pci:` ioctls in the loader's `sysctrl` driver with the DMA descriptor
chain at each write of 0x830 (walked from Lua, up to `$CARNEVIL_M2_CHAIN_MAX` records,
100,000), the cause-bit-4 handler, the disk hooks (the wake from Lua, comparing the words it
changes), the movie player's sector call, the end of each scheduler wait, the break hooks,
the catch-all and ioctl 0x6905. `$CARNEVIL_M2_SECONDS` (150) is the run length after the
entry; `$CARNEVIL_M2_PLAY` = 1 is the play run: only the break hooks, the catch-all and
0x6905 are armed, and the inputs are those of `tasksw.lua`'s `PLAY_STEPS` (run B of
`task-switch.md`) at the same emulated times, with the same snapshots.

**`voodooramp.lua`**: every Voodoo access GAME.EXE's own code makes, written and read, from
its entry to `$CARNEVIL_RAMP_SECONDS` (2.0) emulated seconds after it. That span covers the
start-up memory test, the dither ramp and the first two DAC sequences of
`docs/analysis/hardware-usage.md`. A write and a read watchpoint on physical 0x08000000 to
0x08FFFFFF, limited to pcs inside GAME.EXE, log `VR W` and `VR R` lines in execution order.
It was run once, with facts-A2's options in a folder of its own (blank NVRAM): 37 s wall
time, 19% speed. `voodoo-ramp.txt` is cut from its `debug.log` (its header names the fields;
cycles converted as below).

```
cd <scratch>/ramp && timeout 3600 mame carnevil <the same options> \
  -autoboot_script <repo>/tools/mame/voodooramp.lua
```

In this build only the first breakpoint that hits at an address acts, so where one address
needs both a `printf` and a Lua stop (the 0x7301 write path), the two breakpoints have
complementary conditions. Debugger reads of the GT64010's TIMER_CONTROL, TIMER1 and TIMER2 at
the entry are not reproducible (two identical runs gave different values); every other
entry value was the same in both.

```
cd <scratch>/dcs-A && timeout 7200 mame carnevil -rompath <roms> -nodrc -debug \
  -debugger none -debuglog -video none -sound none -nothrottle -cfg_directory cfg \
  -nvram_directory nvram -snapshot_directory snap -diff_directory diff \
  -autoboot_script <repo>/tools/mame/dcstap.lua
cd <scratch>/facts-A2 && timeout 7200 mame carnevil <the same options> \
  -autoboot_script <repo>/tools/mame/m2facts.lua
cd <scratch>/dcs-F2 && CARNEVIL_DCS_FORCE_CLEAR=1 CARNEVIL_DCS_SNAP=1 timeout 7200 \
  mame carnevil <the same options> -autoboot_script <repo>/tools/mame/dcstap.lua
cd <scratch>/facts-P && CARNEVIL_M2_PLAY=1 CARNEVIL_M2_SECONDS=230 timeout 7200 mame carnevil \
  <the same options> -autoboot_script <repo>/tools/mame/m2facts.lua
```

Wall times (`wall.txt`, the Linux container running one MAME at a time): dcs-A 1,333 s (11.6% speed),
facts-A2 1,272 s (12.2%), dcs-F2 1,429 s (10.8%), facts-P 1,941 s (12.1%). Two earlier runs are kept
too: facts-A (1,240 s), the same as facts-A2 but with the chain walk capped at 64 records, and
dcs-F (1,469 s), the same as dcs-F2 without the task log and snapshots (their DCS, console
and interrupt lines are identical). dcs-A ran before the task-creation breakpoint was added. The play run's 22
snapshots are byte for byte those of run B. Between facts-A and facts-A2 the events and values
agree except that from the first DMA start the cycle counts differ by up to a few hundred and
some spin counts by one, so the Lua stop at each chain walk is not quite free of timing effects.

The files in `mame/m2-facts/` are text, one event per line, fields separated by single
spaces, numbers in hexadecimal with `0x` except times (decimal nanoseconds since power-on)
and counts, text fields last; the first line of each starts with `#` and names the columns.
They were cut from the runs' `debug.log` lines by field (the formats in the scripts' headers,
cycles converted as above), or from the M2 measurements where those already held the data:

- from facts-A2: `entry-hw.txt` (`M2 ENTRY`, `M2 NV`), `inputs.txt` (`M2 IN`, `M2 GUN`,
  `M2 ARST`: each distinct value with its count), `voodoo-reads.txt` (`M2 VR`, `M2 PCI`),
  `dma0.txt` (`M2 GW`, `M2 GR`, `M2 CHAIN`, `M2 RECS`, `M2 B4`), `disk-hooks.txt` (`M2 MCB`,
  `M2 WAIT`, `M2 WAKE`, `M2 WAKED`, `M2 MOV`), `spin.txt` (`M2 SPIN`), and with facts-P
  `breaks.txt` (`M2 BRK`, `M2 BRKR`, `M2 CATCH`, `M2 6905`);
- from the `voodooramp.lua` run: `voodoo-ramp.txt` (`VR W`, `VR R`; the raw run was not kept);
- from dcs-A: `dcs-trace.txt` (`DT R`, `DT W`, `DT E`, `DT I3`, `DT H3`);
- from `m2-measure`: `opens.txt` (loader-A's `loader-calls-first.txt` and the `LS` lines of
  its `debug.log.gz`), `loader-first.txt` (`loader-calls-first.txt`), `tasks.txt` (`TS CR`
  and `TS X` of `tasksw-A/debug.log`), and `console.txt`: the 25 console writes of loader-A,
  each with its bytes taken in order from dcs-A's `DT U` lines after the entry (loader-A's
  `debug.log` lost the escapes of its `buf` fields; the UART bytes after the entry are
  exactly the 902 bytes written, and the 14 writes in `loader-calls-first.txt` match them).

The raw runs are kept in `mame/m2-facts/raw/<run>/` (`debug.log.gz`, `mame.out`, `wall.txt`,
and the snapshots of facts-P and dcs-F2); the scripts as run are in `mame/m2-facts/scripts/`.
