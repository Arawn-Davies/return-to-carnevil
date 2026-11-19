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

The tap runs each kept MAME's state in a folder of their own (on the share, each run folder
holds `cfg`, `nvram`, `snap` and `diff`), which gives the blank NVRAM: add
`-cfg_directory`, `-nvram_directory`, `-snapshot_directory` and `-diff_directory` pointing
into an empty folder.

The taps are removed and reinstalled every 30 frames, because the GT64010 remaps the CPU
address space when its windows are written, and an access between a remap and the next
reinstall is lost (`hardware-usage.md`).
