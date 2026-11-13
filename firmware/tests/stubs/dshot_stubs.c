#include "../support/dshot_stubs_host.h"

#if defined(__GNUC__) || defined(__clang__)
#define TEST_WEAK __attribute__((weak))
#else
#define TEST_WEAK
#endif

TEST_WEAK void dshot_command(struct dshot_controller *controller, uint16_t channel,
                             uint16_t command, uint8_t repeat_count) {
    if (controller == 0 || channel >= DSHOT_MAX_CHANNELS) {
        return;
    }

    controller->motor[channel].current_command = command;
    controller->motor[channel].command_counter = repeat_count;
}

TEST_WEAK void dshot_throttle(struct dshot_controller *controller, uint16_t channel,
                              uint16_t throttle) {
    if (controller == 0 || channel >= DSHOT_MAX_CHANNELS) {
        return;
    }

    controller->motor[channel].last_throttle_value = throttle;
}

TEST_WEAK void dshot_loop(struct dshot_controller *controller) {
    (void)controller;
}

TEST_WEAK bool dshot_is_telemetry_active(const struct dshot_controller *controller) {
    (void)controller;
    return true;
}

TEST_WEAK void dshot_telemetry_usb_flush(void) {}
