#ifndef CONTROL_RUNTIME_H
#define CONTROL_RUNTIME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    bool (*request_protocol)(uint16_t protocol, uint16_t speed);
    bool (*protocol_ready)(uint16_t protocol, uint16_t speed);
    bool (*maintenance_active)(void);
    bool (*recovery_required)(void);
    bool (*outputs_initialized)(void);
    uint16_t (*output_protocol)(void);
    // Optional (NULL on pico/pico2): ESC power switch (src/esc_power.h). thrust_permitted gates
    // every motor output, esc_power_request answers MSG_ESC_POWER (0x26) with an ACK result, and
    // esc_power_state is the wire state reported in ATTITUDE health bits 4-6.
    bool (*thrust_permitted)(void);
    uint32_t (*esc_power_request)(uint32_t on);
    uint8_t (*esc_power_state)(void);
} control_runtime_hooks_t;

void control_runtime_init(const control_runtime_hooks_t *hooks);
void control_runtime_capabilities(uint8_t request_id);
void control_runtime_receive(const uint8_t *packet, size_t length);
// Returns false only for a frame that does not decode (length, CRC); every other frame is host
// activity, also one the session rejects.
bool control_runtime_receive_at(const uint8_t *packet, size_t length, uint32_t received_us);
void control_runtime_service(void);
void control_runtime_check_stall(void);
void control_runtime_legacy_input(const uint16_t motors[8]);
void control_runtime_legacy_input_at(const uint16_t motors[8], uint32_t received_us);
void control_runtime_get_motors(uint16_t motors[8]);
void control_runtime_output_serviced(void);
void control_runtime_inhibit(void);
bool control_runtime_output_permitted(void);
bool control_runtime_extended_active(void);
bool control_runtime_maintenance_latched(void);

#endif
