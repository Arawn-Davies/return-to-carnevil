/* slots.c: rt_loader and the slot table, the helpers every stand-in uses (the call being
 * served, stops, guest strings and stores), loader_reset, and the slots that are not about
 * descriptors: exit, the data-cache flush, the PCI configuration slots, load-and-run, the
 * argument block and the handler registrations. The disk slots are in diskslots.c.
 *
 * docs/analysis/loader-services.md section 1 gives each slot; section 8 says what each
 * stand-in must do.
 */
#include <stdarg.h>
#include <stdio.h>

#include "../hw/hw.h"
#include "loader.h"
#include "machine.h"

struct ld_call ld;

/* The argument block 0x1A8 hands out: the 4 words load-and-run copied (section 1). */
#define EXEC_ARGS 0x800B6E30u

/* --- helpers ------------------------------------------------------------------------------ */

static const char *slot_name(uint32_t slot);

_Noreturn void ld_stop(const char *fmt, ...)
{
    char why[512];
    const char *name = slot_name(ld.slot);
    const cpu_t *c = ld.c;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(why, sizeof why, fmt, ap);
    va_end(ap);
    rt_stop(RT_STOP_STATUS, "loader slot 0x%03X%s%s%s: %s (a0 0x%08X, a1 0x%08X, a2 0x%08X, "
            "a3 0x%08X, ra 0x%08X)", (unsigned)ld.slot, name ? " (" : "", name ? name : "",
            name ? ")" : "", why, (unsigned)c->r[4], (unsigned)c->r[5], (unsigned)c->r[6],
            (unsigned)c->r[7], (unsigned)c->r[31]);
}

void ld_string(uint32_t addr, char out[LD_STRING_MAX])
{
    for (uint32_t i = 0; i < LD_STRING_MAX; i++) {
        out[i] = (char)mem_r8(ld.ram, ld.c, addr + i);
        if (!out[i])
            return;
    }
    ld_stop("the string at 0x%08X is unterminated (no NUL in %d bytes)", (unsigned)addr,
            LD_STRING_MAX);
}

void ld_put32(uint32_t addr, uint32_t value)
{
    /* the loader stores with sw, which on the board raises an address error for an
     * unaligned pointer */
    if (addr & 3u)
        ld_stop("the result pointer 0x%08X is not word aligned", (unsigned)addr);
    mem_w32(ld.ram, ld.c, addr, value);
}

static void (*call_hook)(cpu_t *c, uint32_t slot, int after);

void loader_set_call_hook(void (*fn)(cpu_t *c, uint32_t slot, int after))
{
    call_hook = fn;
}

void loader_reset(void)
{
    call_hook = NULL;
    ld_fd_reset();
    ld_con_reset();
    ld_cmos_reset();
    ld_timer_reset();
    ld_ioasic_reset();
    ld_sysctrl_reset();
    ld_disk_reset();
}

/* --- the slots served here ---------------------------------------------------------------- */

/* 0x0D8 _exit: section 1, a break 0x402 into the debug monitor, which does not come back */
static void s_exit(cpu_t *c)
{
    ld_stop("_exit(%d): the game ended; the board enters its debug monitor",
            (int)(int32_t)c->r[4]);
}

/* 0x0E8 dcache_flush: section 1, nothing to do without a cache model; v0 is not set */
static void s_dcache_flush(cpu_t *c)
{
    (void)c;
}

/* 0x198 exec: section 1 and boot-handoff.md; out of scope for M2 */
static void s_exec(cpu_t *c)
{
    char name[LD_STRING_MAX];
    ld_string(c->r[4], name);
    ld_stop("exec(\"%s\"): load-and-run of another program is not part of the port", name);
}

/* 0x1A8 exec_args: section 1, the 4 argument words, all zero at the hand-off */
static void s_exec_args(cpu_t *c)
{
    for (uint32_t i = 0; i < 4; i++)
        mem_w32(ld.ram, c, EXEC_ARGS + 4 * i, 0);
    c->r[2] = EXEC_ARGS;
}

/* 0x1E0 gt_set_handler: section 1; v0 = bit * 4 on the normal path */
static void s_gt_set_handler(cpu_t *c)
{
    uint32_t bit = c->r[4];
    if (bit > 31)
        ld_stop("bit %u: the loader does nothing for it and its v0 is not documented",
                (unsigned)bit);
    irq_set_gt((int)bit, c->r[5]);
    vclock_reload(c); /* IP2 may have changed */
    c->r[2] = bit * 4;
}

/* 0x1E8 fpe_set_handler: section 1; cause 0 to 4 set or clear the FCSR enable bit
 * 0x80 << cause, through rt_set_fcsr so the host state follows; no useful v0 */
static void s_fpe_set_handler(cpu_t *c)
{
    uint32_t cause = c->r[4], fn = c->r[5];
    if (cause > 5)
        ld_stop("cause %u: the loader does nothing for it and its v0 is not documented",
                (unsigned)cause);
    exc_set_fpe((int)cause, fn);
    if (cause < 5) {
        uint32_t enable = 0x80u << cause, fcsr = rt_get_fcsr(c);
        rt_set_fcsr(c, fn ? fcsr | enable : fcsr & ~enable);
    }
}

/* 0x1F0, 0x1F8 the break 7 and break 6 hooks: section 1; no return value */
static void s_break7_hook(cpu_t *c)
{
    exc_set_break_hook(7, c->r[4]);
}

static void s_break6_hook(cpu_t *c)
{
    exc_set_break_hook(6, c->r[4]);
}

/* 0x200 irq_set_handler: section 1; for line 0 it also sets (or clears) the board's VBLANK
 * enable, which lets the Voodoo's retrace (runtime/hw) reach IP6; v0 = line * 4 (the delay
 * slot at 0x800062E0, after the 8 of the search) */
static void s_irq_set_handler(cpu_t *c)
{
    uint32_t line = c->r[4], fn = c->r[5];
    if (line > 4)
        ld_stop("line %u: no IP maps to it; the loader does nothing and its v0 is not "
                "documented", (unsigned)line);
    irq_set_line((int)line, fn);
    vclock_reload(c); /* a line now unmasked */
    c->r[2] = line * 4;
}

/* --- the PCI configuration slots ---------------------------------------------------------- */

/* The board's PCI devices as the game sees them (section 1, slots 0x0F0 to 0x110; open
 * question 7, measured): the Voodoo, vendor 0x121A, is device 8; the vendor probes of devices
 * 6, 7 and 9 returned 0. What an absent device's configuration words read as was not
 * measured, so any other device, or another vendor on 6, 7 and 9, stops. */
#define VENDOR_3DFX 0x121Au
#define PCI_SCAN_FIRST 6u
#define PCI_SCAN_LAST 9u
#define PCI_NOT_FOUND 10u
#define VOODOO_BAR0 0x08000000u
#define VOODOO_KSEG1 0xA8000000u

static uint32_t cfg_word(uint32_t reg)
{
    return (reg >> 2) & 0x3Fu;
}

static uint32_t cfg_read(uint32_t dev, uint32_t word)
{
    const char *why = "";
    uint32_t v;
    if (dev != VOODOO_PCI_DEV)
        ld_stop("PCI device %u: no configuration read of a device other than the Voodoo (8) "
                "was measured", (unsigned)dev);
    if (voodoo_cfg_read(word, &v, &why) != 0)
        ld_stop("device 8, configuration word 0x%02X: %s", (unsigned)word, why);
    return v;
}

static void cfg_write(uint32_t dev, uint32_t word, uint32_t value)
{
    const char *why = "";
    if (dev != VOODOO_PCI_DEV)
        ld_stop("PCI device %u: no configuration write to a device other than the Voodoo (8) "
                "was measured", (unsigned)dev);
    if (voodoo_cfg_write(word, value, &why) != 0)
        ld_stop("device 8, configuration word 0x%02X = 0x%08X: %s", (unsigned)word,
                (unsigned)value, why);
}

/* Whether device dev has vendor ID vendor, as the probe 0x8000E7D0 compares it. */
static int has_vendor(uint32_t dev, uint32_t vendor)
{
    if (dev == VOODOO_PCI_DEV)
        return (cfg_read(dev, 0) & 0xFFFFu) == vendor;
    if (dev >= PCI_SCAN_FIRST && dev <= PCI_SCAN_LAST && vendor == VENDOR_3DFX)
        return 0;
    ld_stop("vendor 0x%X on PCI device %u: only the probes of devices 6 to 9 for vendor "
            "0x121A were measured", (unsigned)vendor, (unsigned)dev);
}

/* 0x0F0 pci_cfg_read(reg, dev, u32 *out): section 8, returns 1. GT64010 INTR_CAUSE bit 18,
 * which the loader clears after the read when set, is never set here (no PCI error is
 * modelled). */
static void s_pci_cfg_read(cpu_t *c)
{
    ld_put32(c->r[6], cfg_read(c->r[5] & 0x1Fu, cfg_word(c->r[4])));
    c->r[2] = 1;
}

/* 0x0F8 pci_cfg_write(reg, dev, const u32 *value): section 8, returns 1 */
static void s_pci_cfg_write(cpu_t *c)
{
    if (c->r[6] & 3u)
        ld_stop("the value pointer 0x%08X is not word aligned", (unsigned)c->r[6]);
    cfg_write(c->r[5] & 0x1Fu, cfg_word(c->r[4]), mem_r32(ld.ram, c, c->r[6]));
    c->r[2] = 1;
}

/* 0x108 pci_probe(vendor, devid, -, dev): section 8, 1 if device dev has the vendor ID */
static void s_pci_probe(cpu_t *c)
{
    c->r[2] = (uint32_t)has_vendor(c->r[7] & 0x1Fu, c->r[4]);
}

/* 0x110 pci_map(vendor, devid, -, int *dev, index): section 1; the index is the fifth
 * argument, at sp + 16. Devices 6 to 9 are scanned for the index-th with the vendor; found:
 * BAR0 = 0x08000000, *dev = the device, 0xA8000000 returned (the loader also reads
 * configuration register 1 and discards it, which changes nothing here); not found: *dev =
 * 10, 0 returned. */
static void s_pci_map(cpu_t *c)
{
    uint32_t vendor = c->r[4], dev_out = c->r[7];
    uint32_t index = mem_r32(ld.ram, c, c->r[29] + 16), seen = 0;
    for (uint32_t dev = PCI_SCAN_FIRST; dev <= PCI_SCAN_LAST; dev++) {
        if (!has_vendor(dev, vendor) || seen++ != index)
            continue;
        voodoo_cfg_map_bar0(VOODOO_BAR0);
        ld_put32(dev_out, dev);
        c->r[2] = VOODOO_KSEG1;
        return;
    }
    ld_put32(dev_out, PCI_NOT_FOUND);
    c->r[2] = 0;
}

/* --- the slot table ----------------------------------------------------------------------- */

static const struct {
    uint32_t slot;
    const char *name;
    void (*fn)(cpu_t *c);
} SLOTS[] = {
    {0x018, "close", ld_slot_close},
    {0x028, "creat", ld_slot_creat},
    {0x038, "findfirst", ld_slot_findfirst},
    {0x088, "open", ld_slot_open},
    {0x090, "read", ld_slot_read},
    {0x0C0, "write", ld_slot_write},
    {0x0C8, "ioctl", ld_slot_ioctl},
    {0x0D8, "_exit", s_exit},
    {0x0E0, "isatty", ld_slot_isatty},
    {0x0E8, "dcache_flush", s_dcache_flush},
    {0x0F0, "pci_cfg_read", s_pci_cfg_read},
    {0x0F8, "pci_cfg_write", s_pci_cfg_write},
    {0x108, "pci_probe", s_pci_probe},
    {0x110, "pci_map", s_pci_map},
    {0x118, "disk_select_partition", ld_slot_disk_select},
    {0x128, "disk_partition_table", ld_slot_disk_table},
    {0x158, "disk_read_sectors", ld_slot_disk_read},
    {0x190, "unlink", ld_slot_unlink},
    {0x198, "exec", s_exec},
    {0x1A8, "exec_args", s_exec_args},
    {0x1B8, "rename", ld_slot_rename},
    {0x1C0, "lseek", ld_slot_lseek},
    {0x1D8, "disk_set_callback", ld_slot_disk_callback},
    {0x1E0, "gt_set_handler", s_gt_set_handler},
    {0x1E8, "fpe_set_handler", s_fpe_set_handler},
    {0x1F0, "set_break7_hook", s_break7_hook},
    {0x1F8, "set_break6_hook", s_break6_hook},
    {0x200, "irq_set_handler", s_irq_set_handler},
    {0x220, "install_disk_info", ld_slot_disk_info},
};

static int slot_index(uint32_t slot)
{
    for (int i = 0; i < (int)(sizeof SLOTS / sizeof SLOTS[0]); i++)
        if (SLOTS[i].slot == slot)
            return i;
    return -1;
}

static const char *slot_name(uint32_t slot)
{
    int i = slot_index(slot);
    return i < 0 ? NULL : SLOTS[i].name;
}

/* rt_loader (recomp.h). The call context is saved and restored around the slot, so a slot
 * that re-enters game code (0x158, through the disk callbacks) keeps its own. */
void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot)
{
    struct ld_call outer = ld;
    int i;
    ld = (struct ld_call){ram, c, slot};
    if (call_hook)
        call_hook(c, slot, 0);
    i = slot_index(slot);
    if (i < 0)
        ld_stop("not a loader slot (the stub block's 29 are boot-handoff.md's)");
    SLOTS[i].fn(c);
    ld = outer;
    if (call_hook)
        call_hook(c, slot, 1);
}
