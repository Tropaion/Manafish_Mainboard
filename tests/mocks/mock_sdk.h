#ifndef TESTS_MOCK_SDK_H
#define TESTS_MOCK_SDK_H

#include <stdbool.h>
#include <stdint.h>

typedef unsigned int uint;
typedef struct mock_pio_instance {
    uint32_t dummy;
} mock_pio_instance;

typedef struct mock_pio_instance *PIO;

typedef struct {
    uint32_t dummy;
} pio_sm_config;

struct pio_program {
    uint16_t length;
};

#endif
