/* bus.c: bus_read and bus_write (recomp.h): the address folded to a physical one, then the
 * device behind it (runtime/hw/hw.h) or the CMOS (runtime/loader/loader.h, cmos_byte).
 *
 * docs/analysis/hardware-usage.md, "Decisions for the runtime" item 2, lists what GAME.EXE's
 * own code touches: the Voodoo at physical 0x08000000 to 0x08FFFFFF, the sound FIFO at
 * 0x13000000, the CMOS at 0x16100000 (read only: "The game writes no CMOS byte"), the gun at
 * 0x16800000 and the register at 0x17F00000. Every other address, a CMOS write, and kuseg
 * outside RAM stop (hw_stop): the runtime does not answer an access it does not model.
 *
 * Folding: kseg0 (0x80000000 to 0x9FFFFFFF) and kseg1 (0xA0000000 to 0xBFFFFFFF) are the
 * physical address & 0x1FFFFFFF. kuseg is mapped by the loader's TLB entries onto RAM only
 * (recomp.h, "kuseg is mapped onto RAM"), so a kuseg address that reaches the bus is a TLB
 * exception on the board, not a device; kseg2 and kseg3 (0xC0000000 up) are mapped by no TLB
 * entry either. Both stop.
 */
#include <stdarg.h>
#include <stdio.h>

#include "../hw/hw.h"
#include "../loader/loader.h"
#include "machine.h"

#define CMOS_PHYS 0x16100000u /* byte n at CMOS_PHYS + 4n (hardware-usage.md, "CMOS") */

_Noreturn void hw_stop(cpu_t *c, uint32_t phys, int size, int write, uint32_t value,
                       const char *fmt, ...)
{
    char why[256], val[32] = "";
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(why, sizeof why, fmt, ap);
    va_end(ap);
    if (write)
        snprintf(val, sizeof val, " of 0x%X", (unsigned)value);
    rt_stop(RT_STOP_STATUS, "bus %s of %d byte%s%s at physical 0x%08X: %s (pc as last stored "
            "0x%08X, exact only in a --trace-pc build)", write ? "write" : "read", size,
            size == 1 ? "" : "s", val, (unsigned)phys, why, (unsigned)c->pc);
}

/* The physical address of a kseg0 or kseg1 address; kuseg and kseg2/3 stop. */
static uint32_t fold(cpu_t *c, uint32_t addr, int size, int write, uint32_t value)
{
    if (addr >= 0x80000000u && addr <= 0xBFFFFFFFu)
        return addr & 0x1FFFFFFFu;
    if (addr < 0x80000000u)
        rt_stop(RT_STOP_STATUS, "bus %s of %d byte%s at 0x%08X: kuseg outside the RAM the "
                "loader's TLB entries map, a TLB exception on the board (value 0x%X; pc as last "
                "stored 0x%08X, exact only in a --trace-pc build)", write ? "write" : "read",
                size, size == 1 ? "" : "s", (unsigned)addr, (unsigned)value, (unsigned)c->pc);
    rt_stop(RT_STOP_STATUS, "bus %s of %d byte%s at 0x%08X: kseg2/kseg3, which no TLB entry "
            "maps (value 0x%X; pc as last stored 0x%08X, exact only in a --trace-pc build)",
            write ? "write" : "read", size, size == 1 ? "" : "s", (unsigned)addr,
            (unsigned)value, (unsigned)c->pc);
}

/* The stop of a region nothing here models, naming the guest address as well. */
static _Noreturn void unmodelled(cpu_t *c, uint32_t addr, uint32_t phys, int size, int write,
                                 uint32_t value)
{
    hw_stop(c, phys, size, write, value, "0x%08X is no device the runtime models "
            "(hardware-usage.md: GAME.EXE's own code touches only the Voodoo, the sound FIFO, "
            "CMOS reads, the gun and 0x17F00000)", (unsigned)addr);
}

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    uint32_t phys = fold(c, addr, size, 0, 0);
    if (phys - VOODOO_PHYS < VOODOO_SIZE)
        return voodoo_read(c, phys - VOODOO_PHYS, size);
    if (phys - GUN_PHYS < GUN_SIZE)
        return gun_read(c, phys - GUN_PHYS, size);
    if (phys == ARST_PHYS)
        return arst_read(c, size);
    if (phys == SNDFIFO_PHYS)
        hw_stop(c, phys, size, 0, 0, "0x%08X is the sound FIFO, which the game only writes",
                (unsigned)addr);
    if (phys - CMOS_PHYS < 4 * CMOS_SIZE) {
        /* one byte per word, read with lbu (hardware-usage.md, "CMOS"; the CRC loop at
         * 0x801458C0) */
        if (size != 1 || (phys & 3u))
            hw_stop(c, phys, size, 0, 0, "0x%08X: CMOS reads are of the byte at a word's "
                    "address only (what other sizes or addresses give is not measured)",
                    (unsigned)addr);
        return cmos_byte((phys - CMOS_PHYS) / 4);
    }
    unmodelled(c, addr, phys, size, 0, 0);
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    uint32_t phys = fold(c, addr, size, 1, value);
    if (phys - VOODOO_PHYS < VOODOO_SIZE)
        voodoo_write(c, phys - VOODOO_PHYS, size, value);
    else if (phys - GUN_PHYS < GUN_SIZE)
        gun_write(c, phys - GUN_PHYS, size, value);
    else if (phys == ARST_PHYS)
        arst_write(c, size, value);
    else if (phys == SNDFIFO_PHYS)
        sndfifo_write(c, size, value);
    else if (phys - CMOS_PHYS < 4 * CMOS_SIZE)
        hw_stop(c, phys, size, 1, value, "0x%08X: a direct CMOS write; the game writes the "
                "CMOS only through `cmos:` (hardware-usage.md, \"CMOS\")", (unsigned)addr);
    else
        unmodelled(c, addr, phys, size, 1, value);
}
