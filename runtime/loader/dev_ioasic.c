/* dev_ioasic.c: the IOASIC's registers, `ioasic:` (descriptor 4 at the hand-off).
 *
 * docs/analysis/loader-services.md section 3 (`ioasic:`): plain register accessors; a read
 * stores register & 0xFFFF through arg, a write takes arg & 0xFFFF. The input ports start
 * at the measured no-input values and ioasic_set_inputs replaces them; INTCTL is machine_t's
 * word (the dispatcher clears and sets its bit 14 around every exception), and a write of it
 * re-derives the IP3 level; INTSTAT and the sound-board commands come from the sound board's
 * model (runtime/hw/dcs_stub.c). Section 2: only init and ioctl, so open, close, read and
 * write are the shared return-0 routine.
 */
#include <stdio.h>

#include "hw.h"
#include "loader.h"
#include "machine.h"

/* The INTCTL bits the game's writes set (see 0x690F below). */
#define INTCTL_MEASURED 0x4009u

/* PORT0 to PORT3 with no input, as the game read them in MAME (measured: loader-services.md
 * section 3, `ioasic:`, "With no input"). */
#define PORT0_IDLE 0xBDFEu
#define PORT1_IDLE 0xFFFFu
#define PORT2_IDLE 0xFFFFu
#define PORT3_IDLE 0xFFFFu

static uint16_t ports[4]; /* PORT0 to PORT3 */

void ioasic_set_inputs(const uint16_t p[4])
{
    for (int i = 0; i < 4; i++)
        ports[i] = p[i];
}

void ld_ioasic_reset(void)
{
    ports[0] = PORT0_IDLE;
    ports[1] = PORT1_IDLE;
    ports[2] = PORT2_IDLE;
    ports[3] = PORT3_IDLE;
}

/* ioctl: section 3, `ioasic:` */
static int ioasic_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    switch (cmd) {
    case 0x6900: /* PORT0 (bus word 3, DIP switches) */
    case 0x6901: /* PORT1 (bus word 4, player inputs) */
    case 0x6902: /* PORT2 (bus word 2) */
    case 0x6903: /* PORT3 (bus word 1) */
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
        ld_put32(arg, dcs_intstat(ld.c) & 0xFFFFu);
        return 0;
    case 0x690F: /* INTCTL write: its enables decide the IP3 level (dcs_stub.c) */
        /* the game wrote 0x4000 6 times, 0x4008 once and 0x4009 once (measured,
         * dcs-trace.txt): bits 14, 3 and 0. Another enable could make an INTSTAT bit the model
         * holds steady (0x0080 is always set) a permanent IP3, which the board never showed.
         * Bit 14 is among them; the dispatcher's own sets of it do not come through here. */
        if ((arg & 0xFFFFu) & ~INTCTL_MEASURED)
            ld_stop("INTCTL write 0x%04X enables bits the game was never seen to (it wrote "
                    "0x4000, 0x4008 and 0x4009)", (unsigned)(arg & 0xFFFFu));
        machine.intctl = arg & 0xFFFFu;
        dcs_irq_update(ld.c);
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
