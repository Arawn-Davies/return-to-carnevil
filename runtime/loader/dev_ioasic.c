/* dev_ioasic.c: the IOASIC's registers, `ioasic:` (descriptor 4 at the hand-off).
 *
 * docs/analysis/loader-services.md section 3 (`ioasic:`): plain register accessors; a read
 * stores register & 0xFFFF through arg, a write takes arg & 0xFFFF. The input ports come
 * from ioasic_set_inputs; INTCTL is machine_t's word (the dispatcher clears and sets its
 * bit 14 around every exception); the sound-board commands go to dcs_ioctl. Section 2: only
 * init and ioctl, so open, close, read and write are the shared return-0 routine.
 */
#include <stdio.h>

#include "loader.h"
#include "machine.h"

/* INTSTAT (bus word 7) at the game's entry, measured (loader-services.md, open question 1).
 * Nothing in this task raises an IOASIC interrupt, so it is also every later read's value;
 * the sound-board task owns its dynamic bits. */
#define IOASIC_INTSTAT_IDLE 0x20C9u

static uint16_t ports[4]; /* PORT0 to PORT3 */
static int ports_set;

void ioasic_set_inputs(const uint16_t p[4])
{
    for (int i = 0; i < 4; i++)
        ports[i] = p[i];
    ports_set = 1;
}

void ld_ioasic_reset(void)
{
    ports_set = 0;
}

/* The sound-board task replaces this definition. */
int dcs_ioctl(cpu_t *c, uint32_t cmd, uint32_t arg)
{
    (void)c, (void)arg;
    ld_stop("sound board ioctl 0x%04X is not implemented yet", (unsigned)cmd);
}

/* ioctl: section 3, `ioasic:` */
static int ioasic_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    switch (cmd) {
    case 0x6900: /* PORT0 (bus word 3, DIP switches) */
    case 0x6901: /* PORT1 (bus word 4, player inputs) */
    case 0x6902: /* PORT2 (bus word 2) */
    case 0x6903: /* PORT3 (bus word 1) */
        if (!ports_set)
            ld_stop("ioctl 0x%04X reads an input port before ioasic_set_inputs gave them",
                    (unsigned)cmd);
        ld_put32(arg, ports[cmd - 0x6900]);
        return 0;
    case 0x6905: /* COIN register write: a coin counter pulse (section 3, measured), logged */
        fprintf(stderr, "carnevil: ioasic: ioctl 0x6905 (COIN register write) 0x%X, ra "
                "0x%08X\n", (unsigned)(arg & 0xFFFFu), (unsigned)ld.c->r[31]);
        return 0;
    case 0x6906: /* SOUNDCTL write */
    case 0x6907: /* SOUNDCTL read */
    case 0x6908: /* SOUNDOUT write */
    case 0x6909: /* SOUNDSTAT read */
    case 0x690A: /* SOUNDIN read */
    case 0x6911: /* SOUNDIN write, the acknowledgement */
        return dcs_ioctl(ld.c, cmd, arg);
    case 0x690E: /* INTSTAT read */
        ld_put32(arg, IOASIC_INTSTAT_IDLE & 0xFFFFu);
        return 0;
    case 0x690F: /* INTCTL write */
        machine.intctl = arg & 0xFFFFu;
        return 0;
    case 0x6910: /* INTCTL read */
        ld_put32(arg, machine.intctl & 0xFFFFu);
        return 0;
    default: /* 0x6904 (COIN read), 0x690B to 0x690D (the PIC registers): not issued */
        ld_bad_ioctl(fd, cmd, arg);
    }
}

const struct ldev ld_ioasic = {"ioasic:", ld_ret0_open, ld_ret0_close, ld_ret0_rw, ld_ret0_rw,
                               ioasic_ioctl};
