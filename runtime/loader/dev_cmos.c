/* dev_cmos.c: the CMOS (NVRAM), `cmos:` (descriptor 3 at the hand-off), and its host file.
 *
 * docs/analysis/loader-services.md section 2 ("cmos") and section 3 (`cmos:`): 32 KB of byte
 * cells, each a 32-bit word of the bus at 0xB6100000 + 4 x offset. The position word holds
 * such an address; read and write move n bytes from the position onwards, clamped at the end
 * of the CMOS, and never advance the position.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "loader.h"
#include "machine.h"

#define CMOS_BASE 0xB6100000u /* the position of byte 0 (section 3, 0x6301) */
#define CMOS_END 0xB6120000u  /* transfers are clamped to end here (section 2) */

static uint8_t cells[CMOS_SIZE];
static uint32_t pos;  /* the position word 0x80014D78 */
static char *path;    /* the host file, NULL for memory only */
static int saving;    /* cmos_save is registered with rt_at_stop */

void ld_cmos_reset(void)
{
    memset(cells, CMOS_BLANK, sizeof cells);
    pos = CMOS_BASE;
    free(path);
    path = NULL;
    saving = 0;
}

void cmos_save(void)
{
    char *tmp;
    FILE *f;
    if (!path)
        return;
    tmp = malloc(strlen(path) + 5);
    if (!tmp)
        rt_stop(RT_STOP_STATUS, "cmos_save: out of host memory");
    strcpy(tmp, path);
    strcat(tmp, ".tmp");
    f = fopen(tmp, "wb");
    if (!f || fwrite(cells, 1, sizeof cells, f) != sizeof cells || fclose(f) != 0)
        rt_stop(RT_STOP_STATUS, "cmos_save: cannot write %s: %s", tmp, strerror(errno));
    if (rename(tmp, path) != 0)
        rt_stop(RT_STOP_STATUS, "cmos_save: cannot rename %s to %s: %s", tmp, path,
                strerror(errno));
    free(tmp);
}

void cmos_load(const char *file)
{
    FILE *f = fopen(file, "rb");
    free(path);
    path = malloc(strlen(file) + 1);
    if (!path)
        rt_stop(RT_STOP_STATUS, "cmos_load: out of host memory");
    strcpy(path, file);
    if (!f) {
        if (errno != ENOENT)
            rt_stop(RT_STOP_STATUS, "cmos_load: cannot open %s: %s", file, strerror(errno));
        memset(cells, CMOS_BLANK, sizeof cells); /* a blank NVRAM, created now */
        cmos_save();
    } else {
        long size;
        if (fseek(f, 0, SEEK_END) != 0 || (size = ftell(f)) < 0 || fseek(f, 0, SEEK_SET) != 0)
            rt_stop(RT_STOP_STATUS, "cmos_load: cannot read %s: %s", file, strerror(errno));
        if (size != (long)CMOS_SIZE)
            rt_stop(RT_STOP_STATUS, "cmos_load: the NVRAM file %s holds %ld bytes, not the "
                    "CMOS's %u", file, size, CMOS_SIZE);
        if (fread(cells, 1, sizeof cells, f) != sizeof cells)
            rt_stop(RT_STOP_STATUS, "cmos_load: cannot read %s", file);
        fclose(f);
    }
    if (!saving) {
        rt_at_stop(cmos_save);
        saving = 1;
    }
}

uint8_t cmos_byte(uint32_t offset)
{
    if (offset >= CMOS_SIZE)
        rt_stop(RT_STOP_STATUS, "cmos_byte: offset 0x%X is outside the 32 KB CMOS",
                (unsigned)offset);
    return cells[offset];
}

/* The first cell of a transfer of n bytes and the clamped count (section 2). */
static uint32_t span(uint32_t n, uint32_t *off)
{
    uint32_t left;
    if (pos & 3u)
        ld_stop("the CMOS position 0x%08X (set by ioctl 0x6300) is not word aligned, so the "
                "cells a transfer reaches are not documented", (unsigned)pos);
    *off = (pos - CMOS_BASE) / 4;
    left = (CMOS_END - pos) / 4;
    return n < left ? n : left;
}

/* read: section 2, bytes from the position on, position unchanged, n clamped */
static int cmos_read(int fd, uint32_t buf, uint32_t n)
{
    uint32_t off;
    (void)fd;
    n = span(n, &off);
    for (uint32_t i = 0; i < n; i++)
        mem_w8(ld.ram, ld.c, buf + i, cells[off + i]);
    return (int)n;
}

/* write: section 2, as read (the write-enable strobe before each byte is not modelled) */
static int cmos_write(int fd, uint32_t buf, uint32_t n)
{
    uint32_t off;
    (void)fd;
    n = span(n, &off);
    for (uint32_t i = 0; i < n; i++)
        cells[off + i] = (uint8_t)mem_r8(ld.ram, ld.c, buf + i);
    return (int)n;
}

/* ioctl: section 3, `cmos:` */
static int cmos_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    switch (cmd) {
    case 0x6300: /* set position: accepted when arg + 0x49F00000 <= 0x1FFFF, else -2 */
        if (arg - CMOS_BASE > 0x1FFFFu)
            return -2;
        pos = arg;
        return 0;
    case 0x6301: /* get base */
        ld_put32(arg, CMOS_BASE);
        return 0;
    case 0x6302: /* get size: the word 0x80014D7C, 0x8000 (section 7) */
        ld_put32(arg, CMOS_SIZE);
        return 0;
    default:
        ld_bad_ioctl(fd, cmd, arg);
    }
}

/* open is the shared return-0 routine (inferred: section 2 documents no open of its own,
 * and the game uses descriptor 3, opened by the loader) */
const struct ldev ld_cmos = {"cmos:", ld_ret0_open, ld_ret0_close, cmos_read, cmos_write,
                             cmos_ioctl};
