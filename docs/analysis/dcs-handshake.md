# The DCS handshake, measured

What the game says to the DCS sound board in attract mode and what it expects back, for the
M2 runtime's stand-in (`loader-services.md`, section 5 and open question 3). The game reaches
the board only through ioctls on fd 4 (`ioasic:`: SOUNDCTL 0x6906/0x6907, SOUNDOUT 0x6908,
SOUNDSTAT 0x6909, SOUNDIN 0x690A/0x6911) and the sound FIFO at 0x13000000. This page is about
the ioctl exchange; it quotes command and reply words, which are protocol values, and no sound
data.

Each statement is marked **measured** (MAME), **read** (disassembly of GAME.EXE or the loader)
or **inferred**. MAME's DCS source (`dcs.cpp`) was not read: the Linux container has the Debian MAME package
and no MAME source tree, and nothing was downloaded. In MAME the DCS board runs its own ADSP-2115
program, so every reply below is what that program sent; a reply called a rule is one that a
single formula predicts for every instance in the trace, which is inferred from the trace, not
read from code.

## Evidence

- MAME 0.276 in the Linux container, `carnevil` set and CHD, blank NVRAM, no input, `-nodrc` with the
  debugger on, `tools/mame/dcstap.lua` (command lines and line formats in
  `tools/mame/README.md`). Run dcs-A: 150 emulated seconds from GAME.EXE's entry (5.004 s
  after power-on) to 155.018 s, as run A of `task-switch.md`.
- Breakpoints on the loader's `ioasic:` read path (0x80006CC8, just after the load) and write
  path (0x80006CDC) log every access the ioctls make; the routine 0x80006BE0 has one of each
  for all its commands (read).
- The counts agree with `loader-calls.md` (measured): SOUNDOUT 1,230,931 writes, SOUNDIN 112
  reads and 60 acknowledgements, SOUNDCTL 10 writes and 10 reads; SOUNDSTAT 1,679,832 reads,
  two more than the 1,679,830 ioctls of the loader-calls run (not explained; every other count
  is identical).
- The file `mame/m2-facts/dcs-trace.txt` on the share holds every event in order with its
  emulated time; this page summarises it.

## The status bits (measured)

- SOUNDSTAT bit 0x80 is set when the board can take a word; the game's write routines poll it
  before every SOUNDOUT write (read: 0x8016A510 and the others of section 5). It was clear for
  a few polls after most writes and never stayed clear long enough to time out.
- SOUNDSTAT bit 0x40 is set when a word from the board waits in SOUNDIN; the game's read
  routines poll it (read: 0x8016A42C, 0x8016A930). After the game reads SOUNDIN it writes
  SOUNDIN with 0 (ioctl 0x6911, argument 0: the acknowledgement); bit 0x40 is clear at the next
  poll.
- One write routine, 0x8016A6E8, waits until both 0x8000 and 0x0080 are set (it masks with
  0x8080 and compares with 0x8080; read); the others test 0x80 or 0x40 alone (read). Bit
  0x8000 was set in 1,119,145 of the 1,679,832 SOUNDSTAT reads; the commonest values were
  0x83B0 (856,370 reads), 0x03B0 (412,433) and 0x8388 (246,461) (measured).
- The other bits seen vary (0x0008, 0x0010, 0x0020, 0x0100, 0x0200; 19 distinct values from
  0x0088 to 0x83B0; measured). No game code tests them (read: the masks above are the only
  ones applied to a SOUNDSTAT result in 0x8016A42C to 0x8016AAA8). So a stand-in that returns
  0x8080, with 0x40 added while a reply waits, satisfies every test the game makes (inferred).
- Bits 0x0038 follow the sound FIFO's fill (measured): 0x0008 at every one of the 411,004
  reads before the IP3 handler's FIFO writes (12.790 s), 0x0030 at every one of the 1,268,828
  after them. `hardware-usage.md` measured 512 such writes, all by the game's streamer; read
  as 0x0008 empty, 0x0010 half full and 0x0020 full (the names are inferred; that 0x0010 starts
  at half of 512 words is inferred, since no fill between 0 and 512 was observed).
- Bits 0x0100 and 0x0200 change when the board posts certain replies, at the board's times
  (measured): 0x0100 comes with the 0x000C reply (first seen at trace lines 61,580 and 452,676)
  and goes with a reset and with the reply to 0x00B9 (line 2,910,972); 0x0200 comes with the
  first two block sums of each sound load (lines 77,977, 84,662, 469,073, 474,821) and goes
  with the 0x0223 reply (lines 83,981, 399,699, 474,140). Not modelled: these bits are returned
  as the idle pattern (clear); the game is not known to test them (read: the routines of
  `loader-services.md` section 5).

## The exchange in attract mode (measured)

In order, with emulated seconds since power-on. Words are hexadecimal 16-bit values; "block"
means a command word followed by a header and data words, sent back to back.

1. 9.290 s: INTCTL read 0x4000, written 0x4000 (fd 4, 0x6910/0x690F).
2. 10.137 s: SOUNDOUT 0x55AA, 0xC837. No reply. (Correction, 2026-11-20: a word of the
   board's was already waiting before this, from before the game's entry: the first SOUNDSTAT
   read, trace line 4, is 0x00C8 with bit 0x40, and INTSTAT at the entry is 0x20C9 with its
   0x0040. The game never read it; the reset of step 3 dropped it, line 14 reads 0x0088.)
3. Reset: SOUNDCTL read 0x0001, written 0x0000, read 0x0000, written 0x0001, each about 1 ms
   apart; then INTCTL read and written 0x4000. **Reply 0x000A** about 4 ms later. (Correction,
   2026-11-20: 2.03 to 11.21 ms after the SOUNDCTL 0x0001 write, not about 4 ms: 5.152 ms for
   this reset (write at trace line 11, first seen at line 4,029), 11.208 ms for the second
   (lines 4,035 and 13,913), 2.028 ms and 4.491 ms at the second sound load (lines 399,705 and
   400,700; 400,706 and 404,084), for the same writes at the same spacing each time.)
4. A second reset, the same; **reply 0x000A** again.
5. Four boot-load blocks, each `0x001A, start, end, space` followed by the data: (end − start
   + 1) words when space is 1, twice that when space is 0 (blocks of 1,028, 9,732, 12,292 and
   260 words with their headers). **After each block: reply = the 16-bit sum of its data
   words**, then **reply 0x000A**.
6. SOUNDOUT 0x002A. **Reply 0x000C** about 1 ms later.
7. A block `0x55D0, start high, start low, end high, end low` followed by (end − start + 1)
   data words (start 0, end 0x1FFF: 8,197 words). **Reply = the 16-bit sum of the data
   words** (0xE000).
8. SOUNDOUT 0x55D3. No reply: the game reads SOUNDIN five times without polling SOUNDSTAT
   (bit 0x40 was clear) and acknowledges once; the five reads return the last reply again
   (0xE000). This is the `main_init_sound: (information) requesting version number` step on
   the console (10.238 s).
9. SOUNDOUT 0x55D4. **Reply 0x0223** 4 to 6 ms later (the same in both sound loads).
   (Correction, 2026-11-20: 4.29 to 6.20 ms, as in "Timing".)
10. Then, three times: SOUNDOUT 0x55D2, 0x55D3 (no reply; five reads of the stale SOUNDIN
    and one acknowledgement, as in step 8), a short 0x55D0 block (13 to 333 words), **reply =
    its data sum**, a long 0x55D0 block (21,924 to 69,828 words, `main_init_sound(): Loading
    banks...`), **reply = its data sum**.
11. 10.641 s: SOUNDOUT 0x55AA, 0xC837, 0x55B2, 0xBB44, 0x55D3; nothing more until the task
    `loadwave` starts at 12.29 s, where the game reads the five stale words of step 8 and
    acknowledges, sends 0x55D4 and gets **0x0223**. (Correction, 2026-11-20: the five reads
    return 0xBF10, trace lines 395,536 to 395,540, the last reply before them (the sum of the
    last 0x55D0 block, line 395,520), as the stale-SOUNDIN rule says; not step 8's 0xE000.
    Second correction, 2026-11-20: at 10.641 s only 0x55AA, 0xC837, 0x55B2, 0xBB44 were sent,
    lines 395,523 to 395,533; the 0x55D3 came at 12.2907 s, line 395,535, just before the five
    stale reads.)
12. 12.295 s to 12.790 s: steps 3 to 10 again, word for word, with the same replies
    (the second `main_init_sound` block on the console).
13. 12.790 s: SOUNDOUT 0x55AA, 0xC837, 0x55B2, 0xBB44, 0x55AE, 0x0100 (no reply); SOUNDCTL
    read 0x0001 and written 0x0001, then 0x000F; INTCTL written 0x4008, then 0x4009 (the
    IOASIC interrupt enabled, see below); INTSTAT read twice (0x2089, 0x2081).
14. 12.790 s to 19.775 s: SOUNDOUT 0x55D7, 0x0000, 0xFF00, 0x0000 (no reply), then four
    rounds of 0x55D2, 0x55D3 (stale reads as in step 8), a short `0x55D1` block (13 words with
    its header the first time, 9 the other three) and a long `0x55D1` block (22,288 to 560,227
    words), **each block answered by its data sum**.
15. 22.600 s: SOUNDOUT 0x00B9, 0xFF7F, 0x8000, 0x55AE, 0x0100. No reply. Nothing more was
    sent or read on the DCS ioctls up to 155 s. (Correction, 2026-11-20: 0x00B9, 0xFF7F and
    0x8000 were written at 22.5997 s (trace lines 2,910,965 to 2,910,971), but 0x55AE and 0x0100
    at 132.459 s (lines 2,910,973 and 2,910,975). And the board did reply to 0x00B9 0xFF7F
    0x8000: the next SOUNDSTAT read, at 132.459 s (line 2,910,972, 0x82F0), has bit 0x40, a word
    waiting, which the game never read, so its value and when in those 110 s it came are
    unknown.)

The replies the game reads are therefore, per occasion:

- after a reset: 0x000A (constant);
- after each `0x001A` block: its data sum, then 0x000A;
- after 0x002A: 0x000C (constant);
- after 0x55D4: 0x0223 (constant);
- after each `0x55D0` or `0x55D1` block: its data sum;
- after every other command word: nothing.

## Which replies are rules and which are recorded values

- **Rules** (inferred from the trace; every instance fits): the block checksums. For all 4
  `0x001A` blocks of each sound load (8 in the run; correction, 2026-11-20: the count was per
  load) and all 22 `0x55D0`/`0x55D1` blocks of the run, the reply is the sum of the
  data words (the words after the 4- or 5-word header) modulo 0x10000. The block lengths follow
  from the headers as given in steps 5 and 7, so a stand-in can compute the reply from the
  words it receives without knowing their meaning.
- **Rules** (inferred from the trace): SOUNDIN keeps the last word the board sent, so a read
  with bit 0x40 clear returns it again (the five reads after each 0x55D3).
- **Recorded values** (constants seen, no rule known): 0x000A after a reset and after a boot
  block, 0x000C after 0x002A, 0x0223 after 0x55D4. They were the same at every occurrence in
  the run (two resets per sound load, two sound loads), but what makes the board send them, and
  whether another board or ROM revision sends other values, was not established. 0x0223 is
  plausibly a firmware version (the console says "requesting version number" just before;
  inferred).
- The words the game sends without a reply (0x55AA 0xC837, 0x55B2 0xBB44, 0x55AE 0x0100,
  0x55D2, 0x55D3, 0x55D7 and its three words) are recorded, not interpreted. (Correction,
  2026-11-20: 0x00B9 0xFF7F 0x8000 was answered by a word the game never read; step 15.)
- Each of the commands with arguments that are not a download was sent with one argument
  pattern only (measured, the trace recounted on 2026-11-20): 0x55AA 0xC837 3 times, 0x55B2
  0xBB44 twice, 0x55AE 0x0100 twice, 0x55D7 0x0000 0xFF00 0x0000 once, 0x00B9 0xFF7F 0x8000
  once. The commands without arguments: 0x002A twice, 0x55D2 10 times, 0x55D3 13 times, 0x55D4
  3 times. The downloads: 0x001A 8 times (4 with space 0, 4 with space 1), 0x55D0 14 times,
  0x55D1 8 times. SOUNDCTL was written 0x0000 4 times, 0x0001 5 times and 0x000F once; INTCTL
  0x4000 6 times, 0x4008 and 0x4009 once each. The responder takes these argument patterns and
  values only, and stops on any other, since what the board does with another was not seen;
  the downloads' addresses and lengths are free, because their reply is computed.

## Timing (measured)

- The board raised bit 0x40 within 0.001 to 0.009 ms of the last word of a block, 1 to 2 ms
  after 0x002A, 4 to 6 ms after 0x55D4, and 2 to 4 ms after the end of a reset. Each is well
  inside the game's 25 ms read timeout (`loader-services.md`, section 5). (Correction,
  2026-11-20: after a reset it was 2.03 to 11.21 ms from the releasing SOUNDCTL write, step 3;
  after 0x55D4 4.29 to 6.20 ms, trace lines 77,989 to 83,981, 395,543 to 399,699 and 469,085
  to 474,140. A block's sum was at the first poll after its last word in all 30 blocks.)
- Bit 0x80 was clear at 45 of the reads, one to three polls (1.1 to 3.3 us) after a command
  word, never after a block's data word. The same word at the same delay differs by the
  board's state: 0x55AA read with 0x80 set 1.2 us after its write at line 4 (before any
  download) and clear at line 395,524 (program running).
- Bit 0x8000 is never set before line 61,279 (10.2197 s, 0.74 ms after the first 0x002A, line
  60,565), and from there on it is clear for about 1 ms every 7 to 8 ms, on a 1 ms lattice whose
  phase shifts after longer gaps (for example lines 224,321 to 224,362): 831 transitions, none
  explained by a word the game sent. It is clear between each reset and the next 0x002A.
- No DCS routine returned 0xEEEE in the whole run (breakpoints on the `jr ra` of the seven
  routines that can, conditioned on v0).

## The IOASIC interrupt (measured)

- Run A has one exception with Cause IP3, at 12.790 s (frame 711), with EPC 0x80006CE4: the
  instruction after the loader's INTCTL store, so the interrupt came as the game wrote INTCTL
  0x4009. At the vector INTSTAT was 0x2089 and INTCTL 0x4009. The game's IP3 handler 0x8013F678
  ran once (a0 = 0x800, the IP3 bit). Its two INTSTAT reads gave 0x2089 and then 0x2081.
- So the cause is INTSTAT bit 0x0008, pending before the write and enabled by INTCTL bit 3
  (with bit 0, set in the same write), and cleared during the handler (measured); what bit 3
  stands for is inferred: the handler is the sound streamer's entry, which fills the sound FIFO
  (`loader-services.md`, section 5), so bit 0x0008 is most likely the FIFO's "needs data"
  status. No IP3 came again in the 142 s that followed, with INTCTL left at 0x4009.

## Without replies: SOUNDSTAT bit 0x40 forced clear (measured)

Run dcs-F is run dcs-A with bit 0x40 of every SOUNDSTAT result cleared before the loader
stores it (`CARNEVIL_DCS_FORCE_CLEAR`), so the game never sees a reply; the board itself ran
as before. Run dcs-F2 repeats it with the task creations logged and a snapshot every 10 s
(`CARNEVIL_DCS_SNAP`).

- Each DCS reset now waits for its 0x000A in vain. The reset routine (ending at 0x8016A424)
  reads one word through 0x8016A42C and returns 0 only if it is 0x000A, else 0xEEEE (read: an
  `xori 0xA` and a `movz`). So **0x000A after a reset is required**, not just recorded.
- 0xEEEE returns, 13 in all (measured): 0x8016A42C timed out 8 times for the reset routine
  (return address 0x8016A408; four resets per sound load, two sound loads) and once for the
  version query 0x8016B990 (return address 0x8016B9C0; it returns the reply to 0x55D4, or
  0xEEEE; read); the write routine 0x8016A6E8 timed out 4 times for the block sender
  (return address 0x8016C1DC), which then abandons the block (read: it branches to its exit
  with 0xEEEE).
- The 0x8016A6E8 timeouts are not caused by the forcing directly: that routine waits for
  SOUNDSTAT 0x8080, and **bit 0x8000 never came in this run** (the SOUNDSTAT values were
  0x0088, 0x00B0 and 0x0030 only). In run A it first came at 10.2197 s, just after the boot
  load and the 0x002A command, and from then on it was set most of the time, clear for about
  1 ms every 7 to 8 ms (measured). Bit 0x8000 therefore appears to come from the board's
  downloaded program once it runs (inferred).
- The console shows what the game makes of it: after each failed reset
  `main_init_sound(): ERROR initializing sound system.` and `retrying the motherfucker` (the
  game's own words), three times, then `fuck this shit... Aborting` (10.284 s); the same at
  the second sound load (12.012 s to 12.129 s). The `Loading banks...` and `done` lines of run
  A are missing, and `NSS reset = e` comes earlier (10.779 s against 11.126 s).
- No boot-load or bank block was sent (40 SOUNDOUT writes in the whole run, against
  1,230,931). From 12.13 s the game sent four `0x55D1` block headers (start 2, end 5 or 9)
  without their data, then `0x55AE 0x0100`, and nothing more on the DCS ioctls after 13.07 s.
- Where the 0xEEEE results end up (read): each is returned up the call chain to the sound
  initialisation, which prints the messages above. No store of the result into a global flag
  was found on that path in the reads made for this page, so whether the game keeps a "sound
  dead" flag was not established.
- **Attract mode still runs** (measured, dcs-F2): the same seven tasks as in run A were created
  (`main`, `loadfram`, `utime`, `volume`, `ccproc`, `attract` at frame 661 against 679 in run
  A, `loadwave` at 675 against 711), and 13 of the 14 snapshots from 20 s to 150 s after the
  entry show attract-mode pictures with `INSERT 2 COINS`; the one at 120 s is black (between
  two scenes, inferred). Runs dcs-F and dcs-F2 gave identical DCS, console and interrupt lines.

The IP3 of run A happened here too (12.129 s, as the game wrote INTCTL 0x4009; INTSTAT 0x20C9
then 0x20C1; one handler call): the IOASIC interrupt does not depend on the DCS replies.

**For a DCS-less runtime** (inferred from the two runs): answering nothing costs 13 timeouts
of 25 ms, prints the error messages and skips the sound download, and the game runs on
without sound. Answering by the rules and recorded values above (0x000A after a reset and
after each boot block, the block sums, 0x000C after 0x002A, 0x0223 after 0x55D4, SOUNDSTAT
0x8080 with 0x40 while a reply waits) reproduces the exchange of run A.

## Known gaps in the responder

The runtime's responder, `runtime/hw/dcs_stub.c`, answers by the rules and recorded values
above with a state machine over the words the game sends; `tests/test_dcs.py` replays this
page's trace through it. Every SOUNDIN, SOUNDCTL, INTSTAT and INTCTL result of the trace comes
out as recorded, and so do SOUNDSTAT's FIFO bits, with the IP3 handler's 512 FIFO writes fed
after its first INTSTAT read (measured, the replay). What it does not reproduce:

- **The timing bits are not the board's.** When the board takes a word and when its reply
  appears are decided by its own processor, which the runtime does not emulate. The responder
  is never slower than the board instead: a reply appears after the shortest delay the trace
  shows for its kind (2.027 ms after a reset, 1.047 ms after 0x002A, 4.293 ms after 0x55D4,
  none after a block), bit 0x80 is always set, and bit 0x8000 is set throughout the program
  state, from the 0x002A write to the next reset. In the replay every SOUNDSTAT read the board
  answered with one of these bits set is answered with it set too, and the model shows the bit
  where the board did not at 17,978 reads for 0x40, 45 for 0x80 and 495,782 for 0x8000
  (measured). Bits 0x0100 and 0x0200 are left clear (not modelled, see "The status bits"):
  1,611,974 reads differ in them, none tested by the game (read).
- **What would show it** (inferred): the game polls SOUNDSTAT fewer times than in MAME, so the
  counts of SOUNDSTAT reads and of loader calls during the downloads differ from
  `loader-calls.md`'s, and every wait ends sooner, so the download and the sound loads finish
  earlier in emulated time than in run A. A game path that depends on a wait taking long (a
  timeout that is meant to fire, or a poll count used as a delay) would behave differently;
  none is known.
- **The FIFO is never drained** (inferred from the one IP3 in 150 s): once full it stays full,
  and a word written to a full FIFO stops. Play, where the streamer runs on, needs the board's
  consumption of the FIFO.
- Emulating the sound processor (its boot ROM and the downloaded program) removes these gaps
  (inferred).

## Not done

- Play: the DCS exchange during a game (sound commands for effects and music) was not traced;
  attract mode sends nothing on these ioctls between 22.6 s and 132.459 s, and nothing after
  132.459 s (correction, 2026-11-20: the text said nothing after 22.6 s; see step 15).
- The sound FIFO's contents and its interrupt timing beyond the one IP3 above.
- MAME's `dcs.cpp` was not read (no source in the Linux container).
