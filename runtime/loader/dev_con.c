/* dev_con.c: the console, `con:` (the tty driver; descriptors 0 to 2 at the hand-off).
 *
 * docs/analysis/loader-services.md section 2, "tty": the board's console is the IOASIC UART.
 * Here a write goes to the host's standard output and to the console hook (the trace log);
 * there is no input, so a read returns 0 bytes and never blocks. The tty ioctls (0x6602,
 * 0x7401, 0x7405, 0x7406) are issued by the loader's own read and write and its key-hit test,
 * never by the game (section 3), so the driver has no ioctl routine and each stops.
 */
#include <stdio.h>
#include <stdlib.h>

#include "loader.h"

#define FL_POLLED 0x08u

static void (*hook)(int fd, const uint8_t *bytes, uint32_t n);

void loader_set_console_hook(void (*fn)(int fd, const uint8_t *bytes, uint32_t n))
{
    hook = fn;
}

void ld_con_reset(void)
{
    hook = NULL;
}

/* open: the tty driver's open marks the descriptor polled (section 2, flag 0x08) */
static int con_open(int fd, uint32_t name, int mode)
{
    (void)name, (void)mode;
    ld_desc((uint32_t)fd)->flags |= FL_POLLED;
    return 0;
}

/* read: section 8, no input, 0 bytes */
static int con_read(int fd, uint32_t buf, uint32_t n)
{
    (void)fd, (void)buf, (void)n;
    return 0;
}

/* write: section 8, every byte out, n returned */
static int con_write(int fd, uint32_t buf, uint32_t n)
{
    uint8_t *bytes;
    if (n > RAM_SIZE)
        ld_stop("write(%d, 0x%08X, %u): more bytes than RAM holds", fd, (unsigned)buf,
                (unsigned)n);
    bytes = malloc(n ? n : 1);
    if (!bytes)
        ld_stop("write(%d, 0x%08X, %u): out of host memory", fd, (unsigned)buf, (unsigned)n);
    for (uint32_t i = 0; i < n; i++)
        bytes[i] = (uint8_t)mem_r8(ld.ram, ld.c, buf + i);
    fwrite(bytes, 1, n, stdout);
    fflush(stdout);
    if (hook)
        hook(fd, bytes, n);
    free(bytes);
    return (int)n;
}

const struct ldev ld_con = {"con:", con_open, ld_ret0_close, con_read, con_write, NULL};
