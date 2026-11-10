#ifndef LV_CONF_H
#define LV_CONF_H
#define LV_COLOR_DEPTH 32
#define LV_MEM_SIZE (1024 * 1024U)
#define LV_USE_LOG 1
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_18 1
#define LV_USE_SNAPSHOT 1
#if USE_DESKTOP
#define LV_USE_SDL 1
#else
#define LV_USE_LINUX_FBDEV 1
#endif
#endif
