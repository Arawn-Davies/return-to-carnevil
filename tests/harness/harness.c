/* harness.c: runs instruction test cases through the runtime, for tests/charness.py.
 *
 * Usage: harness MODE < cases > results. MODE "interp" runs each case with interp_run; MODE
 * "recomp" runs it by calling the linked function its "fn" line names. Unknown modes are an
 * error.
 *
 * Wire format: text, one record per line, a keyword then fields; numbers are hexadecimal.
 * Input, per case, in this order ("fn", "slice" and "mem" are optional; "mem" may repeat):
 *   case NAME              starts a case (NAME has no spaces)
 *   fn NAME                the function "recomp" mode calls, as registered with
 *                          harness_register_named (required in that mode, ignored in
 *                          "interp" mode, so one input runs in both)
 *   r R0 .. R31            general registers
 *   hilo HI LO
 *   fpr F0 .. F31          FP register halves
 *   fcsr X                 FCSR with its condition bits in place (cc0 bit 23, cc1-7 25-31)
 *   slice N RELOAD         cpu_t.slice at the start, and the value rt_tick reloads it with
 *                          (RELOAD at least 1; default both HARNESS_SLICE, more check points
 *                          than any case program makes)
 *   mem ADDR WORD          a word of RAM (code or data) at a RAM address
 *   run ENTRY              runs the case from ENTRY
 * Output, per case:
 *   case NAME
 *   then, in the order they happen:
 *   call bus_read ADDR SIZE
 *   call bus_write ADDR SIZE VALUE
 *   call loader SLOT
 *   call break PC CODE
 *   call tick R0 .. R31    rt_tick, with the general registers at the check point
 *   call host_switch KIND SP
 *   call fp_trap CAUSE
 *   unrecompiled ADDR      rt_log_unrecompiled
 *   then the state after the case:
 *   r R0 .. R31
 *   hilo HI LO
 *   fpr F0 .. F31
 *   fcsr X                 as rt_get_fcsr returns it
 *   mem ADDR WORD          each non-zero word of the scratch page 0x80200000-0x80200FFF
 *   end
 * A fatal error (an unsupported instruction, a sanitizer report) exits non-zero with its
 * message on stderr.
 *
 * Each case starts from zeroed RAM, a fresh cpu_t, the host FP environment the process
 * started with (restored after every case), and an empty entry log in the interpreter.
 * The bus records each access; a read returns 0xDEADBEEF masked to its size. rt_loader,
 * rt_break, rt_host_switch and rt_fp_trap record their call and return; rt_tick records its
 * call, reloads slice with the case's RELOAD and returns.
 *
 * Recompiled functions: a C file linked into the harness includes harness.h and registers
 * its functions with harness_register(addr, fn), typically from a constructor;
 * recomp_lookup finds them. No function is registered unless a linked file registers it.
 * The functions of "recomp" mode are registered separately by name, with
 * harness_register_named(name, fn): case programs all start at the same address, so the
 * address registry cannot tell them apart. "recomp" mode calls the function directly, on a
 * state set up as for "interp" mode; it does not go through recomp_lookup. Host-switch
 * points are registered with harness_register_switch(pc, kind) for rt_switch_kind; none is
 * unless a linked file registers it.
 */
#include <fenv.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "harness.h"

#define SCRATCH 0x80200000u
#define SCRATCH_SIZE 0x1000u
#define BUS_READ_VALUE 0xDEADBEEFu
#define HARNESS_SLICE 0x40000000

static _Noreturn void die(const char *msg, const char *detail)
{
    fflush(stdout);
    fprintf(stderr, "harness: %s%s%s\n", msg, detail ? ": " : "", detail ? detail : "");
    exit(EXIT_FAILURE);
}

/* --- the registry of recompiled functions ---------------------------------------------- */

#define REGISTRY_SIZE (1u << 17) /* open addressing, power of two */

static uint32_t reg_addr[REGISTRY_SIZE];
static recomp_fn reg_fn[REGISTRY_SIZE];
static uint32_t reg_count;

void harness_register(uint32_t addr, recomp_fn fn)
{
    uint32_t i = (addr >> 2) * 2654435761u & (REGISTRY_SIZE - 1);
    while (reg_fn[i] && reg_addr[i] != addr)
        i = (i + 1) & (REGISTRY_SIZE - 1);
    if (!reg_fn[i] && ++reg_count > REGISTRY_SIZE / 2)
        die("too many registered functions", NULL);
    reg_addr[i] = addr;
    reg_fn[i] = fn;
}

recomp_fn recomp_lookup(uint32_t addr)
{
    uint32_t i = (addr >> 2) * 2654435761u & (REGISTRY_SIZE - 1);
    while (reg_fn[i]) {
        if (reg_addr[i] == addr)
            return reg_fn[i];
        i = (i + 1) & (REGISTRY_SIZE - 1);
    }
    return NULL;
}

/* --- the registry of host-switch points ------------------------------------------------ */

#define SWITCHES_MAX 64

static uint32_t switch_pc[SWITCHES_MAX];
static int switch_kind[SWITCHES_MAX];
static unsigned switch_count;

void harness_register_switch(uint32_t pc, int kind)
{
    for (unsigned i = 0; i < switch_count; i++) {
        if (switch_pc[i] == pc) {
            switch_kind[i] = kind;
            return;
        }
    }
    if (switch_count == SWITCHES_MAX)
        die("too many host-switch points", NULL);
    switch_pc[switch_count] = pc;
    switch_kind[switch_count++] = kind;
}

int rt_switch_kind(uint32_t pc)
{
    for (unsigned i = 0; i < switch_count; i++)
        if (switch_pc[i] == pc)
            return switch_kind[i];
    return -1;
}

/* --- the registry of functions by name, for "recomp" mode ------------------------------ */

struct named {
    const char *name;
    recomp_fn fn;
};

static struct named *named_fns;
static size_t named_count, named_cap;

void harness_register_named(const char *name, recomp_fn fn)
{
    for (size_t i = 0; i < named_count; i++) {
        if (strcmp(named_fns[i].name, name) == 0) {
            named_fns[i].fn = fn;
            return;
        }
    }
    if (named_count == named_cap) {
        named_cap = named_cap ? 2 * named_cap : 256;
        named_fns = realloc(named_fns, named_cap * sizeof *named_fns);
        if (!named_fns)
            die("out of memory", NULL);
    }
    named_fns[named_count].name = name;
    named_fns[named_count].fn = fn;
    named_count++;
}

static recomp_fn find_named(const char *name)
{
    for (size_t i = 0; i < named_count; i++)
        if (strcmp(named_fns[i].name, name) == 0)
            return named_fns[i].fn;
    return NULL;
}

/* --- the recording runtime -------------------------------------------------------------- */

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    (void)c;
    printf("call bus_read %08" PRIX32 " %X\n", addr, (unsigned)size);
    return size == 4 ? BUS_READ_VALUE : BUS_READ_VALUE & ((1u << 8 * size) - 1);
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    (void)c;
    printf("call bus_write %08" PRIX32 " %X %08" PRIX32 "\n", addr, (unsigned)size, value);
}

void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot)
{
    (void)ram, (void)c;
    printf("call loader %08" PRIX32 "\n", slot);
}

void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code)
{
    (void)ram, (void)c;
    printf("call break %08" PRIX32 " %08" PRIX32 "\n", pc, code);
}

static int32_t slice_reload = HARNESS_SLICE;

void rt_tick(uint8_t *ram, cpu_t *c)
{
    (void)ram;
    fputs("call tick", stdout);
    for (int i = 0; i < 32; i++)
        printf(" %08" PRIX32, c->r[i]);
    putchar('\n');
    c->slice = slice_reload;
}

void rt_host_switch(uint8_t *ram, cpu_t *c, int kind)
{
    (void)ram;
    printf("call host_switch %X %08" PRIX32 "\n", (unsigned)kind, c->r[29]);
}

void rt_fp_trap(cpu_t *c, uint32_t cause_bit)
{
    (void)c;
    printf("call fp_trap %08" PRIX32 "\n", cause_bit);
}

/* Returning lets the interpreter stop with its own message naming the address. */
void rt_bad_target(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    (void)ram, (void)c, (void)addr;
}

static void log_unrecompiled(uint32_t addr)
{
    printf("unrecompiled %08" PRIX32 "\n", addr);
}

/* --- input ------------------------------------------------------------------------------- */

static uint32_t hex(void)
{
    uint32_t v;
    if (scanf("%" SCNx32, &v) != 1)
        die("bad input", "expected a hexadecimal number");
    return v;
}

static void put_words(const char *key, const uint32_t *v, int n)
{
    fputs(key, stdout);
    for (int i = 0; i < n; i++)
        printf(" %08" PRIX32, v[i]);
    putchar('\n');
}

int main(int argc, char **argv)
{
    static char name[256], fn_name[256], key[16];
    int recomp_mode;
    fenv_t host_env;
    uint8_t *ram = malloc(RAM_SIZE);
    cpu_t c;
    uint32_t fcsr = 0;

    if (argc != 2 || (strcmp(argv[1], "interp") != 0 && strcmp(argv[1], "recomp") != 0))
        die("usage: harness MODE (MODE: interp, recomp)", argc == 2 ? argv[1] : NULL);
    recomp_mode = strcmp(argv[1], "recomp") == 0;
    if (!ram)
        die("out of memory", NULL);
    if (fegetenv(&host_env) != 0)
        die("fegetenv failed", NULL);
    rt_log_unrecompiled = log_unrecompiled;

    memset(&c, 0, sizeof c);
    memset(ram, 0, RAM_SIZE);
    while (scanf("%15s", key) == 1) {
        if (strcmp(key, "case") == 0) {
            if (scanf("%255s", name) != 1)
                die("bad input", "case without a name");
            memset(&c, 0, sizeof c);
            memset(ram, 0, RAM_SIZE);
            c.slice = slice_reload = HARNESS_SLICE;
            fcsr = 0;
            fn_name[0] = '\0';
        } else if (strcmp(key, "fn") == 0) {
            if (scanf("%255s", fn_name) != 1)
                die("bad input", "fn without a name");
        } else if (strcmp(key, "r") == 0) {
            for (int i = 0; i < 32; i++)
                c.r[i] = hex();
        } else if (strcmp(key, "hilo") == 0) {
            c.hi = hex();
            c.lo = hex();
        } else if (strcmp(key, "fpr") == 0) {
            for (int i = 0; i < 32; i++)
                c.fpr[i] = hex();
        } else if (strcmp(key, "fcsr") == 0) {
            fcsr = hex();
        } else if (strcmp(key, "slice") == 0) {
            c.slice = (int32_t)hex();
            slice_reload = (int32_t)hex();
            if (slice_reload < 1)
                die("bad input", "slice: RELOAD must be at least 1");
        } else if (strcmp(key, "mem") == 0) {
            uint32_t addr = hex(), word = hex();
            if ((addr & 3u) || !mem_is_ram(addr, 4))
                die("mem: not an aligned RAM address", name);
            memcpy(ram + (addr & 0x1FFFFFFFu), &word, 4);
        } else if (strcmp(key, "run") == 0) {
            uint32_t entry = hex();
            printf("case %s\n", name);
            c.r[0] = 0;
            interp_forget_entries();
            rt_set_fcsr(&c, fcsr);
            if (recomp_mode) {
                recomp_fn fn = fn_name[0] ? find_named(fn_name) : NULL;
                if (!fn)
                    die(fn_name[0] ? "no registered function" : "case has no fn line",
                        fn_name[0] ? fn_name : name);
                fn(ram, &c);
            } else {
                interp_run(ram, &c, entry);
            }
            uint32_t after_fcsr = rt_get_fcsr(&c);
            if (fesetenv(&host_env) != 0)
                die("fesetenv failed", name);
            put_words("r", c.r, 32);
            printf("hilo %08" PRIX32 " %08" PRIX32 "\n", c.hi, c.lo);
            put_words("fpr", c.fpr, 32);
            printf("fcsr %08" PRIX32 "\n", after_fcsr);
            for (uint32_t a = SCRATCH; a < SCRATCH + SCRATCH_SIZE; a += 4) {
                uint32_t word;
                memcpy(&word, ram + (a & 0x1FFFFFFFu), 4);
                if (word)
                    printf("mem %08" PRIX32 " %08" PRIX32 "\n", a, word);
            }
            puts("end");
        } else {
            die("bad input: unknown keyword", key);
        }
    }
    if (!feof(stdin))
        die("bad input", "unreadable");
    free(ram);
    return 0;
}
