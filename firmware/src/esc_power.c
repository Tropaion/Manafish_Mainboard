#include "esc_power.h"
#include <stdbool.h>
#include <stdint.h>

// Include it unconditionally, also where nothing from it is used (pico/pico2): it pulls in pico.h
// and with it the board header that defines MANAFISH_ESC_POWER_PIN (the host tests get
// tests/mocks/hardware/gpio.h). esc_power_wire_state() below makes a miss fail to link.
#include <hardware/gpio.h> // IWYU pragma: keep

static esc_power_state_t state = ESC_POWER_STATE_ABSENT;
// GP14 fall (OFF_HOLD), GP14 rise (STARTING, ARMING, ON) or stop request (STOPPING).
static uint32_t since_ms;
static uint32_t busy_reasons;
static uint32_t host_activity_ms;
static bool level;
// STOPPING has parked the lines; GP14 drops on the next service call.
static bool stop_parked;

// Wrap-safe. A start up to 24 days ahead of now (a time read before it) never counts as elapsed.
static bool reached(uint32_t now_ms, uint32_t start_ms, uint32_t duration_ms) {
    uint32_t elapsed = now_ms - start_ms;
    return elapsed >= duration_ms && elapsed <= (uint32_t)INT32_MAX;
}

static void drive(bool high) {
    level = high;
#ifdef MANAFISH_ESC_POWER_PIN
    gpio_put(MANAFISH_ESC_POWER_PIN, high);
#endif
}

static void switch_off(uint32_t now_ms) {
    drive(false);
    state = ESC_POWER_STATE_OFF_HOLD;
    since_ms = now_ms;
}

static void stop(uint32_t now_ms) {
    if (state == ESC_POWER_STATE_STARTING) {
        switch_off(now_ms);
    } else if (state == ESC_POWER_STATE_ARMING || state == ESC_POWER_STATE_ON) {
        state = ESC_POWER_STATE_STOPPING;
        since_ms = now_ms;
        stop_parked = false;
    }
}

void esc_power_init(uint32_t now_ms) {
#ifdef MANAFISH_ESC_POWER_PIN
    gpio_init(MANAFISH_ESC_POWER_PIN);
    gpio_set_drive_strength(MANAFISH_ESC_POWER_PIN, GPIO_DRIVE_STRENGTH_12MA);
    gpio_put(MANAFISH_ESC_POWER_PIN, 0);
    gpio_set_dir(MANAFISH_ESC_POWER_PIN, GPIO_OUT);
    state = ESC_POWER_STATE_OFF_HOLD;
    since_ms = now_ms;
    busy_reasons = 0;
    host_activity_ms = now_ms;
    level = false;
#else
    (void)now_ms;
#endif
}

esc_power_result_t esc_power_request(uint32_t on, uint32_t now_ms) {
    if (state == ESC_POWER_STATE_ABSENT) {
        return ESC_POWER_RESULT_UNSUPPORTED;
    }
    host_activity_ms = now_ms;
    if (on > 1u) {
        return ESC_POWER_RESULT_INVALID;
    }
    if (on == 0u) {
        // The ESCs wait in the AM32 bootloader: keep them powered for RECOVER_BEGIN and COMMIT.
        // A later power-up on parked (low) lines would start the partly programmed application.
        if ((busy_reasons & ESC_POWER_BUSY_RECOVERY) != 0u) {
            return ESC_POWER_RESULT_BUSY;
        }
        stop(now_ms);
        return ESC_POWER_RESULT_APPLIED;
    }
    if (state == ESC_POWER_STATE_STARTING || state == ESC_POWER_STATE_ARMING ||
        state == ESC_POWER_STATE_ON) {
        return ESC_POWER_RESULT_APPLIED;
    }
    if (state != ESC_POWER_STATE_OFF_READY || busy_reasons != 0u) {
        return ESC_POWER_RESULT_BUSY;
    }
    state = ESC_POWER_STATE_STARTING;
    since_ms = now_ms;
    drive(true);
    return ESC_POWER_RESULT_APPLIED;
}

void esc_power_service(uint32_t now_ms) {
    if ((busy_reasons & ESC_POWER_BUSY_RECOVERY) != 0u) {
        host_activity_ms = now_ms;
    } else if (reached(now_ms, host_activity_ms, ESC_POWER_HOST_LOSS_MS)) {
        stop(now_ms);
    }
    if (state == ESC_POWER_STATE_OFF_HOLD && reached(now_ms, since_ms, ESC_POWER_OFF_HOLD_MS)) {
        state = ESC_POWER_STATE_OFF_READY;
    }
    if (state == ESC_POWER_STATE_STARTING && reached(now_ms, since_ms, ESC_POWER_SIGNAL_DELAY_MS)) {
        state = ESC_POWER_STATE_ARMING;
    }
    if (state == ESC_POWER_STATE_ARMING && reached(now_ms, since_ms, ESC_POWER_THRUST_DELAY_MS)) {
        state = ESC_POWER_STATE_ON;
    }
    if (state == ESC_POWER_STATE_STOPPING && reached(now_ms, since_ms, ESC_POWER_STOP_MS)) {
        if (stop_parked) {
            switch_off(now_ms);
        } else {
            stop_parked = true;
        }
    }
}

void esc_power_host_activity(uint32_t now_ms) {
    host_activity_ms = now_ms;
}

void esc_power_set_busy(uint32_t reasons) {
    busy_reasons = reasons;
}

esc_power_state_t esc_power_state(void) {
    return state;
}

#ifdef MANAFISH_ESC_POWER_PIN
// Pin builds only: main.c's hooks reference it, so a pin build of main.c with a stub of this
// file (board header missed) fails to link. Never add a stub version.
uint8_t esc_power_wire_state(void) {
    static const uint8_t wire[] = {
        [ESC_POWER_STATE_ABSENT] = 0,    [ESC_POWER_STATE_OFF_HOLD] = 1,
        [ESC_POWER_STATE_OFF_READY] = 2, [ESC_POWER_STATE_STARTING] = 3,
        [ESC_POWER_STATE_ARMING] = 3,    [ESC_POWER_STATE_ON] = 4,
        [ESC_POWER_STATE_STOPPING] = 5,
    };
    return wire[state];
}
#endif

bool esc_power_lines_live(void) {
    return state == ESC_POWER_STATE_ABSENT || state == ESC_POWER_STATE_ARMING ||
           state == ESC_POWER_STATE_ON || (state == ESC_POWER_STATE_STOPPING && !stop_parked);
}

bool esc_power_thrust_permitted(void) {
    return state == ESC_POWER_STATE_ABSENT || state == ESC_POWER_STATE_ON;
}

bool esc_power_gpio_level(void) {
    return level;
}
