/*
 * Lightweight logging over USB CDC for the microcontroller firmware.
 * Log packets share the serial link with telemetry but use a distinct
 * start byte (0xB5) so the host firmware can demultiplex them.
 *
 * Packet format (variable length):
 *   [0xB5] [level] [length] [message...] [xor_checksum]
 *
 * Rate-limited to LOG_MAX_PER_SECOND to avoid saturating USB bandwidth.
 */

#ifndef LOG_H
#define LOG_H

#include <stdarg.h>
#include <stdint.h>

#define LOG_START_BYTE 0xB5
#define LOG_MAX_MESSAGE_SIZE 200
#define LOG_MAX_PER_SECOND 10

enum log_level {
    LOG_LEVEL_INFO = 0,
    LOG_LEVEL_WARN = 1,
    LOG_LEVEL_ERROR = 2,
};

/* Initialize the log subsystem. Call once before any log_* functions. */
void log_init(void);

void log_info(const char *message);
void log_warn(const char *message);
void log_error(const char *message);
void log_infof(const char *format, ...) __attribute__((format(printf, 1, 2)));
void log_warnf(const char *format, ...) __attribute__((format(printf, 1, 2)));
void log_errorf(const char *format, ...) __attribute__((format(printf, 1, 2)));

#endif
