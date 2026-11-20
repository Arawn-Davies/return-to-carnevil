/* loader.h: the native stand-ins for the resident boot loader's services, which GAME.EXE
 * calls through the 29 slots of the stub block (rt_loader, recomp.h).
 *
 * docs/analysis/loader-services.md is the reference: section 1 for each slot, section 2 for
 * the descriptors, the name table and the drivers, section 3 for every ioctl the game issues,
 * section 7 for the start-up state. The loader is a small DOS-style C library: a table of 16
 * descriptors, devices opened by name through a name table, files on partition 1 of the disk
 * (runtime/disk/disk.h), ioctls per driver.
 *
 * The files: slots.c (rt_loader and the slot table, the handler registrations), fd.c (the
 * descriptor table, the name table, open/close/read/write/ioctl/isatty and the generic
 * ioctls), dev_file.c (the file driver and the file slots: find-first, unlink, rename,
 * lseek), and one file per device driver: dev_con.c, dev_cmos.c, dev_pic.c, dev_timer.c,
 * dev_ioasic.c.
 *
 * Every request the documents do not show the game making (a slot that is not one of the
 * 29, an ioctl command section 3 does not list, a command on a driver that does not have it,
 * an argument outside what the game passes) stops the program (rt_stop, RT_STOP_STATUS)
 * with a message naming the slot, the request, a0 to a3 and ra: the generated code stores
 * no pc at a loader call, so ra identifies the call site. Errors the loader itself returns
 * for requests the game is known to make (a bad descriptor, an absent file, a read at end of
 * file) are returned as documented.
 *
 * Guest memory is reached only through recomp.h's mem_r8/16/32 and mem_w8/16/32 with the
 * guest address, so a bad pointer from the game goes to the bus and stops there instead of
 * touching host memory.
 *
 * Leftover registers: each slot sets v0 as section 1 gives it, and v1 where section 1 gives
 * it ("Leftover registers after a slot call"); a slot section 1 gives no return value for
 * leaves v0 as it was. No other caller-saved register is written: the game reads none after
 * a loader call.
 */
#ifndef CARNEVIL_LOADER_H
#define CARNEVIL_LOADER_H

#include <stdint.h>

#include "recomp.h"

/* --- for the rest of the runtime --------------------------------------------------------- */

/* loader_reset: descriptors 0 to 5 open as at the hand-off (section 7) and the rest free,
 * every file handle closed, the CMOS blank (every byte 0xFF) at position 0xB6100000 with no
 * file behind it, timers 1 to 3 not started, the input ports at their no-input values, no
 * console hook. For tests and
 * for the start of a run; machine_reset does not call it. */
void loader_reset(void);

/* loader_set_console_hook: fn (or NULL) is called with every console write (fd and bytes),
 * after the bytes have gone to the host's standard output. */
void loader_set_console_hook(void (*fn)(int fd, const uint8_t *bytes, uint32_t n));

/* The CMOS (NVRAM, `cmos:`): 32,768 bytes. cmos_load reads them from path, creating the file
 * blank (every byte 0xFF, the fill MAME gives a blank NVRAM: section 1, open question 1) when
 * it does not exist; a file of another size, or one that cannot be read or created, stops.
 * It registers cmos_save with rt_at_stop. cmos_save writes them back to the path through a
 * temporary file next to it and a rename, so a crash never leaves half a file; without a
 * path it does nothing. cmos_byte: byte offset (0 to 0x7FFF; others stop), for the bus. */
#define CMOS_SIZE 0x8000u
#define CMOS_BLANK 0xFFu
void cmos_load(const char *path);
void cmos_save(void);
uint8_t cmos_byte(uint32_t offset);

/* ioasic_set_inputs: the four input ports (PORT0 to PORT3, which ioctls 0x6900 to 0x6903
 * read: section 3), replacing the measured no-input values loader_reset gives them. */
void ioasic_set_inputs(const uint16_t ports[4]);

/* dcs_ioctl: the `ioasic:` sound-board commands 0x6906 to 0x690A and 0x6911 (section 3),
 * with the guest state of the call. Defined in dev_ioasic.c as a stop until the sound-board
 * task replaces the definition. Returns the ioctl's result. */
int dcs_ioctl(cpu_t *c, uint32_t cmd, uint32_t arg);

/* --- between runtime/loader's files ------------------------------------------------------ */

/* The call being served: rt_loader sets it for the duration of a slot. */
struct ld_call {
    uint8_t *ram;
    cpu_t *c;
    uint32_t slot;
};
extern struct ld_call ld;

/* ld_stop: stops with "loader slot 0x... (name): " and the message, then a0 to a3 and ra of
 * the call. */
_Noreturn void ld_stop(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* ld_string: the NUL-terminated guest string at addr into out (at most LD_STRING_MAX bytes
 * with its NUL); an unterminated one stops. */
#define LD_STRING_MAX 256
void ld_string(uint32_t addr, char out[LD_STRING_MAX]);
/* ld_put32: a 32-bit store through a guest pointer (an ioctl's "*arg"). */
void ld_put32(uint32_t addr, uint32_t value);

/* A driver: the routines of the loader's driver table that the game reaches. open returns 0
 * or -1 (name is the guest address of the name, for the file driver); close, read and write
 * return the slot's result; buf is a guest address. A NULL routine is one the documents do
 * not give a result for: calling it stops. ioctl gets the commands the generic layer passes
 * on (section 1, slot 0x0C8). */
struct ldev {
    const char *prefix;
    int (*open)(int fd, uint32_t name, int mode);
    int (*close)(int fd);
    int (*read)(int fd, uint32_t buf, uint32_t n);
    int (*write)(int fd, uint32_t buf, uint32_t n);
    int (*ioctl)(int fd, uint32_t cmd, uint32_t arg);
};
extern const struct ldev ld_con, ld_cmos, ld_ioasic, ld_timer, ld_pic, ld_pci, ld_file;

/* ld_bad_ioctl: the stop for a command a driver does not have. The ld_ret0_* routines are
 * the loader's shared return-0 routine 0x80006984, which fills every routine slot a driver
 * does not implement (hardware-usage.md, "Loader reachability"; section 1, slot 0x018, for
 * the closes). */
_Noreturn void ld_bad_ioctl(int fd, uint32_t cmd, uint32_t arg);
int ld_ret0_open(int fd, uint32_t name, int mode);
int ld_ret0_close(int fd);
int ld_ret0_rw(int fd, uint32_t buf, uint32_t n);

/* The descriptor table (section 2). flags as the loader's: bits 0 to 1 the access mode (0
 * free), 0x04 non-blocking console, 0x08 console polled, 0x20 set by ioctl 0x6603. dev is
 * the name-table entry's driver; it outlives a close, as the loader's entry pointer does
 * (isatty reads it). fh is the file system's handle of a file descriptor. */
#define LD_FDS 16
struct ldesc {
    uint32_t flags;
    const struct ldev *dev;
    int fh;
};
/* ld_desc: descriptor fd, NULL for fd 16 or more; ld_open_desc: the same, also NULL for a
 * free descriptor. */
struct ldesc *ld_desc(uint32_t fd);
struct ldesc *ld_open_desc(uint32_t fd);

/* The slots each file serves, called by rt_loader with the arguments in a0 to a3; each sets
 * v0 (and v1) itself. */
void ld_slot_close(cpu_t *c);
void ld_slot_creat(cpu_t *c);
void ld_slot_open(cpu_t *c);
void ld_slot_read(cpu_t *c);
void ld_slot_write(cpu_t *c);
void ld_slot_ioctl(cpu_t *c);
void ld_slot_isatty(cpu_t *c);
void ld_slot_findfirst(cpu_t *c);
void ld_slot_unlink(cpu_t *c);
void ld_slot_rename(cpu_t *c);
void ld_slot_lseek(cpu_t *c);
/* open as slot 0x088 does it, for creat too: the fd, or -1. */
int ld_open(uint32_t name, uint32_t mode);

/* Each driver's state to its start-up value, for loader_reset. */
void ld_fd_reset(void);
void ld_con_reset(void);
void ld_cmos_reset(void);
void ld_timer_reset(void);
void ld_ioasic_reset(void);

#endif
