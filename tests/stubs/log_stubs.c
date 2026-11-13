#include <stdarg.h>

void log_init(void) {}

void log_info(const char *message) {
    (void)message;
}

void log_warn(const char *message) {
    (void)message;
}

void log_error(const char *message) {
    (void)message;
}

void log_infof(const char *format, ...) {
    va_list args;
    va_start(args, format);
    va_end(args);
}

void log_warnf(const char *format, ...) {
    va_list args;
    va_start(args, format);
    va_end(args);
}

void log_errorf(const char *format, ...) {
    va_list args;
    va_start(args, format);
    va_end(args);
}
