#!/usr/bin/env python3
"""Skyline plugins: the code moveset and new-character mods ship (plugin.nro), made part of the
game (docs/SKYLINE.md).

Usage:
  skyline.py sync  [--tree <dir>]               the turned-on mods' plugins: recompiled, listed to load
  skyline.py build [--tree <dir>] [--jobs N]    sync, then compile and link them into the game
  skyline.py status [--tree <dir>]              what is listed, and whether the game has it built in
  skyline.py probe [--tree <dir>]               start the game once for the plugins' select-screen entries; apply the mods again if they changed

sync (tools/mods.py apply runs it): each turned-on mod's plugin goes into dump/nro/ (named and
placed by tools/recomp.py, as a game NRO the runtime harvested would be), its C is generated
(build/nro/<name>/) when it is new, and dump/skyline/plugins.txt lists the plugins to load, in
the mods' order. The plugins the runtime provides itself (Smashline 2, lib_paramconfig,
ARCropolis, nro_hook, the CSK Collection) are left out wherever a mod ships them. A plugin that
is listed but not compiled into the game yet is left out at start-up (the log says so): build
compiles it - the runtime and the plugins only, a minute or two, not the whole game.

Hooks: plugins hook game functions with Skyline's A64HookFunction; the runtime does that
through the dispatch tables, which reach every indirect call. A function the game calls
directly as well needs it built in: the runtime lists every hook target it is asked for in
dump/skyline/hooks.txt, and the targets the generated C calls directly are made hookable
(tools/recomp.py HOOKABLE) by the next build, which regenerates the C of the modules they are in
(that takes long for main: tens of minutes)."""
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMPILER = ("recomp.py", "lift.py", "lift_simd.py", "regcache.py", "plugin_eh.py", "eh.py")   # (what makes a plugin's C)
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)

# Game functions runtime/hle_skyline.c hooks itself that the game also calls directly (the CSK
# Collection's select-screen redirect: the fighter set-up and the portrait paths)
NATIVE_HOOKS = ("main 66dcf0", "main 3238a70", "main 64b730", "main 6079d0") + tuple(   # (+ Smashline's cloned-weapon swaps, inline)
    f"main {o:x} inline" for o in (0x33b6bd8, 0x33ad528, 0x33af3e8, 0x33ae488, 0x33ac5f0, 0x17e0a0c, 0x33ad42c, 0x33af2ec,
                                   0x33ae38c, 0x33ac504, 0x17e094c, 0x33b6de0, 0x33b6fa0, 0x33b72c4, 0x33ad43c, 0x33af2fc,
                                   0x33ae39c, 0x33ac514))
# (provided natively by runtime/hle_skyline.c, or linked into the plugins that use them)
NATIVE = {"libsmashline_plugin.nro", "libsmashline_hook.nro", "libparam_config.nro", "libarcropolis.nro",
          "libnro_hook.nro", "libthe_csk_collection.nro", "libskyline.nro"}


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


class Paths:
    def __init__(self, tree):
        self.tree = os.path.abspath(tree)
        self.nro = os.path.join(self.tree, "dump", "nro")
        self.modules = os.path.join(self.tree, "dump", "modules")
        self.skyline = os.path.join(self.tree, "dump", "skyline")
        self.list = os.path.join(self.skyline, "plugins.txt")
        self.record = os.path.join(self.skyline, "plugins.json")   # build id -> module name, mod, source
        self.hooks = os.path.join(self.skyline, "hooks.txt")
        self.build = os.path.join(self.tree, "build", "nro")


def load_record(p):
    try:
        with open(p.record, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


STANDARD = ("skyline", "plugins")   # (<tree>/skyline/plugins: the community plugins WindowSmash ships, run as they are)


def standard(tree):
    """[(label, plugin file)] of the standard plugins (<tree>/skyline/plugins/*.nro), by name."""
    folder = os.path.join(tree, *STANDARD)
    names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".nro")) if os.path.isdir(folder) else []
    return [("(standard) " + n, os.path.join(folder, n)) for n in names]


def turned_on(tree):
    """[(mod name, plugin file)]: the standard plugins, then the turned-on mods' in their order. A
    mod's own copy of a standard plugin (or of one the runtime provides) is left out."""
    import mods
    t = mods.Tree(tree)
    state = t.load()
    out = standard(tree)
    have = {os.path.basename(f).lower() for _n, f in out}
    for name in state["order"]:
        if not state["mods"].get(name, {}).get("enabled"):
            continue
        for rel in mods.plugins(os.path.join(t.mods, name)):
            if os.path.basename(rel).lower() in NATIVE | have:
                continue
            out.append((name, os.path.join(t.mods, name, rel)))
    return out


def recomp(p, *args):
    cmd = [sys.executable, os.path.join(HERE, "recomp.py"), *args, "--tree", p.tree]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"recomp.py {' '.join(args)} failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    return r.stdout


def module_of(p, source, record):
    """The module name tools/recomp.py gives the plugin: placed in dump/nro/ if it is new."""
    import recomp as rc
    from nso import Module
    m = Module(source)
    placed = os.path.join(p.nro, record[m.build_id]["module"] + ".nro") if m.build_id in record else None
    if placed and os.path.exists(placed):
        if not filecmp.cmp(source, placed, shallow=False):   # (same build, other bytes: a ported plugin, tools/port_plugin.py)
            shutil.copyfile(source, placed)
        return m.build_id, record[m.build_id]["module"]
    rc.NRO_DIR = p.nro
    shutil.copyfile(source, os.path.join(p.nro, m.build_id[:16] + ".nro"))
    rc.nro_modules()   # (names it after the module, gives it an address: bases.txt)
    for row in open(os.path.join(p.nro, "bases.txt"), encoding="utf-8").read().splitlines():
        parts = row.split()
        if len(parts) == 4 and parts[3] == m.build_id:
            return m.build_id, parts[0]
    raise SystemExit(f"{source}: recomp.py did not place it")


def generate(p, name, log, own_exports=False):
    """The plugin's image and C, if missing or older than the plugin. own_exports: a standard plugin,
    whose exports stay its own (not the runtime's native versions of the same names)."""
    nro = os.path.join(p.nro, name + ".nro")
    img = os.path.join(p.modules, name + ".reloc.bin")
    out = os.path.join(p.build, name)
    table = os.path.join(out, "g_module.c")
    # (again when the recompiler is newer than the C: a fix in it reaches plugins generated before)
    made_by = max(os.path.getmtime(os.path.join(HERE, n)) for n in RECOMPILER if os.path.exists(os.path.join(HERE, n)))
    fresh_c = not os.path.exists(table) or os.path.getmtime(table) < max(os.path.getmtime(nro), made_by)
    # (the image goes with the C: its size, and the unwind tables recomp.py adds to a plugin's)
    if fresh_c or not os.path.exists(img) or os.path.getmtime(img) < os.path.getmtime(nro):
        recomp(p, "image", nro, "--out", img)
    if fresh_c:
        log(f"{name}: generating its C")
        if own_exports:
            os.environ["SSBU_OWN_EXPORTS"] = "1"
        try:
            log(recomp(p, "build", nro, "--out", out, "--jobs", "1").strip().splitlines()[-1])
        finally:
            os.environ.pop("SSBU_OWN_EXPORTS", None)


SEED = os.path.join(HERE, "skyline_seed")   # (tools/skyline_seed: hooks.txt, patches.txt the standard plugins need, 13.0.5)


def seed_lists(p):
    """The standard plugins' hook points and code patches (what their probe boots and a first match
    would record: tools/skyline_seed), added to dump/skyline's lists, so the first build has them."""
    if not standard(p.tree):
        return
    for name in ("hooks.txt", "patches.txt"):
        seed = os.path.join(SEED, name)
        if not os.path.exists(seed):
            continue
        dest = os.path.join(p.skyline, name)
        have = open(dest, encoding="utf-8").read().splitlines() if os.path.exists(dest) else []
        new = [l for l in open(seed, encoding="utf-8").read().splitlines() if l.strip() and l not in have]
        if new:
            with open(dest, "a", encoding="utf-8", newline="\n") as f:
                f.writelines(l + "\n" for l in new)


def sync(tree, log=print):
    """Recompile the turned-on mods' plugins and list them to load; returns the list."""
    p = Paths(tree)
    os.makedirs(p.skyline, exist_ok=True)
    seed_lists(p)
    have = open(p.hooks, encoding="utf-8").read().split("\n") if os.path.exists(p.hooks) else []
    missing = [h for h in NATIVE_HOOKS if h not in have]
    if missing:
        with open(p.hooks, "a", encoding="utf-8", newline="\n") as f:
            f.writelines(h + "\n" for h in missing)
    record = load_record(p)
    names = []
    skip = set()   # (dump/skyline/skip.txt: plugins left out, by module name or mod-relative file - one that does not run yet)
    if os.path.exists(os.path.join(p.skyline, "skip.txt")):
        skip = {l.split("#")[0].strip() for l in open(os.path.join(p.skyline, "skip.txt"), encoding="utf-8")} - {""}
    for mod, source in turned_on(tree):
        build_id, name = module_of(p, source, record)
        if name in skip or os.path.relpath(source, os.path.join(tree, "mods")).replace(os.sep, "/") in skip:
            log(f"{mod}: plugin {os.path.relpath(source, os.path.join(tree, 'mods', mod))} left out (dump/skyline/skip.txt)")
            continue
        record[build_id] = {"module": name, "mod": mod, "file": os.path.relpath(source, tree)}
        generate(p, name, log, mod.startswith("(standard) "))
        if name not in names:
            names.append(name)
            log(f"{mod}: plugin {os.path.basename(source)} -> {name}")
    with open(p.record, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    import mods
    t = mods.Tree(tree)
    state = t.load()
    with open(os.path.join(p.skyline, "mods.txt"), "w", encoding="utf-8", newline="\n") as f:   # (the runtime's mods: mount)
        written = set()
        for n in state["order"]:   # (each data root of a mod: a pack keeps its mods in subfolders)
            if state["mods"].get(n, {}).get("enabled"):
                folder = os.path.join(t.mods, n)
                roots = []
                for dp, dirs, _f in os.walk(folder):
                    if any(d.lower() in mods.ROOTS for d in dirs):
                        roots.append(dp)
                    dirs[:] = [d for d in dirs if d.lower() not in mods.ROOTS]   # (not into a root's own data)
                roots = roots or [folder]
                for r in roots:   # (once each: a pack and its parts can name the same folder)
                    rel = os.path.relpath(r, t.mods).replace(os.sep, "/")
                    if rel not in written:
                        written.add(rel)
                        f.write(rel + "\n")
        if os.path.isdir(os.path.join(t.mods, mods.CHARACTERS)):   # (the plugins' new characters: last, mods.characters_mod)
            f.write(mods.CHARACTERS + "\n")
    # With the real ARCropolis among the standard plugins, it loads the mods' plugins itself (its
    # chainloader, from each mod's folder, as on the console): only the standard ones are listed
    if any(m == "(standard) libarcropolis.nro" for m, _s in turned_on(tree)):
        std = {module_of(p, s, record)[1] for m, s in turned_on(tree) if m.startswith("(standard) ")}
        names = [n for n in names if n in std]
    text = "".join(f"{n}\n" for n in names)
    old = open(p.list, encoding="utf-8").read() if os.path.exists(p.list) else ""
    if text != old:
        with open(p.list, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    missing = [n for n in names if not built_in(tree, n)]
    if missing:
        log("not in the game yet (build them: Mods app > Build plugins into the game, or tools/skyline.py build): " + ", ".join(missing))
    return names


def built_in(tree, name):
    """Whether the game's program has the plugin compiled in (its nro_table names it)."""
    for table in (os.path.join(tree, "build-mac", "gen", "nro_table.c"), os.path.join(tree, "build", "rt", "nro_table.c")):
        if os.path.exists(table) and f"rt_module_{name}," in open(table, encoding="utf-8").read():
            return True
    return False


def plugin_state(tree, mod, rels):
    """[{"file", "module", "built"}] of a mod's code plugins (`rels`: paths in its folder) the game
    runs: built is whether the game's program has it compiled in (None: never recompiled - the mod
    was not turned on and applied yet). The plugins WindowSmash provides itself are left out."""
    from nso import Module
    record = load_record(Paths(tree))
    out = []
    for rel in rels:
        if os.path.basename(rel).lower() in NATIVE:
            continue
        try:
            entry = record.get(Module(os.path.join(tree, "mods", mod, rel)).build_id)
        except Exception:   # (not an NRO the recompiler reads)
            entry = None
        name = entry and entry["module"]
        out.append({"file": rel, "module": name, "built": built_in(tree, name) if name else None})
    return out


def hook_targets(p):
    """{module: {offset}} of the hook targets the runtime listed (dump/skyline/hooks.txt)."""
    out = {}
    if os.path.exists(p.hooks):
        for line in open(p.hooks, encoding="utf-8"):
            m = re.match(r"(\w+)\s+([0-9a-f]+)$", line.strip())
            if m:
                out.setdefault(m.group(1), set()).add(int(m.group(2), 16))
    return out


def inline_targets(p):
    """{module: {offset}} of the inline hook points the runtime listed ("<module> <offset> inline")."""
    out = {}
    if os.path.exists(p.hooks):
        for line in open(p.hooks, encoding="utf-8"):
            m = re.match(r"(\w+)\s+([0-9a-f]+)\s+inline$", line.strip())
            if m:
                out.setdefault(m.group(1), set()).add(int(m.group(2), 16))
    return out


def patch_lines(p):
    """{module: text} of the plugins' code patches (dump/skyline/patches.txt), as recomp.py records
    the ones a module's C was generated with (<build dir>/plugin_patches.txt)."""
    out = {}
    path = os.path.join(p.skyline, "patches.txt")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            parts = line.split()
            if len(parts) == 4:
                out[parts[0]] = out.get(parts[0], "") + " ".join(parts[1:]) + "\n"
    return out


def stale_hooks(tree):
    """The game modules whose generated C lacks hook targets or code patches the runtime listed
    (hooks.txt, patches.txt)."""
    import recomp as rc
    rc.TREE = tree
    out = []
    p = Paths(tree)
    hooks, inline, patches = hook_targets(p), inline_targets(p), patch_lines(p)
    for mod in sorted(set(hooks) | set(inline) | set(patches)):
        offs = hooks.get(mod, set())
        table = os.path.join(tree, "build", mod, "g_module.c") if mod in ("main", "subsdk0", "sdk") else \
            os.path.join(tree, "build", "nro", mod, "g_module.c")
        base = rc.MODULES.get(mod) or rc.nro_modules().get(mod) if os.path.exists(table) else None
        if not base:
            continue
        text = open(table, encoding="utf-8").read()
        rec = os.path.join(os.path.dirname(table), "plugin_patches.txt")
        if patches.get(mod, "") != (open(rec, encoding="utf-8").read() if os.path.exists(rec) else ""):
            out.append(mod)
            continue
        if any(f"f_{base + o:x}_orig" not in text for o in offs):
            out.append(mod)
            continue
        if inline.get(mod):   # (the C files: is each point's rt_inline_hook in one?)
            folder = os.path.dirname(table)
            have = set()
            for n in os.listdir(folder):
                if n.startswith("g") and n.endswith(".c") and n != "g_module.c":
                    have |= {int(h, 16) for h in re.findall(r"rt_inline_hook\(c, 0x([0-9A-Fa-f]+)ull\)", open(os.path.join(folder, n), encoding="utf-8").read())}
            if any(base + o not in have for o in inline[mod]):
                out.append(mod)
    return out


def program(tree):
    for exe in (os.path.join(tree, "build-mac", "WindowSmash.app", "Contents", "MacOS", "ssbu"),
                os.path.join(tree, "build", "ssbu.exe"), os.path.join(tree, "build-linux", "ssbu")):
        if os.path.exists(exe):
            return exe
    return None


def probe(tree, log=print):
    """Start the game until its plugins have loaded (SSBU_SKYLINE_PROBE): what they add to the select
    screen goes to dump/skyline/csk.json. True if that changed."""
    exe, p = program(tree), Paths(tree)
    if not exe or not os.path.exists(p.list) or not open(p.list, encoding="utf-8").read().strip():
        return False
    path = os.path.join(p.skyline, "csk.json")

    def entries():   # (as data: a plugin's maps come out in a different order each start)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    def outputs():   # (the files plugins' file callbacks are for, and what they made: dump/skyline/arc/*.out)
        out = {}
        for n in ["callbacks.txt"] + [os.path.join("arc", x) for x in sorted(os.listdir(os.path.join(p.skyline, "arc")))
                                       if x.endswith(".out")] if os.path.isdir(os.path.join(p.skyline, "arc")) else ["callbacks.txt"]:
            try:
                with open(os.path.join(p.skyline, n), "rb") as f:
                    out[n] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                pass
        return out
    before = entries(), outputs()
    env = dict(os.environ, SSBU_SKYLINE_PROBE="1", SDL_AUDIO_DRIVER="dummy", SSBU_CWD=tree, SSBU_TESTRUN="skyline-probe",
               SSBU_LOG=os.path.join(p.skyline, "probe.log"))
    log("starting the game once for the plugins' select-screen entries")
    try:
        subprocess.run([exe, os.path.join(tree, "dump", "modules")], env=env, timeout=180, cwd=tree)
    except subprocess.TimeoutExpired:
        log("the probe did not finish (see dump/skyline/probe.log)")
        return False
    return (entries(), outputs()) != before


def probe_and_apply(tree, log=print):
    """Probe, and apply the mods again while that changed what the plugins add (at most 3 times: the
    file callbacks need an apply for their input, a start for their output, an apply to put it in)."""
    import mods
    for _ in range(3):
        if not probe(tree, log):
            return
        log("the plugins' select-screen entries or file changes changed: applying the mods again")
        mods.apply(mods.Tree(tree), log)


def build(tree, jobs, log=print, again=True):
    """sync, the C of the modules with new hook targets, the compile and link, then the probe and
    (if the plugins' characters changed) the mods applied again; then once more if that start listed
    new hook points or code patches (a new plugin's: the first build can't know them)."""
    sync(tree, log)
    exefs = os.path.join(tree, "dump", "exefs")
    for mod in stale_hooks(tree):
        log(f"{mod}: generating its C again for the plugins' hooks")
        src = os.path.join(exefs, mod) if mod in ("main", "subsdk0", "sdk") else os.path.join(tree, "dump", "nro", mod + ".nro")
        out = os.path.join(tree, "build", mod) if mod in ("main", "subsdk0", "sdk") else os.path.join(tree, "build", "nro", mod)
        log(recomp(Paths(tree), "build", src, "--out", out, "--jobs", str(jobs)).strip().splitlines()[-1])
    if sys.platform == "darwin":
        cmd = [sys.executable, os.path.join(HERE, "mac", "build_mac.py"), "--port", tree, "--jobs", str(jobs)]
    else:   # (Windows, Linux: the game's build compiles and links every module's C)
        cmd = [sys.executable, os.path.join(HERE, "recomp.py"), "all", exefs, "--out", os.path.join(tree, "build"),
               "--jobs", str(jobs), "--tree", tree]
    log(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc:
        return rc
    probe_and_apply(tree, log)
    if again and stale_hooks(tree):
        log("the plugins asked for more hook points: building once more")
        return build(tree, jobs, log, again=False)
    return 0


def status(tree):
    p = Paths(tree)
    names = open(p.list, encoding="utf-8").read().split() if os.path.exists(p.list) else []
    if not names:
        print("no plugins listed")
    for n in names:
        print(f"{n}: {'built in' if built_in(tree, n) else 'NOT built in yet'}")
    for mod, offs in sorted(hook_targets(p).items()):
        print(f"hooks in {mod}: " + " ".join(f"{o:#x}" for o in sorted(offs)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tree = os.path.abspath(arg("--tree", ROOT))
    if sys.argv[1] == "sync":
        sync(tree)
    elif sys.argv[1] == "build":
        sys.exit(build(tree, int(arg("--jobs", "2"))))
    elif sys.argv[1] == "status":
        status(tree)
    elif sys.argv[1] == "probe":   # (the plugins' select-screen entries again, and the mods applied if they changed)
        probe_and_apply(tree)
    else:
        sys.exit(__doc__)
