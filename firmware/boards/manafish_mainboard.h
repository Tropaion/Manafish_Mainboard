// Manafish mainboard: RP2354A (an RP2350A die with 2 MB flash in the package) and a 12 MHz
// crystal. Modelled on the Pico SDK 2.3.1 boards/pico2.h. CMakeLists.txt adds this directory
// to PICO_BOARD_HEADER_DIRS; build with -DPICO_BOARD=manafish_mainboard (make build-mainboard).

// -----------------------------------------------------
// NOTE: THIS HEADER IS ALSO INCLUDED BY ASSEMBLER SO
//       SHOULD ONLY CONSIST OF PREPROCESSOR DIRECTIVES
// -----------------------------------------------------

// This header may be included by other board headers as "boards/manafish_mainboard.h"

#ifndef _BOARDS_MANAFISH_MAINBOARD_H
#define _BOARDS_MANAFISH_MAINBOARD_H

// The pico_board_cmake_set*() markers have no semicolon, so clang-format would indent each one
// after the first as a continuation line.
// clang-format off

pico_board_cmake_set(PICO_PLATFORM, rp2350)

// For board detection (SDK convention: no value, test it with #ifdef)
#define MANAFISH_MAINBOARD

// --- RP2350 VARIANT ---
#define PICO_RP2350A 1

// --- UART, LED, VBUS, VSYS, SMPS ---
// None on purpose. GP0-5 and GP15-17 are not connected, there is no LED and no Pico power
// circuit, and J13 (GP22-29) is the only UART access. Without PICO_DEFAULT_UART the SDK's
// stdio_uart_init() does nothing, so no pin is claimed for stdio.

// --- I2C, SPI ---
// No defaults: the BMI270 uses spi1 on GP10-13 directly (src/imu/bmi270_sensor.c).

// --- FLASH ---

#define PICO_BOOT_STAGE2_CHOOSE_W25Q080 1

#ifndef PICO_FLASH_SPI_CLKDIV
#define PICO_FLASH_SPI_CLKDIV 2
#endif

pico_board_cmake_set_default(PICO_FLASH_SIZE_BYTES, (2 * 1024 * 1024))
#ifndef PICO_FLASH_SIZE_BYTES
#define PICO_FLASH_SIZE_BYTES (2 * 1024 * 1024)
#endif

// --- CRYSTAL ---
// Y1 runs at the SDK's default XOSC_HZ (12 MHz) but starts slowly (ESR 60 ohm, 1 kohm in
// series): wait about 64 ms instead of the SDK's 6 ms.
#ifndef PICO_XOSC_STARTUP_DELAY_MULTIPLIER
#define PICO_XOSC_STARTUP_DELAY_MULTIPLIER 64
#endif

// --- USB ---
// Product string only; VID:PID stay the SDK's 2e8a:0009 and the manufacturer "Raspberry Pi".
// At most 19 characters: the SDK truncates strings to USBD_DESC_STR_MAX (20) - 1.
#ifndef USBD_PRODUCT
#define USBD_PRODUCT "Manafish Mainboard"
#endif

// --- MANAFISH ---
// GP14 = ESC_PWR_EN through R31 (180 ohm) to the IC16 LED; src/esc_power.c drives it.
#define MANAFISH_ESC_POWER_PIN 14

// BMI270 axis map (src/imu/bmi270_sensor.c): output axis = SIGN * raw sensor axis AXIS.
// Upstream's Pico values; measure at B13.
#ifndef MANAFISH_IMU_AXIS_X
#define MANAFISH_IMU_AXIS_X 0
#endif
#ifndef MANAFISH_IMU_AXIS_Y
#define MANAFISH_IMU_AXIS_Y 1
#endif
#ifndef MANAFISH_IMU_AXIS_Z
#define MANAFISH_IMU_AXIS_Z 2
#endif
#ifndef MANAFISH_IMU_SIGN_X
#define MANAFISH_IMU_SIGN_X 1
#endif
#ifndef MANAFISH_IMU_SIGN_Y
#define MANAFISH_IMU_SIGN_Y (-1)
#endif
#ifndef MANAFISH_IMU_SIGN_Z
#define MANAFISH_IMU_SIGN_Z (-1)
#endif

pico_board_cmake_set_default(PICO_RP2350_A2_SUPPORTED, 1)
#ifndef PICO_RP2350_A2_SUPPORTED
#define PICO_RP2350_A2_SUPPORTED 1
#endif

// clang-format on

#endif
