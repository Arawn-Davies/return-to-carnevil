/* stub.c: the smallest runtime, enough for the generated code to link and run (game_stub).
 *
 * Usage: game_stub [ENTRY [EXE]]. With no argument it only shows the program links (make
 * linkcheck). With ENTRY (hexadecimal), it calls the function there through rt_call (the
 * recompiled function recomp_lookup finds, otherwise the interpreter) on zeroed RAM, with
 * every register 0 but sp = STACK_TOP, then prints the general registers as one line,
 * "r R0 .. R31" in hexadecimal. EXE: a CarnEvil EXE (an 8-byte header: checksum, load
 * address; then the image) loaded into RAM first, so the interpreter has code to run.
 *
 * The runtime entry points log to stderr, one line each, prefixed "stub: ":
 * - bus_read returns 0; bus_read and bus_write log an address the first time each sees it
 *   ("bus_read ADDR size N", "bus_write ADDR size N value V");
 * - rt_loader, rt_break, rt_host_switch and rt_fp_trap log their arguments and return
 *   ("rt_loader slot S", "rt_break pc P code C", "rt_host_switch kind K sp SP",
 *   "rt_fp_trap cause C"; after rt_fp_trap the division gives its IEEE result);
 * - rt_tick reloads slice with STUB_SLICE and does nothing else: no clock, no interrupt;
 * - rt_bad_target returns, so the interpreter stops with its own message;
 * - every address the interpreter is entered at, once ("interp_run entered at ADDR").
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "recomp.h"

#define STACK_TOP 0x807FFFF0u
#define SEEN_SIZE 4096u /* addresses remembered per bus function; past that, all are logged */
#define STUB_SLICE INT32_MAX

struct seen {
    uint32_t addr[SEEN_SIZE];
    uint8_t used[SEEN_SIZE];
    uint32_t count;
};

static struct seen reads, writes;

/* 1 the first time addr is given (or when the set is full), 0 after that. */
static int first_time(struct seen *s, uint32_t addr)
{
    uint32_t i = (addr * 2654435761u) & (SEEN_SIZE - 1);
    if (s->count == SEEN_SIZE)
        return 1;
    while (s->used[i]) {
        if (s->addr[i] == addr)
            return 0;
        i = (i + 1) & (SEEN_SIZE - 1);
    }
    s->used[i] = 1;
    s->addr[i] = addr;
    s->count++;
    return 1;
}

uint32_t bus_read(cpu_t *c, uint32_t addr, int size)
{
    (void)c;
    if (first_time(&reads, addr))
        fprintf(stderr, "stub: bus_read 0x%08" PRIX32 " size %d\n", addr, size);
    return 0;
}

void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value)
{
    (void)c;
    if (first_time(&writes, addr))
        fprintf(stderr, "stub: bus_write 0x%08" PRIX32 " size %d value 0x%08" PRIX32 "\n", addr,
                size, value);
}

void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot)
{
    (void)ram;
    (void)c;
    fprintf(stderr, "stub: rt_loader slot 0x%03" PRIX32 "\n", slot);
}

void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code)
{
    (void)ram;
    (void)c;
    fprintf(stderr, "stub: rt_break pc 0x%08" PRIX32 " code 0x%05" PRIX32 "\n", pc, code);
}

void rt_tick(uint8_t *ram, cpu_t *c)
{
    (void)ram;
    c->slice = STUB_SLICE;
}

void rt_host_switch(uint8_t *ram, cpu_t *c, int kind)
{
    (void)ram;
    fprintf(stderr, "stub: rt_host_switch kind %d sp 0x%08" PRIX32 "\n", kind, c->r[29]);
}

void rt_fp_trap(cpu_t *c, uint32_t cause_bit)
{
    (void)c;
    fprintf(stderr, "stub: rt_fp_trap cause 0x%05" PRIX32 "\n", cause_bit);
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
    fprintf(stderr, "stub: interp_run entered at 0x%08" PRIX32 "\n", addr);
}

static int load_exe(uint8_t *ram, const char *path)
{
    FILE *f = fopen(path, "rb");
    uint8_t header[8];
    uint32_t load;
    size_t n;
    if (!f) {
        perror(path);
        return -1;
    }
    if (fread(header, 1, sizeof header, f) != sizeof header) {
        fprintf(stderr, "stub: %s: shorter than its header\n", path);
        fclose(f);
        return -1;
    }
    memcpy(&load, header + 4, 4);
    if (!mem_is_ram(load, 1)) {
        fprintf(stderr, "stub: %s: load address 0x%08" PRIX32 " is not RAM\n", path, load);
        fclose(f);
        return -1;
    }
    load &= 0x1FFFFFFFu;
    n = fread(ram + load, 1, RAM_SIZE - load, f);
    if (ferror(f) || fgetc(f) != EOF) {
        fprintf(stderr, "stub: %s: unreadable, or larger than RAM from its load address\n",
                path);
        fclose(f);
        return -1;
    }
    (void)n;
    fclose(f);
    return 0;
}

int main(int argc, char **argv)
{
    uint8_t *ram;
    cpu_t c;
    uint32_t entry;
    char *end;
    if (argc < 2)
        return 0;
    if (argc > 3) {
        fprintf(stderr, "usage: %s [ENTRY [EXE]]\n", argv[0]);
        return 2;
    }
    entry = (uint32_t)strtoul(argv[1], &end, 16);
    if (*argv[1] == '\0' || *end != '\0') {
        fprintf(stderr, "stub: entry %s is not a hexadecimal address\n", argv[1]);
        return 2;
    }
    ram = calloc(RAM_SIZE, 1);
    if (!ram) {
        fprintf(stderr, "stub: no memory for RAM\n");
        return 1;
    }
    if (argc == 3 && load_exe(ram, argv[2]) != 0)
        return 1;
    memset(&c, 0, sizeof c);
    c.r[29] = STACK_TOP;
    c.slice = STUB_SLICE;
    rt_log_unrecompiled = log_unrecompiled;
    rt_call(ram, &c, entry);
    printf("r");
    for (int i = 0; i < 32; i++)
        printf(" %08" PRIX32, c.r[i]);
    printf("\n");
    free(ram);
    return 0;
}
