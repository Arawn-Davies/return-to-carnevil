/* smoke.c: run the recompiled game from GAME.EXE's entry to its first loader call.
 *
 * Usage: smoke RAM_IN RAM_OUT [NAME=VALUE ...]. RAM_IN is the 8 MB of RAM at GAME.EXE's entry
 * (MAME's dump, mame/dumps/ram-at-game-entry.bin). Each NAME=VALUE (hexadecimal with 0x, or
 * decimal) sets a general register by its ABI name (sp, gp, ra, a0, ...), "hi", "lo" or FCSR
 * ("fcsr", as ctc1 writes it); every other register is 0 and no interrupt is ever delivered
 * (rt_tick only reloads slice). The program calls fn_800C4000 (it must be recompiled) and, at
 * the first rt_loader call, prints
 * "slot S" and the state (common.h, print_state) to stdout, writes the 8 MB of RAM to RAM_OUT
 * and exits 0. If the entry function returns without a loader call it exits 1.
 *
 * Anything else the run does before that is printed to stderr, one line each, prefixed
 * "smoke: ": every bus access ("bus_read ADDR size N", "bus_write ADDR size N value V"; after
 * MAX_BUS_LINES of them only their number, at the end), every rt_break, rt_host_switch and
 * rt_fp_trap (each returns), and every address the interpreter is entered at.
 */
#include "common.h"

#define ENTRY 0x800C4000u
#define MAX_BUS_LINES 64

static const char *ram_out;
static unsigned long bus_accesses;

static void bus_line(const char *what, uint32_t addr, int size, uint32_t value)
{
    if (++bus_accesses > MAX_BUS_LINES)
        return;
    fprintf(stderr, "smoke: %s 0x%08" PRIX32 " size %d", what, addr, size);
    if (what[4] == 'w')
        fprintf(stderr, " value 0x%08" PRIX32, value);
    fprintf(stderr, "\n");
}

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    (void)c;
    bus_line("bus_read", addr, size, 0);
    return 0;
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    (void)c;
    bus_line("bus_write", addr, size, value);
}

static void bus_summary(void)
{
    if (bus_accesses > MAX_BUS_LINES)
        fprintf(stderr, "smoke: %lu bus accesses in all\n", bus_accesses);
}

void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot)
{
    bus_summary();
    printf("slot %03" PRIX32 "\n", slot);
    print_state(stdout, c);
    fflush(stdout);
    exit(ram_save("smoke", ram_out, ram) == 0 ? 0 : 1);
}

void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code)
{
    (void)ram;
    (void)c;
    fprintf(stderr, "smoke: rt_break pc 0x%08" PRIX32 " code 0x%05" PRIX32 "\n", pc, code);
}

void rt_tick(uint8_t *ram, cpu_t *c)
{
    (void)ram;
    c->slice = STUB_SLICE;
}

void rt_host_switch(uint8_t *ram, cpu_t *c, int kind)
{
    (void)ram;
    fprintf(stderr, "smoke: rt_host_switch kind %d sp 0x%08" PRIX32 "\n", kind, c->r[29]);
}

void rt_fp_trap(cpu_t *c, uint32_t cause_bit)
{
    (void)c;
    fprintf(stderr, "smoke: rt_fp_trap cause 0x%05" PRIX32 "\n", cause_bit);
}

/* Returning lets the interpreter stop with its own message naming the address. */
void rt_bad_target(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    (void)ram;
    (void)c;
    (void)addr;
}

static void log_unrecompiled(uint32_t addr)
{
    fprintf(stderr, "smoke: interp_run entered at 0x%08" PRIX32 "\n", addr);
}

int main(int argc, char **argv)
{
    cpu_t c;
    uint8_t *ram;
    recomp_fn entry;
    uint32_t fcsr = 0;
    if (argc < 3) {
        fprintf(stderr, "usage: %s RAM_IN RAM_OUT [NAME=VALUE ...]\n", argv[0]);
        return 2;
    }
    memset(&c, 0, sizeof c);
    c.slice = STUB_SLICE;
    for (int k = 3; k < argc; k++) {
        char name[8], *end;
        const char *eq = strchr(argv[k], '=');
        unsigned long long v;
        int i = -1;
        if (eq && (size_t)(eq - argv[k]) < sizeof name) {
            memcpy(name, argv[k], (size_t)(eq - argv[k]));
            name[eq - argv[k]] = '\0';
            i = strcmp(name, "fcsr") == 0 ? 32 : strcmp(name, "hi") == 0 ? 33
                : strcmp(name, "lo") == 0 ? 34 : reg_index(name);
        }
        v = eq ? strtoull(eq + 1, &end, 0) : 0;
        if (i < 0 || i == 0 || eq[1] == '\0' || *end != '\0' || v > 0xFFFFFFFFull) {
            fprintf(stderr, "smoke: %s is not REGISTER=VALUE\n", argv[k]);
            return 2;
        }
        if (i == 32)
            fcsr = (uint32_t)v;
        else if (i == 33)
            c.hi = (uint32_t)v;
        else if (i == 34)
            c.lo = (uint32_t)v;
        else
            c.r[i] = (uint32_t)v;
    }
    ram = ram_load("smoke", argv[1]);
    if (!ram)
        return 2;
    ram_out = argv[2];
    entry = recomp_lookup(ENTRY);
    if (!entry) {
        fprintf(stderr, "smoke: no recompiled function at 0x%08X\n", ENTRY);
        return 2;
    }
    rt_set_fcsr(&c, fcsr);
    rt_log_unrecompiled = log_unrecompiled;
    entry(ram, &c);
    bus_summary();
    fprintf(stderr, "smoke: fn_%08X returned without a loader call\n", ENTRY);
    free(ram);
    return 1;
}
