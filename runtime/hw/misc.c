/* misc.c: the sound FIFO at physical 0x13000000, the register at 0x17F00000, and hw_reset.
 *
 * docs/analysis/hardware-usage.md, "GAME.EXE" and "Dynamic check: the other hardware": the
 * game feeds the DCS2 sound FIFO with word writes (0x8013FF98) and never reads it; here the
 * writes are counted and go to the IOASIC's FIFO state in dcs_stub.c (its fill, which the
 * status bits and INTSTAT show); the words themselves are dropped. 0x17F00000 (MAME's "ASIC
 * reset", inferred to be the gun flash control) is read once with lb and written once with
 * sb by the game ("Measured values the runtime answers with": it read 0x0E and wrote 0x1E,
 * the value read with bit 4 set); here it is a byte register that starts at the value the
 * game read and keeps what is written. What the register does on the board is not modelled.
 */
#include "hw.h"

#define ARST_POWER_ON 0x0Eu /* the value the game's one read returned (measured) */

static struct {
    uint64_t fifo_writes;
    uint32_t arst;
} m;

void misc_reset(void)
{
    m.fifo_writes = 0;
    m.arst = ARST_POWER_ON;
}

void hw_reset(void)
{
    voodoo_reset();
    misc_reset();
    dcs_reset();
}

void sndfifo_write(cpu_t *c, int size, uint32_t value)
{
    if (size != 4)
        hw_stop(c, SNDFIFO_PHYS, size, 1, value, "the game writes the sound FIFO with sw only");
    m.fifo_writes++;
    dcs_fifo_write(c);
}

uint64_t sndfifo_writes(void)
{
    return m.fifo_writes;
}

uint32_t arst_read(cpu_t *c, int size)
{
    if (size != 1)
        hw_stop(c, ARST_PHYS, size, 0, 0, "the game reads 0x17F00000 with lb only");
    return m.arst;
}

void arst_write(cpu_t *c, int size, uint32_t value)
{
    if (size != 1)
        hw_stop(c, ARST_PHYS, size, 1, value, "the game writes 0x17F00000 with sb only");
    m.arst = value;
}
