#!/usr/bin/env python3
"""Build the game for macOS on Apple Silicon: compile the recompiled game and the runtime.

Usage: build_macos.py [--port <dir>] [--out <dir>] [--jobs N] [--runtime-only] [--compile-only]

First draft, untested on a Mac. Follows tools/linux/build_linux.py. What it does now:
  - compiles the runtime (runtime/*.c, runtime/linux/* except the profiler, and runtime/macos/*)
  - compiles the recompiled game (<port>/build/{main,subsdk0,sdk,nro/*}), as build_linux.py does
  - links, if the graphics libraries are there (see link() for what it needs)

Not done yet, and where the first failures are expected:
  - the overlay font header: made on Windows with GDI, copied to <out>/gen/overlay_font.h
  - vkd3d-shader and DXVK for macOS: SSBU_VKD3D_MAC and SSBU_DXVK_MAC
  - the macOS link flags (see link())
"""
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from toolchain_macos import CFLAGS, CLANG, DEPS, INCLUDES, MISSING, REPO, RUNTIME, SDK, check  # noqa: E402


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PORT_DIR = REPO if os.path.isdir(os.path.join(REPO, "dump")) else os.path.join(os.path.dirname(REPO), "native-port")
PORT = os.path.abspath(arg("--port") or (os.path.join(PORT_DIR, "v13.0.5") if os.path.isdir(os.path.join(PORT_DIR, "v13.0.5", "build")) else PORT_DIR))
OUT = os.path.abspath(arg("--out", os.path.join(REPO, "build-macos")))
JOBS = int(arg("--jobs", str(max(4, (os.cpu_count() or 8) - 2))))
GEN = os.path.join(OUT, "gen")

RUNTIME_INCLUDES = INCLUDES + ["-I", os.path.join(RUNTIME, "linux", "include"), "-I", GEN]
RUNTIME_DEFINES = ["-D_REENTRANT", "-DSSBU_HAVE_OPUS", "-DSSBU_HAVE_VPX", "-DSSBU_HAVE_SDL2"]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def compile_each(job):
    """Compile one (src, obj, opt[, flags]) job; returns (src, error text) on failure."""
    src, obj, opt = job[:3]
    flags = job[3] if len(job) > 3 else []
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    lang = ["-x", "c++", "-std=gnu++20", "-fexceptions"] if src.endswith((".cpp", ".cc")) or "c++" in flags else []
    flags = [f for f in flags if f != "c++"]
    code, out = run([CLANG, "-c", opt, *CFLAGS, *lang, *flags, src, "-o", obj])
    return (src, out) if code else None


def needs(obj, *srcs):
    return not os.path.exists(obj) or os.path.getmtime(obj) < max(os.path.getmtime(s) for s in srcs)


def modules():
    """(name, generated C directory, output directory) for every module the Windows build has."""
    build = os.path.join(PORT, "build")
    found = [(n, os.path.join(build, n), os.path.join(OUT, n)) for n in ("main", "subsdk0", "sdk")]
    nro = os.path.join(build, "nro")
    if os.path.isdir(nro):
        found += [(n, os.path.join(nro, n), os.path.join(OUT, "nro", n)) for n in sorted(os.listdir(nro))
                  if os.path.exists(os.path.join(nro, n, "manifest.tsv"))]
    return [m for m in found if os.path.exists(os.path.join(m[1], "manifest.tsv"))]


def has_landing_sites(path):
    with open(path, "rb") as f:
        return b"__try" in f.read()


def game_jobs():
    recomp_h = os.path.join(RUNTIME, "recomp.h")
    jobs = []
    for _name, src_dir, out_dir in modules():
        with open(os.path.join(src_dir, "manifest.tsv")) as f:
            rows = [line.split("\t") for line in f.read().splitlines() if line]
        for fname, _insns, biggest in rows + [["g_module.c", "0", "0"]]:
            src = os.path.join(src_dir, fname)
            obj = os.path.join(out_dir, fname[:-2] + ".o")
            opt = "-O1" if int(biggest) > 20000 or fname == "g_module.c" else "-O2"
            if not needs(obj, src, recomp_h):
                continue
            if fname != "g_module.c" and has_landing_sites(src):
                # guest C++ try/catch: compiled as C++ (see recomp.h), as on Linux
                if int(biggest) > 50000:
                    opt = "-O0"
                wrapper = os.path.join(out_dir, fname[:-2] + "_cxx.cpp")
                os.makedirs(out_dir, exist_ok=True)
                text = '#include "recomp.h"\nextern "C" {\n#include "%s"\n}\n' % src.replace("\\", "/")
                if not os.path.exists(wrapper) or open(wrapper).read() != text:
                    with open(wrapper, "w") as f:
                        f.write(text)
                jobs.append((wrapper, obj, opt))
            else:
                jobs.append((src, obj, opt))
    return jobs


def hle_names():
    names = set()
    for n in os.listdir(RUNTIME):
        if n.endswith(".c"):
            with open(os.path.join(RUNTIME, n), encoding="utf-8") as f:
                names |= set(re.findall(r"^HLE\((\w+)\)", f.read(), re.M))
    return sorted(names)


def hle_wraps():
    found = set()
    for n in os.listdir(RUNTIME):
        if n.endswith(".c"):
            with open(os.path.join(RUNTIME, n), encoding="utf-8") as f:
                found |= {int(a, 16) for a in re.findall(r"^HLE_WRAP\(([0-9a-fA-F]+)\)", f.read(), re.M)}
    return sorted(found)


def write_if_changed(path, text):
    if os.path.exists(path) and open(path, encoding="utf-8").read() == text:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def runtime_sources():
    """runtime/*.c (the core), runtime/linux/* (the platform layer, minus the profiler), runtime/macos/*."""
    core = [os.path.join(RUNTIME, n) for n in sorted(os.listdir(RUNTIME))
            if re.fullmatch(r"(host|kernel|gpu\w*|hle\w*|ipc\w*|net\w*|debug_watchdog)\.c", n)]
    linux = [os.path.join(RUNTIME, "linux", n) for n in sorted(os.listdir(os.path.join(RUNTIME, "linux")))
             if n.endswith((".c", ".cpp")) and n != "profile.c"]
    mac = [os.path.join(RUNTIME, "macos", n) for n in sorted(os.listdir(os.path.join(RUNTIME, "macos")))
           if n.endswith(".c")]
    return core + linux + mac


def runtime_jobs():
    os.makedirs(GEN, exist_ok=True)
    rt_out = os.path.join(OUT, "rt")
    headers = [os.path.join(RUNTIME, n) for n in os.listdir(RUNTIME) if n.endswith(".h")]
    jobs = []
    for src in runtime_sources():
        obj = os.path.join(rt_out, os.path.basename(src).rsplit(".", 1)[0] + ".o")
        if needs(obj, src, *headers):
            jobs.append((src, obj, "-O2", RUNTIME_INCLUDES + RUNTIME_DEFINES))
    names = hle_names()
    table = os.path.join(GEN, "hle_table.c")
    changed = write_if_changed(table, '#include "host.h"\n\n' + "".join(f"GuestFn hle_{n};\n" for n in names)
                               + "const HleEntry hle_table[] = {\n" + "".join(f'    {{"{n}", hle_{n}}},\n' for n in names)
                               + f"    {{0, 0}}\n}};\nconst u32 hle_count = {len(names)};\n")
    if changed or needs(os.path.join(rt_out, "hle_table.o"), table, *headers):
        jobs.append((table, os.path.join(rt_out, "hle_table.o"), "-O1", RUNTIME_INCLUDES + RUNTIME_DEFINES))
    missing = os.path.join(GEN, "wrap_missing.c")
    changed = write_if_changed(missing, '#include "host.h"\n\nvoid rt_wrap_missing(Cpu *c);\n'
                               + "".join(f"__attribute__((weak)) void f_{a:x}_orig(Cpu *c) {{ rt_wrap_missing(c); }}\n"
                                         for a in hle_wraps()))
    if changed or needs(os.path.join(rt_out, "wrap_missing.o"), missing, *headers):
        jobs.append((missing, os.path.join(rt_out, "wrap_missing.o"), "-O1", RUNTIME_INCLUDES + RUNTIME_DEFINES))
    nros = [n for n, src, _o in modules() if os.path.basename(os.path.dirname(src)) == "nro"]
    nro_table = os.path.join(GEN, "nro_table.c")
    changed = write_if_changed(nro_table, '#include "module.h"\n\n' + "".join(f"extern const RtModule rt_module_{n};\n" for n in nros)
                               + "const RtModule *const rt_nro_modules[] = {" + "".join(f"&rt_module_{n}, " for n in nros) + "0};\n"
                               + f"const u32 rt_nro_count = {len(nros)};\n")
    if changed or needs(os.path.join(rt_out, "nro_table.o"), nro_table):
        jobs.append((nro_table, os.path.join(rt_out, "nro_table.o"), "-O1", ["-I", RUNTIME]))
    return jobs


def compile_all(jobs, what):
    if not jobs:
        print(f"{what}: up to date")
        return True
    t0 = time.time()
    failures = []
    jobs = sorted(jobs, key=lambda j: os.path.getsize(j[0]), reverse=True)   # biggest first
    with ThreadPoolExecutor(JOBS) as pool:
        futures = [pool.submit(compile_each, job) for job in jobs]
        for done, fut in enumerate(as_completed(futures), 1):
            result = fut.result()
            if result:
                failures.append(result)
            if done % 64 == 0 or done == len(jobs):
                print(f"{what}: {done}/{len(jobs)} compiled, {time.time() - t0:,.0f}s", flush=True)
    for src, err in failures[:8]:
        print(f"FAILED {src}:\n{err[:3000]}")
    return not failures


def link():
    """Link the program. Needs vkd3d-shader and DXVK for macOS, which this build doesn't make yet:
    SSBU_VKD3D_MAC (libvkd3d-shader.a built for arm64 macOS) and SSBU_DXVK_MAC (the folder holding
    libdxvk_d3d11.dylib and libdxvk_dxgi.dylib). Paths the Linux build uses are in build_linux.py."""
    vkd3d = os.environ.get("SSBU_VKD3D_MAC")
    dxvk = os.environ.get("SSBU_DXVK_MAC")
    if not vkd3d or not dxvk:
        print("not linking yet: set SSBU_VKD3D_MAC (libvkd3d-shader.a for arm64 macOS) and SSBU_DXVK_MAC "
              "(the folder with libdxvk_d3d11.dylib and libdxvk_dxgi.dylib). Objects are compiled.")
        return False
    objs = []
    rt_out = os.path.join(OUT, "rt")
    objs += sorted(os.path.join(rt_out, n) for n in os.listdir(rt_out) if n.endswith(".o"))
    for _name, _src, out_dir in modules():
        objs += sorted(os.path.join(out_dir, n) for n in os.listdir(out_dir) if n.endswith(".o"))
    exe = os.path.join(OUT, "ssbu")
    libs = [os.path.join(DEPS["opus"], "lib", "libopus.a") if DEPS["opus"] else "-lopus",
            os.path.join(DEPS["libvpx"], "lib", "libvpx.a") if DEPS["libvpx"] else "-lvpx"]
    frameworks = ["-framework", "Cocoa", "-framework", "Metal", "-framework", "QuartzCore",
                  "-framework", "IOKit", "-framework", "CoreAudio", "-framework", "AudioToolbox",
                  "-framework", "CoreVideo", "-framework", "CoreHaptics", "-framework", "GameController"]
    cmd = [CLANG, *CFLAGS, "-isysroot", SDK, *objs, vkd3d, *libs,
           "-L" + os.path.join(DEPS["sdl2"], "lib") if DEPS["sdl2"] else "-lSDL2",
           "-lSDL2", "-L" + dxvk, "-ldxvk_d3d11", "-ldxvk_dxgi", "-lc++", "-lm", "-lpthread",
           "-Wl,-rpath," + dxvk, *frameworks, "-o", exe]
    code, out = run(cmd)
    if code:
        print("link failed\n" + out[:6000])
        return False
    print(f"linked {len(objs)} objects into {exe}")
    return True


def main():
    os.makedirs(OUT, exist_ok=True)
    check()   # the SDK, clang and Homebrew packages; stops if any is missing
    if not modules():
        raise SystemExit(f"no recompiled game in {os.path.join(PORT, 'build')} (tools/recomp.py all)")
    overlay = os.path.join(GEN, "overlay_font.h")
    if not os.path.exists(overlay):
        raise SystemExit(f"{overlay} is missing: copy the overlay_font.h the Windows build makes into it (see the module docstring)")
    print(f"game tree: {PORT}")
    if not compile_all(runtime_jobs(), "runtime"):
        return False
    if "--runtime-only" in sys.argv:
        return True
    if not compile_all(game_jobs(), "game"):
        return False
    if "--compile-only" in sys.argv:
        return True
    return link()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
