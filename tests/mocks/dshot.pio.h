#ifndef MOCK_DSHOT_PIO_H
#define MOCK_DSHOT_PIO_H

#include <hardware/pio.h>

static const struct pio_program pio_dshot_program = {.length = 0};

static inline pio_sm_config pio_dshot_program_get_default_config(uint offset) {
    (void)offset;
    pio_sm_config c = {0};
    return c;
}

#endif
