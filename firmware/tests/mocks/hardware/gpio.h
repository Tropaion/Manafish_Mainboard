#ifndef MOCK_HARDWARE_GPIO_H
#define MOCK_HARDWARE_GPIO_H

#include "../mock_sdk.h"

#define GPIO_FUNC_SIO 0
#define GPIO_FUNC_PWM 4
#define GPIO_IN 0
#define GPIO_OUT 1
#define MOCK_GPIO_CALL_LOG_SIZE 32

typedef uint mock_gpio_uint_t;

enum gpio_drive_strength {
    GPIO_DRIVE_STRENGTH_2MA = 0,
    GPIO_DRIVE_STRENGTH_4MA = 1,
    GPIO_DRIVE_STRENGTH_8MA = 2,
    GPIO_DRIVE_STRENGTH_12MA = 3,
};

typedef enum {
    MOCK_GPIO_CALL_INIT,
    MOCK_GPIO_CALL_DRIVE_STRENGTH,
    MOCK_GPIO_CALL_PUT,
    MOCK_GPIO_CALL_DIR,
    MOCK_GPIO_CALL_FUNCTION,
} mock_gpio_call_kind_t;

typedef struct {
    mock_gpio_call_kind_t kind;
    uint pin;
    int value;
} mock_gpio_call_t;

extern int mock_gpio_function[30];
extern int mock_gpio_direction[30];
extern int mock_gpio_value[30];
extern bool mock_gpio_pwm_enabled_when_selected[30];
extern bool mock_pwm_enabled[8];
extern mock_gpio_call_t mock_gpio_calls[MOCK_GPIO_CALL_LOG_SIZE];
extern int mock_gpio_call_count;

static inline void mock_gpio_record(mock_gpio_call_kind_t kind, uint pin, int value) {
    if (mock_gpio_call_count < MOCK_GPIO_CALL_LOG_SIZE) {
        mock_gpio_calls[mock_gpio_call_count++] = (mock_gpio_call_t){kind, pin, value};
    }
}

static inline void gpio_init(uint pin) {
    mock_gpio_function[pin] = GPIO_FUNC_SIO;
    mock_gpio_direction[pin] = GPIO_IN;
    mock_gpio_value[pin] = 0;
    mock_gpio_record(MOCK_GPIO_CALL_INIT, pin, 0);
}

static inline void gpio_set_drive_strength(uint pin, enum gpio_drive_strength drive) {
    mock_gpio_record(MOCK_GPIO_CALL_DRIVE_STRENGTH, pin, (int)drive);
}

static inline void gpio_set_pulls(uint pin, bool up, bool down) {
    (void)pin;
    (void)up;
    (void)down;
}

static inline void gpio_set_function(uint pin, int fn) {
    mock_gpio_function[pin] = fn;
    mock_gpio_record(MOCK_GPIO_CALL_FUNCTION, pin, fn);
    if (fn == GPIO_FUNC_PWM) {
        mock_gpio_pwm_enabled_when_selected[pin] = mock_pwm_enabled[(pin >> 1U) & 7U];
    }
}

static inline void gpio_disable_pulls(uint pin) {
    (void)pin;
}

static inline void gpio_set_dir(uint pin, int out) {
    mock_gpio_direction[pin] = out;
    mock_gpio_record(MOCK_GPIO_CALL_DIR, pin, out);
}

static inline void gpio_put(uint pin, int value) {
    mock_gpio_value[pin] = value;
    mock_gpio_record(MOCK_GPIO_CALL_PUT, pin, value);
}

#endif
