#ifndef MOCK_HARDWARE_CLOCKS_H
#define MOCK_HARDWARE_CLOCKS_H

#include "../pico/types.h"

enum clock_index {
    clk_sys,
};

typedef absolute_time_t mock_clock_absolute_time_t;

static inline uint32_t clock_get_hz(enum clock_index clk_index) {
    (void)clk_index;
    return 125000000u;
}

#endif
