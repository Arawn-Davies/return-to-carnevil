/* recomp.h: the contract between generated code, the reference interpreter and a runtime.
 *
 * The CPU is an R5000 run with 32-bit general registers and FR = 0: 32 FP register halves of
 * 32 bits; a single is one half, a double in even register n is half n (low word) and half
 * n + 1 (high word). The board has 8 MB of RAM, seen at 0x80000000 (kseg0) and 0xA0000000
 * (kseg1); every other address is the bus.
 *
 * Results that are MAME's own behaviour rather than an established R5000 fact (the oracle
 * the port is checked against, see vectors.MAME_SPECIFIC) are marked "MAME:" below.
 *
 * Build every file including this one with -std=c11 -O2 -ffp-contract=off
 * -fno-strict-aliasing -fwrapv, never -ffast-math.
 */
#ifndef RECOMP_H
#define RECOMP_H

#include <math.h>
#include <stdint.h>
#include <string.h>

#if !defined(__BYTE_ORDER__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "recomp.h: RAM is accessed in host byte order, which must be little-endian"
#endif

typedef struct cpu {
    uint32_t r[32];
    uint32_t hi, lo;
    uint32_t fpr[32];
    uint32_t fcsr;  /* FCSR without its condition bits (FCSR bits 23 and 25..31 are 0 here) */
    uint8_t fcc;    /* bit n is FP condition code n */
    uint32_t pc;    /* used by the interpreter only */
    volatile uint32_t irq_pending;
    void *rt;       /* the runtime's own data */
} cpu_t;

typedef void (*recomp_fn)(uint8_t *ram, cpu_t *c);

#define RAM_SIZE 0x800000u

/* --- provided by a runtime ------------------------------------------------------------ */

uint32_t bus_read(cpu_t *c, uint32_t addr, int size);
void bus_write(cpu_t *c, uint32_t addr, int size, uint32_t value);
void rt_loader(uint8_t *ram, cpu_t *c, uint32_t slot);
/* code: the 20-bit field of the break word, bits 6 to 25 */
void rt_break(uint8_t *ram, cpu_t *c, uint32_t pc, uint32_t code);
void rt_interrupt(uint8_t *ram, cpu_t *c);
/* the recompiled function at addr, or NULL when there is none */
recomp_fn recomp_lookup(uint32_t addr);

/* --- provided by runtime/core --------------------------------------------------------- */

/* The recompiled function at addr if there is one, otherwise the interpreter. */
void rt_call(uint8_t *ram, cpu_t *c, uint32_t addr);
/* The same, for a jump that leaves its function. */
void rt_jump(uint8_t *ram, cpu_t *c, uint32_t addr);
/* FCSR as ctc1 $31 writes it (condition bits in place); also sets the host rounding mode
 * from RM and host flush-to-zero from FS. */
void rt_set_fcsr(cpu_t *c, uint32_t value);
/* FCSR as cfc1 $31 reads it. */
uint32_t rt_get_fcsr(const cpu_t *c);
/* Called once per distinct address the interpreter is entered at; NULL: not called. */
extern void (*rt_log_unrecompiled)(uint32_t addr);

#define RT_CHECK(ram, c)                                                                    \
    do {                                                                                    \
        if ((c)->irq_pending)                                                               \
            rt_interrupt((ram), (c));                                                       \
    } while (0)

/* --- provided by runtime/interp ------------------------------------------------------- */

/* Run from pc until the function entered there returns (its jr ra). */
void interp_run(uint8_t *ram, cpu_t *c, uint32_t pc);
/* Forget the entry addresses already given to rt_log_unrecompiled (for test harnesses
 * that run independent cases in one process). */
void interp_forget_entries(void);

/* --- memory ---------------------------------------------------------------------------- */

static inline int mem_is_ram(uint32_t addr, uint32_t size)
{
    return addr >= 0x80000000u && addr <= 0xBFFFFFFFu && (addr & 0x1FFFFFFFu) + size <= RAM_SIZE;
}

static inline uint32_t mem_r8(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    if (mem_is_ram(addr, 1))
        return ram[addr & 0x1FFFFFFFu];
    return bus_read(c, addr, 1) & 0xFFu;
}

static inline uint32_t mem_r16(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    if (mem_is_ram(addr, 2)) {
        uint16_t v;
        memcpy(&v, ram + (addr & 0x1FFFFFFFu), 2);
        return v;
    }
    return bus_read(c, addr, 2) & 0xFFFFu;
}

static inline uint32_t mem_r32(uint8_t *ram, cpu_t *c, uint32_t addr)
{
    if (mem_is_ram(addr, 4)) {
        uint32_t v;
        memcpy(&v, ram + (addr & 0x1FFFFFFFu), 4);
        return v;
    }
    return bus_read(c, addr, 4);
}

static inline void mem_w8(uint8_t *ram, cpu_t *c, uint32_t addr, uint32_t value)
{
    if (mem_is_ram(addr, 1))
        ram[addr & 0x1FFFFFFFu] = (uint8_t)value;
    else
        bus_write(c, addr, 1, value & 0xFFu);
}

static inline void mem_w16(uint8_t *ram, cpu_t *c, uint32_t addr, uint32_t value)
{
    if (mem_is_ram(addr, 2)) {
        uint16_t v = (uint16_t)value;
        memcpy(ram + (addr & 0x1FFFFFFFu), &v, 2);
    } else {
        bus_write(c, addr, 2, value & 0xFFFFu);
    }
}

static inline void mem_w32(uint8_t *ram, cpu_t *c, uint32_t addr, uint32_t value)
{
    if (mem_is_ram(addr, 4))
        memcpy(ram + (addr & 0x1FFFFFFFu), &value, 4);
    else
        bus_write(c, addr, 4, value);
}

/* --- integer helpers -------------------------------------------------------------------- */

/* MAME: division by zero leaves hi and lo unchanged. INT_MIN / -1 (which MAME cannot run)
 * gives lo = 0x80000000, hi = 0, as the R5000 does. */
static inline void rc_div(cpu_t *c, uint32_t rs, uint32_t rt)
{
    if (rt == 0)
        return;
    if (rs == 0x80000000u && rt == 0xFFFFFFFFu) {
        c->lo = 0x80000000u;
        c->hi = 0;
        return;
    }
    int32_t a = (int32_t)rs, b = (int32_t)rt;
    c->lo = (uint32_t)(a / b);
    c->hi = (uint32_t)(a % b);
}

static inline void rc_divu(cpu_t *c, uint32_t rs, uint32_t rt)
{
    if (rt == 0)
        return;
    c->lo = rs / rt;
    c->hi = rs % rt;
}

static inline void rc_mult(cpu_t *c, uint32_t rs, uint32_t rt)
{
    uint64_t p = (uint64_t)((int64_t)(int32_t)rs * (int64_t)(int32_t)rt);
    c->lo = (uint32_t)p;
    c->hi = (uint32_t)(p >> 32);
}

static inline void rc_multu(cpu_t *c, uint32_t rs, uint32_t rt)
{
    uint64_t p = (uint64_t)rs * rt;
    c->lo = (uint32_t)p;
    c->hi = (uint32_t)(p >> 32);
}

/* --- FP registers ----------------------------------------------------------------------- */

static inline float rc_fs(const cpu_t *c, unsigned n)
{
    float f;
    memcpy(&f, &c->fpr[n & 31], 4);
    return f;
}

static inline void rc_set_fs(cpu_t *c, unsigned n, float f)
{
    memcpy(&c->fpr[n & 31], &f, 4);
}

/* A double in register n: halves n & ~1 (low) and (n & ~1) + 1 (high). An odd n is not a
 * double register under FR = 0; masking it keeps the access inside fpr (what the R5000 or
 * MAME does with one is not verified, and the reached code is not known to use one). */
static inline double rc_fd(const cpu_t *c, unsigned n)
{
    uint64_t bits = (uint64_t)c->fpr[n & 30] | (uint64_t)c->fpr[(n & 30) + 1] << 32;
    double d;
    memcpy(&d, &bits, 8);
    return d;
}

static inline void rc_set_fd(cpu_t *c, unsigned n, double d)
{
    uint64_t bits;
    memcpy(&bits, &d, 8);
    c->fpr[n & 30] = (uint32_t)bits;
    c->fpr[(n & 30) + 1] = (uint32_t)(bits >> 32);
}

/* --- FP conversions --------------------------------------------------------------------- */

/* MAME: a NaN or a value outside int32 gives 0x80000000 (the x86 conversion result). */
static inline uint32_t rc_trunc_w_s(float f)
{
    if (f >= -2147483648.0f && f < 2147483648.0f)
        return (uint32_t)(int32_t)f;
    return 0x80000000u;
}

static inline uint32_t rc_trunc_w_d(double d)
{
    if (d > -2147483649.0 && d < 2147483648.0)
        return (uint32_t)(int32_t)d;
    return 0x80000000u;
}

static inline float rc_sqrt_s(float f)
{
    return sqrtf(f);
}

/* --- FP condition codes and compares ---------------------------------------------------- */

static inline int rc_cc(const cpu_t *c, unsigned cc)
{
    return (c->fcc >> (cc & 7)) & 1;
}

static inline void rc_set_cc(cpu_t *c, unsigned cc, int cond)
{
    uint8_t bit = (uint8_t)(1u << (cc & 7));
    c->fcc = (uint8_t)(cond ? c->fcc | bit : c->fcc & ~bit);
}

/* An unordered pair (a NaN) compares false. MAME raises no exception for it. */
static inline void rc_c_eq_s(cpu_t *c, unsigned cc, float a, float b) { rc_set_cc(c, cc, a == b); }
static inline void rc_c_lt_s(cpu_t *c, unsigned cc, float a, float b) { rc_set_cc(c, cc, a < b); }
static inline void rc_c_le_s(cpu_t *c, unsigned cc, float a, float b) { rc_set_cc(c, cc, a <= b); }
static inline void rc_c_eq_d(cpu_t *c, unsigned cc, double a, double b) { rc_set_cc(c, cc, a == b); }
static inline void rc_c_lt_d(cpu_t *c, unsigned cc, double a, double b) { rc_set_cc(c, cc, a < b); }
static inline void rc_c_le_d(cpu_t *c, unsigned cc, double a, double b) { rc_set_cc(c, cc, a <= b); }

/* --- the multiply-add family ------------------------------------------------------------ */

/* MAME: the product is rounded to the format, then the sum (two roundings; never fused, so
 * two statements); nmadd and nmsub negate the rounded sum. Operands in assembler order:
 * madd.fmt fd, fr, fs, ft is fd = fs * ft + fr. */
static inline float rc_madd_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    return p + fr;
}

static inline float rc_msub_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    return p - fr;
}

static inline float rc_nmadd_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    float s = p + fr;
    return -s;
}

static inline float rc_nmsub_s(float fr, float fs, float ft)
{
    float p = fs * ft;
    float s = p - fr;
    return -s;
}

static inline double rc_madd_d(double fr, double fs, double ft)
{
    double p = fs * ft;
    return p + fr;
}

#endif
