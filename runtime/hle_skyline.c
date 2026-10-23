/* Skyline plugins (docs/SKYLINE.md): the code GameBanana's moveset and new-character mods ship
 * (plugin.nro, Switch code built with skyline-rs), recompiled like the game's own NROs
 * (tools/recomp.py) and run here as Skyline runs them on the console.
 *
 *   loading  once the game mounts its RomFS (nn::fs::MountRom, as Skyline does), each module
 *            named in <data dir>/../skyline/plugins.txt is loaded (rt_load_nro_named) and its
 *            `main` run
 *   Skyline  the C API plugins import from Skyline itself: A64HookFunction (through the
 *            dispatch tables, rt_hook_function), getRegionAddress, sky_memcpy, the logger,
 *            get_program_id, get_plugin_addresses
 *   nn::ro   LookupSymbol answers from the runtime's symbols, which have the native plugins'
 *            names too: that is how plugins find Smashline
 *   Smashline 2, natively: the scripts plugins install (smashline_install_*), put into each
 *            fighter's agents as the game creates them (the create_agent functions, hooked)
 *
 * SSBU_SKYLINE_LOG=0 quiets the log of each call. */
#define _CRT_SECURE_NO_WARNINGS
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "host.h"

u64 rt_wrap_symbol(const char *name, GuestFn *fn);   /* (host.c) */
void fs_mount_sd(void);                              /* (hle_fs.c) */

#define MAIN_BASE 0x7100000000ull
#define PROGRAM_ID 0x01006A800016E000ull
#define R_NOT_FOUND RESULT(22, 1100)

static int quiet;
#define LOG(...) do { if (!quiet) { fprintf(stderr, "[skyline] " __VA_ARGS__); fputc('\n', stderr); } } while (0)

static const char *guest_str(u64 a) { return a ? P(const char, a) : "(null)"; }

/* ---- what the console's Skyline provides ------------------------------------------- */
/* (region: 0 text, 1 rodata, 2 data, 3 bss, 4 heap) - main's segments, from its MOD0 */
HLE(getRegionAddress) {
    u32 region = (u32)c->x[0] & 0xFF;
    u64 mod0 = MAIN_BASE + *P(u32, MAIN_BASE + 4);
    u64 addr = 0;
    if (region == 0) addr = MAIN_BASE;
    else if (region == 3) addr = mod0 + *P(i32, mod0 + 8);
    else LOG("getRegionAddress(%u): not known here, 0", region);
    c->x[0] = addr;
}

/* Every hook target, "<module> <offset>" a line in <data dir>/../skyline/hooks.txt: the next build
 * makes the ones the game also calls directly reach their hooks too (tools/recomp.py hookable). */
static void note_hook_target(u64 target) {
    char where[96], line[128], path[1200];
    rt_describe(target, where, sizeof where);
    char *plus = strchr(where, '+');
    if (!plus || strncmp(plus, "+0x", 3)) return;
    *plus = 0;
    snprintf(line, sizeof line, "%s %s\n", where, plus + 3);
    snprintf(path, sizeof path, "%s/../skyline/hooks.txt", rt_data_dir);
    FILE *f = fopen(path, "r");
    char have[128];
    while (f && fgets(have, sizeof have, f))
        if (!strcmp(have, line)) { fclose(f); return; }
    if (f) fclose(f);
    if ((f = fopen(path, "a"))) { fputs(line, f); fclose(f); }
}

/* A64HookFunction(symbol, replace, result): calls to `symbol` go to `replace`; *result reaches
 * the function as it was. */
HLE(A64HookFunction) {
    u64 target = c->x[0], replace = c->x[1], result = c->x[2];
    char a[96], b[96];
    note_hook_target(target);
    u64 original = rt_hook_function(target, replace);
    LOG("A64HookFunction %s -> %s%s", rt_describe(target, a, sizeof a), rt_describe(replace, b, sizeof b),
        original ? "" : ": NOT a function entry, left alone");
    if (result) *P(u64, result) = original ? original : target;
}

/* A64InlineHook(address, callback): a callback at one instruction, with the registers - code
 * a recompiled function does not have, so not here (it would be a code patch at build time). */
HLE(A64InlineHook) {
    char a[96];
    LOG("A64InlineHook at %s: inline hooks are not supported yet, left out", rt_describe(c->x[0], a, sizeof a));
}

/* sky_memcpy(dest, src, n): plugins patch code and read-only data with it. Data changes work;
 * a change to code means nothing to the recompiled function. */
HLE(sky_memcpy) {
    char a[96];
    LOG("sky_memcpy %#llx bytes to %s", (unsigned long long)c->x[2], rt_describe(c->x[0], a, sizeof a));
    memcpy(G2H(c->x[0]), G2H(c->x[1]), c->x[2]);
    c->x[0] = 0;
}

HLE(skyline_tcp_send_raw) {   /* (data, size): the plugin's println! */
    fwrite(G2H(c->x[0]), 1, c->x[1], stderr);
}

HLE(get_program_id) { c->x[0] = PROGRAM_ID; }

/* get_plugin_addresses(addr, *start, *end): the plugin's module around an address */
HLE(get_plugin_addresses) {
    char a[96];
    LOG("get_plugin_addresses(%s): not known here", rt_describe(c->x[0], a, sizeof a));
    if (c->x[1]) *P(u64, c->x[1]) = 0;
    if (c->x[2]) *P(u64, c->x[2]) = 0;
}

/* nn::ro::LookupSymbol(*address, name) */
static void lookup_symbol(Cpu *c) {
    const char *name = guest_str(c->x[1]);
    u64 addr = rt_resolve_weak(name);
    LOG("LookupSymbol %s: %#llx", name, (unsigned long long)addr);
    if (!addr) { c->x[0] = R_NOT_FOUND; return; }
    *P(u64, c->x[0]) = addr;
    c->x[0] = 0;
}

/* Plugins check that the plugins they need are installed (rom:/skyline/plugins/<lib>.nro); the
 * ones the runtime provides natively are. (hle_fs.c asks for a RomFS path with no "rom:/".) */
int skyline_provides(const char *path) {
    static const char *const natives[] = {"skyline/plugins/libsmashline_plugin.nro", "skyline/plugins/libnro_hook.nro",
                                          "skyline/plugins/libarcropolis.nro",
                                          /* (linked into each plugin that uses it) */
                                          "skyline/plugins/libparam_config.nro",
                                          /* (not yet: plugins check for it, Sonic Re-Imagined without using it) */
                                          "skyline/plugins/libthe_csk_collection.nro", 0};
    while (*path == '/') path++;
    for (int i = 0; natives[i]; i++)
        if (!strcmp(path, natives[i])) return 1;
    return 0;
}

/* ---- Smashline 2 ------------------------------------------------------------------- */
/* Hash40: the CRC-32 of the name, its length in bits 32-39 */
static u64 hash40(const char *s, size_t n) {
    u32 crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) {
        crc ^= (u8)s[i];
        for (int k = 0; k < 8; k++) crc = crc >> 1 ^ (0xEDB88320u & (0u - (crc & 1)));
    }
    return (u64)~crc | (u64)(n & 0xFF) << 32;
}

/* A Smashline costume list ({const usize *data; usize len}), copied: the slots a script is for. */
typedef struct { u32 n; u32 *slots; } Costume;
typedef struct { u64 agent, script, function; int category; Costume costume; } AcmdScript;
typedef struct { u64 agent, function; int status, line; Costume costume; } StatusScript;
typedef struct { u64 agent, function; int line; u32 ran; Costume costume; } LineCallback;
static AcmdScript *acmd;
static StatusScript *statuses;
static LineCallback *lines;
static u32 acmd_n, status_n, line_n, acmd_cap, status_cap, line_cap;
static SRWLOCK scripts_lock = SRWLOCK_INIT;
static const char *const CATEGORY[] = {"game", "effect", "sound", "expression"};
enum { LINE_MAIN = 1, SET_STATUS_SCRIPTS = 9 };   /* (smashline StatusLine; L2CFighterBase's virtual function) */
static u64 FIGHTER_HASH, WEAPON_HASH;             /* Hash40("fighter"), ("weapon"): scripts for every one */

#define PUSH(arr, n, cap, ...) do { \
    AcquireSRWLockExclusive(&scripts_lock); \
    if (n == cap) arr = realloc(arr, sizeof *arr * (cap = cap * 2 + 64)); \
    arr[n++] = (__VA_ARGS__); \
    ReleaseSRWLockExclusive(&scripts_lock); \
} while (0)

static Costume costume_of(u64 data, u64 len) {
    Costume k = {0, NULL};
    if (!data || !len || len > 4096) return k;
    k.slots = malloc(sizeof *k.slots * len);
    for (u64 i = 0; i < len; i++) k.slots[i] = (u32)*P(u64, data + 8 * i);
    k.n = (u32)len;
    return k;
}
static int has_slot(Costume k, int slot) {
    for (u32 i = 0; i < k.n; i++) if ((int)k.slots[i] == slot) return 1;
    return 0;
}
/* Smashline's rule: a slot some plugin claimed for this agent (a "new character" on extra
 * costumes) gets only the scripts for that slot; any other slot gets the scripts with no list. */
static int claimed(u64 agent, int slot) {
    for (u32 i = 0; i < acmd_n; i++) if (acmd[i].agent == agent && has_slot(acmd[i].costume, slot)) return 1;
    for (u32 i = 0; i < status_n; i++) if (statuses[i].agent == agent && has_slot(statuses[i].costume, slot)) return 1;
    for (u32 i = 0; i < line_n; i++) if (lines[i].agent == agent && has_slot(lines[i].costume, slot)) return 1;
    return 0;
}
static int for_slot(Costume k, int is_claimed, int slot) { return is_claimed ? has_slot(k, slot) : !k.n; }

static const char *slots_text(Costume k) {
    static __declspec(thread) char text[48];
    if (!k.n) return "";
    if (k.n == 1) snprintf(text, sizeof text, " (costume c%02u)", k.slots[0]);
    else snprintf(text, sizeof text, " (%u costumes, c%02u...c%02u)", k.n, k.slots[0], k.slots[k.n - 1]);
    return text;
}

static void add_acmd(u64 agent, Costume k, u64 script, int category, u64 function) {
    PUSH(acmd, acmd_n, acmd_cap, (AcmdScript){agent, script, function, category, k});
    LOG("acmd %#llx %s script %#llx%s -> %#llx", (unsigned long long)agent, CATEGORY[category & 3], (unsigned long long)script,
        slots_text(k), (unsigned long long)function);
}
static void add_status(u64 agent, Costume k, int status, int line, u64 function) {
    if (!agent) agent = hash40("common", 6);
    PUSH(statuses, status_n, status_cap, (StatusScript){agent, function, status, line, k});
    LOG("status %#llx kind %d line %d%s -> %#llx", (unsigned long long)agent, status, line, slots_text(k), (unsigned long long)function);
}
static void add_line(u64 agent, Costume k, int line, u64 function) {
    PUSH(lines, line_n, line_cap, (LineCallback){agent, function, line, 0, k});
    LOG("line callback %#llx line %d%s -> %#llx%s", (unsigned long long)agent, line, slots_text(k), (unsigned long long)function,
        line == LINE_MAIN ? "" : " (only the Main line's are run yet)");
}

/* (agent, script, category, priority, function) and (agent, costume, script, category, priority, function) */
HLE(smashline_install_acmd_script) { add_acmd(c->x[0], (Costume){0}, c->x[1], (int)c->x[2], c->x[4]); }
HLE(smashline_install_acmd_script_costume) { add_acmd(c->x[0], costume_of(c->x[1], c->x[2]), c->x[3], (int)c->x[4], c->x[6]); }
/* (agent or 0: common, status, line, function) */
HLE(smashline_install_status_script) { add_status(c->x[0], (Costume){0}, (int)c->x[1], (int)c->x[2], c->x[3]); }
HLE(smashline_install_status_script_costume) { add_status(c->x[0], costume_of(c->x[1], c->x[2]), (int)c->x[3], (int)c->x[4], c->x[5]); }
/* (agent or 0, line, function) */
HLE(smashline_install_line_callback) { add_line(c->x[0], (Costume){0}, (int)c->x[1], c->x[2]); }
HLE(smashline_install_line_callback_costume) { add_line(c->x[0], costume_of(c->x[1], c->x[2]), (int)c->x[3], c->x[4]); }
/* (agent or 0, event, function): object events need hooks inside main's object code - not yet */
HLE(smashline_install_state_callback) {
    LOG("state callback %#llx event %d -> %#llx: NOT SUPPORTED yet, left out", (unsigned long long)c->x[0], (int)c->x[1],
        (unsigned long long)c->x[2]);
}
HLE(smashline_install_state_callback_costume) {
    LOG("state callback %#llx event %d (costumes) -> %#llx: NOT SUPPORTED yet, left out", (unsigned long long)c->x[0], (int)c->x[3],
        (unsigned long long)c->x[4]);
}

/* What the game had before a plugin's script went in: (agent, script hash or status/line) ->
 * function, for smashline_get_original_acmd and _status. Agents come and go at the same
 * addresses: an entry is overwritten when its agent is made again. */
#define ORIGINALS 65536
static struct { u64 agent, key, function; } originals[ORIGINALS];
static SRWLOCK originals_lock = SRWLOCK_INIT;
static u32 original_slot(u64 agent, u64 key) { return (u32)((agent * 0x9E3779B97F4A7C15ull ^ key * 0xC2B2AE3D27D4EB4Full) >> 48); }
static void remember(u64 agent, u64 key, u64 function) {
    AcquireSRWLockExclusive(&originals_lock);
    u32 i = original_slot(agent, key);
    for (u32 k = 0; k < 64; k++, i = (i + 1) & (ORIGINALS - 1))
        if (!originals[i].agent || (originals[i].agent == agent && originals[i].key == key)) {
            originals[i].agent = agent; originals[i].key = key; originals[i].function = function;
            break;
        }
    ReleaseSRWLockExclusive(&originals_lock);
}
static u64 recall(u64 agent, u64 key) {
    u64 f = 0;
    AcquireSRWLockShared(&originals_lock);
    u32 i = original_slot(agent, key);
    for (u32 k = 0; k < 64 && originals[i].agent; k++, i = (i + 1) & (ORIGINALS - 1))
        if (originals[i].agent == agent && originals[i].key == key) { f = originals[i].function; break; }
    ReleaseSRWLockShared(&originals_lock);
    return f;
}
#define STATUS_KEY(line, status) ((u64)(u32)(line) << 32 | (u32)(status) | 1ull << 63)
HLE(smashline_get_original_acmd) { c->x[0] = recall(c->x[0], c->x[1]); }                       /* (agent, hash) */
HLE(smashline_get_original_status) { c->x[0] = recall(c->x[0], STATUS_KEY(c->x[1], c->x[2])); }   /* (agent, line, kind) */
/* (module name {ptr, len}, offset): an address in another module's code */
HLE(smashline_get_target_function) {
    char name[96] = "";
    u64 len = c->x[1] < sizeof name ? c->x[1] : sizeof name - 1;
    if (c->x[0]) memcpy(name, P(char, c->x[0]), len);
    name[len] = 0;
    u64 base = strcmp(name, "main") ? rt_load_nro_named(c, name) : MAIN_BASE;
    LOG("get_target_function %s+%#llx: %s", name, (unsigned long long)c->x[2], base ? "found" : "NOT loaded");
    c->x[0] = base ? base + c->x[2] : 0;
}

/* ---- which agent: name, costume slot ---- */
#define FIGHTER_NAMES (MAIN_BASE + 0x4f81e20)       /* const char *[118]: fighter names, by kind */
#define FIGHTER_COUNT 118
#define WEAPON_NAMES (MAIN_BASE + 0x5186bd0)        /* const char *[0x267]: weapon names, by kind */
#define WEAPON_OWNER_NAMES (MAIN_BASE + 0x5189240)  /* ... and their owners' */
#define WEAPON_COUNT 0x267
#define ENTRY_TABLE (MAIN_BASE + 0x5324680)         /* (smashline utils.rs: an entry's costume) */

static const char *guest_name(u64 table, int i, int count) {
    if (i < 0 || i >= count) return NULL;
    u64 ptr = *P(u64, table + 8ull * i);
    return ptr >= MAIN_BASE && ptr < MAIN_BASE + 0x8000000 ? P(const char, ptr) : NULL;
}

typedef struct { u64 hash; int slot, weapon; char name[64]; } Agent;

/* The agent a battle object is: a fighter ("sonic") or a weapon ("sonic_gimmickjump"), and the
 * costume slot of the player it belongs to (-1: none). 0 hash: neither. */
static Agent agent_of(u64 object, int kind) {
    Agent a = {0, -1, 0, ""};
    if (!object) return a;
    u32 category = *P(u32, object + 8) >> 28;
    if (kind < 0) kind = *P(i32, object + 0xC);
    if (category == 0) {
        const char *n = guest_name(FIGHTER_NAMES, kind, FIGHTER_COUNT);
        if (n) snprintf(a.name, sizeof a.name, "%s", n);
    } else if (category == 1) {
        const char *o = guest_name(WEAPON_OWNER_NAMES, kind, WEAPON_COUNT), *n = guest_name(WEAPON_NAMES, kind, WEAPON_COUNT);
        if (o && n) snprintf(a.name, sizeof a.name, "%s_%s", o, n);
        a.weapon = 1;
    }
    if (!*a.name) return a;
    a.hash = hash40(a.name, strlen(a.name));
    int entry = *P(i32, object + 0x10);
    u64 vec = *P(u64, ENTRY_TABLE);
    if (entry >= 0 && entry <= 7 && vec) {
        u64 info = *P(u64, vec + 8ull * entry + 0xE8);
        if (info) a.slot = *P(i32, info + 100);
    }
    return a;
}

/* ---- ACMD: the agent factories ---- */
/* The game's agent factories (main, 13.0.5; smashline's create_agent.rs): (object, module
 * accessor, lua state) -> agent, one per script category; the "share" ones, for fighters that
 * share another's code, take the fighter kind first. */
static const struct { u64 offset; int category, share; } FACTORIES[] = {
    {0x64c310, 0, 0}, {0x64c930, 1, 0}, {0x64cf50, 3, 0}, {0x64d570, 2, 0},
    {0x64db90, 0, 1}, {0x64e2a0, 1, 1}, {0x64e8b0, 3, 1}, {0x64eec0, 2, 1},
    {0x33acde0 + 0x5B0, 0, 0}, {0x33add40 + 0x5B0, 1, 0}, {0x33aeca0 + 0x5B0, 2, 0},   /* weapons */
};
#define NFACTORIES (sizeof FACTORIES / sizeof *FACTORIES)
static u64 factory_original[NFACTORIES];

/* lib::L2CAgent's function_map (a libc++ unordered_map<Hash40, fn> at +0x10): each node
 * {next, hash, key, function}, the list from +0x20 */
static u64 agent_function(u64 agent, u64 script) {
    u64 node = *P(u64, agent + 0x20);
    for (int i = 0; node && i < 100000; i++, node = *P(u64, node))
        if (*P(u64, node + 16) == script) return *P(u64, node + 24);
    return 0;
}

static void created(Cpu *c, int which) {
    int category = FACTORIES[which].category, share = FACTORIES[which].share;
    u64 object = c->x[share ? 1 : 0];
    int kind = share ? (int)c->x[0] : -1;
    rt_call(c, factory_original[which]);
    u64 agent = c->x[0];
    Agent a = agent && object ? agent_of(object, kind) : (Agent){0};
    if (!a.hash) return;
    u32 put = 0;
    AcquireSRWLockShared(&scripts_lock);
    u32 n = acmd_n;
    int is_claimed = claimed(a.hash, a.slot);
    ReleaseSRWLockShared(&scripts_lock);
    for (u32 i = 0; i < n; i++)
        if (acmd[i].agent == a.hash && acmd[i].category == category && for_slot(acmd[i].costume, is_claimed, a.slot)) {
            remember(agent, acmd[i].script, agent_function(agent, acmd[i].script));
            rt_callsym(c, "_ZN3lib8L2CAgent20sv_set_function_hashEPvN3phx6Hash40E", 3, agent, acmd[i].function, acmd[i].script);
            put++;
        }
    if (put) LOG("%s (c%02d) %s agent %#llx: %u acmd scripts from plugins", a.name, a.slot, CATEGORY[category], (unsigned long long)agent, put);
    c->x[0] = agent;
}
#define FACTORY(k) static void factory_##k(Cpu *c) { created(c, k); }
FACTORY(0) FACTORY(1) FACTORY(2) FACTORY(3) FACTORY(4) FACTORY(5) FACTORY(6) FACTORY(7) FACTORY(8) FACTORY(9) FACTORY(10)
static GuestFn *const factory_hooks[NFACTORIES] = {factory_0, factory_1, factory_2, factory_3, factory_4, factory_5,
                                                   factory_6, factory_7, factory_8, factory_9, factory_10};

/* ---- status scripts ---- */
/* The status agent (L2CFighterBase, or L2CWeaponCommon) has its status functions put in by its
 * virtual set_status_scripts; the agents the factories make get a copy of their class's vtable
 * whose set_status_scripts runs the original and then puts in the plugins' (as smashline does),
 * with sv_set_status_func(status, line, function); new status kinds raise the agent's status
 * count (global table [0xC]). */
static const u64 STATUS_FACTORIES[] = {0x64bbd0, 0x33abee0 + 0x5B0};   /* fighters, weapons */
static u64 status_factory_original[2];
static struct { u64 original, copy; } vtables[256];
static u32 vtable_n;
static SRWLOCK vtable_lock = SRWLOCK_INIT;

/* A lib::L2CValue holding an integer, on the guest stack at `at` */
static void l2c_int(Cpu *c, u64 at, int value) { rt_callsym(c, "_ZN3lib8L2CValueC1Ei", 2, at, (u64)(u32)value); }

static void set_status_scripts(Cpu *c) {
    u64 agent = c->x[0], vtable = *P(u64, agent), original = 0;
    AcquireSRWLockShared(&vtable_lock);
    for (u32 i = 0; i < vtable_n; i++)
        if (vtables[i].copy == vtable) original = *P(u64, vtables[i].original + 8 * SET_STATUS_SCRIPTS);
    ReleaseSRWLockShared(&vtable_lock);
    if (original) rt_call(c, original);
    Agent a = agent_of(*P(u64, agent + 0x38), -1);
    if (!a.hash) return;
    u64 common = a.weapon ? WEAPON_HASH : FIGHTER_HASH;
    AcquireSRWLockShared(&scripts_lock);
    u32 n = status_n;
    int is_claimed = claimed(a.hash, a.slot);
    ReleaseSRWLockShared(&scripts_lock);
    u32 put = 0;
    int most = -1;
    u64 sp = c->sp;
    c->sp = (sp - 0x40) & ~15ull;
    u64 id = c->sp, line = c->sp + 0x10, value = c->sp + 0x20;
    for (int pass = 0; pass < 2; pass++)   /* (every agent's first, then this one's: these win) */
        for (u32 i = 0; i < n; i++) {
            StatusScript s = statuses[i];
            if (s.agent != (pass ? a.hash : common) || s.line < 0 || !for_slot(s.costume, is_claimed, a.slot)) continue;
            l2c_int(c, id, s.status);
            l2c_int(c, line, s.line);
            c->x[8] = value;   /* (the returned L2CValue) */
            rt_callsym(c, "_ZN7lua2cpp12L2CAgentBase18sv_get_status_funcERKN3lib8L2CValueES4_", 3, agent, id, line);
            remember(agent, STATUS_KEY(s.line, s.status), *P(u32, value) ? *P(u64, value + 8) : 0);
            rt_callsym(c, "_ZN3lib8L2CValueD1Ev", 1, value);
            rt_callsym(c, "_ZN7lua2cpp12L2CAgentBase18sv_set_status_funcERKN3lib8L2CValueES4_Pv", 4, agent, id, line, s.function);
            rt_callsym(c, "_ZN3lib8L2CValueD1Ev", 1, line);
            rt_callsym(c, "_ZN3lib8L2CValueD1Ev", 1, id);
            if (s.status > most) most = s.status;
            put++;
        }
    if (most >= 0) {   /* the status count: L2CFighterBase's global_table (after the 0xC8-byte L2CAgentBase), entry 0xC */
        u64 entry = rt_callsym(c, "_ZNK3lib8L2CValueixEi", 2, agent + 0xC8, 0xCull);
        if (entry && (*P(u32, entry) == 2 || *P(u32, entry) == 0) && *P(i64, entry + 8) < most + 1) {
            LOG("%s: status count %lld -> %d (new status kinds)", a.name, (long long)*P(i64, entry + 8), most + 1);
            *P(u32, entry) = 2;
            *P(i64, entry + 8) = most + 1;
        }
    }
    c->sp = sp;
    if (put) LOG("%s (c%02d) status agent %#llx: %u status scripts from plugins", a.name, a.slot, (unsigned long long)agent, put);
}

static void status_created(Cpu *c, int which) {
    rt_call(c, status_factory_original[which]);
    u64 agent = c->x[0];
    if (!agent || !status_n) return;
    u64 vtable = *P(u64, agent), copy = 0;
    AcquireSRWLockExclusive(&vtable_lock);
    for (u32 i = 0; i < vtable_n && !copy; i++)
        if (vtables[i].original == vtable || vtables[i].copy == vtable) copy = vtables[i].copy;
    if (!copy && vtable_n < sizeof vtables / sizeof *vtables) {
        u64 table[64];
        memcpy(table, P(void, vtable), sizeof table);   /* (more than the class has: never read past its own) */
        table[SET_STATUS_SCRIPTS] = rt_native(set_status_scripts, "smashline set_status_scripts");
        copy = rt_guest_copy(table, sizeof table);
        vtables[vtable_n].original = vtable;
        vtables[vtable_n++].copy = copy;
    }
    ReleaseSRWLockExclusive(&vtable_lock);
    if (copy) *P(u64, agent) = copy;
    c->x[0] = agent;
}
static void status_created_fighter(Cpu *c) { status_created(c, 0); }
static void status_created_weapon(Cpu *c) { status_created(c, 1); }

/* ---- line callbacks ---- */
/* Status Main line callbacks (once per frame, after the status's main function: "opff"-style
 * code): after the agent's status line runs (lua2cpp_common+0x3250, (agent, variadic, string,
 * va_list): smashline's call_line_status_hook), each plugin callback for that agent, or for every
 * fighter or weapon. The other lines' callbacks are inline hooks in Smashline: not yet. */
#define LINE_STATUS_OFFSET 0x3250
static u64 line_status_original;

static void line_status(Cpu *c) {
    u64 fighter = c->x[0];
    rt_call(c, line_status_original);
    u64 result = c->x[0];
    Agent a = agent_of(*P(u64, fighter + 0x38), -1);
    if (!a.hash) return;
    u64 common = a.weapon ? WEAPON_HASH : FIGHTER_HASH;
    u32 n = line_n;
    int is_claimed = -1;
    for (u32 i = 0; i < n; i++) {
        if (lines[i].line != LINE_MAIN || (lines[i].agent != a.hash && lines[i].agent != common)) continue;
        if (lines[i].agent == a.hash) {
            if (is_claimed < 0) is_claimed = claimed(a.hash, a.slot);
            if (!for_slot(lines[i].costume, is_claimed, a.slot)) continue;
        }
        c->x[0] = fighter;
        rt_call(c, lines[i].function);
        if (!lines[i].ran++) {
            char d[96];
            LOG("line callback %s ran for the first time (%s, c%02d)", rt_describe(lines[i].function, d, sizeof d), a.name, a.slot);
        }
    }
    c->x[0] = result;
}

/* A module the game loaded (host.c): the hooks that go into it. */
void skyline_module_loaded(const char *name, u64 base) {
    if (!line_n || strcmp(name, "lua2cpp_common") || line_status_original) return;
    line_status_original = rt_hook_function(base + LINE_STATUS_OFFSET, rt_native(line_status, "smashline line callbacks"));
    LOG("lua2cpp_common loaded: Main line callbacks %s", line_status_original ? "hooked" : "NOT hooked (no function there)");
}

/* ---- ARCropolis' API (what plugins ask of it) ------------------------------------- */
/* The mods' files are already in the game's data (tools/mods.py), so the file system is
 * "mounted" from the start: the event callbacks run once the plugins' mains have. */
static u64 event_callbacks[64][2];   /* (event, function) */
static u32 event_n;
HLE(arcrop_register_event_callback) {   /* (event, fn(event)) */
    char a[96];
    LOG("arcrop event callback for event %u: %s", (u32)c->x[0] & 0xFF, rt_describe(c->x[1], a, sizeof a));
    if (event_n < 64) { event_callbacks[event_n][0] = (u32)c->x[0] & 0xFF; event_callbacks[event_n++][1] = c->x[1]; }
}
HLE(arcrop_require_api_version) { (void)c; }
HLE(arcrop_api_version) { static u64 version; if (!version) { u32 v[2] = {1, 8}; version = rt_guest_copy(v, sizeof v); } c->x[0] = version; }
HLE(arcrop_is_mod_enabled) { c->x[0] = 1; }    /* (hash of a mod's folder): the turned-on mods are the ones applied */
HLE(arcrop_is_file_loaded) { c->x[0] = 1; }
HLE(arcrop_register_callback) {
    LOG("arcrop_register_callback for file %#llx: file callbacks are NOT SUPPORTED yet, left out", (unsigned long long)c->x[0]);
}
HLE(arcrop_register_callback_with_path) {
    LOG("arcrop_register_callback_with_path for %#llx: NOT SUPPORTED yet, left out", (unsigned long long)c->x[0]);
}
HLE(arcrop_register_extension_callback) { LOG("arcrop_register_extension_callback: NOT SUPPORTED yet, left out"); }
HLE(arcrop_get_decompressed_size) { c->x[0] = 0; }
HLE(arcrop_load_file) { c->x[0] = 0; }

/* ---- the CSK Collection's API: new characters on the select screen --------------------- */
/* Plugins add character entries (ui_chara_db, ui_layout_db) as Rust structs (the_csk_collection_api:
 * each field an enum, Overwrite(value) or Optional(Option<value>)). They are written to
 * <data dir>/../skyline/csk.json, and tools/mods.py puts them into the game's databases when it
 * applies the mods (a new entry is a copy of the one it is cloned from, with these fields set). */
enum { F_STR, F_H40, F_I16, F_I32, F_I8, F_U8, F_BOOL, F_F32, F_U8MAP, F_H40MAP };
static const struct { u32 size, payload; const char *prc; } FTYPE[] = {
    {24, 8, "string"}, {24, 8, "hash40"}, {8, 4, "short"}, {12, 4, "int"}, {8, 4, "sbyte"}, {8, 4, "byte"},
    {8, 4, "bool"}, {12, 4, "float"}, {56, 8, "byte"}, {56, 8, "hash40"},
};
typedef struct { const char *name; int type; } Field;
static const Field CHARA_FIELDS[] = {
    {"name_id", F_STR}, {"fighter_kind", F_H40}, {"fighter_kind_corps", F_H40}, {"ui_series_id", F_H40},
    {"fighter_type", F_H40}, {"alt_chara_id", F_H40}, {"exhibit_year", F_I16}, {"exhibit_day_order", F_I32},
    {"ext_skill_page_num", F_I8}, {"is_img_ext_skill_page0", F_BOOL}, {"is_img_ext_skill_page1", F_BOOL},
    {"is_img_ext_skill_page2", F_BOOL}, {"skill_list_order", F_I8}, {"disp_order", F_I8}, {"save_no", F_I8},
    {"chara_count", F_I8}, {"can_select", F_BOOL}, {"is_usable_soundtest", F_BOOL}, {"is_called_pokemon", F_BOOL},
    {"is_mii", F_BOOL}, {"is_boss", F_BOOL}, {"is_hidden_boss", F_BOOL}, {"is_dlc", F_BOOL}, {"is_patch", F_BOOL},
    {"is_plural_message", F_BOOL}, {"is_plural_narration", F_BOOL}, {"is_article", F_BOOL}, {"extra_flags", F_I32},
    {"has_multiple_face", F_BOOL}, {"result_pf0", F_BOOL}, {"result_pf1", F_BOOL}, {"result_pf2", F_BOOL},
    {"color_num", F_U8}, {"extra_index_maps", F_U8MAP}, {"extra_hash_maps", F_H40MAP}, {"shop_item_tag", F_H40}, {0, 0}};
/* ui_layout_db: ui_chara_id, chara_color, then eye flashes and the many offsets/scales (floats) */
static Field LAYOUT_FIELDS[160];
static void layout_fields(void) {
    static char names[160][40];
    int n = 0;
#define ADD(t, ...) do { snprintf(names[n], sizeof names[n], __VA_ARGS__); LAYOUT_FIELDS[n].name = names[n]; LAYOUT_FIELDS[n++].type = t; } while (0)
    ADD(F_H40, "ui_chara_id"); ADD(F_U8, "chara_color");
    for (int e = 0; e < 3; e++) {
        ADD(F_U8, "eye_%d_flash_count", e);
        for (int k = 0; k < 5; k++) { ADD(F_F32, "eye_%d_flash%d_pos_x", e, k); ADD(F_F32, "eye_%d_flash%d_pos_y", e, k); }
    }
    ADD(F_F32, "eye_flash_info_pos_x"); ADD(F_F32, "eye_flash_info_pos_y"); ADD(F_BOOL, "spirits_eye_visible");
    static const char *const sets[] = {"chara_1", "chara_1_1", "chara_1_2", "chara_1_3", "chara_1_4", "chara_1_5", "chara_3_0",
                                       "chara_3_1", "chara_3_2", "chara_3_3", "chara_3_4", "chara_3_5", "chara_3_6", "chara_3_7",
                                       "chara_5", "chara_select_icon_list", "chara_7_0", "chara_7_1", "chara_0", 0};
    for (int k = 0; sets[k]; k++) { ADD(F_F32, "%s_offset_x", sets[k]); ADD(F_F32, "%s_offset_y", sets[k]); ADD(F_F32, "%s_scale", sets[k]); }
    LAYOUT_FIELDS[n].name = 0;
#undef ADD
}

static FILE *csk;   /* (open while plugins load: the json being written) */
static u64 csk_current;   /* (the ui_chara_id of the entry being written) */
typedef struct { u64 ui_chara, base; int start; } Redirect;   /* (base: the fighter's own ui_chara, found when needed) */
static Redirect redirects[64];
static u32 redirect_n;
static int csk_items;

static void json_str(FILE *f, const char *s) {
    fputc('"', f);
    for (; *s; s++) { if (*s == '"' || *s == '\\') fputc('\\', f); if ((u8)*s >= 0x20) fputc(*s, f); }
    fputc('"', f);
}

/* One value of a field type at `at` (an Overwrite's or a Some's), as JSON. */
static void json_value(FILE *f, int type, u64 at) {
    switch (type) {
    case F_STR: { u64 p = *P(u64, at); json_str(f, p ? P(const char, p) : ""); break; }
    case F_H40: fprintf(f, "\"%#llx\"", (unsigned long long)*P(u64, at)); break;
    case F_I16: fprintf(f, "%d", *P(i16, at)); break;
    case F_I32: fprintf(f, "%d", *P(i32, at)); break;
    case F_I8: fprintf(f, "%d", *P(i8, at)); break;
    case F_U8: fprintf(f, "%u", *P(u8, at)); break;
    case F_BOOL: fprintf(f, "%s", *P(u8, at) ? "true" : "false"); break;
    case F_F32: fprintf(f, "%.9g", (double)*P(f32, at)); break;
    }
}

/* Where an Optional's value is (Rust's Option layout: the tag first), or 0 for None. */
static u64 option_value(int type, u64 at) {
    switch (type) {
    case F_STR: case F_H40: return *P(u8, at) == 1 ? at + 8 : 0;
    case F_I16: return *P(u16, at) == 1 ? at + 2 : 0;
    case F_I32: case F_F32: return *P(u32, at) == 1 ? at + 4 : 0;
    case F_I8: case F_U8: return *P(u8, at) == 1 ? at + 1 : 0;
    case F_BOOL: return *P(u8, at) <= 1 ? at : 0;   /* (bool's niche: 2 is None) */
    }
    return 0;
}

/* A std HashMap<u64, V> (hashbrown): its table {ctrl, bucket_mask, growth_left, items} and the hash
 * keys {k0, k1} - in that order as built for skyline-rs (the other order, keys first and
 * bucket_mask before ctrl, is taken too); the entries (key, V) lie below ctrl, a byte of ctrl
 * each (top bit clear: in use). */
static int guest_pointer(u64 v) { return v >= 0x1000000000ull && v < 0x8000000000ull && !(v & 7); }
static void json_map(FILE *f, int type, u64 map, int *first) {
    u64 q[6];
    for (int i = 0; i < 6; i++) q[i] = *P(u64, map + 8 * i);
    u64 ctrl, mask, items;
    if (guest_pointer(q[0])) ctrl = q[0], mask = q[1], items = q[3];
    else ctrl = q[3], mask = q[2], items = q[5];
    if (!guest_pointer(ctrl) || mask & (mask + 1)) return;
    if (!ctrl || mask > 4095 || items > mask + 1) return;
    int vtype = type == F_U8MAP ? F_U8 : F_H40;
    u64 size = 8 + (vtype == F_U8 ? 8 : 24);
    for (u64 i = 0; i <= mask; i++) {
        if (*P(u8, ctrl + i) & 0x80) continue;
        u64 e = ctrl - (i + 1) * size, key = *P(u64, e), v = e + 8;
        u32 tag = *P(u32, v);
        u64 val = tag == 0 ? v + FTYPE[vtype].payload : tag == 1 ? option_value(vtype, v + FTYPE[vtype].payload) : 0;
        if (!val) continue;
        if (key == 0x11895f00fcull && redirect_n < 64 && csk_current)   /* (color_start_index: csk redirect) */
            redirects[redirect_n++] = (Redirect){csk_current, 0, *P(u8, val)};
        fprintf(f, "%s\"%#llx\": [\"%s\", ", *first ? "" : ", ", (unsigned long long)key, FTYPE[vtype].prc);
        json_value(f, vtype, val);
        fputc(']', f);
        *first = 0;
    }
}

/* An entry struct: id (u64), clone-from id (Option<u64>), then the fields, repr(C)-aligned. */
static void json_entry(const char *kind, const char *id_key, const Field *fields, u64 at) {
    if (!csk) return;
    csk_current = !strcmp(kind, "ui_chara_db") ? *P(u64, at) : 0;
    fprintf(csk, "%s  {\"kind\": \"%s\", \"%s\": \"%#llx\", \"clone_from\": ", csk_items++ ? ",\n" : "", kind, id_key,
            (unsigned long long)*P(u64, at));
    if (*P(u8, at + 8) == 1) fprintf(csk, "\"%#llx\"", (unsigned long long)*P(u64, at + 16));
    else fprintf(csk, "null");
    fprintf(csk, ", \"set\": {");
    u64 o = 24;
    int first = 1;
    for (const Field *fl = fields; fl->name; fl++) {
        u32 align = FTYPE[fl->type].payload;
        o = (o + align - 1) & ~(u64)(align - 1);
        u64 v = at + o, p = v + FTYPE[fl->type].payload;
        u32 tag = *P(u32, v);
        if (fl->type == F_U8MAP || fl->type == F_H40MAP) {
            if (tag <= 1) json_map(csk, fl->type, p, &first);
        } else {
            u64 val = tag == 0 ? p : tag == 1 ? option_value(fl->type, p) : 0;
            if (val) {
                fprintf(csk, "%s\"%s\": [\"%s\", ", first ? "" : ", ", fl->name, FTYPE[fl->type].prc);
                json_value(csk, fl->type, val);
                fputc(']', csk);
                first = 0;
            }
        }
        o += FTYPE[fl->type].size;
    }
    fprintf(csk, "}}");
}

HLE(add_chara_db_entry_info) {
    LOG("CSK: character entry %#llx", (unsigned long long)*P(u64, c->x[0]));
    json_entry("ui_chara_db", "ui_chara_id", CHARA_FIELDS, c->x[0]);
}
HLE(add_chara_layout_db_entry_info) {
    LOG("CSK: character layout entry %#llx", (unsigned long long)*P(u64, c->x[0]));
    if (!LAYOUT_FIELDS[0].name) layout_fields();
    json_entry("ui_layout_db", "ui_layout_id", LAYOUT_FIELDS, c->x[0]);
}
HLE(add_narration_characall_entry) {   /* (C string) -> bool */
    if (csk) { fprintf(csk, "%s  {\"kind\": \"narration\", \"label\": ", csk_items++ ? ",\n" : ""); json_str(csk, guest_str(c->x[0])); fprintf(csk, "}"); }
    c->x[0] = 1;
}
HLE(set_fighter_jingle) { LOG("CSK: fighter jingle for %#llx: %s (left out)", (unsigned long long)c->x[0], guest_str(c->x[1])); }
HLE(allow_ui_chara_hash_online) { (void)c; }
HLE(disable_ui_chara_hash_online) { (void)c; }
HLE(play_bgm) { (void)c; }
HLE(is_online) { c->x[0] = 0; }
HLE(csk_collection_version) { static u64 v; if (!v) { u32 ver[3] = {5, 0, 6}; v = rt_guest_copy(ver, sizeof ver); } c->x[0] = v; }
#define CSK_UNSUPPORTED(name) HLE(name) { (void)c; LOG("CSK: " #name " is NOT SUPPORTED yet, left out"); }
CSK_UNSUPPORTED(add_series_db_entry_info) CSK_UNSUPPORTED(add_bgm_db_entry_info) CSK_UNSUPPORTED(add_stream_set_entry_info)
CSK_UNSUPPORTED(add_assigned_info_entry_info) CSK_UNSUPPORTED(add_stream_property_entry_info) CSK_UNSUPPORTED(add_new_sli_entry)
CSK_UNSUPPORTED(add_new_bgm_property_entry) CSK_UNSUPPORTED(add_tracks_to_playlist) CSK_UNSUPPORTED(add_stage_db_entry)
CSK_UNSUPPORTED(add_ui_stage_db_resources_entry) CSK_UNSUPPORTED(add_gametitle_db_entry_info) CSK_UNSUPPORTED(add_tips_db_entry_info)
CSK_UNSUPPORTED(add_amiibo_db_entry_info) CSK_UNSUPPORTED(add_mii_body_db_entry_info) CSK_UNSUPPORTED(add_mii_hat_db_entry_info)
CSK_UNSUPPORTED(load_ui_file) CSK_UNSUPPORTED(change_entry_chara_ui)
HLE(get_color_from_entry_id) { c->x[0] = 0; }
HLE(get_ui_chara_from_entry_id) { c->x[0] = 0; }
HLE(get_victor_color) { c->x[0] = 0; }

/* ---- the CSK Collection's select-screen redirect: a new character's colors are its fighter's
 * costumes from color_start_index on (c120...). The game makes each player's fighter from a
 * record (main+0x66dcf0: kind at +8, costume at +0x19, entry at +0xa7); before that, its
 * pre-battle set-up (main+0x141b360) has each player's choice. */
static u64 entry_chara[8];   /* the ui_chara each entry chose (0: not known) */
#define PRE_BATTLE 0x141b360
#define FIGHTER_SETUP 0x66dcf0
#define COLOR_START_KEY 0x11895f00fcull   /* hash40("color_start_index") */
static u64 pre_battle_original, fighter_setup_original;

static void scan_for(const char *what, u64 at, u64 size) {
    for (u64 o = 0; o + 8 <= size; o += 8) {
        u64 v = *P(u64, at + o) & 0xFFFFFFFFFFull;
        for (u32 r = 0; r < redirect_n; r++)
            if (v == redirects[r].ui_chara) LOG("redirect probe: %s+%#llx holds %#llx", what, (unsigned long long)o, (unsigned long long)v);
    }
}

static void pre_battle(Cpu *c) {
    u64 setup = *P(u64, c->x[0] + 0x28);
    rt_call(c, pre_battle_original);
    if (!redirect_n || !setup) return;
    u64 list = *P(u64, setup + 0x1010);
    int n = list ? *P(i32, list) : 0;
    if (getenv("SSBU_REDIRECT_PROBE")) {
        scan_for("setup", setup, 0x1100);
        if (list) scan_for("list", list, 8 * 0x1d8 + 0x100);
    }
    LOG("pre-battle: %d players", n);
}

/* The redirect a record names, if any: a new character's ui_chara hash among its first 0x200 bytes. */
static Redirect *redirect_in(u64 rec, u64 *where) {
    for (u64 o = 0; o + 8 <= 0x200; o += 8) {
        u64 v = *P(u64, rec + o) & 0xFFFFFFFFFFull;
        for (u32 r = 0; r < redirect_n; r++)
            if (v == redirects[r].ui_chara) { *where = o; return &redirects[r]; }
    }
    return NULL;
}

/* The game's portrait paths (main+0x3238a70: (tagged ui_chara hash, color, image kind)): a
 * redirected character's costume (c120...) is its own color again (0...), with its own
 * portraits - wherever the game asks by the costume, as the battle's HUD does. */
#define UI_PATH 0x3238a70
static u64 ui_path_original;

static void ui_path(Cpu *c) {
    u64 h = c->x[0] & 0xFFFFFFFFFFull;
    int color = (int)c->x[1];
    for (u32 i = 0; i < redirect_n; i++) {
        Redirect *r = &redirects[i];
        if (color < r->start || color >= r->start + 64) continue;
        if (h == r->ui_chara || (r->base && h == r->base)) {
            c->x[0] = (c->x[0] & ~0xFFFFFFFFFFull) | r->ui_chara;
            c->x[1] = (u64)(color - r->start);
            break;
        }
    }
    rt_call(c, ui_path_original);
}

static void fighter_setup(Cpu *c) {
    u64 rec = c->x[0], where = 0;
    int entry = *P(u8, rec + 0xa7);
    Redirect *r = redirect_n ? redirect_in(rec, &where) : NULL;
    if (!r && entry < 8 && entry_chara[entry])
        for (u32 i = 0; i < redirect_n && !r; i++)
            if (redirects[i].ui_chara == entry_chara[entry]) r = &redirects[i];
    if (r && !r->base) {   /* (the fighter's own ui_chara: ui_chara_<its name>) */
        const char *n = guest_name(FIGHTER_NAMES, *P(i8, rec + 8), FIGHTER_COUNT);
        char ui[64];
        if (n) { snprintf(ui, sizeof ui, "ui_chara_%s", n); r->base = hash40(ui, strlen(ui)); }
    }
    if (r && *P(u8, rec + 0x19) < r->start) {
        LOG("redirect: entry %d (%#llx at +%#llx): costume c%02u -> c%02u", entry, (unsigned long long)r->ui_chara,
            (unsigned long long)where, *P(u8, rec + 0x19), *P(u8, rec + 0x19) + r->start);
        *P(u8, rec + 0x19) += (u8)r->start;
    } else if (getenv("SSBU_REDIRECT_PROBE")) {
        LOG("fighter set-up: entry %d kind %d costume %u: no redirect", entry, *P(i8, rec + 8), *P(u8, rec + 0x19));
    }
    rt_call(c, fighter_setup_original);
}

/* ---- loading ----------------------------------------------------------------------- */
void skyline_load_plugins(Cpu *c) {
    static int done;
    if (done++) return;
    char path[1200], line[256];
    snprintf(path, sizeof path, "%s/../skyline/plugins.txt", rt_data_dir);
    FILE *f = fopen(path, "r");
    if (!f) return;
    const char *q = getenv("SSBU_SKYLINE_LOG");
    quiet = q && *q == '0';
    u64 saved[31];
    memcpy(saved, c->x, sizeof saved);
    /* (plugins find Smashline with nn::ro::LookupSymbol: the native one, from now on) */
    rt_wrap_symbol("_ZN2nn2ro12LookupSymbolEPmPKc", lookup_symbol);
    fs_mount_sd();
    for (u32 k = 0; k < NFACTORIES; k++) {
        factory_original[k] = rt_hook_function(MAIN_BASE + FACTORIES[k].offset, rt_native(factory_hooks[k], "smashline create_agent"));
        if (!factory_original[k]) LOG("create_agent at main+%#llx is not a function: Smashline's acmd scripts will not reach it",
                                      (unsigned long long)FACTORIES[k].offset);
    }
    FIGHTER_HASH = hash40("fighter", 7);
    WEAPON_HASH = hash40("weapon", 6);
    GuestFn *status_hooks[2] = {status_created_fighter, status_created_weapon};
    for (int k = 0; k < 2; k++) {
        status_factory_original[k] = rt_hook_function(MAIN_BASE + STATUS_FACTORIES[k], rt_native(status_hooks[k], "smashline create_agent_status"));
        if (!status_factory_original[k]) LOG("create_agent_status at main+%#llx is not a function: status scripts will not reach it",
                                             (unsigned long long)STATUS_FACTORIES[k]);
    }
    char csk_path[1200], csk_tmp[1220];
    snprintf(csk_path, sizeof csk_path, "%s/../skyline/csk.json", rt_data_dir);
    snprintf(csk_tmp, sizeof csk_tmp, "%s.tmp", csk_path);
    csk = fopen(csk_tmp, "w");
    if (csk) fprintf(csk, "[\n");
    pre_battle_original = rt_hook_function(MAIN_BASE + PRE_BATTLE, rt_native(pre_battle, "csk pre-battle"));
    fighter_setup_original = rt_hook_function(MAIN_BASE + FIGHTER_SETUP, rt_native(fighter_setup, "csk fighter set-up"));
    note_hook_target(MAIN_BASE + FIGHTER_SETUP);   /* (called directly too: hookable from the next build on) */
    ui_path_original = rt_hook_function(MAIN_BASE + UI_PATH, rt_native(ui_path, "csk portrait paths"));
    note_hook_target(MAIN_BASE + UI_PATH);
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n#")] = 0;
        if (!*line) continue;
        u64 base = rt_load_nro_named(c, line);
        if (!base) { fprintf(stderr, "[skyline] plugin %s is not recompiled into this build: left out\n", line); continue; }
        u64 entry = rt_module_export(base, "main");
        fprintf(stderr, "[skyline] plugin %s at %#llx: running main\n", line, (unsigned long long)base);
        if (entry) rt_call(c, entry);
        fprintf(stderr, "[skyline] plugin %s: main returned\n", line);
    }
    fclose(f);
    for (int ev = 0; ev < 2; ev++)   /* ArcFilesystemMounted, ModFilesystemMounted */
        for (u32 i = 0; i < event_n; i++)
            if (event_callbacks[i][0] == (u64)ev) {
                char a[96];
                LOG("running arcrop event %d callback %s", ev, rt_describe(event_callbacks[i][1], a, sizeof a));
                c->x[0] = (u64)ev;
                rt_call(c, event_callbacks[i][1]);
            }
    if (csk) {
        fprintf(csk, "\n]\n");
        fclose(csk);
        csk = NULL;
        /* (a change means the mods need applying again: tools/skyline.py sync does) */
        FILE *old_f = fopen(csk_path, "rb"), *new_f = fopen(csk_tmp, "rb");
        int same = old_f && new_f;
        while (same) { int a = fgetc(old_f), b = fgetc(new_f); if (a != b) same = 0; if (a == EOF || b == EOF) break; }
        if (old_f) fclose(old_f);
        if (new_f) fclose(new_f);
        if (same) remove(csk_tmp);
        else {
            rename(csk_tmp, csk_path);
            fprintf(stderr, "[skyline] the plugins' character entries changed (%s): the mods need applying again for them to show\n", csk_path);
        }
    }
    if (getenv("SSBU_SKYLINE_PROBE")) { fprintf(stderr, "[skyline] probe done\n"); fflush(stderr); ExitProcess(0); }
    fprintf(stderr, "[skyline] plugins installed %u acmd scripts, %u status scripts, %u line callbacks\n", acmd_n, status_n, line_n);
    memcpy(c->x, saved, sizeof saved);
}
