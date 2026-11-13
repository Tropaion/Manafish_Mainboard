#ifndef ESC_POWER_H
#define ESC_POWER_H

#include <stdbool.h>
#include <stdint.h>

// ESC power switch (ESC_PWR_EN) state machine, core 0 only. Time is passed in as ms since boot.
// It never switches on by itself, persists nothing and has no force-off.
// Without MANAFISH_ESC_POWER_PIN (pico/pico2) the module is a stub: ABSENT, every request
// UNSUPPORTED, lines always live and thrust always permitted, as upstream behaves.
#define ESC_POWER_OFF_HOLD_MS 5000u
#define ESC_POWER_SIGNAL_DELAY_MS 5000u
#define ESC_POWER_THRUST_DELAY_MS 15000u
#define ESC_POWER_STOP_MS 2000u
// Auto-off after 30 s without host activity.
#define ESC_POWER_HOST_LOSS_MS 30000u

// Busy reasons: any of them answers an on request with BUSY. RECOVERY (ESCs left in the AM32
// bootloader by a failed flash) also answers off with BUSY and holds the host-loss timer, which
// then runs again from the last service call that saw it: power stays on until a recovery flash
// succeeds. An upload holds nothing; its accepted packets count as host activity.
#define ESC_POWER_BUSY_SETTINGS 0x1u
#define ESC_POWER_BUSY_TRANSITION 0x2u
#define ESC_POWER_BUSY_UPDATE 0x4u
#define ESC_POWER_BUSY_RECOVERY 0x8u

typedef enum {
    ESC_POWER_STATE_ABSENT,
    ESC_POWER_STATE_OFF_HOLD,
    ESC_POWER_STATE_OFF_READY,
    ESC_POWER_STATE_STARTING,
    ESC_POWER_STATE_ARMING,
    ESC_POWER_STATE_ON,
    ESC_POWER_STATE_STOPPING,
} esc_power_state_t;

// Values match the extended ACK results.
typedef enum {
    ESC_POWER_RESULT_APPLIED = 0,
    ESC_POWER_RESULT_INVALID = 2,
    ESC_POWER_RESULT_BUSY = 3,
    ESC_POWER_RESULT_UNSUPPORTED = 6,
} esc_power_result_t;

// Drives the pin low with 12 mA and starts the off hold.
void esc_power_init(uint32_t now_ms);
// on: 0 off, 1 on, anything else INVALID. Every request counts as host activity.
esc_power_result_t esc_power_request(uint32_t on, uint32_t now_ms);
// Call once per main-loop pass and park the lines whenever lines_live() is false. When STOPPING
// ends, one call parks the lines and the next drops GP14.
void esc_power_service(uint32_t now_ms);
void esc_power_host_activity(uint32_t now_ms);
void esc_power_set_busy(uint32_t reasons);
esc_power_state_t esc_power_state(void);
// Defined only with MANAFISH_ESC_POWER_PIN (the ATTITUDE hook): a pin build that compiled the
// stub fails to link instead of shipping without the switch.
uint8_t esc_power_wire_state(void);
bool esc_power_lines_live(void);
bool esc_power_thrust_permitted(void);
bool esc_power_gpio_level(void);

#endif
