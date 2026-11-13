# AGENTS.md

## Purpose

Thruster firmware for the Raspberry Pi Pico / Pico 2 used by the Manafish
ROV, and for the RP2354A on the Manafish mainboard. Single binary supports two
runtime-selectable ESC protocols: DShot (digital) and PWM (analog).
Communicates with the host firmware over USB CDC.

## Stack

- C11, Pico SDK (fetched by CMake)
- arm-none-eabi-gcc, CMake, Make, picotool, picocom
- clang-format, clang-tidy
- Unity for host-side unit tests
- Nix flake + direnv for the dev shell

## Structure

- `src/` — firmware sources (`main.c`, `usb_comm.*`, `runtime_config.*`,
  `log.*`, `esc_power.*`, `dshot/`, `pwm/`, `control/`, `imu/`). Core 1 owns
  SPI and pure control; core 0 owns USB, motor outputs and safety arbitration.
- `src/esc_power.*` — mainboard ESC power switch (GP14) state machine with time
  passed in; a stub without `MANAFISH_ESC_POWER_PIN` (pico/pico2). Its
  unconditional `<hardware/gpio.h>` include brings in the board header, and
  `esc_power_wire_state()` exists only with the pin, so a mainboard build that
  compiled the stub fails to link. Read the ESC power section of
  `docs/PICO_CONTROL_PROTOCOL.md` before changing it.
- `boards/manafish_mainboard.h` — mainboard board header (RP2354A, 2 MB flash,
  crystal start-up delay, USB product, ESC power pin, IMU axis map);
  preprocessor only, found through `PICO_BOARD_HEADER_DIRS` in `CMakeLists.txt`.
- `third_party/bmi270/` — pinned official Bosch driver/config image and license;
  excluded from project formatting and lint fixes.
- `docs/PICO_CONTROL_PROTOCOL.md` — read before changing control math, USB
  framing/settings, cross-core ownership, or maintenance transitions.
- `scripts/control_smoke.py` — coordinator-operated neutral-output hardware smoke
  test; requires explicit `--execute`, stopped Pi service and disconnected ESCs.
- `scripts/mainboard_bringup.py` — coordinator-operated mainboard bench helper
  (`info`, `imu`, `esc on|off|watch`); imports `control_smoke.py`, requires
  `--execute` and a stopped Pi service; port `/dev/manafish-mcu`.
- `tests/` — Unity tests (`test_*.c`), `mocks/`, `stubs/`, `support/`,
  `unity/`, and `test_startup_commands.py` (compiles the real startup function
  with recording C stubs), the standalone Bosch SPI/USB buffer emulators, and
  `test_control_runtime.py` (real runtime/transport against a fake SDK),
  `test_control_smoke.py` (offline checks of bench evidence handling),
  `test_mainboard_bringup.py` (offline checks of the mainboard bench helper), and
  `test_build_identity.py` (CMake release/development metadata and real USB replies;
  requires host CMake, not the Pico SDK).
- `CMakeLists.txt`, `pico_sdk_import.cmake` — build setup
- `Makefile` — wraps CMake for the common targets
- `flake.nix` — host toolchain (ARM GCC, Clang, CMake). CMake fetches the
  SDK pinned in `pico_sdk_import.cmake` in both Nix and CI; no shell SDK override.
- `cmake/control_memory.cmake` — checked SDK linker-fragment overrides for the
  real 16 KiB core0 stack. Preserve upstream TLS/platform sections and core1's
  explicit 8 KiB BSS stack; do not fix SDK upgrades by changing only a path.

## Commands

Use the dev shell (`direnv allow` in repo, or `nix develop`). Then:

- Help: `make help`
- Build: `make build-pico` / `make build-pico2` / `make build-mainboard`
- Flash: `make flash-pico` / `make flash-pico2` / `make flash-mainboard`. The
  mainboard has no BOOTSEL button and `flash-mainboard` no `-f`: the RP2354A must
  already be in BOOTSEL, entered by the CM5 through GPIO24 (RUN) and GPIO25
  (QSPI_SS); see `docs/PICO_CONTROL_HANDOFF.md`.
- Clean: `make clean`
- Debug serial: `picocom -b 115200 /dev/ttyACM0` (Linux) or
  `/dev/tty.usbmodem*` (Darwin)

### Quality (must pass before flashing or merging)

```sh
make format-check
make lint-check          # clang-tidy with -Werror on the mainboard compile DB
make test                # Unity, Python/C regressions and memory-verifier tests
make build-pico         # includes real ELF/map memory verification
make build-pico2        # includes real ELF/map memory verification
make build-mainboard    # includes real ELF/map memory verification
```

Auto-fix: `make format`, `make lint`.

Pre-commit hook runs `clang-format` on staged files. Install once:
`pre-commit install`.

## Rules

- Keep DShot and PWM behind the runtime selector in `runtime_config.*`. Don't
  fork the codebase per protocol.
- USB protocol lives in `usb_comm.*`. Changes here must be reflected in
  whatever host (firmware/app) consumes it.
- Match existing C style; no warnings in `lint-check`.
- Nix and CI use LLVM 19 as the validated lint/format baseline. CI selects the
  versioned binaries through Make's `CLANG_FORMAT` / `CLANG_TIDY` overrides.
  Before upgrading it, assess new diagnostics separately; preserve controller math
  and vendor bytes rather than folding a broad source cleanup into a lock update.
- In lint commands, keep GCC-internal headers after target libc and Clang headers
  with `-idirafter`; prioritizing GCC's `stdint.h` breaks Clang constant macros.
- Don't widen the toolchain (extra deps, alternative SDKs) without reason.
- Don't push without being asked. Required CI builds Pico, Pico 2 and the
  mainboard and runs `tests/test_linker_memory.py` on each ELF/map. Release
  builds run the same verification. A host/mock test or a build against an
  older SDK does not validate an SDK upgrade. Record the actual configured SDK
  path/version.

## Releases

**Never cut a release without being explicitly asked, and confirm the version
and release notes back to the user before doing anything.**

In the Manafish mainboard monorepo the root `CLAUDE.md` overrides this file on
commits (plain capitalised sentences, no Conventional Commits), tags, releases,
CI and pre-commit; its "Tags and releases" section is authoritative and this
section is a summary.

- Tag: `firmware-vX.Y.Z-mb.N`. X.Y.Z comes from the
  `MCU_FIRMWARE_VERSION_MAJOR/MINOR/PATCH` macros in `src/version.h`; don't bump
  them. N counts mainboard releases of that X.Y.Z, starting at 1. Never put
  `-rc` in the tag.
- No release commit. Push only that tag: `git push origin firmware-vX.Y.Z-mb.N`,
  never `git push --tags`.
- The tag triggers the root `.github/workflows/firmware-release.yml`. It checks
  X.Y.Z against `src/version.h`, builds with
  `-DMANAFISH_RELEASE_VERSION=X.Y.Z-mb.N` and publishes (not as a draft, not as
  a prerelease) `mainboard-vX.Y.Z-mb.N.uf2` with the third-party notices
  (`NOTICE.md`, BMI270, Pico SDK, pico_printf, TinyUSB and pio-dshot licenses).
- The quality gates above must pass first.

Workflow before tagging: confirm the version with the user, confirm the release
notes text, then tag and push the tag. Once the workflow has published, set the
confirmed notes with `gh release edit <tag> --notes-file <file>`.

## Commits

Conventional Commits, focused on **why**.

```
<type>(<scope>): <subject>

[body explaining why, ~72 char wrap]
```

- Types: `feat`, `fix`, `refactor`, `perf`, `docs`, `chore`, `ci`, `build`,
  `revert`. `chore(deps)` reserved for Renovate.
- Scopes: `control`, `imu`, `dshot`, `pwm`, `usb`, `config`, `log`, `tests`, `cmake`, `flake`,
  `ci`.
- Subject: imperative, lowercase, ≤72 chars, no period.

## Keep this file useful

If you add a top-level source dir, change Makefile targets, swap toolchain
pieces, or alter the quality gates — update this file in the same commit.
