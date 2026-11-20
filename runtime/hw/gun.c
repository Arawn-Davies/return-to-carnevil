/* gun.c: the CarnEvil gun's eight byte registers at physical 0x16800000, as the game sees
 * them with no input.
 *
 * docs/analysis/hardware-usage.md: "GAME.EXE" ("CarnEvil gun": byte reads of all eight
 * registers, P1 X low, X high, Y low, Y high at offsets 0x00 to 0x0C and P2 at 0x10 to 0x1C,
 * and writes of offsets 0 and 4) and "Measured values the runtime answers with" (the value
 * every read returned with no input, from mame/m2-facts/inputs.txt; offset 0 written by the
 * `sw` at 0x800D5430, offset 4 by the `sb` at 0x800D50C0). There is no state: the reads
 * return the measured values and the two writes are accepted without effect, since what they
 * do on the board was not measured. Every other offset or size stops. Gun input is not
 * modelled yet.
 */
#include "hw.h"

/* the no-input values by register (offset / 4), measured */
static const uint8_t NO_INPUT[8] = {0x00, 0x48, 0x00, 0x02, 0x00, 0x48, 0x00, 0x02};

uint32_t gun_read(cpu_t *c, uint32_t off, int size)
{
    if (size != 1 || (off & 3u))
        hw_stop(c, GUN_PHYS + off, size, 0, 0, "the gun is read with lbu at its eight word "
                "addresses only (hardware-usage.md, \"CarnEvil gun\")");
    return NO_INPUT[off / 4];
}

void gun_write(cpu_t *c, uint32_t off, int size, uint32_t value)
{
    if (!(off == 0 && size == 4) && !(off == 4 && size == 1))
        hw_stop(c, GUN_PHYS + off, size, 1, value, "the game writes the gun only with sw at "
                "offset 0 and sb at offset 4 (hardware-usage.md, \"CarnEvil gun\")");
}
