/* The clock behind host.c's rt_clock_ns (included there once; tools/dev/clocktest.c checks it).
 *
 * What tells time asks it: the game's system tick, the kernel's timeouts, rt_now_ns and the
 * renderer's own timing (twice a draw) - tens of thousands of times a second. That is
 * QueryPerformanceCounter, some 20 ns a call, except on a PC that Windows was told to time
 * with the HPET (bcdedit useplatformclock, a common "tweak") or the ACPI timer: there a call
 * takes a microsecond or more, and a player's 8-fighter match spent some 40% of the game's
 * main thread in it. So a slow one is measured at start and, where the CPU's time stamp
 * counter runs at a constant rate (CPUID: invariant TSC - x64 CPUs of the last decade), that
 * counter is read instead, scaled to the same nanoseconds: measured against the system timer
 * at start, and again after a few seconds, half a minute and ten minutes, each time from
 * where the clock stands then (it never steps back). SSBU_CLOCK=qpc or tsc picks one.
 *
 * Needs <windows.h> (or the Linux port's) first. */
#ifndef SSBU_CLOCK_H
#define SSBU_CLOCK_H
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* The x86 counter (TSC) and CPUID exist only on x86. On arm64 (Apple silicon) they are absent:
 * clock_tsc() returns 0 and cpuid_ext_edx() returns 0, so clock_steady stays 0 and the system
 * timer is used, as the header describes for a CPU without an invariant TSC. */
#if defined(_MSC_VER)
#include <intrin.h>
#define SSBU_CLOCK_X86 1
#elif defined(__x86_64__) || defined(__i386__)
#include <cpuid.h>
#include <x86intrin.h>
#define SSBU_CLOCK_X86 1
#endif

typedef struct ClockScale { uint64_t tsc0, ns0, mul; } ClockScale;   /* ns = ns0 + (tsc - tsc0) * mul / 2^32 */
static const ClockScale *volatile clock_scale;   /* NULL: the system timer */
static INIT_ONCE clock_once = INIT_ONCE_STATIC_INIT;
static volatile LONG clock_ready, clock_refining;
static uint64_t qpc_hz, clock_start_ns, cal_tsc, cal_ns, next_refine;   /* (cal_*: the first measurement) */
static int clock_refines, clock_steady, clock_has_rdtscp;
static double clock_read_ns;   /* what a system timer read took, at start */

/* The CPU's counter, read in order (plain RDTSC may run ahead of earlier loads: a thread could
 * then see a time before one it had just read from another thread - as the system timer never does). */
static inline uint64_t clock_tsc(void) {
#ifdef SSBU_CLOCK_X86
    if (clock_has_rdtscp) {
        unsigned aux;
        return __rdtscp(&aux);
    }
    _mm_lfence();
    return __rdtsc();
#else
    return 0;   /* (no TSC here; clock_steady is 0, so this is never used) */
#endif
}

static uint64_t qpc_ns(void) {
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    uint64_t q = (uint64_t)t.QuadPart;
    return q / qpc_hz * 1000000000ull + q % qpc_hz * 1000000000ull / qpc_hz;
}

/* CPUID's extended leaf's EDX (0 where there is none) */
static unsigned cpuid_ext_edx(unsigned leaf) {
#if defined(_MSC_VER)
    int r[4];
    __cpuid(r, (int)0x80000000);
    if ((unsigned)r[0] < leaf) return 0;
    __cpuid(r, (int)leaf);
    return (unsigned)r[3];
#elif defined(SSBU_CLOCK_X86)
    unsigned a, b, c, d;
    return __get_cpuid(leaf, &a, &b, &c, &d) ? d : 0;
#else
    (void)leaf;
    return 0;
#endif
}

/* A time stamp counter reading and the system timer's time at the same moment (the middle of the read). */
static void clock_pair(uint64_t *tsc, uint64_t *ns) {
    uint64_t t0 = clock_tsc();
    *ns = qpc_ns();
    uint64_t t1 = clock_tsc();
    *tsc = t0 + (t1 - t0) / 2;
}

static uint64_t clock_scaled(const ClockScale *s, uint64_t tsc) {
    if ((int64_t)(tsc - s->tsc0) < 0) tsc = s->tsc0;   /* (a core whose counter is a little behind) */
    return s->ns0 + (uint64_t)((unsigned __int128)(tsc - s->tsc0) * s->mul >> 32);
}

/* A new scale from the first measurement to now, taking over from where the clock is. */
static void clock_rescale(void) {
    uint64_t tsc, ns;
    clock_pair(&tsc, &ns);
    ClockScale *n = malloc(sizeof *n);
    if (!n || tsc <= cal_tsc) { free(n); return; }
    n->mul = (uint64_t)((double)(ns - cal_ns) / (double)(tsc - cal_tsc) * 4294967296.0 + 0.5);
    const ClockScale *old = __atomic_load_n(&clock_scale, __ATOMIC_ACQUIRE);
    n->tsc0 = tsc;
    n->ns0 = old ? clock_scaled(old, tsc) : ns;
    __atomic_store_n(&clock_scale, n, __ATOMIC_RELEASE);   /* (the old one stays: another thread may be reading it) */
}

static BOOL CALLBACK clock_setup(PINIT_ONCE once, PVOID param, PVOID *context) {
    (void)once; (void)param; (void)context;
    LARGE_INTEGER f, a, b, t;
    QueryPerformanceFrequency(&f);
    qpc_hz = (uint64_t)f.QuadPart;
    QueryPerformanceCounter(&a);
    for (int i = 0; i < 256; i++) QueryPerformanceCounter(&t);
    QueryPerformanceCounter(&b);
    clock_read_ns = (double)(b.QuadPart - a.QuadPart) * 1e9 / (double)qpc_hz / 257;
    const char *want = getenv("SSBU_CLOCK");
    int slow = clock_read_ns > 250;
    clock_steady = cpuid_ext_edx(0x80000007) >> 8 & 1;    /* invariant TSC */
    clock_has_rdtscp = cpuid_ext_edx(0x80000001) >> 27 & 1;
    int tsc = want && !strcmp(want, "tsc") ? clock_steady : want && !strcmp(want, "qpc") ? 0 : slow && clock_steady;
    clock_start_ns = qpc_ns();
    if (tsc) {   /* a first scale over 20 ms */
        clock_pair(&cal_tsc, &cal_ns);
        while (qpc_ns() - cal_ns < 20000000) {}
        clock_rescale();
        next_refine = cal_tsc + (uint64_t)((double)(clock_tsc() - cal_tsc) * 100);   /* after some 2 s */
    }
    fprintf(stderr, "[clock] system timer %.2f MHz, %.0f ns a read%s\n", qpc_hz / 1e6, clock_read_ns,
            tsc ? (slow ? ": slow (Windows set to use the HPET? \"bcdedit /deletevalue useplatformclock\" undoes that) - timing from the CPU's counter instead"
                        : ": timing from the CPU's counter (SSBU_CLOCK=tsc)")
                : slow ? (clock_steady ? " (SSBU_CLOCK=qpc)" : ": slow, and this CPU's counter does not run at a constant rate - expect a lower frame rate")
                       : want && !strcmp(want, "tsc") ? " (SSBU_CLOCK=tsc, but this CPU's counter does not run at a constant rate)" : "");
    __atomic_store_n(&clock_ready, 1, __ATOMIC_RELEASE);   /* (after all of the above, for readers that skip InitOnce) */
    return TRUE;
}

/* Nanoseconds on the system timer's time line (since the PC started), cheaply. */
static uint64_t clock_now_ns(void) {
    if (!__atomic_load_n(&clock_ready, __ATOMIC_ACQUIRE)) InitOnceExecuteOnce(&clock_once, clock_setup, NULL, NULL);
    const ClockScale *s = __atomic_load_n(&clock_scale, __ATOMIC_ACQUIRE);
    if (!s) return qpc_ns();
    uint64_t tsc = clock_tsc();
    /* the scale again over a longer time: after 2 s, 30 s and 10 min */
    if (tsc >= next_refine && clock_refines < 3 && !InterlockedExchange(&clock_refining, 1)) {
        if (tsc >= next_refine && clock_refines < 3) {
            clock_rescale();
            static const uint32_t factor[3] = {15, 20, 0};
            next_refine = cal_tsc + (tsc - cal_tsc) * factor[clock_refines++];
        }
        InterlockedExchange(&clock_refining, 0);
        s = __atomic_load_n(&clock_scale, __ATOMIC_ACQUIRE);
    }
    return clock_scaled(s, tsc);
}

#endif
