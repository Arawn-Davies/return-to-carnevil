# M2 report: the runtime, headless to attract mode

Milestone M2 (19 and 20 November 2026) built a native runtime under the recompiled game. The
program `carnevil` loads `GAME.EXE` from the user's own disk image, runs it with no picture,
no sound and no input for a chosen span of virtual time, and in the 150 s checked against
MAME it reaches attract mode and stays there, with its console output, its file opens, its
task activity and its first loader calls matching MAME's. This page says what now works, how
strongly each claim is established, what was decided during the work and what each decision
costs if it is wrong, and what is still unknown. How the runtime is put together is in
[runtime](runtime.md); the plan it reports against is
`docs/superpowers/plans/2026-11-19-carnevil-recomp-m2.md`; the record of the work, task by
task, is the ledger `.superpowers/sdd/2026-11-19-carnevil-recomp-m2/progress.md`.

Every fact below carries its basis, as in the [M1 report](M1-report.md):

- **static**: measured by the committed tools or by counting the committed files;
- **read**: read from the code by hand (the game's or the loader's disassembly);
- **test**: checked by the test suite (`python3 -W error -m unittest`);
- **MAME**: measured in MAME 0.276 (run headless in the Debian container the Linux container) by the script
  named; all MAME runs cover boot and attract mode with a blank NVRAM and no input, except
  where play is named;
- **run**: measured on the port's own 150 s run (its trace), on the Mac unless named;
- **from MAME's model**: a hardware rule taken from MAME's source, read, not built;
- **inferred**: a conclusion from the above, not measured.

## The numbers

- **Code**: `runtime/` holds 9,423 lines of C and headers (static, `cat runtime/*/*.c
  runtime/*/*.h | wc -l`), 7,665 of them in files new in M2 (static, `git diff --name-only
  --diff-filter=A 4cc16c4 e50f325 -- runtime | xargs cat | wc -l`). The milestone is 24
  commits from the plan (`4cc16c4`) to `e50f325`, plus
  this report.
- **The run**: `carnevil --disk carnevil.raw --seconds 150` exits 0 after 150 s of virtual
  time on the Mac (Apple clang, arm64). Task 9 also ran it in the Linux container (GCC 14.2, x86-64) and
  reported that trace's SHA-256 equal to its Mac trace's; the Task 9 reviewer's own Mac run
  produced a trace with that same hash. The reviewer ran nothing on Linux, so the Linux run
  rests on Task 9's report, supported by the matching hash, not re-run independently (run).
- **Against MAME** (test, `tests/test_boot.py`): console writes 25 of 25; opens 193 of 193;
  task creations and exits 9 of 9; of the first 400 loader calls on each side, the 389 made
  outside interrupts, 389 of 389; at most 6 tasks alive at once; the `attract` task alive at
  the end; 8,072 VBLANK interrupts; no interpreter entry and no tolerated request. Each is
  defined under "What was compared with MAME" below.
- **Host speed**: 150 virtual seconds in 10.4 to 12.3 s of wall time on the Mac (12 to 14
  virtual seconds per host second), and in 29 s in the Linux container (5.2 per host second) as Task 9
  reported it, not re-run independently (run; below).
- **Tests**: 491 tests with game data, 5 of them skipped; 485 without (`CARNEVIL_WORK` set to
  a missing folder), 61 skipped (test, at this report's commit; the M1 figures were 309 and
  303).
- **Builds**: `make carnevil` builds with zero warnings under `-Wall -Wextra -Werror` on the
  Mac and, as Task 9 reported (not re-run independently), with GCC 14.2 in the Linux container (run).

## What runs

From the entry, the game runs its own start-up: Glide's PCI set-up and card checks, the
handler registrations, the scheduler's start ("Starting process dispatcher"), the CMOS and
high-score checks a blank NVRAM fails, two sound downloads to the DCS responder, and the
attract loop with its movie streaming from the disk. At 150 s five tasks are alive:
`loadfram`, `utime`, `volume`, `ccproc` and `attract` (function 0x800CB084) (run); MAME's task
events, which the port's equal, leave the same five (MAME, `tasks.txt`; inferred). The
game's tasks run on host threads, one at a time.

What "runs" does not mean: nothing is drawn beyond what Glide's start-up checks read back,
nothing is played, no input is taken, and the program's pace is neither the board's nor real
time ("The clock", below). Attract mode is the only state checked. A played game is expected
to stop early (inferred: play was never run in the port, and the runtime stops on every
request it has no evidence for): play reaches requests nobody has measured (input ports with
input, coin
ioctls, sound commands with other arguments, DMA chain shapes and Voodoo modes not seen in
attract mode, the drained sound FIFO), and every such request stops the program by design
("Stopping instead of guessing" in [runtime](runtime.md)).

## What was compared with MAME, and how strong it is

### The boot run

One shared run of `--seconds 150` is compared with MAME's run A (`mame/m2-facts/`, made by
`tools/mame/m2facts.lua` and the earlier `loadercalls.lua` and `tasksw.lua`; MAME). The
comparisons are of order and values, never of times, because the port's clock does not keep
the board's pace.

- **Console**: the list of (fd, text) of every console write equals MAME's `console.txt`
  exactly: 25 writes, 902 bytes (test, exact).
- **Opens**: the ordered list of (name, mode, fd) equals `opens.txt` over the common length,
  and the test requires at least 180 of MAME's 193. The run made 193 and all 193 are equal, so
  in this run the comparison is complete; the test itself would also pass a run that made
  between 180 and 192 matching opens (test, prefix; run, 193 of 193).
- **Tasks**: the ordered list of (create or exit, task function) equals `tasks.txt`: 7
  creations and 2 exits (`main` and `loadwave`), 9 of 9 (test, exact); at most 6
  tasks on the list at once (test). The port finds creations by reading the game's task list
  at each host switch, so a creation is placed at the next switch, not where it happened; a
  creation and an unrelated exit between two switches could be ordered differently from
  MAME's. In this run they are not (run).
- **First loader calls**: of the first 400 loader calls on each side, those made outside an
  exception (the port: Status EXL clear; MAME: sp outside the loader's exception stack),
  389 on each side, agree in order on the slot, the arguments that slot's signature has, and
  v0 (test, exact over the common length; run, 389 of 389). Not compared: the 11 calls inside
  interrupts, sp, v1, unused argument registers, and anything after the first 400.
- **Attract mode reached and kept**: exit status 0 at 150 s, at least 8,000 VBLANK interrupts
  (8,072), and `attract` alive at the end (test). The 8,072 equal one VBLANK per card frame of
  18,476,190 ns from the game's VBLANK registration at 0.857 s to 150 s (inferred, from the
  trace's times).
- **Nothing unexplained**: no interpreter entry and no tolerated request; the list of
  exceptions, `config/runtime.expected.toml`, is empty (test). The tolerated half of that
  test cannot fail at present, because nothing in the runtime produces a tolerated request
  (Open items).
- **Determinism and the sanitizer**: two traced 20 s runs are byte-identical, and an untraced
  one prints the same output and ends with the same RAM hash (test); a 20 s run built under
  `-fsanitize=undefined` reports nothing on the Mac (test), and in the Linux container as Task 9 reported
  (not re-run independently).
- **The user's files**: a missing image, an image of the wrong size and an image whose
  `GAME.EXE` has another hash each exit 2 with one line naming the file; the raw image's size
  and modification time are unchanged by a run; a second run on the first run's overlay and
  NVRAM does not print "reseting high scores" (test).
- **Independent check**: the Task 9 reviewer built and ran the program itself (150 s, exit
  0), recomputed the four comparisons with its own script from its own trace (25/25,
  193/193, 9/9, 389/389, with identical slot sequences and every a0 to a3 or v0 difference in
  a field the slot's signature lacks), and found its Mac trace's SHA-256 equal to the one
  Task 9 reported for its the Linux container trace. It ran nothing on Linux.

Counts the tests do not compare (run against MAME's `loader-calls.md`): over 150 s the port
made 5,556,700 loader calls against MAME's 5,939,280. The sound data words agree exactly
(ioctl 0x6908: 1,230,931 on both sides); the SOUNDSTAT polls do not (0x6909: 1,421,720
against 1,679,830), because the DCS responder answers sooner than the board (below). The port
ran 3,673 scheduler passes (one 0x0E8 call and one DMA chain each) against MAME's 3,580,
because its clock's pace differs.

### Each model, and what it was checked against

- **The disk library** (`runtime/disk`): every one of the 1,523 files of partition 1 is found
  by the C reader with the size the Python reader gives, and reads back with the same
  SHA-256; all 1,504 files of the disk's own checksum table `FILESYS.CHK` are present and
  match; `GAME.EXE` has the supported hash; a write round trip through the overlay reads back,
  survives a second process, and leaves the raw image's bytes and modification time
  unchanged; damaged chains and directories are refused without reading out of bounds
  (test, 23 tests; the reviewer recounted the 1,523 files itself). The directory rules were
  confirmed in the loader's routines (read). Not checked: the loader's own write path (it
  writes the directory entry at close and allocates from a hint; the C library does neither),
  crash consistency of the overlay, and two processes on one overlay.
- **The clock and interrupt delivery** (`clock.c`, `irq.c`, `exc.c`): checked against
  `loader-services.md` section 6, which was read from the loader's disassembly, on synthetic
  guest code: the frame's layout word by word, no nesting, masking, the order of lines,
  handlers that edit the frame, every stop path (test, 24 tests). The game's VBLANK, IP3 and
  GT64010 handlers then ran in the boot run. Not checked against MAME: no MAME run recorded a
  frame or the timing of an interrupt's delivery; the port delivers only at check points,
  where the board takes an interrupt at any instruction. The exception path other than
  interrupts (break hooks, the FP catch-all) never ran in MAME's attract or play runs
  (MAME, `breaks.txt`), so it has run only on synthetic code in the port.
- **The task contexts** (`tasks.c`): 9 tests, each repeated 20 times without a failure in
  Task 5, and the scenarios again under the thread sanitizer on the Mac with no report (test).
  Task 9 reported that in the Linux container the task tests pass and that the 150 s run and the 20 s
  sanitizer run end tasks through `pthread_exit` from deep inside generated code with glibc's
  forced unwind, on 1 MB host stacks; this was not re-run independently, and the 150 s part is
  supported by the matching trace hash. The thread sanitizer does not run in the Linux container (its test
  skips there).
- **The loader stand-ins** (`runtime/loader`): each slot and ioctl against
  `loader-services.md` (read) and the measured values it quotes (41 tests), and in the boot
  run the first-400 comparison above, which covers 18 of the 29 slots with their arguments
  and results (test; the 18 counted in `loader-first.txt`). Measured inputs: the four input
  ports' no-input values, the PIC words,
  the blank NVRAM's fill (MAME). Not checked: the slots and ioctls attract mode never calls
  (they stop), the CMOS contents against MAME's NVRAM after a run, and the loader calls after
  the first 400 beyond their counts.
- **The PCI slots**: the game's 44 PCI slot calls in MAME (probes, set-ups, configuration
  reads and writes) replay with MAME's results (test).
- **The Voodoo start-up model** (`voodoo_null.c`, `voodoo_fb.c`, `voodoo_tmu.c`): the capture
  `voodoo-ramp.txt` (MAME, `tools/mame/voodooramp.lua`, the entry to 2 s after it) is replayed
  at its recorded times, and every one of its 274,967 reads returns MAME's value, with no
  exclusion: 266,645 status reads, 85 register reads, the memory test's 9 LFB reads, the
  dither ramp's 8,192 and the TMU check's 36 (test; the reviewer re-ran the replay). The DAC
  model reproduces the 9 DAC reads (test). The rules are from MAME's model; no difference
  between it and the capture was found. Not checked: anything after the first 2 s, where the
  capture ends; the swap and displayed-buffer fields, which no capture exercises (the 253
  status reads of 0x0FFFF47F after 11.18 s follow swaps whose writes came by DMA and were not
  captured); every drawing mode the start-up checks do not use.
- **The card holding its writer**: from MAME's model (voodoo.cpp and gt64xxx.cpp), with unit
  tests (test). Its effect on the DMA channel is checked through the completion times below.
  Not checked by any measurement: the stall itself (no MAME run recorded the CPU's or the DMA
  channel's progress against the card's FIFO); the hold from the PCI FIFO's low-water mark
  and the release with the memory FIFO off are untested (Open items).
- **The DMA completion times** (`dev_sysctrl.c`): the fitted delay reproduces all 3,580
  measured starts of `dma0.txt` (MAME) to within 5 ns; in the port's 150 s run every one of
  its 3,673 chains, the 3,273 held ones included, completes within 5 ns of MAME's times for
  its shape (test). MAME's own burst model of the GT64010 does not reproduce those times
  (4.7% short on the 28-record chain, 6.8% on the movie chain; from MAME's model, compared in
  Task 9), so the fit stays the completion time.
- **The gun, 0x17F00000, the sound FIFO and the CMOS reads**: the values and access sizes MAME
  showed (MAME, `inputs.txt` and the M0 hardware tap; test). With no input only.
- **The sound responder** (`dcs_stub.c`): MAME's trace of the exchange (`dcs-trace.txt`,
  2,910,975 events) is replayed through it (test, re-run and its 30 block sums recomputed by
  the reviewer). Exact at every event: SOUNDCTL reads 10 of 10, SOUNDIN reads 112 of 112,
  INTSTAT reads 2 of 2, INTCTL reads 8 of 8, and SOUNDSTAT's FIFO bits at all 1,679,832 reads.
  The timing bits are compared one-sidedly: at no read is a bit (0x40 reply waiting, 0x80
  ready, 0x8000 program running) set on the board and clear in the model; the model shows it
  early at 17,978 reads for 0x40, 45 for 0x80 and 495,782 for 0x8000, and 0x40 is never shown
  with nothing queued (which holds by construction). Bits 0x0100 and 0x0200 are not modelled
  and differ at 1,611,974 reads; the game does not test them (read). The test injects the IP3
  handler's 512 sound-FIFO writes, which the trace does not record, after the handler's first
  INTSTAT read. Not checked: any exchange outside attract mode, the board's timing, the
  draining of the FIFO.

## What was not checked or not built

- **Play.** No coin, no start, no gun. A played game is expected to stop early (above). M2's
  MAME play run measured only the break hooks, the FP catch-all and the coin ioctl 0x6905, and
  the earlier run B of `task-switch.md` the tasks in play (MAME); nothing of the port ran in
  play.
- **Sound output.** The DCS responder answers the protocol; no sound processor runs and
  nothing is played (M5).
- **Any picture.** The Voodoo draws only flat and point-sampled-texture triangles of the kinds
  the start-up checks use, into a software frame buffer that nothing displays (M3).
- **Input.** The input ports and the gun return their no-input values; `ioasic_set_inputs`
  exists but nothing calls it.
- **The board's timing.** Interrupts are taken at check points, the clock's pace is not the
  board's, the DCS responder is never slower than the board, and DMA words arrive at the card
  earlier than MAME delivers them (from MAME's model: its GT64010 moves 32 words per 64 PCI
  clocks, where the port copies a chain at once up to a hold).
- **Not built, as the plan said**: IDE, the GT64010's PCI side, timers as registers and
  channels 1 to 3, the UART, the security PIC, the loader's 100 ms tick, the IP4 (IDE)
  interrupt path.
- **The known gaps of the device models**, each documented where the model is:
  - `hardware-usage.md`, "Known gaps in the card model": a held DMA chain's words go in at once
    up to the hold, not in MAME's 32-word bursts, and the completion time is the measured one,
    not derived; the 253 swap reads of 0x0FFFF47F cannot be replayed; MAME's memory FIFO lives
    in frame-buffer memory over the aux buffer, the model keeps it apart; a CPU store during a
    hold the DMA started is queued and then held, where MAME saves and re-issues it (added in
    this milestone's last task).
  - `dcs-handshake.md`, "Known gaps in the responder": the timing bits are not the board's
    (the game polls fewer times and every wait ends sooner); bits 0x0100 and 0x0200 are left
    clear; the FIFO is never drained, so a word written to a full FIFO stops, and play needs
    the board's consumption of it.

## Host speed

- **Mac** (Apple clang, arm64, 10 cores; one thread runs at a time): 150 virtual seconds in
  12.3 s of wall time in Task 9's run, 12.2 virtual seconds per host second; the reviewer
  measured 10.4 s for the same run length, 14.4 per host second (run). A 20 s run with a trace
  took 2.44 s, 8.2 per host second, including the start-up and both sound downloads (run,
  2026-11-20). With the round-1 trace of 556 MB (every loader call written), 150 s took 15.7 s.
- **The Linux container** (GCC 14.2, x86-64; 4 cores, as the M1 report gives the container): 29 s for 150
  virtual seconds, 5.2 per host second, as Task 9 reported it; not re-run independently
  (run).

These are virtual seconds of the port's clock, which does not run at the board's pace, so
they are not a measure of how fast the game would run against real time. They say only that
the headless runtime has a wide margin on both hosts.

## The clock

Virtual time counts check points at 100 ns each (`VCLOCK_NS_PER_CHECK`). A check point is a
function entry or a backward branch inside a function, so the instructions per check vary
from one code path to another, and the guest's pace against the virtual clock varies with
them. The calibration data, virtual seconds after the entry, port against MAME (run against
MAME, `console.txt` and `tasks.txt`; Task 9):

- console line 1, the banner: 0.667 against 0.189;
- line 13, "screen resolution": 0.857 against 4.287;
- line 15, "Errors in check_adjustments": 0.894 against 4.342;
- line 16, "reseting high scores": 0.904 against 4.366;
- line 18, "requesting version number": 1.194 against 5.234;
- line 20, "main_init_sound(): done": 1.260 against 5.637;
- line 21, "NSS reset": 1.314 against 6.122;
- line 23, the second "requesting version number": 2.517 against 7.384;
- line 25, the second "done": 2.584 against 7.787;
- task events (placed at the next host switch, so late by up to one pass): `main` 0.864
  against 4.287, `attract` 2.416 against 7.212, `loadwave`'s exit 8.607 against 17.264;
- the scheduler's VBLANK-wait spin counts: in the port 1,721 waits recorded (a wait equal to
  the one before is not written), median 349,784, largest 553,383; in MAME (`spin.txt`) 3,580
  waits, median 215,767, largest 376,317.

So the first line comes 3.5 times later than on the board, and the later milestones up to 5
times earlier; the spin counts are 1.5 to 1.6 times MAME's (largest and median) because the
CPU runs fast against the card's VBLANK (inferred). The game's own limit, a reboot after
100,000,000 spins, is far
off (test: the largest is below 10,000,000).

What the next milestone needs: an instruction-weighted slice, in which the emitter gives each
check point the number of instructions (or cycles) of the code since the previous one, so
that virtual time follows the work done. It was deferred (ruling 34) because the comparisons
ignore time and pass, and the card's hold already makes the CPU wait for the card where it
matters; the right place for it is M3, where real-time pacing and the frame rate arrive. What
it costs meanwhile: stall counts, spin counts, the share of time spent in the hold and the
timer values the game reads are not MAME's.

## The Voodoo's write counts

From the 150 s run's trace totals (run), against M0's MAME counts in `hardware-usage.md`
("Dynamic check: Voodoo accesses"; MAME). The two are not fully comparable: M0's run lasted
90 s and counted CPU accesses only, split into those whose pc was inside `GAME.EXE` and those
outside it, while the port counts every write that reaches the card, the GT64010's DMA
included, over 150 s.

- **Equal, register by register**: the triangle set-up of the start-up checks (vertexAx to
  Cy, startS/T/W, dS/T/WdX and dS/T/WdY 260 each; the colour, Z and alpha starts and
  gradients 2 each; triangleCMD 258), clipLeftRight and clipLowYHighY 5, chromaKey 2, stipple
  2, the fog table 2 each, the NCC tables 2 each, the video timing (vRetrace 1, backPorch,
  videoDimensions, hSync and vSync 2 each), fbiInit0 to fbiInit4 (17, 25, 22, 6, 6), dacData
  31, maxRgbDelta 2, trexInit0 5, texBaseAddr_1, _2 and _3_8 2 each.
- **Equal only with the "outside GAME.EXE" writes added**: texture memory 836,287 in the port
  against 436,973 + 399,314 = 836,287 in MAME, and trexInit1 12 against 11 + 1. The port runs
  no boot ROM code, so all of these are the game's own; `hardware-usage.md` inferred that the
  writes outside `GAME.EXE` were the boot ROM's POST program, and these two exact sums say
  instead that they are the game's, attributed to other pcs by MAME's recompiler (inferred;
  `hardware-usage.md` is not changed here, see Open items).
- **Different, mostly carried by DMA** (the port's counts do not separate CPU from DMA
  writes; MAME's `dma0.txt` shows these registers among the DMA's destinations, for example
  45,563 fbzMode, 18,399 textureMode, 4,042 fastfillCMD and 7,159 nopCMD writes in 150 s):
  fbzColorPath 44,553, fogMode 6,949, alphaMode 129,084, fbzMode 53,715, lfbMode 13,363,
  nopCMD 7,614, fastfillCMD 4,074, zaColor 28,984, color0 15,970, color1 24,368, textureMode
  19,156, tLOD 19,153, tDetail 18,863, texBaseAddr 19,154, all through the standard map; and
  through the alternate map 126,984 each of fvertexAx to Cy, fstartS/T/W, fdS/T/WdX,
  fdS/T/WdY and ftriangleCMD.
- **swapbufferCMD**: 3,673, one per scheduler pass (run), by the CPU: MAME's DMA carried none
  (MAME, `dma0.txt`). M0's tap counted 108 inside `GAME.EXE` and 1,916 outside it in 90 s;
  their sum, 2,024, is about one per scheduler pass of those 90 s (the split run counted 2,025
  DMA starts, one per pass), which again points to the game making the "outside" writes
  (inferred). MAME's DMA (`dma0.txt`, 150 s) carried 137,936 ftriangleCMD; the port's count
  is lower because its 150 s cover a different share of attract mode (inferred).
- **Different, not explained**: clutData 4,356 against 33, and fogColor 3 against 2 (Open
  items).

Dropped draws by reason (run): 126,984 triangles for "subpixel adjust", the first missing
feature in pipeline order, and 335,155,200 LFB words for "an LFB write mode other than raw RGB
565 or depth" (the movie frames: 3,273 chains of 256 records of 400 words); no other reason
counted. Because a triangle is counted only under its first missing feature, these counts say
nothing about the features further down the pipeline. Sound FIFO writes 512 and sound data
words 1,230,740 (run), as in MAME.

What they say about M3: every textured triangle of attract mode arrives by DMA through the
alternate map as a float triangle (run; in MAME the CPU wrote only the start-up checks' 258
triangles in 90 s, and the DMA 137,936 float triangles in 150 s), and in the port each fails
at the first check, sub-pixel correction, so M3
must render the float triangle path with sub-pixel correction and whatever the rest of the
pipeline needs, which these counts cannot show (alphaMode's 129,084 writes, fogMode's 6,949
and zaColor's 28,984 suggest blending, fog and depth; inferred), and the movie frames, which
arrive as LFB writes in a mode the model does not draw. The first step of M3 can be a count of
every missing feature per triangle, not only the first.

## Departures from the plan, and every ruling

The controller took 35 rulings labelled as such during the milestone (the ledger's "Ruling"
lines, numbered 1 to 35 below), and 3 more that the ledger records without the label, as
"Carried rulings" of the Task 7 dispatch (C1 to C3, under Task 7). The plan was wrong in
three places (its "plan notes"). Each ruling is one item: what was decided, why, and what it
costs if wrong. They are numbered in ledger order and grouped by task.

The ledger's other unlabelled decisions are already here: the plan's own decisions, which the
owner confirmed on 2026-11-19 ("Decisions in the plan stand"), are the plan's text; the
items Task 1 carried into later dispatches are measured facts (SOUNDSTAT bit 0x8000, the
0x000A reply, the reply rules, the Voodoo as PCI device 8, the synchronous disk hooks, the
order-not-time comparison for Task 9) plus ruling 4; the Task 7 dispatch's other two carried
items repeat ruling 4 and the device-8 fact; and the Task 8 dispatch's carried items repeat
Task 1's facts, ruling 15 (c) and C3. The owner's choice of how to finish M1 (merge locally)
concerns M1, not this milestone.

### Before the tasks (pre-flight scan)

1. **Task 2 may edit `runtime/stub/*`** as far as the contract change needs, since its own
   Step 4 requires the stubs to build. Cost if wrong: none.
2. **Task 3's overlay round trip is compared through the C driver**, with no overlay option
   in the Python tools, to keep one implementation of the overlay format. Cost: the Python
   tools cannot read an overlay until someone needs it.
3. **The Global Constraint wins over `loader-services.md` section 8**, which has unknown ioctl
   commands return -1 and some drivers return 0: commands the game was never seen or read to
   issue stop, because a silent -1 hides exactly the play-only paths the review focus is
   about. Cost if wrong: a stop in play where the board would carry on, fixed by adding the
   command.

### Task 1: the remaining facts from MAME

4. **The DMA completion delay is a function of the chain's size fitted to `dma0.txt`**, not
   the plan's median: a constant would be wrong by two orders of magnitude at one end or the
   other (chains of 28 to 576 records, 0.06 to 7.2 ms). Refined in Task 7 to records and
   frame-buffer bytes, and kept as the completion time in Task 9 (ruling 33). Cost if wrong:
   the timing of the bit-4 handler differs from MAME, visible only in timer-3 timeouts.
5. **"the Linux container" may appear in the analysis documents**: M0 and M1 used it, and it names a
   container, not a host or an address. Cost if wrong: one search and replace before the
   repository goes public.

### Task 3: the disk image and its file system in C

6. **The C reader uses the directory layout the data shows** (a zero first name word is a
   free entry, the next-block link is only at byte 4092, a link of 0 ends), against M0's
   reader, because files the game's own checksum table lists were missing. The task first
   counted 1,359 files and then, on finding a ninth directory block, 1,523. Cost if wrong: a
   mis-listed directory, which the `FILESYS.CHK` comparison would catch.
7. **`disk.py`, its tests and `disk-format.md` are corrected in Task 3**, in a first commit of
   their own, because Task 3's tests compare the C reader with the Python one. Cost: Task 3
   touched M0 files outside its list.
8. **Unlink does what the loader does**: it zeroes the entry's first name word and frees the
   chain. Cost if wrong: an entry other tools would read differently.
9. **`fs_open` with mode 3 returns -1**: `loader-services.md` leaves the mode unspecified and
   the measured opens use modes 0, 1 and 2 only. Cost if wrong: a stop in play.

### Task 4: the clock, interrupts and exceptions

10. **`exc_raise` returns the resume pc**: the brief's `void` signature contradicted its own
    text. Cost: none.
11. **After a break hook or handler returns, a resume pc equal to the epc stops** (it would
    loop for ever), epc + 4 continues, anything else stops naming both pcs, because generated
    code cannot resume at an arbitrary pc from inside `rt_break`. Cost if wrong: a stop where
    the board carries on, which is visible and fixable.
12. **The FP trap's epc is `c->pc`**, exact only in a `--trace-pc` build, and the stop message
    says which; an inexact crash-audit pc is accepted in M2 since the trap ends the program
    either way. Cost: the game's crash record names a stale pc. Left for the final review to
    triage whether the emitter should always store the pc before an FP divide (Open items).
13. **Task 4 may edit `call.c`, `interp.c`, `recomp.h` and the stubs** minimally to route a
    call or jump outside RAM to a new entry, `rt_bad_target`, because the plan requires a
    jump to the reset vector to stop and M1 had left that to M2. Cost: none recorded.
14. **IOASIC INTCTL lives in `machine_t`**: the dispatcher sets bit 14 and the default IP3
    work clears bit 0, and Task 6's ioctls read and write the same word. Cost: none recorded.
15. **Task 4's minors that later tasks build on go into Task 6 as a first, separate commit**:
    (a) `vclock_reload` at the end of `exc_raise` once EXL is clear, documented for devices
    that raise a line; (b) `c->pc` = the resume pc after the restore; (c) the default IP3 work
    lowers IP3, or the IOASIC model derives its level; (d) the IP0, IP1, IP7 and IP4 stops
    name the guest pc; (e) the FP-trap stop says the pc is exact only with `--trace-pc`; (f)
    a false comment in `irq.c`; (g) tests of the IP order with two lines pending and of the
    return-1 absorption. They are cheap and Tasks 6 to 8 depend on (a) and (c). Cost: Task 6's
    diff touches Task 4's files.

### Task 5: host contexts for the game's tasks

16. **Task 5's minors join that same commit**: (h) the synthetic exit stores the predecessor
    in 0x801A3118 before its `j`, so a glue keyed on that word would fail; (i) a scenario that
    resumes a record still holding a parked context at the trampoline (retire then spawn, the
    attract case); (j) an exit on a task context with no map entry stops instead of ending
    silently. They pin the two deliberate behaviours play depends on. Cost: Task 6's first
    commit also touches `tasks.c` and its tests.

### Task 6: loader slots I

17. **The measured no-input values of the input ports become the reset default**
    (0x6900 0xBDFE, 0x6901 to 0x6903 0xFFFF), quoted first in `loader-services.md` and marked
    measured, as the INTSTAT idle value already was; `ioasic_set_inputs` overrides them.
    Without a default a run would stop at the first VBLANK. Cost if wrong: none, they are what
    MAME returned.
18. **A stopped timer holds its elapsed value**, frozen at the stop (the GT64010 counter holds
    its count when disabled and the loader computes (saved − current) × 20; marked inferred),
    because the bit-4 handler stops timer 3 while the DMA start polls it; a never-started
    timer still stops. And **`cmos_load` validates the file before replacing the stored
    path**, so a failed second load cannot make the at-stop save overwrite the user's file.
    Cost if wrong: a DMA wait sees a slightly wrong elapsed value instead of a stop.

### Task 7: loader slots II and the bus

Carried into the Task 7 dispatch (ledger, the Task 7 dispatch line; no "Ruling" label):

- C1. **The disk hooks are called synchronously, inside the slot, and the game's wait routine
  is never called.** Transfers complete inside slot 0x158, and the completion callback (slot
  0x1D8) or the wake hook (slot 0x220) is called through `rt_call` before the slot returns,
  as the plan's disk decision said. Why: MAME measured the hooks' context (`disk-hooks.txt`,
  `loader-services.md` open question 5): the movie callback runs 3,122 times with a0 = 0, and
  each of the 64 waits is followed by one wake, which changes only the sleeping task's record
  +0xC; so a synchronous call is harmless provided the slot never calls wait, which in the
  loader's transfer path (section 4) would put the caller to sleep through the game's
  scheduler from inside a loader call. Cost if wrong: a hook that depends on running at
  interrupt level or after the slot has returned would see other state, and the movie
  streamer or the sound loads would take another path, which the boot comparisons would
  show.
- C2. **Measured device values are quoted into the documents, marked measured, before they
  become constants** (the gun's no-input values and the register at 0x17F00000, into
  `hardware-usage.md`, "Measured values the runtime answers with"). Why: the same rule as
  ruling 17 for Task 6's input defaults, so every constant the runtime answers with cites a
  documented measurement. Cost if wrong: none in behaviour; a wrong value would be wrong in
  the document and the code together, traceable to its fixture.
- C3. **A slot that raises an interrupt line calls `vclock_reload` afterwards**, so the line
  is delivered at the next check point. Why: the delivery-latency finding of Task 4's review
  (a line raised outside a clock event waited up to 1,000,000 checks, 100 ms of virtual time)
  and ruling 15 (a), which documented the rule for devices. Cost if wrong: at most a slice
  reloaded once more than needed.

19. **Task 7 is extended in a second commit** when two Voodoo reads could not be answered from
    the fixtures: (1) the DAC read-back is modelled as the controller recalled MAME's Voodoo
    DAC, confirmed against the game's detect routine (it fitted unchanged: all 9 measured
    reads); (2) the frame-buffer read-back is captured in MAME and the smallest drawing that
    explains it is to be built. A replay of recorded values was rejected, as it would answer
    by rote. Cost: a MAME run and a small renderer piece M3 replaces. Superseded in part: the
    read-back was not fastfill but 258 triangles, and the drawing moved to Task 7b (ruling
    20).
20. **Task 7b is added: a software frame buffer** with the card's LFB addressing and memory
    layout and flat dithered triangles, enough for the start-up checks, since Glide's
    start-up reads back pixels the card rendered (the memory test through LFB aliasing, the
    dither detection) and the game cannot reach attract mode without them. Rejected:
    replaying recorded pixels (rote); replacing Glide's detection with native stand-ins
    (leaves the memory-access level, and M3 needs the frame buffer anyway); weakening the
    exit check. MAME 0.276's BSD-3-Clause Voodoo sources may be fetched read-only into the
    scratch folder and read, with the code written fresh and the source credited. Cost: M2
    grows by a small rasteriser; if the owner prefers another route, Task 7b is a few
    commits to revert. This is the largest departure from the plan, whose premise was
    "Voodoo accepts and discards writes".
21. **The status register's swap and displayed-buffer fields move to Task 7b**, which has
    MAME's model and the frame buffer the swap acts on; the writes behind the measured swaps
    came by DMA and were not captured, so Task 7 could not verify a model. Meanwhile the doc
    says what the 253 reads are, and a status read after any swapbufferCMD write stops. Cost:
    the run stops at the first status read after the first swap until Task 7b, which is the
    order of work anyway. Superseded by Task 7b's model (ruling 24).
22. **Three never-guess minors join the fix round**: configuration writes accept only the
    measured words; an fbiInit write while initEnable bit 0 is clear stops; a DAC register-5
    read before register 7 was written stops. Cost: a stop in a path the game was not seen to
    take.

### Task 7b: the Voodoo frame buffer

23. **A non-flat triangle is counted and dropped, never a stop**, since attract mode draws
    them constantly; to keep never-guess, an LFB read from a buffer that has received a
    dropped draw since a modelled fill stops. Cost: none recorded. Refined by rulings 25
    (what "drawable" means) and 27 (the mark is per buffer).
24. **Task 7b also owns** the alternate register map, fastfillCMD (one precedes the memory
    test), swapbufferCMD with the status register's displayed-buffer, swaps-pending and busy
    fields, and the removal of Task 7's three interim stops (LFB read, status after a swap,
    status after an alternate-map write), all from MAME's model. Cost: the task is larger
    than its plan text, and the swap model is "from MAME's model", checked only by the boot
    run, because the 253 swap reads cannot be replayed.
25. **The texture path the TMU check needs, and "drawn" defined by capability**: a third
    start-up check (2 textured triangles, 36 reads) needs the TMU configuration send, texture
    memory and point sampling. A triangle is drawn only if every feature its mode registers
    enable is one the model implements; otherwise it is counted, dropped and its buffer
    marked. A separate task would only move the same work, and a replay that ends at a stop
    cannot reach attract mode. Cost: a small piece of a real texture pipeline.
26. **The card owns the vertical-retrace clock.** Its period and phase come from the video
    timing registers as MAME's model derives them (no retrace until all four are non-zero),
    and the board's VBLANK interrupt is raised at each retrace start while the board's VBLANK
    enable is set; the fixed `VBLANK_PERIOD_NS` and slot 0x200's start of a clock go. The
    board's VBLANK is the card's vsync, the status bit and the interrupt must share one
    clock, and the measured waits end 12 to 22 µs after retrace onset. Cost: Task 7b touches
    `irq.c`, `machine.h`, `slots.c` and their tests; the frame period changes from 18,467,220
    to 18,476,190 ns. Test-only driver commands to set the clock are allowed.
27. **The dropped mark is per buffer**, as ruling 23 said, for every kind of dropped draw: a
    dropped LFB write marks its write-select buffer and the aux buffer; a mark clears only on
    a modelled whole-buffer overwrite; the per-pixel bitmap goes. It is simpler and cannot be
    wrong about a footprint. Cost: after the first dropped draw into a buffer, any LFB read of
    it stops until a full fastfill; the measured reads all come before such draws.
28. **Two minors join the fix round** because they can hang or miscount: card time going
    backwards (a `machine_reset` without `hw_reset`) stops; a triangle for draw buffer 2 or 3
    is counted as dropped. Cost: none recorded.

### Task 8: the DCS responder

29. **The responder is functional, not timed, and never slower than the board.** Bit 0x40 is
    set from a reply's queueing plus the smallest delay measured for its kind, bit 0x80 always,
    bit 0x8000 whenever the board's state allows it. The replay asserts every result exactly
    and the timing bits one-sidedly ("recorded set implies model set", and 0x40 only while a
    reply is queued), and counts the early reads. The bits follow the sound processor's own
    timing (identical resets answered after 2.0 to 11.2 ms), which no rule over the game's
    words can give, and emulating the ADSP-2115 is M5's work. This replaces the plan's "bit
    0x40 equal at each recorded read". Cost if wrong: the game polls fewer times than on the
    board, its 25 ms timeouts cannot fire, and loader-call counts during the downloads differ
    from MAME's.
30. **The replay may inject the IP3 handler's 512 sound-FIFO writes** at the handler's event,
    since M0 measured exactly 512 FIFO writes by the game and the trace does not record them;
    the runtime's FIFO path feeds the same model; the FIFO is not drained (inferred from the
    single IP3 in 150 s). Cost if wrong: a FIFO that drains on the board fills differently in
    the port.
31. **Commands with measured argument words accept only those words** (0x55AA, 0x55B2,
    0x55AE, 0x55D7, 0x00B9); any other value stops, naming the command, the argument and the
    state, because a different argument may make the board reply. Cost: play stops at the
    first sound command with other arguments until M5.
32. **Three minors join the fix round**: step 11's wrong time for 0x55D3 in `dcs-handshake.md`
    (a sixth disagreement with the trace); the replay pins the early-read counts and the
    data-word count; an INTCTL write enabling anything outside the measured enables stops
    (INTSTAT bit 7 is always set, so enabling it with bit 0 would hold IP3 high for ever).
    Cost: none recorded.

### Task 9: the program and the boot

33. **The card holds its writer, from MAME's model.** A CPU store that would pass a FIFO's mark
    advances the card and then the virtual clock by the wait, through a new clock call that
    runs due events on the way; for DMA the GT64010 channel is held, not the CPU, its
    remaining words go in as the card drains, and the completion interrupt follows. The
    measured completion times stay the standard: MAME's burst model does not reproduce them
    within 1%, so the fit stays. The run had stopped at 0.865 s with the memory FIFO at its
    high-water mark; a scratch experiment with a crude stall passed every comparison, and
    recalibrating the clock alone does not remove the stall. This replaces the plan's
    instantaneous DMA. Cost: the Voodoo model gains FIFO timing, which M3 keeps.
34. **`VCLOCK_NS_PER_CHECK` stays 100 ns in M2.** The pace differs from MAME's (the dispatcher
    starts at 0.857 s against 4.29 s) because a check point is not a fixed number of
    instructions; the fix is an instruction-weighted slice in the emitter, for M3, when
    real-time pacing arrives. Recorded as a known gap; the comparisons ignore time and pass.
    Cost: the CPU runs fast against the card and VBLANK, so stall and spin counts are not
    MAME's.
35. **The trace stays small**: loader-call lines are limited (the first 2,000), with totals at
    the end, so a 150 s trace is about 1.8 MB instead of 556 MB; `--frames` gets a test. Cost:
    loader calls after the first 2,000 are visible only as counts.

### Where the plan was wrong (plan notes)

- **Task 4, `test_no_nesting`**: the brief said the VBLANKs due while a long handler runs are
  delivered after it returns. On the board the default work's VBLANK acknowledge absorbs them
  when the handler returns 1 (`loader-services.md` section 6, read). The implementation
  follows the board; the test uses a handler that returns 0. The reviewer judged the test
  reshaped, not weakened.
- **Task 5, the exit**: the brief said to drop the context of the record at 0x801A3118. The
  game's free makes the exiting task's predecessor current before the switch (read), so the
  word names another task. The implementation drops the running context's entry.
- **Task 6, slot 0x200**: the brief's "line 0 ORs IM3, IM6 and IE" is the state after both of
  the game's registrations; section 1's rule (each line its own IP bit and IE) was built.

### Other departures from the plan's text

- The Voodoo is not a sink: Task 7b, the card's frame buffer, texture unit, FIFOs and holds
  (rulings 19 to 28, 33).
- VBLANK comes from the card's video timing, not from a fixed period started by slot 0x200
  (ruling 26).
- The DMA copies whole words through `mem_r32` and `mem_w32`, not bytes, because the card's
  registers take aligned words and every measured record is word-sized (Task 7).
- A kuseg address on the bus stops, where the plan said to fold it: the loader's TLB maps only
  the RAM range, so the board would take a TLB exception (Task 7).
- The disk library added `img_partition` and `fs_unmount` (Task 3); `tasks.h` added
  `ctx_count` (Task 6); the synthetic guest scheduler of `test_guest_scheduler` is 69
  instructions, not 40 (Task 5).
- The console goes to the host's standard output, as the brief said; `loader-services.md`
  sections 2 and 8 say standard error and were not corrected (Open items).
- `test_first_loader_calls_match_mame` takes MAME's interrupt calls by an sp range of
  0x800C0000 to 0x800C1400, wider than the documented 0x800C1320 to 0x800C1368, and asserts a
  floor of 300 common calls instead of equal lengths (the same 389 either way; Open items).

## What M2 corrected in the earlier analysis

- **The disk directory** (`disk-format.md`, `carnevil_tools/disk.py`, Task 3). M0's reader
  stopped at a deleted directory entry (block 14313, slot 166: first name word zero) and read
  its length as a link past the partition's end. Each directory block holds 170 entries, a
  zero first name word marks a free entry, the link to the next block is the word at byte
  4092, and 0 ends the directory; block 14313 links on to a ninth block. Partition 1 holds
  1,523 files, not 1,356: 167 were missed. What showed it: three files after the deleted
  entry (`GLASS.WMS`, `NOBL1X1.WMS`, `NOBL1X2.WMS`) are listed in `FILESYS.CHK` with matching
  size and checksum; with all 1,523, every one of the 1,504 files `FILESYS.CHK` lists is
  present and matches (static; the rules were then read in the loader's lookup, unlink,
  find-or-create and allocator routines). **The extracted folder on the share,
  `work/part0_files`, predates the fix and lacks the 167 files.** Regenerating it with the
  corrected `extract` only adds files: `GAME.EXE` and `DIAG.EXE`, the only files the tests use
  from it, are unchanged ([getting started](../getting-started.md) says how).
- **`boot-handoff.md`** (Task 1): slot 0x0D8 is `_exit`; GT64010 cause handlers get a0 = 1;
  the loader's partition numbers start at 1.
- **`hardware-usage.md`**: the GT64010 DMA correction to recommendation 5 (Task 1); the
  measured values the runtime answers with (Task 7); PCI offset 0xC0 written 0 twice, not "1,
  later 0" (Task 7); the 253 status reads of 0x0FFFF47F are idle reads with buffer 1
  displayed, not busy or swap-pending (Task 7); the memory taps saw CPU accesses only, and
  attract mode does use the float triangle path, 137,936 ftriangleCMD by DMA through the
  alternate map (Task 7b); 256 flat triangles with 8,192 reads and 2 textured with 36, not
  258 and 8,228 (Task 7b); the gun's offset-0 write is a `sw` (Task 9); the hold of the writer
  and the DMA completion (Task 9); and in this task the hold counts, the release rule and a
  new known gap (below).
- **`loader-services.md`**: the eleven open questions answered in place (Task 1); 2.36
  corrected to 2.54 cycles per byte for the DMA (Task 7); the measured input defaults and
  "a stopped timer holds" (Task 6); slot 0x200 returns line × 4, not 8 (Task 9); and in this
  task a note on the hook order (below).
- **`dcs-handshake.md`** (new in Task 1; corrected in Task 8): six disagreements with its own
  trace, among them a word waiting at the first read, reset replies 2 to 11 ms after the
  release, the stale reads at 12.29 s returning 0xBF10, 0x55AE 0x0100 at 132.459 s, and
  0x00B9 0xFF7F 0x8000 answered by a word the game never read.
- **`M1-report.md`**: dated notes that the scheduler is paced by VBLANK, not by `timer:`, and
  that record +0x10 is the task stack's base (Task 1).
- **In this task** (2026-11-20, dated notes in each):
  - `loader-calls.md`: the Voodoo is found by the third PCI probe, device 8, not the fourth;
    and the SOUNDSTAT count of its run, 1,679,830, is 2 lower than the later dcs-A run's
    1,679,832, a difference between runs, not a constant of the game;
  - `functions.md`: format 3, three host-switch entries, no interpreted function;
  - `hardware-usage.md`: the hold counts name what each counts (187 holds a CPU store
    started; 3.38 s of every CPU wait, including those behind holds the DMA started); the
    PCI FIFO releases with more than twice its low-water mark free, as the code does; and a
    new known gap, a CPU store during a DMA-started hold;
  - `loader-services.md`: sections 1 and 8 disagree on which hook runs when a disk callback
    and a wake hook are both installed; the code follows section 8;
  - `M1-report.md`: its "measured now" covered GCC figures that were not re-measured.

## What M3 (the picture) needs from here

- **The frame buffer and the capability rule, extended.** M3 extends `voodoo_fb.c` and
  `voodoo_tmu.c` rather than replacing a sink: each feature it implements moves from "counted
  and dropped" to "drawn", and the start-up replay (274,967 reads) must keep passing. The
  first features the counts above name are sub-pixel correction on the float triangle path
  and the LFB write mode of the movie frames.
- **The register stream.** Attract mode's drawing arrives almost entirely by DMA: the float
  triangles through the alternate map, the mode registers and the fastfills through the
  standard map, 3,673 chains in 150 s (run). The card model already decodes both maps and
  executes writes in order; M3 renders from the executed state.
- **The frame timing.** The card owns the retrace, 291 rows at 15,750 Hz, 18,476,190 ns a
  frame (from MAME's model, measured agreement); a swap with bit 0 waits for the retrace. M3's
  presentation hooks onto the swap and the retrace, not onto the old fixed VBLANK.
- **The clock's pace.** Real-time pacing needs the virtual clock to follow the board's work:
  an instruction-weighted slice in the emitter (ruling 34), then a mapping of virtual to host
  time.
- **Which thread owns the graphics context.** The game's tasks are host threads, one running
  at a time, and a Voodoo write can come from any of them, or from the GT64010's DMA inside a
  loader call on any of them. OpenGL wants one thread per context. M3 either renders on one
  dedicated thread fed by the card model (a hand-off, and the runtime's first lock besides the
  baton), moves the context at each host switch, or replaces the threads with fibers behind
  `tasks.h` (the plan's decision 2 named the last two).
- **The DMA burst granularity.** A held chain's words reach the card at once up to the hold,
  not 32 words every 1,939.39 ns as in MAME. The words still reach the card in their order, so
  what is drawn should not change (inferred); anything M3 reads back during a chain (a status
  value, an LFB read) may.
- **Real-time pacing** itself, with the window: none of M2 reads the host's clock.

## Open items

Every deferred minor and every warning of the ledger, by area, one line each, with what has
happened to it since where something has. This is the list for the whole-branch review to
triage. Items this report adds are marked "(new)".

### MAME scripts and fixtures

- `inputs.txt` is one line per distinct (source, value), and its 0x17F00000 rows mix reads and
  writes under a "reads" header (Task 1).
- `voodoo-reads.txt`'s pc is MAME's next pc with no correction rule stated (subtract 4, delay
  slot); a load in the delay slot of a branch leaving `GAME.EXE` is dropped by the pc filter
  of `m2facts.lua` (Task 1).
- `dcstap.lua` logs any v0 with low half 0xEEEE as a timeout; its header should note the
  ambiguity (Task 1).
- `PLAY_STEPS` is duplicated between `m2facts.lua` and `tasksw.lua` (Task 1).
- The derivation scripts for `opens.txt`, `tasks.txt` and `loader-first.txt` live on the share
  only (Task 1).

### Analysis documents

- `dcs-handshake.md` step 11 wording ("the five stale words of step 8" are 0xBF10) (Task 1):
  corrected in Task 8.
- `loader-services.md` open question 4 mixes a mean with a median for cycles per spin; one line
  of question 3 is over the wrap width (Task 1).
- `functions.md` stale (format 2, two interpreted entries) (Task 2): corrected in this task.
- Nothing says `FILESYS.CHK` is not maintained on writes (Task 3).
- ⚠️ The loader routine addresses quoted as "read" in `disk-format.md` were not re-derived by
  the reviewer (Task 3).
- The retrace lower bound in `hardware-usage.md` is mis-derived: 251,727 ns is the valid bound,
  the doc still says 251,354 ns; the constant is consistent either way (Task 7).
- `loader-services.md` sections 1 and 8 disagree on which disk hook runs when both are
  installed (Task 7): noted in both places in this task; the loader itself was not re-read.
- The DAC paragraph of `hardware-usage.md` read oddly (Task 7): rewritten in Task 7's second
  fix round.
- `hardware-usage.md`'s "(Task 7b will)" forward reference (Task 7): replaced in Task 7's
  second fix round.
- The README said the ramp's raw run was not kept, the report that it is in scratch on the
  MAME host (Task 7): corrected in Task 7's first fix round.
- "Virtual time 0 = the entry" was documented only in `hw.h` (Task 7b): now in
  `hardware-usage.md` and [runtime](runtime.md).
- The Task 7b report's "6.5675 s" for fbiInit3 bit 0 is wrong; the doc's 6.5527 s is right
  (Task 7b): corrected in the report's fix round.
- "Emulating the sound processor removes these gaps" lacked a mark (Task 8): marked inferred
  in Task 8's fix round.
- `dcs-handshake.md` step 9 still said "4 to 6 ms" outside the Timing section (Task 8):
  corrected in Task 8's fix round.
- `dcs-handshake.md` names the Linux container ("no source in the Linux container"), covered by ruling 5 (Task 8).
- The Task 9 report has a typo in the trace size (Task 9).
- `hardware-usage.md` paired the CPU hold count with a duration of a different set (Task 9):
  corrected in this task.
- `hardware-usage.md`'s release wording said "more than its low-water mark free" where the
  code uses twice the mark (Task 9): corrected in this task.
- `work/part0_files` on the share predates the directory fix and lacks 167 files until
  `extract` is rerun with the corrected tool (Task 3, a follow-up for the owner).
- `loader-services.md` sections 2 and 8 say the console stand-in writes to the host's standard
  error; the code writes standard output, as the Task 6 brief said (new).
- `hardware-usage.md` infers that the Voodoo writes "outside GAME.EXE" in M0's tap were the boot
  ROM's POST program; the port's exact texture-write and trexInit1 totals say they are the
  game's (inferred; new, see "The Voodoo's write counts").

### Recompiler and tools

- `--trace-pc` does not store the pc at a `j` into the stub block, a tail `j` to a function
  start, or `rt_call`/`rt_jump` of `jr` defaults (Task 2). The loader stand-ins name `ra`, as
  that item asked.
- The CLI still lists the two host-switch `j`s as "j out of its function" (Task 2).
- A non-integer `addr` in a manual entry gives an uncaught `TypeError` (Task 2, an M1 pattern).
- `STUB_SLICE` is defined twice (`common.h`, `stub.c`), and the kind mapping twice
  (`functions.HOST_SWITCH_KINDS`, `recomp.SWITCH_KINDS`) (Task 2).
- `_require_ubsan` probes `cc` without the global C flags; `replay.c`'s header reflow left a
  dangling short line (Task 2).
- Ruling 12: whether the emitter should always store the pc before an FP divide, so the trap's
  epc is exact (Task 4).

### The disk library

- `dir_scan` detected a directory loop only after `nblocks` steps (Task 3): fixed in Task 3's
  first fix round (a visited bitmap).
- The overlay is refused as truncated or over-long after a crash between appending a record
  and updating the count (a trailing uncommitted record could be ignored), and there is no
  `fdatasync` between the record and the count (Task 3; touches the user's files).
- A second handle on a file truncated by a mode-1 open, or unlinked, keeps a stale first and
  current block and can write into reallocated blocks (Task 3).
- The C reader keeps an entry with a non-zero first word but a blank name, where `disk.py`
  raises (Task 3).
- `fs_find` reports a wrapped size for a length of 0x40000000 words or more (Task 3).
- The raw-unchanged test compares whole lists with `assertNotEqual` instead of elementwise
  (Task 3).
- The Makefile also adds a header prerequisite line (Task 3).
- The overlay's padding is not validated (Task 3).
- No lock against two processes on one overlay (Task 3).
- `get32`/`put32` are duplicated in `fs.c` and `image.c` (Task 3).
- Every `fs_write` rewrites the directory block (Task 3).
- The name-set count in `test_part0_matches_reference` (Task 3).
- `dir_scan` fails the whole call on a bad link even when the name was found in an earlier
  block (Task 3, a consistent choice).
- Note for Task 7, recorded in Task 3: the chain table is cached at mount and not invalidated
  by `img_write` to its sectors; raw sector writes would stale it. Slot 0x158 only reads.

### Clock, interrupts and exceptions

- `rt_fp_trap` and the core keep the RAM pointer in a global (`cpu_t.rt` could hold it)
  (Task 4).
- A runtime starting with `slice` above 0 loses the first slice's checks silently;
  `carnevil.c` starts at 0 but does not assert it (Task 4).
- `note_entry` logs "entered at BFC00000", and `RT_CHECK` may tick before `rt_bad_target`
  (Task 4).
- Masked-but-pending lines are served per section 6's literal text; check 0x80005100 or MAME
  before any IDE interrupt is modelled (Task 4).
- Remaining test gaps: GT cause order, Status restored from an edited frame, INTCTL bit 14
  clear during handling, the IP3 default, the host rounding mode, a1 on later handlers (Task
  4).

### Task contexts

- Retiring a context that never ran is untested (Task 5).
- Misuse messages are matched only from the start of stderr (Task 5).
- `test_ping_pong` cannot detect two threads at once without the thread sanitizer (Task 5).
- The map's comment should state its bound (records pooled 16 at a time, 6 alive) (Task 5).
- ⚠️ `pthread_exit` from generated frames on glibc, and 1 MB stacks under UBSan (Task 5):
  settled in Task 9 (the Linux container's 150 s run and 20 s sanitizer run).

### Loader stand-ins

- `cmos` reads `errno` after `free`/`malloc` (Task 6).
- `saving` can fall out of step with `machine.at_stop` after a `machine_reset` without
  `loader_reset` (Task 6).
- No `fsync` before the rename in `cmos_save` (Task 6).
- The console read ignores the blocking mode (Task 6, doc-sanctioned).
- Find-first on a hit does not write bytes 0 to 11 and 16 to 19, and writes the upper-cased
  request as the name (Task 6).
- Slot 0x1A8 rewrites the zero words on every call (Task 6).
- The timer tests never exercise the round-down-to-20 branch or just below saturation (Task 6).
- `FL_POLLED` is defined twice (Task 6).
- `dev_file.c` was outside the listed file set (Task 6, justified).
- Header comment lines over the wrap width in `dev_ioasic.c`, `dev_timer.c`, `loader.h`
  (Task 6).
- `cmos_load` leaks its name and file on a stop (the process exits) (Task 6).
- `s_pci_cfg_read` and `s_pci_map` do not check the output pointer's alignment (Task 7).
- Slot 0x158 returns a 32-bit count where section 1 says 16 bits sign-extended (Task 7).
- Slot 0x118 compares n unsigned (the loader's signedness is undocumented), and the stored
  selection is unused by the file system (Task 7).
- Stale docstrings in `test_voodoo_registers` and `test_voodoo_reads_like_mame` (Task 7):
  fixed in Task 7's first fix round.

### The Voodoo

- ⚠️ fbiInit0 and fbiInit1 reads at 7.93 to 9.29 s are "storage" by assumption, with no
  writes captured after 7.0 s (Task 7). The boot run did not stop on them, but their values
  were not compared with MAME's.
- The swap and displayed-buffer model is unverified by measurement (the capture has no swap)
  (Task 7b).
- The checks that a DMA record's swap and alternate-map write reach the card, and the
  cross-check against `voodoo-reads.txt`, were dropped with the old tests (Task 7b): the
  DMA-record checks were restored in Task 7b's fix round; the cross-check was not.
- `test_reads_like_mame_full` does not assert the split between status and register reads
  (Task 7b).
- `tests/voodoo_timing.py` is a transliteration of the rule, not an independent check
  (Task 7b).
- The 2 × 2 dither is untested (Task 7b).
- `tmu_texel` calls `layout()` per pixel (Task 7b).
- `voodoo_null.c` is large (784 lines then, 881 now); the video timing is a clean split
  candidate (Task 7b).
- ⚠️ How MAME's GT64010 delivers a DMA chain was unchecked, since `gt64xxx.cpp` was not fetched
  (Task 7b): fetched and read in Task 9; its burst model does not reproduce the measured
  completions (above).
- The hSync half of the zero check is unreachable (Task 7b).
- Any fbiInit1 or fbiInit2 write flags every buffer while a flag is set, even if the layout is
  unchanged (Task 7b).
- A stale line in `test_textured_triangle`'s docstring (Task 7b).
- Untested: the hold from the PCI FIFO's low-water mark, and the release with the memory FIFO
  off (Task 9).
- A CPU store during a DMA-started hold is queued then held, where MAME saves and re-issues it
  (Task 9): recorded in "Known gaps in the card model" in this task.
- clutData: 4,356 writes in the port's 150 s against 33 in M0's 90 s CPU tap, with none by
  MAME's DMA; fogColor 3 against 2. Not explained (new).

### The sound responder

- The "0x40 only while queued" count holds by construction (Task 8).
- The `irq.c` comment on when IP3 rises again was incomplete (Task 8): rewritten in Task 8's
  fix round.
- The appear-event path is not exercised by a test; the replay sets the clock directly
  (Task 8).
- ⚠️ `dcs_irq_update(NULL)` from a clock event skips `vclock_reload`; reachable only when
  INTCTL enables INTSTAT 0x40, which the trace never does and the INTCTL stop now refuses
  (Task 8).
- `INTCTL_MEASURED` is a `#define` while the measured list lives in a comment and a message,
  which could drift (Task 8).
- The allowed INTCTL set includes subsets never written (0x4001, 0x0001, 0x0008, 0x0000)
  (Task 8).

### The program, the trace and the boot test

- `test_first_loader_calls_match_mame` uses the sp range 0x800C0000 to 0x800C1400 instead of
  the documented 0x800C1320 to 0x800C1368, and asserts an unruled floor (more than 300 common
  calls) without asserting equal lengths; the result is the same 389 either way (Task 9).
- The port's interrupt-time loader calls run with sp 0x800C13A8 to 0x800C13D0, MAME's with
  0x800C1320 to 0x800C1368: the dispatcher's stack depth differs from the loader's, which the
  game does not see in this run (Task 9).
- The `tolerated` half of `test_nothing_unexplained` cannot fail (nothing produces a tolerated
  request), and `trace.c`'s format comment omits `tolerated` (Task 9).
- `trace_open` comes before the overlay's `img_open`, so an overlay error leaves an empty trace
  (Task 9).
- `trace.c`'s loader-call stack is one global stack, which would be wrong if a slot yielded to
  a task inside a slot (Task 9).
- ⚠️ The the Linux container evidence was not re-run by the reviewer; it is supported by the identical trace
  SHA-256 (Task 9).
- `make run` without `OVERLAY` and `NVRAM` puts both files next to the default image, which
  for the default `DISK` is in the work folder on the share; the guide says to set them (new).
- The opens comparison is a prefix with a floor of 180, so the test alone would accept fewer
  than 193 opens (new; the run had 193 of 193).
