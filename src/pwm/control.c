#include "control.h"
#include <stdint.h>

uint16_t pwm_translate_throttle(uint16_t cmd) {
    if (cmd > PWM_CMD_RANGE_MAX) {
        cmd = PWM_CMD_RANGE_MAX;
    }
    return PWM_MIN + ((cmd * (PWM_MAX - PWM_MIN)) / PWM_CMD_RANGE_MAX);
}
