CLANG_FORMAT ?= clang-format
CLANG_TIDY ?= clang-tidy
BUILD_DIR_PICO = build/pico
BUILD_DIR_PICO2 = build/pico2
BUILD_DIR_MAINBOARD = build/mainboard
TEST_DIR = tests
TEST_BUILD_DIR = build/tests
TEST_SRC = $(TEST_DIR)/test_main.c $(TEST_SUITE_SRC)
TEST_SUITE_SRC = $(filter-out $(TEST_DIR)/test_main.c,$(wildcard $(TEST_DIR)/test_*.c))
TEST_STUB_SRC = $(wildcard $(TEST_DIR)/stubs/*.c)
TEST_UNITY_SRC = $(TEST_DIR)/unity/unity.c
TEST_APP_SRC = src/control/controller.c src/control/protocol.c src/usb_rx.c src/usb_tx.c src/usb_comm.c src/runtime_config.c src/esc_firmware/update.c src/pwm/control.c src/pwm/pwm.c src/dshot/control.c src/dshot/telemetry_usb.c src/dshot/current_sensing.c src/esc_power.c
TEST_RELEASE_VERSION = 1.0.2-rc.3
CMAKE_FLAGS = -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DPICO_SDK_FETCH_FROM_GIT=ON -DPython3_EXECUTABLE=$(shell which python3)
CMAKE_FLAGS_PICO2 = $(CMAKE_FLAGS) -DPICO_BOARD=pico2
# boards/manafish_mainboard.h, found through PICO_BOARD_HEADER_DIRS in CMakeLists.txt.
CMAKE_FLAGS_MAINBOARD = $(CMAKE_FLAGS) -DPICO_BOARD=manafish_mainboard
# GCC's internal stdint.h needs GCC-only macros; keep it after target libc/Clang headers.
ARM_GCC_INCLUDE = $(shell arm-none-eabi-gcc -print-file-name=include)
SYSROOT_A = $(shell arm-none-eabi-gcc -print-sysroot)/include
SYSROOT_B = /usr/arm-none-eabi/include
SYSROOT_C = /usr/lib/arm-none-eabi/include

.PHONY: build-pico build-pico2 build-mainboard flash-pico flash-pico2 flash-mainboard clean format format-check lint lint-check test help

build-pico:
	mkdir -p $(BUILD_DIR_PICO)
	cd $(BUILD_DIR_PICO) && cmake -S $(CURDIR) -B . $(CMAKE_FLAGS) && cmake --build .
	python3 $(TEST_DIR)/test_linker_memory.py pico $(BUILD_DIR_PICO)

build-pico2:
	mkdir -p $(BUILD_DIR_PICO2)
	cd $(BUILD_DIR_PICO2) && cmake -S $(CURDIR) -B . $(CMAKE_FLAGS_PICO2) && cmake --build .
	python3 $(TEST_DIR)/test_linker_memory.py pico2 $(BUILD_DIR_PICO2)

build-mainboard:
	mkdir -p $(BUILD_DIR_MAINBOARD)
	cd $(BUILD_DIR_MAINBOARD) && cmake -S $(CURDIR) -B . $(CMAKE_FLAGS_MAINBOARD) && cmake --build .
	python3 $(TEST_DIR)/test_linker_memory.py mainboard $(BUILD_DIR_MAINBOARD)

flash-pico: build-pico
	picotool load $(BUILD_DIR_PICO)/firmware.uf2 -f

flash-pico2: build-pico2
	picotool load $(BUILD_DIR_PICO2)/firmware.uf2 -f

# No -f: the RP2354A must already be in BOOTSEL (2e8a:000f). The mainboard has no BOOTSEL
# button: the CM5 enters it through GPIO24 (RUN) and GPIO25 (QSPI_SS), with the pinctrl
# sequence in docs/PICO_CONTROL_HANDOFF.md (mcu-recover once the CM5 image has it).
flash-mainboard: build-mainboard
	picotool load -v -x $(BUILD_DIR_MAINBOARD)/firmware.uf2

clean:
	rm -rf build

format:
	find . \( -name "*.c" -o -name "*.h" \) ! -path "./build/*" ! -path "./tests/unity/*" ! -path "./third_party/*" | xargs $(CLANG_FORMAT) -i

format-check:
	find . \( -name "*.c" -o -name "*.h" \) ! -path "./build/*" ! -path "./tests/unity/*" ! -path "./third_party/*" | xargs $(CLANG_FORMAT) --dry-run --Werror

lint: build-mainboard
	find src -name "*.c" | xargs $(CLANG_TIDY) --fix-errors \
	-p $(BUILD_DIR_MAINBOARD)/compile_commands.json \
	-header-filter="^$(CURDIR)/src/.*" \
	--extra-arg=-idirafter$(ARM_GCC_INCLUDE) \
	--extra-arg=-I$(SYSROOT_A) \
	--extra-arg=-I$(SYSROOT_B) \
	--extra-arg=-I$(SYSROOT_C)

lint-check: build-mainboard
	find src -name "*.c" | xargs $(CLANG_TIDY) --warnings-as-errors=* \
	-p $(BUILD_DIR_MAINBOARD)/compile_commands.json \
	-header-filter="^$(CURDIR)/src/.*" \
	--extra-arg=-idirafter$(ARM_GCC_INCLUDE) \
	--extra-arg=-I$(SYSROOT_A) \
	--extra-arg=-I$(SYSROOT_B) \
	--extra-arg=-I$(SYSROOT_C)

test:
	mkdir -p $(TEST_BUILD_DIR)
	cc -std=c11 -Wall -Wextra -DMANAFISH_RELEASE_VERSION='"$(TEST_RELEASE_VERSION)"' -I$(TEST_DIR)/mocks -I$(TEST_DIR) -Isrc \
		$(TEST_SRC) $(TEST_STUB_SRC) $(TEST_UNITY_SRC) $(TEST_APP_SRC) \
		-lm -o $(TEST_BUILD_DIR)/run_tests
	./$(TEST_BUILD_DIR)/run_tests
	python3 $(TEST_DIR)/test_startup_commands.py
	cc -std=c11 -Wall -Wextra -Werror -DBMI270_SENSOR_TEST -Ithird_party/bmi270 \
		$(TEST_DIR)/test_bmi270_sensor.c third_party/bmi270/bmi2.c third_party/bmi270/bmi270.c \
		-lm -o $(TEST_BUILD_DIR)/test_bmi270_sensor
	./$(TEST_BUILD_DIR)/test_bmi270_sensor
	cc -std=c11 -Wall -Wextra -Werror -DBMI270_SENSOR_TEST -Ithird_party/bmi270 \
		-DMANAFISH_IMU_AXIS_X=2 -DMANAFISH_IMU_AXIS_Y=0 -DMANAFISH_IMU_AXIS_Z=1 \
		-DMANAFISH_IMU_SIGN_X=-1 -DMANAFISH_IMU_SIGN_Y=1 -DMANAFISH_IMU_SIGN_Z=1 \
		$(TEST_DIR)/test_bmi270_sensor.c third_party/bmi270/bmi2.c third_party/bmi270/bmi270.c \
		-lm -o $(TEST_BUILD_DIR)/test_bmi270_sensor_axis_map
	./$(TEST_BUILD_DIR)/test_bmi270_sensor_axis_map
	cc -std=c11 -Wall -Wextra -Werror -DUSB_BUFFERS_TEST -I$(TEST_DIR)/usb_buffer_mocks \
		$(TEST_DIR)/test_usb_buffers.c -o $(TEST_BUILD_DIR)/test_usb_buffers
	./$(TEST_BUILD_DIR)/test_usb_buffers
	python3 $(TEST_DIR)/test_control_runtime.py
	python3 $(TEST_DIR)/test_control_smoke.py
	python3 $(TEST_DIR)/test_mainboard_bringup.py
	python3 $(TEST_DIR)/test_build_identity.py
	python3 $(TEST_DIR)/test_linker_memory.py

help:
	@echo "Available targets:"
	@echo "  build-pico      - Build firmware for Pico"
	@echo "  build-pico2     - Build firmware for Pico 2"
	@echo "  build-mainboard - Build firmware for the Manafish mainboard (RP2354A)"
	@echo "  flash-pico         - Build and flash unified firmware for Pico"
	@echo "  flash-pico2        - Build and flash unified firmware for Pico 2"
	@echo "  flash-mainboard    - Build and flash the mainboard (RP2354A already in BOOTSEL)"
	@echo "  clean           - Clean build dirs"
	@echo "  format          - Format C code"
	@echo "  format-check    - Check C code formatting"
	@echo "  lint            - Lint C code on the mainboard build (auto-fix errors)"
	@echo "  lint-check      - Check C code linting on the mainboard build (report only)"
	@echo "  test            - Build and run host unit tests"
	@echo "  help            - Show this help"
