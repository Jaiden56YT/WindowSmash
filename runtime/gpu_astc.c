/* ASTC (Adaptive Scalable Texture Compression) blocks to RGBA8, for textures in the ASTC formats.
 *
 * The Switch's GPU samples ASTC; Direct3D 11 has no ASTC format, so the renderers take such
 * a texture as 8-bit RGBA, decoded here as it is read (gpu_texture.c). The game's Mii faces
 * are drawn from ASTC 4x4 textures (the face parts); without this they came out blank.
 *
 * LDR profile, 2D blocks: every block size, 1-4 partitions, dual plane, every LDR colour
 * endpoint mode and the void-extent (constant colour) block. HDR endpoint modes and reserved
 * encodings decode as the error colour (magenta), as LDR hardware does. Follows the ASTC
 * specification (Khronos Data Format 1.3, chapter 23). sRGB textures keep their endpoints
 * as the specification's sRGB decode does (the low byte 0x80 rather than a copy). */
#include <string.h>

#include "gpu_texture.h"

static u32 bits_upto(const u8 *p, u32 start, u32 n, u32 end) {   /* n (<= 32) bits from bit `start`, little-endian; 0 from `end` */
    u64 v = 0;
    for (u32 i = 0; i < n && start + i < end; i++) v |= (u64)((p[(start + i) >> 3] >> ((start + i) & 7)) & 1) << i;
    return (u32)v;
}

static u32 bits_at(const u8 *p, u32 start, u32 n) { return bits_upto(p, start, n, 128); }

/* ---- integer sequence encoding --------------------------------------------------------------- */
/* A range of values: 2^bits times 1, 3 (trits) or 5 (quints). */
typedef struct Range { u8 trits, quints, bits; } Range;

static u32 ise_bits(Range r, u32 count) {
    return count * r.bits + (r.trits ? (count * 8 + 4) / 5 : 0) + (r.quints ? (count * 7 + 2) / 3 : 0);
}

static void decode_trits(u32 t, u32 *out) {
    u32 c, t0, t1, t2, t3, t4;
    if (((t >> 2) & 7) == 7) { c = ((t >> 5) & 7) << 2 | (t & 3); t4 = 2; t3 = 2; }
    else {
        c = t & 0x1F;
        if (((t >> 5) & 3) == 3) { t4 = 2; t3 = (t >> 7) & 1; }
        else { t4 = (t >> 7) & 1; t3 = (t >> 5) & 3; }
    }
    if ((c & 3) == 3) { t2 = 2; t1 = (c >> 4) & 1; t0 = ((c >> 3) & 1) << 1 | ((c >> 2) & 1 & ~(c >> 3)); }
    else if (((c >> 2) & 3) == 3) { t2 = 2; t1 = 2; t0 = c & 3; }
    else { t2 = (c >> 4) & 1; t1 = (c >> 2) & 3; t0 = ((c >> 1) & 1) << 1 | (c & 1 & ~(c >> 1)); }
    out[0] = t0; out[1] = t1; out[2] = t2; out[3] = t3; out[4] = t4;
}

static void decode_quints(u32 q, u32 *out) {
    u32 c, q0, q1, q2;
    if (((q >> 1) & 3) == 3 && ((q >> 5) & 3) == 0) {
        q2 = (q & 1) << 2 | (((q >> 4) & 1) & ~q & 1) << 1 | (((q >> 3) & 1) & ~q & 1);
        q1 = 4; q0 = 4;
    } else {
        if (((q >> 1) & 3) == 3) { q2 = 4; c = ((q >> 3) & 3) << 3 | (~(q >> 5) & 3) << 1 | (q & 1); }
        else { q2 = (q >> 5) & 3; c = q & 0x1F; }
        if ((c & 7) == 5) { q1 = 4; q0 = (c >> 3) & 3; }
        else { q1 = (c >> 3) & 3; q0 = c & 7; }
    }
    out[0] = q0; out[1] = q1; out[2] = q2;
}

/* count values of range r from bit `start` of p: each its trit/quint (tq) and low bits (m). A
 * last group cut short has its missing bits 0. */
static void ise_decode(const u8 *p, u32 start, Range r, u32 count, u8 *tq, u8 *m) {
    u32 pos = start, end = start + ise_bits(r, count);
    if (r.trits) {
        for (u32 g = 0; g < count; g += 5) {
            static const u8 tbits[5] = {2, 2, 1, 2, 1};
            u32 t = 0, tpos = 0, v[5];
            for (u32 i = 0; i < 5; i++) {
                u32 mi = bits_upto(p, pos, r.bits, end); pos += r.bits;
                t |= bits_upto(p, pos, tbits[i], end) << tpos; pos += tbits[i]; tpos += tbits[i];
                if (g + i < count) m[g + i] = (u8)mi;
            }
            decode_trits(t, v);
            for (u32 i = 0; i < 5 && g + i < count; i++) tq[g + i] = (u8)v[i];
        }
    } else if (r.quints) {
        for (u32 g = 0; g < count; g += 3) {
            static const u8 qbits[3] = {3, 2, 2};
            u32 q = 0, qpos = 0, v[3];
            for (u32 i = 0; i < 3; i++) {
                u32 mi = bits_upto(p, pos, r.bits, end); pos += r.bits;
                q |= bits_upto(p, pos, qbits[i], end) << qpos; pos += qbits[i]; qpos += qbits[i];
                if (g + i < count) m[g + i] = (u8)mi;
            }
            decode_quints(q, v);
            for (u32 i = 0; i < 3 && g + i < count; i++) tq[g + i] = (u8)v[i];
        }
    } else
        for (u32 i = 0; i < count; i++) { m[i] = (u8)bits_at(p, pos, r.bits); tq[i] = 0; pos += r.bits; }
}

/* ---- unquantization -------------------------------------------------------------------------- */
static u32 replicate(u32 v, u32 from, u32 to) {   /* bit replication of a from-bit value to `to` bits */
    if (!from) return 0;
    u32 out = 0;
    for (int shift = (int)to - (int)from; shift > -(int)from; shift -= (int)from)
        out |= shift >= 0 ? v << shift : v >> -shift;
    return out & ((1u << to) - 1);
}

static u32 unquant_color(Range r, u32 tq, u32 m) {
    if (!r.trits && !r.quints) return replicate(m, r.bits, 8);
    u32 a = m & 1 ? 0x1FF : 0, x = m >> 1, b = 0, c;
    if (r.trits) {
        static const u8 cs[] = {0, 204, 93, 44, 22, 11, 5};
        c = cs[r.bits];
        switch (r.bits) {
        case 2: b = x << 8 | x << 4 | x << 2 | x << 1; break;
        case 3: b = x << 7 | x << 2 | x; break;
        case 4: b = x << 6 | x; break;
        case 5: b = x << 5 | x >> 2; break;
        case 6: b = x << 4 | x >> 4; break;
        }
    } else {
        static const u8 cs[] = {0, 113, 54, 26, 13, 6};
        c = cs[r.bits];
        switch (r.bits) {
        case 2: b = x << 8 | x << 3 | x << 2; break;
        case 3: b = x << 7 | x << 1 | x >> 1; break;
        case 4: b = x << 6 | x >> 1; break;
        case 5: b = x << 5 | x >> 3; break;
        }
    }
    u32 t = (tq * c + b) ^ a;
    return (a & 0x80) | (t >> 2 & 0xFF);
}

static u32 unquant_weight(Range r, u32 tq, u32 m) {
    u32 w;
    if (!r.trits && !r.quints) w = replicate(m, r.bits, 6);
    else if (!r.bits) {
        static const u8 trit[3] = {0, 32, 63}, quint[5] = {0, 16, 32, 47, 63};
        w = r.trits ? trit[tq] : quint[tq];
    }
    else {
        u32 a = m & 1 ? 0x7F : 0, x = m >> 1, b = 0, c;
        if (r.trits) {
            c = r.bits == 1 ? 50 : r.bits == 2 ? 23 : 11;
            b = r.bits == 2 ? (x << 6 | x << 2 | x) : r.bits == 3 ? (x << 5 | x) : 0;
        } else {
            c = r.bits == 1 ? 28 : 13;
            b = r.bits == 2 ? (x << 6 | x << 1) : 0;
        }
        u32 t = (tq * c + b) ^ a;
        w = (a & 0x20) | (t >> 2 & 0x1F);
    }
    return w + (w > 32);
}

/* ---- colour endpoints ------------------------------------------------------------------------ */
static int clamp255(int v) { return v < 0 ? 0 : v > 255 ? 255 : v; }

static void transfer_signed(int *a, int *b) {   /* (offset a, base b) */
    *b >>= 1; *b |= *a & 0x80; *a >>= 1; *a &= 0x3F;
    if (*a & 0x20) *a -= 0x40;
}

static void blue_contract(int *e) { e[0] = (e[0] + e[2]) >> 1; e[1] = (e[1] + e[2]) >> 1; }

/* Endpoints e0/e1 (RGBA, 0-255) of an LDR mode; 0 for an HDR mode. */
static int endpoints(u32 mode, const int *v, int *e0, int *e1) {
    int a[8];
    memcpy(a, v, sizeof a);
    switch (mode) {
    case 0:
        e0[0] = e0[1] = e0[2] = a[0]; e0[3] = 255; e1[0] = e1[1] = e1[2] = a[1]; e1[3] = 255; return 1;
    case 1: {
        int l0 = (a[0] >> 2) | (a[1] & 0xC0), l1 = l0 + (a[1] & 0x3F);
        if (l1 > 255) l1 = 255;
        e0[0] = e0[1] = e0[2] = l0; e0[3] = 255; e1[0] = e1[1] = e1[2] = l1; e1[3] = 255; return 1;
    }
    case 4:
        e0[0] = e0[1] = e0[2] = a[0]; e0[3] = a[2]; e1[0] = e1[1] = e1[2] = a[1]; e1[3] = a[3]; return 1;
    case 5:
        transfer_signed(&a[1], &a[0]); transfer_signed(&a[3], &a[2]);
        e0[0] = e0[1] = e0[2] = a[0]; e0[3] = a[2];
        e1[0] = e1[1] = e1[2] = clamp255(a[0] + a[1]); e1[3] = clamp255(a[2] + a[3]); return 1;
    case 6:
        for (int i = 0; i < 3; i++) { e0[i] = (a[i] * a[3]) >> 8; e1[i] = a[i]; }
        e0[3] = e1[3] = 255; return 1;
    case 8: case 12:
        e0[3] = mode == 12 ? a[6] : 255; e1[3] = mode == 12 ? a[7] : 255;
        if (a[1] + a[3] + a[5] >= a[0] + a[2] + a[4]) {
            for (int i = 0; i < 3; i++) { e0[i] = a[2 * i]; e1[i] = a[2 * i + 1]; }
        } else {
            for (int i = 0; i < 3; i++) { e0[i] = a[2 * i + 1]; e1[i] = a[2 * i]; }
            int t = e0[3]; e0[3] = e1[3]; e1[3] = t;
            blue_contract(e0); blue_contract(e1);
        }
        return 1;
    case 9: case 13:
        for (int i = 0; i < (mode == 13 ? 4 : 3); i++) transfer_signed(&a[2 * i + 1], &a[2 * i]);
        if (mode == 9) { a[6] = 255; a[7] = 0; }
        if (a[1] + a[3] + a[5] >= 0) {
            for (int i = 0; i < 4; i++) { e0[i] = a[2 * i]; e1[i] = a[2 * i] + a[2 * i + 1]; }
        } else {
            for (int i = 0; i < 4; i++) { e0[i] = a[2 * i] + a[2 * i + 1]; e1[i] = a[2 * i]; }
            blue_contract(e0); blue_contract(e1);
        }
        for (int i = 0; i < 4; i++) { e0[i] = clamp255(e0[i]); e1[i] = clamp255(e1[i]); }
        return 1;
    case 10:
        for (int i = 0; i < 3; i++) { e0[i] = (a[i] * a[3]) >> 8; e1[i] = a[i]; }
        e0[3] = a[4]; e1[3] = a[5]; return 1;
    }
    return 0;
}

/* ---- partitions ------------------------------------------------------------------------------ */
static u32 hash52(u32 p) {
    p ^= p >> 15; p -= p << 17; p += p << 7; p += p << 4;
    p ^= p >> 5; p += p << 16; p ^= p >> 7; p ^= p >> 3;
    p ^= p << 6; p ^= p >> 17;
    return p;
}

static u32 select_partition(u32 seed, u32 x, u32 y, u32 count, int small_block) {
    if (small_block) { x <<= 1; y <<= 1; }
    seed += (count - 1) * 1024;
    u32 r = hash52(seed);
    u8 s[12];
    static const u8 sh[12] = {0, 4, 8, 12, 16, 20, 24, 28, 18, 22, 26, 30};
    for (int i = 0; i < 11; i++) s[i] = (u8)((r >> sh[i]) & 0xF);
    s[11] = (u8)(((r >> 30) | (r << 2)) & 0xF);
    for (int i = 0; i < 12; i++) s[i] = (u8)(s[i] * s[i]);
    int sh1, sh2;
    if (seed & 1) { sh1 = seed & 2 ? 4 : 5; sh2 = count == 3 ? 6 : 5; }
    else { sh1 = count == 3 ? 6 : 5; sh2 = seed & 2 ? 4 : 5; }
    int sh3 = seed & 0x10 ? sh1 : sh2;
    for (int i = 0; i < 8; i++) s[i] >>= i & 1 ? sh2 : sh1;
    for (int i = 8; i < 12; i++) s[i] >>= sh3;
    int a = (s[0] * x + s[1] * y + (r >> 14)) & 0x3F;   /* (z = 0: 2D) */
    int b = (s[2] * x + s[3] * y + (r >> 10)) & 0x3F;
    int c = (s[4] * x + s[5] * y + (r >> 6)) & 0x3F;
    int d = (s[6] * x + s[7] * y + (r >> 2)) & 0x3F;
    if (count < 4) d = 0;
    if (count < 3) c = 0;
    if (a >= b && a >= c && a >= d) return 0;
    if (b >= c && b >= d) return 1;
    if (c >= d) return 2;
    return 3;
}

/* ---- blocks ---------------------------------------------------------------------------------- */
static void fill(u8 *out, u32 stride, u32 w, u32 h, const u8 *rgba) {
    for (u32 y = 0; y < h; y++)
        for (u32 x = 0; x < w; x++) memcpy(out + (size_t)y * stride + x * 4, rgba, 4);
}

/* Weight ranges by the block mode's R (bits) and H (high precision). */
static Range weight_range(u32 r, u32 h) {
    static const Range low[8] = {{0}, {0}, {0, 0, 1}, {1, 0, 0}, {0, 0, 2}, {0, 1, 0}, {1, 0, 1}, {0, 0, 3}};
    static const Range high[8] = {{0}, {0}, {0, 1, 1}, {1, 0, 2}, {0, 0, 4}, {0, 1, 2}, {1, 0, 3}, {0, 0, 5}};
    return h ? high[r] : low[r];
}

/* Colour ranges, largest first. */
static const Range COLOR_RANGES[] = {
    {0, 0, 8}, {1, 0, 6}, {0, 1, 5}, {0, 0, 7}, {1, 0, 5}, {0, 1, 4}, {0, 0, 6}, {1, 0, 4}, {0, 1, 3}, {0, 0, 5},
    {1, 0, 3}, {0, 1, 2}, {0, 0, 4}, {1, 0, 2}, {0, 1, 1}, {0, 0, 3}, {1, 0, 1},
};

/* One block (16 bytes) to bw x bh texels of RGBA8 at out (stride bytes a row), clipped to w x h. */
void astc_decode_block(const u8 *blk, u32 bw, u32 bh, int srgb, u8 *out, u32 stride, u32 w, u32 h) {
    static const u8 error[4] = {255, 0, 255, 255};
    u32 mode = bits_at(blk, 0, 11);
    if ((mode & 0x1FF) == 0x1FC) {   /* void extent: one colour */
        u32 s0 = bits_at(blk, 12, 13), s1 = bits_at(blk, 25, 13), t0 = bits_at(blk, 38, 13), t1 = bits_at(blk, 51, 13);
        int all_ones = s0 == 0x1FFF && s1 == 0x1FFF && t0 == 0x1FFF && t1 == 0x1FFF;
        if (mode & 0x200) { fill(out, stride, w, h, error); return; }   /* (HDR) */
        if (bits_at(blk, 10, 2) != 3 || (!all_ones && (s0 >= s1 || t0 >= t1))) { fill(out, stride, w, h, error); return; }
        u8 c[4];
        for (int i = 0; i < 4; i++) c[i] = (u8)(bits_at(blk, 64 + 16 * i, 16) >> 8);
        fill(out, stride, w, h, c);
        return;
    }
    /* block mode: weight grid size, range, dual plane */
    u32 gw, gh, r, hp = (mode >> 9) & 1, dual = (mode >> 10) & 1;
    u32 A = (mode >> 5) & 3, B = (mode >> 7) & 3;
    if (mode & 3) {
        r = ((mode >> 4) & 1) | (mode & 3) << 1;
        switch ((mode >> 2) & 3) {
        case 0: gw = B + 4; gh = A + 2; break;
        case 1: gw = B + 8; gh = A + 2; break;
        case 2: gw = A + 2; gh = B + 8; break;
        default:
            if (mode & 0x100) { gw = (B & 1) + 2; gh = A + 2; }
            else { gw = A + 2; gh = (B & 1) + 6; }
        }
    } else {
        r = ((mode >> 4) & 1) | ((mode >> 2) & 3) << 1;
        if ((mode & 0xF) == 0) { fill(out, stride, w, h, error); return; }   /* (reserved) */
        switch ((mode >> 7) & 3) {
        case 0: gw = 12; gh = A + 2; break;
        case 1: gw = A + 2; gh = 12; break;
        case 2: gw = A + 6; gh = ((mode >> 9) & 3) + 6; hp = 0; dual = 0; break;
        default:
            if ((mode >> 5) & 2) { fill(out, stride, w, h, error); return; }   /* (reserved) */
            if ((mode >> 5) & 1) { gw = 10; gh = 6; } else { gw = 6; gh = 10; }
        }
    }
    Range wr = weight_range(r, hp);
    u32 partitions = bits_at(blk, 11, 2) + 1;
    u32 nweights = gw * gh * (dual + 1), wbits = ise_bits(wr, nweights);
    if (gw > bw || gh > bh || nweights > 64 || wbits < 24 || wbits > 96 || (dual && partitions == 4)) {
        fill(out, stride, w, h, error); return;
    }
    /* colour endpoint modes */
    u32 cem[4], color_start, extra_bits = 0, below = 128 - wbits;
    if (partitions == 1) { cem[0] = bits_at(blk, 13, 4); color_start = 17; }
    else {
        color_start = 29;
        u32 info = bits_at(blk, 23, 6);
        if ((info & 3) == 0)
            for (u32 i = 0; i < partitions; i++) cem[i] = info >> 2;
        else {
            extra_bits = 3 * partitions - 4;
            u32 v = info >> 2 | bits_at(blk, below - extra_bits, extra_bits) << 4, base = (info & 3) - 1;
            for (u32 i = 0; i < partitions; i++) cem[i] = ((base + ((v >> i) & 1)) << 2) | ((v >> (partitions + 2 * i)) & 3);
        }
    }
    u32 ccs = dual ? bits_at(blk, below - extra_bits - 2, 2) : 0;
    u32 color_end = below - extra_bits - (dual ? 2 : 0), nvalues = 0;
    for (u32 i = 0; i < partitions; i++) nvalues += 2 * ((cem[i] >> 2) + 1);
    if (nvalues > 18 || color_end < color_start) { fill(out, stride, w, h, error); return; }
    const Range *cr = NULL;
    for (u32 i = 0; i < sizeof COLOR_RANGES / sizeof *COLOR_RANGES; i++)
        if (ise_bits(COLOR_RANGES[i], nvalues) <= color_end - color_start) { cr = &COLOR_RANGES[i]; break; }
    if (!cr) { fill(out, stride, w, h, error); return; }
    u8 tq[64], m[64];
    ise_decode(blk, color_start, *cr, nvalues, tq, m);
    int ep[4][2][4];
    for (u32 i = 0, k = 0; i < partitions; i++) {
        int v[8] = {0};
        u32 n = 2 * ((cem[i] >> 2) + 1);
        for (u32 j = 0; j < n; j++, k++) v[j] = (int)unquant_color(*cr, tq[k], m[k]);
        if (!endpoints(cem[i], v, ep[i][0], ep[i][1])) { fill(out, stride, w, h, error); return; }
    }
    /* weights: stored backwards from the block's top bit */
    u8 rev[16];
    for (int i = 0; i < 16; i++) {
        u8 b = blk[15 - i], o = 0;
        for (int j = 0; j < 8; j++) o |= (u8)(((b >> j) & 1) << (7 - j));
        rev[i] = o;
    }
    ise_decode(rev, 0, wr, nweights, tq, m);
    u8 gwt[64 + 16] = {0};
    for (u32 i = 0; i < nweights; i++) gwt[i] = (u8)unquant_weight(wr, tq[i], m[i]);
    /* texels: weights infilled from the grid, then interpolated */
    u32 ds = (1024 + bw / 2) / (bw - 1), dt = bh > 1 ? (1024 + bh / 2) / (bh - 1) : 0;
    u32 seed = partitions > 1 ? bits_at(blk, 13, 10) : 0;
    int small_block = bw * bh < 31, planes = (int)dual + 1;
    for (u32 y = 0; y < h; y++)
        for (u32 x = 0; x < w; x++) {
            u32 gs = (ds * x * (gw - 1) + 32) >> 6, gt = (dt * y * (gh - 1) + 32) >> 6;
            u32 js = gs >> 4, fs = gs & 0xF, jt = gt >> 4, ft = gt & 0xF;
            u32 w11 = (fs * ft + 8) >> 4, w10 = ft - w11, w01 = fs - w11, w00 = 16 - fs - ft + w11;
            u32 v0 = js + jt * gw, wt[2];
            for (int p = 0; p < planes; p++) {
                const u8 *g = gwt + p;
                u32 p00 = g[v0 * planes], p01 = w01 ? g[(v0 + 1) * planes] : 0;
                u32 p10 = w10 ? g[(v0 + gw) * planes] : 0, p11 = w11 ? g[(v0 + gw + 1) * planes] : 0;
                wt[p] = (p00 * w00 + p01 * w01 + p10 * w10 + p11 * w11 + 8) >> 4;
            }
            u32 part = partitions > 1 ? select_partition(seed, x, y, partitions, small_block) : 0;
            u8 *o = out + (size_t)y * stride + x * 4;
            for (u32 c = 0; c < 4; c++) {
                u32 wc = dual && c == ccs ? wt[1] : wt[0];
                u32 c0 = (u32)ep[part][0][c], c1 = (u32)ep[part][1][c];
                if (srgb && c < 3) { c0 = c0 << 8 | 0x80; c1 = c1 << 8 | 0x80; }
                else { c0 = c0 << 8 | c0; c1 = c1 << 8 | c1; }
                o[c] = (u8)(((c0 * (64 - wc) + c1 * wc + 32) >> 6) >> 8);
            }
        }
}

/* Rows of blocks (bx x by of them, 16 bytes each, row after row) to a w x h RGBA8 image. */
void astc_decode(const u8 *blocks, u32 bw, u32 bh, u32 w, u32 h, int srgb, u8 *out) {
    u32 bx = (w + bw - 1) / bw, by = (h + bh - 1) / bh;
    for (u32 j = 0; j < by; j++)
        for (u32 i = 0; i < bx; i++) {
            u32 cw = w - i * bw < bw ? w - i * bw : bw, ch = h - j * bh < bh ? h - j * bh : bh;
            astc_decode_block(blocks + ((size_t)j * bx + i) * 16, bw, bh, srgb, out + ((size_t)j * bh * w + i * bw) * 4, w * 4, cw, ch);
        }
}
