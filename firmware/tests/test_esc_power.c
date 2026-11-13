#include "esc_power.h"
#include "unity/unity.h"
#include <hardware/gpio.h>

mock_gpio_call_t mock_gpio_calls[MOCK_GPIO_CALL_LOG_SIZE];
int mock_gpio_call_count;

// Without MANAFISH_ESC_POWER_PIN (pico/pico2): the src/esc_power.c linked through TEST_APP_SRC.
static void test_stub_without_pin_is_absent_and_permits_thrust(void) {
    mock_gpio_call_count = 0;
    esc_power_init(0);
    esc_power_set_busy(0);
    esc_power_host_activity(0);
    esc_power_service(100000);

    TEST_ASSERT_EQUAL_INT(0, mock_gpio_call_count);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ABSENT, esc_power_state());
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_UNSUPPORTED, esc_power_request(1, 100000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_UNSUPPORTED, esc_power_request(0, 100000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_UNSUPPORTED, esc_power_request(2, 100000));
    esc_power_service(200000);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ABSENT, esc_power_state());
    TEST_ASSERT_TRUE(esc_power_thrust_permitted());
    TEST_ASSERT_TRUE(esc_power_lines_live());
    TEST_ASSERT_FALSE(esc_power_gpio_level());
    TEST_ASSERT_EQUAL_INT(0, mock_gpio_call_count);
}

static void test_results_match_extended_ack_codes(void) {
    TEST_ASSERT_EQUAL_INT(0, ESC_POWER_RESULT_APPLIED);
    TEST_ASSERT_EQUAL_INT(2, ESC_POWER_RESULT_INVALID);
    TEST_ASSERT_EQUAL_INT(3, ESC_POWER_RESULT_BUSY);
    TEST_ASSERT_EQUAL_INT(6, ESC_POWER_RESULT_UNSUPPORTED);
}

// With MANAFISH_ESC_POWER_PIN: compile the module again here under other names, so it does not
// clash with the stub copy above.
#define MANAFISH_ESC_POWER_PIN 14
#define esc_power_init pin_esc_power_init
#define esc_power_request pin_esc_power_request
#define esc_power_service pin_esc_power_service
#define esc_power_host_activity pin_esc_power_host_activity
#define esc_power_set_busy pin_esc_power_set_busy
#define esc_power_state pin_esc_power_state
#define esc_power_wire_state pin_esc_power_wire_state
#define esc_power_lines_live pin_esc_power_lines_live
#define esc_power_thrust_permitted pin_esc_power_thrust_permitted
#define esc_power_gpio_level pin_esc_power_gpio_level
#include "../src/esc_power.c"

#define PIN 14

static void boot(uint32_t now) {
    mock_gpio_call_count = 0;
    esc_power_init(now);
}

static void assert_call(int index, mock_gpio_call_kind_t kind, int value) {
    TEST_ASSERT_EQUAL_INT(kind, mock_gpio_calls[index].kind);
    TEST_ASSERT_EQUAL_UINT(PIN, mock_gpio_calls[index].pin);
    TEST_ASSERT_EQUAL_INT(value, mock_gpio_calls[index].value);
}

static void assert_off(esc_power_state_t state) {
    TEST_ASSERT_EQUAL_INT(state, esc_power_state());
    TEST_ASSERT_FALSE(esc_power_gpio_level());
    TEST_ASSERT_EQUAL_INT(0, mock_gpio_value[PIN]);
    TEST_ASSERT_FALSE(esc_power_lines_live());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());
}

static void test_init_drives_pin_low_with_12ma_before_enabling_output(void) {
    mock_gpio_value[PIN] = 1;
    boot(0);

    TEST_ASSERT_EQUAL_INT(4, mock_gpio_call_count);
    assert_call(0, MOCK_GPIO_CALL_INIT, 0);
    assert_call(1, MOCK_GPIO_CALL_DRIVE_STRENGTH, GPIO_DRIVE_STRENGTH_12MA);
    assert_call(2, MOCK_GPIO_CALL_PUT, 0);
    assert_call(3, MOCK_GPIO_CALL_DIR, GPIO_OUT);
    TEST_ASSERT_EQUAL_INT(GPIO_OUT, mock_gpio_direction[PIN]);
    assert_off(ESC_POWER_STATE_OFF_HOLD);
    TEST_ASSERT_EQUAL_UINT8(1, esc_power_wire_state());
}

// Boots at `now` and waits out the hold; returns the time the switch became OFF_READY.
static uint32_t ready(uint32_t now) {
    boot(now);
    esc_power_service(now + ESC_POWER_OFF_HOLD_MS);
    return now + ESC_POWER_OFF_HOLD_MS;
}

static int count_puts(int value) {
    int count = 0;
    for (int i = 0; i < mock_gpio_call_count; ++i) {
        count += mock_gpio_calls[i].kind == MOCK_GPIO_CALL_PUT && mock_gpio_calls[i].value == value;
    }
    return count;
}

static void test_hold_lasts_five_seconds_after_boot_and_never_switches_on(void) {
    boot(1000);
    esc_power_service(1000 + ESC_POWER_OFF_HOLD_MS - 1);
    assert_off(ESC_POWER_STATE_OFF_HOLD);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_BUSY,
                          esc_power_request(1, 1000 + ESC_POWER_OFF_HOLD_MS - 1));
    assert_off(ESC_POWER_STATE_OFF_HOLD);

    esc_power_service(1000 + ESC_POWER_OFF_HOLD_MS);
    assert_off(ESC_POWER_STATE_OFF_READY);
    TEST_ASSERT_EQUAL_UINT8(2, esc_power_wire_state());

    for (uint32_t now = 6000; now < 6000 + 10u * ESC_POWER_HOST_LOSS_MS; now += 1000) {
        esc_power_service(now);
    }
    assert_off(ESC_POWER_STATE_OFF_READY);
    TEST_ASSERT_EQUAL_INT(0, count_puts(1));
}

static void test_on_powers_first_then_signals_then_permits_thrust(void) {
    uint32_t on = ready(0);

    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, on));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STARTING, esc_power_state());
    TEST_ASSERT_EQUAL_UINT8(3, esc_power_wire_state());
    TEST_ASSERT_TRUE(esc_power_gpio_level());
    TEST_ASSERT_EQUAL_INT(1, mock_gpio_value[PIN]);
    TEST_ASSERT_FALSE(esc_power_lines_live());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());

    esc_power_service(on + ESC_POWER_SIGNAL_DELAY_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STARTING, esc_power_state());
    TEST_ASSERT_FALSE(esc_power_lines_live());

    esc_power_service(on + ESC_POWER_SIGNAL_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());
    TEST_ASSERT_EQUAL_UINT8(3, esc_power_wire_state());
    TEST_ASSERT_TRUE(esc_power_lines_live());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());

    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());

    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    TEST_ASSERT_EQUAL_UINT8(4, esc_power_wire_state());
    TEST_ASSERT_TRUE(esc_power_lines_live());
    TEST_ASSERT_TRUE(esc_power_thrust_permitted());
    TEST_ASSERT_TRUE(esc_power_gpio_level());
    TEST_ASSERT_EQUAL_INT(1, count_puts(1));
}

static void test_late_service_still_counts_thrust_delay_from_power_on(void) {
    uint32_t on = ready(0);
    esc_power_request(1, on);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());

    on = ready(0);
    esc_power_request(1, on);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
}

static void test_repeated_on_is_applied_without_restarting(void) {
    uint32_t on = ready(0);
    esc_power_request(1, on);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, on + 1000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STARTING, esc_power_state());

    esc_power_service(on + ESC_POWER_SIGNAL_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, on + 6000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());

    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, on + 16000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    TEST_ASSERT_EQUAL_INT(1, count_puts(1));
    TEST_ASSERT_EQUAL_INT(1, count_puts(0));
}

static void test_on_is_busy_while_any_busy_reason_is_set(void) {
    const uint32_t reasons[] = {ESC_POWER_BUSY_SETTINGS, ESC_POWER_BUSY_TRANSITION,
                                ESC_POWER_BUSY_UPDATE, ESC_POWER_BUSY_RECOVERY};
    for (int i = 0; i < 4; ++i) {
        uint32_t now = ready(0);
        esc_power_set_busy(reasons[i]);
        TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_BUSY, esc_power_request(1, now));
        esc_power_service(now + 1);
        assert_off(ESC_POWER_STATE_OFF_READY);

        esc_power_set_busy(0);
        TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, now + 2));
        TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STARTING, esc_power_state());
    }

    // An already running switch stays APPLIED: a busy update is no reason to restart it.
    uint32_t on = ready(0);
    esc_power_request(1, on);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    esc_power_set_busy(ESC_POWER_BUSY_UPDATE);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, on + 16000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
}

static void test_request_values_other_than_zero_and_one_are_invalid(void) {
    uint32_t now = ready(0);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_INVALID, esc_power_request(2, now));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_INVALID, esc_power_request(UINT32_MAX, now));
    assert_off(ESC_POWER_STATE_OFF_READY);

    esc_power_request(1, now);
    esc_power_service(now + ESC_POWER_THRUST_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_INVALID, esc_power_request(2, now + 16000));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
}

// Switches on at the end of the hold; returns the time GP14 rose.
static uint32_t power_on(uint32_t now) {
    uint32_t on = ready(now);
    esc_power_request(1, on);
    return on;
}

static void assert_hold_from(uint32_t off) {
    esc_power_service(off + ESC_POWER_OFF_HOLD_MS - 1);
    assert_off(ESC_POWER_STATE_OFF_HOLD);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_BUSY,
                          esc_power_request(1, off + ESC_POWER_OFF_HOLD_MS - 1));
    esc_power_service(off + ESC_POWER_OFF_HOLD_MS);
    assert_off(ESC_POWER_STATE_OFF_READY);
}

static void test_off_while_starting_drops_power_directly(void) {
    uint32_t on = power_on(0);
    uint32_t off = on + ESC_POWER_SIGNAL_DELAY_MS - 1;
    esc_power_service(off);

    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(0, off));
    assert_off(ESC_POWER_STATE_OFF_HOLD);
    TEST_ASSERT_EQUAL_UINT8(1, esc_power_wire_state());
    assert_hold_from(off);
}

// Neutral for STOP_MS, then the lines park while GP14 is still high, then GP14 drops.
static void assert_stops_from(uint32_t off) {
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(0, off));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STOPPING, esc_power_state());
    TEST_ASSERT_EQUAL_UINT8(5, esc_power_wire_state());
    TEST_ASSERT_TRUE(esc_power_lines_live());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());
    TEST_ASSERT_TRUE(esc_power_gpio_level());

    esc_power_service(off + ESC_POWER_STOP_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STOPPING, esc_power_state());
    TEST_ASSERT_TRUE(esc_power_lines_live());
    TEST_ASSERT_TRUE(esc_power_gpio_level());

    esc_power_service(off + ESC_POWER_STOP_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STOPPING, esc_power_state());
    TEST_ASSERT_EQUAL_UINT8(5, esc_power_wire_state());
    TEST_ASSERT_FALSE(esc_power_lines_live());
    TEST_ASSERT_TRUE(esc_power_gpio_level());
    TEST_ASSERT_EQUAL_INT(1, mock_gpio_value[PIN]);

    esc_power_service(off + ESC_POWER_STOP_MS + 1);
    assert_off(ESC_POWER_STATE_OFF_HOLD);
    assert_hold_from(off + ESC_POWER_STOP_MS + 1);
}

static void test_off_while_arming_stops_before_dropping_power(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_SIGNAL_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());
    assert_stops_from(on + ESC_POWER_SIGNAL_DELAY_MS + 1);
}

static void test_off_while_on_stops_before_dropping_power(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    assert_stops_from(on + 20000);
}

static void test_on_is_busy_while_stopping(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    uint32_t off = on + 20000;
    esc_power_request(0, off);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_BUSY, esc_power_request(1, off + 1));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STOPPING, esc_power_state());

    esc_power_service(off + ESC_POWER_STOP_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_BUSY, esc_power_request(1, off + ESC_POWER_STOP_MS));
    TEST_ASSERT_FALSE(esc_power_lines_live());
}

static void test_off_is_applied_without_restarting_when_already_off_or_stopping(void) {
    boot(0);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(0, 4000));
    assert_hold_from(0);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(0, 6000));
    assert_off(ESC_POWER_STATE_OFF_READY);

    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    uint32_t off = on + 20000;
    esc_power_request(0, off);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(0, off + 1000));
    esc_power_service(off + ESC_POWER_STOP_MS);
    TEST_ASSERT_FALSE(esc_power_lines_live());
    TEST_ASSERT_EQUAL_INT(1, count_puts(1));
}

// Services every 100 ms over [from, to); the switch must stay ON throughout.
static void assert_on_until(uint32_t from, uint32_t to) {
    for (uint32_t now = from; now != to; now += 100) {
        esc_power_service(now);
        TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    }
}

static void assert_host_loss_at(uint32_t now) {
    esc_power_service(now - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    esc_power_service(now);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STOPPING, esc_power_state());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());
    esc_power_service(now + ESC_POWER_STOP_MS);
    esc_power_service(now + ESC_POWER_STOP_MS + 1);
    assert_off(ESC_POWER_STATE_OFF_HOLD);
}

static void test_host_loss_after_the_on_request_turns_power_off(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    assert_host_loss_at(on + ESC_POWER_HOST_LOSS_MS);
}

static void test_host_activity_restarts_host_loss_timer(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    uint32_t seen = on + 20000;
    esc_power_host_activity(seen);
    esc_power_service(seen + ESC_POWER_HOST_LOSS_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    esc_power_host_activity(seen + ESC_POWER_HOST_LOSS_MS - 1);
    assert_host_loss_at(seen + 2 * ESC_POWER_HOST_LOSS_MS - 1);
}

static void test_recovery_holds_host_loss_until_it_ends(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    uint32_t start = on + ESC_POWER_THRUST_DELAY_MS;
    esc_power_set_busy(ESC_POWER_BUSY_RECOVERY | ESC_POWER_BUSY_SETTINGS);
    uint32_t end = start + 10u * ESC_POWER_HOST_LOSS_MS;
    assert_on_until(start, end);

    esc_power_service(end);
    esc_power_set_busy(0);
    assert_host_loss_at(end + ESC_POWER_HOST_LOSS_MS);
}

// Every accepted E8 packet is host activity, so a live upload renews the timer itself. A host
// that dies mid-upload must not leave ESC power on.
static void test_upload_without_host_traffic_still_turns_power_off(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    esc_power_set_busy(ESC_POWER_BUSY_UPDATE);
    assert_host_loss_at(on + ESC_POWER_HOST_LOSS_MS);
}

// Recovery: the ESCs wait in the AM32 bootloader. Power stays on until a recovery flash
// succeeds, because RECOVER_BEGIN and COMMIT need ON, and powering the ESCs up again on parked
// (low) lines would start their partly programmed application.
static void test_off_is_busy_only_while_recovery_is_required(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    uint32_t now = on + 20000;
    esc_power_set_busy(ESC_POWER_BUSY_RECOVERY);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_BUSY, esc_power_request(0, now));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, now));
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_INVALID, esc_power_request(2, now));
    esc_power_service(now + ESC_POWER_STOP_MS + 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    TEST_ASSERT_TRUE(esc_power_lines_live());
    TEST_ASSERT_TRUE(esc_power_gpio_level());
    TEST_ASSERT_EQUAL_INT(1, mock_gpio_value[PIN]);
    TEST_ASSERT_EQUAL_INT(1, count_puts(0)); // init only

    esc_power_set_busy(ESC_POWER_BUSY_SETTINGS | ESC_POWER_BUSY_TRANSITION | ESC_POWER_BUSY_UPDATE);
    assert_stops_from(now + ESC_POWER_STOP_MS + 2);
}

static void test_blocking_flash_then_host_activity_keeps_power_on(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    uint32_t start = on + 20000;
    esc_power_set_busy(ESC_POWER_BUSY_UPDATE);
    esc_power_service(start);

    // Core 0 is blocked for minutes without service; the integration reports activity after it.
    uint32_t back = start + 10u * ESC_POWER_HOST_LOSS_MS;
    esc_power_host_activity(back);
    esc_power_set_busy(0);
    esc_power_service(back);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    assert_host_loss_at(back + ESC_POWER_HOST_LOSS_MS);
}

static void test_settings_and_transition_do_not_hold_host_loss(void) {
    uint32_t on = power_on(0);
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    esc_power_set_busy(ESC_POWER_BUSY_SETTINGS | ESC_POWER_BUSY_TRANSITION);
    assert_host_loss_at(on + ESC_POWER_HOST_LOSS_MS);
}

static void assert_full_cycle_from(uint32_t base) {
    boot(base);
    assert_hold_from(base);
    uint32_t on = base + ESC_POWER_OFF_HOLD_MS;
    TEST_ASSERT_EQUAL_INT(ESC_POWER_RESULT_APPLIED, esc_power_request(1, on));
    esc_power_service(on + ESC_POWER_SIGNAL_DELAY_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STARTING, esc_power_state());
    esc_power_service(on + ESC_POWER_SIGNAL_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ARMING, esc_power_state());
    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());
    assert_host_loss_at(on + ESC_POWER_HOST_LOSS_MS);
    assert_hold_from(on + ESC_POWER_HOST_LOSS_MS + ESC_POWER_STOP_MS + 1);
}

static void test_time_wrap_keeps_every_boundary(void) {
    // Each base makes a different interval cross zero: hold, signal, thrust, host loss, stop.
    const uint32_t before_wrap[] = {2000, 6000, 15000, 32000, 36000};
    for (int i = 0; i < 5; ++i) {
        assert_full_cycle_from(UINT32_MAX - before_wrap[i]);
    }
}

// A caller may pass a time read just before the one stored by a request or host activity.
static void test_clock_behind_a_stored_time_never_counts_as_elapsed(void) {
    boot(1000);
    esc_power_service(999);
    assert_off(ESC_POWER_STATE_OFF_HOLD);

    uint32_t on = power_on(0);
    esc_power_service(on - 1);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STARTING, esc_power_state());
    TEST_ASSERT_FALSE(esc_power_lines_live());
    TEST_ASSERT_FALSE(esc_power_thrust_permitted());

    esc_power_service(on + ESC_POWER_THRUST_DELAY_MS);
    esc_power_host_activity(on + 20001);
    esc_power_service(on + 20000);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_ON, esc_power_state());

    esc_power_request(0, on + 21000);
    esc_power_service(on + 20999);
    TEST_ASSERT_EQUAL_INT(ESC_POWER_STATE_STOPPING, esc_power_state());
    TEST_ASSERT_TRUE(esc_power_lines_live());
}

void test_esc_power(void) {
    RUN_TEST(test_stub_without_pin_is_absent_and_permits_thrust);
    RUN_TEST(test_results_match_extended_ack_codes);
    RUN_TEST(test_init_drives_pin_low_with_12ma_before_enabling_output);
    RUN_TEST(test_hold_lasts_five_seconds_after_boot_and_never_switches_on);
    RUN_TEST(test_on_powers_first_then_signals_then_permits_thrust);
    RUN_TEST(test_late_service_still_counts_thrust_delay_from_power_on);
    RUN_TEST(test_repeated_on_is_applied_without_restarting);
    RUN_TEST(test_on_is_busy_while_any_busy_reason_is_set);
    RUN_TEST(test_request_values_other_than_zero_and_one_are_invalid);
    RUN_TEST(test_off_while_starting_drops_power_directly);
    RUN_TEST(test_off_while_arming_stops_before_dropping_power);
    RUN_TEST(test_off_while_on_stops_before_dropping_power);
    RUN_TEST(test_on_is_busy_while_stopping);
    RUN_TEST(test_off_is_applied_without_restarting_when_already_off_or_stopping);
    RUN_TEST(test_host_loss_after_the_on_request_turns_power_off);
    RUN_TEST(test_host_activity_restarts_host_loss_timer);
    RUN_TEST(test_recovery_holds_host_loss_until_it_ends);
    RUN_TEST(test_upload_without_host_traffic_still_turns_power_off);
    RUN_TEST(test_off_is_busy_only_while_recovery_is_required);
    RUN_TEST(test_blocking_flash_then_host_activity_keeps_power_on);
    RUN_TEST(test_settings_and_transition_do_not_hold_host_loss);
    RUN_TEST(test_time_wrap_keeps_every_boundary);
    RUN_TEST(test_clock_behind_a_stored_time_never_counts_as_elapsed);
}
