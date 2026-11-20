/* loader_driver.c: runs the loader stand-ins (runtime/loader/loader.h) from commands on
 * standard input, for tests/test_loader.py, with the runtime core (clock, interrupts,
 * exceptions), the interpreter and the disk library (runtime/disk/disk.h).
 *
 * One command per line. Numbers are hexadecimal, except times in nanoseconds (decimal):
 *   reset                machine_reset, loader_reset; RAM and cpu_t zeroed, FCSR 0
 *   img RAW OVERLAY      img_open (OVERLAY - for none); "img 0", or "img -1 MESSAGE"
 *   mem ADDR WORD        a RAM word
 *   str ADDR TEXT        TEXT and a NUL into RAM at ADDR (byte by byte); TEXT "-" is empty
 *   fill ADDR N BYTE     N RAM bytes from ADDR set to BYTE
 *   r N V                general register N
 *   fcsr V               FCSR as ctc1 writes it
 *   call SLOT A0 A1 A2 A3   a0 to a3 set, rt_loader(SLOT); then "ret V0 V1"
 *   dump ADDR N          "dump W0 .. W(N-1)", N RAM words from ADDR
 *   peek ADDR N          "peek HEX", N RAM bytes from ADDR as hexadecimal
 *   sha ADDR N           "sha HEX", the SHA-256 of N RAM bytes from ADDR
 *   cmosload PATH        cmos_load
 *   cmossave             cmos_save
 *   cmosbyte OFF         "cmosbyte V"
 *   inputs P0 P1 P2 P3   ioasic_set_inputs
 *   hook                 loader_set_console_hook of a hook printing "hook FD HEX"
 *   checks K             K check points that do not reach 0 (slice -= K), then "now NOW"
 *   advance NS           ticks as if each slice ran out until vclock_now reaches NS, then
 *                        "now NOW"
 *   run PC               interp_run from PC, then "ran"
 *   desc FD              "desc FLAGS" of descriptor FD (ld_desc)
 *   state                "m STATUS CAUSE_IP INTCTL NOW FCSR VBLANK_PERIOD"
 *   handlers             "handlers LINE0 .. LINE7 GT4 FPE0 .. FPE5 BREAK7 BREAK6 GTMASK"
 *   atstop               rt_at_stop of a function that prints "at_stop" on stderr
 * The bus stops (rt_stop) naming the address: a guest pointer outside RAM ends the run there.
 *
 * recomp_lookup knows two functions: PROBE (0x80300000), which prints "probe A0 A1 A2 SP
 * STATUS NOW" and returns, and the stub thunk of slot 0x0C8 (ioctl, at 0x800C4154), whose
 * body is rt_loader(ram, c, 0x0C8) as in the generated code.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "disk.h"
#include "loader.h"
#include "machine.h"

#define PROBE 0x80300000u
#define STUB_BLOCK 0x800C408Cu

static uint8_t *ram;
static cpu_t cpu;

static _Noreturn void die(const char *what, const char *detail)
{
    fflush(stdout);
    fprintf(stderr, "loader_driver: %s%s%s\n", what, detail ? ": " : "", detail ? detail : "");
    exit(1);
}

/* --- what the runtime needs that it does not provide ------------------------------------- */

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    (void)c;
    rt_stop(RT_STOP_STATUS, "bus read of %d bytes at 0x%08" PRIX32, size, addr);
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    (void)c;
    rt_stop(RT_STOP_STATUS, "bus write of %d bytes at 0x%08" PRIX32 " (0x%" PRIX32 ")", size,
            addr, value);
}

void rt_host_switch(uint8_t *r, cpu_t *c, int kind)
{
    (void)r, (void)c;
    fprintf(stderr, "loader_driver: rt_host_switch %d\n", kind);
    exit(1);
}

int rt_switch_kind(uint32_t pc)
{
    (void)pc;
    return -1;
}

static void probe(uint8_t *r, cpu_t *c)
{
    (void)r;
    printf("probe %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %08" PRIX32
           " %" PRIu64 "\n", c->r[4], c->r[5], c->r[6], c->r[29], m_status(), vclock_now(c));
}

static void stub_ioctl(uint8_t *r, cpu_t *c)
{
    rt_loader(r, c, 0x0C8);
}

recomp_fn recomp_lookup(uint32_t addr)
{
    if (addr == PROBE)
        return probe;
    if (addr == STUB_BLOCK + 0x0C8)
        return stub_ioctl;
    return NULL;
}

/* --- hooks -------------------------------------------------------------------------------- */

static void console_hook(int fd, const uint8_t *bytes, uint32_t n)
{
    printf("hook %d ", fd);
    for (uint32_t i = 0; i < n; i++)
        printf("%02X", bytes[i]);
    putchar('\n');
}

static void at_stop(void)
{
    fputs("at_stop\n", stderr);
}

/* --- input and output -------------------------------------------------------------------- */

static uint32_t hex(void)
{
    uint32_t v;
    if (scanf("%" SCNx32, &v) != 1)
        die("bad input", "expected a hexadecimal number");
    return v;
}

static uint64_t dec(void)
{
    uint64_t v;
    if (scanf("%" SCNu64, &v) != 1)
        die("bad input", "expected a decimal number");
    return v;
}

static void word(char *buf, size_t n)
{
    char fmt[16];
    snprintf(fmt, sizeof fmt, "%%%zus", n - 1);
    if (scanf(fmt, buf) != 1)
        die("bad input", "expected a word");
}

static uint8_t *ram_at(uint32_t addr, uint32_t n)
{
    if (!mem_is_ram(addr, n ? n : 1))
        die("not a RAM range", NULL);
    return ram + (addr & 0x1FFFFFFFu);
}

static void reset(void)
{
    machine_reset();
    loader_reset();
    memset(ram, 0, RAM_SIZE);
    memset(&cpu, 0, sizeof cpu);
    rt_set_fcsr(&cpu, 0);
}

static void advance(uint64_t until)
{
    while (vclock_now(&cpu) < until) {
        cpu.slice = 0; /* every check point of the slice ran */
        rt_tick(ram, &cpu);
    }
    printf("now %" PRIu64 "\n", vclock_now(&cpu));
}

int main(void)
{
    static char key[16], text[1024];
    ram = malloc(RAM_SIZE);
    if (!ram)
        die("out of memory", NULL);
    reset();
    while (scanf("%15s", key) == 1) {
        if (strcmp(key, "reset") == 0) {
            reset();
        } else if (strcmp(key, "img") == 0) {
            static char raw[1024], ovl[1024], err[256];
            word(raw, sizeof raw);
            word(ovl, sizeof ovl);
            if (img_open(raw, strcmp(ovl, "-") ? ovl : NULL, err, sizeof err) == 0)
                puts("img 0");
            else
                printf("img -1 %s\n", err);
        } else if (strcmp(key, "mem") == 0) {
            uint32_t addr = hex(), w = hex();
            memcpy(ram_at(addr, 4), &w, 4);
        } else if (strcmp(key, "str") == 0) {
            uint32_t addr = hex();
            size_t n;
            word(text, sizeof text);
            if (strcmp(text, "-") == 0)
                text[0] = 0;
            n = strlen(text) + 1;
            memcpy(ram_at(addr, (uint32_t)n), text, n);
        } else if (strcmp(key, "fill") == 0) {
            uint32_t addr = hex(), n = hex(), b = hex();
            memset(ram_at(addr, n), (int)b, n);
        } else if (strcmp(key, "r") == 0) {
            uint32_t n = hex();
            if (n > 31)
                die("bad input", "register number above 31");
            cpu.r[n] = hex();
        } else if (strcmp(key, "fcsr") == 0) {
            rt_set_fcsr(&cpu, hex());
        } else if (strcmp(key, "call") == 0) {
            uint32_t slot = hex();
            for (int i = 4; i < 8; i++)
                cpu.r[i] = hex();
            rt_loader(ram, &cpu, slot);
            printf("ret %08" PRIX32 " %08" PRIX32 "\n", cpu.r[2], cpu.r[3]);
        } else if (strcmp(key, "dump") == 0) {
            uint32_t addr = hex(), n = hex();
            fputs("dump", stdout);
            for (uint32_t i = 0; i < n; i++) {
                uint32_t w;
                memcpy(&w, ram_at(addr + 4 * i, 4), 4);
                printf(" %08" PRIX32, w);
            }
            putchar('\n');
        } else if (strcmp(key, "peek") == 0) {
            uint32_t addr = hex(), n = hex();
            const uint8_t *p = ram_at(addr, n);
            fputs("peek ", stdout);
            for (uint32_t i = 0; i < n; i++)
                printf("%02X", p[i]);
            putchar('\n');
        } else if (strcmp(key, "sha") == 0) {
            uint32_t addr = hex(), n = hex();
            uint8_t out[32];
            sha256(ram_at(addr, n), n, out);
            fputs("sha ", stdout);
            for (int i = 0; i < 32; i++)
                printf("%02x", out[i]);
            putchar('\n');
        } else if (strcmp(key, "cmosload") == 0) {
            word(text, sizeof text);
            cmos_load(text);
        } else if (strcmp(key, "cmossave") == 0) {
            cmos_save();
        } else if (strcmp(key, "cmosbyte") == 0) {
            printf("cmosbyte %02X\n", cmos_byte(hex()));
        } else if (strcmp(key, "inputs") == 0) {
            uint16_t ports[4];
            for (int i = 0; i < 4; i++)
                ports[i] = (uint16_t)hex();
            ioasic_set_inputs(ports);
        } else if (strcmp(key, "hook") == 0) {
            loader_set_console_hook(console_hook);
        } else if (strcmp(key, "checks") == 0) {
            cpu.slice -= (int32_t)hex();
            printf("now %" PRIu64 "\n", vclock_now(&cpu));
        } else if (strcmp(key, "advance") == 0) {
            advance(dec());
        } else if (strcmp(key, "run") == 0) {
            interp_run(ram, &cpu, hex());
            puts("ran");
        } else if (strcmp(key, "desc") == 0) {
            struct ldesc *d = ld_desc(hex());
            if (!d)
                die("desc: no such descriptor", NULL);
            printf("desc %02" PRIX32 "\n", d->flags);
        } else if (strcmp(key, "state") == 0) {
            printf("m %08" PRIX32 " %08" PRIX32 " %08" PRIX32 " %" PRIu64 " %08" PRIX32
                   " %" PRIu64 "\n", m_status(), machine.cause_ip, machine.intctl,
                   vclock_now(&cpu), rt_get_fcsr(&cpu), machine.vblank_period);
        } else if (strcmp(key, "handlers") == 0) {
            fputs("handlers", stdout);
            for (int i = 0; i < 8; i++)
                printf(" %08" PRIX32, machine.line_fn[i]);
            printf(" %08" PRIX32, machine.gt_fn[4]);
            for (int i = 0; i < 6; i++)
                printf(" %08" PRIX32, machine.fpe_fn[i]);
            printf(" %08" PRIX32 " %08" PRIX32 " %08" PRIX32 "\n", machine.break7_hook,
                   machine.break6_hook, machine.gt_mask);
        } else if (strcmp(key, "atstop") == 0) {
            rt_at_stop(at_stop);
        } else {
            die("bad input: unknown command", key);
        }
        fflush(stdout);
    }
    if (!feof(stdin))
        die("bad input", "unreadable");
    free(ram);
    return 0;
}
