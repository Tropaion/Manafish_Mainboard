#ifndef MOCK_HARDWARE_PIO_H
#define MOCK_HARDWARE_PIO_H

#include "../mock_sdk.h"

typedef PIO mock_pio_handle_t;
typedef pio_sm_config mock_pio_sm_config_t;

static mock_pio_instance mock_pio0_instance;
static mock_pio_instance mock_pio1_instance;

#define pio0 (&mock_pio0_instance)
#define pio1 (&mock_pio1_instance)

static inline uint pio_add_program(PIO pio, const struct pio_program *program) {
    (void)pio;
    (void)program;
    return 0;
}

static inline void sm_config_set_out_pins(pio_sm_config *c, uint pin, uint count) {
    (void)c;
    (void)pin;
    (void)count;
}

static inline void sm_config_set_set_pins(pio_sm_config *c, uint pin, uint count) {
    (void)c;
    (void)pin;
    (void)count;
}

static inline void sm_config_set_in_pins(pio_sm_config *c, uint pin) {
    (void)c;
    (void)pin;
}

static inline void sm_config_set_jmp_pin(pio_sm_config *c, uint pin) {
    (void)c;
    (void)pin;
}

static inline void pio_gpio_init(PIO pio, uint pin) {
    (void)pio;
    (void)pin;
}

static inline void sm_config_set_out_shift(pio_sm_config *c, bool shift_right, bool autopull,
                                           uint pull_threshold) {
    (void)c;
    (void)shift_right;
    (void)autopull;
    (void)pull_threshold;
}

static inline void sm_config_set_in_shift(pio_sm_config *c, bool shift_right, bool autopush,
                                          uint push_threshold) {
    (void)c;
    (void)shift_right;
    (void)autopush;
    (void)push_threshold;
}

static inline void sm_config_set_clkdiv(pio_sm_config *c, float div) {
    (void)c;
    (void)div;
}

static inline void pio_sm_init(PIO pio, uint sm, uint offset, const pio_sm_config *config) {
    (void)pio;
    (void)sm;
    (void)offset;
    (void)config;
}

static inline void pio_sm_set_enabled(PIO pio, uint sm, bool enabled) {
    (void)pio;
    (void)sm;
    (void)enabled;
}

static inline void pio_sm_clear_fifos(PIO pio, uint sm) {
    (void)pio;
    (void)sm;
}

static inline void pio_sm_restart(PIO pio, uint sm) {
    (void)pio;
    (void)sm;
}

static inline bool pio_sm_is_tx_fifo_empty(PIO pio, uint sm) {
    (void)pio;
    (void)sm;
    return true;
}

static inline void pio_sm_put(PIO pio, uint sm, uint32_t data) {
    (void)pio;
    (void)sm;
    (void)data;
}

static inline bool pio_sm_is_rx_fifo_empty(PIO pio, uint sm) {
    (void)pio;
    (void)sm;
    return true;
}

static inline uint32_t pio_sm_get(PIO pio, uint sm) {
    (void)pio;
    (void)sm;
    return 0;
}

#endif
