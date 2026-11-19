/* common.h: what the smoke and replay programs (smoke.c, replay.c) share: 8 MB RAM images
 * on disk, register names, and the state report. Each program provides the runtime entry
 * points itself.
 */
#ifndef STUB_COMMON_H
#define STUB_COMMON_H

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "recomp.h"

/* What rt_tick reloads slice with, and slice at the start: these programs model no clock, so
 * a check point never needs to call rt_tick in practice. */
#define STUB_SLICE INT32_MAX

static const char *const REG_NAMES[32] = {
    "zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6",
    "t7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9", "k0", "k1", "gp", "sp",
    "fp", "ra"};

/* An 8 MB RAM image read from path into a new buffer; NULL (reported) when the file cannot
 * be read or is not exactly RAM_SIZE bytes. */
static inline uint8_t *ram_load(const char *prog, const char *path)
{
    FILE *f = fopen(path, "rb");
    uint8_t *ram;
    if (!f) {
        fprintf(stderr, "%s: %s: cannot open\n", prog, path);
        return NULL;
    }
    ram = malloc(RAM_SIZE);
    if (!ram || fread(ram, 1, RAM_SIZE, f) != RAM_SIZE || fgetc(f) != EOF) {
        fprintf(stderr, "%s: %s: not an %u-byte RAM image\n", prog, path, RAM_SIZE);
        free(ram);
        fclose(f);
        return NULL;
    }
    fclose(f);
    return ram;
}

static inline int ram_save(const char *prog, const char *path, const uint8_t *ram)
{
    FILE *f = fopen(path, "wb");
    if (!f || fwrite(ram, 1, RAM_SIZE, f) != RAM_SIZE || fclose(f) != 0) {
        fprintf(stderr, "%s: %s: cannot write\n", prog, path);
        return -1;
    }
    return 0;
}

/* The general register named name ("sp", "a0", ...), or -1. */
static inline int reg_index(const char *name)
{
    for (int i = 0; i < 32; i++)
        if (strcmp(name, REG_NAMES[i]) == 0)
            return i;
    return -1;
}

/* The state as lines "r R0 .. R31", "hi H", "lo L", "fpr F0 .. F31", "fcsr X" (FCSR as cfc1
 * reads it), in hexadecimal. */
static inline void print_state(FILE *out, const cpu_t *c)
{
    fprintf(out, "r");
    for (int i = 0; i < 32; i++)
        fprintf(out, " %08" PRIX32, c->r[i]);
    fprintf(out, "\nhi %08" PRIX32 "\nlo %08" PRIX32 "\nfpr", c->hi, c->lo);
    for (int i = 0; i < 32; i++)
        fprintf(out, " %08" PRIX32, c->fpr[i]);
    fprintf(out, "\nfcsr %08" PRIX32 "\n", rt_get_fcsr(c));
}

#endif
