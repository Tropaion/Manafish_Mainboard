#ifndef MOCK_PICO_TIME_H
#define MOCK_PICO_TIME_H

#include "types.h"
#include <stdint.h>

static inline absolute_time_t get_absolute_time(void) {
    return 0;
}
static inline int64_t absolute_time_diff_us(absolute_time_t from, absolute_time_t to) {
    return (int64_t)(to - from);
}
static inline absolute_time_t delayed_by_ms(absolute_time_t t, uint32_t ms) {
    return t + ((absolute_time_t)ms * 1000u);
}
static inline absolute_time_t delayed_by_us(absolute_time_t t, uint64_t us) {
    return t + (absolute_time_t)us;
}
static inline absolute_time_t make_timeout_time_us(uint64_t us) {
    return (absolute_time_t)us;
}
static inline uint32_t to_ms_since_boot(absolute_time_t t) {
    return (uint32_t)(t / 1000u);
}
static inline void sleep_us(uint64_t us) {
    (void)us;
}
static inline void sleep_ms(uint32_t ms) {
    (void)ms;
}

#endif
