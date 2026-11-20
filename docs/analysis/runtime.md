# The runtime

Milestone M2 built the runtime under the recompiled game: the C that stands in for the arcade
board and for the boot ROM's loader, and the program `carnevil` that runs `GAME.EXE` headless
from the user's own disk image. This note is for a C programmer who wants to understand, run
or change it. The generated C and its contract are in [recompiler](recompiler.md); what was
checked against MAME, and how strongly, is in the [M2 report](M2-report.md); the commands are
in [tools.md](../tools.md).

Two kinds of statement appear here. Facts about the game or the board carry a mark, as in the
other analysis notes: **measured** (MAME 0.276), **read** (disassembly of `GAME.EXE` or the
loader), **inferred**, or **from MAME's model** where a rule of the hardware was taken from
MAME's source, read but not built. Statements about the runtime's own code are a description
of that code as of commit `e50f325`, not evidence about the machine: each section names the
source file or files it describes, and names the test that checks a behaviour where one
exists. A behaviour with no test named has none that this note knows of.

## The pieces

Everything is C11 with POSIX threads, built with the flags of the [recompiler](recompiler.md)
note plus `-Wall -Wextra -Werror`. The files, by directory (`Makefile` groups them the same
way):

- `runtime/include/recomp.h`: the contract shared with the generated C (M1, extended in M2).
  `runtime/include/machine.h`: the state the runtime's own files share, one `machine_t`.
- `runtime/core/`: `call.c` and `fpu.c` (M1: `rt_call`, `rt_jump`, the host FP control);
  `clock.c` (the virtual clock, its events, `rt_tick`); `irq.c` (Status and Cause, the handler
  tables, the exception dispatcher and its frame, the interrupt loop, VBLANK); `exc.c`
  (`rt_break`, `rt_fp_trap`, exceptions other than interrupts, `rt_stop`); `tasks.c` and
  `tasks.h` (host contexts and the game glue `rt_host_switch`); `bus.c` (`bus_read`,
  `bus_write`).
- `runtime/interp/interp.c`: the reference interpreter (M1). No game function runs in it in
  M2.
- `runtime/disk/`: `image.c` (the raw image, read-only, and the overlay), `fs.c` (the file
  system of partition 1), `sha256.c`, `disk.h`.
- `runtime/loader/`: the stand-ins for the loader's 29 service slots. `slots.c` (`rt_loader`,
  the slot table, the handler registrations, the PCI slots, exit and exec), `fd.c` (the
  descriptor and name tables, open, close, read, write, ioctl, isatty), `dev_file.c` (files,
  find-first, unlink, rename, lseek), `diskslots.c` (partitions, raw sectors, the disk hooks),
  and one file per driver: `dev_con.c`, `dev_cmos.c`, `dev_pic.c`, `dev_timer.c`,
  `dev_ioasic.c`, `dev_sysctrl.c` (`pci:`: the GT64010's registers and DMA channel 0).
- `runtime/hw/`: the hardware the game touches itself. `voodoo_null.c` (the Voodoo's
  registers, status, FIFOs, video timing and PCI configuration), `voodoo_fb.c` (its frame
  buffer and drawing), `voodoo_tmu.c` (its texture unit), `gun.c`, `misc.c` (the sound FIFO,
  the register at 0x17F00000, `hw_reset`), `dcs_stub.c` (the DCS sound board's responder),
  `hw.h`.
- `runtime/main/`: `carnevil.c` (options, start-up, the run) and `trace.c` (the observers and
  the trace file).
- `runtime/stub/`: M1's stub runtimes (`game_stub`, `smoke`, `replay`), unchanged in purpose.

Each header and each `.c` file starts with a comment that gives its rules and the document
section each rule comes from. When this note and a file's comment differ, the comment is
nearer the code.

## A run from start to end

`carnevil --disk FILE [--overlay FILE] [--nvram FILE] [--seconds N | --frames N] [--trace
FILE] [--quiet]` (`runtime/main/carnevil.c`, whose top comment gives this order; the
exit statuses and file errors are checked by `tests/test_boot.py`, `MainTest`, and
`test_user_files`):

1. The options. Then four resets, each once: `machine_reset` (the clock, its events, the
   interrupt state, the at-stop list), `hw_reset` (the devices), `loader_reset` (descriptors 0
   to 5 open as at the hand-off, the measured input-port values, no hooks), `tasks_reset`
   (this thread becomes the scheduler's context).
2. The files. The raw image must be a regular file of 4,310,046,720 bytes, the size of the
   supported disk (0x0080730C sectors, measured: `loader-services.md`, section 7). `GAME.EXE`
   is read from partition 1 through the file system without the overlay, and its SHA-256 must
   be the one the generated code was made from (`recomp_exe_sha256`, which `make recomp`
   writes into `func_table.c`). Only then are the overlay, the NVRAM file and the trace opened
   or created, and `GAME.EXE` is read again through the overlay and checked again. Without
   `--overlay` and `--nvram` the two files go next to the raw image as `carnevil.overlay` and
   `carnevil.nvram`. The raw image is only ever opened with `O_RDONLY`.
3. RAM: 8 MB of zeros, then the image at its load address (`exe-format.md`). Nothing is placed
   below it: each stand-in writes its own areas (the argument block, the partition-table
   copy) when the game calls it.
4. The entry state from `boot_state.h`, which `make carnevil` writes from
   `config/boot_state.toml` (`bootstate` in [tools.md](../tools.md)): the general registers,
   `hi`, `lo`, FCSR and Status (measured in M1). `slice` is 0, so the first check point
   enters `rt_tick` and the clock starts there.
5. The observers (`trace.c`), the at-stop functions (the CMOS save, the overlay flush, the
   trace's end), the end of the run as a clock event at N seconds (or a VBLANK count for
   `--frames`), and the call of the entry 0x800C4000. It does not return: `_start` calls
   `main`, which calls the scheduler, which runs for ever.

A run ends in one of four ways. At the limit, the end event calls `rt_exit(0)`, which runs
the at-stop functions and exits 0, after one line on stderr, `carnevil: ran N ns of guest
time; RAM SHA-256 H` (left out with `--quiet`). A file or usage error before the game starts
exits 2 with one line naming the file. Anything the runtime does not model calls `rt_stop`,
which prints `carnevil: stop: ` and the reason, runs the same at-stop functions (so the NVRAM
and the overlay are saved) and exits 3. SIGINT, SIGTERM or SIGHUP (`rt_catch_signals`,
`exc.c`) only set a flag; `rt_poll_signal`, at the start of every `rt_tick` and in the
Voodoo's hold loop, sees it and calls `trace_interrupted`, which writes `end interrupted
SIGNAME` and ends through `rt_exit(130)`, the same path as the limit (since the whole-branch
review of M2, finding I1; `tests/test_clock_irq.py`,
`test_signal_ends_the_run_at_the_next_check_point`, and `tests/test_boot.py`,
`test_interrupted_run_saves_and_closes` and `test_interrupted_boot`).

The at-stop functions run once, on the thread that ends the run. A stop inside one of them
(the NVRAM save failing, the trace not written) is printed and traced like any other stop and
no longer skips the rest: the others still run and the program exits 3 (finding M10,
`test_failed_at_stop_function_does_not_skip_the_rest`). The NVRAM is written to a temporary
file, synced to the disk, renamed over the old one and its folder synced; the trace's
buffered tail is written when it is closed, and a failed write or flush is a stop naming
the file.

## The clock

Files: `runtime/core/clock.c`, `runtime/include/machine.h`. Tests in
`tests/test_clock_irq.py`: `test_event_order`, `test_timer_far_future` (an event at 2^40 ns,
monotonic time), `test_vclock_advance`, `test_vclock_advance_delivers_nothing`,
`test_raise_outside_event_delivered_next_check`.

Time is virtual and deterministic: nothing in the runtime reads the host's clock, so two runs
of the same image give byte-identical traces (`tests/test_boot.py`, `test_deterministic`).

- The unit is the check point. The generated code runs `RT_CHECK` at every function entry and
  before every backward branch inside a function ([recompiler](recompiler.md)); `RT_CHECK`
  decrements `c->slice` and, when it reaches 0, stores the check point's address in `c->pc`
  (the function's address at an entry, the target at a back edge) and calls `rt_tick`, so an
  interrupt delivered there records that address as its resume pc and EPC, the same in
  generated code and in the interpreter. Each check is worth `VCLOCK_NS_PER_CHECK` = 100 ns
  (`machine.h`).
- `machine.now` is the time at the last accounting. `vclock_now(c)` adds the checks of the
  current slice used so far, so a device reads the exact time of the access.
- Events (`vclock_at`, `vclock_cancel`) are kept sorted by time, then by the order they were
  scheduled; at most 64 are pending. `rt_tick` accounts the checks, runs every event that is
  due, delivers at most one interrupt, runs the events again, and reloads `slice` with the
  checks until the next event (at least 1, at most 1,000,000), or with 1 when an interrupt is
  deliverable.
- `vclock_reload(c)` does the accounting and the reload outside a tick. Code that makes an
  interrupt deliverable outside a clock event (a loader slot raising a line, a Status change)
  calls it, so the interrupt is taken at the next check point rather than up to a million
  checks later (`machine.h`, `irq_raise`).
- `vclock_advance(c, ns)` is time passing without check points, for a device that holds the
  CPU: it runs each event that falls due at its own time and delivers nothing. Only the
  Voodoo uses it (below).
- Virtual time 0 is `GAME.EXE`'s entry, which MAME reaches 5,003,569,666,667 ps after power-on
  (measured: cycle 750,535,450 at 150 MHz; `VOODOO_ENTRY_PS` in `hw.h`). The Voodoo's own
  clock counts from power-on, so its first frames line up with MAME's.

100 ns per check is not the board's pace. A check point covers a variable number of
instructions, so the guest runs faster or slower against the virtual clock depending on the
code: the first console line comes 3.5 times later than in MAME's virtual time, and the later
milestones up to 5 times earlier (measured; the [M2 report](M2-report.md), "The clock", has
the data). The comparisons with MAME compare order and values, never times.

## Interrupts and exceptions

Files: `runtime/core/irq.c`, `runtime/core/exc.c`, `runtime/include/machine.h`. The tests
are named at the end of the section.

The runtime replaces the loader's exception vectors and dispatcher with C that does what the
dispatcher does to the game's state (`loader-services.md`, section 6, read). The handlers are
the game's own code, called through `rt_call`.

- **Lines.** A device raises a CPU interrupt line with `irq_raise(ip)` (IP2 to IP6) and lowers
  it with `irq_clear`. Delivery happens only in `rt_tick`, when Status has IE set and EXL
  clear and a pending line is unmasked. So an interrupt is delayed to a check point, never
  lost; a line raised twice before it is served is one pending bit, as on the board.
- **The handler tables** are filled by the slots that fill them on the board: 0x200 (a
  line's handler: IP6 is line 0, IP2 line 1, IP4 line 2, IP3 line 3, IP5 line 4, read from
  the loader's line map), 0x1E0 (a GT64010 cause bit's handler), 0x1E8 (an FPU cause's
  handler), 0x1F0 and 0x1F8 (the break hooks), and `break 0x409` (an exception code's
  handler).
- **Dispatch** (`exc_raise`, `irq.c`). The frame is written at guest 0x80014328 with the
  loader's layout: general registers sign-extended into 8-byte slots (k0 not written), the
  resume pc at +0x110, Status at +0x178, Cause at +0x180, the FP pairs from +0x218 and FCSR
  at +0x31C (read). EXL is set, INTCTL bit 14 cleared, and the handlers run on the loader's
  exception stack (sp 0x800C1400) with a0 = Cause and a1 = the frame. Afterwards every
  register but k0, Status and FCSR come back from the frame, INTCTL bit 14 is set, and the
  slice is reloaded so a line that became pending meanwhile is taken at the next check point.
  A second exception while EXL is set stops, where the board prints "Exception while in
  exeption handler" and reboots (read).
- **The interrupt loop** serves every IP bit of the Cause it read, in ascending order: the
  line's handler with a0 = 0x100 << n, and when it returns non-zero (or there is none) the
  loader's default work for the line (read): for IP6, acknowledge VBLANK; for IP3, clear
  INTCTL bit 0 and lower the line; for IP2, every pending and enabled GT64010 cause bit in
  ascending order, cleared and then passed to its handler with a0 = 1; for IP5, the loader's
  message on stderr. The IP4 default (the IDE completion) is not modelled and stops: the disk
  stand-ins complete inside the call, so nothing raises IP4.
- **Who raises what.** The Voodoo's retrace raises IP6 (VBLANK) at each retrace start while
  the board's VBLANK enable is set, which slot 0x200 for line 0 sets (from MAME's model: the
  board's VBLANK is the card's vertical sync). The DCS responder drives IP3 from INTSTAT and
  INTCTL. The GT64010 model raises cause bit 4, and with it IP2, when a DMA chain completes.
- **The resume pc.** After an interrupt the generated code carries on where it was, so a
  handler that moves the resume pc stops the run. After a `break 7`, `break 6` or an FP trap
  the runtime resumes only at the next instruction; a handler that leaves the pc on the
  faulting instruction (which would loop for ever on the board) or moves it elsewhere stops,
  naming both pcs.
- **`rt_break`** (`exc.c`): 0x409 installs a handler; 0x1C00 (`break 7`, divide by zero) and
  0x1800 (`break 6`, overflow) raise exception code 9, whose default calls the break hook with
  (Cause, frame); 0x407 (assert) and 0x402 stop, as the board enters its debug monitor; any
  other code stops (read: the loader's break table 0x80004684).
- **`rt_fp_trap`**: an FP divide by zero with the FCSR enable set (the game sets it through
  slot 0x1E8) raises code 15 at `c->pc`, and the default calls the FPU handler of each FCSR
  cause bit. The game's catch-all then jumps to the reset vector, and `rt_bad_target` stops:
  "game jumped to the reset vector". `c->pc` is there the dividing instruction's address in
  every build: the generated code stores it before each FP divide, and the interpreter at
  every instruction. Where the pc is still only as last stored (a bus access, a loader call, a
  host switch, a transfer outside RAM in generated code built without `--trace-pc`), the
  message that names it says so.
  In MAME's attract and play runs the break hooks and the catch-all never ran (measured,
  `breaks.txt`).

What is not modelled: the loader's VBLANK count, LED and watchdog writes (the game never reads
them, read), its 100 ms tick on GT64010 cause bit 8, and the IP4 path. The tests are in
`tests/test_clock_irq.py`, on synthetic guest code: `test_vblank_delivery`,
`test_frame_layout`, `test_handler_edits_frame`, `test_no_nesting`,
`test_long_vblank_handler_absorbs`, `test_masked_until_registered`,
`test_masked_pending_not_delivered`, `test_ip_order`, `test_gt_cause`,
`test_ip3_default_lowers_the_line`, `test_pending_after_exception_delivered_next_check`,
`test_break_services`, `test_fp_trap_reaches_catch_all`, `test_fpe_handler_advances` and
`test_stop_paths` (the stop messages and status 3).

## The game's tasks on host threads

The game runs its own cooperative scheduler (read and measured, [task-switch](task-switch.md)).
`main` calls the scheduler once; each pass resumes the due tasks one by one with a `jr ra`
into the task (0x80151E88); a task gives the processor back through the yield, whose
`j 0x80151E90` (0x80151B48) lands in the scheduler's restore path; a finished task leaves
through the exit's `j 0x80151E90` (0x80151B78). The recompiler emits those three transfers
as calls of `rt_host_switch`; all the guest-visible work of the switch (the counters, the
registers saved on the guest stacks, the `sp`/`k1` swap) stays recompiled code.

- **Contexts** (`tasks.h`, generic, no game knowledge). A context is a POSIX thread with its
  own host stack. One mutex guards a single "baton", the context allowed to run, and each
  context has its own condition variable; a context runs only while it holds the baton, and
  only the scheduler hands the baton to a task. So only one thread touches the guest's
  `cpu_t` and RAM at a time, with a mutex release and acquire between consecutive threads.
  The protocol is argued in full at the top of `tasks.c`. `ctx_spawn`, `ctx_switch_to`,
  `ctx_park`, `ctx_end`, `ctx_retire` and `ctx_is_scheduler` are the interface; fibers could
  implement it instead.
- **The glue** (`rt_host_switch`, the bottom of `tasks.c`), keyed by the guest's current-task
  word 0x801A3118 (read):
  - resume with `ra` = 0x8014A930 (the trampoline the creation routines plant, read) is a
    task's first run: any context still held for that record is retired (the game freed the
    task while it was parked and reused its record), and a new context with a 1 MB host stack
    is spawned, whose entry calls `rt_call(0x8014A930)`;
  - any other resume switches to the record's parked context;
  - yield parks the running context;
  - exit ends the running context. It is found by context, not by the word at 0x801A3118,
    because the game's free makes the exiting task's predecessor current before the `j`
    (read).
  - After every switch the host FP environment is re-applied from the shared `cpu_t`
    (`rt_set_fcsr(c, rt_get_fcsr(c))`), because rounding mode and flush-to-zero are per
    thread on the host.
- **Refused**: a switch with EXL set (MAME showed none inside an exception, measured), a
  yield or exit on the scheduler's context, a resume on a task's context, a resume of a
  record with no context and another `ra`, an exit of an unknown context, and a 65th context.
  Each stops naming the record and the pc.
- **Stopping with tasks parked**: nothing is registered at exit and nothing has a destructor,
  so `rt_stop` from any thread exits at once with the other threads still blocked.

MAME measured at most 6 tasks alive at once (measured, `task-switch.md`); the port's 150 s run
reaches 6 too. Files: `runtime/core/tasks.c`, `runtime/core/tasks.h`. The tests are in
`tests/test_tasks.py`: `test_ping_pong` (3,000 parks among three contexts),
`test_retire_parked`, `test_stop_from_task`, `test_fcsr_follows_cpu`, `test_misuse_stops`,
`test_record_reuse`, `test_switch_hook`, `test_guest_scheduler` (a synthetic guest
scheduler, recompiled and interpreted) and `test_under_thread_sanitizer` (the scenarios
again under the thread sanitizer, skipped where the host cannot run it). Interrupts and task
switches together (since the whole-branch review of M2, finding I4), on the same guest
scheduler in both modes with the real clock and dispatcher: `test_task_created_in_vblank_handler`
(VBLANKs every 400 ns; the handler links a new task record, which the scheduler then first-runs
on a context of its own; the handler always runs with EXL set and returns normally) and
`test_interrupt_due_at_a_host_switch` (IP6 raised at the start of a chosen resume, yield or
exit: it is delivered once, at the first check point after the switch, on the resumed task's
context or the scheduler's, and the tasks end as without it).

## The loader's stand-ins

The game reaches files, the disk, the settings writes, PCI set-up and most devices through
29 slots of a jump table into the boot ROM's resident loader (read, [boot-handoff](boot-handoff.md)).
The generated code calls `rt_loader(ram, c, slot)` there, and `slots.c` dispatches to a
stand-in that reads its arguments from a0 to a3 and the guest stack and sets v0 (and v1
where the loader leaves a meaningful one), as `loader-services.md` sections 1 and 8 give
them (read). The stand-ins reach guest memory only through `mem_r*` and `mem_w*` with guest
addresses, so a bad pointer from the game goes to the bus and stops there.

- **Descriptors and drivers** (`fd.c`, section 2). 16 descriptors; 0 to 5 open at the start
  as the loader leaves them (`con:` three times, `cmos:`, `ioasic:`, `timer:`; read from the
  RAM dump); the lowest free one is handed out. `open` resolves the name through the loader's
  name table: `con:`, `cmos:`, `ioasic:`, `timer:`, `pic:`, `pci:`, and files. A driver is a
  `struct ldev` of open, close, read, write and ioctl; a routine the documents give no result
  for is NULL and stops.
- **Files** (`dev_file.c`, over `runtime/disk/fs.c`): open, read (in whole words: a count
  that is not a multiple of 4 returns 0, end of file returns 0xFFFFDBAE), write, close,
  find-first (the game's existence check, slot 0x038), unlink, rename and lseek, on
  partition 1, with the loader's return values (read).
- **Console** (`dev_con.c`): a write to fd 0 to 2 goes to the host's standard output (and to
  the trace); `--quiet` keeps it out of the terminal.
- **CMOS** (`dev_cmos.c`): 32,768 byte cells backed by the NVRAM file, created filled with
  0xFF (measured: the fill of a blank NVRAM in MAME), saved through a temporary file and a
  rename at the end of every run. ioctls 0x6300 to 0x6302 move the position (an address
  0xB6100000 + 4 × offset); read and write never advance it (read).
- **PIC** (`dev_pic.c`): the three words the loader read from the security chip before the
  hand-off, 0x0001E240, 0x000001D5 and 0x00001B88 (read from the RAM dump).
- **Timers** (`dev_timer.c`): the GT64010's timers 1 to 3 as 24-bit down-counters of 20 ns,
  on the virtual clock. A one-shot reads exactly 0x13FFFFEC once it has run out, a periodic
  timer wraps with its period (read), and a stopped timer holds its value (inferred).
- **IOASIC** (`dev_ioasic.c`): the four input ports at their no-input values, 0xBDFE and
  0xFFFF three times (measured, `inputs.txt`), which `ioasic_set_inputs` can replace; INTCTL;
  INTSTAT and the sound-board commands, which go to the DCS responder (below).
- **`pci:` and the GT64010** (`dev_sysctrl.c`): ioctl 0x7301 on the registers of DMA channel
  0, with the three control values the game writes (measured). The enable write walks the
  chain of 16-byte records (byte count, source, destination, next), checks every record, and
  copies each record's data through the guest-memory helpers, so a Voodoo destination reaches
  `bus_write` and the card; the card may hold the channel (below). Cause bit 4 is raised at
  the measured completion time, 2,663.890 ns + 1,939.399 ns per record not writing the frame
  buffer + 16.946 ns per frame-buffer byte (a fit to the 3,580 measured chains, within 5 ns
  of each; measured and inferred), or when the last word goes in, if later.
- **PCI configuration** (slots 0x0F0, 0x0F8, 0x108, 0x110 in `slots.c`, over the Voodoo's
  configuration space): the Voodoo is device 8 (measured); only the words and values the game
  read and wrote in MAME are answered or accepted.
- **Disk slots** (`diskslots.c`): 0x118 selects a partition (kept; the file system reads
  partition 1, the only one the game selects), 0x128 hands out the partition table at the
  loader's own address 0x800B7160, 0x158 reads raw sectors from the image. Transfers complete
  inside the call; the completion callback (slot 0x1D8) or the wake hook (slot 0x220) is
  called before the slot returns, which MAME's measurement showed harmless provided the slot
  never calls the game's wait routine (measured and inferred, `loader-services.md`, open
  question 5), and it never does. Since the whole-branch review of M2 (finding M9) the hook
  is called as the board calls it, at interrupt level (`exc_hook_call`, `irq.c`): Status has
  EXL set for its duration (measured: `disk-hooks.txt`, Status 0x30015C03 in all 3,122
  callback and 64 wake calls of facts-A2), so an interrupt that falls due is delivered at the
  first check point after the slot returns, and the registers the dispatcher saves (every GPR
  but k0, HI, LO, Status, the FPRs, FCSR) are put back afterwards by the dispatcher's own
  restore (`regs_give`). Two differences from the board remain: the hook runs on the caller's
  stack (the board's sp in every call measured is 0x800C1358, on the exception stack), and no
  frame is written. The 150 s boot trace, overlay, NVRAM and RAM hash are byte for byte the
  same before and after the change (measured on the Mac, 2026-11-20).
- **Handler registrations** (0x1E0, 0x1E8, 0x1F0, 0x1F8, 0x200): fill the tables of the
  previous section. 0x1E8 also sets the FCSR enable bit, as the loader does (read); 0x200
  returns line × 4 (read and measured).
- **Exit and exec** (0x0D8, 0x198) stop, naming the status or the file.

Every request the documents do not show the game making stops, through `ld_stop`, with the
slot, the request, a0 to a3 and `ra` (the generated code stores no pc at a loader call, so
`ra` identifies the call site). The tests, in `tests/test_loader.py`: descriptors
(`test_close_then_ioctl`, `test_open_pci_is_6`, `test_open_nosuchfile`,
`test_open_resolution`, `test_loader_reset`); files (`test_findfirst`, `test_read_rules`,
`test_lseek_words`, `test_creat_write_unlink_rename`, `test_file_access_modes`,
`test_long_file_names_stop`); the console (`test_console`, `test_console_echo_off`); the CMOS
(`test_cmos`, `test_cmos_saved_at_stop`, `test_cmos_stops`, `test_cmos_reload_rejected`,
`test_cmos_file_errors`); the PIC (`test_pic`); the timers (`test_timer_oneshot`,
`test_timer_periodic`, `test_timer_stopped_holds`, `test_timer_stops`,
`test_timer_read_write`); the IOASIC (`test_ioasic`, `test_input_defaults`,
`test_input_ports`, `test_entry_hardware`); the registrations and other slots
(`test_irq_set_handler`, `test_gt_set_handler`, `test_fpe_set_handler`, `test_misc_slots`,
`test_exit_exec`); stops and bad pointers (`test_unknown_requests_stop`,
`test_generic_ioctls`, `test_bad_guest_memory`, `test_pci_read_stops`); slots called from a
handler (`test_slots_from_handler`). In `tests/test_hw.py`: `pci:` and DMA (`test_dma0_chain`,
`test_dma0_stops`, `test_dma0_delay_model`, `test_dma0_delay_fits_mame`); the PCI slots
(`test_pci_probe`, `test_pci_probe_other`, `test_pci_config`, `test_pci_calls_like_mame`);
the disk slots (`test_partition_table`, `test_raw_read_callback`, `test_raw_read_wake`,
`test_disk_hooks_store`, `test_disk_slots_stop`, `test_movie_frame_read`,
`test_hook_keeps_fp_registers_and_fcsr`, `test_vblank_during_a_hook_is_delivered_after_the_slot`).

## The bus and the devices

The generated code sends every access that is not RAM to `bus_read` and `bus_write`
(`runtime/core/bus.c`). kseg0 and kseg1 are folded to physical addresses; kuseg (outside the
RAM the loader's TLB maps), kseg2 and kseg3 stop, as they would raise a TLB exception on the
board. Then:

- **0x08000000 to 0x08FFFFFF: the Voodoo** (below).
- **0x13000000: the sound FIFO.** Word writes only; the words are counted and fill the
  responder's FIFO state, and are otherwise dropped (`misc.c`).
- **0x16100000 + 4n: CMOS byte n**, read with `lbu` only (the game's CRC loop at 0x801458C0,
  measured and read). A direct write stops: the game writes the CMOS only through `cmos:`.
- **0x16800000 to 0x1680001F: the gun.** The eight byte registers return the values MAME's
  gun gave with no input; the `sw` at offset 0 and the `sb` at offset 4 are accepted with no
  effect (measured, `hardware-usage.md`, "Measured values the runtime answers with").
- **0x17F00000**: a byte register starting at 0x0E, the value the game's one read returned
  (measured), keeping what is written.
- Anything else stops, naming the address, size, direction, value and the pc as last stored.

Files: `runtime/core/bus.c`, `runtime/hw/misc.c`, `runtime/hw/gun.c`, the CMOS cells in
`runtime/loader/dev_cmos.c`. Tests in `tests/test_hw.py`: `test_unmodelled_bus_stops`,
`test_sound_fifo`, `test_cmos_direct_read`, `test_gun`, `test_asic_reset_register`,
`test_gun_and_asic_like_mame`, `test_hw_reset`.

### The Voodoo

The card is modelled from MAME 0.276's BSD-3-Clause Voodoo sources, read as reference, with the
code written fresh; [hardware-usage](hardware-usage.md), "The Voodoo model", gives every rule
with its mark and its source file.

- **Registers and status** (`voodoo_null.c`). Writes are executed in time order. A register
  the game reads is either storage (it reads back what was last executed; a read before any
  write stops), status (computed: the PCI and memory FIFOs' free space, busy, retrace, the
  displayed buffer, the swaps pending), or, for fbiInit2 under PCI initEnable bit 2, the DAC's
  latched data. Any other read stops. The alternate register map (address bit 21, under
  fbiInit3 bit 0) is decoded as MAME's model renumbers it.
- **Busy and the FIFOs.** A command (triangle, fastfill, swap) keeps the card busy for its
  clocks at 50 MHz. While it is busy, writes queue in the PCI FIFO (128 entries) and the
  memory FIFO behind it, and run in order as the card frees. The card is brought up to date
  lazily, at each access, by running what has come due. A write that would overflow a FIFO
  stops (MAME's model asserts or loses it).
- **Holding the writer.** At the FIFOs' water marks the card holds its writer (from MAME's
  model). A CPU store is held by advancing the virtual clock (`vclock_advance`) to the next
  time the hold may end, with the clock's events, the retrace among them, running on the
  way; the interrupt they raise is taken after the store. The GT64010's DMA channel is held
  instead of the CPU: it asks `voodoo_holding()` before each word and, while held, polls at
  `voodoo_hold_check_ns()`, and the CPU runs meanwhile.
- **The frame buffer** (`voodoo_fb.c`): 2 MB of 16-bit pixels laid out by fbiInit1 and
  fbiInit2, the linear-frame-buffer addressing with its aliasing, raw RGB 565 and depth
  writes, the 4 × 4 and 2 × 2 dither, fastfill, MAME's triangle fill rule, and the swap.
- **The texture unit** (`voodoo_tmu.c`): one TMU with 4 MB, texture memory placed as MAME's
  model places it, point-sampled RGB 565 from one LOD without perspective, the pass-through
  texture combine and the TMU configuration send.
- **What is drawn: the capability rule.** At each triangle the model reads the mode
  registers and draws only if every feature they enable is one it implements (flat colour or
  the texel, dither, clipping, the y origin, the RGB write). Otherwise the triangle is
  counted under the first missing feature in pipeline order (`DROP_*` in `hw.h`) and
  dropped, and the buffer it would have drawn into is flagged; an LFB write in a mode the
  model does not implement is counted and dropped the same way. An LFB read of a flagged
  buffer, or of a pixel nothing modelled has written since reset, stops. This is how start-up
  works without M3's renderer: the game's three start-up checks read back pixels the card
  drew (measured), and all 258 of their triangles are of the drawable kind. In the 150 s
  attract run every one of the 126,984 later triangles and every movie-frame LFB write was
  dropped, fastfills were drawn, and no LFB read met a flagged buffer (the run would have
  stopped) (the port's run, its trace totals).
- **Video timing.** The card owns the vertical retrace, which is the board's VBLANK. It has
  none until the game programs hSync, vSync, backPorch and videoDimensions; then the frame is
  291 rows at 15,750 Hz, 18,476,190 ns, and the retrace 4 rows, 253,968 ns (from MAME's
  model; both within the measured bounds). Each retrace start is a clock event that also
  calls `vblank_raise`.

The tests: in `tests/test_hw.py`, the registers and status (`test_voodoo_registers`,
`test_voodoo_status`, `test_lfb_readback`, `test_dac_readback`, `test_degenerate_video_timing`,
`test_clock_backwards`, `test_voodoo_reads_like_mame`, `test_dac_reads_like_mame`) and the
holds (`test_cpu_store_held_until_the_card_drains`,
`test_hold_lets_the_retrace_interrupt_wait`, `test_dma_chain_held_in_pieces`); in
`tests/test_voodoo_fb.py`, the drawing (`test_flat_triangle`, `test_lfb_aliasing`,
`test_non_flat_triangle_dropped`, `test_dropped_lfb_write_flags_buffers`,
`test_textured_triangle`, `test_alternate_map`) and the replay of MAME's capture of the first
2 s after the entry (`test_memory_test_like_mame`, `test_dither_ramp_like_mame`,
`test_tmu_check_like_mame`, `test_retrace_phase_like_mame`, and `test_reads_like_mame_full`,
which returns all 274,967 measured reads). The hold from the PCI FIFO's low-water mark and the
release with the memory FIFO off have no test.

### The DCS responder

The DCS sound board has its own ADSP-2115 processor, which M5 will emulate. M2 has a
responder instead (`dcs_stub.c`, reference [dcs-handshake](dcs-handshake.md)): a state machine
over the words the game sends through the `ioasic:` sound ioctls (a reset through SOUNDCTL,
the boot loader's 0x001A blocks and 0x002A, the program's 0x55D0 and 0x55D1 blocks, 0x55D4 and
the commands without a reply). A block's reply is the 16-bit sum of its data words (a rule
inferred from the trace and holding for all 30 blocks, measured); 0x000A, 0x000C and 0x0223
are recorded values with no known rule. Commands with fixed argument words accept only the
measured words. Any other command word stops. Since MAME's 600 s capture (the whole-branch
review's finding I2; `dcs-handshake.md`, "After 150 s") it also takes 0x00BB, 0x00B7 and 0x0059
and a repeated 0x00B9, with 0x00B9's arguments, only while an unread word already waits: then
nothing the game reads depends on whether they reply, and the queue's depth becomes unknown,
so an acknowledgement after them stops.

Its timing is not the board's. A reply appears after the shortest delay MAME showed for its
kind, bit 0x80 (the board can take a word) is always set, and bit 0x8000 is set throughout the
state in which MAME first shows it. So the responder is never slower than the board, and the
game polls fewer times than in MAME. The sound FIFO's fill gives SOUNDSTAT bits 0x08 to 0x20
and INTSTAT bit 0x08; the FIFO is never drained (inferred from MAME's one IP3 interrupt in
150 s). The tests are in `tests/test_dcs.py`: `test_no_reply_without_command`,
`test_unknown_command_stops`, `test_block_rules`, `test_reply_timing`, `test_fifo_and_ip3`,
`test_pinned_arguments`, `test_soundctl_measured_values`, `test_intctl_enables_limited`, and
`test_replays_mame_exchange` (the replay of MAME's 2.9 million recorded events),
`test_commands_whose_reply_cannot_be_seen` and `test_replays_mame_exchange_600` (the 600 s
capture by the same standard).

## The disk and the user's files

Files: `runtime/disk/image.c`, `runtime/disk/fs.c`, `runtime/disk/disk.h`, and the NVRAM file
in `runtime/loader/dev_cmos.c`. Tests in `tests/test_disk_c.py`: the overlay
(`test_overlay_created_on_first_open_and_holds_writes`, `test_overlay_for_another_image_is_refused`,
`test_damaged_overlays_are_refused`, `test_without_an_overlay_writes_fail`,
`test_bad_raw_images_are_refused`), the file system (`test_find_read_and_handles`,
`test_write_read_seek_unlink_rename`, `test_unlink_zeroes_the_first_name_word`,
`test_truncating_open`, `test_full_directory_is_extended`), damaged input
(`test_bad_first_block`, `test_chain_that_loops`, `test_chain_leaves_the_partition`,
`test_bad_directory_link`, `test_directory_blocks_that_link_in_a_circle`,
`test_extension_ignores_a_link_past_the_length`), crash ordering and host errors (since the
whole-branch review of M2: `test_uncounted_trailing_record_is_ignored`,
`test_host_error_is_reported_not_returned`, and `CrashOrderTests`, a kill before every sector
write of a create, an extension, a truncation, an unlink and a directory extension) and, with
game data, `test_every_file_matches_the_python_reader`, `test_write_round_trip`,
`test_python_reads_the_written_partition` and `test_raw_image_unchanged`
(`tests/test_boot.py`: `test_host_write_error_stops`).

- **The raw image** is opened read-only and is never written (`runtime/disk/image.c`).
- **The overlay** holds every sector the game writes. Its header is a 512-byte sector: the
  magic "CEOVRLAY", version 1, the record count, the raw image's size and the SHA-256 of its
  first sector; then one 520-byte record per sector (the LBA, a zero word, the 512 bytes).
  The first write of a sector appends a record, syncs the file, and then updates the count;
  later writes rewrite the record. The count is the commit point: bytes after the counted
  records are a write that did not commit (a kill between the record and the count), which
  opening ignores and the next new record overwrites, so a kill loses at most the sector being
  written for the first time. An overlay made for another image, or a truncated one (shorter
  than its count needs), is refused with a message (exit 2).
- **Host errors.** A failed or short host read, write or sync of the raw image or the overlay
  is not the game's disk error: from the start of the run it stops (exit 3) with a message
  naming the file, the sector and the system's reason, instead of reaching the game as the
  loader's -1 (finding M2). The NVRAM save stops the same way.
- **The file system** (`fs.c`) follows `disk-format.md` and the loader's routines (read): 170
  entries per directory block, an entry whose first name word is zero is free, the link to the
  next block at byte 4092, 0 ends the directory. Allocation is first fit; the date and time it
  writes are a constant, not the loader's clock. A damaged chain or directory is bounded and
  refused rather than followed. 1,523 files on partition 1 (static: counted by the C and the
  Python readers alike). It does not update the disk's own checksum table `FILESYS.CHK` when
  the game writes a file, so a file the game rewrites no longer matches its entry there;
  whether the board's loader updates it is not read. Its writes are ordered so that a kill between any two leaves a
  consistent file system (finding I5): when blocks are allocated, the chain table is written
  before the directory entry or link that reaches them; when they are freed, the entry is
  written before the chain table releases them. The worst a kill leaves is allocated blocks
  nothing refers to. The order holds against a kill of the process; against a power cut it
  holds for first writes of sectors (synced) but not for rewrites in place, which are not
  synced one by one.
- **The NVRAM file** is the CMOS, 32,768 bytes; one of another size stops. A second run on the
  same NVRAM is a non-blank CMOS, and the game then does not print "reseting high scores"
  (`test_boot.py`, `test_user_files`).

## Stopping instead of guessing

The rule throughout is that the runtime never answers a request it does not model by
guessing. An unknown slot, ioctl command, argument, bus address, register read, `break` code
or device state stops the program with `carnevil: stop:` and a message naming the guest pc
(or the slot's `ra`), the request and its values, and exit status 3. The only escape is
`config/runtime.expected.toml`, a list of interpreter entries and tolerated requests, each
with a reason; it is empty, and `test_nothing_unexplained` (`tests/test_boot.py`) fails on an
entry the run does not use. Files: `rt_stop` in `runtime/core/exc.c`, `ld_stop` in
`runtime/loader/slots.c`, `hw_stop` in `runtime/core/bus.c`; the stop messages of the clock,
interrupt and exception code are checked by `test_stop_paths`, those of the stand-ins and
devices by the stop tests named in their sections.

A stop is therefore the expected result of driving the game anywhere attract mode with a
blank NVRAM and no input did not go in MAME: play, a coin, the operator menus, a long run
whose later states were never measured. The message names what to model next.

## Reading a trace

`--trace FILE` writes one line per event, `TIME KIND FIELDS...`, with TIME the virtual time in
ns since the entry and numbers in hexadecimal with 0x unless the format says decimal. The
format is the top comment of `runtime/main/trace.c`; `tests/test_boot.py` reads it
(`test_timed_run`, `test_stop_is_traced`, `test_frames`, `test_deterministic` and the
comparisons with MAME). The kinds:

- `console FD N TEXT`: a console write, escaped as `mame/m2-facts/console.txt` is.
- `open MODE FD NAME`: an open, with the mode as passed, the fd returned and the name.
- `loader N SLOT IRQ A0 A1 A2 A3 SP V0`: one of the first 2,000 loader calls, written when it
  returns (IRQ 1 inside an exception). Later calls are only counted.
- `dma RECORDS N_OTHER LFB DELAY`: a DMA chain's completion, its shape and its delay in ns.
- `spin N`: the scheduler's VBLANK-wait spin count (the word at 0x801A36CC), when it changes.
- `switch KIND RECORD FN`: a host switch, the current-task record and the task's function.
- `task create|exit RECORD FN`: a change of the game's task list, read from the list head
  0x801A3110 at each host switch, so a creation is timed at the next switch, not when it
  happened.
- `interp ADDR`: the interpreter entered (none in attract mode).
- `pending ...` and `stop STATUS MESSAGE`: at a stop, the loader calls still running and the
  message.
- `end seconds N` or `end frames N`, then the totals: `count vblank`, `count spin_max`,
  `count tasks_max`, `count loader_calls`, per-slot and per-ioctl counts, Voodoo writes per
  register by the standard map (`voodoo_reg`) and through the alternate map (`voodoo_alt`),
  texture writes, the card's holds (`hold_cpu`, `hold_cpu_ns`, `hold_dma`), dropped draws by
  reason (`dropped I N WHY`), sound FIFO writes, words the sound board took as data; and one
  `alive RECORD FN` per task still on the list.

A 20 s run writes about 400 KB of trace, a 150 s run about 1.8 MB (measured, 2026-11-20).
To find where a run went wrong, compare its `console`, `open` and `task` lines with
`console.txt`, `opens.txt` and `tasks.txt` of `mame/m2-facts/`, and its first `loader` lines
with `loader-first.txt`, in order; never by time.

## Changing the runtime

- The tests drive the devices and stand-ins through drivers in `tests/harness/`
  (`machine_driver.c`, `tasks_driver.c`, `loader_driver.c`, `hw_driver.c`, `disk_driver.c`),
  each of which reads commands on stdin and prints results; the Python tests build a driver
  with the global flags and UBSan into a cache under the system temp folder
  (`tests/charness.py`) and compare what it prints. Guest
  code for synthetic tests is assembled with `carnevil_tools.vectors.assemble`.
- A new behaviour is added only from evidence: a measurement (a MAME capture under
  `$CARNEVIL_WORK/mame/`), a reading of the code, or MAME's model, cited in the doc and in the
  code, with a test in the owning test file. Until the evidence exists, the request stops.
- `make carnevil` builds the program, `make carnevil-ubsan` the same under the undefined
  behaviour sanitizer; `tests/test_boot.py` runs the game for 150 s and compares it with
  MAME (18 tests, skipped without the game data).
- State lives in one struct per file, reset by the file's reset function; the four resets of
  the start-up are the only order that matters (above).
- Only one thread runs at a time, so the runtime has no locks besides the baton. Anything that
  would run on its own thread (M3's window, M5's sound processor) needs a lock or a hand-off
  of its own.

## What the runtime does not do

- Draw a picture, play sound, or take input: the Voodoo draws only what the start-up checks
  read back, the DCS is a responder, the guns and ports return their no-input values.
- Run at the board's pace or in real time: the clock is virtual and its pace is not the
  board's (above).
- Model the IDE controller, the GT64010's PCI side and its other DMA channels, the UART, the
  security PIC as registers: the stand-ins replace the loader that drove them.
- Anything attract mode with a blank NVRAM did not reach in MAME: such a request stops.
