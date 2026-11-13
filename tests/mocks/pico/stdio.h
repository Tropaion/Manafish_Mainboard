#ifndef MOCK_PICO_STDIO_H
#define MOCK_PICO_STDIO_H

#include <stdint.h>

#define PICO_ERROR_TIMEOUT (-1)

static inline int getchar_timeout_us(uint32_t timeout_us) {
    (void)timeout_us;
    return PICO_ERROR_TIMEOUT;
}

#endif
