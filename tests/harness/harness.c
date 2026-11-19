/* harness.c: runs instruction test cases through the runtime, for tests/charness.py.
 *
 * Usage: harness MODE < cases > results. MODE "interp" runs each case with interp_run.
 * Unknown modes are an error.
 *
 * Wire format: text, one record per line, a keyword then fields; numbers are hexadecimal.
 * Input, per case, in this order ("irq" and "mem" are optional; "mem" may repeat):
 *   case NAME              starts a case (NAME has no spaces)
 *   r R0 .. R31            general registers
 *   hilo HI LO
 *   fpr F0 .. F31          FP register halves
 *   fcsr X                 FCSR with its condition bits in place (cc0 bit 23, cc1-7 25-31)
 *   irq X                  cpu_t.irq_pending (default 0)
 *   mem ADDR WORD          a word of RAM (code or data) at a RAM address
 *   run ENTRY              runs the case from ENTRY
 * Output, per case:
 *   case NAME
 *   then, in the order they happen:
 *   call bus_read ADDR SIZE
 *   call bus_write ADDR SIZE VALUE
 *   call loader SLOT
 *   call break PC CODE
 *   call interrupt
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
 * rt_break and rt_interrupt record their call and return (irq_pending is left as it is).
 *
 * Recompiled functions: a C file linked into the harness registers its functions with
 * harness_register(addr, fn), typically from a constructor; recomp_lookup finds them. No
 * function is registered unless a linked file registers it.
 */
#include <fenv.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "recomp.h"

#define SCRATCH 0x80200000u
#define SCRATCH_SIZE 0x1000u
#define BUS_READ_VALUE 0xDEADBEEFu

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

void harness_register(uint32_t addr, recomp_fn fn);

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

void rt_interrupt(uint8_t *ram, cpu_t *c)
{
    (void)ram, (void)c;
    printf("call interrupt\n");
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
    static char name[256], key[16];
    fenv_t host_env;
    uint8_t *ram = malloc(RAM_SIZE);
    cpu_t c;
    uint32_t fcsr = 0;

    if (argc != 2 || strcmp(argv[1], "interp") != 0)
        die("usage: harness MODE (MODE: interp)", argc == 2 ? argv[1] : NULL);
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
            fcsr = 0;
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
        } else if (strcmp(key, "irq") == 0) {
            c.irq_pending = hex();
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
            interp_run(ram, &c, entry);
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
