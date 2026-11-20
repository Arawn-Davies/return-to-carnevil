/* slots.c: rt_loader and the slot table, the helpers every stand-in uses (the call being
 * served, stops, guest strings and stores), loader_reset, and the slots that are not about
 * descriptors: exit, the data-cache flush, load-and-run, the argument block and the handler
 * registrations.
 *
 * docs/analysis/loader-services.md section 1 gives each slot; section 8 says what each
 * stand-in must do. The 9 slots of the next task (PCI configuration, partitions, raw sectors,
 * the disk hooks) have placeholders that stop.
 */
#include <stdarg.h>
#include <stdio.h>

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

void loader_reset(void)
{
    ld_fd_reset();
    ld_con_reset();
    ld_cmos_reset();
    ld_timer_reset();
    ld_ioasic_reset();
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

/* 0x200 irq_set_handler: section 1; line 0 also starts the VBLANK source the first time;
 * v0 = 8 on the normal path */
static void s_irq_set_handler(cpu_t *c)
{
    uint32_t line = c->r[4], fn = c->r[5];
    if (line > 4)
        ld_stop("line %u: no IP maps to it; the loader does nothing and its v0 is not "
                "documented", (unsigned)line);
    vclock_account(c); /* so the first VBLANK is one period after this call */
    if (line == 0 && fn && machine.vblank_period == 0)
        vblank_start(VBLANK_PERIOD_NS);
    irq_set_line((int)line, fn);
    vclock_reload(c); /* the new event, and a line now unmasked */
    c->r[2] = 8;
}

/* The next task's slots. */
static void s_not_yet(cpu_t *c)
{
    (void)c;
    ld_stop("slot 0x%03X is not implemented yet", (unsigned)ld.slot);
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
    {0x0F0, "pci_cfg_read", s_not_yet},
    {0x0F8, "pci_cfg_write", s_not_yet},
    {0x108, "pci_probe", s_not_yet},
    {0x110, "pci_map", s_not_yet},
    {0x118, "disk_select_partition", s_not_yet},
    {0x128, "disk_partition_table", s_not_yet},
    {0x158, "disk_read_sectors", s_not_yet},
    {0x190, "unlink", ld_slot_unlink},
    {0x198, "exec", s_exec},
    {0x1A8, "exec_args", s_exec_args},
    {0x1B8, "rename", ld_slot_rename},
    {0x1C0, "lseek", ld_slot_lseek},
    {0x1D8, "disk_set_callback", s_not_yet},
    {0x1E0, "gt_set_handler", s_gt_set_handler},
    {0x1E8, "fpe_set_handler", s_fpe_set_handler},
    {0x1F0, "set_break7_hook", s_break7_hook},
    {0x1F8, "set_break6_hook", s_break6_hook},
    {0x200, "irq_set_handler", s_irq_set_handler},
    {0x220, "install_disk_info", s_not_yet},
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
 * that ever re-enters game code (none here does) keeps its own. */
void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot)
{
    struct ld_call outer = ld;
    int i;
    ld = (struct ld_call){ram, c, slot};
    i = slot_index(slot);
    if (i < 0)
        ld_stop("not a loader slot (the stub block's 29 are boot-handoff.md's)");
    SLOTS[i].fn(c);
    ld = outer;
}
