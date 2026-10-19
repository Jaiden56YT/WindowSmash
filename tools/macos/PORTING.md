# macOS port: inventory

Working notes for the Apple Silicon (arm64) port, from reading `runtime/` and `runtime/linux/`.
Not user documentation. Nothing here has been compiled for macOS yet.

## What is already portable

- The guest code: `tools/recomp.py` output is plain C. On arm64 it compiles to native arm64,
  so no translation is needed for the game itself.
- SIMD: `tools/lift_simd.py` emits per-lane C loops, not x86 intrinsics. The compiler maps them
  to NEON. The Linux build's x86 flags (`-mavx2 -mfma -mf16c -mbmi -mbmi2 -mlzcnt -mpopcnt`)
  are dropped for arm64.
- Floating point: `-ffp-contract=off` must stay. Clang on arm64 contracts `a*b+c` into FMA by
  default, which would change guest results.
- Most of `runtime/*.c` (HLE services, GPU command parsing, network relay) uses only standard C
  and POSIX-ish calls. It should compile unchanged; each compile error is a concrete item.
- C++ exceptions for guest try/catch (`runtime/linux/eh.cpp`) use the standard C++ runtime,
  which exists on macOS as libc++.

## What must change for macOS

Ranked by how much work or risk each one carries.

### 1. Write-watch (guest memory dirty tracking): use the fallback only

`runtime/linux/win32.c` has two modes:

- `WW_SCAN`: userfaultfd (`SYS_userfaultfd`), `/proc/self/pagemap`, `PAGEMAP_SCAN`. Linux-only.
- `WW_PROTECT`: `mprotect` plus a SIGSEGV handler. This is the existing fallback, and the code
  calls it "the fallback, for testing".

macOS has neither userfaultfd nor `/proc`. Plan: always use `WW_PROTECT` on macOS, with no
`ww_init` probing. Check that `rt_ww_fault` works with the signal handler the mac host installs.

### 2. Futex-based condition variables and sync

`runtime/linux/win32.c` implements `WakeConditionVariable`, `WakeAllConditionVariable` and
`SleepConditionVariableCS` with a raw `syscall(SYS_futex, ...)`. macOS has no futex.

Status: done in `runtime/linux/win32.c` under `#ifdef __APPLE__`, using `__ulock_wait` and
`__ulock_wake` (untested; not compiled here because DXVK's headers aren't in this sandbox).
The Linux path is unchanged. The `pthread_mutex_t` layout check is also guarded for macOS.

Other options considered:
- `os_sync_wait_on_address` / `os_sync_wake_by_address_*` (macOS 14.4 and later). Public, but
  the target is macOS 13 in `toolchain_macos.py` now.
- `__ulock_wait` / `__ulock_wake`: private, but used widely and works on macOS 13 and later.
- A `pthread_mutex` plus `pthread_cond` pair per condition variable. Simplest, but changes the
  structure of `CONDITION_VARIABLE`.

Decision needed: which option, and whether to raise the minimum macOS version.

### 3. Memory mapping flags

`win32.c` uses `MAP_FIXED_NOREPLACE` (Linux 4.17+ only), `MAP_NORESERVE` and `MAP_STACK`.
Check on the Mac SDK which of these exist. For `MAP_FIXED_NOREPLACE`: a hint address without
`MAP_FIXED`, then check the returned address, is the usual substitute.

### 4. Signal handling reads x86 registers

`runtime/host.c` (`on_fault`, around line 428) reads `uc->uc_mcontext.gregs[REG_ERR]` and
`gregs[REG_RIP]`. `runtime/linux/profile.c` (around line 369) reads `gregs[REG_RIP]`. These are
x86-64 Linux layouts. On macOS arm64 the context is `uc_mcontext->__ss.__pc`, `__es.__far` and
similar, and the write-fault bit (`REG_ERR & 2`) has no direct equivalent.

On arm64, the write-fault check can use the exception syndrome in `__es.__esr` (the WnR bit).
That needs its own small platform function, `rt_fault_info(ucontext, &pc, &is_write)`, with
one implementation per platform.

The guest-address-in-diagnostic lookup should work the same once `pc` is correct.

### 5. Thread naming

`pthread_setname_np(name)` on Linux takes `(thread, name)`. On macOS it takes only the name, and
only for the current thread. Wrap it in the platform layer.

### 6. Entropy and misc

- `sys/random.h` `getrandom`: use `getentropy` or `arc4random_buf` on macOS.
- `linux/futex.h` and `linux/userfaultfd.h` are included at the top of `win32.c`. Remove them.
- `sched.h` / `sched_yield`: exists on macOS, but check the affinity calls, which don't.
- `execinfo.h` (`backtrace`): exists on macOS.

### 7. Graphics

`DXVK` is the Vulkan-based D3D11 layer. DXVK doesn't officially support macOS. The plan is
DXVK's native libraries on MoltenVK (Vulkan on Metal). This is unverified: the build may fail,
and the first thing to try is building DXVK's native libraries against MoltenVK on the Mac.
The fallback is a native Metal backend behind `runtime/gpu_backend.h`.

### 8. Build

- `tools/linux/build_linux.py` and `tools/linux/toolchain.py` target `x86_64-linux-gnu` with a
  glibc sysroot. A macOS driver needs `arm64-apple-macos13` and the Xcode SDK.
  `tools/macos/toolchain_macos.py` covers the toolchain discovery.
- Linking: `-fuse-ld=lld` with `-no-pie` is Linux-specific. On macOS use Apple's `ld64`
  (or `ld64.lld`), and `-Wl,-no_pie` only if the guest needs a fixed address.
- The guest is linked at fixed addresses (`-fno-pic`). On arm64 macOS, check whether the binary
  can be linked at the addresses the game expects. This may be the hardest link-time issue.

## Open questions

1. Minimum macOS version? (Currently 13.0 in `toolchain_macos.py`.)
2. Futex replacement: `__ulock` (works now) or `os_sync` (needs 14.4 and cleaner API)?
3. Graphics: MoltenVK-based DXVK first, as planned, or go straight to Metal?
4. Fixed-address linking on macOS: does the program need the same addresses as on Linux?

## Status log

- **Done (untested):** Futex condition variables use `__ulock` on macOS (`runtime/linux/win32.c`).
- **Done (untested):** Write watch uses the mprotect fallback on macOS. The userfaultfd and
  PAGEMAP_SCAN code is under `#ifndef __APPLE__`, with stubs for the rest of the file.
- **Still Linux-only in `runtime/linux/win32.c`:**
  - `sys/random.h` and `getrandom` (around line 1222): use `getentropy` on macOS.
  - `/proc/self/maps` in `VirtualQuery` (around line 755): needs Mach `vm_region` on macOS.
  - `/proc/self/status` (around line 1246): needs a macOS equivalent.
  - `MAP_FIXED_NOREPLACE` (around line 688) and `MAP_NORESERVE` (lines 565, 688): check the
    macOS SDK.
  - `pthread_setname_np` (around line 470): macOS takes only the name.
  - `sysconf(_SC_NPROCESSORS_ONLN)` (around line 789): exists on macOS.
  - `sys/syscall.h` is still included; it's harmless on macOS but should be guarded.
- **Done (untested):** the crash handler in `runtime/host.c` reads the program counter and
  write flag through `fault_context`, with the arm64 and x86-64 macOS layouts. `gettid` is
  replaced by `rt_tid`.
- **Done (compiles):** the sampling profiler is stubbed out on macOS in
  `runtime/macos/profile_stub.c`. The macOS build should use this file instead of
  `runtime/linux/profile.c`. Porting the profiler is later work.
- **Still to do:** the macOS build driver and the graphics path.
