# The cooperative task switch, measured

What M2 needs to know about GAME.EXE's tasks before it chooses how the port runs them (M1
report, "Risks and decisions for M2", item 1): how many tasks exist at once, how often they
yield, how big their stacks are and how deep they go, what paces the scheduler, and whether
interrupts or the loader ever take part in a switch. Facts and numbers only; no design.

Each statement is marked **measured** (MAME), **read** (disassembly of GAME.EXE or of the
loader) or **inferred**.

## Summary

- **Tasks** (measured): at most 6 alive at once, typically 5, in attract mode and in play. Six
  long-lived ones (`loadfram`, `utime`, `volume`, `ccproc`, `attract`, `game`) and short ones
  (`main`, the first task, which starts the others and exits after 158 frames; `loadwave` per
  sound load; `start` per start press, which exits within 2 frames). Tasks end almost only
  through the exit 0x80151B50; one kill by id (`attract`, when play starts).
- **Yields** (measured): 1.83 per frame on average in attract mode and 1.32 in play, at most
  10 in one frame (5 in play); 3 to 5 per scheduler pass, one per due task. The sleep argument
  is 1 in 99.4% to 99.9% of yields. 18 (attract) to 22 of the 70 static call sites run.
- **Stacks** (read and measured): every task stack is a 2 KB heap block (0x800 bytes; the
  creation wrappers always pass 0 extra). The deepest use seen was 0x4F8 bytes (1,272) below
  the block's end; the lowest 0x300 bytes of every block were never written.
- **Pacing** (measured): VBLANK. The scheduler waits until the VBLANK handler's counter
  reaches a target of 2 or 3 frames, so the game logic runs at 27 or 18 passes a second on
  MAME's 54.15 Hz screen. The scheduler's `timer:` ioctls 0x7403/0x7407 never run (their flag
  is never set; its setters are never called).
- **k1** (measured): equal to the scheduler's sp at every exception taken in a task and at
  every yield, and unchanged across every exception (entry against eret), in all runs.
- **The task list** (measured): written only by GAME.EXE's own task routines; no yield, exit
  or kill inside an exception. One exception to "no interrupt touches the task list": **in
  play, each start press creates a task from inside the VBLANK interrupt**, which writes the
  free list and the list head (and allocates a stack from the heap) at interrupt time.
- **Other stack switches** (read, and checked dynamically): none.
- **Loader calls**: `loader-calls.md`.

## Evidence

- MAME 0.276 (Debian package) in the Linux container, `carnevil` set and CHD, blank NVRAM, default DIP
  switches, `-nodrc` with the debugger on (headless), as in `tools/mame/README.md`. The
  script is `tools/mame/tasksw.lua`, the summary `tools/mame/tasksw_report.py`; command lines,
  the log's line formats and outputs are in the README.
- **Run A**, attract mode: no input, 150 emulated seconds from GAME.EXE's entry (entry at
  5.004 s after power-on, end at 155.018 s).
- **Run B**, play: the same with scripted inputs, 230 emulated seconds from the entry. What was
  injected, and how play is known to have started, is under "Run B: getting into play". Run
  B1, an earlier play run of 110 s (same inputs up to 75 s, none after), is kept and quoted
  where it adds something.
- Breakpoint and watchpoint actions do not change emulated time (the debugger stops the
  machine), so the runs are as deterministic as the earlier ones: run A and the first 24
  emulated seconds after the entry of run B (before the first input) gave the same events
  (13,049 identical event lines before the first input at frame 1588, cycle counts
  included), and the two play runs B1 and B gave the same 46,263 lines up to their first
  differing input (frame 4348).
- Frame numbers are MAME's screen frame counter; 54.15 frames per emulated second in both
  runs. The vblank count the scheduler waits on (0x801A2FE8, below) advances once per frame.
- Outputs on the share: `mame/m2-measure/tasksw-A/`, `tasksw-B/` and `tasksw-B1/`
  (`debug.log`, `report.txt`, the play runs' snapshots).

## The mechanism (read)

- **Task record**, 0x30 bytes (read, 0x80148DD0 allocates them 16 at a time with the heap
  allocator 0x80143EE8 when the free list at 0x801A3114 is empty and the word 0x801A3120 is
  0): +0 next, +4 previous (a doubly linked list from the head at 0x801A3110), +8 saved sp,
  +0xC sleep counter (halfword), +0xE halfword cleared at creation, +0x10 **the stack block's
  base address** (the M1 report called it "an argument": the trampoline passes it as a0, see
  below), +0x14 a hook 0x80149C58 calls before freeing (cleared at creation), +0x18 an id the
  creator gives (0x80149D20 kills every task whose id matches under a mask), +0x1C an 8-byte
  name, +0x24 the task function, +0x2C cleared at creation.
- **Creation**, 0x80148FA4 (a0 name, a1 id, a2 pointer to 4 argument words, a3 function,
  fifth argument at sp + 0x10 an extra stack size) and 0x80149200 (the same arguments): take a
  record from the free list, allocate a stack block of the fifth argument + 0x800 bytes from
  the heap (0x80143EE8; the creation fails and returns 0 if that fails), set the counter to 1,
  the saved sp to base + size + 0x7F8, store 0x8014A930 at saved sp - 0x28 (where the restore
  path loads ra), copy the 4 argument words to the stack base, copy the name and return the
  record. 0x80148FA4 links the record at the head of the list; 0x80149200 links it after the
  current task.
- **Callers.** Each creation routine has one caller, a wrapper that passes 0 as the fifth
  argument: 0x8015201C (13 `jal` sites) calls 0x80148FA4 and 0x80152098 (no `jal` site; not
  seen called) calls 0x80149200. **So every task stack is exactly 0x800 bytes (2 KB)**, from
  the heap (read; measured below: every creation in all three runs had a fifth argument of 0).
- **First entry**, the trampoline 0x8014A930: a0 = record + 0x10, the stack base, which
  holds the 4 argument words; it calls record + 0x24 with that a0 and, if the function
  returns, jumps to the exit 0x80151B50 (a1 = 1 in the delay slot).
- **Exit**, 0x80151B50: calls 0x80149C58 (current task, 1), swaps sp and k1 and jumps to the
  scheduler's restore path 0x80151E90. 0x80149C58 (record, a1) finds the record in the list,
  calls its +0x14 hook if set, and calls 0x8014947C, which unlinks it (if it is the current
  task, the current task becomes its predecessor, so the scheduler's walk goes on from there),
  returns it to the free list and frees its stack block (0x8014431C). The exit therefore frees
  the stack it is still running on and keeps using it until the swap; nothing allocates in
  between (read).
- **Kills.** 0x80149C58 is also called from other places on other tasks, and 0x80149D20
  (id, mask) kills every task whose id matches, except the current one (read).
- **The yield**, 0x80151AC0 (a0 = number of passes to sleep): asserts that a current task
  exists (`break 0x407` otherwise), ORs a0 into its counter, swaps sp and k1 (three `xor`s),
  saves s0-s7, fp, ra and the FP halves f20-f31 just below the task's sp, stores that sp at
  +8 and jumps to 0x80151E90.
- **The scheduler**, 0x80151BC0, called once from `main` (0x800C4524, at 0x800C4740) on
  `main`'s stack, just after `main` created the first task, also named `main` (0x800C50AC,
  from 0x800C4708). Each pass: wait (below), call two per-pass routines
  (0x80168224 when 0x801A36B8 is set, then 0x80152288), set the current task to the head and
  walk the list: for each task with a positive counter, decrement it; at 0 resume it (save the
  scheduler's registers below its sp, sp -= 0xCC, load the task's saved registers from its
  saved sp, swap sp and k1, `jr ra`). After the walk: 0x801522CC, then 0x80148BBC, 0x8014CC94
  and 0x80153814 when 0x801A36B8 is set, then 0x8014A0F4, and `ioctl(5, 0x7407, 0x801A36C0)`
  when 0x801A36C4 is 1.
- **The wait at the start of a pass** (0x80151C04 to 0x80151C80): store the vblank count
  0x801A2FE8 to 0x801E0EBC, then spin until 0x801A2FE8 >= 0x801A36BC (a spin counter
  0x801A36CC calls the hook at 0x801A36C8 if it passes 100,000,000), then set 0x801A2FE8 to 0
  and, when 0x801A36C4 is 1, call `ioctl(5, 0x7403, 0)`. 0x801A36BC is set only by 0x80152114
  (stores a0); 0x801A36C4 only by 0x8015217C (sets 1) and 0x80152190 (clears it).
- **Who increments 0x801A2FE8** (static stores): 0x800C6090 in 0x800C6030 (the CPU line-0
  interrupt handler `main` registers, which is VBLANK: slot 0x200 enables the VBLANK interrupt
  when line 0 is installed, `hardware-usage.md`), 0x8013F4C8 in 0x8013F4A8 and 0x80147928 in
  0x80147908 (two more copies of the same increment), and the scheduler's own clears at
  0x80151BD0 and 0x80151C88.
- **No other stack switch** (read, static, all 165,760 reached words of GAME.EXE and every
  other word of the text): the only instructions that write sp other than as sp plus a
  constant (`addiu`/`addi`/`daddiu`/`daddi` sp, sp, imm) are `_start`'s `lui sp, 0x8080`
  (0x800C4000) and the three `xor`s that swap sp and k1 in the yield (0x80151AE8), the exit
  (0x80151B74) and the scheduler (0x80151E84). The only writers of k1 are those swaps and the
  scheduler's `lw k1, 8(t1)` (0x80151E20). No reached code writes k0. A static scan of the
  unreached text words found no other sp writer either.

## Run B: getting into play (measured)

Inputs, set from Lua through MAME's input fields at fixed emulated times after the entry
(`PLAY_STEPS` in `tasksw.lua`; each set and release is logged as a `TS IN` line):

- `:SYSTEM` "Coin 1" pressed for 0.3 s at 24, 25, 26 and 27 s (the attract screen at 20 s
  shows `CREDITS: 0 OF 2`);
- `:SYSTEM` "1 Player Start" for 0.3 s at 29 and 33 s;
- `:FAKE` "P1 Trigger" for 0.2 s at 31 s and then every second from 38 s on (the gun's
  position was never set);
- from 75 s on: "1 Player Start" for 0.3 s every 3 s (75.6 s, 78.6 s, ...) and "Coin 1" twice
  every 10 s (75.3 s and 76.3 s, 85.3 s and 86.3 s, ...), to take the "Continue?" offers.

How play is known to have started (measured): the start press at 29 s (frame 1859) creates the
tasks `start` (0x800C6D04) and `game` (0x800C5830) in the same frame, and snapshots (`snap/` in
the run folder, one every 10 s from 40.5 s) show the level-select screen at 36 to 50.5 s,
`PLEASE WAIT` at 60.5 s, and the level (Rickety Town, "Slay Ride", health bar and gun visible)
from 70.5 s. From then to the end of the run (230 s, frame 12738) every snapshot, one every 10
s, shows the level being played (enemies, the `RELOAD!` prompt, the health bar, the credit
count rising as the coins go in, sometimes the second player's `PRESS START`): **about 160
emulated seconds of play, frames 4105 to 12737**, which the "play" figures below cover. The
task `game` stayed alive throughout. Only the first level's first part was seen: the gun was
never aimed.

Run B1, the first play run, used the same inputs up to 75 s but none after (110 s from the
entry): the player died at about 78 s without aiming (`Continue?` at 80.5 s, `GAME OVER` at
100.5 s), so it has only about 15 s of play. Its numbers are given beside run B's where they
differ.

## 1. Tasks (measured)

- **Tasks alive at once: at most 6, typically 5**, counted from the creations and frees at the
  start of every scheduler pass (a frame with two passes counted once): run A 5 at 3,408
  passes, 6 at 169 (while `loadwave` ran, frames 711 to 1223), 1 at the first; run B 5 at 3,727
  passes and 6 at 402 (median 5, maximum 6 from the play frame on); B1 5 at 1,438, 6 at 577.
- **The task functions** (read from the records: name at +0x1C, function at +0x24, id at
  +0x18, and the call site of the wrapper 0x8015201C that created each), in order of first
  creation; run A:
  - `main`, 0x800C50AC, id 0, created by `main` (0x800C4524) from 0x800C4708 at frame 521:
    started 1, exits after 158 frames (it creates the next five and `attract`);
  - `loadfram`, 0x800E7D50, id 0x400, from 0x800E77C8: started 1, never exits;
  - `utime`, 0x800C4F30, id 0x8000, from 0x800C5314: started 1, never exits;
  - `volume`, 0x800C6F44, id 0x8201, from 0x800C5568: started 1, never exits;
  - `ccproc`, 0x800C7B3C, id 0x8200, from 0x800C5590: started 1, never exits;
  - `attract`, 0x800CB084, id 0x100, from 0x800C5728: started 1, never exits in run A;
  - `loadwave`, 0x800F5CBC, id 0x800, from 0x800F5BA8: started 1, exits after 512 frames.
- Play adds (runs B1 and B): `start`, 0x800C6D04, from 0x800C6CEC, created on each start press
  and exiting in the same frame; `game`, 0x800C5830, from 0x800C5818, created by the first
  start press and living until the game is over; `attract` freed when play starts and created
  again (as `game`, function 0x800CB084, from 0x800C5E20) after it; `loadwave` once per level
  load (4 times in B1). In run B `start` ran 54 times (one per accepted start press, each
  living 0 to 2 frames), `loadwave` twice, and the game never ended, so `attract` was not
  created again.
- **Lifetimes**: tasks are long-lived. In 150 s of attract mode 7 tasks were created and 2
  ended, both through the exit 0x80151B50 (`main` and `loadwave`); 5 were alive at the end.
  Run B: 63 created, 58 ended, 57 of them through the exit (54 `start`, `main`, 2 `loadwave`).
  Run B1: 14 created, 9 ended, 8 through the exit. The one other end in each play run is
  `attract`, freed by the `start` task when play starts, through 0x801495EC (free the first
  task with a given id, here 0x100, without calling the +0x14 hook; called from 0x800C57E0;
  the free 0x8014947C is called from 0x80149634). Every 0x80149C58 call came from the exit
  (ra 0x80151B70) and 0x80149D20 was never called: no other task was killed.
- Every task's first resume entered the trampoline 0x8014A930 (ra loaded from saved sp -
  0x28), as many times as there were creations.
- 0x80149200 (insert after the current task) was never called; every creation came through
  0x80148FA4 and the wrapper 0x8015201C.

## 2. Yields (measured)

- **Per frame** (frames from the first pass, frame 521, to the end): run A, 14,462 yields in
  7,887 frames, **mean 1.83, maximum 10**, histogram (yields: frames) 0: 4,309, 1: 1, 3: 38,
  4: 3,357, 5: 180, 9: 1, 10: 1. Most frames have none because a pass runs every 2 or 3
  frames (section 4). Run B, the whole run: 16,807 yields in 12,217 frames, mean 1.38,
  maximum 10; **in play** (frames 4105 to 12737): 8,633 frames, **mean 1.32, maximum 5**,
  histogram 0: 5,755, 3: 96, 4: 2,773, 5: 9. The 9- and 10-yield frames are frames holding two
  passes, all outside play.
- **Per scheduler pass**: run A 4 (3,358 passes), 5 (183), 3 (38), 1 (1); run B1 4 (1,370),
  5 (582), 3 (70), 6 (1), 1 (1). Each task that is due yields once per resume, so a pass
  makes as many yields as it resumes tasks (resumes: 14,464 against 14,462 yields and 2
  exits in run A). Run B in play: 4 (2,772 passes), 3 (96), 5 (9).
- **Rate**: 96 yields per emulated second in run A, 73 in run B.
- **Call sites**: 18 of the 70 static `jal 0x80151AC0` sites ran in run A, 22 in run B1,
  21 in run B. The hot ones (run A count, call site, task): 3,579 at 0x800C7B6C (`ccproc`),
  3,579 at 0x800C7034 (`volume`), 3,122 at 0x800E7E54 and 457 at 0x800E7F2C (`loadfram`),
  2,973 at 0x800CB27C, 240 at 0x800CB1D0 and 150 at 0x800CB444 (`attract`), 172 at
  0x800F5BC8, 107 at 0x8016BE30 and 64 at 0x80149A88 (all three `loadwave`; in B1 the
  `game` task also yields at 0x80149A88), 12 at 0x800C4F74 (`utime`); the rest ran once or a
  few times. Every site seen is one of the 70. In play the
  `game` task adds 0x800C5CE0, 0x800C5A8C and others (`report.txt`).
- **Sleep argument a0**: almost always 1 (run A 14,445 of 14,462; B1 8,583 of 8,607). The
  others: 0x12C (300 passes; `utime`, 12 in A, 7 in B1), 0x14 (20, once, from 0x800C56A4 in
  `main`), 5 and 6 (twice each in A), and in B1 2, 3, 6 and 0x11. The argument is ORed into
  the counter, which the scheduler then decrements once per pass (read).

## 3. Stacks

- **Where they come from** (read): the heap, through 0x80143EE8, one block of the fifth
  argument + 0x800 bytes per task; the record holds the block's base at +0x10 and the task
  starts with sp = base + 0x7F8 (the block's last 8 bytes stay unused by the first frame).
  The wrappers pass 0, so every block is 0x800 bytes: **every task stack is 2 KB** (measured:
  all 7 creations in run A, 14 in B1 and 63 in B, had a fifth argument of 0). The heap
  reuses blocks and records: `loadwave` got `main`'s freed record and block in both runs.
- **Deepest use**, two ways (measured):
  - sp at every yield and exit, against the block's end (base + 0x800): the deepest was
    0x470 bytes (`loadwave`, A and B1) and 0x4A0 (`game` 0x800CB084, B1); the always-running
    tasks yield 0x30 to 0x58 bytes below the end;
  - the words of each stack that changed between the creation and the free (or the end of the
    run), compared with a copy made at the creation (not invasive; a write of the value already
    there is not seen, so this is a lower bound): the lowest changed word was **0x4F8 bytes
    below the block's end at most** (`game` 0x800CB084 in B1); by task, `main` 0x470, `attract`
    0x480, `loadwave` 0x4C8, `game` 0x800C5830 0x2D8, `ccproc` 0x130, `utime` 0x120, `loadfram`
    0x118, `start` 0x138, `volume` 0x90. Run B's deepest was 0x4C8 (`loadwave`); the 0x4F8 is
    from B1's second `attract` (function 0x800CB084, created after the game over).
  - The lowest 0x300 bytes of every 2 KB block were never written in either run, the 4
    argument words at the base included (the map of 256-byte blocks in each `KS`/`LS` line).
  - Only sp at yields and exits was sampled, not at function entries (a breakpoint on every
    function entry would cost far more than the run); the changed-word comparison covers every
    write between the creation and the free instead, interrupt time included.
- Interrupts do not use the task stacks (read): the loader's exception entry saves state in
  its own areas and its dispatcher switches to the exception stack below 0x800C1400
  (`boot-handoff.md`); measured: the loader calls made from interrupt handlers ran with sp at
  0x800C1320 to 0x800C1368 (`loader-calls.md`), and the stack comparison above includes
  whatever interrupts wrote.

## 4. What paces the scheduler (measured)

- **VBLANK, not the timer ioctls.** The flag 0x801A36C4 that guards `ioctl(5, 0x7403)` and
  `ioctl(5, 0x7407)` in the scheduler was 0 at every pass of all three runs, and neither call was
  made from the scheduler (0 hits at 0x80151CAC and 0x80151FF8). Statically the two setters
  of the flag, 0x8015217C and 0x80152190, have no `jal` caller and their addresses are not
  built anywhere in the code (read). The 7,160 calls of `ioctl(5, 0x7403)` in the attract run
  (`loader-calls.md`, two per pass) come from 0x8014FE24 instead, which the per-pass routine
  0x80168224 and code at 0x8015EECC and 0x8015EF40 call, some of them inside interrupts; the
  scheduler does not wait on that timer.
- The scheduler's wait spins until the vblank count 0x801A2FE8 reaches the target
  0x801A36BC. The count is written by 0x800C6090 (in the VBLANK handler 0x800C6030) inside an
  exception once per frame, 7,887 writes in 7,887 frames in run A, and by the scheduler's own
  clears; nothing else wrote it (watchpoint, attributed by pc).
- **Passes per frame**: run A 3,581 passes in 7,887 frames, mean 0.454, never more than 2 in
  a frame (3 frames). The target was 2 at 2,973 passes and 3 at 607 (1 at the first): the
  game logic runs at 27.1 or 18.1 passes per second against 54.15 frames per second (MAME's
  `400x256 54Hz` mode). Frames between pass starts: 2 at 2,980 passes, 3 at 560, 1 at 12, 4 at
  14, 5 to 6 at 9, and the two start-up gaps (32 and 99 frames). Run B1: target 3 at 1,542
  passes and 2 at 482; run B: 3 at 3,826 passes and 2 at 309, 3 frames between passes at
  3,669 of them. In play the target was 3 (18.0 passes a second).
- **Time in the wait**: mean 5.19 million cycles (34.6 ms) per pass in run A, median 4.76
  million, max 8.29 million; only 2 passes found the count already at the target. So the pass's
  work takes well under one frame in attract mode. Run B: mean 5.79 million cycles (38.6 ms),
  median 5.71 million, 12 passes without a wait. Over the time from the first pass, the
  scheduler spent about 85% (A) and 71% (B) of the CPU's cycles in this wait (computed from the
  measured cycles).
- The target is set through 0x80152114 only (the one store to 0x801A36BC), from 9 call
  sites; four pass the constants 1, 3 or 9, the others computed values (read).

## 5. Exceptions and the task list

- **Exceptions taken while a task ran** (measured; context by sp at the exception vector, in a
  task's block or not): run A 892 of 16,609 exceptions (the others in the scheduler's wait, on
  `main`'s stack), B1 1,581 of 14,046, B 1,266 of 43,806. Of run A's 892, 770 had their EPC in
  the loader (a task inside a loader service), 115 in GAME.EXE, 7 in the stub block. Exception
  codes: interrupts (code 0) but 16 breakpoints (code 9: the 16 `break 0x409` handler
  registrations of `main`, which return through the debug monitor's eret 0x80004804). Interrupt
  lines (Cause IP bits) in run A: IP6 7,886 (VBLANK), IP2 5,079, IP4 3,626, IP3 1, IP2+IP6 1.
- **k1**: at every one of those exceptions k1 held the scheduler's sp (0x807FFEEC, the only
  value the scheduler ever ran tasks with) at the vector, and **k1 at the eret equalled k1 at
  the vector for every exception of all three runs** (A: 892 in tasks, 15,717 elsewhere; B1:
  1,581 and 12,465; B: 1,266 and 42,540). Every yield (14,462 in A, 8,607 in B1, 16,807 in B)
  saw k1 equal to the scheduler's sp. The loader's dispatcher saves k1 with the other registers
  and restores it with `ld k1, 0xD8(k0)` before its eret at 0x80004D64 (read).
- No exception was taken inside the yield, the exit or the scheduler's restore path; one (run
  B1, frame 5315, VBLANK) hit the resume path at 0x80151DE0, while the scheduler saved its
  registers, before the stack swap.
- **Yields, exits, kills and frees inside an exception: none** in any run. Every yield's ra was
  in GAME.EXE; no loader service called the yield.
- **Task creation inside an exception: yes, in play.** Each "1 Player Start" press that the
  game accepted created the task `start` from inside the VBLANK interrupt (run B1: 2 of its 14
  creations, frames 1859 and 2075; run B: 54 of its 63 creations, every `start`): the creation
  ran on the loader's exception stack (sp 0x800C1310), with the current task 0 (the scheduler
  was in its wait), and wrote the free list 0x801A3114 and the list head 0x801A3110 from
  0x80149048 and 0x801490A8 inside the exception. The path: `main` stores 0x800C6CC0 as the
  callback of switches 2 and 5 in the table at 0x801AAB78 through 0x80146C04 (calls at
  0x800C56C0 and 0x800C56CC; read); the VBLANK handler 0x800C6030 calls the switch scan
  0x80146D9C at 0x800C6104 (read), whose ioctls on fd 4 are measured inside the interrupt
  (`loader-calls.md`); that the scan calls the switch callbacks (through 0x80147130 or
  0x801470A4) is inferred, and the wrapper's saved return address in the logged creation is
  0x800C6CF4, in 0x800C6CC0 (measured). The creation also allocates the task's stack from the
  heap (0x80143EE8) inside the interrupt (read).
- **Writes to the task list words 0x801A3110 to 0x801A311B** (watchpoint; pc is the next pc, so
  each site below is the store's address + 4), run A, 21,668 writes, all by GAME.EXE code
  outside exceptions: current task 0x801A3118 from the scheduler (0x80151CE4: 3,580, the walk's
  start; 0x80151F7C: 18,067 and 0x80151F94: 1, its steps) and from the free routine 0x8014947C
  (0x801494B0: 2); the head 0x801A3110 from creation (0x801490AC: 7) and the free routine
  (0x801494D4: 1); the free list 0x801A3114 from the record pool (0x80148E44: 1), creation
  (0x8014904C: 7) and the free routine (0x80149598: 2). Run B1 adds only the 4 writes inside
  the VBLANK exception above. Run B: 25,557 writes, 108 of them inside the VBLANK exception
  (the 54 interrupt-time creations, two each), all at the same sites.
- Loader services called from interrupt handlers (`loader-calls.md`): the VBLANK handler's
  switch scan calls `ioctl` on fd 4 four times a frame, and `timer:` ioctls 0x7403 and 0x740B
  also run at interrupt time (0x740B through 0x8014FE54, which the GT64010 bit-4 handler
  0x8014FDF0 calls). No loader service writes the task list words (all writes above are by
  GAME.EXE code) and none calls the yield.

## 6. Other stack switches

- **Read** (static, above): no reached instruction of GAME.EXE writes sp other than as sp plus
  a constant, except `_start`'s initial `lui` and the three swaps; no `move sp, ...`, no `lw
  sp`, no setjmp/longjmp-like code. An `addiu sp` can move sp by at most 32 KB.
- **Measured**: at every yield and exit of all three runs sp was inside the current task's 2 KB
  block, and at every resume the saved sp loaded from the record equalled the sp of that
  task's last yield (or its initial sp before its first run): 0 exceptions to either rule.
- Not done: a check of every `jr ra` (sp at the return against sp at the call). It would need
  a breakpoint on every `jr ra` and every call, millions of hits a second; the static
  argument above covers the same ground for GAME.EXE's code, and the per-yield and per-resume
  checks show that no task ever reached a yield on a foreign stack. The loader's own code was
  not scanned: its dispatcher switches to its exception stack and back (read), and it runs no
  game code on that stack except the registered handlers (and, in play, the task creation
  above).

## Not measured

- Play beyond the first part of the first level (the gun was never aimed): other levels,
  bosses, the movie player during play, high-score entry; two-player play; the test menu.
- sp at function entries (section 3 explains what was done instead).
- Whether the board's R5000 would take an interrupt inside the switch code more often than
  MAME, which takes interrupts only between instructions it chooses (MAME-specific timing).
