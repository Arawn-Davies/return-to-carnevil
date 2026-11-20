# Loader services as GAME.EXE sees them

This page specifies, for milestone M2, every service of the boot ROM's resident loader that
`GAME.EXE` calls, in enough detail to write a native stand-in for each and a test against it.
It covers the 29 slots of the stub block (`boot-handoff.md`), the descriptor and driver model
behind the file slots, every ioctl command the game issues, the disk paths, sound, the
interrupt and exception paths, and the loader state the game inherits.

Facts, addresses and constants only: no instruction listings. Every statement is tagged:

- **read**: read from the loader's (or the game's) code with the disassembler;
- **dump**: taken from MAME's RAM dump at the game's entry (`mame/dumps/ram-at-game-entry.bin`
  on the share; the dump at the first loader call, `ram-at-first-loader-call.bin`, was used to
  check that nothing below changed in between, and is cited where it differs);
- **measured**: measured in an earlier MAME run, with the page that reports it;
- **inferred**: a conclusion drawn from the above, not observed.

The loader code was read from the RAM dump (physical 0x00000000 to 0x00015C40 holds the
loader as it runs; `boot-handoff.md` shows that the first 0x240 bytes equal the loader's ROM
image). Function discovery over that image gives the same 333 functions as M0. Addresses are
KSEG0 unless written as 0xA.../0xB... (KSEG1). "Bus word n" of the IOASIC is 0xB6000000 + 4n,
named as in `hardware-usage.md` (CarnEvil's unlocked order).

What this page does not cover, or covers only in part, is listed at the end under "Not done".

## The loader is a DOS-style C library

The service layout and return conventions are those of a Watcom-style DOS runtime (read):

- the file slots split into a POSIX-like layer (open, close, read, write, ioctl, lseek, isatty)
  and a `_dos_*`-like layer that returns 0 for success and writes results through pointers
  (find-first, find-next, create, get attributes, get file time, date and time);
- devices are opened by name through a name table; a name that matches no device is a file;
- the error values of the file system are small negative numbers near -9290 (0xFFFFDB..),
  returned as they are; there is **no errno word** in the loader. The game keeps its own errno
  at 0x801E0958, written only by its own wrappers (read: 0x80145318 and 0x80145698 write 4 and
  10 there), never by the loader.

## 1. The 29 slots

Each entry gives a C-like signature, the arguments, the return value, the memory the call
writes that the game can see, the state it changes, whether it blocks, and its effect on
Status. "Leftovers" are the caller-saved registers at return where they were cheap to tell.
Unless an entry says otherwise, a slot does not write Status and returns with the Status it
was called with (read: none of the routines below executes `mtc0` to Status except where
noted). The exceptions are slot 0x200 (sets the line's IM bit and IE) and every slot call
that issues a disk transfer (file open, read, write and close, find-first, unlink and rename
when they miss the caches, and slot 0x158), which clears and then sets IM4 and sets IE
(section 4).

### 0x018 close — `int close(int fd)` (0x8000B1D0, read)

- fd: index into the descriptor table (0 to 15).
- Returns -1 if fd is 16 or more, or the descriptor is not open (flags word 0). Otherwise
  calls the driver's close routine with the descriptor and returns what it returns, then sets
  the descriptor's flags to 0 (the descriptor becomes free).
- Driver close results: `file` returns 0, or a file-system error (0xFFFFDBB8 if the file was not
  open, 0xFFFFDBB3 if its directory block was invalid, or a block-cache error); every other
  driver the game can close returns 0 (shared return-0 routine 0x80006984).
- For a file opened for writing, close writes the directory entry (length, date/time word)
  and flushes the dirty cached blocks to disk (0x800094B4, 0x80007BC4; read).
- The fd map at 0x800B83A0 (below) is not cleared by close (read), so it keeps a stale entry.
- Does not block, except for the disk writes of a file flush (see section 4).
- Leftovers: v0 is the result; nothing else is meaningful.

### 0x028 create — `int creat(const char *name, unsigned attr, int *fd_out)` (0x8000B240, read)

- Calls open(name, 1) (write-only), stores the result (fd or -1) at `*fd_out`, returns 0 when
  the fd is 0 or more, else -1. `attr` (a1) is ignored: open overwrites a1.
- The game calls it only from its open wrapper (0x80145758), when open failed and the game's
  open flags have bit 0x100 set (create; read).
- Write-only open of a file truncates it: see the file driver (section 2).

### 0x038 find-first — `int findfirst(const char *pattern, unsigned attr, struct find_t *buf)` (0x8000B70C, read)

**Resolved.** This is `_dos_findfirst`.

- pattern: a file name, possibly with `*` and `?`; attr: ignored.
- It parses the pattern into `buf` (0x8000B33C): bytes 0 to 11 hold the pattern as an
  upper-cased, space-padded `NNNNNNNN.EEE`, and the 32-bit word at bytes 16 to 19 (written
  byte by byte, unaligned) is a mask with bit i set when character i must match (cleared for
  `?`, and for the positions a `*` covers). It then rewinds the directory scan
  (0x80009564 on the scan state at 0x800B8628) and calls find-next (slot 0x040, below) on `buf`.
- Returns 0 when a matching file was found and `buf` was filled, -1 when none matched.
- `struct find_t` as filled by find-next (read, 0x8000B94C): bytes 0 to 20 reserved (the
  pattern and mask above); byte 21 the attribute byte; bytes 22 to 23 the time half and
  24 to 25 the date half of the directory entry's date/time word; bytes 26 to 29 the file size
  in bytes (u32, little-endian, unaligned); bytes 30 to 42 the name, NUL-terminated (at most 12
  characters plus NUL). This is the Watcom `find_t` layout.
- The game's only call: `_start` → 0x801412D0 → 0x80145318 → slot 0x038 with
  pattern 0x80174EC0 = `"game.env"`, attr 0, buf 0x807FFEC0 (on its stack). The game's
  wrapper 0x80145318 swaps a1 and a2 (its own signature is `(name, buf, attr)`) and returns -1
  with its errno 4 if name or buf is null (read).
- What the game does with it (read, 0x801412D0): on 0 it reads the size from `buf` + 26,
  allocates that much (failure prints "Can not allocate memory for environment strings"),
  opens `game.env` with mode "rb" and reads it as environment strings; on non-zero it skips the
  environment entirely.
- `GAME.ENV` is not on partition 0 (the extracted file list has no such file; dump and disk),
  so on this disk the call returns -1. **The stand-in must return non-zero for `game.env`**
  (and may leave `buf` untouched) unless a host-side environment file is wanted.
- Scan state: the directory iterator at 0x800B8628 (16 bytes) and the current entry's name at
  0x800B8638 are loader-private; the game never reads them.
- Blocks: reads the directory through the block cache (section 4), so it can do disk reads.
- Leftovers at return (read): v0 0 or -1; the other caller-saved registers hold scan
  temporaries, not documented.

### 0x088 open — `int open(const char *name, int mode)` (0x8000BB68, read)

- Resolution, descriptor layout and flags: see section 2.
- mode: only bits 0 to 3 are used. 0 → read (flags 1), 1 → write (flags 2), 2 → read/write
  (flags 3); any other value leaves the flags word 0, so the descriptor is handed back as fd but
  stays free and every later call on it fails (read). The game's wrapper 0x80145698 passes
  `mode & 0xF3`, so the game's text/binary bits never reach the loader (read).
- Returns the fd (the descriptor index, 0 to 15) or -1 when no descriptor is free, no device or
  file driver matches, or the driver's open fails.
- Writes: the descriptor; `fdmap[fd] = fd` at 0x800B83A0 + 4·fd; for a file, the file driver
  upper-cases the caller's name string **in place** (0x80008A20, 0x80008904; read): the game's
  buffer changes.
- Blocks: a file open reads the directory (disk reads); device opens do not.

### 0x090 read — `int read(int fd, void *buf, unsigned n)` (0x8000BCB0, read)

- Returns -1 for a bad or closed fd. Otherwise it first polls every descriptor whose flags
  have bit 0x08 (the console descriptors 0 to 2) by issuing ioctl(fd, 0x6602, 0) on each
  (0x80006800), which drains the UART receiver into the console ring buffer; then it calls the
  driver's read routine (slot 2) with (descriptor, buf, n) and returns its result. If the
  descriptor has flag 0x10 it instead stores buf and n in the descriptor and calls the driver's
  slot 5 with (descriptor, 1); every driver's slot 5 is the return-0 routine and nothing sets
  flag 0x10 (read), so this path is dead.
- Driver results: section 2 (`file`: bytes read, 0 if n is not a multiple of 4, 0xFFFFDBAE at
  end of file or on error; `cmos`: n clamped; `tty`: blocking console input).

### 0x0C0 write — `int write(int fd, const void *buf, unsigned n)` (0x8000BE10, read)

- As read, with the driver's write routine (slot 3) and the flag-0x10 path passing 2.
- Driver results: section 2. Console writes busy-wait on the UART (section 2, `tty`).

### 0x0C8 ioctl — `int ioctl(int fd, int cmd, int arg)` (0x8000BEB0, read)

- Returns -1 for a bad fd or a closed descriptor (flags 0).
- Handled here, for every device: 0x6601 sets (arg non-zero) or clears (arg 0) flag 0x04
  (non-blocking console read) and returns 0 without calling the driver; 0x6603 sets flag 0x20,
  0x6604 clears it, and both are then passed to the driver as well.
- Everything else is passed to the driver's ioctl routine (slot 6) as (descriptor, cmd, arg)
  and its result returned. A driver whose slot 6 is the return-0 routine (led, mdiag, disk,
  scsi) returns 0 for every command.
- The commands the game issues are in section 3.

### 0x0D8 exit — `void _exit(int status)` (0x80005048, read)

**Correction to `boot-handoff.md`**, which names this slot "reset all handler tables". The slot
jumps to 0x80005048, which is a `break 0x402` (word 0x0001008D); the reset of the handler
tables is the next routine, 0x80005050, which load-and-run calls. The game's only call is from
0x80143DD4, the end of its `exit()` (0x80143D78: it runs the atexit chain, flushes, then calls
the slot with the status in a0). `break 0x402` enters the debug monitor (section 6), which on
this board (no SCSI debugger card) does not return to the game. **Stand-in: terminate the
runtime with the status in a0.**

### 0x0E0 isatty — `int isatty(int fd)` (0x80006754, read)

- Returns 1 if the descriptor's name-table entry is named exactly `con:` (descriptors 0 to 2 at
  the hand-off), else 0; 0 for a bad fd. It does not check that the descriptor is open.

### 0x0E8 flush data cache — `void dcache_flush(void)` (0x80004E3C, read)

- Index Writeback Invalidate over 0x80000000 to 0x80008000 in 32-byte lines: the whole 32 KB
  primary data cache is written back and invalidated. No instruction-cache operation. No
  return value (v0 not set). The game calls it before starting GT64010 DMA (0x80168064,
  0x80168274, 0x80168434; read). **Stand-in: no-op.**

### 0x0F0 PCI config read — `int pci_cfg_read(int reg, int dev, u32 *out)` (0x8000F95C, read)

- reg: the byte offset of the configuration register; it is shifted right 2. dev: the PCI
  device number. Writes the 32-bit configuration word to `*out` and returns 1.
- The access is CONFIG_ADDRESS (0x0C000CF8) = 0x80000000 | (dev & 0x1F) << 11 |
  (reg & 0x3F) << 2, then CONFIG_DATA (0x0C000CFC); if GT64010 INTR_CAUSE bit 18 is set after the
  read it is cleared (0x8000F774). Caller: Glide `sst1Init` code (20 sites).

### 0x0F8 PCI config write — `int pci_cfg_write(int reg, int dev, const u32 *value)` (0x8000F930, read)

- As 0x0F0, writing `*value`. Returns 1. 42 call sites, Glide.

### 0x108 PCI vendor probe — `int pci_probe(unsigned vendor, unsigned devid, void *unused, int dev)` (0x8000E7D0, read)

- Reads configuration word 0 of device `dev` and returns 1 if its low 16 bits equal `vendor`,
  else 0. `devid` and the third argument are ignored.

### 0x110 PCI device set-up — `void *pci_map(unsigned vendor, unsigned devid, void *unused, int *dev_out, int index)` (0x8000E80C, read)

**Resolved.**

- Scans PCI device numbers 6 to 9 for the `index`-th device (the fifth argument, from the
  caller's stack at sp + 16) whose vendor ID equals `vendor` (`devid` is ignored, as in 0x108).
- Found: reads configuration register 1 (the result is discarded), writes register 4 (BAR0) =
  0x08000000, stores the device number at `*dev_out`, returns 0xA8000000 (the KSEG1 address of
  the Voodoo's registers).
- Not found: stores 10 at `*dev_out` and returns 0.
- The game calls it twice (0x801570F8, 0x801571D8) and keeps the base at 0x801E5DE0
  (`hardware-usage.md`). **Stand-in: return 0xA8000000 and the Voodoo's device number for
  index 0 and the 3dfx vendor ID; return 0 and 10 otherwise.** Which device number the Voodoo
  has on the board was not read (open question 7, answered below).

### 0x118 select partition — `int disk_select_partition(int n)` (0x80010368, read)

- The partition table copy is at 0x800B7160 (slot 0x128). The current-partition pointer at
  0x80015AD0 becomes table + 8 + 12·n, and 0 is returned; if n is greater than the table's
  count word (17) it returns 2 and changes nothing (n equal to the count is accepted).
- The current partition is a 2-word record {start sector, size in sectors}. Index 0 is the
  whole disk: table + 8 is {0, total sectors} because the loader overwrites the table's word at
  + 0xC with the drive's sector count (0x0080730C in the dump; the disk has 0x00603900 there).
  Index n ≥ 1 is the disk's partition-table entry n - 1 ({start, size} at table + 0x14 +
  12·(n-1)). **So the loader's partition numbers are 1-based: partition 1 is the disk's part0,
  the game file system** (read, dump).
- At the hand-off the pointer is 0x800B7174, partition 1 (dump).
- All four game calls pass 1 (0x800C50E8, 0x800F5DB0, 0x800F625C, 0x800F6F60; read).
- It does not re-mount the file system or flush any cache (read).

### 0x128 partition table — `const void *disk_partition_table(void)` (0x800103BC, read)

- Returns 0x800B7160, the in-RAM copy of the disk's first sector header: "TRAP" magic, count
  (17), and the 12-byte entries, with the patched total-sector word at + 0xC (dump, compared
  with `carnevil.raw`: identical except that word).
- The movie player computes an absolute sector as `word at (table + 8 + 12·n)` (partition n's
  start) plus an offset (read, 0x800E7DF4 to 0x800E7E30). The stand-in must return a pointer to
  an identical structure in game-visible memory.

### 0x158 raw sector read — `int disk_read_sectors(u32 lba, void *buf, int count)` (0x800112F4, read)

- lba: absolute sector number on the disk (not partition-relative); count: 512-byte sectors.
- With the PIO flag 0x80015388 clear (it is 0 at the hand-off, dump), the call goes to the
  queued DMA path 0x80011DE4 (section 4); with it set, a polled READ MULTIPLE (0xC4) loop.
- Return value on the DMA path: the call's 16-bit result, sign-extended: count when the
  transfer completed synchronously, 0 when it was queued asynchronously (a completion callback
  or a process hook is installed), -1 when queueing failed. Out-of-range requests (lba or
  lba + count at or beyond the drive size) are not transferred, call the completion callback
  (if any) with `count`, and return count. The PIO path returns 1 or 0.
- Callers: the movie player (0x800E7E2C) and the game's sound streamer (0x8013FDAC,
  0x8013FDF4, 0x8013FE6C), which reads into 0x801FAD70 + 1 KB·k (read).
- Blocking, interrupts and callbacks: section 4.

### 0x190 unlink — `int unlink(const char *name)` (0x80009A80 → 0x8000884C, read)

**Resolved.**

- Converts the name to the directory's 8.3 form, looks it up; if found, clears the entry's first
  word (the name), frees the file's block chain (0x80007E68 from the entry's first block),
  flushes the directory, and sets the two words 0x800B8674 and 0x80096350 to 3 (file-system
  scan/cache state, loader-private).
- Returns 0 on success and also 0 when the file does not exist; a negative file-system error
  otherwise. Writes the disk. Game caller: 0x80143C38 (`remove()`), not seen executing.

### 0x198 load and run — `int exec(const char *name, void *load, const u32 args[4])` (0x8000E930)

As in `boot-handoff.md` (read there). The game calls it once (0x800C6990) with `diag.exe`
after removing the disk hooks (slot 0x220 with zeros) and calling 0x80151AC0. It does not
return on success. **Stand-in: out of scope for M2; stop the runtime with a message naming the
file.**

### 0x1A8 argument block — `u32 *exec_args(void)` (0x8000EE30, read)

- Returns 0x800B6E30, the 4 argument words load-and-run copied (all zero at the hand-off,
  dump). The game calls it from `_start` and passes the result to `main`, which ignores it.

### 0x1B8 rename — `int rename(const char *old, const char *new)` (0x80009C9C → 0x80008B74, read)

**Resolved.**

- Converts both names to 8.3 form, looks up `old`; if not found returns 0xFFFFDBB7 (-9289);
  otherwise writes the new 12-byte name into the directory entry in its block, marks the block
  dirty and flushes. Returns 0, or a negative error from the lookup or the flush. It does not
  check whether `new` already exists. Game caller: 0x80143D60, not seen executing.

### 0x1C0 lseek — `long lseek(int fd, long offset, int whence)` (0x80009CBC, read)

**Resolved.**

- whence 0, 1, 2 as SEEK_SET, SEEK_CUR, SEEK_END; any other whence, and a bad fd, return 0.
- The file system positions in 32-bit words: the offset is divided by 4 (arithmetic shift, so
  the low two bits are dropped) and the result is the new position × 4, in bytes.
- SEEK_SET: position = offset/4. SEEK_CUR: position = current + offset/4. **SEEK_END ignores
  the offset**: the position becomes the file length.
- It does not check that the fd is a file: it uses the descriptor's per-fd file block
  (0x80016040 + 44·fd) whatever the driver, so seeking a device descriptor operates on an
  unused block (read). Seeking beyond the end fails inside the file system (0xFFFFDBAF) and the
  return value is then the stale local (initialised 0 for SEEK_SET; inferred from the code, the
  error paths of 0x80008F64 were not read to the end).
- The game's only call (0x80145388) is its append-mode open: lseek(fd, 0, 2).

### 0x1D8 set disk completion callback — `void disk_set_callback(void (*cb)(int status))` (0x80012718, read)

**Resolved.**

- First waits for the disk request queue (head 0x800158C4, tail 0x800158C8) to drain, by
  spinning up to 100,000,000 iterations (0x05F5E100) on head ≠ tail. The queue empties only as
  IDE completion is serviced, which happens in the loader's default IP4 interrupt path
  (section 6), so **this wait needs interrupts**. On timeout it resets the queue (head = tail =
  0, 0x800126E4) and resets the block/sector caches (0x800077D8).
- Then stores `cb` at 0x800158BC (0 removes it). No meaningful return value.
- While a callback is installed, raw transfers (slot 0x158, and every cache fill behind file
  reads) are asynchronous: they return at once and `cb` is called from the IDE completion path
  with 0 for success or 1 for failure after 4 retries (read, 0x80012158).
- Game calls: the movie player installs 0x800E7CF8 before each frame read (0x800E7DDC) and
  removes it with 0 (0x800E7594); 0x800F6174 and 0x800F69B4 remove it. 0x800E7CF8 records the
  status at 0x801DE93C, prints "Movie: Disk error %d reading frame" when it is non-zero, and
  decrements the pending count at 0x8023741C (read).

### 0x1E0 GT64010 cause handler — `void gt_set_handler(int bit, int (*fn)(void))` (0x800060BC, read)

- bit 0 to 31 (others: no action). fn non-zero: clears that bit in INTR_CAUSE (0x0C000C18, byte
  read-modify-write), sets it in CPU_MASK (0x0C000C1C), stores fn in the table at 0x800146E0.
  fn zero: unless bit is 8 (the loader's own timer-0 tick), clears the bit in CPU_MASK and in
  INTR_CAUSE; stores 0.
- No return value worth using (v0 = bit·4 on the normal path). It does not return the previous
  handler.
- How the handler is called: section 6 (a0 = 1, not (Cause, frame)).

### 0x1E8 FPU exception handler — `void fpe_set_handler(int cause, void (*fn)(void *frame))` (0x8000619C, read)

- cause 0 to 5 (inexact, underflow, overflow, divide by zero, invalid, unimplemented; the FCSR
  cause-bit order) stored in the table at 0x80014648. For cause 0 to 4, a non-zero fn **sets the
  matching FCSR enable bit** (0x80 << cause: bits 7 to 11) and a zero fn clears it; cause 5 has
  no enable bit. No useful return value.
- The game registers cause 5 (0x80150860) and cause 3 (0x8014FEF0), so after `main` FCSR has
  the divide-by-zero enable (bit 10) set again (`_start` wrote 0x01000000 without it). The
  stand-in must make that FCSR change, because it decides whether a floating-point divide by
  zero traps.

### 0x1F0, 0x1F8 break hooks — `void set_break7_hook(fn)`, `void set_break6_hook(fn)` (0x80006200, 0x80006210, read)

- Store fn at 0xA00B93EC (break 7, divide by zero) and 0xA00B93F0 (break 6, overflow), KSEG1
  words in the debug-monitor area. No return value. Use: section 6.

### 0x200 CPU interrupt-line handler — `void irq_set_handler(int line, int (*fn)(int ipbit))` (0x80006220, read)

- line 0 to 7. It finds the IP number n whose entry in the line map 0x800146C0 equals line
  (IP2 → 1, IP3 → 3, IP4 → 2, IP5 → 4, IP6 → 0; IP0, IP1, IP7 none; dump). If no IP maps to
  the line (lines 5 to 7), or line is out of range, nothing happens, not even the store.
- fn non-zero: for line 0 it first writes 1 to VBLANK clear (0x17700000) and sets bit 7 of the
  interrupt enable register (0x17300000); then **Status |= (0x100 << n) | 1** (0x80004B9C):
  the IP line is unmasked and IE set.
- fn zero: for line 0 it clears bit 7 of 0x17300000 and writes 1 to VBLANK clear; then Status
  &= ~(0x100 << n) (0x80004BBC).
- Stores fn in the table at 0x800146A0. Returns 8 in v0 on the normal path (a loop bound), not a
  previous handler.
- Game: line 0 (VBLANK on IP6) 0x800C6030, line 3 (IOASIC on IP3) 0x8013F678
  (`boot-handoff.md`). So after `main` Status has IM2 (from the loader), IM3 and IM6 set.

### 0x220 install_disk_info — `void install_disk_info(int *proc, void (*wait)(void), void (*wake)(int cookie))` (0x800127AC, read)

- Stores proc at 0x80015AD4, wait at 0x800B83E0, wake at 0x800B839C.
- When proc is 0 (uninstall) and the disk queue is not empty, it prints
  "install_disk_info() - Uninstall while requests are queued" on the console and spins until
  the queue empties, **with no timeout** (needs interrupts).
- Effect on transfers (read, 0x80011DE4): each request takes `*proc` as its cookie. If the cookie
  is non-zero, after queueing the loader calls `wait()` (unless the queue has already emptied,
  in which case it returns 0 at once), and on completion the IDE interrupt path calls
  `wake(cookie)` instead of the slot-0x1D8 callback.
- Game: 0x800F5DA8 and 0x800F6F58 install (0x801A3118, 0x80149A44, 0x80149B40); five sites
  uninstall with zeros. 0x801A3118 holds the game's current process (0 at the hand-off, dump);
  0x80149A44 puts the current process to sleep through the game's scheduler (it calls
  0x80151AC0), and 0x80149B40 marks the process with that cookie runnable (read, in outline).
  So with the game's process manager running, **a file read can make the calling process
  sleep inside the loader and let other game processes run until the DMA completes** (read;
  see section 4 for what the stand-in should do).

### Leftover registers after a slot call

- A static check over every `jal` into the stub block in `GAME.EXE` (all 29 slots, 279 `jal` sites)
  looked at the straight-line code after the delay slot, up to the first branch or 16
  instructions, for a read of at, v1, a0 to a3 or t0 to t9 before it is written. There is none
  (the only hit was the `break 0x407` after the write at 0x801448AC, whose code bits decode as
  register fields; read). So the game relies on v0 only after a loader call, as far as
  straight-line code shows; code reached through a branch right after the call was not
  followed (inferred to be the same, since the compiler treats these registers as clobbered).
- The a3 leftover that matters at the first loader call (`boot-handoff.md`) is an entry value
  passed through, not a value a service returns.
- v0 is as given per slot. Known v1 values, for tests that compare registers with MAME: open
  returns with v1 = 4·fd on success; creat with v1 = its own result; the timer elapsed reads
  with v1 = the elapsed value; the other slots leave intermediate values (read).

## 2. Descriptors, name table and drivers

### Descriptor table (read, dump)

- 16 descriptors of 36 bytes at 0x800B83E8 (to 0x800B8628). The fd is the descriptor index:
  slot calls map fd → 0x800B83E8 + 36·fd for fd < 16 (0x80006570).
- Descriptor layout: +0x00 buffer and +0x04 count (only for the dead flag-0x10 path; fd 1 holds
  leftovers 0x80014082 and -1 in the dump); +0x10 flags; +0x14 pointer to the name-table entry;
  +0x18 pointer to the driver's routine table; +0x1C pointer to the descriptor's 44-byte file
  block (0x80016040 + 44·i, set at start-up and never changed); +0x20 the name pointer (files
  only).
- Flags: bits 0 to 1 the access mode (1 read, 2 write, 3 read/write; 0 free), 0x04 non-blocking
  console (ioctl 0x6601), 0x08 console input polled before every read and write (set by the tty
  driver's open), 0x10 the dead path above, 0x20 set and cleared by ioctls 0x6603 and 0x6604.
- An fd map of 16 words at 0x800B83A0 (initialised to -1) receives fdmap[i] = i on open; only
  load-and-run's device reset reads it (closing every fd ≥ 6, 0x800066EC).
- A free descriptor is the first with flags 0 (0x800065A0).

### Descriptors open at the hand-off (dump, confirmed)

- fd 0: `con:` (tty), flags 0x09 (read, polled)
- fd 1: `con:` (tty), flags 0x0A (write, polled)
- fd 2: `con:` (tty), flags 0x0A
- fd 3: `cmos:`, flags 3
- fd 4: `ioasic:`, flags 3
- fd 5: `timer:`, flags 3 — **fd 5 is the timer driver** (its routine pointer is the `timer`
  entry 0x800135D0), not tty
- fd 6: free (flags 0), with a stale `scsi:` entry from the boot probe; the game's
  `open("pci:", 6)` in `main` therefore gets **fd 6** unless the game opened a file earlier
  (inferred: `game.env` is not found, so nothing else is open before `main`).
- fd 7 to 15: free and empty.

### Name table and open resolution (read, dump)

The name table at 0x80013180 has 28-byte entries {name, description, driver name, ...}. In
order:

- `led:` (leds) → led; `ioasic:` (i/o subsystem) → ioasic; `pic:` (PIC Micro) → pic;
  `pci:` (pci bus) → **sysctrl**; `timer:` (timers) → timer; `con:` (terminal) → tty;
  `mdiag:` (embedded diagnostics) → mdiag; `c:` (ide disk) → disk; `file:` (file system) →
  file; `scsi:` (debugger com) → scsi; `wdog:` (watchdog) → wdog; `cmos:` (nvram) → cmos;
  `snd:` (sound subsystem) → sound.

open(name) walks the table in that order and takes the first entry for which the comparison
0x80006D74 returns 0; that comparison returns 0 when the two strings agree up to the end of the
**shorter** one, so it is a prefix match in both directions: `led:` matches `led:xyz`, and the
empty string or `l` matches `led:` (read). If no entry matches, the `file:` entry is used and
the name is kept at descriptor + 0x20. The entry's driver name is then looked up with the same
comparison in the driver table at 0x80013530 (13 drivers, 7 routines and a name each:
`loader.DEVICE_SLOTS`). There is no `sound:`, `tty:` or `pci:` driver name; the devices are
reached as `snd:`, `con:` and `pci:` (read).

### Drivers the game reaches

- **tty (`con:`, fds 0 to 2; read, in outline).** Write sends each byte to UARTOUT (bus
  word 10) after busy-waiting for INTSTAT (bus word 7) bit 0x2000 (transmitter ready), polling
  console input in the loop, and stalls while an XOFF (0x13) received is in force (cleared by
  XON 0x11; flag word 0x800B6E50 bit 1). Read blocks until n characters are in the 256-byte
  ring at 0x800B6E5C unless flag 0x04 is set. Ioctl: 0x6602 polls the receiver (INTSTAT bit
  0x1000, then UARTIN, bus word 11); 0x7401 sets or clears bit 0 of 0x800B6E50; 0x7405
  returns 0; 0x7406 stores 1 if input is waiting, else 0 (the loader's own "key hit" test on
  fd 0, 0x800067C8). The game writes its debug messages to fd 1 (for example 0x801447B4).
  **Stand-in: write to the host's standard error, return n; read returns 0 bytes when
  non-blocking and never blocks; 0x6602 returns 0.**
- **cmos (fd 3; read).** Position word 0x80014D78 = the KSEG1 address of the current byte's word
  (0xB6100000 at the hand-off, dump). Read copies n bytes from position, position + 4, ... and
  returns n; write first writes 1 to the CMOS write enable (0x17000000) before every byte and
  stores the byte as a 32-bit word. n is clamped so the transfer ends at 0xB6120000. **Neither
  read nor write advances the position**: a second read rereads the same bytes. Ioctls in
  section 3.
- **ioasic (fd 4; read).** Only init and ioctl; read and write return 0. Ioctls in section 3.
- **timer (fd 5; read).** Only ioctl. Section 3.
- **pic (`pic:`; read).** Only init and ioctl. The init, which reads the PIC serial block, ran
  before the hand-off; the game's ioctls only return stored words (section 3).
- **sysctrl (`pci:`; read).** Open succeeds (returns 0) only when the sysctrl init flag
  0x80015890 is set (1 at the hand-off, dump), else -1; the game prints "Can not open the system
  controller device" and calls exit on failure (0x800C455C). Ioctls in section 3.
- **file (any other name; read).** Open: mode 1 (flags 1) opens an existing file (0xFFFFDBB7 if
  absent → open returns -1); mode 2 (write) opens the file if it exists, closes it, deletes it
  and creates it empty, or creates it; mode 3 (read/write) creates it (0x80008904) without
  first deleting, which on an existing name would leave two entries (inferred; the create
  routine was not read for a duplicate check). The name is upper-cased in place and
  converted to the 12-byte directory form. Read and write move whole words: n must be a
  multiple of 4 or the call returns 0 and transfers nothing; read returns the bytes read
  (clamped to the file length) or 0xFFFFDBAE (-9298) at end of file or on error; write returns
  n on success, -1 on failure. Ioctl: 0x99F6 to 0x99FA (the loader's own), others -1; the game
  issues none. The 44-byte file block: +0 position (words), +4 length (words), +8 and +10 the
  date and time halves, +12 attribute, +16 current block, +20 word offset in it (0 to 1024),
  +24 first block, +28 directory block (0 = not open), +32 entry index (words), +40 written
  flag.
- **wdog, led, mdiag, disk, scsi, sound**: the game opens none of them (no constant name
  matching them in the game's calls, `hardware-usage.md`; read). `snd:` is never opened: the
  game drives the DCS through `ioasic:` and the FIFO (section 5).

## 3. Every ioctl command the game issues

The command list and site counts are those of `hardware-usage.md`. Unless noted, the driver
returns 0 for success and -1 for an unknown command, and "*arg" is a 32-bit store through the
pointer in a2.

### `cmos:` (fd 3; driver ioctl 0x8000A1A4, read)

- 0x6300 set position: arg is the KSEG1 address of a CMOS byte's word; accepted when it is in
  0xB6100000 to 0xB611FFFF (it checks `arg + 0x49F00000 <= 0x1FFFF`), stored at 0x80014D78,
  returns 0; otherwise returns -2 and keeps the old position. So positions are
  0xB6100000 + 4·offset, as `hardware-usage.md` says.
- 0x6301 get base: *arg = 0xB6100000. 0x6302 get size: *arg = the word at 0x80014D7C (0x8000,
  dump). Both return 0.

### `ioasic:` (fd 4; driver ioctl 0x80006BE0, read)

Plain register accessors. Reads store `register & 0xFFFF`; writes store `arg & 0xFFFF`. None
blocks, waits or keeps state.

- 0x6900 read bus word 3, PORT0 (DIP switches)
- 0x6901 read bus word 4, PORT1 (player inputs; inferred from MAME's port names)
- 0x6902 read bus word 2, PORT2
- 0x6903 read bus word 1, PORT3
- With no input (measured, run A of the M2 facts, `m2-facts/inputs.txt`): every one of the
  7,889 reads of 0x6900 returned 0xBDFE, and every read of 0x6901 (7,888 reads), 0x6902
  (11,469) and 0x6903 (11,468) returned 0xFFFF. The stand-in starts with these values.
- 0x6904 read bus word 0, COIN (not issued by the game)
- 0x6905 write bus word 0 (COIN register) — what a write there does is not known (open
  question 10)
- 0x6906 write bus word 12, SOUNDCTL (DCS reset/control)
- 0x6907 read bus word 12, SOUNDCTL
- 0x6908 write bus word 13, SOUNDOUT (data to the DCS)
- 0x6909 read bus word 14, SOUNDSTAT (DCS handshake status)
- 0x690A read bus word 15, SOUNDIN (data from the DCS)
- 0x690B to 0x690D PICOUT/PICIN (not issued)
- 0x690E read bus word 7, INTSTAT
- 0x690F write bus word 8, INTCTL
- 0x6910 read bus word 8, INTCTL
- 0x6911 write bus word 15, SOUNDIN (used by the game right after reading SOUNDIN: the
  acknowledgement, inferred from the order)

### `timer:` (fd 5; driver ioctl 0x8000A228, read)

The GT64010's four 24-bit down-counters, clocked at TClk. The driver converts counts to time by
multiplying by 20, so it assumes 50 MHz: **one count = 20 ns, and the elapsed values are in
nanoseconds** (read; the clock rate is inferred from the factor). The control register
(TIMER_CONTROL, 0x0C000864) has 2 bits per timer: enable (bit 2k) and timer mode (bit 2k+1).

- 0x7400 start timer 0 (not issued; the loader owns timer 0 for its 100 ms tick, see section 7).
- 0x7401, 0x7402, 0x7403 start timer 1, 2, 3: stop it (clear both bits), then
  - arg 0: load count 0xFFFFFF and set only the enable bit: **counter mode, one shot**, about
    335.5 ms, after which the counter stays at 0 (inferred from the GT64010 counter mode and
    corroborated by the game, which tests for the saturated value 0x13FFFFEC, see below);
  - arg n ≠ 0: load n and set both bits: **timer mode**, reloading every n counts.
  The loaded count is saved (0x800B6D74, 0x800B6D78, 0x800B6D7C; timer 3 also at 0x800B8394).
  Returns 0.
- 0x7404: *arg = 0 (timer 0 elapsed is not supported). Not issued.
- 0x7405, 0x7406, 0x7407: *arg = (saved count − current count) × 20, for timer 1, 2, 3: **the
  time in ns since the start**, up to 0xFFFFFF × 20 = 335,544,300 = **0x13FFFFEC** for a
  one-shot that has run out. In timer mode the value wraps with the period. Returns 0. The
  product is a 32-bit multiply.
- 0x7408: returns 0. Not issued.
- 0x7409, 0x740A, 0x740B: stop timer 1, 2, 3 (clear both control bits). Returns 0.

What the game uses them for (read):

- **The scheduler is not paced by `timer:`.** The main loop (0x80151BD4) waits by spinning until
  the VBLANK count 0x801A2FE8 (incremented by the game's own line-0 handler 0x800C6030) reaches
  the frame divisor at 0x801A36BC (1), with a loop counter: after 100,000,000 spins it calls
  through 0x801A36C8, which holds 0xBFC00000, **a reboot**. Timer 3 is used in that loop only
  when the profiling flag 0x801A36C4 is 1 (set and cleared by 0x8015217C/0x80152190): 0x7403
  with arg 0 at the frame's start and 0x7407 into 0x801A36C0 at its end, a frame-time
  measurement. The flag is 0 at the hand-off (dump).
- Timer 3 is also the timeout of the Voodoo idle wait 0x8015EEAC (and of the GT DMA start
  0x80168224): it starts a one-shot (0x8014FE24: 0x7403, arg 0), polls the Voodoo, and each
  time 0x7407 returns exactly 0x13FFFFEC (the one-shot ran out) it prints "* Delay process
  system by printing this mess..." and restarts; after the fifth it stops the timer
  (0x8014FE54: 0x740B) and calls the error routine 0x800C47A8. The GT64010 bit-4 handler
  0x8014FDF0 stops timer 3 too.
- Timers 1 and 2 are the DCS handshake timeouts and a delay (section 5): 0x7401 arg 0, then
  0x7405 polled until it exceeds 25,000,000 (25 ms), then 0x7409; 0x8016A9E8 uses timer 2
  (0x7402, 0x7406, 0x740A) as a busy delay.

**Stand-in:** keep per-timer {start time, period or one-shot}; elapsed = min(now − start,
0xFFFFFF counts) × 20 ns for a one-shot, (now − start) mod period for timer mode, using the
emulated clock the rest of the runtime uses; return exactly 0x13FFFFEC once a one-shot has run
out. A stand-in whose time never advances would make every DCS wait and every Voodoo wait spin
forever.

**A stopped timer holds** (inferred): the loader's stop (0x7409 to 0x740B) only clears the
timer's enable and mode bits, a disabled GT64010 counter holds its count, and the elapsed
read computes (saved count − current count) × 20. So an elapsed read after a stop gives the
value of the moment of the stop (saturated or wrapped as above), until the timer is started
again. This matters because the GT64010 bit-4 handler 0x8014FDF0 stops timer 3 while the DMA
start 0x80168224 may still be polling it as its timeout.

### `pic:` (the game's own descriptor from `open("pic:", 0)` at 0x800C6480; driver ioctl 0x8000B0D8, read)

- 0x7000: *arg = word at 0x800B8650 (0x0001E240 = 123,456, dump)
- 0x7001: *arg = word at 0x800B8398 (0x000001D5 = 469, dump)
- 0x7002: *arg = word at 0x800B867C (0x00001B88 = 7,048, dump)
- All return 0 and touch no hardware. (0x7003 to 0x7006, the PIC clock commands, are not
  issued; the `_dos` date/time slots 0x048, 0x070, 0x098 and 0x188 use them, and the game does
  not call those slots.) The meanings of the three words (serial number, game number, date code
  are plausible) were not decoded from 0x8000AE2C (inferred).

### `pci:` (fd 6, the word at 0x801DE89C; driver ioctl 0x8000FB44, read)

- 0x7300 read a GT64010 register: arg points at {u32 offset, u32 value}; value =
  word at 0xAC000000 | offset. Returns 0.
- 0x7301 write a GT64010 register: word at 0xAC000000 | offset = value. Returns 0.
- (0x7302, 0x7303: PCI configuration read and write by {device, register}; not issued.)
- What the game does with them (read): 0x80167FF8 reads offset 0x840 (DMA channel 0 control);
  0x80168224 and 0x801683EC each write offset s0 (a control-register offset held in a saved
  register, values 0x0DC0, then 0x2DC0, then 0x1DC0) and offset 0x830 (DMA channel 0 next-record
  pointer) = a descriptor chain in game memory at 0x801Bxxxx. That is **a chained GT64010 DMA
  channel 0 transfer started by the game through the loader**, after a data-cache flush (slot
  0x0E8) and with timer 3 as its timeout; its completion is GT64010 cause bit 4, handled by
  0x8014FDF0 (`boot-handoff.md`). The MAME split run's "DMA0 6,075 control and 2,025 next
  writes" by loader code after the hand-off (`hardware-usage.md`) are these ioctls (inferred
  from the attribution). **So the runtime must emulate GT64010 DMA channel 0 (with chained
  records) behind ioctl 0x7301 and raise cause bit 4 when it completes**; this corrects
  `hardware-usage.md`'s view that only the loader's own code needs the GT64010 DMA.

## 4. Files on the disk

### How a file is found and read (read)

- Partition: the current partition (slot 0x118; partition 1 = part0) supplies the start sector
  added to every file-system sector. The file system is the one of `disk-format.md`: home block
  at sector 2, chain table from sector 67, blocks of 4096 bytes, the root directory at block 3,
  24-byte entries, names stored byte-reversed in 4-byte groups (the loader converts with
  0x80009964), length in words.
- Open looks the name up by a linear scan of the directory chain, reading directory blocks
  through the block cache.
- Read (0x800092D4) works in words. Within a 4 KB block it copies from the block cache; at a
  block boundary it follows the chain (0x80007CCC) and, for runs of whole blocks, counts how
  many consecutive blocks follow (0x80007FC8) and reads them **directly into the caller's
  buffer** (0x80011558, 8 sectors per block) instead of through the block cache.
- A chain link below 3 ends the read early (bytes so far are returned).

### Caches the game could observe (read)

- A block cache (0x800079F8) for directory and partial blocks, written back on flush/close.
- Below it, a sector cache of **4 lines of 128 KB** (256 sectors each, aligned to 256-sector
  boundaries of the absolute LBA; table at 0x80016300, LRU by counters, 0x80007538). Every
  partition-relative read goes through it (0x80007628): a miss reads the whole 128 KB line. So
  the loader reads ahead up to 128 KB. Partition writes update a cached line if present
  (write-through, 0x800076D8).
- Neither cache is visible to the game except through timing and through the fact that data
  arrives in the caller's buffer only by the time the call returns (inferred: the game never
  reads the caches' memory).

### Transfer mechanism (read)

- With 0x80015388 clear (the hand-off value), every sector transfer, including cache fills
  for file reads, goes through the DMA request queue: a ring of 128 request pointers at
  0x800158CC, requests of 32 bytes at 0x800B7390 ({lba, buffer, count, write flag, done or
  cookie word, chunk, retries}), head 0x800158C4, tail 0x800158C8. Before queueing a buffer that
  is not in KSEG1 the whole data cache is written back and invalidated (0x80004E78).
- The IDE controller is driven by bus-master DMA (0x0A000F00) and completes through IP4. The
  service routine 0x80012158 runs **from the loader's default IP4 interrupt path**; it advances
  chunked requests, retries a failed one up to 4 times, starts the next queued request, and
  signals completion (wake(cookie), or the slot-0x1D8 callback with 0 or 1, and the done word
  -1).
- A synchronous caller (no callback, no process hook) spins on the done word for up to
  100,000,000 iterations; on timeout it calls the hook at 0x800158C0 (none set), prints
  "Disk timeout" and resets the caches. Writes, and enqueues when the ring is full, spin the
  same way, or, when the caller is itself running inside an exception handler (Status.EXL set,
  tested at 0x80011BD0 and 0x80011CA8), service the controller by polling Cause bit 0x1000 and
  calling 0x80012158 directly.
- Status: the enqueue routine masks IM4 (Status &= ~0x1000, 0x80011C8C) while it updates the
  ring, and the transfer-start routine 0x800124F4 unmasks it and sets IE (Status |= 0x1000 | 1,
  0x80012518). So **after the game's first disk transfer Status has IM4 set** as well as IM2
  (read); the hand-off Status has only IM2 because load-and-run masked IM3 to IM7 and unmasked
  IM2 (0x8000E9D4 to 0x8000EA00).
- **So every disk read made by the game needs either IP4 interrupts or the loader's polled
  fallback to complete.** None of this is visible to the game once the call has returned,
  except: the completion callback and the wake hook run at interrupt level (Status.EXL set,
  on the loader's exception stack), and with the process hook installed a reading process
  sleeps in the game's scheduler (`wait()`) until the interrupt wakes it.

### Writing files (read, in outline)

- Write (0x80009040) writes words at the current position, through the block cache for partial
  blocks and directly (0x80011698) for whole blocks, allocating free blocks from the chain table
  (first entry equal to 1) and linking them. Length and the written flag are updated in the file
  block; the directory entry (length, date/time) is updated and the dirty blocks flushed on
  close. The game writes files only for its high-score tables and audits, if at all (inferred;
  not traced).

### The movie player (read)

- Uses raw sector reads, not files: it selects partition 1 (slot 0x118) at start-up, takes the
  partition table (slot 0x128), and for each frame computes an absolute LBA = start of
  partition n (n = the word at 0x801DE92C) + frame number (0x801DE914) × sectors per frame
  (0x801DE938).
- Per frame: if no read is pending (flag word 0x801DE900 bit 1 clear) and the previous buffer
  is free, it sets the pending count 0x8023741C to 1, installs the completion callback
  0x800E7CF8 (slot 0x1D8), and issues slot 0x158 (lba, buffer = (word at 0x801DE910) + 8,
  count = sectors per frame). The call returns at once; the game then calls its sound streamer
  (0x8013FF24) and waits with 0x80151AC0(1) (one scheduler sleep per call) while the pending
  count is positive, up to 200 times. The callback, run from the IDE interrupt, decrements the
  count.
- When the movie ends it removes the callback (slot 0x1D8 with 0) and clears its flags.
- Which partition holds the movies and the frame geometry were not read (open question 6,
  answered below).

### Stand-in recommendation for the disk (inferred)

- Complete every transfer synchronously from the host's disk image, and then deliver the
  completion exactly as the loader does when a transfer finishes before the caller looks:
  call the slot-0x1D8 callback with 0 (or 1 on a host read error) and return 0 from slot 0x158
  when a callback is installed; with a process hook installed and a non-zero cookie, call
  `wake(cookie)` and return 0 without calling `wait()`; otherwise return count. The callback
  and wake normally run at interrupt level; calling them synchronously from inside the slot
  changes only when they run, and both only adjust counters and process states (read in
  outline), but this should be checked (open question 5, answered below).
- Slot 0x1D8 and the uninstall path of 0x220 then never wait.

## 5. Sound

### The path to the DCS board (read)

- The game drives the DCS2 itself: data streams go to the sound FIFO at 0x13000000 from
  0x8013FF78 (direct stores through the base in 0x801A25CC, `hardware-usage.md`), and the
  command handshake goes through `ioasic:` ioctls 0x6906 to 0x6911. The loader's `snd:`
  driver (`sound`, ioctl 0x8001020C) is never opened by the game.
- The game's DCS routines (0x8016A42C to 0x8016D96C, read in outline):
  - read a word: start timer 1 (0x7401, arg 0); poll SOUNDSTAT (0x6909) for **bit 0x40** (data
    from the DCS waiting); on timeout (0x7405 > 25,000,000 ns, i.e. 25 ms) stop the timer and
    return **0xEEEE**; else read SOUNDIN (0x690A) and write SOUNDIN (0x6911, acknowledge);
  - write a word: poll SOUNDSTAT for **bit 0x80** (the DCS has taken the previous word; one
    variant waits on 0x8080) with the same 25 ms timeout returning 0xEEEE; then write SOUNDOUT
    (0x6908);
  - 0x8016A930 polls bit 0x40 with a counter, sleeping one scheduler tick (0x80151AC0) between
    polls, and returns 0xEEEE when the counter runs out;
  - reset: SOUNDCTL (0x6906) writes; delays with timer 2 (0x8016A9E8).
- 0xEEEE is propagated as the error result through about 150 sites in the DCS layer (read: the
  constant appears throughout 0x80169474 to 0x8016D968). What the game does at the top when the
  DCS does not answer (retry, mark sound dead, or stop) was not traced (open question 3,
  answered below).
- The SOUNDSTAT writes the census found (0x801470A4 ...) did not run in MAME (`hardware-usage.md`).

### What the loader's own code expects from the DCS

- The `sound` driver's init and ioctl (0x8000FCDC, 0x8001020C) and 0x8000D370 implement the
  same handshake with GT64010 timer-based timeouts (0x8000FC60, 0x8000FCBC; `hardware-usage.md`),
  but they run only at the loader's start-up (before the hand-off) and for `snd:`, which the game
  does not open (read in outline; not traced further).
- The default IP3 path (IOASIC summary) clears INTCTL bit 0 and services nothing else for sound
  (section 6). The game's own IP3 handler 0x8013F678 does the sound-interrupt work.

### What a runtime without DCS emulation must answer (inferred)

- SOUNDSTAT (0x6909): report bit 0x80 set always (the DCS is ready for a word), so writes never
  time out. For bit 0x40 there are two consistent choices: always clear, so every read times
  out after 25 ms of emulated time and returns 0xEEEE (honest, but costs 25 ms per read and may
  make the game give up on sound), or answer reads with the values the DCS would send. Which
  replies the game needs (boot version query, command acknowledgements) is open question 3
  (answered below; `dcs-handshake.md`).
- SOUNDIN (0x690A): whatever reply is being modelled; SOUNDCTL (0x6907): read back the last
  write; writes to SOUNDOUT, SOUNDCTL and SOUNDIN: accept and drop.
- The sound FIFO at 0x13000000 (direct): accept and drop.
- IP3 (line 3): deliver no sound interrupts until the IOASIC is modelled; the handler 0x8013F678
  must not run with nothing pending (inferred).
- Timeouts that matter: 25 ms per DCS read or write wait (timer 1), the counted polls of
  0x8016A930 (scheduler ticks), and none in the loader for the game's path.

## 6. Interrupts and exceptions as the game sees them

### Entry (read, 0x80004368)

- The general exception vector saves at and v0 in the debug-monitor area (0xA00B86A8 + 0x328,
  + 0x330), takes the exception code from Cause, and, if that code's bit is set in the monitor
  mask word 0xA00B89E0 (0xFFFF0200 at the hand-off: code 9 and codes 16 to 31), enters the
  debug monitor; otherwise it goes to the dispatcher 0x80004B18.
- For code 0 (interrupt) with IP7 pending it reads the interrupt state 0x17500000: bit 0x40
  enters the monitor; bit 0x08 with the SCSI card answering (0xA9000014 & 7) enters the monitor
  too. Neither happens on this board (no IP7 source; inferred).

### The frame (read, 0x80004808; base 0x80014328, one frame, not a stack)

All slots are 8 bytes, written with `sd`/`sdc1` (so a 32-bit register appears sign-extended in
its slot), except where a 32-bit `sw` is noted.

- +0x000: not written (r0)
- +0x008 + 8·(n−1) for GPR n = 1 to 25: at +0x008, v0 +0x010, v1 +0x018, a0 to a3 +0x020 to
  +0x038, t0 to t7 +0x040 to +0x078, s0 to s7 +0x080 to +0x0B8, t8 +0x0C0, t9 +0x0C8
- +0x0D0: k0, **not saved** (the slot keeps stale data)
- +0x0D8 k1, +0x0E0 gp, +0x0E8 sp, +0x0F0 fp (s8), +0x0F8 ra (written first, before the save
  routine is called)
- +0x100 LO, +0x108 HI
- +0x110 the **resume PC** (32-bit `sw`): EPC, or ErrorEPC when Status.ERL is set. This is the
  word the dispatcher restores, so it is the one a handler changes to skip an instruction.
- +0x118 + 8·r for CP0 register r = 0 to 31 (each read with `mfc0`): Index +0x118, Random
  +0x120, EntryLo0 +0x128, EntryLo1 +0x130, Context +0x138, PageMask +0x140, Wired +0x148,
  (7) +0x150, BadVAddr +0x158, Count +0x160, EntryHi +0x168, Compare +0x170, **Status +0x178**,
  **Cause +0x180**, EPC +0x188, PRId +0x190, Config +0x198, LLAddr +0x1A0, WatchLo +0x1A8,
  WatchHi +0x1B0, XContext +0x1B8, (21 to 25) +0x1C0 to +0x1E0, ECC +0x1E8, CacheErr +0x1F0,
  TagLo +0x1F8, TagHi +0x200, ErrorEPC +0x208, (31) +0x210
- FPRs, only when the saved Status has CU1 (bit 29): `sdc1 f(2k)` at +0x218 + 16k for k = 0 to
  15. With Status.FR clear (the game's case, Status 0x30010401) each such store writes the pair:
  **f(2k) at +0x218 + 16k and f(2k+1) at +0x21C + 16k**, and the words +0x220 + 16k to +0x227 +
  16k are not written. With FR set, the odd registers would be stored at +0x220 + 16k.
- +0x318 FIR (cfc1 $0, 32-bit), **+0x31C FCSR** (cfc1 $31, 32-bit)

### Dispatch (read, 0x80004B18, 0x80005244)

- After the save, sp = 0x800C1400 (the exception stack) and 0x80005244 is called with
  a0 = Cause (read again from CP0, not the frame) and a1 = the frame. Status is as the exception
  left it: **EXL set, so interrupts are off throughout, and kernel mode**. The handlers never
  re-enable interrupts.
- A guard word 0x8001486C detects a nested exception: an exception while it is set prints
  "Exception while in exeption handler" and "Reboot", writes the watchdog and spins (so a
  `break`, address error or FP trap inside an interrupt handler **reboots the board**).
- During the handling, bit 14 (0x4000) of IOASIC INTCTL (bus word 8, 16-bit access) is cleared;
  **afterwards it is set, whatever it was before** (observable through ioctl 0x6910).
- Code 0: the interrupt loop below. Any other code: the handler in the exception table
  0x80014660 (installed with break 0x409) is called with (Cause, frame); if there is none or it
  returns non-zero, the loader's default for that code (table 0x80014760) is called with
  (Cause, frame).
- Resume (0x80004BE0): HI, LO, **Status from frame + 0x178**, EPC (or ErrorEPC) from frame +
  0x110, the FPRs (if CU1), **FCSR from frame + 0x31C**, all GPRs but k0, then `eret`. So a
  handler can change the resumed Status, PC, FCSR and any register through the frame.

### The interrupt loop (read, 0x80005100)

- For IP0 to IP7 **in ascending order**, for each bit set in Cause (the Cause read at entry,
  not re-read): look up the line in the map (IP2 → 1, IP3 → 3, IP4 → 2, IP5 → 4, IP6 → 0);
  - no line (IP0, IP1, IP7): print "***** INTERRUPT: IP" and the Cause, call the debug entry
    0x80005E1C (which, without the SCSI card, prints "go_debug() - debug entry with no SCSI card"
    and returns) and **stop the loop** (later bits are not served this time);
  - a game handler installed for the line: call it with **a0 = the IP bit (0x100 << n)**; a1 is
    not set (on the first call it still holds the frame, on later calls whatever the previous
    callee left). If it returns 0 the interrupt is done; if it returns non-zero, or there is no
    handler, the loader's default 0x80005368 is called with (IP bit, frame);
  - then the bit is cleared in the saved Cause (frame + 0x180).
- The game's line-0 (VBLANK) handler 0x800C6030 returns 1 (read), so the loader's default VBLANK
  work always follows it.

### The loader's default interrupt work (read, 0x80005368)

- IP6 (VBLANK): increments the loader's VBLANK count 0x800147C4; every 8th, toggles LED bit 1
  (0x17900000 ^= 2); if the watchdog flag 0x800147C8 is set (0 at the hand-off, dump) and the
  count is a multiple of 64, writes the watchdog; **writes 0 to VBLANK clear (0x17700000)**. The
  game never clears VBLANK itself (`hardware-usage.md`), so the runtime's VBLANK source must be
  acknowledged by this default work, or modelled as edge-triggered (inferred).
- IP3 (IOASIC): clears bit 0 of INTCTL (bus word 8, 32-bit read-modify-write).
- IP5: prints "Hilink/NSS/Widget Interrupt - must be handled". Nothing else.
- IP4 (IDE): runs the disk completion service 0x80012158 (section 4).
- IP2 (GT64010): pending = INTR_CAUSE & CPU_MASK; for bits 0 to 31 **in ascending order**, a set
  bit is first cleared in INTR_CAUSE (byte read-modify-write), then:
  - bit 8 (timer 0) while the tick flag 0x80014D88 is set (1 at the hand-off): the loader's
    100 ms tick 0x8000A650 (a counter at 0x80014D8C; every 0.5 s it writes the watchdog and
    toggles LED bit 2) and a disk-timeout check 0x800128F8, then the game's handler for bit 8 if
    any (none);
  - another bit with a game handler: the handler is called with **a0 = 1** (left over from the
    bit test) and no other argument; its return value is ignored. **Correction to
    `boot-handoff.md`**, which says GT64010 cause handlers get (Cause, frame);
  - a bit without a handler: bits 8 to 11 are ignored; any other prints "GT INT:" and its name.
- The game observes: its handlers run; VBLANK, IOASIC INTCTL bit 0 and the GT cause bits are
  acknowledged; the loader's VBLANK count and tick count change (the game never reads them).

### Exceptions other than interrupts (read)

- The game installs, with break 0x409 from 0x800C4A08, the catch-all 0x800C47E0 for codes 1 to 8
  and 10 to 15 (code 15, floating point, included; read: the list is 1, 2, 3, 4, 5, 6, 7, 8, 10,
  11, 12, 13, 14, 15). The catch-all records Cause, the resume PC (+0x110) and the GPR slots
  into its crash audit (CMOS) and then jumps to 0xBFC00000: **any such exception reboots**.
  Later, 0x801519E0 installs 0x80151A70 for code 13 (trap).
- Because the exception table entry for code 15 is the catch-all, which never returns, **the
  loader's default for code 15 (0x80005CD8), which calls the FPU handlers of slot 0x1E8, is not
  reached once 0x800C4A08 has run** (read). So the FPU handlers 0x80150860 (unimplemented
  operation) and 0x8014FEF0 (divide by zero) are dead after that point, and a floating-point
  exception with its enable set reboots. With slot 0x1E8 having set the divide-by-zero enable,
  **an FP divide by zero in game code reboots the board** (inferred from the two reads; whether
  0x800C4A08 runs before every possible FP exception was not checked).
- For completeness, the default for code 15 walks FCSR cause bits 12 to 17 in ascending order
  (inexact first, unimplemented last), calls the FPU handler for each set bit with a0 = frame
  (return ignored; without a handler it prints the FPCSR and calls the debug entry), and clears
  that cause bit in the saved FCSR. The handler must itself advance frame + 0x110 past the
  instruction (0x80150860 does, `boot-handoff.md`).

### `break` codes (read, 0x80004410 and the table at 0x80004684)

The code is the instruction word at EPC shifted right 6.

- 0x1C00 (`break 7`, divide by zero) and 0x1800 (`break 6`, overflow): if the hook (slot 0x1F0 or
  0x1F8) is set, the exception goes to the normal dispatcher as code 9; the exception table has
  no game handler for 9, so the loader's default 0x80005AA0 calls **hook(Cause, frame)** and
  returns; its return value is ignored. **The resume PC is not advanced**: unless the hook
  changes frame + 0x110 (or does not return), the `break` executes again. If the hook is not
  set, the debug monitor is entered. The game's hooks are 0x800C4DA0 and 0x800C4E68 (not read
  in detail; open question 9, answered below: never hit).
- 0x400 to 0x40A, the monitor services (EPC is advanced by 4 and `eret` returns to the next
  instruction, with every register as before the `break`, at and v0 restored, except where the
  service enters the monitor). Cause.BD is not checked:
  - 0x409, the game's use (0x801A355C, 18 calls): a0 = exception code, a1 = handler; for a0 0 to
    15 it clears that code's bit in the monitor mask and stores a1 in the exception table
    0x80014660[a0]; out-of-range a0 does nothing. No return value (v0 unchanged).
  - 0x407 (assert, 4 sites): advances EPC and enters the monitor with code 9; it does not come
    back on this board. **Stand-in: stop with a message naming the PC.**
  - 0x402 (slot 0x0D8, exit) and 0x401: enter the monitor (do not come back).
  - 0x403 and 0x406: set the monitor mask to a0; 0x405: save the mask at + 0x33C and set
    0xFFFF0200; 0x408: no-op; 0x40A: CPU_MASK |= 1 << a2; 0x400: enters the monitor only if
    a debugger answers; 0x404: enters the monitor. None is used by the game.

### Status as the game sees it (read)

- At the hand-off: 0x30010401 (`boot-handoff.md`). Slot 0x200 ORs in IM3 and IM6 and IE.
  Nothing else the game calls writes Status, except the disk path, which clears IM4 while it
  enqueues and sets IM4 and IE when it starts a transfer (section 4). So in steady state the
  game runs with IM2, IM3, IM4, IM6 and IE set (inferred from the reads).
- Handlers run with EXL set; the resumed Status is the saved one (frame + 0x178).

## 7. Start-up state the game inherits (dump unless noted)

- Descriptors 0 to 5 as in section 2; descriptor 6 free.
- CMOS position 0x80014D78 = 0xB6100000; size 0x80014D7C = 0x8000; init flag 0x80014D80 = 1.
- PIC words: 0x800B8650 = 0x0001E240, 0x800B8398 = 0x000001D5, 0x800B867C = 0x00001B88.
- Timers: timer 0 runs in timer mode with count 0x004C4B40 (5,000,000 counts = 100 ms at
  20 ns; 0x800B6D70 and 0x800B8394), CPU_MASK bit 8 enabled, tick flag 0x80014D88 = 1, tick
  count 0x80014D8C = 1 at the entry and 3 at the first loader call (two ticks were taken in
  `_start`'s clear loop: these are the two IP2 interrupts `boot-handoff.md` reports). Timers 1
  to 3: saved counts 0 (not started).
- Interrupt bookkeeping: loader VBLANK count 0x800147C4 = 0; watchdog flag 0x800147C8 = 0;
  exception guard 0x8001486C = 0; all handler tables and both break hooks empty; monitor mask
  0xA00B89E0 = 0xFFFF0200 (and its saved copy).
- Line map 0x800146C0: IP0 −1, IP1 −1, IP2 1, IP3 3, IP4 2, IP5 4, IP6 0, IP7 −1.
- Disk: current partition 1 (0x80015AD0 = 0x800B7174); drive size word 0x800158A8 =
  0x0080730C sectors; PIO flag 0x80015388 = 0 (DMA path); request queue empty (head = tail = 0);
  no completion callback (0x800158BC), no disk info (0x80015AD4), no timeout hook; file system
  mounted (0x80014D68 = 1); IDE multiple-sector count 0x800B7374 = 16.
- sysctrl init flag 0x80015890 = 1 (so `pci:` opens); the partition-table copy at 0x800B7160.
- Console: flags word 0x800B6E50 = 0; ring buffer empty (head = tail = 0x800B6E5C).
- Not in the dump (hardware registers): the value of IOASIC INTCTL, the interrupt enable
  register (0x17300000; its bit 7 is set only when line 0 is installed), CPU_MASK beyond bit 8,
  and TIMER_CONTROL. These need a MAME read (open question 1, answered below for all but
  TIMER_CONTROL, whose debugger read was not reproducible).

## 8. What each stand-in must do

### Slots

- **0x018 close(fd):** fd ≥ 16 or not open → -1. Else run the device's close (files: write back
  the directory entry and data), mark free, return 0. Test: close(3) returns 0 and a following
  ioctl(3, 0x6301, &x) returns -1; close(16) returns -1.
- **0x028 creat(name, attr, int *fd):** fd = open(name, 1); store it (or -1) at *fd; return 0 if
  fd ≥ 0 else -1.
- **0x038 findfirst(pattern, attr, find_t *buf):** for `game.env` (and any name absent from
  partition 1) return -1 and leave buf alone. For a present file, fill buf per section 1 (attr
  byte 21, time 22, date 24, size 26 as u32, name 30) and return 0.
- **0x088 open(name, mode):** prefix-match the name table in the order of section 2, else a file
  on partition 1; mode & 0xF selects flags 1/2/3 (other values: hand back a free descriptor that
  stays free). Return the lowest free descriptor index or -1. Upper-case a file name in place.
  Test: open("pci:", 2) after the hand-off returns 6; open("nosuchfile", 0) returns -1.
- **0x090 read(fd, buf, n):** -1 for a bad fd; files: 0 if n % 4 ≠ 0, else min(n, remaining)
  bytes copied and returned, 0xFFFFDBAE at end of file; cmos: n clamped, bytes from position,
  position unchanged; console: non-blocking, 0 when no input.
- **0x0C0 write(fd, buf, n):** -1 for a bad fd; console: write to host stderr, return n; cmos:
  store the bytes, return n (position unchanged); files: n % 4 ≠ 0 → 0, else write and return
  n, or -1.
- **0x0C8 ioctl(fd, cmd, arg):** -1 for a bad or free fd; 0x6601/0x6603/0x6604 flag handling as
  in section 1; then the per-driver commands of section 3; unknown commands -1; led, mdiag,
  disk, scsi return 0 for all.
- **0x0D8 _exit(status):** stop the runtime, reporting status.
- **0x0E0 isatty(fd):** 1 for fds whose name entry is `con:` (0 to 2 at start), else 0.
- **0x0E8 dcache flush:** no-op.
- **0x0F0 / 0x0F8 PCI config read/write (reg, dev, u32 *):** route to the Voodoo PCI model by
  device number and reg/4; return 1.
- **0x108 pci_probe(vendor, devid, -, dev):** 1 if device dev's vendor ID is vendor.
- **0x110 pci_map(vendor, devid, -, int *dev, index):** for the Voodoo: *dev = its device
  number, BAR0 = 0x08000000, return 0xA8000000; else *dev = 10, return 0.
- **0x118 select_partition(n):** n > 17 → 2; else current = n (1-based; 0 = whole disk), return 0.
- **0x128 partition_table():** return a pointer to a game-visible copy of the disk header with
  the total-sector word at + 0xC (0x0080730C in MAME).
- **0x158 read_sectors(lba, buf, count):** absolute LBA from the disk image; out of range: no
  transfer, callback(count) if installed, return count. In range: copy, then if a 0x1D8
  callback is set call it with 0 (1 on a host error) and return 0; else if disk info is set
  with a non-zero cookie call wake(cookie) and return 0; else return count.
- **0x190 unlink(name):** remove from partition 1 (or refuse if the image is read-only, and say
  so); return 0 also when absent.
- **0x198 exec(name, ...):** stop the runtime with a message naming the file.
- **0x1A8 exec_args():** pointer to 4 zero words.
- **0x1B8 rename(old, new):** 0xFFFFDBB7 if old is absent, else rename and return 0.
- **0x1C0 lseek(fd, off, whence):** positions in words: SET off/4, CUR cur + off/4, END length
  (offset ignored); return the new position × 4; bad fd or whence > 2 → 0.
- **0x1D8 set_callback(cb):** store cb (no wait needed with synchronous transfers).
- **0x1E0 gt_set_handler(bit, fn):** store; enable/disable the bit in the runtime's GT64010
  CPU_MASK model as in section 1; deliver later as fn() with a0 = 1, return value ignored.
- **0x1E8 fpe_set_handler(cause, fn):** store; for cause < 5 set (fn ≠ 0) or clear FCSR bit
  7 + cause. Test: after (3, fn) FCSR bit 10 is set.
- **0x1F0 / 0x1F8 break hooks:** store; on `break 7` / `break 6` call hook(Cause, frame) with a
  loader-layout frame, then resume at frame + 0x110 (re-executing the `break` if the hook did
  not move it).
- **0x200 irq_set_handler(line, fn):** store; for line 0 model the VBLANK enable (bit 7) and
  clear; unmask the line's IP bit and set IE in the runtime's Status. Deliver as fn(0x100 << n);
  when fn returns non-zero (VBLANK always does), do the loader's default work for that line.
- **0x220 install_disk_info(proc, wait, wake):** store the three words; never wait.

### ioctls

- **cmos 0x6300:** accept 0xB6100000 ≤ arg ≤ 0xB611FFFF → position = arg, 0; else -2.
- **cmos 0x6301 / 0x6302:** *arg = 0xB6100000 / 0x8000; 0.
- **ioasic 0x6900 to 0x6903:** *arg = DIP switches / inputs (bus words 3, 4, 2, 1), 16 bits.
- **ioasic 0x6905:** accept (log).
- **ioasic 0x6906 / 0x6907:** SOUNDCTL write / read back.
- **ioasic 0x6908:** SOUNDOUT write (to the DCS model, or drop).
- **ioasic 0x6909:** SOUNDSTAT: bit 0x80 set; bit 0x40 per the DCS model (section 5).
- **ioasic 0x690A / 0x6911:** SOUNDIN read / acknowledge write.
- **ioasic 0x690E / 0x690F / 0x6910:** INTSTAT read; INTCTL write; INTCTL read (remember that
  the loader's dispatcher sets INTCTL bit 14 after every exception).
- **timer 0x7401 to 0x7403:** start timer 1 to 3; arg 0 one-shot of 0xFFFFFF counts, else period
  arg counts; 0.
- **timer 0x7405 to 0x7407:** *arg = elapsed counts × 20 (ns), saturating at 0x13FFFFEC for a
  one-shot; 0. Test: immediately after 0x7403 with arg 0, 0x7407 gives a small value; after
  more than 335.5 ms of emulated time it gives exactly 0x13FFFFEC.
- **timer 0x7409 to 0x740B:** stop; 0.
- **pic 0x7000 / 0x7001 / 0x7002:** *arg = 0x0001E240 / 0x000001D5 / 0x00001B88; 0.
- **pci 0x7300 / 0x7301:** read / write the runtime's GT64010 register model at the given
  offset; DMA channel 0 writes (0x800 to 0x84C) start chained DMA, whose completion raises
  cause bit 4.
- **console 0x6602** (issued by read and write, not by the game): poll host input, 0.

## Open questions that need a MAME run

Answered on 2026-11-19 from MAME runs in the Linux container (MAME 0.276, blank NVRAM, `-nodrc`, the
debugger on): attract runs of 150 s from the entry, as run A of `task-switch.md`, and a play
run with run B's inputs (`tools/mame/m2facts.lua`, `tools/mame/dcstap.lua`; command lines in
`tools/mame/README.md`). The runs are deterministic: the attract runs repeat run A event for
event, and the play run's 22 snapshots are byte for byte those of run B. Each answer names its
file in `mame/m2-facts/` on the share.

1. **Hardware state at the hand-off.** Read, at the breakpoint on 0x800C4000: IOASIC INTCTL (bus
   word 8) and INTSTAT, the interrupt enable register 0x17300000, interrupt configuration
   0x17400000, GT64010 CPU_MASK (0x0C000C1C), INTR_CAUSE and TIMER_CONTROL (0x0C000864), and
   TIMER0 to TIMER3. Settles the start values in section 7.
   **Answer (measured, debugger reads without side effects; `entry-hw.txt`):** IOASIC INTSTAT
   0x000020C9, INTCTL 0x00004000; interrupt enable 0x17300000 = 0x00000002 (bit 7, VBLANK, is
   clear until slot 0x200 installs line 0); interrupt configuration 0x17400000 = 0x00008004;
   GT64010 INTR_CAUSE 0, CPU_MASK 0x0010011E (bits 1 to 4, 8 and 20), TIMER0 0x00313793
   (counting down from 0x4C4B40), TIMER3 0x00F0BDB9; Status 0x30010401. The debugger reads of
   TIMER_CONTROL, TIMER1 and TIMER2 are **not reproducible**: two identical runs gave
   TIMER_CONTROL 0x00000003 and 0x50000027, TIMER1 and TIMER2 0x442AFB52 and 0 (facts-A and
   facts-A2), so they are not measured start values. That timer 0 runs in timer mode at the
   entry is read from the dump (section 7: the loader's tick, count 0x4C4B40) and agrees with
   TIMER0 counting down; whether timers 1 to 3 are enabled in TIMER_CONTROL is inferred (their
   saved counts are 0, section 7), not measured. A blank NVRAM is filled with 0xFF: all 32,768
   CMOS bytes read 0xFF at the entry.
2. **Whether `pci:` is fd 6.** Breakpoint on the return of slot 0x088 called from 0x800C4540;
   log v0 (expected 6).
   **Answer (measured, `loader-calls.md`, call 3; `opens.txt`):** yes. `open("pci:", 2)` from
   the game's open wrapper (return address 0x80145738) returned 6, the first open of the run.
3. **The DCS replies the game needs.** Tap ioctls 0x6908 (arguments) and 0x690A (results) and
   SOUNDSTAT reads during the first 20 s of a normal run, and log every return of 0xEEEE from
   0x8016A42C, 0x8016A510, 0x8016A82C and 0x8016A930; then rerun with SOUNDSTAT bit 0x40 forced
   clear (Lua write tap) and record whether attract mode still runs and whether the game sets a
   "sound dead" flag (watch where the 0xEEEE results are stored). Settles what a DCS-less
   runtime must return.
   **Answer (measured; `dcs-trace.txt`, details in `dcs-handshake.md`):** with the board
   answering, no DCS routine returned 0xEEEE in 150 s; the replies the game reads are 0x000A
   after each reset and after each boot-load block, the 16-bit sum of each block's data words,
   0x000C after 0x002A and 0x0223 after 0x55D4, and the bulk writes wait for SOUNDSTAT 0x8080.
   With bit 0x40 forced clear (a breakpoint action in the loader's read path rather than a
   tap) the game timed out 13 times, printed `main_init_sound(): ERROR initializing sound
   system.` with three retries and `Aborting` at each of its two sound loads, sent no sound
   data, and still ran attract mode (the same seven tasks, attract-mode snapshots to 150 s). No global flag receiving the 0xEEEE results was found (read, in part).
4. **Scheduler loop timing.** Breakpoint on 0x80151C58 to 0x80151C7C (the VBLANK wait) logging
   the spin counter 0x801A36CC at exit, for 600 frames. Shows how close the wait comes to its
   100,000,000 limit and therefore how late a missing VBLANK reboots the game.
   **Answer (measured, `spin.txt`):** in the 600 frames from the first pass (160 passes) the
   counter at the end of the wait was at most 373,548 (median 219,574); over all 3,580 passes
   of the 150 s at most 376,317, 0.38% of the limit. With `task-switch.md`'s mean wait of 5.19
   million cycles at a median of about 216,000 spins, one spin takes about 24 cycles in MAME,
   so 100,000,000 spins are about 2.4 × 10^9 cycles, **about 16 s at 150 MHz**: a VBLANK that
   stops turns into a reboot only after some 16 s of spinning (inferred; the real R5000's spin
   rate may differ).
5. **Disk completion context.** Breakpoints on 0x800E7CF8 (movie callback) and 0x80149B40
   (wake) logging Status, sp and ra, and on 0x80149A44 (wait). Confirms that they run at
   interrupt level and whether wait is ever entered in attract mode; and whether wake is safe
   to call synchronously (log the process state words it changes).
   **Answer (measured, `disk-hooks.txt`):** the movie callback ran 3,122 times, every time at
   interrupt level (Status 0x30015C03: EXL and IE set), on the loader's exception stack (sp
   0x800C1358), called from the IDE completion path (return address 0x800124D4), with a0 = 0
   (success). Wait was entered in attract mode, 64 times, all during `loadwave`'s sound load
   (12.79 s to 22.23 s): at task level (Status 0x30015C01), on the task's own stack, called
   from the loader's queue path (return address 0x80011F58); its a0 held a leftover
   (0xAA000F00), not an argument. Each wait was followed by one wake, 64 in all, at interrupt
   level from the same completion path, with a0 = the cookie 0x803F75E0 (`loadwave`'s task
   record). Each wake changed exactly one word: the record's +0xC, from 0x00008001 to
   0x00000001 (bit 15 of the sleep-counter halfword cleared); nothing in the task-list words
   0x801A3110 to 0x801A3123 changed. So wake only makes the sleeping task due again, and calling
   it synchronously from inside the slot is harmless provided the slot then does not call wait
   (inferred from these words; wait itself was not read).
6. **Movie partition and geometry.** At the movie player's slot-0x158 call (0x800E7E2C) log a0,
   a1, a2 and the words 0x801DE92C, 0x801DE914, 0x801DE938. Gives the partition and sectors per
   frame.
   **Answer (measured, `disk-hooks.txt`; read for the address arithmetic):** the partition word
   was 13 for the attract movie (frames 0 to 2,545 from 22.6 s, then frames 2,475 to 2,545
   looped six times) and 15 for a second movie (frames 0 to 149 from 146.7 s). In the loader's
   1-based numbering these are the disk's part12 (start sector 0x00513CF0, 0x81E20 sectors)
   and part14 (start 0x005D75A6). Sectors per frame 0xD0 = 208 (104 KB a frame); a2 = 208 in
   every call. The logged a0 is the partition's start sector: the call's delay slot adds the
   frame offset (frame × sectors per frame, from `mflo`), so the LBA is start + frame × 208
   (read, 0x800E7DF0 to 0x800E7E30). The buffers (a1) cycle through 0x803F87B0, 0x8041F1B8 and
   0x80445BC0.
7. **The Voodoo's PCI device number.** Log the value stored through a3 by slot 0x110 at
   0x801570F8 and 0x801571D8.
   **Answer (measured, `voodoo-reads.txt`):** **8**. The vendor probe (slot 0x108, vendor
   0x121A) returned 1 for device 8 and 0 for 6, 7 and 9; slot 0x110 stored 8 and returned
   0xA8000000 for index 0 at both sites, and stored 10 and returned 0 for indexes 1 to 14.
   (`loader-calls.md` says "the fourth finds it"; it is the third probe, device 8.)
8. **GT64010 DMA programming.** Log every ioctl 0x7301 {offset, value} pair from 0x80168224 and
   0x801683EC and the descriptor chain at 0x830's value (32 bytes per record until the end
   marker), plus the time to the cause-bit-4 interrupt. Gives the exact DMA model.
   **Answer (measured, `dma0.txt`):** once per scheduler pass (3,580 times in 150 s), always
   in the same order: offset 0x840 (channel 0 control) = 0x0DC0, offset 0x830 (next record) =
   the chain's address, 0x840 = 0x2DC0, 0x840 = 0x1DC0 (bit 0x1000, the channel enable, set
   only in the last). Ioctl 0x7300 was never issued. The chain addresses alternate between
   0x002375C0 and 0x003175C0 (physical; two buffers). Each record is the 16-byte GT64010
   descriptor {byte count, source, destination, next}; the chain ends with next = 0 (no other
   end marker), and its last record always copies 4 bytes to 0x001B31B8, a word in game RAM,
   which is how the game sees completion besides the interrupt (inferred). The records are not
   32 bytes apiece: small ones (4 to 0x28 bytes) carry their source data right after the
   descriptor (source = record + 0x10, next 32 or 64 bytes on) and go to Voodoo registers
   (destinations 0x08000000 to 0x082FFFFF); the large ones copy 0x640 bytes from elsewhere in
   RAM to 0x084xxxxx (the Voodoo's frame-buffer range) with next = record + 0x10. Chains had
   28 records (the first, 116 bytes), 126, 195, 279, 564 to 576, or 386 (3,116 of the 3,580:
   256 large records, 411,540 bytes). The cause-bit-4 handler ran once per start, at a time
   from the enable write that depends only on the chain's shape (to within one cycle): 8,545
   cycles for 28 records, 37,054 for 126, 57,127 for 195, 81,563 for 279, 167,963 for 576 and
   1,079,381 (7.20 ms) for the 386-record chain. These fit about 400 cycles plus 291 per record
   for the small records, plus about 2.36 cycles per byte of the large ones (inferred from the
   fit; MAME's GT64010 DMA timing was not read). The descriptors are logged; the source data
   they copy is not.
9. **Break hooks.** Breakpoints on 0x800C4DA0 and 0x800C4E68 (were they ever hit? run 3 did not
   say) and, if hit, the frame + 0x110 before and after. Settles whether the hooks advance the
   PC or reboot.
   **Answer (measured, `breaks.txt`):** neither hook was hit, in 150 s of attract mode or in
   230 s with about 160 s of play. So no `break 6` or `break 7` executed, and whether the hooks
   advance the PC or reboot remains unobserved; they were not read either.
10. **What ioctl 0x6905 writes.** Log its arguments and callers; read MAME's IOASIC write
    handler for bus word 0 in the unlocked order.
    **Answer (measured, `breaks.txt`):** not issued in attract mode. In play it was issued 144
    times, four per coin for the 36 coins inserted: one frame after each coin press the game
    writes 0x10 (return address 0x800C7C24) and 0x01 (0x800C7C34), and four frames (about 74
    ms) later 0x10 (0x800C7C4C) and 0x00 (0x800C7C5C); all four calls are in the function
    0x800C7C04 (`config/game.functions.toml`), next to the task function `ccproc` 0x800C7B3C.
    So the game drives a pulse on bit 0, with 0x10 written before each change: a coin counter
    (inferred from the timing). MAME's handler was not read: the Linux container has no MAME source tree.
11. **FP exceptions in play.** A Lua breakpoint on 0x800C47E0 with Cause code 15 during a
    played game (scripted coin and start) shows whether FP traps ever happen; if they do, the
    catch-all reboot is observable.
    **Answer (measured, `breaks.txt`):** the catch-all was never entered, in attract mode or in
    the play run (run B's inputs, about 160 s of the first level): no FP trap and no other
    exception of codes 1 to 8 or 10 to 15. Play beyond the first part of the first level was
    not covered.

## Not done

- The file write path (0x80009040), create (0x80008904) and the directory update on close were
  read only in outline: allocation order, the date/time word the loader writes, and the
  duplicate-name behaviour of mode-3 open are not specified.
- The error returns of the seek routine 0x80008F64 were not read to the end; lseek's value on a
  failed seek is inferred.
- The tty driver was read in outline (enough for the stand-in); the UART set-up is not
  documented.
- The game's DCS protocol above the register level (which commands it sends and which replies
  it requires) was not traced here; `dcs-handshake.md` (2026-11-19) traces it in attract mode.
- The game's FPU handler 0x80150860's indexing of the FPR slots was not decoded, so it is not
  shown that it agrees with the FR = 0 layout above (it was written against this loader, so it
  should); section 6 shows the handler is unreachable after 0x800C4A08 anyway.
- The break hooks 0x800C4DA0 and 0x800C4E68 and the trap handler 0x80151A70 were not read.
- The debug monitor itself (0x8000D970 onwards) was not read beyond its entry conditions.
- The `sound` driver and 0x8000D370 were not read (the game does not use them).
- The PIC serial decode (0x8000AE2C), so the meaning of the three PIC words, was not read.
- Load-and-run (slot 0x198) was not re-read; its description is `boot-handoff.md`'s.
