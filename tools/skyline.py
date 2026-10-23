#!/usr/bin/env python3
"""Skyline plugins: the code moveset and new-character mods ship (plugin.nro), made part of the
game (docs/SKYLINE.md).

Usage:
  skyline.py sync  [--tree <dir>]               the turned-on mods' plugins: recompiled, listed to load
  skyline.py build [--tree <dir>] [--jobs N]    sync, then compile and link them into the game
  skyline.py status [--tree <dir>]              what is listed, and whether the game has it built in

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
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)

# Game functions runtime/hle_skyline.c hooks itself that the game also calls directly (the CSK
# Collection's select-screen redirect: the fighter set-up and the portrait paths)
NATIVE_HOOKS = ("main 66dcf0", "main 3238a70")
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


def turned_on(tree):
    """[(mod name, plugin file)] of the turned-on mods, in their order."""
    import mods
    t = mods.Tree(tree)
    state = t.load()
    out = []
    for name in state["order"]:
        if not state["mods"].get(name, {}).get("enabled"):
            continue
        for rel in mods.plugins(os.path.join(t.mods, name)):
            if os.path.basename(rel).lower() in NATIVE:
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
    if m.build_id in record and os.path.exists(os.path.join(p.nro, record[m.build_id]["module"] + ".nro")):
        return m.build_id, record[m.build_id]["module"]
    rc.NRO_DIR = p.nro
    shutil.copyfile(source, os.path.join(p.nro, m.build_id[:16] + ".nro"))
    rc.nro_modules()   # (names it after the module, gives it an address: bases.txt)
    for row in open(os.path.join(p.nro, "bases.txt"), encoding="utf-8").read().splitlines():
        parts = row.split()
        if len(parts) == 4 and parts[3] == m.build_id:
            return m.build_id, parts[0]
    raise SystemExit(f"{source}: recomp.py did not place it")


def generate(p, name, log):
    """The plugin's image and C, if missing or older than the plugin."""
    nro = os.path.join(p.nro, name + ".nro")
    img = os.path.join(p.modules, name + ".reloc.bin")
    out = os.path.join(p.build, name)
    if not os.path.exists(img) or os.path.getmtime(img) < os.path.getmtime(nro):
        recomp(p, "image", nro, "--out", img)
    if not os.path.exists(os.path.join(out, "g_module.c")) or os.path.getmtime(os.path.join(out, "g_module.c")) < os.path.getmtime(nro):
        log(f"{name}: generating its C")
        log(recomp(p, "build", nro, "--out", out, "--jobs", "1").strip().splitlines()[-1])


def sync(tree, log=print):
    """Recompile the turned-on mods' plugins and list them to load; returns the list."""
    p = Paths(tree)
    os.makedirs(p.skyline, exist_ok=True)
    have = open(p.hooks, encoding="utf-8").read().split("\n") if os.path.exists(p.hooks) else []
    missing = [h for h in NATIVE_HOOKS if h not in have]
    if missing:
        with open(p.hooks, "a", encoding="utf-8", newline="\n") as f:
            f.writelines(h + "\n" for h in missing)
    record = load_record(p)
    names = []
    for mod, source in turned_on(tree):
        build_id, name = module_of(p, source, record)
        record[build_id] = {"module": name, "mod": mod, "file": os.path.relpath(source, tree)}
        generate(p, name, log)
        if name not in names:
            names.append(name)
            log(f"{mod}: plugin {os.path.basename(source)} -> {name}")
    with open(p.record, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    import mods
    t = mods.Tree(tree)
    state = t.load()
    with open(os.path.join(p.skyline, "mods.txt"), "w", encoding="utf-8", newline="\n") as f:   # (the runtime's mods: mount)
        f.writelines(f"{n}\n" for n in state["order"] if state["mods"].get(n, {}).get("enabled"))
    text = "".join(f"{n}\n" for n in names)
    old = open(p.list, encoding="utf-8").read() if os.path.exists(p.list) else ""
    if text != old:
        with open(p.list, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    missing = [n for n in names if not built_in(tree, n)]
    if missing:
        log("not in the game yet (build them: Mods app > Rebuild, or tools/skyline.py build): " + ", ".join(missing))
    return names


def built_in(tree, name):
    """Whether the game's program has the plugin compiled in (its nro_table names it)."""
    for table in (os.path.join(tree, "build-mac", "gen", "nro_table.c"), os.path.join(tree, "build", "rt", "nro_table.c")):
        if os.path.exists(table) and f"rt_module_{name}," in open(table, encoding="utf-8").read():
            return True
    return False


def hook_targets(p):
    """{module: {offset}} of the hook targets the runtime listed (dump/skyline/hooks.txt)."""
    out = {}
    if os.path.exists(p.hooks):
        for line in open(p.hooks, encoding="utf-8"):
            m = re.match(r"(\w+)\s+([0-9a-f]+)$", line.strip())
            if m:
                out.setdefault(m.group(1), set()).add(int(m.group(2), 16))
    return out


def stale_hooks(tree):
    """The game modules whose generated C lacks hook targets the runtime listed (hooks.txt)."""
    import recomp as rc
    rc.TREE = tree
    out = []
    for mod, offs in sorted(hook_targets(Paths(tree)).items()):
        table = os.path.join(tree, "build", mod, "g_module.c") if mod in ("main", "subsdk0", "sdk") else \
            os.path.join(tree, "build", "nro", mod, "g_module.c")
        base = rc.MODULES.get(mod) or rc.nro_modules().get(mod) if os.path.exists(table) else None
        if not base:
            continue
        text = open(table, encoding="utf-8").read()
        if any(f"f_{base + o:x}_orig" not in text for o in offs):
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
    before = entries()
    env = dict(os.environ, SSBU_SKYLINE_PROBE="1", SDL_AUDIO_DRIVER="dummy", SSBU_CWD=tree, SSBU_TESTRUN="skyline-probe",
               SSBU_LOG=os.path.join(p.skyline, "probe.log"))
    log("starting the game once for the plugins' select-screen entries")
    try:
        subprocess.run([exe, os.path.join(tree, "dump", "modules")], env=env, timeout=180, cwd=tree)
    except subprocess.TimeoutExpired:
        log("the probe did not finish (see dump/skyline/probe.log)")
        return False
    return entries() != before


def build(tree, jobs, log=print):
    """sync, the C of the modules with new hook targets, the compile and link, then the probe and
    (if the plugins' characters changed) the mods applied again."""
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
    if probe(tree, log):
        log("the plugins' select-screen entries changed: applying the mods again")
        import mods
        mods.apply(mods.Tree(tree), log)
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
    else:
        sys.exit(__doc__)
