#include "support/pwm_control_host.h"
#include "unity/unity.h"

static void test_pwm_translate_throttle_maps_zero_to_pwm_min(void) {
    TEST_ASSERT_EQUAL_UINT16(PWM_MIN, pwm_translate_throttle(0));
}

static void test_pwm_translate_throttle_maps_midpoint_to_neutral(void) {
    TEST_ASSERT_EQUAL_UINT16(PWM_NEUTRAL, pwm_translate_throttle(1000));
}

static void test_pwm_translate_throttle_maps_max_to_pwm_max(void) {
    TEST_ASSERT_EQUAL_UINT16(PWM_MAX, pwm_translate_throttle(PWM_CMD_RANGE_MAX));
}

static void test_pwm_translate_throttle_clamps_values_above_range(void) {
    TEST_ASSERT_EQUAL_UINT16(PWM_MAX, pwm_translate_throttle(PWM_CMD_RANGE_MAX + 1));
}

static void test_pwm_translate_throttle_handles_boundary_values(void) {
    TEST_ASSERT_EQUAL_UINT16(1000, pwm_translate_throttle(1));
    TEST_ASSERT_EQUAL_UINT16(1250, pwm_translate_throttle(500));
    TEST_ASSERT_EQUAL_UINT16(1999, pwm_translate_throttle(1999));
}

void test_pwm_control(void) {
    RUN_TEST(test_pwm_translate_throttle_maps_zero_to_pwm_min);
    RUN_TEST(test_pwm_translate_throttle_maps_midpoint_to_neutral);
    RUN_TEST(test_pwm_translate_throttle_maps_max_to_pwm_max);
    RUN_TEST(test_pwm_translate_throttle_clamps_values_above_range);
    RUN_TEST(test_pwm_translate_throttle_handles_boundary_values);
}
