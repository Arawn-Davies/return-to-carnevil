/* fpu.c: FCSR, and the host FP environment that follows it.
 *
 * The rounding mode FCSR.RM (bits 0-1: 0 nearest, 1 toward zero, 2 toward +inf, 3 toward
 * -inf) becomes the host rounding mode, and FCSR.FS (bit 24) host flush-to-zero, so that
 * plain C arithmetic in generated code and in the interpreter rounds as the R5000 would.
 * Host FS: arm64 FPCR.FZ flushes denormal inputs and results; on x86-64 MXCSR FTZ and DAZ
 * are both set, so the two hosts agree. Any other host is a compile error.
 *
 * FCSR's flag and cause bits change only through ctc1: MAME does not accumulate them
 * (no recorded vector changes them), so neither does this runtime.
 */
#include "recomp.h"

#if defined(__aarch64__)

static void host_fp(uint32_t rm, int flush)
{
    /* FPCR.RMode, bits 22-23: 0 nearest, 1 toward +inf, 2 toward -inf, 3 toward zero */
    static const uint64_t rmode[4] = {0u, 3u, 1u, 2u};
    uint64_t fpcr;
    __asm__ volatile("mrs %0, fpcr" : "=r"(fpcr));
    fpcr &= ~(3ull << 22 | 1ull << 24);
    fpcr |= rmode[rm & 3] << 22 | (uint64_t)(flush != 0) << 24;
    __asm__ volatile("msr fpcr, %0" : : "r"(fpcr) : "memory");
}

#elif defined(__x86_64__)

#include <xmmintrin.h>

static void host_fp(uint32_t rm, int flush)
{
    /* MXCSR.RC, bits 13-14: 0 nearest, 1 toward -inf, 2 toward +inf, 3 toward zero;
     * FTZ is bit 15, DAZ bit 6 */
    static const unsigned rc[4] = {0u, 3u, 2u, 1u};
    unsigned mxcsr = _mm_getcsr();
    mxcsr &= ~(3u << 13 | 1u << 15 | 1u << 6);
    mxcsr |= rc[rm & 3] << 13 | (flush ? 1u << 15 | 1u << 6 : 0u);
    _mm_setcsr(mxcsr);
}

#else
#error "fpu.c: no host FP control for this architecture (arm64 and x86-64 only)"
#endif

#define FCSR_CC_MASK 0xFE800000u /* cc0 is bit 23, cc1..cc7 are bits 25..31 */
#define FCSR_FS (1u << 24)

void rt_set_fcsr(cpu_t *c, uint32_t value)
{
    c->fcsr = value & ~FCSR_CC_MASK;
    c->fcc = (uint8_t)((value >> 23 & 1u) | (value >> 24 & 0xFEu));
    host_fp(value & 3u, (value & FCSR_FS) != 0);
}

uint32_t rt_get_fcsr(const cpu_t *c)
{
    return c->fcsr | (uint32_t)(c->fcc & 1u) << 23 | (uint32_t)(c->fcc & 0xFEu) << 24;
}
