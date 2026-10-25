/* What the files of the Vulkan back end share across translation units. nvn_vk.c is still one unit with the files it
 * includes (nvn_vk_draw.c, which includes nvn_vk_m4.c and the others); a file that is a unit of its own reaches them
 * through this header, which the unit includes where that file used to be included. */
#ifndef NVN_VK_INT_H
#define NVN_VK_INT_H
#include <stddef.h>
#include <stdint.h>
#include <windows.h>
LONG64 tk(void);                                        /* nvn_vk_draw.c: performance counter ticks */
uint64_t fnv(uint64_t h, const void *p, size_t n);      /* nvn_vk_m4.c */
extern int g_m4_quit;                                   /* nvn_vk_m4.c: the game is closing */
extern volatile int g_store_pct;                        /* nvn_vk_m4.c: how much of data.arc the store has read */
int vks_build(const char *dir);                         /* nvn_vk_store.c: the container store, from data.arc */
#endif
