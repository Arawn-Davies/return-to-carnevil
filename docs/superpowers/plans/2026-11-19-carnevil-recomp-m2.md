# CarnEvil M2: Runtime to the Attract Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A native program, `carnevil`, that loads `GAME.EXE` from the user's disk image, runs the recompiled game with no window and no sound, and reaches and stays in attract mode, with its console log, file activity and task activity matching MAME's.

**Architecture:** The recompiled C of M1 links against a real runtime in `runtime/`. Every call the game makes into the boot ROM's loader is answered by a native stand-in (`docs/analysis/loader-services.md`); the hardware the game touches itself (Voodoo, gun, sound FIFO, CMOS) is answered at the bus. Time is a deterministic virtual clock advanced by the generated code's check points, and interrupts are delivered at those points with the loader's calling convention. The game's cooperative tasks each run on their own host context.

**Tech Stack:** C11 with POSIX threads, the host's `cc` and `make` (Apple clang on the Mac, GCC 14 in the Linux container); Python 3.12+ stdlib for the tools and the tests (`unittest`); MAME 0.276 in the Linux container as the oracle.

**Spec:** `docs/superpowers/specs/2026-11-19-carnevil-recomp-design.md` (the runtime sections, and all "Corrections" sections at its end, including "Corrections from M2 planning"). Findings this plan builds on: `docs/analysis/M1-report.md` ("What M2 needs from here", "Risks and decisions for M2"), `docs/analysis/loader-services.md`, `docs/analysis/task-switch.md`, `docs/analysis/loader-calls.md`, `docs/analysis/hardware-usage.md`, `docs/analysis/boot-handoff.md`, `docs/analysis/disk-format.md`.

## Decisions this plan takes

Each is recorded in the spec's "Corrections from M2 planning". Each can be overruled before Task 2 starts at little cost.

- **Task switch: the yield, the exit and the scheduler stay recompiled; only the three guest transfers between them become host switches.** These are the `j 0x80151E90` at 0x80151B48 (yield) and at 0x80151B78 (exit), and the `jr ra` at 0x80151E88 (resume). Nothing hand-written reproduces guest effects. This is the M1 reviewer's alternative.
- **A host context is a POSIX thread, and only the thread holding the baton runs.** MAME measured at most 6 tasks alive and about 100 switches a second, so the cost of a thread hand-over (microseconds) does not matter, and threads need no assembly, no deprecated `ucontext` and no sanitizer annotations on any of the three targets. The interface (`runtime/core/tasks.h`) is small enough to put fibers behind later. Known cost: in M3, OpenGL calls would come from several threads; M3 either moves the GL context at each switch or replaces this file with fibers.
- **Time is virtual and deterministic.** `RT_CHECK` counts down a slice; each check point is worth `VCLOCK_NS_PER_CHECK` = 100 ns of emulated time. VBLANK, the GT64010 timers and DMA completion are events on that clock. Nothing reads the host clock in M2, so two runs give the same log. Real-time pacing arrives with the window in M3.
- **Disk transfers complete inside the call**, and the completion callback or wake hook is called before the slot returns, as the loader does when a transfer finishes early. Task 1 checks in MAME that the hooks tolerate this.
- **Disk writes go to an overlay file.** The user's `carnevil.raw` is opened read-only and never modified.
- **A jump to the reset vector 0xBFC00000, `_exit`, `exec` and the assert `break 0x407` stop the program** with a message and a non-zero status. M2 does not restart the game.
- **An FP divide by zero follows the board:** the game's catch-all handler runs, records its crash audit and jumps to the reset vector, which stops the program as above. The FPU handlers of slot 0x1E8 are stored but, as on the board, unreachable once the catch-all is installed.
- **Sound without a DCS:** a small responder returns the replies MAME's DCS gave in attract mode (Task 8), so the game takes the same path as on the board. Nothing is played.
- **Not built, against the outline in the master plan:** IDE, GT64010 PCI and timers as registers, the UART and the security PIC. The game never touches them; the loader did, and the stand-ins replace the loader (M0 decision 1). The GT64010 is modelled only as far as the `pci:` ioctls reach: a register file and DMA channel 0.

## Global Constraints

- No game data, extracted files, ROMs, RAM dumps, MAME captures or generated C in the git repo, ever. Generated C goes to `$CARNEVIL_WORK/generated`, objects and binaries to `$CARNEVIL_WORK/build/<uname -s>-<uname -m>` or a temp dir, MAME outputs to `$CARNEVIL_WORK/mame/`. `CARNEVIL_WORK` defaults to `/Volumes/archive/reincarnevilation/work` (the Linux container: `/mnt/reincarnevilation/work`). Notes and config may hold addresses, counts and short strings, not code listings.
- BYOCHD: the program takes the user's own `carnevil.raw` and opens it read-only. Implementers never `git push`; no Claude attribution in commits.
- Nothing is installed on the Mac or in the Linux container. Python is stdlib only. C uses the C library and pthreads only. MAME runs only in the Linux container (`tools/mame/README.md` says how).
- Never run `head` or `tail`. Never print secrets. `rm -rf` and `curl` are blocked; delete nothing you did not create.
- One supported revision: `GAME.EXE` SHA-256 `f9455ce801d2e5c26a7b6e00eff88efa573deb7ab15fa29f9c90100623bfd3a6`.
- Tests that need game data use `tests/gamedata.py` and skip when the file is missing. Tests that need a C compiler skip, naming `cc`. The suite must pass with `python3 -W error -m unittest`, with the share mounted and with `CARNEVIL_WORK=/nonexistent`.
- C flags for every C file: `-std=c11 -O2 -ffp-contract=off -frounding-math -fno-strict-aliasing -fwrapv`, never `-ffast-math`. Runtime C also builds with `-Wall -Wextra -Werror`; generated C with `-Wall -Werror -Wno-unused-label -Wno-unused-variable -Wno-unused-but-set-variable`. Test binaries are also built with `-fsanitize=undefined -fno-sanitize-recover=undefined`.
- Three-way parity from M1 holds: whatever the emitter does at an address, the interpreter does too (the host switches, the slice count, the divide seam).
- The runtime never answers a request it does not model by guessing. An unknown slot, ioctl command, bus address or `break` code stops the program with a message naming the guest pc (or the slot's return address), the request and its arguments. The one exception is a list in `config/runtime.expected.toml`, each entry with a reason.
- Every statement in a doc is marked measured (MAME), read (disassembly) or inferred. Docs are human-readable, in complete sentences, with no markdown tables and no internal host names or IP addresses under `docs/analysis` and `docs/*.md`.

## Review Focus

1. **A request the runtime does not model** (a slot never called in attract mode, an ioctl command or Voodoo register that only play uses): the program stops and names it; it never returns 0 and carries on. Tests in Tasks 6 and 7.
2. **The user's files:** `carnevil.raw` missing, truncated, of another revision, or unreadable; the overlay or the NVRAM file missing (first run), truncated, or from another image. Each gives one clear message; the raw image is never opened for writing; a first run creates the overlay and a blank NVRAM. Tests in Tasks 3 and 9.
3. **Interrupts at awkward moments:** one becoming due while a handler runs, a loader service called from a handler, a task created inside the VBLANK handler (measured in play), an interrupt due at a host switch. None is lost, none nests, none runs on a task being switched. Tests in Tasks 4 and 5.
4. **Tasks that end unusually:** a task killed while parked (MAME: `attract` is freed by `start`), a record reused for a new task, the program stopping while tasks are parked. No hang at exit, no resumed dead context. Tests in Task 5.
5. **Time limits the game enforces on itself:** the VBLANK wait reboots after 100,000,000 spins; a one-shot timer must read exactly 0x13FFFFEC once run out; DCS waits time out at 25 ms. The virtual clock must make each of these behave as on the board, including over a run long enough to wrap 32-bit nanosecond values. Tests in Tasks 4 and 6.

---

## File structure

- `runtime/include/recomp.h` (modify): the slice counter, `rt_tick`, `rt_host_switch`, `rt_fp_trap`.
- `runtime/include/machine.h`: what the runtime's own files share (the `machine_t` singleton: clock, Status and Cause model, handler tables, devices, options).
- `runtime/core/clock.c`: virtual clock and event queue. `runtime/core/irq.c`: interrupt and exception dispatch with the loader's frame. `runtime/core/exc.c`: `rt_break`, the FP trap, stopping. `runtime/core/tasks.c`, `tasks.h`: host contexts. `runtime/core/bus.c`: `bus_read`, `bus_write` by physical address.
- `runtime/disk/image.c`, `fs.c`, `disk.h`: the raw image with its overlay, and the file system on partition 1.
- `runtime/loader/slots.c` (the 29 slots), `fd.c` (descriptors and names), `dev_con.c`, `dev_cmos.c`, `dev_pic.c`, `dev_timer.c`, `dev_ioasic.c`, `dev_sysctrl.c` (GT64010 register file and DMA 0), `diskslots.c`.
- `runtime/hw/voodoo_null.c` (PCI configuration, registers, a readable LFB, texture sink), `gun.c`, `misc.c` (sound FIFO, 0x17F00000), `dcs_stub.c`.
- `runtime/main/carnevil.c`: options, loading, the run loop, the trace log.
- `carnevil_tools/bootstate.py`: writes `boot_state.h` from `config/boot_state.toml` into the generated folder.
- `config/runtime.expected.toml`: interpreter entries and tolerated requests, each with a reason.
- `tools/mame/m2facts.lua`, `dcstap.lua`: Task 1's measurements.
- `tests/test_disk_c.py`, `test_clock_irq.py`, `test_tasks.py`, `test_loader.py`, `test_hw.py`, `test_dcs.py`, `test_boot.py`.
- `docs/analysis/M2-report.md`, `docs/analysis/runtime.md`, `docs/analysis/dcs-handshake.md`.

Runtime tests follow `tests/charness.py`: Python builds a small C driver against the runtime objects, runs it and compares what it prints. Guest code in tests that need no game data is assembled with `vectors.assemble`.

---

### Task 1: The remaining facts from MAME, and the doc corrections

**Files:**
- Create: `tools/mame/m2facts.lua`, `tools/mame/dcstap.lua`, `docs/analysis/dcs-handshake.md`
- Modify: `tools/mame/README.md`, `docs/analysis/loader-services.md`, `docs/analysis/boot-handoff.md`, `docs/analysis/hardware-usage.md`, `docs/analysis/M1-report.md`

**Interfaces:**
- Produces, on the share under `mame/m2-facts/` (attract run of 150 s from the entry, blank NVRAM, as run A of `task-switch.md`):
  - `entry-hw.txt`: the hardware state at 0x800C4000 (open question 1 of `loader-services.md`), and the fill byte of a blank NVRAM.
  - `inputs.txt`: the values ioctls 0x6900 to 0x6903 store, the 8 gun registers' read values and the 0x17F00000 read value, with no input.
  - `dcs-trace.txt`: every 0x6906, 0x6908, 0x6911 argument and every 0x6907, 0x6909, 0x690A result in order with emulated time, plus every 0xEEEE return and the cause of the one IP3 interrupt of run A. One line per event, `time_ns kind value`.
  - `dma0.txt`: every ioctl 0x7300/0x7301 `{offset, value}`, the descriptor chain each start points at (all words of each record), and the emulated time from each start to its cause-bit-4 interrupt.
  - `voodoo-reads.txt`: every Voodoo register and LFB read by game code in the first 15 s, `time_ns address value pc`, and the PCI configuration reads and writes of slots 0x0F0/0x0F8/0x108/0x110 with results (this gives the Voodoo's device number).
  - `disk-hooks.txt`: open questions 5 and 6 (context of the callback 0x800E7CF8, wake 0x80149B40 and wait 0x80149A44; the movie's partition and sectors per frame; the words wake changes).
  - `spin.txt`: the VBLANK wait's spin counter at exit for 600 frames (question 4).
  - `breaks.txt`: hits of the break hooks and of the catch-all 0x800C47E0 in run A and in a play run (questions 9, 11).
  - `opens.txt`, `console.txt`, `tasks.txt`, `loader-first.txt`: machine-readable copies of what `loader-calls.md` and `task-switch.md` report (one event per line), for Task 9's comparisons: the 193 opens `(name, mode, fd)`, the game's 25 console writes `(fd, text)`, the task creations and exits `(event, function)`, and the first 400 loader calls `(n, slot, a0, a1, a2, a3, sp, v0)`. Reuse `mame/m2-measure/` where it already holds them.

- [ ] **Step 1:** Write the two Lua scripts in the style of `tools/mame/tasksw.lua`, and run them in the Linux container. A second DCS run forces SOUNDSTAT bit 0x40 clear and records whether attract mode still starts and where the 0xEEEE results end up (question 3).
- [ ] **Step 2:** Write `docs/analysis/dcs-handshake.md`: the exchange the game has with the DCS in attract mode as a list of command words sent and the reply expected for each, with the rule that gives each reply (a constant, an echo, a checksum over downloaded words; read MAME's `dcs.cpp`, BSD-licensed, for the rule where the trace alone does not show it). Say plainly which replies are rules and which are only recorded values.
- [ ] **Step 3:** Answer the eleven open questions in `loader-services.md` in place, each marked measured. Apply its three corrections to `boot-handoff.md` (slot 0x0D8 is `_exit`; GT64010 cause handlers get a0 = 1; partitions are 1-based) and its GT64010 DMA correction to `hardware-usage.md`. Correct `M1-report.md` where `task-switch.md` refutes it (the scheduler's pacing), by a dated note, not by rewriting.
- [ ] **Step 4:** Run the suite (both ways). Commit: `analysis: M2 facts from MAME, DCS handshake, doc corrections`.

---

### Task 2: Contract changes: slice clock, host-switch entries, FP trap seam

**Files:**
- Modify: `runtime/include/recomp.h`, `runtime/interp/interp.c`, `runtime/stub/stub.c`, `carnevil_tools/functions.py`, `carnevil_tools/recomp.py`, `carnevil_tools/emit.py`, `carnevil_tools/cli.py`, `config/game.manual.toml`, `tests/harness/harness.c`, `tests/test_recomp.py`, `tests/test_interp.py`, `tests/test_whole_image.py`, `docs/analysis/recompiler.md`

**Interfaces:**
- Produces in `recomp.h`:
  - `cpu_t` loses `irq_pending` and gains `int32_t slice;`. `RT_CHECK(ram, c)` becomes `if (--(c)->slice <= 0) rt_tick((ram), (c));`. `void rt_tick(uint8_t *ram, cpu_t *c);` replaces `rt_interrupt`: the runtime accounts the checks used, runs due events, delivers interrupts and reloads `slice` (at least 1).
  - `enum { RT_SWITCH_RESUME, RT_SWITCH_YIELD, RT_SWITCH_EXIT }; void rt_host_switch(uint8_t *ram, cpu_t *c, int kind);`
  - `void rt_fp_trap(cpu_t *c, uint32_t cause_bit);` called by `rc_div_s`/`rc_div_d` when the divisor is zero, the dividend is finite and non-zero, and FCSR enable bit 10 is set; it does not return in the real runtime. The stub runtime logs and returns, so M1's vectors still pass.
  - `int rt_switch_kind(uint32_t pc);` generated in `func_table.c`: the kind at pc, or -1.
- A new manual entry kind `[[host_switch]]` with `addr`, `kind` (`"resume"`, `"yield"`, `"exit"`) and `reason`. The emitter runs the delay slot, calls `rt_host_switch`, and then: for `resume`, continues at `addr + 8`; for `yield`, returns from the C function; for `exit`, also returns (the real runtime never comes back from an exit; the stub does). The interpreter does the same at the same addresses through `rt_switch_kind`.
- The two `[[interpreted]]` entries (0x80151AC0, 0x80151B50) are removed; three `[[host_switch]]` entries are added (0x80151E88 resume, 0x80151B48 yield, 0x80151B78 exit).
- The emitter gains `--trace-pc`: before each load, store and call it stores the guest pc in `c->pc`. Off by default.

- [ ] **Step 1: Failing tests.**
  - `test_recomp.py::test_host_switch_shapes`: synthetic functions with each kind; the stub's `rt_host_switch` records `(kind, c->r[29])`; assert the delay slot ran before the call, `resume` continues at `addr + 8`, `yield` and `exit` return, and the interpreter gives the same register state and the same record.
  - `test_recomp.py::test_manual_host_switch_validation`: an entry whose `addr` is not a `j` or `jr ra` in reached code, or with an unknown kind, or without a reason, is an error naming the entry.
  - `test_interp.py::test_slice_counts_checks`: a loop of 10 backward branches with `slice = 4` calls `rt_tick` when the count reaches 0 and not before; emitted C and the interpreter call it the same number of times at the same guest state.
  - `test_interp.py::test_fp_trap_seam`: `div.s` 1.0 / 0.0 with FCSR bit 10 set calls `rt_fp_trap(c, 1 << 15)` (FCSR bit 15 is the divide-by-zero cause); with bit 10 clear it does not; 0.0 / 0.0 does not.
  - `test_whole_image.py::test_no_interpreted_functions`: the recompile output's manifest lists no interpreted function, and 0x80151AC0, 0x80151B50 and 0x80151BC0 are in the lookup.
- [ ] **Step 2:** Run them; expect failures naming the missing names.
- [ ] **Step 3:** Implement. Keep M1's check-point rule unchanged (where a check is emitted does not move).
- [ ] **Step 4:** `make recomp linkcheck smoke replay` and the suite: all of M1's results hold (1,352 vectors, the smoke run, 127 + 25 replays).
- [ ] **Step 5:** Fix the five minor notes of M1's re-review that touch these files (the `RT_CHECK` comment about stub thunks, `recip.s` of 0 bypassing the divide seam, the UBSan test skipping without the runtime). Commit: `recomp: slice clock, host-switch entries, FP trap seam`.

---

### Task 3: The disk image and its file system, in C

**Files:**
- Create: `runtime/disk/disk.h`, `runtime/disk/image.c`, `runtime/disk/fs.c`, `tests/test_disk_c.py`, `tests/harness/disk_driver.c`

**Interfaces:**
- Produces (`disk.h`):
  - `int img_open(const char *raw_path, const char *overlay_path, char *err, size_t errlen);` raw read-only; the overlay is created if absent and holds changed sectors with a header carrying the raw image's size and the SHA-256 of its first sector, checked on open.
  - `int img_read(uint32_t lba, void *buf, uint32_t count);`, `int img_write(uint32_t lba, const void *buf, uint32_t count);` (0, or -1 out of range), `uint32_t img_sectors(void);`, `void img_flush(void);`
  - `const uint8_t *img_header(void);` the 512-byte disk header the partition-table slot hands out.
  - `int fs_find(const char *name, struct fs_stat *st);` (name compared upper-cased; `st` has `size`, `attr`, `time`, `date`), `int fs_open(const char *name, int mode);` returns a handle or -1 (mode 0 read, 1 write-create-truncate, 2 read-write, 3 as the loader's), `int fs_read(int h, void *buf, uint32_t n);`, `int fs_write(int h, const void *buf, uint32_t n);`, `long fs_seek_words(int h, long off_bytes, int whence);`, `int fs_close(int h);`, `int fs_unlink(const char *name);`, `int fs_rename(const char *a, const char *b);` with the return values of `loader-services.md` section 8 (read returns 0xFFFFDBAE at end of file, 0 when `n % 4 != 0`; rename 0xFFFFDBB7 when absent).
- The layout is `docs/analysis/disk-format.md` and `carnevil_tools/disk.py`; partition numbers are the loader's (1 is the first partition).

- [ ] **Step 1: Failing tests** (`test_disk_c.py`, driver prints results):
  - with game data: every file `disk.list_files` finds on partition 1 is found by `fs_find` with the same size, and `fs_read` of the whole file gives the same SHA-256 as the Python extractor's file; `GAME.EXE`'s hash is the supported revision's.
  - `game.env` is absent: `fs_find` returns -1.
  - write round trip on an overlay: create `TEST0001.WMS` of 0x2084 bytes, close, reopen, read back equal; `fs_unlink` then `fs_find` returns -1; rename then find under the new name; afterwards `python3 -m carnevil_tools` reading raw plus overlay (add a `--overlay` option to the split/list path only if the test needs it; otherwise compare through the C driver) lists the same files as before plus the changes, and `verify_files` still passes for the untouched files.
  - the raw file's bytes and modification time are unchanged after all of the above.
  - without game data (a synthetic 64-sector image built by the test): overlay created on first open; reopening with an overlay made for another image fails with a message naming both; a truncated overlay fails with a message; `img_read` beyond the end returns -1.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement. Free-block allocation takes the first free entry of the chain table; the date and time written are fixed constants (documented as not the loader's).
- [ ] **Step 4:** Run; expect passes. Commit: `runtime: disk image with overlay, file system`.

---

### Task 4: Clock, interrupts, exceptions

**Files:**
- Create: `runtime/include/machine.h`, `runtime/core/clock.c`, `runtime/core/irq.c`, `runtime/core/exc.c`, `tests/test_clock_irq.py`, `tests/harness/machine_driver.c`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `rt_tick`, `rt_fp_trap`, `rt_break` from `recomp.h`; `rt_call`.
- Produces (`machine.h`):
  - `#define VCLOCK_NS_PER_CHECK 100`, `uint64_t vclock_now(cpu_t *c);` (includes the part of the slice already used), `void vclock_at(uint64_t t_ns, void (*fn)(void *), void *arg);`, `void vclock_cancel(void (*fn)(void *), void *arg);`
  - `void irq_raise(int ip);`, `void irq_clear(int ip);` (ip 2 to 6), `uint32_t m_status(void);`, `void m_status_set(uint32_t set, uint32_t clear);`
  - handler tables: `void irq_set_line(int line, uint32_t fn);` (line map of `loader-services.md`: IP6 is line 0, IP3 line 3, IP2 line 1, IP4 line 2, IP5 line 4), `void irq_set_gt(int bit, uint32_t fn);`, `void gt_cause_raise(int bit);`, `void exc_set_handler(int code, uint32_t fn);`, `void exc_set_fpe(int cause, uint32_t fn);`, `void exc_set_break_hook(int which, uint32_t fn);`
  - `void exc_raise(uint8_t *ram, cpu_t *c, int code, uint32_t epc);` and `_Noreturn void rt_stop(int status, const char *fmt, ...);`
  - `void vblank_start(uint64_t period_ns);` period 18,467,220 ns (1e9 / 54.15, MAME's screen) until M3 derives it from the Voodoo's timing registers.
- Behaviour, all from `loader-services.md` section 6:
  - Delivery happens in `rt_tick` only, when Status has IE set and EXL clear and a pending IP bit is unmasked. The dispatcher writes the frame at guest 0x80014328 with the layout of section 6 (GPRs sign-extended into 8-byte slots, k0 not written, resume PC at +0x110, Status +0x178, Cause +0x180, the FPR pairs from +0x218, FCSR +0x31C), sets EXL, sets sp to 0x800C1400, and serves the IP bits in ascending order: the line handler through `rt_call` with a0 = `0x100 << n`; when it returns non-zero or is absent, the default work for the line (VBLANK: acknowledge; IP3: clear INTCTL bit 0; IP2: for each GT cause bit pending and enabled, ascending, clear it and call its handler with a0 = 1). Then it restores every register but k0 from the frame, Status from +0x178 and FCSR from +0x31C, and sets INTCTL bit 14.
  - An exception other than an interrupt calls the handler of its code with (Cause, frame); if absent or it returns non-zero, the default: for code 9 with a break hook, `hook(Cause, frame)`; for code 15, the FPU handlers by FCSR cause bit; otherwise `rt_stop`. Execution resumes at the frame's +0x110: the dispatcher returns that pc to its caller, and `rt_break` resumes through `rt_jump` only when the hook moved it (a hook that leaves it unchanged would loop for ever on the board: `rt_stop` naming the pc).
  - `rt_break` codes: 0x409 installs a handler (a0 code, a1 function; codes above 15 ignored); 0x407 and 0x402 `rt_stop`; 0x1C00 and 0x1800 raise code 9; any other code `rt_stop`.
  - `rt_fp_trap` sets the FCSR cause bit and raises code 15 at `c->pc`.
  - A second exception while EXL is set is `rt_stop` ("exception inside a handler", as the board reboots).
  - `rt_call` to 0xBFC00000 (and `rt_jump`) is `rt_stop` ("game jumped to the reset vector").

- [ ] **Step 1: Failing tests** (synthetic guest handlers assembled by the test; no game data):
  - `test_vblank_delivery`: with a line-0 handler that increments a guest word and returns 1, running a guest loop for 10 periods of virtual time increments it 10 times; the handler saw a0 = 0x4000, sp = 0x800C1400, and guest registers after each interrupt equal those before it (all 32 but k0, hi, lo, FPRs).
  - `test_no_nesting`: a handler that loops for 3 periods is entered once; the VBLANKs due meanwhile are delivered after it returns, one at a time, none lost beyond the single pending bit.
  - `test_masked_until_registered`: before any `irq_set_line`, 10 periods pass with no delivery and no stop.
  - `test_frame_layout`: a handler copies frame words to scratch memory; assert the offsets above for a known register pattern, including an odd FPR at +0x21C + 16k.
  - `test_handler_edits_frame`: a code-13 handler adds 4 to +0x110 and changes v0's slot; execution resumes at the next instruction with the new v0.
  - `test_gt_cause`: `gt_cause_raise(4)` with a handler for bit 4 calls it with a0 = 1 once; the cause bit is clear afterwards.
  - `test_break_services`: `break 0x409` installs; `break 0x407` stops with a message containing the pc in hex; `break 7` with a hook calls hook(Cause, frame) and, the hook leaving the pc, stops naming the pc.
  - `test_fp_trap_reaches_catch_all`: with a code-15 handler installed by `break 0x409` that jumps to 0xBFC00000, `div.s` by zero with the enable set stops with "reset vector" and exit status 3.
  - `test_timer_far_future`: an event at 2^40 ns fires after that much virtual time (the slice reload does not overflow 32 bits), and `vclock_now` is monotonic across `rt_tick`.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect passes. Commit: `runtime: virtual clock, interrupt and exception dispatch`.

---

### Task 5: Host contexts for the game's tasks

**Files:**
- Create: `runtime/core/tasks.h`, `runtime/core/tasks.c`, `tests/test_tasks.py`, `tests/harness/tasks_driver.c`

**Interfaces:**
- Consumes: `rt_host_switch` kinds (Task 2); `rt_set_fcsr`, `rt_get_fcsr`; `m_status` (Task 4).
- Produces (`tasks.h`), with no game knowledge in it:
  - `typedef struct task_ctx task_ctx; task_ctx *ctx_spawn(void (*entry)(void *), void *arg, size_t host_stack);` the context starts parked.
  - `void ctx_switch_to(task_ctx *t);` called on the scheduler's context: runs `t` until it parks or ends, then returns. `void ctx_park(void);` called on a task's context: hands back to the scheduler and returns when switched to again. `_Noreturn void ctx_end(void);` `void ctx_retire(task_ctx *t);` ends a parked context without running it. `int ctx_is_scheduler(void);`
- Produces in `tasks.c`, the game glue `rt_host_switch`:
  - RESUME: the record is the guest word at 0x801A3118. If `c->r[31]` is 0x8014A930 (the trampoline: a task's first run), retire any context held for that record and spawn one (host stack 1 MB) whose entry calls `rt_call(ram, c, 0x8014A930)`; otherwise the record must have a parked context. Switch to it. On return, re-apply `rt_set_fcsr(c, rt_get_fcsr(c))`.
  - YIELD: `ctx_park()`, then re-apply the FCSR. EXIT: drop the record's context and `ctx_end()`.
  - A host switch with EXL set, a YIELD or EXIT on the scheduler's context, a RESUME on a task's context, or a RESUME of a record with no context and another `ra`, is `rt_stop` with a message naming the record and pc.
  - At most 64 contexts; the 65th is `rt_stop`.

- [ ] **Step 1: Failing tests.**
  - `test_ping_pong` (C only): 3 contexts each park 1,000 times; the scheduler's loop sees them run in the order it chose, 3,000 switches, and all three end.
  - `test_retire_parked`: a parked context is retired; its entry never runs again; the process exits with status 0 while two other contexts are still parked (no hang: the test has a 20 s timeout).
  - `test_fcsr_follows_cpu`: a context sets round-toward-zero through `rt_set_fcsr` and parks; the next context computes 1.0f / 3.0f under the mode the shared `cpu_t` says, checked by the bits of the result.
  - `test_guest_scheduler` (no game data): a 40-instruction guest program assembled by the test with the three host-switch shapes (a scheduler loop resuming two task records by `jr ra`, tasks that yield through a `j`, one that exits), with records at 0x801A3118 as the glue expects: each task's counter in guest memory reaches the expected value, in emitted C and in the interpreter.
  - `test_misuse_stops`: a YIELD on the scheduler's context stops with a message naming the pc.
  - with game data, `test_first_tasks_match_mame`: built in Task 9 (listed here so the reviewer knows it exists).
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement with one mutex and one condition variable per context (or one shared pair); exactly one thread runs at any time.
- [ ] **Step 4:** Run; expect passes, also under `-fsanitize=undefined` and, where the host has it, `-fsanitize=thread` (skip otherwise, saying so). Commit: `runtime: host contexts for the game's cooperative tasks`.

---

### Task 6: Loader slots I: descriptors, files, console, CMOS, PIC, timers, inputs, handler registration

**Files:**
- Create: `runtime/loader/slots.c`, `fd.c`, `dev_con.c`, `dev_cmos.c`, `dev_pic.c`, `dev_timer.c`, `dev_ioasic.c`, `runtime/loader/loader.h`, `tests/test_loader.py`, `tests/harness/loader_driver.c`

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces: `void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot);` arguments from a0 to a3 and the guest stack as each signature of `loader-services.md` section 1 says, result in v0; a driver interface `struct ldev { const char *prefix; int (*open)(...); int (*close)(...); int (*read)(...); int (*write)(...); int (*ioctl)(int fd, uint32_t cmd, uint32_t arg); };` registered in the name table's order; `void cmos_load(const char *path); void cmos_save(void); uint8_t cmos_byte(uint32_t offset);` (Task 7's bus uses it); `void ioasic_set_inputs(const uint16_t ports[4]);`
- Slots in this task, exactly as section 8 of `loader-services.md`: 0x018, 0x028, 0x038, 0x088, 0x090, 0x0C0, 0x0C8, 0x0D8, 0x0E0, 0x0E8, 0x190, 0x198, 0x1A8, 0x1B8, 0x1C0, 0x1E0, 0x1E8, 0x1F0, 0x1F8, 0x200. Descriptors 0 to 5 are open at the start as the dump shows (`con:` three times, `cmos:`, `ioasic:`, `timer:`); the lowest free descriptor is handed out; 16 descriptors.
- ioctls in this task: `cmos:` 0x6300 to 0x6302; `pic:` 0x7000 to 0x7002 (0x0001E240, 0x000001D5, 0x00001B88); `timer:` 0x7401 to 0x7403, 0x7405 to 0x7407, 0x7409 to 0x740B on `vclock_now`; `ioasic:` 0x6900 to 0x6903 (the values of Task 1's `inputs.txt`), 0x6905, 0x690E, 0x690F, 0x6910; the generic 0x6601/0x6603/0x6604. The sound commands 0x6906 to 0x690A and 0x6911 call `dcs_ioctl(cmd, arg)`, which this task provides as a stub that stops; Task 8 replaces it.
- The console writes each `write` to the host's stdout, and to the trace log with the fd. The CMOS is a 32 KB file, created filled with the blank byte of `entry-hw.txt`. Slot 0x1A8 returns 0x800B6E30 (4 zero words). Slot 0x200 for line 0 starts the VBLANK source and ORs IM3, IM6 and IE into Status.

- [ ] **Step 1: Failing tests** (driver calls `rt_loader` with a `cpu_t` and guest memory it sets up):
  - the tests section 8 names: `close(3)` then `ioctl(3, 0x6301)` is -1; `close(16)` is -1; `open("pci:", 2)` is 6 (with Task 7's driver absent, a placeholder `pci:` entry that stops on ioctl); `open("nosuchfile", 0)` is -1; after `fpe_set_handler(3, fn)` FCSR bit 10 is set.
  - `test_findfirst`: `game.env` returns 0xFFFFFFFF and leaves the buffer; `FONT0001.WMS` returns 0 with size 0x2AAD4 at +26 and the name at +30 (game data).
  - `test_read_rules`: a read with `n % 4 != 0` returns 0; at end of file 0xFFFFDBAE; a whole-file read of `FONT0001.WMS` puts the Python extractor's bytes in guest memory.
  - `test_lseek_words`: SET, CUR and END as section 8; whence 3 returns 0.
  - `test_cmos`: position by 0x6300 at 0xB6100000 + 4 × offset; out of range -2; a 4-byte write then read returns the bytes; the position does not move; the file holds them after `cmos_save`; a truncated NVRAM file is an error message, a missing one is created blank.
  - `test_timer_oneshot`: after `0x7403` arg 0, `0x7407` gives `elapsed checks × 100` rounded down to a multiple of 20; after 335.6 ms of virtual time exactly 0x13FFFFEC, and still that value after 10 s; after `0x740B` and a restart it starts from 0 again. Periodic mode wraps with the period.
  - `test_console`: `write(2, "abc\n", 4)` returns 4 and the driver's stdout has `abc`; `isatty(1)` is 1, `isatty(3)` is 0.
  - `test_unknown_requests_stop`: slot 0x048 (not one of the 29), `ioctl(4, 0x690B)` and `ioctl(5, 0x7400)` each stop with a message naming the slot or command and `ra`; none returns.
  - `test_exit_exec`: 0x0D8 stops with the status; 0x198 stops naming the file.
  - `test_slots_from_handler`: an ioctl issued while EXL is set (from a synthetic line-0 handler) works and delivers nothing.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect passes. Commit: `runtime: loader slots, descriptors, console, CMOS, PIC, timers, inputs`.

---

### Task 7: Loader slots II and the bus: PCI, Voodoo sink, GT64010 DMA 0, disk slots, gun, CMOS reads

**Files:**
- Create: `runtime/core/bus.c`, `runtime/hw/hw.h`, `voodoo_null.c`, `gun.c`, `misc.c`, `runtime/loader/dev_sysctrl.c`, `diskslots.c`, `tests/test_hw.py`, `tests/harness/hw_driver.c`
- Modify: `runtime/loader/slots.c`

**Interfaces:**
- Consumes: Tasks 3, 4, 6; Task 1's `voodoo-reads.txt`, `dma0.txt`, `inputs.txt`, `disk-hooks.txt`.
- Produces: `bus_read`/`bus_write` by physical address (kseg0, kseg1 and kuseg aliases folded): Voodoo 0x08000000 to 0x08FFFFFF (registers, LFB, texture memory), gun, sound FIFO 0x13000000 (dropped, counted), CMOS 0x16100000 (reads: one byte per word from `cmos_byte`; a direct write stops), 0x17F00000 (the value of `inputs.txt`; writes accepted). Anything else stops, naming the address, size, direction and `c->pc`.
- Slots: 0x0F0, 0x0F8, 0x108, 0x110 against the Voodoo's PCI configuration (vendor 0x121A at the device number Task 1 measured; BAR0 0x08000000; slot 0x110 returns 0xA8000000, or 0 and `*dev = 10` for another device); 0x118, 0x128, 0x158, 0x1D8, 0x220 as section 8, the callback or wake called through `rt_call` before the slot returns.
- `pci:` (the `sysctrl` driver) ioctls 0x7300 and 0x7301 on a GT64010 register file. A write that starts DMA channel 0 walks the descriptor chain as `dma0.txt` shows, copies each record from its source to its destination through `mem_r8`/`mem_w8` (so a Voodoo destination reaches the sink), and schedules cause bit 4 after the delay MAME showed (the median of `dma0.txt`).
- Voodoo sink (`voodoo_null.c`): registers hold what was written; `status` reports the FIFO empty, not busy, no swap pending, and the vertical-retrace bit from the virtual clock; the registers Glide's `sst1Init` reads back during set-up (DAC and `fbiInit` reads) answer as MAME's did; the LFB is real memory (written pixels read back); texture writes are counted and dropped; every register write is counted by register for the report. A read of a register that is in neither `voodoo-reads.txt` nor the set-up list stops.

- [ ] **Step 1: Failing tests.**
  - `test_pci_probe`: the four probes of `loader-calls.md` (devices 6 to 9) find the Voodoo only at the measured device; slot 0x110 returns 0xA8000000 and stores the device number.
  - `test_voodoo_reads_like_mame` (game data): for every register in `voodoo-reads.txt` other than `status`, after replaying the trace's preceding writes, the sink's read value is one MAME returned for that register; `status` has the bits above.
  - `test_lfb_readback`: a 16-bit pixel written at an LFB address reads back; a texture write is dropped and counted.
  - `test_dma0_chain`: a two-record chain in guest memory, started as `dma0.txt`'s sequence does, copies both records, and the bit-4 handler runs once after the delay with a0 = 1.
  - `test_raw_read_callback`: slot 0x158 with a callback set copies the sectors, calls the callback with 0 before returning, and returns 0; without callback or hook it returns the count; an LBA beyond the image transfers nothing and calls the callback with the count.
  - `test_cmos_direct_read`: after a `cmos:` write of a byte at offset 5, `mem_r8(0xB6100014)` reads it.
  - `test_unmodelled_bus_stops`: a read of 0xB6000000 (IOASIC) stops naming the address and pc.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect passes. Commit: `runtime: bus, PCI and Voodoo sink, GT64010 DMA 0, disk slots`.

---

### Task 7b: Voodoo frame buffer for the start-up checks (added 2026-11-20)

Task 7 found that the premise "the Voodoo accepts and discards writes" does not hold: Glide's start-up reads back pixels the card rendered. Two checks run before attract mode (both measured, `mame/m2-facts/voodoo-ramp.txt`): a frame-buffer memory test that depends on how LFB addresses alias (it writes 0xDEAD at 0x08432100 and later reads 0xFFFF there after writing 0x0852BE3C), and a dither detection that draws 258 flat-coloured dithered triangles with `triangleCMD` and reads back 4 × 4 pixel blocks (routine 0x8015B250). This task gives the sink a real frame buffer for exactly those two.

**Files:**
- Create: `runtime/hw/voodoo_fb.c`, `tests/test_voodoo_fb.py`
- Modify: `runtime/hw/voodoo_null.c`, `runtime/hw/hw.h`, `tests/harness/hw_driver.c`, `tests/test_hw.py`, `docs/analysis/hardware-usage.md`, `Makefile`

**Interfaces:**
- Consumes: Task 7's sink (register storage, `hw_reset`), `voodoo-ramp.txt` and `voodoo-reads.txt`.
- Produces, internal to `runtime/hw/`: `void vfb_reset(void);`, `uint32_t vfb_lfb_read(uint32_t offset, int size);`, `void vfb_lfb_write(uint32_t offset, int size, uint32_t value);`, `void vfb_triangle(void);` (draws from the sink's current register values when `triangleCMD` is written). The frame buffer's size, the buffer offsets, the LFB address decoding, the 16-bit pixel format conversions, the dither matrices and the triangle fill rule are the card's, taken from MAME 0.276's BSD-3-Clause Voodoo sources as reference (fetched read-only into the scratchpad, never into the repo; the code is written fresh and `hardware-usage.md` credits the source and marks each rule "from MAME's model").
- Drawn: flat-shaded (constant `color1`) triangles with the dither mode `fbzMode` selects, into the buffer `fbzMode` selects, clipped to the clip rectangle. Not drawn, and a stop if the mode registers ask for it at a `triangleCMD`: texturing, Gouraud colour, depth test, alpha test or blend, fog, chroma key, stipple. In attract mode the game draws with all of these; so `triangleCMD` in a mode outside the flat set is counted and dropped once the start-up checks are over. The rule for "over" is not a time or a count: a triangle in a non-flat mode is dropped (and counted), a triangle in the flat mode is drawn. LFB reads always return what the frame buffer holds.

- [ ] **Step 1: Failing tests.**
  - `test_memory_test_like_mame` (game data): replaying the capture's writes in order, every LFB read of the memory test returns its measured value.
  - `test_dither_ramp_like_mame` (game data): replaying the capture's writes in order, every one of the 8,228 ramp reads returns its measured value.
  - `test_reads_like_mame_full` (game data): every Voodoo read in `voodoo-ramp.txt` returns its measured value (registers, status excepted, and LFB).
  - `test_flat_triangle` (no game data): a triangle with vertices (0,0), (36,0), (0,36) and `color1` 0x808080 without dither fills exactly the pixels the card's fill rule covers (assert the pixel count and the four corner cases) with 0x8410; with 4 × 4 dither, a 4 × 4 block at (4,4) holds the card's dither pattern for that colour.
  - `test_lfb_aliasing` (no game data): two LFB addresses that the card maps to one pixel read back the last write; an address outside the buffers reads the card's value for unmapped frame-buffer memory.
  - `test_non_flat_triangle_dropped`: a `triangleCMD` with texturing enabled changes no pixel and increments the dropped count; with depth test or blending enabled, the same.
  - `test_lfb_readback` in `test_hw.py` is changed back from "stops" to reading what was written.
- [ ] **Step 2:** Run; expect assertion failures.
- [ ] **Step 3:** Implement. Where MAME's model and the capture disagree, the capture wins and the difference is reported.
- [ ] **Step 4:** Run the suite both ways. Commit: `runtime: Voodoo frame buffer for the start-up memory test and dither detection`.

---

### Task 8: The DCS responder

**Files:**
- Create: `runtime/hw/dcs_stub.c`, `tests/test_dcs.py`
- Modify: `runtime/loader/dev_ioasic.c`, `docs/analysis/dcs-handshake.md`

**Interfaces:**
- Consumes: `docs/analysis/dcs-handshake.md` and `dcs-trace.txt` (Task 1).
- Produces: `int dcs_ioctl(uint32_t cmd, uint32_t arg_ptr_or_value);` for 0x6906 to 0x690A and 0x6911: SOUNDCTL written and read back; SOUNDSTAT with bit 0x80 always set and bit 0x40 set while a reply is waiting; SOUNDOUT words fed to a state machine that queues the replies by the rules of `dcs-handshake.md`; SOUNDIN returns the head of the queue and the 0x6911 write removes it. Line 3 (IP3) is raised only where the trace shows the board raised it.

- [ ] **Step 1: Failing test** `test_replays_mame_exchange` (game data): feeding the trace's SOUNDCTL and SOUNDOUT events in order yields the trace's SOUNDIN values in order, and SOUNDSTAT bit 0x40 is set at each recorded SOUNDIN read; `test_no_reply_without_command`: with no command, bit 0x40 stays clear and bit 0x80 set; `test_unknown_command_recorded`: a command word outside the documented set is accepted, queues nothing and is counted for the report (it is sound data in most cases; the count appears in the trace log).
- [ ] **Step 2:** Run; expect failure.
- [ ] **Step 3:** Implement the rules. Where a reply is a recorded value with no rule, say so in the doc and in a comment, with the trace line.
- [ ] **Step 4:** Run; expect passes. Commit: `runtime: DCS responder for attract mode`.

---

### Task 9: The `carnevil` program, and the boot to attract mode

**Files:**
- Create: `runtime/main/carnevil.c`, `carnevil_tools/bootstate.py`, `config/runtime.expected.toml`, `tests/test_boot.py`
- Modify: `Makefile` (targets `carnevil`, `run`), `carnevil_tools/cli.py`, `runtime/stub/smoke.c` and `replay.c` only as far as the contract change needs

**Interfaces:**
- Consumes: everything above.
- Produces: `carnevil --disk carnevil.raw [--overlay FILE] [--nvram FILE] [--seconds N | --frames N] [--trace FILE] [--quiet]`. Defaults: overlay and NVRAM next to the raw image as `carnevil.overlay` and `carnevil.nvram`. It loads `GAME.EXE` from the image through `fs`, checks its SHA-256 against the one the generated code was made from (a constant in `func_table.c`) and refuses another, places the image as `docs/analysis/exe-format.md` says, sets the entry state from `boot_state.h` (registers, Status, FCSR) and RAM below the image to zero except what the stand-ins own, and calls the entry. Exit status: 0 when the requested time has run, 2 for a usage or file error, 3 for a stop.
- The trace file has one line per event: `time_ns kind fields...` for console writes, opens, loader calls (with `in_irq`), task switches (kind, record, function at +0x24), interpreter entries, stops, and at the end the per-register Voodoo write counts and the dropped sound words.

- [ ] **Step 1: Failing tests** (`test_boot.py`, game data; one run of `--seconds 150` shared by the tests):
  - `test_console_matches_mame`: the game's console writes, as `(fd, text)` in order, equal MAME's 25 (`console.txt`).
  - `test_opens_match_mame`: the ordered `(name, mode, fd)` list equals `opens.txt` over the common prefix, and the port has at least 180 of MAME's 193.
  - `test_tasks_match_mame`: the ordered creations and exits `(event, function)` equal `tasks.txt`; at most 6 tasks are alive at once.
  - `test_first_loader_calls_match_mame`: among the first 400 loader calls of each side, those made outside interrupts agree in order on slot, the arguments each slot's signature has, and v0.
  - `test_reaches_and_stays_in_attract`: the run ends with status 0; the VBLANK count reaches at least 8,000; the task whose function `task-switch.md` names `attract` is alive at the end.
  - `test_nothing_unexplained`: every interpreter entry and every tolerated request in the trace is listed in `config/runtime.expected.toml` with a reason; the file has no entry the run did not use.
  - `test_deterministic`: two runs of `--seconds 20` give byte-identical traces.
  - `test_spin_counter_sane`: the largest VBLANK-wait spin count in the run is below 10,000,000.
  - `test_user_files`: a missing image, an image of the wrong size and a `GAME.EXE` of another hash (a patched copy of the image's first sectors in a temp file is enough to fail the hash) each exit 2 with one line on stderr naming the file; the raw image's modification time is unchanged after a run; a second run reuses the overlay and NVRAM and prints the lines a non-blank NVRAM gives (no `reseting high scores`).
  - `test_ubsan_run`: `--seconds 20` built with the sanitizer has no report.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement, then bring the game up: run, read the stop message, find the cause, fix the stand-in or the model it names, and repeat. Each fix that changes what a stand-in does gets its own test in the owning task's test file and a line in the report's list of corrections to `loader-services.md`. Do not add to `runtime.expected.toml` to get past a stop without a reason that holds on the board.
- [ ] **Step 4:** Run the whole suite on the Mac, then build and run `test_boot.py` in the Linux container (GCC 14, Linux x86-64): same results, and the 20-second traces of the two hosts are identical except for lines that hold host NaN bits, if any (list them).
- [ ] **Step 5:** Commit: `runtime: carnevil runs headless to attract mode`.

---

### Task 10: M2 report and documentation

**Files:**
- Create: `docs/analysis/M2-report.md`, `docs/analysis/runtime.md`
- Modify: `README.md`, `docs/getting-started.md`, `docs/how-it-works.md`, `docs/hardware.md`, `docs/roadmap.md`, `docs/tools.md`, `docs/superpowers/plans/2026-11-19-carnevil-recomp.md` (M2 marked done, pointing at the report), the spec's corrections

- [ ] **Step 1:** `runtime.md`: how the runtime is put together, for a reader who knows C: the clock, interrupt delivery, the task contexts, each stand-in group and each device, and how to read a trace.
- [ ] **Step 2:** `M2-report.md`: what runs, the comparisons with MAME and how strong each is, what was not checked (play, sound, pictures), every departure from this plan, the corrections the bring-up made to the analysis docs, the measured host speed of the headless run (virtual seconds per host second on the Mac and in the Linux container), the Voodoo write counts against `hardware-usage.md`'s MAME counts, and what M3 needs (the Voodoo register stream, the frame timing, the GL thread question).
- [ ] **Step 3:** Getting-started gains the run instructions. Fix the remaining M1 minor notes in the guides (`tools.md` smoke arguments, the "measured now" wording).
- [ ] **Step 4:** Run the suite both ways. Commit: `docs: M2 report, runtime guide`.

---

## Exit check for M2

- `make carnevil` builds with zero warnings on the Mac and in the Linux container.
- `carnevil --disk … --seconds 150` exits 0 on both, with the console, opens and task comparisons against MAME passing.
- No interpreter entry or tolerated request without a reason in `config/runtime.expected.toml`.
- The suite passes with the share mounted and with `CARNEVIL_WORK=/nonexistent`.
- The user's `carnevil.raw` is byte-identical before and after.
