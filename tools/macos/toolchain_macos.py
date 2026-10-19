"""Cross/native toolchain for the macOS (Apple Silicon, arm64) build.

Untested on a Mac so far: written from tools/linux/toolchain.py. Run `python3 tools/macos/toolchain_macos.py`
on the Mac to print what it found.

Everything comes from the Mac itself:
  Xcode Command Line Tools  clang, ld64, and the macOS SDK     (xcode-select --install)
  Homebrew                  sdl2, molten-vk, libopus, libvpx  (brew install ...)

Graphics: DXVK's native libraries (Direct3D 11 on Vulkan) run on MoltenVK (Vulkan on Metal).
See docs/MACOS.md (to be written) for the graphics decision.
"""
import glob
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RUNTIME = os.path.join(REPO, "runtime")

MIN_MACOS = "13.0"  # MoltenVK and SDL2 both support macOS 13 and later on Apple Silicon
TARGET = f"--target=arm64-apple-macos{MIN_MACOS}"


def _run_out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _find_sdk():
    sdk = os.environ.get("SSBU_MACOS_SDK") or _run_out(["xcrun", "--sdk", "macosx", "--show-sdk-path"])
    if not sdk or not os.path.isdir(sdk):
        raise SystemExit("no macOS SDK found: run `xcode-select --install`, or set SSBU_MACOS_SDK")
    return sdk


def _find_clang():
    clang = os.environ.get("SSBU_MACOS_CLANG") or _run_out(["xcrun", "--find", "clang"])
    if not clang or not os.path.exists(clang):
        raise SystemExit("no clang found: run `xcode-select --install`, or set SSBU_MACOS_CLANG")
    return clang


def _brew_prefix(formula):
    """Install prefix of a Homebrew formula (e.g. 'sdl2'), or '' if it isn't installed."""
    brew = shutil.which("brew") or ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew") else None)
    if not brew:
        return ""
    prefix = _run_out([brew, "--prefix", formula])
    return prefix if prefix and os.path.isdir(prefix) else ""


SDK = _find_sdk()
CLANG = _find_clang()

# Homebrew prefixes (Apple Silicon Homebrew lives in /opt/homebrew).
SDL2 = _brew_prefix("sdl2")
MOLTENVK = _brew_prefix("molten-vk")
OPUS = _brew_prefix("opus")
VPX = _brew_prefix("libvpx")
DEPS = {"sdl2": SDL2, "molten-vk": MOLTENVK, "opus": OPUS, "libvpx": VPX}
MISSING = sorted(name for name, path in DEPS.items() if not path)

# Guest FP semantics need IEEE-exact arithmetic: no contraction, no fast-math (as on Windows).
# Clang on arm64 contracts a*b+c into FMA by default, so -ffp-contract=off is essential here.
# The lifted SIMD code (tools/lift_simd.py) is portable C, so no architecture flags are needed:
# the compiler emits NEON from it. The x86-only flags of the Linux build (-mavx2 etc.) are dropped.
CFLAGS = [TARGET, "-isysroot", SDK, "-fno-strict-aliasing", "-ffp-contract=off", "-fno-pic", "-w",
          "-fasynchronous-unwind-tables", "-I", RUNTIME]

INCLUDES = [f"-I{prefix}/include" for prefix in (SDL2, MOLTENVK, OPUS, VPX) if prefix]
LIBDIRS = [f"-L{prefix}/lib" for prefix in (SDL2, MOLTENVK, OPUS, VPX) if prefix]


def check():
    """Print what was found; the build refuses to start while anything is missing."""
    print("SDK:    ", SDK)
    print("clang:  ", CLANG)
    for name, path in DEPS.items():
        print(f"{name:<9}", path or "MISSING (brew install " + name + ")")
    if MISSING:
        raise SystemExit("missing Homebrew packages: " + ", ".join(MISSING))
    print("OK")


if __name__ == "__main__":
    check()
