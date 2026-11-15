/*
 * Kingmaker native engine v2: American checkers.
 *
 *  - 32-square bitboards, principal-variation alpha-beta
 *  - shared lock-free transposition table, Lazy SMP multi-threading
 *  - late move reductions, killer and history move ordering, capture extensions
 *  - draw rules: repetition and the 80-ply reversible-move rule, with a "draw value"
 *    so the engine can be told to seek draws (safe) or avoid them (aggressive)
 *  - style weights (balanced / safe / aggressive) and a risk-aware tie-break
 *  - runaway-man, king and structure evaluation
 *
 * Every km_analyze call owns its state, so searches can overlap safely.
 *
 * Build:  cc -O3 -shared -fPIC -pthread native/kingmaker.c -o libkingmaker.so
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#define KM_EXPORT __declspec(dllexport)
static double now_ms(void) { return (double)GetTickCount64(); }
typedef HANDLE kthread_t;
typedef struct { void (*fn)(void *); void *arg; } Tramp;
static DWORD WINAPI tramp_win(LPVOID p) { Tramp *t = (Tramp *)p; t->fn(t->arg); return 0; }
static int kt_start(kthread_t *h, Tramp *t) { *h = CreateThread(NULL, 0, tramp_win, t, 0, NULL); return *h != NULL; }
static void kt_join(kthread_t h) { WaitForSingleObject(h, INFINITE); CloseHandle(h); }
#else
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#define KM_EXPORT __attribute__((visibility("default")))
static double now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec / 1e6;
}
typedef pthread_t kthread_t;
typedef struct { void (*fn)(void *); void *arg; } Tramp;
static void *tramp_posix(void *p) { Tramp *t = (Tramp *)p; t->fn(t->arg); return NULL; }
static int kt_start(kthread_t *h, Tramp *t) { return pthread_create(h, NULL, tramp_posix, t) == 0; }
static void kt_join(kthread_t h) { pthread_join(h, NULL); }
#endif

#define POP(x) ((int)__builtin_popcount(x))
#define CTZ(x) ((int)__builtin_ctz(x))

#define MAXP 128
#define MS 96
#define WIN 10000
#define INF 32000
#define TT_BITS 22
#define HMAX 80          /* reversible plies without a capture or man move before a draw */
#define MAX_THREADS 16
#define MAX_ALTS 24

/* ------------------------------------------------------------------ tables */
static int8_t COL32[32];
static int8_t SD[32][2], SU[32][2], SDN[32], SUN[32];
static int8_t JD[32][2][2], JU[32][2][2], JDN[32], JUN[32];
static uint32_t CONE_DN[32], CONE_UP[32];
static uint64_t ZOB[32][4], ZSIDE;
static uint32_t CENTER, EDGE, MAINDIAG;
static int g_init = 0;

static int idx32(int r, int c) {
    if (r < 0 || r > 7 || c < 0 || c > 7 || ((r + c) & 1) == 0) return -1;
    return r * 4 + (c >> 1);
}
static uint64_t splitmix(uint64_t *s) {
    uint64_t z = (*s += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

KM_EXPORT void km_init(void) {
    if (g_init) return;
    for (int i = 0; i < 32; i++) COL32[i] = ((i >> 2) & 1) ? 2 * (i & 3) : 2 * (i & 3) + 1;
    for (int i = 0; i < 32; i++) {
        int r = i >> 2, c = COL32[i];
        SDN[i] = SUN[i] = JDN[i] = JUN[i] = 0;
        for (int k = 0; k < 2; k++) {
            int dc = k ? 1 : -1, t, m, l;
            if ((t = idx32(r + 1, c + dc)) >= 0) SD[i][SDN[i]++] = (int8_t)t;
            if ((t = idx32(r - 1, c + dc)) >= 0) SU[i][SUN[i]++] = (int8_t)t;
            m = idx32(r + 1, c + dc); l = idx32(r + 2, c + 2 * dc);
            if (m >= 0 && l >= 0) { JD[i][JDN[i]][0] = (int8_t)m; JD[i][JDN[i]][1] = (int8_t)l; JDN[i]++; }
            m = idx32(r - 1, c + dc); l = idx32(r - 2, c + 2 * dc);
            if (m >= 0 && l >= 0) { JU[i][JUN[i]][0] = (int8_t)m; JU[i][JUN[i]][1] = (int8_t)l; JUN[i]++; }
        }
        CONE_DN[i] = CONE_UP[i] = 0;
        for (int j = 0; j < 32; j++) {
            int dr = (j >> 2) - r, dc = COL32[j] - c;
            if (dc < 0) dc = -dc;
            if (dr > 0 && dc <= dr) CONE_DN[i] |= 1u << j;
            if (dr < 0 && dc <= -dr) CONE_UP[i] |= 1u << j;
        }
    }
    uint64_t seed = 0x5EED1234ULL;
    for (int i = 0; i < 32; i++) for (int t = 0; t < 4; t++) ZOB[i][t] = splitmix(&seed);
    ZSIDE = splitmix(&seed);
    {
        static const int c[] = { 10, 11, 14, 15, 18, 19, 22, 23 }, d[] = { 4, 8, 11, 15, 18, 22, 25, 29 };
        uint32_t e = 0, cm = 0, dm = 0;
        for (int i = 0; i < 32; i++) if (COL32[i] == 0 || COL32[i] == 7) e |= 1u << i;
        for (int i = 0; i < 8; i++) { cm |= 1u << (c[i] - 1); dm |= 1u << (d[i] - 1); }
        EDGE = e; CENTER = cm; MAINDIAG = dm;
    }
    g_init = 1;
}

/* ------------------------------------------------------------------ types */
typedef struct { uint32_t bm, bk, rm, rk; uint64_t h; int32_t hm; } Pos;
typedef struct { uint32_t caps; int8_t from, to, plen; int8_t path[16]; } Move;
typedef struct { uint64_t kx, d; } TTE;   /* kx = key ^ d, so torn writes from other threads are detected */

typedef struct {
    int32_t style;        /* 0 balanced, 1 safe, 2 aggressive */
    int32_t draw_value;   /* centipawns the engine's side gets for a draw (positive: likes draws) */
    int32_t threads;
    int32_t halfmove;     /* reversible plies already played before the root position */
    int32_t nhist;        /* positions in hist_boards (oldest first, ending with the one before the root) */
    int32_t want_alts;
    int32_t rules;        /* 0 American (captures compulsory), 1 optional captures */
    int32_t pad;
} Options;

typedef struct { int8_t path[16]; int8_t plen; int8_t ncaps; int8_t caps[12]; } OutMove;
#define NO_SCORE (-32768)
typedef struct { OutMove mv; int32_t score; int32_t lv[3]; } Alt;   /* lv: scores at depth 4, depth 8 and the deep level */
typedef struct {
    int32_t score, depth, nmoves, npv, nalts, pad;
    uint64_t nodes;
    OutMove best;
    OutMove pv[8];
    Alt alts[MAX_ALTS];
} Result;
typedef void (*ProgressFn)(int32_t depth, int32_t score, uint64_t nodes, const OutMove *best);

typedef struct {
    uint64_t nodes;
    double deadline;
    volatile int32_t *stop, *stop2;
    int aborted;
    TTE *tt;
    int32_t hist[1024];
    int16_t killers[MAXP][2];
    uint64_t path[MAXP + 160];
    int pbase;
    int rs, dv, advp, backp, opt;
    Move mv[MAXP][MS];
    int32_t sc[MAXP][MS];
} Search;

/* ------------------------------------------------------------------ move generation */
typedef struct { Move *out; int n; uint32_t occ, opp; int wp; } Gen;

static void rec(Gen *g, int origin, int to, uint32_t caps, const int8_t *path, int plen) {
    if (g->n >= MS) return;
    Move *m = &g->out[g->n++];
    m->from = (int8_t)origin; m->to = (int8_t)to; m->caps = caps; m->plen = (int8_t)plen;
    if (g->wp) memcpy(m->path, path, (size_t)plen);
}

static void jumps(Gen *g, int origin, int sq, int king, int side, uint32_t caps, int8_t *path, int plen) {
    int found = 0;
    for (int dir = 0; dir < 2; dir++) {
        if (!(king || (dir == 0 && side > 0) || (dir == 1 && side < 0))) continue;
        int cnt = dir ? JUN[sq] : JDN[sq];
        for (int k = 0; k < cnt; k++) {
            int mid = dir ? JU[sq][k][0] : JD[sq][k][0];
            int land = dir ? JU[sq][k][1] : JD[sq][k][1];
            if (!((g->opp >> mid) & 1) || ((caps >> mid) & 1)) continue;
            if (((g->occ >> land) & 1) && land != origin) continue;
            found = 1;
            uint32_t nc = caps | (1u << mid);
            path[plen] = (int8_t)land;
            int crowned = !king && (side > 0 ? land >= 28 : land <= 3);
            if (crowned) rec(g, origin, land, nc, path, plen + 1);
            else jumps(g, origin, land, king, side, nc, path, plen + 1);
        }
    }
    if (!found && caps) rec(g, origin, sq, caps, path, plen);
}

static int gen(Move *out, const Pos *p, int side, int wp, int opt, int *ncap) {
    Gen g;
    g.out = out; g.n = 0; g.wp = wp;
    g.occ = p->bm | p->bk | p->rm | p->rk;
    uint32_t men = side > 0 ? p->bm : p->rm, kings = side > 0 ? p->bk : p->rk;
    g.opp = side > 0 ? (p->rm | p->rk) : (p->bm | p->bk);
    int8_t path[16];
    for (uint32_t m = men; m; m &= m - 1) { int sq = CTZ(m); path[0] = (int8_t)sq; jumps(&g, sq, sq, 0, side, 0, path, 1); }
    for (uint32_t m = kings; m; m &= m - 1) { int sq = CTZ(m); path[0] = (int8_t)sq; jumps(&g, sq, sq, 1, side, 0, path, 1); }
    if (ncap) *ncap = g.n;
    if (g.n && !opt) return g.n;   /* American rules: a capture is compulsory. Optional rules: quiet moves are allowed too. */
    for (uint32_t m = men; m; m &= m - 1) {
        int sq = CTZ(m);
        int cnt = side > 0 ? SDN[sq] : SUN[sq];
        for (int k = 0; k < cnt; k++) {
            int t = side > 0 ? SD[sq][k] : SU[sq][k];
            if (!((g.occ >> t) & 1)) { path[0] = (int8_t)sq; path[1] = (int8_t)t; rec(&g, sq, t, 0, path, 2); }
        }
    }
    for (uint32_t m = kings; m; m &= m - 1) {
        int sq = CTZ(m);
        for (int d = 0; d < 2; d++) {
            int cnt = d ? SUN[sq] : SDN[sq];
            for (int k = 0; k < cnt; k++) {
                int t = d ? SU[sq][k] : SD[sq][k];
                if (!((g.occ >> t) & 1)) { path[0] = (int8_t)sq; path[1] = (int8_t)t; rec(&g, sq, t, 0, path, 2); }
            }
        }
    }
    return g.n;
}

static Pos make(const Pos *p, int side, const Move *m) {
    Pos q = *p;
    uint32_t fb = 1u << m->from, tb = 1u << m->to, c = m->caps;
    uint64_t h = q.h;
    int t0, t1, king;
    if (side > 0) {
        king = (q.bk & fb) != 0;
        t0 = king ? 1 : 0;
        for (uint32_t x = c; x; x &= x - 1) { int s = CTZ(x); h ^= ZOB[s][((q.rk >> s) & 1) ? 3 : 2]; }
        q.bm &= ~fb; q.bk &= ~fb; q.rm &= ~c; q.rk &= ~c;
        if (king || m->to >= 28) { q.bk |= tb; t1 = 1; } else { q.bm |= tb; t1 = 0; }
    } else {
        king = (q.rk & fb) != 0;
        t0 = king ? 3 : 2;
        for (uint32_t x = c; x; x &= x - 1) { int s = CTZ(x); h ^= ZOB[s][((q.bk >> s) & 1) ? 1 : 0]; }
        q.rm &= ~fb; q.rk &= ~fb; q.bm &= ~c; q.bk &= ~c;
        if (king || m->to <= 3) { q.rk |= tb; t1 = 3; } else { q.rm |= tb; t1 = 2; }
    }
    q.h = h ^ ZOB[m->from][t0] ^ ZOB[m->to][t1] ^ ZSIDE;
    q.hm = (c || !king) ? 0 : p->hm + 1;
    return q;
}

static uint64_t hash_pos(const Pos *p, int side) {
    uint64_t h = 0;
    const uint32_t b[4] = { p->bm, p->bk, p->rm, p->rk };
    for (int t = 0; t < 4; t++) for (uint32_t x = b[t]; x; x &= x - 1) h ^= ZOB[CTZ(x)][t];
    if (side < 0) h ^= ZSIDE;
    return h;
}

/* ------------------------------------------------------------------ evaluation */
static const int ADV[8] = { 0, 3, 7, 12, 18, 26, 38, 0 };

static int evaluate(const Pos *p, int side, int advp, int backp) {
    uint32_t bm = p->bm, bk = p->bk, rm = p->rm, rk = p->rk;
    int nbm = POP(bm), nbk = POP(bk), nrm = POP(rm), nrk = POP(rk);
    int sc = 100 * (nbm - nrm) + 155 * (nbk - nrk);
    int adv = 0;
    for (int r = 1; r < 7; r++) adv += ADV[r] * POP(bm & (0xFu << (4 * r))) - ADV[7 - r] * POP(rm & (0xFu << (4 * r)));
    sc += adv * advp / 100;
    sc += 6 * (POP(bm & CENTER) - POP(rm & CENTER)) + 10 * (POP(bk & CENTER) - POP(rk & CENTER));
    sc -= 4 * (POP(bm & EDGE) - POP(rm & EDGE)) + 8 * (POP(bk & EDGE) - POP(rk & EDGE));
    int back = 0;
    if (nrm > 0) back += 9 * POP(bm & 0x5u) + 3 * POP(bm & 0xAu);
    if (nbm > 0) back -= 9 * POP(rm & 0xA0000000u) + 3 * POP(rm & 0x50000000u);
    sc += back * backp / 100;
    /* runaway men: nothing of the enemy stands in front, and no enemy king can chase */
#ifdef KM_RUNAWAY   /* tested: the runaway-man term made the engine weaker, so it is off */
    if (nrk == 0) for (uint32_t m = bm; m; m &= m - 1) { int s = CTZ(m); if (!((rm) & CONE_DN[s])) sc += 10 + 8 * (s >> 2); }
    if (nbk == 0) for (uint32_t m = rm; m; m &= m - 1) { int s = CTZ(m); if (!((bm) & CONE_UP[s])) sc -= 10 + 8 * (7 - (s >> 2)); }
#endif
    uint32_t empty = ~(bm | bk | rm | rk);
    uint32_t bd = bm | bk, rd = rm | rk;
    int mb = POP(((bd << 4) | ((bd & 0xE0E0E0E0u) << 3) | ((bd & 0x07070707u) << 5)) & empty)
           + POP(((bk >> 4) | ((bk & 0x07070707u) >> 3) | ((bk & 0xE0E0E0E0u) >> 5)) & empty);
    int mr = POP(((rd >> 4) | ((rd & 0x07070707u) >> 3) | ((rd & 0xE0E0E0E0u) >> 5)) & empty)
           + POP(((rk << 4) | ((rk & 0xE0E0E0E0u) << 3) | ((rk & 0x07070707u) << 5)) & empty);
    sc += 2 * (mb - mr);
    int tot = nbm + nbk + nrm + nrk;
    int diff = (nbm - nrm) * 100 + (nbk - nrk) * 155;
    if (diff) {
        int lead = diff > 0 ? 1 : -1, ad = diff > 0 ? diff : -diff;
        sc += lead * (ad > 300 ? 300 : ad) * (24 - tot) / 60;
        if (tot <= 8 && ad >= 100 && (nbk + nrk) > 0) {
            uint32_t lm = lead > 0 ? (bm | bk) : (rm | rk), om = lead > 0 ? (rm | rk) : (bm | bk);
            int na = 0, nb = 0, ds = 0;
            for (uint32_t a = lm; a; a &= a - 1) {
                int sa = CTZ(a); na++;
                nb = 0;
                for (uint32_t b = om; b; b &= b - 1) {
                    int sb = CTZ(b); nb++;
                    int dr = (sa >> 2) - (sb >> 2), dc = COL32[sa] - COL32[sb];
                    if (dr < 0) dr = -dr;
                    if (dc < 0) dc = -dc;
                    ds += dr > dc ? dr : dc;
                }
            }
            if (na && nb) sc += lead * (48 * na * nb - 6 * ds) / (na * nb);
        }
        if (tot <= 7 && ad >= 100) { if (lead > 0) sc -= 10 * POP(rk & MAINDIAG); else sc += 10 * POP(bk & MAINDIAG); }
    }
    return side > 0 ? sc : -sc;
}

/* ------------------------------------------------------------------ transposition table */
#define TT_PACK(score, move, depth, flag) \
    ((uint64_t)(uint16_t)(score) | ((uint64_t)(uint16_t)(move) << 16) | ((uint64_t)(uint8_t)(depth) << 32) | ((uint64_t)(uint8_t)(flag) << 40))

/* ------------------------------------------------------------------ search */
static inline int draw_score(const Search *S, int side) { return side == S->rs ? S->dv : -S->dv; }

static int search(Search *S, int ply, const Pos *pos, int side, int depth, int alpha, int beta) {
    if ((++S->nodes & 2047) == 0 && (now_ms() > S->deadline || (S->stop && *S->stop) || (S->stop2 && *S->stop2))) S->aborted = 1;
    if (S->aborted) return 0;
    int cur = S->pbase + ply;
    S->path[cur] = pos->h;
    if (ply > 0) {
        if (pos->hm >= HMAX) return draw_score(S, side);
        if (pos->hm >= 4) {
            int lo = cur - pos->hm; if (lo < 0) lo = 0;
            for (int j = cur - 2; j >= lo; j -= 2) if (S->path[j] == pos->h) return draw_score(S, side);
        }
    }
    Move *mv = S->mv[ply];
    int32_t *sc = S->sc[ply];
    int ncap = 0;
    int n = gen(mv, pos, side, 0, S->opt, &ncap);
    if (n == 0) return -(WIN - ply);
    int capture = ncap > 0;
    if (ply >= MAXP - 3) return evaluate(pos, side, S->advp, S->backp);
    if (S->opt) {
        if (depth <= 0) {
            /* optional captures: stand pat, then look at captures only */
            int stand = evaluate(pos, side, S->advp, S->backp);
            if (!capture || ply >= MAXP - 12) return stand;
            if (stand >= beta) return stand;
            if (stand > alpha) alpha = stand;
            int qbest = stand;
            for (int i = 0; i < ncap; i++) {
                int bi = i;
                for (int j = i + 1; j < ncap; j++) if (POP(mv[j].caps) > POP(mv[bi].caps)) bi = j;
                if (bi != i) { Move t = mv[i]; mv[i] = mv[bi]; mv[bi] = t; }
                Pos child = make(pos, side, &mv[i]);
                int v = -search(S, ply + 1, &child, -side, 0, -beta, -alpha);
                if (S->aborted) return 0;
                if (v > qbest) { qbest = v; if (v > alpha) alpha = v; }
                if (alpha >= beta) break;
            }
            return qbest;
        }
    } else if (depth <= 0 && !capture) {
        return evaluate(pos, side, S->advp, S->backp);
    }

    uint64_t h = pos->h;
    TTE *b0 = &S->tt[(h & (((uint64_t)1 << TT_BITS) - 1)) & ~(uint64_t)1];
    int ttm = -1;
    if (depth > 0) {
        for (int w = 0; w < 2; w++) {
            uint64_t d = b0[w].d, kx = b0[w].kx;
            if ((kx ^ d) == h && ((d >> 32) & 0xFF) > 0) {
                ttm = (int16_t)((d >> 16) & 0xFFFF);
                int ed = (int)((d >> 32) & 0xFF), flag = (int)((d >> 40) & 0xFF), s = (int16_t)(d & 0xFFFF);
                if (ed >= depth) {
                    if (s > WIN - 200) s -= ply; else if (s < -WIN + 200) s += ply;
                    if (flag == 0 || (flag == 1 && s >= beta) || (flag == 2 && s <= alpha)) return s;
                }
                break;
            }
        }
    }
    int k0 = S->killers[ply][0], k1 = S->killers[ply][1];
    for (int i = 0; i < n; i++) {
        int code = mv[i].from * 32 + mv[i].to;
        if (code == ttm) sc[i] = 1000000000;
        else if (mv[i].caps) sc[i] = 1000000 + 1000 * POP(mv[i].caps);
        else if (code == k0) sc[i] = 900000;
        else if (code == k1) sc[i] = 800000;
        else sc[i] = S->hist[code];
    }
    int alpha0 = alpha, best = -INF, bestcode = -1;
    for (int i = 0; i < n; i++) {
        int bi = i;
        for (int j = i + 1; j < n; j++) if (sc[j] > sc[bi]) bi = j;
        if (bi != i) { Move t = mv[i]; mv[i] = mv[bi]; mv[bi] = t; int32_t ts = sc[i]; sc[i] = sc[bi]; sc[bi] = ts; }
        Move m = mv[i];
        Pos child = make(pos, side, &m);
        int nd = (n == 1 && m.caps) ? depth : depth - 1, v;
        if (i == 0) v = -search(S, ply + 1, &child, -side, nd, -beta, -alpha);
        else {
            int red = 0;
#ifndef KM_NO_LMR
            if (depth >= 4 && i >= 4 && !m.caps && !capture) red = (depth >= 7 && i >= 8) ? 2 : 1;
#endif
            v = -search(S, ply + 1, &child, -side, nd - red, -alpha - 1, -alpha);
            if (red && v > alpha && !S->aborted) v = -search(S, ply + 1, &child, -side, nd, -alpha - 1, -alpha);
            if (v > alpha && v < beta && !S->aborted) v = -search(S, ply + 1, &child, -side, nd, -beta, -alpha);
        }
        if (S->aborted) return 0;
        if (v > best) { best = v; bestcode = m.from * 32 + m.to; if (v > alpha) alpha = v; }
        if (alpha >= beta) {
            if (!m.caps) {
                int code = m.from * 32 + m.to;
                S->hist[code] += depth * depth;
                if (S->killers[ply][0] != code) { S->killers[ply][1] = S->killers[ply][0]; S->killers[ply][0] = (int16_t)code; }
            }
            break;
        }
    }
    if (depth > 0) {
        int flag = best <= alpha0 ? 2 : (best >= beta ? 1 : 0);
        int s = best;
        if (s > WIN - 200) s += ply; else if (s < -WIN + 200) s -= ply;
        int slot = -1, low = 0, lowd = 1000;
        for (int w = 0; w < 2; w++) {
            uint64_t od = b0[w].d;
            int odepth = (int)((od >> 32) & 0xFF);
            if ((b0[w].kx ^ od) == h && odepth > 0) { slot = w; break; }
            if (odepth < lowd) { lowd = odepth; low = w; }
        }
        int write = 1;
        if (slot >= 0) { if ((int)((b0[slot].d >> 32) & 0xFF) > depth) write = 0; }
        else slot = low;
        if (write) {
            uint64_t d = TT_PACK(s, flag == 2 ? -1 : bestcode, depth > 100 ? 100 : depth, flag);
            b0[slot].d = d; b0[slot].kx = h ^ d;
        }
    }
    return best;
}

static void to_out(const Move *m, OutMove *o) {
    memset(o, 0, sizeof *o);
    o->plen = m->plen;
    memcpy(o->path, m->path, (size_t)m->plen);
    int k = 0;
    for (uint32_t x = m->caps; x && k < 12; x &= x - 1) o->caps[k++] = (int8_t)CTZ(x);
    o->ncaps = (int8_t)k;
}

/* ------------------------------------------------------------------ iterative deepening (one per thread) */
typedef struct {
    Search *S;
    Pos root;
    int side, n, max_depth, start_depth, is_main;
    Move rm[MS];
    int best_score, done;
    ProgressFn cb;
} Job;

static void iterate(Job *J) {
    Search *S = J->S;
    for (int depth = J->start_depth; depth <= J->max_depth; depth++) {
        int alpha = -INF, ib = -1, is = -INF;
        for (int i = 0; i < J->n; i++) {
            Pos child = make(&J->root, J->side, &J->rm[i]);
            int nd = (J->n == 1 && J->rm[i].caps) ? depth : depth - 1, v;
            if (i == 0) v = -search(S, 1, &child, -J->side, nd, -INF, -alpha);
            else {
                v = -search(S, 1, &child, -J->side, nd, -alpha - 1, -alpha);
                if (v > alpha && !S->aborted) v = -search(S, 1, &child, -J->side, nd, -INF, -alpha);
            }
            if (S->aborted) break;
            if (v > is) { is = v; ib = i; }
            if (v > alpha) alpha = v;
        }
        if (S->aborted) break;
        if (ib > 0) { Move t = J->rm[ib]; memmove(&J->rm[1], &J->rm[0], (size_t)ib * sizeof(Move)); J->rm[0] = t; }
        J->best_score = is; J->done = depth;
        if (J->is_main && J->cb) { OutMove o; to_out(&J->rm[0], &o); J->cb(depth, is, S->nodes, &o); }
        if ((is > WIN - 150 || is < -WIN + 150) && depth >= 2) break;
        if (now_ms() > S->deadline) break;
        if (S->stop2 && *S->stop2) break;
    }
}

static void job_thread(void *arg) { iterate((Job *)arg); }

static void setup_search(Search *S, TTE *tt, volatile int32_t *stop, volatile int32_t *stop2, double deadline,
                         int rs, const Options *o, const Pos *rp, int side, const int8_t *hist_boards, int hist_n) {
    memset(S, 0, sizeof *S);
    S->tt = tt; S->stop = stop; S->stop2 = stop2; S->deadline = deadline;
    S->rs = rs;
    S->dv = o->draw_value;
    S->opt = o->rules ? 1 : 0;
    S->advp = o->style == 1 ? 60 : o->style == 2 ? 140 : 100;
    S->backp = o->style == 1 ? 170 : o->style == 2 ? 70 : 100;
    S->pbase = hist_n;
    for (int i = 0; i < hist_n; i++) {
        /* hist_boards[i] is the position with the side to move alternating, ending with -side */
        int hs = ((hist_n - i) & 1) ? -side : side;
        Pos hp;
        memset(&hp, 0, sizeof hp);
        for (int s = 0; s < 64; s++) {
            int v = hist_boards[i * 64 + s];
            if (!v) continue;
            int k = idx32(s >> 3, s & 7);
            if (k < 0) continue;
            uint32_t bit = 1u << k;
            if (v == 1) hp.bm |= bit; else if (v == 2) hp.bk |= bit; else if (v == -1) hp.rm |= bit; else hp.rk |= bit;
        }
        S->path[i] = hash_pos(&hp, hs);
    }
    S->path[hist_n] = rp->h;
}

static int analyze(const Pos *root, int side, int ms, int max_depth, volatile int32_t *stop, ProgressFn cb,
                   const Options *opt, const int8_t *hist_boards, Result *res) {
    memset(res, 0, sizeof *res);
    Move rm[MS];
    int opt_rules = opt->rules ? 1 : 0;
    int n = gen(rm, root, side, 1, opt_rules, NULL);
    res->nmoves = n;
    if (n == 0) { res->score = -WIN; return 0; }
    int hist_n = opt->nhist;
    if (hist_n < 0) hist_n = 0;
    if (hist_n > 150) { hist_boards += (size_t)(hist_n - 150) * 64; hist_n = 150; }
    int threads = opt->threads < 1 ? 1 : opt->threads > MAX_THREADS ? MAX_THREADS : opt->threads;
    if (n == 1) { threads = 1; if (ms > 400) ms = 400; }

    TTE *tt = (TTE *)calloc((size_t)1 << TT_BITS, sizeof(TTE));
    Job *jobs = (Job *)calloc((size_t)threads, sizeof(Job));
    Search **ss = (Search **)calloc((size_t)threads, sizeof(Search *));
    kthread_t *th = (kthread_t *)calloc((size_t)threads, sizeof(kthread_t));
    int *started = (int *)calloc((size_t)threads, sizeof(int));
    if (!tt || !jobs || !ss || !th || !started) { free(tt); free(jobs); free(ss); free(th); free(started); return -1; }
    volatile int32_t helpers_stop = 0;
    double t0 = now_ms(), deadline = t0 + ms;
    Pos rp = *root;
    rp.h = hash_pos(root, side);
    rp.hm = opt->halfmove < 0 ? 0 : opt->halfmove;
    int ok = 1;
    for (int t = 0; t < threads; t++) {
        ss[t] = (Search *)malloc(sizeof(Search));
        if (!ss[t]) { ok = 0; break; }
        setup_search(ss[t], tt, stop, t == 0 ? NULL : &helpers_stop, deadline, side, opt, &rp, side, hist_boards, hist_n);
        if (t == 0) ss[t]->stop2 = NULL;
        jobs[t].S = ss[t]; jobs[t].root = rp; jobs[t].side = side; jobs[t].n = n;
        jobs[t].max_depth = max_depth; jobs[t].start_depth = 1 + (t & 1) + (t >= 4 ? 1 : 0);
        jobs[t].is_main = (t == 0); jobs[t].cb = t == 0 ? cb : NULL;
        memcpy(jobs[t].rm, rm, sizeof rm);
        if (t > 0) {
            /* helpers search the root moves in a different order so they diverge from the main thread */
            for (int i = 0; i < n; i++) { Move tmp = jobs[t].rm[i]; int j = (i + t) % n; jobs[t].rm[i] = jobs[t].rm[j]; jobs[t].rm[j] = tmp; }
        }
    }
    if (!ok) {
        for (int t = 0; t < threads; t++) free(ss[t]);
        free(tt); free(jobs); free(ss); free(th); free(started);
        return -1;
    }
    Tramp *tr = (Tramp *)calloc((size_t)threads, sizeof(Tramp));
    for (int t = 1; t < threads; t++) {
        tr[t].fn = job_thread; tr[t].arg = &jobs[t];
        started[t] = kt_start(&th[t], &tr[t]);
    }
    iterate(&jobs[0]);
    helpers_stop = 1;
    for (int t = 1; t < threads; t++) if (started[t]) kt_join(th[t]);

    Job *J = &jobs[0];
    Search *S = J->S;
    uint64_t nodes = 0;
    for (int t = 0; t < threads; t++) nodes += ss[t]->nodes;
    int best_score = J->best_score, done = J->done;
    int depth_for_tests = done > 0 ? done : 1;

    /* style tie-break: among moves that are almost as good, pick the one that fits the style */
    if (opt->style != 0 && n > 1 && done >= 4) {
        S->aborted = 0; S->stop2 = NULL;
        S->deadline = now_ms() + (ms * 0.2 < 1500 ? ms * 0.2 : 1500);
        int thr = best_score - 10;
        int chosen = 0, chosen_metric = 0, have = 0;
        for (int i = 0; i < n && !S->aborted; i++) {
            int qualifies = (i == 0);
            if (!qualifies) {
                Pos child = make(&rp, side, &J->rm[i]);
                int nd = depth_for_tests - 1;
                int r = search(S, 1, &child, -side, nd, -thr, -thr + 1);
                qualifies = !S->aborted && r <= -thr;
            }
            if (!qualifies) continue;
            Pos child = make(&rp, side, &J->rm[i]);
            int metric;
            if (opt->style == 1) {
                Move om[MS];
                int cc = 0;
                int cn = gen(om, &child, -side, 0, opt_rules, &cc);
                (void)cn;
                metric = -cc * 10;                                       /* fewer forcing replies is safer */
            } else {
                metric = -evaluate(&child, -side, 100, 100);             /* better static position for us */
            }
            if (!have || metric > chosen_metric) { chosen = i; chosen_metric = metric; have = 1; }
        }
        if (have && chosen > 0 && !S->aborted) {
            Move t = J->rm[chosen];
            memmove(&J->rm[1], &J->rm[0], (size_t)chosen * sizeof(Move));
            J->rm[0] = t;
        }
    }

    res->score = best_score; res->depth = done; res->nodes = nodes;
    to_out(&J->rm[0], &res->best);
    res->npv = 1; to_out(&J->rm[0], &res->pv[0]);
    {
        Pos cur = make(&rp, side, &J->rm[0]);
        int cs = -side;
        for (int k = 1; k < 8; k++) {
            TTE *pb = &tt[(cur.h & (((uint64_t)1 << TT_BITS) - 1)) & ~(uint64_t)1];
            uint64_t d = 0; int hit = 0;
            for (int w = 0; w < 2; w++) {
                uint64_t dd = pb[w].d;
                if ((pb[w].kx ^ dd) == cur.h && ((dd >> 32) & 0xFF) > 0) { d = dd; hit = 1; break; }
            }
            if (!(hit && (int16_t)((d >> 16) & 0xFFFF) >= 0)) break;
            int code = (int16_t)((d >> 16) & 0xFFFF);
            Move ml[MS];
            int c = gen(ml, &cur, cs, 1, opt_rules, NULL), f = -1;
            for (int i = 0; i < c; i++) if (ml[i].from * 32 + ml[i].to == code) { f = i; break; }
            if (f < 0) break;
            to_out(&ml[f], &res->pv[res->npv++]);
            cur = make(&cur, cs, &ml[f]);
            cs = -cs;
        }
    }

    /* scores for every legal move at three depths (4, 8 and a deep level), so the app can model how a
       human opponent of unknown strength chooses, and so you can compare options */
    if (opt->want_alts && n > 1 && done >= 3) {
        S->aborted = 0; S->stop2 = NULL;
        S->deadline = now_ms() + (ms * 0.15 < 1200 ? ms * 0.15 : 1200);
        int deep = done - 3 < 2 ? 2 : done - 3;
        int levels[3] = { deep < 4 ? deep : 4, deep < 8 ? deep : 8, deep };
        int cnt = n < MAX_ALTS ? n : MAX_ALTS;
        for (int i = 0; i < cnt; i++) {
            to_out(&J->rm[i], &res->alts[i].mv);
            res->alts[i].score = NO_SCORE;
            for (int l = 0; l < 3; l++) res->alts[i].lv[l] = NO_SCORE;
        }
        for (int l = 0; l < 3 && !S->aborted; l++) {
            for (int i = 0; i < cnt; i++) {
                Pos child = make(&rp, side, &J->rm[i]);
                int v = -search(S, 1, &child, -side, levels[l] - 1, -INF, INF);
                if (S->aborted) break;
                res->alts[i].lv[l] = v;
                res->alts[i].score = v;
            }
        }
        res->nalts = cnt;
    }

    for (int t = 0; t < threads; t++) free(ss[t]);
    free(tr); free(tt); free(jobs); free(ss); free(th); free(started);
    return 0;
}

/* ------------------------------------------------------------------ exported API */
static Pos from_board(const int8_t *b) {
    Pos p;
    memset(&p, 0, sizeof p);
    for (int s = 0; s < 64; s++) {
        int v = b[s];
        if (!v) continue;
        int i = idx32(s >> 3, s & 7);
        if (i < 0) continue;
        uint32_t bit = 1u << i;
        if (v == 1) p.bm |= bit; else if (v == 2) p.bk |= bit; else if (v == -1) p.rm |= bit; else p.rk |= bit;
    }
    return p;
}

/* board64: 64 signed bytes, 1/2 black man/king, -1/-2 red man/king. side: 1 black, -1 red. */
KM_EXPORT int km_analyze(const int8_t *board64, int side, int ms, int max_depth, volatile int32_t *stop,
                         ProgressFn cb, const Options *opt, const int8_t *hist_boards, Result *out) {
    km_init();
    Pos p = from_board(board64);
    Options o;
    if (opt) o = *opt; else memset(&o, 0, sizeof o);
    if (!hist_boards) o.nhist = 0;
    return analyze(&p, side, ms, max_depth, stop, cb, &o, hist_boards, out);
}

static uint64_t perft(Move (*buf)[MS], int ply, const Pos *p, int side, int d) {
    int n = gen(buf[ply], p, side, 0, 0, NULL);
    if (d == 1) return (uint64_t)n;
    uint64_t t = 0;
    for (int i = 0; i < n; i++) { Pos q = make(p, side, &buf[ply][i]); t += perft(buf, ply + 1, &q, -side, d - 1); }
    return t;
}
KM_EXPORT uint64_t km_perft(const int8_t *board64, int side, int depth) {
    km_init();
    Pos p = from_board(board64);
    Move (*buf)[MS] = (Move (*)[MS])malloc(sizeof(Move) * MS * 40);
    if (!buf) return 0;
    uint64_t r = perft(buf, 0, &p, side, depth);
    free(buf);
    return r;
}
KM_EXPORT int km_abi(void) { return (int)(sizeof(Result) * 1000 + sizeof(OutMove) + sizeof(Options)); }
KM_EXPORT int km_hardware_threads(void) {
#ifdef _WIN32
    SYSTEM_INFO si; GetSystemInfo(&si); return (int)si.dwNumberOfProcessors;
#else
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n < 1 ? 1 : (int)n;
#endif
}
