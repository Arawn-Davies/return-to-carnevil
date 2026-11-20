/* fd.c: the descriptor table, the name table and open's resolution, the descriptor slots
 * (close, creat, open, read, write, ioctl with its generic commands, isatty), and the
 * placeholder `pci:` driver.
 *
 * docs/analysis/loader-services.md section 2 ("Descriptor table", "Descriptors open at the
 * hand-off", "Name table and open resolution") and the slot entries of section 1.
 */
#include <stddef.h>

#include "../disk/disk.h"
#include "loader.h"
#include "machine.h"

#define FL_MODE 0x03u
#define FL_NONBLOCK 0x04u /* ioctl 0x6601 */
#define FL_POLLED 0x08u   /* set by the console's open */
#define FL_6603 0x20u     /* ioctls 0x6603 and 0x6604 */

static struct ldesc table[LD_FDS];

/* The name table 0x80013180 in its order (section 2). An entry without a stand-in is a
 * driver the game never opens (led, mdiag, the IDE disk, scsi, wdog, sound) or the explicit
 * `file:` name, whose meaning to the file driver is not documented: opening one stops. */
static const struct {
    const char *name;
    const struct ldev *dev;
} NAMES[] = {
    {"led:", NULL},   {"ioasic:", &ld_ioasic}, {"pic:", &ld_pic},  {"pci:", &ld_pci},
    {"timer:", &ld_timer}, {"con:", &ld_con}, {"mdiag:", NULL},   {"c:", NULL},
    {"file:", NULL},  {"scsi:", NULL},       {"wdog:", NULL},     {"cmos:", &ld_cmos},
    {"snd:", NULL},
};

struct ldesc *ld_desc(uint32_t fd)
{
    return fd < LD_FDS ? &table[fd] : NULL;
}

struct ldesc *ld_open_desc(uint32_t fd)
{
    struct ldesc *d = ld_desc(fd);
    return d && d->flags ? d : NULL;
}

int ld_ret0_open(int fd, uint32_t name, int mode)
{
    (void)fd, (void)name, (void)mode;
    return 0;
}

int ld_ret0_close(int fd)
{
    (void)fd;
    return 0;
}

int ld_ret0_rw(int fd, uint32_t buf, uint32_t n)
{
    (void)fd, (void)buf, (void)n;
    return 0;
}

_Noreturn void ld_bad_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    ld_stop("ioctl(fd %d, 0x%04X, 0x%X) on %s: a command this driver does not have, or one "
            "section 3 does not show the game issuing", fd, (unsigned)cmd, (unsigned)arg,
            table[fd].dev->prefix);
}

/* Descriptors 0 to 5 as at the hand-off (sections 2 and 7): the console three times (fd 0
 * read and polled, fds 1 and 2 write and polled), cmos:, ioasic:, timer:. Descriptor 6 has
 * the boot probe's stale `scsi:` entry, which has no stand-in (NULL here); 7 to 15 are
 * empty. A file still open is closed in the file system. */
void ld_fd_reset(void)
{
    for (int i = 0; i < LD_FDS; i++)
        if (table[i].flags && table[i].dev == &ld_file)
            fs_close(table[i].fh);
    for (int i = 0; i < LD_FDS; i++)
        table[i] = (struct ldesc){0, NULL, -1};
    table[0] = (struct ldesc){1 | FL_POLLED, &ld_con, -1};
    table[1] = (struct ldesc){2 | FL_POLLED, &ld_con, -1};
    table[2] = (struct ldesc){2 | FL_POLLED, &ld_con, -1};
    table[3] = (struct ldesc){3, &ld_cmos, -1};
    table[4] = (struct ldesc){3, &ld_ioasic, -1};
    table[5] = (struct ldesc){3, &ld_timer, -1};
}

/* The comparison 0x80006D74: equal up to the end of the shorter string (section 2). */
static int agrees(const char *a, const char *b)
{
    for (; *a && *b; a++, b++)
        if (*a != *b)
            return 0;
    return 1;
}

/* 0x088 open, resolution and descriptor: section 2 */
int ld_open(uint32_t name, uint32_t mode)
{
    char s[LD_STRING_MAX];
    const struct ldev *dev = &ld_file;
    uint32_t m = mode & 0xFu;
    int fd;
    ld_string(name, s);
    /* only bits 0 to 3 are used; values above 2 leave the descriptor free (section 1), which
     * the game's opens (modes 0, 1 and 2, measured) never ask for */
    if (m > 2)
        ld_stop("open(\"%s\", mode 0x%X): mode 0x%X is none the game uses", s, (unsigned)mode,
                (unsigned)m);
    for (size_t i = 0; i < sizeof NAMES / sizeof NAMES[0]; i++) {
        if (!agrees(s, NAMES[i].name))
            continue;
        if (!NAMES[i].dev)
            ld_stop("open(\"%s\"): the name selects the loader's `%s` entry, which the game "
                    "never opens and which has no stand-in", s, NAMES[i].name);
        dev = NAMES[i].dev;
        break;
    }
    for (fd = 0; fd < LD_FDS && table[fd].flags; fd++)
        ;
    if (fd == LD_FDS)
        return -1;
    struct ldesc saved = table[fd];
    table[fd] = (struct ldesc){m + 1, dev, -1};
    if (dev->open(fd, name, (int)m) != 0) {
        table[fd] = saved;
        return -1;
    }
    return fd;
}

/* 0x088 open: section 1; v1 = 4 * fd on success */
void ld_slot_open(cpu_t *c)
{
    int fd = ld_open(c->r[4], c->r[5]);
    c->r[2] = (uint32_t)fd;
    if (fd >= 0)
        c->r[3] = 4u * (uint32_t)fd;
}

/* 0x028 creat: section 1, open(name, 1) with the fd stored at *fd_out; v1 = the result */
void ld_slot_creat(cpu_t *c)
{
    uint32_t fd_out = c->r[6];
    int fd = ld_open(c->r[4], 1);
    ld_put32(fd_out, (uint32_t)fd);
    c->r[2] = c->r[3] = fd >= 0 ? 0 : 0xFFFFFFFFu;
}

/* 0x018 close: section 1 */
void ld_slot_close(cpu_t *c)
{
    uint32_t fd = c->r[4];
    struct ldesc *d = ld_open_desc(fd);
    if (!d) {
        c->r[2] = 0xFFFFFFFFu;
        return;
    }
    c->r[2] = (uint32_t)d->dev->close((int)fd);
    d->flags = 0;
}

/* 0x090 read and 0x0C0 write: section 1; the console poll before each (ioctl 0x6602 on the
 * polled descriptors) has nothing to drain here, since the console has no input */
static void transfer(cpu_t *c, int write)
{
    uint32_t fd = c->r[4];
    struct ldesc *d = ld_open_desc(fd);
    int (*fn)(int, uint32_t, uint32_t);
    if (!d) {
        c->r[2] = 0xFFFFFFFFu;
        return;
    }
    fn = write ? d->dev->write : d->dev->read;
    if (!fn)
        ld_stop("%s on fd %u (%s): what this driver returns for it is not documented",
                write ? "write" : "read", (unsigned)fd, d->dev->prefix);
    c->r[2] = (uint32_t)fn((int)fd, c->r[5], c->r[6]);
}

void ld_slot_read(cpu_t *c)
{
    transfer(c, 0);
}

void ld_slot_write(cpu_t *c)
{
    transfer(c, 1);
}

/* 0x0C8 ioctl: section 1, the generic commands, then the driver's (section 3) */
void ld_slot_ioctl(cpu_t *c)
{
    uint32_t fd = c->r[4], cmd = c->r[5], arg = c->r[6];
    struct ldesc *d = ld_open_desc(fd);
    if (!d) {
        c->r[2] = 0xFFFFFFFFu;
        return;
    }
    if (cmd == 0x6601) {
        d->flags = arg ? d->flags | FL_NONBLOCK : d->flags & ~FL_NONBLOCK;
        c->r[2] = 0;
        return;
    }
    if (cmd == 0x6603)
        d->flags |= FL_6603;
    else if (cmd == 0x6604)
        d->flags &= ~FL_6603;
    if (!d->dev->ioctl)
        ld_bad_ioctl((int)fd, cmd, arg);
    c->r[2] = (uint32_t)d->dev->ioctl((int)fd, cmd, arg);
}

/* 0x0E0 isatty: section 1, by the name-table entry, whether or not the descriptor is open */
void ld_slot_isatty(cpu_t *c)
{
    struct ldesc *d = ld_desc(c->r[4]);
    c->r[2] = d && d->dev == &ld_con;
}

/* --- `pci:`, the sysctrl driver, until the next task ------------------------------------- */

/* open succeeds, since the sysctrl init flag is set at the hand-off (section 2); read and
 * write are not documented */
static int pci_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    ld_stop("ioctl(fd %d, 0x%04X, 0x%X) on pci: (the sysctrl driver) is not implemented yet",
            fd, (unsigned)cmd, (unsigned)arg);
}

const struct ldev ld_pci = {"pci:", ld_ret0_open, ld_ret0_close, NULL, NULL, pci_ioctl};
