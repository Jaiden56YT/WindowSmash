/* The sampling profiler, on macOS: not ported yet.
 *
 * runtime/linux/profile.c samples threads with SIGRTMIN signals, tgkill, gettid and the x86-64
 * register layout, none of which macOS has. Until that's ported, these are no-ops with the same
 * names, so the renderer and watchdog build unchanged. F6 (profile_start) says so on the console. */
#include <stdio.h>

#include "debug_profile.h"

void profile_start(double seconds) {
    (void)seconds;
    fprintf(stderr, "[profile] the sampling profiler is not available on macOS yet\n");
}

int profile_running(void) { return 0; }

int profile_dump_threads(void (*line)(const char *text)) {
    (void)line;
    return 0;
}

void hitch_present(void) {}
