#!/usr/bin/env python3
"""Exercise main.c's actual DShot startup against recording host stubs.

MCU_TEST_REVISION optionally selects a Git baseline. No hardware is accessed.
"""

import ctypes
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function_source(source, signature):
    """Return the top-level C function that starts at signature, up to its closing brace."""
    start = source.index(signature)
    return source[start:source.index("\n}\n", start) + 3]


def build_library(test_class, name, harness):
    test_class.temp = tempfile.TemporaryDirectory(prefix=f"pico-{name}-")
    test_class.addClassCleanup(test_class.temp.cleanup)
    source = Path(test_class.temp.name) / f"{name}.c"
    library = source.with_suffix(".so")
    source.write_text(harness)
    subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
        "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        "-I", str(ROOT / "tests/mocks"), "-I", str(ROOT / "src"),
        str(source), "-o", str(library)
    ], check=True)
    return ctypes.CDLL(str(library))


class StartupCommandsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        revision = os.environ.get("MCU_TEST_REVISION")
        if revision:
            main = subprocess.check_output(
                ["git", "-C", str(ROOT), "show", revision + ":src/main.c"], text=True
            )
        else:
            main = (ROOT / "src/main.c").read_text()
        start = main.index("static void init_dshot_protocol(")
        end = main.index("static void init_current_protocol(", start)
        startup = main[start:end]
        legacy = "bool persist_3d_mode" in startup
        call = "init_dshot_protocol(speed, true);" if legacy else "init_dshot_protocol(speed);"
        constants = "\n".join(re.findall(
            r"^#define (?:DSHOT_PIO|DSHOT_SM_[01]|DSHOT_TELEMETRY_WARNING_DELAY_MS) .+$",
            main, re.MULTILINE
        ))
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include "dshot/control.h"
#include "dshot/current_sensing.h"
#include "dshot/telemetry_usb.h"
'''+constants+r'''
static struct dshot_controller dshot_controller0, dshot_controller1;
static dshot_telemetry_context_t dshot_context0, dshot_context1;
static bool edt_enable_scheduled[NUM_MOTORS], quality_warned[NUM_MOTORS];
static absolute_time_t edt_enable_time[NUM_MOTORS];
static uint16_t command_values[NUM_MOTORS];
static bool dshot_initialized, dshot_telemetry_warning_pending, outputs_parked;
static absolute_time_t dshot_telemetry_warning_time, next_quality_report_time;
static int command_count, commands[16], repeats[16], initializations, registrations;
static int neutral_sends, frame_cycles, calibration_resets, telemetry_resets;
void dshot_controller_reset_calibration(void) { calibration_resets++; }
void dshot_telemetry_usb_init(void) { telemetry_resets++; }
void dshot_controller_init(struct dshot_controller* controller, uint16_t speed, PIO pio,
                           uint8_t sm, int pin, int channels) {
    assert(speed == 150 || speed == 300 || speed == 600);
    assert(pio == DSHOT_PIO);
    if (controller == &dshot_controller0) {
        assert(sm == DSHOT_SM_0 && pin == MOTOR0_PIN_BASE && channels == NUM_MOTORS_0);
    } else {
        assert(controller == &dshot_controller1);
        assert(sm == DSHOT_SM_1 && pin == MOTOR1_PIN_BASE && channels == NUM_MOTORS_1);
    }
    initializations++;
}
void dshot_telemetry_callback(void* context, int channel, enum dshot_telemetry_type type,
                              uint32_t value) {
    (void)context; (void)channel; (void)type; (void)value;
}
void dshot_register_telemetry_cb(struct dshot_controller* controller,
                                 dshot_telemetry_callback_t callback, void* context) {
    assert(callback == dshot_telemetry_callback);
    assert((controller == &dshot_controller0 && context == &dshot_context0)
        || (controller == &dshot_controller1 && context == &dshot_context1));
    registrations++;
}
void dshot_send_commands(uint16_t* values, struct dshot_controller* a,
                         struct dshot_controller* b) {
    assert(a == &dshot_controller0 && b == &dshot_controller1);
    for (int i = 0; i < NUM_MOTORS; i++) assert(values[i] == CMD_THROTTLE_NEUTRAL);
    neutral_sends++;
}
void dshot_run_frame_cycles(struct dshot_controller* a, struct dshot_controller* b, int cycles) {
    assert(a == &dshot_controller0 && b == &dshot_controller1);
    frame_cycles += cycles;
}
void dshot_send_command_to_all(struct dshot_controller* a, struct dshot_controller* b,
                               uint16_t command, uint8_t repeat_count) {
    assert(a == &dshot_controller0 && b == &dshot_controller1);
    assert(command_count < 16);
    commands[command_count] = command;
    repeats[command_count++] = repeat_count;
}
'''+startup+r'''
void run_startup(uint16_t speed) {
    (void)pio1; // The shared mock header also defines this unused controller.
    command_count = initializations = registrations = neutral_sends = frame_cycles = 0;
    calibration_resets = telemetry_resets = 0;
    dshot_initialized = dshot_telemetry_warning_pending = false;
    for (int i = 0; i < NUM_MOTORS; i++) {
        command_values[i] = CMD_THROTTLE_NEUTRAL;
        edt_enable_scheduled[i] = quality_warned[i] = true;
    }
'''+call+r'''
    assert(initializations == 2 && registrations == 2);
    assert(neutral_sends == 1 && frame_cycles == NUM_MOTORS * 4);
    assert(calibration_resets == 1 && telemetry_resets == 1);
    assert(dshot_initialized && dshot_telemetry_warning_pending);
    assert(next_quality_report_time == dshot_telemetry_warning_time);
    for (int i = 0; i < NUM_MOTORS; i++)
        assert(!edt_enable_scheduled[i] && !quality_warned[i]);
}
void run_parked_startup(uint16_t speed) {
    command_count = initializations = registrations = neutral_sends = frame_cycles = 0;
    calibration_resets = telemetry_resets = 0;
    dshot_initialized = dshot_telemetry_warning_pending = false;
    for (int i = 0; i < NUM_MOTORS; i++) command_values[i] = CMD_THROTTLE_NEUTRAL;
    outputs_parked = true;
'''+call+r'''
    outputs_parked = false;
}
int sent_count(void) { return command_count; }
int sent_command(int i) { return commands[i]; }
int sent_repeats(int i) { return repeats[i]; }
int telemetry_enable_command(void) { return DSHOT_EXTENDED_TELEMETRY_ENABLE; }
int neutral_send_count(void) { return neutral_sends; }
int frame_cycle_count(void) { return frame_cycles; }
int controllers_ready(void) {
    return initializations == 2 && registrations == 2 && dshot_initialized &&
           dshot_telemetry_warning_pending &&
           next_quality_report_time == dshot_telemetry_warning_time;
}
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="pico-startup-")
        cls.addClassCleanup(cls.temp.cleanup)
        folder = Path(cls.temp.name)
        source = folder / "startup.c"
        library = folder / "startup.so"
        source.write_text(harness)
        subprocess.run(
            shlex.split(os.environ.get("CC", "cc"))
            + ["-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
               "-I", str(ROOT / "tests/mocks"), "-I", str(ROOT / "src"),
               str(source), str(ROOT / "src/dshot/current_sensing.c"),
               "-o", str(library)], check=True
        )
        cls.dll = ctypes.CDLL(str(library))
        cls.dll.run_startup.argtypes = [ctypes.c_uint16]
        cls.dll.run_startup.restype = None

    def test_startup_only_sends_volatile_telemetry_enable(self):
        for speed in (150, 300, 600):
            with self.subTest(speed=speed):
                self.dll.run_startup(speed)
                self.assertEqual(self.dll.sent_count(), 1)
                self.assertEqual(self.dll.sent_command(0), self.dll.telemetry_enable_command())
                self.assertEqual(self.dll.sent_repeats(0), 10)

    def test_reinitialization_does_not_write_settings(self):
        for _ in range(3):
            self.dll.run_startup(300)
            self.assertEqual(self.dll.sent_count(), 1)
            self.assertEqual(self.dll.sent_command(0), self.dll.telemetry_enable_command())

    def test_parked_startup_drives_no_frames(self):
        # Parked lines lead to unpowered ESCs: the controllers start, but the blocking neutral
        # and EDT preamble is skipped (the main loop sends both once the lines are live).
        self.dll.run_parked_startup(300)
        self.assertEqual(self.dll.controllers_ready(), 1)
        self.assertEqual(self.dll.neutral_send_count(), 0)
        self.assertEqual(self.dll.frame_cycle_count(), 0)
        self.assertEqual(self.dll.sent_count(), 0)


class CurrentReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main = (ROOT / "src/main.c").read_text()
        start = main.index("static void service_current_reporting(")
        service = main[start:main.index("int main(void)", start)]
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include "dshot/current_sensing.h"
#include "dshot/telemetry_usb.h"
#define THRUSTER_PROTOCOL_DSHOT 1
static bool runtime_config_received = true, protocol_initialized = true;
static bool esc_firmware_recovery_mode, uploading, seen_enabled;
static struct { int protocol; } current_config = {THRUSTER_PROTOCOL_DSHOT};
static uint16_t command_values[NUM_MOTORS];
static uint32_t now_ms;
static int reports, flushes, service_calls;
static bool thrust_permitted = true;
bool esc_power_thrust_permitted(void) { return thrust_permitted; }
uint32_t get_absolute_time(void) { return now_ms; }
uint32_t to_ms_since_boot(uint32_t value) { return value; }
bool esc_firmware_update_receiving(void) { return uploading; }
void current_sensing_service(const uint16_t values[NUM_MOTORS], bool enabled, uint32_t now) {
    (void)values; (void)now; seen_enabled = enabled; service_calls++;
}
int32_t current_sensing_current_ma(uint8_t board, uint32_t now) {
    (void)board; (void)now; return 1000;
}
int32_t current_sensing_baseline_ma(uint8_t board) { (void)board; return 91000; }
void dshot_telemetry_usb_send(uint8_t motor, uint8_t type, int32_t value) {
    (void)motor; (void)type; (void)value; reports++;
}
void dshot_telemetry_usb_flush(void) { flushes++; }
''' + service + r'''
void run_reporting(int upload, int recovery) {
    (void)pio0; (void)pio1;
    uploading = upload != 0; esc_firmware_recovery_mode = recovery != 0;
    now_ms += 1000; reports = flushes = service_calls = 0;
    service_current_reporting();
}
int report_count(void) { return reports; }
int flush_count(void) { return flushes; }
int sensing_enabled(void) { return seen_enabled; }
int sensing_calls(void) { return service_calls; }
void set_thrust_permitted(int permitted) { thrust_permitted = permitted != 0; }
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="pico-current-reporting-")
        cls.addClassCleanup(cls.temp.cleanup)
        source = Path(cls.temp.name) / "reporting.c"
        library = source.with_suffix(".so")
        source.write_text(harness)
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
            "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
            "-I", str(ROOT / "tests/mocks"), "-I", str(ROOT / "src"),
            str(source), "-o", str(library)
        ], check=True)
        cls.dll = ctypes.CDLL(str(library))
        cls.dll.run_reporting.argtypes = [ctypes.c_int, ctypes.c_int]
        cls.dll.run_reporting.restype = None

    def test_upload_and_recovery_disable_sensing_and_usb_reports(self):
        for upload, recovery in ((1, 0), (0, 1), (1, 1)):
            self.dll.run_reporting(upload, recovery)
            self.assertEqual(self.dll.sensing_calls(), 1)
            self.assertEqual(self.dll.sensing_enabled(), 0)
            self.assertEqual(self.dll.report_count(), 0)
            self.assertEqual(self.dll.flush_count(), 0)

    def test_normal_operation_resumes_reporting(self):
        self.dll.run_reporting(1, 0)
        self.dll.run_reporting(0, 0)
        self.assertEqual(self.dll.sensing_enabled(), 1)
        self.assertEqual(self.dll.report_count(), 4)
        self.assertEqual(self.dll.flush_count(), 1)

    def test_current_stays_unavailable_until_thrust_is_permitted(self):
        # Parked, booting (ARMING) or stopping ESCs: no baseline calibration, so the reports
        # carry CURRENT_UNAVAILABLE_MA (-1).
        self.dll.set_thrust_permitted(0)
        try:
            self.dll.run_reporting(0, 0)
            self.assertEqual(self.dll.sensing_calls(), 1)
            self.assertEqual(self.dll.sensing_enabled(), 0)
            self.assertEqual(self.dll.report_count(), 4)
        finally:
            self.dll.set_thrust_permitted(1)


class EscFirmwareGateTests(unittest.TestCase):
    """ESC firmware update begin and commit need powered, settled ESCs (always on pico/pico2)."""

    READY, FAILED, ESC_POWER_OFF = 1, 7, 11

    @classmethod
    def setUpClass(cls):
        main = (ROOT / "src/main.c").read_text()
        start = main.index("static bool runtime_dshot_ready(")
        gate = main[start:main.index("static void prepare_for_esc_firmware_flash(", start)]
        cls.dll = build_library(cls, "esc-firmware-gate", r'''
#include <stdbool.h>
#include <stdint.h>
#include "esc_firmware/update.h"
#define THRUSTER_PROTOCOL_DSHOT 1
static bool runtime_config_received = true, protocol_initialized = true, dshot_initialized;
static bool esc_firmware_recovery_mode, esc_version_discovery_active, power_on;
static struct { int protocol; } current_config = {THRUSTER_PROTOCOL_DSHOT};
static int sent_status, sent_error, statuses;
static uint32_t sent_value;
bool esc_power_thrust_permitted(void) { return power_on; }
static bool all_commands_neutral(void) { return true; }
void esc_firmware_update_send_status(esc_firmware_update_status_t status, uint8_t motor,
                                     esc_firmware_update_error_t error, uint32_t value) {
    (void)motor; sent_status = (int)status; sent_error = (int)error; sent_value = value;
    statuses++;
}
void esc_firmware_update_reset(void) {}
uint16_t esc_firmware_update_image_size(void) { return 0; }
uint16_t esc_firmware_update_received_size(void) { return 0; }
bool esc_firmware_update_validate_image(esc_firmware_update_error_t *error) {
    *error = ESC_FIRMWARE_UPDATE_ERROR_NONE;
    return true;
}
void dshot_telemetry_usb_reset(void) {}
''' + gate + r'''
static void prepare(int on, int dshot_ready) {
    power_on = on != 0; dshot_initialized = dshot_ready != 0;
    sent_status = sent_error = -1; statuses = 0; sent_value = UINT32_MAX;
}
void begin(int recovery, int on, int dshot_ready) {
    prepare(on, dshot_ready);
    handle_esc_firmware_begin(recovery != 0);
}
int commit_safe(int recovery, int on, int dshot_ready) {
    prepare(on, dshot_ready);
    esc_firmware_update_error_t error = ESC_FIRMWARE_UPDATE_ERROR_NONE;
    return esc_firmware_commit_is_safe(recovery != 0, &error);
}
int last_status(void) { return sent_status; }
int last_error(void) { return sent_error; }
int status_count(void) { return statuses; }
uint32_t last_value(void) { return sent_value; }
void set_recovery_mode(int mode) { esc_firmware_recovery_mode = mode != 0; }
''')
        cls.dll.last_value.restype = ctypes.c_uint32

    def test_begin_requires_esc_power(self):
        for recovery, dshot_ready in ((0, 1), (1, 1), (1, 0)):
            with self.subTest(recovery=recovery, dshot_ready=dshot_ready):
                self.dll.begin(recovery, 0, dshot_ready)
                self.assertEqual(self.dll.last_status(), self.FAILED)
                self.assertEqual(self.dll.last_error(), self.ESC_POWER_OFF)
                self.dll.begin(recovery, 1, dshot_ready)
                self.assertEqual(self.dll.last_status(), self.READY)

    def test_commit_requires_esc_power(self):
        for recovery in (0, 1):
            with self.subTest(recovery=recovery):
                self.assertEqual(self.dll.commit_safe(recovery, 0, 1), 0)
                self.assertEqual(self.dll.last_status(), self.FAILED)
                self.assertEqual(self.dll.last_error(), self.ESC_POWER_OFF)
                self.assertEqual(self.dll.commit_safe(recovery, 1, 1), 1)
                self.assertEqual(self.dll.status_count(), 0)

    def test_commit_without_esc_power_keeps_recovery_required(self):
        # After COMMIT the host clears its ESC recovery journal on a FAILED value of 0. Nothing
        # was flashed, so the value repeats whether a recovery is still required.
        for recovery_mode, recovery, required in ((0, 0, 0), (0, 1, 1), (1, 0, 1), (1, 1, 1)):
            with self.subTest(recovery_mode=recovery_mode, recovery=recovery):
                self.dll.set_recovery_mode(recovery_mode)
                try:
                    self.assertEqual(self.dll.commit_safe(recovery, 0, 1), 0)
                    self.assertEqual(self.dll.last_error(), self.ESC_POWER_OFF)
                    self.assertEqual(self.dll.last_value(), required)
                finally:
                    self.dll.set_recovery_mode(0)


class OutputParkingTests(unittest.TestCase):
    """main.c parks the motor lines on SIO low whenever esc_power says they must not be live."""

    PWM, DSHOT, NONE = 0, 1, -1
    SETTINGS, TRANSITION, UPDATE, RECOVERY = 1, 2, 4, 8

    @classmethod
    def setUpClass(cls):
        main = (ROOT / "src/main.c").read_text()
        functions = "\n".join(function_source(main, signature) for signature in (
            "static void park_outputs(",
            "static void init_current_protocol(",
            "static void unpark_outputs(",
            "static uint32_t esc_power_busy_reasons(",
            "static void service_esc_power(",
            "static bool control_outputs_initialized(",
        ))
        cls.dll = build_library(cls, "output-parking", r'''
#include <stdbool.h>
#include <stdint.h>
#include "esc_power.h"
#include "hardware/gpio.h"
#include "motors.h"
#include "pico/time.h"
#define THRUSTER_PROTOCOL_PWM 0
#define THRUSTER_PROTOCOL_DSHOT 1
#define MOCK_GPIO_FUNC_PIO0 6
#define MOCK_GPIO_FUNC_NULL 31
typedef enum {
    RUNTIME_CONFIG_TRANSITION_NONE = 0,
    RUNTIME_CONFIG_TRANSITION_DETECTOR_RESET,
} runtime_config_transition_t;
int mock_gpio_function[30], mock_gpio_direction[30], mock_gpio_value[30];
bool mock_gpio_pwm_enabled_when_selected[30], mock_pwm_enabled[8];
mock_gpio_call_t mock_gpio_calls[MOCK_GPIO_CALL_LOG_SIZE];
int mock_gpio_call_count;
static runtime_config_transition_t runtime_config_transition;
static bool runtime_config_received, protocol_initialized, outputs_parked, uploading;
static bool dshot_initialized, pwm_initialized, all_motor_telemetry_seen;
static bool esc_firmware_recovery_mode, live;
static struct { int protocol; uint16_t dshot_speed; } current_config;
static int dshot_inits, pwm_inits, parked_inits, busy_calls;
static uint16_t init_speed;
static uint32_t busy;
static uint harness_motor_pin(int motor) {
    return motor < NUM_MOTORS_0 ? (uint)(MOTOR0_PIN_BASE + motor)
                                : (uint)(MOTOR1_PIN_BASE + motor - NUM_MOTORS_0);
}
static void mux(int function) {
    for (int i = 0; i < NUM_MOTORS; ++i) {
        mock_gpio_function[harness_motor_pin(i)] = function;
        mock_gpio_value[harness_motor_pin(i)] = 1;
    }
}
static void init_dshot_protocol(uint16_t speed) {
    dshot_inits++; parked_inits += outputs_parked; init_speed = speed;
    dshot_initialized = true; mux(MOCK_GPIO_FUNC_PIO0);
}
static void init_pwm_protocol(void) {
    pwm_inits++; parked_inits += outputs_parked;
    pwm_initialized = true; mux(GPIO_FUNC_PWM);
}
bool esc_firmware_update_receiving(void) { return uploading; }
void esc_power_set_busy(uint32_t reasons) { busy = reasons; busy_calls++; }
void esc_power_service(uint32_t now_ms) { (void)now_ms; }
bool esc_power_lines_live(void) { return live; }
''' + functions + r'''
static void reset(int protocol, int parked) {
    for (int pin = 0; pin < 30; ++pin) {
        mock_gpio_function[pin] = MOCK_GPIO_FUNC_NULL;
        mock_gpio_direction[pin] = GPIO_IN;
        mock_gpio_value[pin] = 1;
    }
    mock_gpio_call_count = 0;
    dshot_inits = pwm_inits = parked_inits = busy_calls = 0;
    busy = UINT32_MAX;
    init_speed = 0;
    current_config.protocol = protocol;
    current_config.dshot_speed = 300;
    outputs_parked = parked != 0;
    dshot_initialized = protocol == THRUSTER_PROTOCOL_DSHOT;
    pwm_initialized = protocol == THRUSTER_PROTOCOL_PWM;
    all_motor_telemetry_seen = true;
    runtime_config_received = true;
    runtime_config_transition = RUNTIME_CONFIG_TRANSITION_NONE;
    uploading = esc_firmware_recovery_mode = false;
}
int motor_lines_parked(void) {
    for (int i = 0; i < NUM_MOTORS; ++i) {
        uint pin = harness_motor_pin(i);
        if (mock_gpio_function[pin] != GPIO_FUNC_SIO || mock_gpio_direction[pin] != GPIO_OUT ||
            mock_gpio_value[pin] != 0) {
            return 0;
        }
    }
    return mock_gpio_function[14] == MOCK_GPIO_FUNC_NULL && mock_gpio_value[14] == 1;
}
static int call_index(uint pin, mock_gpio_call_kind_t kind, int value) {
    for (int i = 0; i < mock_gpio_call_count; ++i) {
        if (mock_gpio_calls[i].pin == pin && mock_gpio_calls[i].kind == kind &&
            mock_gpio_calls[i].value == value) {
            return i;
        }
    }
    return -1;
}
int park(void) {
    reset(THRUSTER_PROTOCOL_DSHOT, 0);
    mux(MOCK_GPIO_FUNC_PIO0);
    park_outputs();
    for (int i = 0; i < NUM_MOTORS; ++i) {
        uint pin = harness_motor_pin(i);
        int put = call_index(pin, MOCK_GPIO_CALL_PUT, 0);
        int dir = call_index(pin, MOCK_GPIO_CALL_DIR, GPIO_OUT);
        int function = call_index(pin, MOCK_GPIO_CALL_FUNCTION, GPIO_FUNC_SIO);
        if (put < 0 || dir < 0 || function < put || function < dir) {
            return 0;
        }
    }
    return outputs_parked && motor_lines_parked();
}
int reinit(int protocol, int parked) {
    reset(protocol, parked);
    init_current_protocol();
    return motor_lines_parked();
}
int go_live(int protocol) {
    reset(protocol, 1);
    park_outputs();
    live = true;
    service_esc_power();
    return outputs_parked;
}
int go_parked(void) {
    reset(THRUSTER_PROTOCOL_DSHOT, 0);
    mux(MOCK_GPIO_FUNC_PIO0);
    live = false;
    service_esc_power();
    return outputs_parked && motor_lines_parked() && dshot_inits == 0;
}
uint32_t busy_reasons(int received, int transition, int upload, int recovery) {
    reset(THRUSTER_PROTOCOL_DSHOT, 1);
    runtime_config_received = received != 0;
    runtime_config_transition = transition ? RUNTIME_CONFIG_TRANSITION_DETECTOR_RESET
                                           : RUNTIME_CONFIG_TRANSITION_NONE;
    uploading = upload != 0;
    esc_firmware_recovery_mode = recovery != 0;
    live = false;
    service_esc_power();
    return busy_calls == 1 ? busy : UINT32_MAX;
}
int outputs_initialized(int initialized, int parked) {
    protocol_initialized = initialized != 0;
    outputs_parked = parked != 0;
    return control_outputs_initialized();
}
int dshot_init_count(void) { return dshot_inits; }
int pwm_init_count(void) { return pwm_inits; }
int parked_init_count(void) { return parked_inits; }
int restart_speed(void) { return init_speed; }
int telemetry_seen(void) { return all_motor_telemetry_seen; }
''')
        cls.dll.busy_reasons.restype = ctypes.c_uint32

    def test_park_drives_every_motor_line_low_through_sio(self):
        # Level and direction are set before the mux switches, so no line glitches high; GP14
        # (ESC_PWR_EN) and every other pin stay untouched.
        self.assertEqual(self.dll.park(), 1)

    def test_protocol_init_while_parked_parks_again(self):
        for protocol in (self.DSHOT, self.PWM):
            with self.subTest(protocol=protocol):
                self.assertEqual(self.dll.reinit(protocol, 1), 1)
                self.assertEqual(self.dll.dshot_init_count() + self.dll.pwm_init_count(), 1)
                self.assertEqual(self.dll.reinit(protocol, 0), 0)

    def test_live_lines_restart_the_protocol_without_its_preamble(self):
        # DShot restarts while still flagged parked (no blocking neutral/EDT preamble, which a
        # live session's 10 ms stall guard would not survive) and forgets old telemetry.
        self.assertEqual(self.dll.go_live(self.DSHOT), 0)
        self.assertEqual(self.dll.dshot_init_count(), 1)
        self.assertEqual(self.dll.parked_init_count(), 1)
        self.assertEqual(self.dll.restart_speed(), 300)
        self.assertEqual(self.dll.telemetry_seen(), 0)
        self.assertEqual(self.dll.go_live(self.PWM), 0)
        self.assertEqual(self.dll.pwm_init_count(), 1)
        self.assertEqual(self.dll.dshot_init_count(), 0)
        self.assertEqual(self.dll.go_live(self.NONE), 0)
        self.assertEqual(self.dll.dshot_init_count() + self.dll.pwm_init_count(), 0)
        self.assertEqual(self.dll.motor_lines_parked(), 1)

    def test_lines_that_must_not_be_live_get_parked(self):
        self.assertEqual(self.dll.go_parked(), 1)

    def test_busy_reasons_follow_main_state(self):
        self.assertEqual(self.dll.busy_reasons(1, 0, 0, 0), 0)
        self.assertEqual(self.dll.busy_reasons(0, 0, 0, 0), self.SETTINGS)
        self.assertEqual(self.dll.busy_reasons(1, 1, 0, 0), self.TRANSITION)
        self.assertEqual(self.dll.busy_reasons(1, 0, 1, 0), self.UPDATE)
        self.assertEqual(self.dll.busy_reasons(1, 0, 0, 1), self.RECOVERY)
        self.assertEqual(self.dll.busy_reasons(0, 1, 1, 1), 15)

    def test_parked_outputs_count_as_uninitialized(self):
        self.assertEqual(self.dll.outputs_initialized(1, 0), 1)
        self.assertEqual(self.dll.outputs_initialized(1, 1), 0)
        self.assertEqual(self.dll.outputs_initialized(0, 0), 0)


class MainWiringTests(unittest.TestCase):
    """Source wiring checks for the ESC power integration in main.c, not a simulated main loop.

    The behaviour behind each call is covered by the slice tests above and tests/test_esc_power.c.
    """

    @classmethod
    def setUpClass(cls):
        cls.main = (ROOT / "src/main.c").read_text()

    def function(self, signature):
        return function_source(self.main, signature)

    def test_esc_power_init_runs_before_stdio(self):
        main = self.function("int main(void)")
        self.assertRegex(main, r"^int main\(void\) \{\s*(?://[^\n]*\n\s*)*esc_power_init\(")
        self.assertLess(main.index("esc_power_init("), main.index("stdio_init_all();"))

    def test_valid_packets_count_as_host_activity(self):
        self.assertIn("esc_power_host_activity(", self.function("static void note_host_activity("))
        rejected = r"[^;]*\)\)\s*\{[^{}]*return;\s*\}\s*note_host_activity\(\);"
        for signature, check in (
            ("static void handle_command_packet(", r"usb_parse_packet\("),
            ("static void handle_config_packet(", r"mcu_runtime_config_parse_packet\("),
            ("static void handle_esc_firmware_control_packet(",
             r"esc_firmware_update_parse_control\("),
            ("static void handle_esc_firmware_data_packet(", r"esc_firmware_update_receive_data\("),
        ):
            with self.subTest(function=signature):
                self.assertRegex(self.function(signature), check + rejected)
        self.assertRegex(
            self.function("int main(void)"),
            r"if \(control_runtime_receive_at\(control_buf,[^;{]*\)\s*\{\s*note_host_activity\(\);",
        )

    def test_blocking_esc_flash_counts_as_host_activity_when_it_returns(self):
        self.assertRegex(
            self.function("static void commit_esc_firmware_update("),
            r"am32_bootloader_flash_all\([^;]*\);\s*(?://[^\n]*\n\s*)*note_host_activity\(\);",
        )

    def test_main_loop_services_esc_power_and_parked_outputs(self):
        main = self.function("int main(void)")
        loop = main[main.index("while (true) {"):]
        self.assertRegex(
            loop,
            r"^while \(true\) \{\s*control_runtime_check_stall\(\);\s*(?://[^\n]*\n\s*)*"
            r"service_esc_power\(\);",
        )
        parked = re.search(r"if \(outputs_parked\) \{([^{}]*)\}", loop)
        self.assertIsNotNone(parked)
        self.assertRegex(
            parked.group(1),
            r"control_runtime_output_serviced\(\);\s*usb_tx_service\(\);\s*continue;",
        )
        self.assertLess(loop.index("if (esc_firmware_recovery_mode)"), parked.start())
        self.assertLess(parked.end(), loop.index("service_dshot_protocol();"))

    def test_esc_power_hooks_exist_only_with_the_pin(self):
        main = self.function("int main(void)")
        hooks = main[main.index("const control_runtime_hooks_t control_hooks = {"):]
        hooks = hooks[:hooks.index("};")]
        self.assertRegex(
            hooks,
            r"#ifdef MANAFISH_ESC_POWER_PIN\s*\.thrust_permitted = esc_power_thrust_permitted,\s*"
            r"\.esc_power_request = control_esc_power_request,\s*"
            r"\.esc_power_state = esc_power_wire_state,\s*#endif",
        )
        self.assertRegex(
            self.main,
            r"#ifdef MANAFISH_ESC_POWER_PIN\n(?:(?!#endif)[\s\S])*"
            r"static uint32_t control_esc_power_request\(uint32_t on\)",
        )

    def test_neutral_holds_never_drive_parked_lines(self):
        self.assertRegex(
            self.function("static void hold_neutral_before_switch("),
            r"set_all_commands_neutral\(\);\s*(?://[^\n]*\n\s*)*"
            r"if \(outputs_parked\) \{\s*return;\s*\}",
        )

    def test_dshot_verdicts_wait_until_thrust_is_permitted(self):
        service = self.function("static void service_dshot_protocol(")
        gate = re.search(
            r"if \(esc_firmware_upload_active \|\| !esc_power_thrust_permitted\(\)\) "
            r"\{\s*return;\s*\}",
            service,
        )
        self.assertIsNotNone(gate)
        self.assertLess(gate.end(), service.index("service_esc_version_discovery();"))


class EscPowerPinLinkTests(unittest.TestCase):
    """A pin build of main.c links esc_power_wire_state through its hooks. esc_power.c defines it
    only when it sees MANAFISH_ESC_POWER_PIN too, so a mainboard build whose esc_power.c misses
    the board header (and compiles the stub) fails to link instead of shipping without GP14.
    """

    CALLER = r'''
#include <stdbool.h>
#include "esc_power.h"
#include "hardware/gpio.h"
int mock_gpio_function[30], mock_gpio_direction[30], mock_gpio_value[30];
bool mock_gpio_pwm_enabled_when_selected[30], mock_pwm_enabled[8];
mock_gpio_call_t mock_gpio_calls[MOCK_GPIO_CALL_LOG_SIZE];
int mock_gpio_call_count;
int main(void) { return esc_power_wire_state(); }
'''

    def link(self, *defines):
        with tempfile.TemporaryDirectory(prefix="pico-esc-power-link-") as temp:
            caller = Path(temp) / "caller.c"
            caller.write_text(self.CALLER)
            return subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", *defines,
                "-I", str(ROOT / "tests/mocks"), "-I", str(ROOT / "src"),
                str(caller), str(ROOT / "src/esc_power.c"), "-o", str(Path(temp) / "caller")
            ], capture_output=True, text=True)

    def test_stub_cannot_satisfy_the_pin_build_reference(self):
        stub = self.link()
        self.assertNotEqual(stub.returncode, 0, "the stub esc_power.c must not link here")
        self.assertIn("esc_power_wire_state", stub.stderr)
        with_pin = self.link("-DMANAFISH_ESC_POWER_PIN=14")
        self.assertEqual(with_pin.returncode, 0, with_pin.stderr)


class RawCurrentTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source_text = (ROOT / "src/dshot/telemetry_usb.c").read_text()
        callback = source_text[source_text.index("void dshot_telemetry_callback("):]
        defines = "\n".join(line for line in source_text.splitlines()
                            if line.startswith("#define "))
        harness = r'''
#include <stdint.h>
#include "motors.h"
#include "dshot/telemetry_usb.h"
#include "pico/time.h"
static esc_version_decoder_t esc_version_decoders[NUM_MOTORS];
static bool esc_versions_reported[NUM_MOTORS];
static uint32_t observed;
static uint8_t observed_motor, sent_motor, sent_type;
static int32_t sent;
void current_sensing_observe_current(uint8_t motor, uint32_t value, uint32_t now) {
    (void)now; observed_motor = motor; observed = value;
}
void current_sensing_observe_erpm(uint8_t motor, uint32_t value, uint32_t now) {
    (void)motor; (void)value; (void)now;
}
void dshot_telemetry_usb_send(uint8_t motor, uint8_t type, int32_t value) {
    sent_motor = motor; sent_type = type; sent = value;
}
bool dshot_telemetry_usb_decode_esc_version(esc_version_decoder_t *decoder,
    enum dshot_telemetry_type type, uint32_t value,
    char version[ESC_FIRMWARE_VERSION_MAX_LENGTH + 1]) {
    (void)decoder; (void)type; (void)value; (void)version; return false;
}
static void dshot_telemetry_usb_send_esc_version(uint8_t motor, const char *version) {
    (void)motor; (void)version;
}
typedef enum {
    ESC_TELEMETRY_REPORT_ERPM = 0,
    ESC_TELEMETRY_REPORT_VOLTAGE,
    ESC_TELEMETRY_REPORT_TEMPERATURE,
    ESC_TELEMETRY_REPORT_CURRENT,
} esc_telemetry_report_kind_t;
bool esc_telemetry_report_due(uint8_t motor_id, esc_telemetry_report_kind_t kind) {
    (void)motor_id; (void)kind; return true;
}
''' + defines + "\n" + callback + r'''
int check_raw_current(void) {
    (void)pio0; (void)pio1;
    for (uint8_t motor = 0; motor < NUM_MOTORS; ++motor) {
        dshot_telemetry_context_t context = {.controller_base_global_id = motor < 4 ? 0 : 4};
        for (uint32_t value = 0; value <= UINT8_MAX; ++value) {
            observed = UINT32_MAX; sent = -99;
            dshot_telemetry_callback(&context, motor % 4, DSHOT_TELEMETRY_TYPE_CURRENT, value);
            if (observed_motor != motor || observed != value) return 1;
            if (sent_motor != motor || sent_type != TELEMETRY_TYPE_CURRENT ||
                sent != (int32_t)value) return 2;
        }
    }
    return 0;
}
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="pico-raw-current-")
        cls.addClassCleanup(cls.temp.cleanup)
        source = Path(cls.temp.name) / "raw_current.c"
        library = source.with_suffix(".so")
        source.write_text(harness)
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
            "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
            "-I", str(ROOT / "tests/mocks"), "-I", str(ROOT / "src"),
            str(source), "-o", str(library)
        ], check=True)
        cls.dll = ctypes.CDLL(str(library))

    def test_callback_preserves_every_raw_byte_for_estimation_and_usb(self):
        self.assertEqual(self.dll.check_raw_current(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
