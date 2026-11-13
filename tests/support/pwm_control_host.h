#ifndef TESTS_SUPPORT_PWM_CONTROL_HOST_H
#define TESTS_SUPPORT_PWM_CONTROL_HOST_H

#include <stdint.h>

#define PWM_MIN 1000
#define PWM_NEUTRAL 1500
#define PWM_MAX 2000
#define PWM_CMD_RANGE_MAX 2000

uint16_t pwm_translate_throttle(uint16_t cmd);

#endif
