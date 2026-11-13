# 2. Architecture Constraints

Status labels are explained on [Home](Home). Hardware facts come from the
schematic and netlist; the PCB is fixed for this board revision.

## 2.1 Technical constraints

### Hardware

| Constraint | Consequence for the architecture |
|---|---|
| The CM5's BCM2712 has no hardware H.264 encoder. | Video is encoded in software: `rpicam-vid` with the libav encoder, served by go2rtc **[planned: Phase 3]**. Upstream uses the Pi 3 hardware encoder and builds `rpicam-apps` without libav **[upstream]**. CPU load and SoC temperature with one and two streams are measured at **[bring-up]** B14. |
| The RP2354A has 2 MB of in-package flash (stepping A4). | A separate board target `manafish_mainboard` sets the flash size, the boot stage 2 and its own USB product string **[done]**. The stock pico2 image assumes other flash and crystal settings. |
| Crystal Y1 is 12 MHz, ESR 60 Ω, with 1 kΩ in series: slow start-up. | The board target raises `PICO_XOSC_STARTUP_DELAY_MULTIPLIER` to 64 **[done]**. Cold boots and cold BOOTSEL at 0-5 °C are tested at **[bring-up]** B7. |
| The Pico SDK (2.3.1) is fetched over the network by CMake (`pico_sdk_import.cmake`), and its version is enforced. | The MCU UF2 is never built inside Nix. CI builds it, a release publishes it, and the software flake fetches it as a GitHub release asset. |
| QSPI_SS (CM5 GPIO25) is also the chip select of the in-package flash. | The CM5 drives GPIO25 low only while GPIO24 holds the MCU in reset (BOOTSEL entry), never while the firmware runs. It never drives GPIO24 or GPIO25 high; it releases them to inputs. |
| The RP2354A has no buttons and no LED; GP0-5 and GP15-17 are not connected. | Reset and BOOTSEL are done by the CM5 through GPIO24 RUN and GPIO25 QSPI_SS. The only UART access is the J13 breakout (GP22-29). |
| I2C1 (GPIO2/3) runs at most at 400 kHz: the LTC4162 and the WSEN-PADS support only up to Fast mode. | `i2c_arm_baudrate` 400000 **[planned: Phase 3]**; upstream sets 1 MHz **[upstream]**. There are no pull-ups on the board; the CM5 module has 1.8 kΩ. |
| 0x68 on I2C1 is the LTC4162 charger, not a BMI270. The BMI270 sits on the MCU's SPI1 (GP10-13). | Upstream's Pi-side BMI270 code would write the BMI270 config blob into the charger. It is removed **[planned: Phase 4]**. |
| The magnet switch is a hard 5 V cut of everything except the charger. The system comes up OFF after a battery or PoE connect. | No clean shutdown can be assumed. The CM5 runs a tmpfs root with a small persistent partition **[upstream]**. Power-cut tests at **[bring-up]** B15. |
| No RTC battery, no fan, no LEDs. No SMBALERT, PGOOD or MAG_ON wire to the CM5. | The clock is not kept across power cuts. Cooling is passive, so SoC temperature is a reading and a soak test (B14). Status is shown only in the app. Charger and sensors are polled; the CM5 cannot see the magnet switch state. |
| The IMU INT pins are not connected. | The MCU polls the BMI270 **[upstream]**. |
| The ESC power switch (GP14 / ESC_PWR_EN) is static: no current limit; soft start ramps in about 1.5 s, 12 s worst case. GP14 drives the IC16 LED through R31 180 Ω. | GP14 needs 12 mA drive and defaults low. Switch only with the motors stopped. Thrust no earlier than 15 s after switch-on; GP14 low for at least 5 s after boot, after a battery plug-in and after any off; never switch on automatically. MCU side **[done]**; the battery plug-in and motors-stopped checks on the CM5 **[planned: Phase 4]**. |
| The ESC signal lines GP6-9 and GP18-21 are bare: no series resistors, no TVS. | Parked lines are driven low by SIO so the unpowered ESCs are not back-powered **[done]**; checked at **[bring-up]** B12. |
| The LTC4162 measures vbat at BAT_IN, ahead of the ESC branch; ibat excludes thruster current (Q3 taps the battery ahead of R20). | vbat sags under thruster load, so battery logic evaluates vbat only after 2 s of neutral motors **[planned: Phase 4]**. |
| The battery-plug event is visible only to the CM5, through the LTC4162. | The CM5 enforces the "5 s after battery plug-in" rule before it requests ESC power **[planned: Phase 4]**. |
| J8 USB-C carries data only (no VBUS); the CM5 is not powered over USB. | Flashing with rpiboot needs the normal board supply. The bootloader EEPROM needs `PSU_MAX_CURRENT=5000`. |
| The RP2354A enumerates as 2e8a:0009, like a Pico 2 (BOOTSEL: 2e8a:000f). | `mcu_board` keeps the values `pico`/`pico2`; `pico` is normalised to `pico2` **[planned: Phase 4]**. |

### Software and build

| Constraint | Consequence |
|---|---|
| NixOS image with a tmpfs root (impermanence) **[upstream]**. `/etc` is tmpfs. | `systemctl edit` overrides are lost at reboot. New Python dependencies need an image rebuild. Only `/home/pi`, logs, `/nix` and a few state files persist. |
| Nix flakes see only git-tracked files. The image version is the monorepo `shortRev`. | New files must be `git add`ed before any `nix` command. Any commit, also under `hardware/`, renames the image. |
| `requires-python = "==3.13.15"` is written by `software/sync-python-deps.sh` from nixpkgs. | The pin is never edited by hand. A new dependency touches `pyproject.toml`, `nix/firmware.nix`, `sync-python-deps.sh` and `uv.lock`. |
| `lgpio` is installed only on aarch64. | GPIO code imports it lazily or gets a fake injected, so host tests run on x86_64 **[planned: Phase 4]**. |
| The host drops the whole MCU session on any NOT_READY ACK **[upstream]**. | New MCU requests (ESC_POWER 0x26) answer BUSY, never NOT_READY **[done]**. |
| `ruff check` rewrites files (`fix = true`, `unsafe-fixes = true` in `pyproject.toml`). | Gates and CI run `ruff check --no-fix`. |
| The host `clang-format` formats differently from the LLVM 19 baseline. | C is formatted and linted only inside `nix develop ./firmware`. |
| A GitHub release asset is limited to 2 GiB. | The on-device ARM/Clang/Pico SDK toolchain is dropped from the image **[planned: Phase 3]**. |

## 2.2 Organisational constraints

| Constraint | Source |
|---|---|
| Ask the owner before every commit, push and `git subtree add/pull`. Push only to `main`; there are no PRs. | [CLAUDE.md](https://github.com/Tropaion/Manafish_Mainboard/blob/main/CLAUDE.md), owner rules |
| The owner runs every `sudo` (also on the device), jumper and hardware step. | CLAUDE.md |
| Never connect a board running a Manafish image to a LAN: it has static 10.10.10.10 and a DHCP server on eth0. Use a direct cable. | CLAUDE.md |
| Prefer simple solutions. Report every pin remap. BOM changes are recommendations only. | CLAUDE.md |
| LTC4162, WSEN-PADS and SHT45 addresses, scales and bits come from the datasheet, never from memory. Charger writes stay disabled until bring-up step B9, except the `force_telemetry_on` read-modify-write. | CLAUDE.md |
| AGPL lineage: both upstreams are AGPL-3.0-or-later, and so is the original work in this repo. Third-party parts keep their terms (Raspberry Pi CM5 IO Board files, Bosch BMI270 SensorAPI, Unity, Pico SDK, TinyUSB, `dshot.c` sources, the bundled AM32 binary). | [LICENSE](https://github.com/Tropaion/Manafish_Mainboard/blob/main/LICENSE), [NOTICE.md](https://github.com/Tropaion/Manafish_Mainboard/blob/main/NOTICE.md) |
| The app contract stays unchanged: `RovConfig`, `RovStatus`, `SystemHealth` keep their shapes. The app is a separate repo. | Plan, Decision 5 |
| Upstream version strings are never edited (`firmware/src/version.h`, `software/pyproject.toml`). This avoids merge conflicts and keeps the config-migration lineage. | CLAUDE.md, tags and releases |
| Releases are GitHub release assets in this repo. No S3, no minisign (the owner does not have upstream's secrets). | CLAUDE.md |
| Never fetch upstream tags: both upstreams have `v*` tags that trigger release workflows. Remotes are added with `--no-tags`. | CLAUDE.md, upstream sync |
| After each subtree pull, `git rm` the removed upstream CI and pre-commit paths again and keep the local patches. | CLAUDE.md, upstream sync |
| No tag or release unless the owner asks; confirm the version and the notes first. `make build-mainboard` and `firmware-release.yml` exist **[done]**. | CLAUDE.md |
| Publishing this wiki to the Gitea wiki repo is a push to an external service and needs the owner's approval. | Phase 1 ruling |

## 2.3 Conventions

| Topic | Convention |
|---|---|
| Commit messages | Plain capitalised sentences, imperative, no type prefix; the body says why. Overrides the Conventional Commits rule in `firmware/AGENTS.md`. |
| MCU tags | `firmware-vX.Y.Z-mb.N`. X.Y.Z from the `MCU_FIRMWARE_VERSION_MAJOR/MINOR/PATCH` macros in `firmware/src/version.h`; never `-rc` in the tag. Asset `mainboard-vX.Y.Z-mb.N.uf2`. |
| Software tags | `software-v<pyproject version>-mb.N`, e.g. `software-v1.2.0-rc.3-mb.1`. Never write `-mb` into `pyproject.toml`. |
| Tag pushes | One tag at a time (`git push origin <tag>`), never `git push --tags`. |
| Names kept from upstream | Python import package `rov_firmware`, distribution `manafish`, service `manafish-firmware`, device path `/home/pi/firmware`. |
| C | C11, clang-format (LLVM base, indent 4, column limit 100) and clang-tidy with warnings as errors, LLVM 19 baseline. Bosch BMI270 sources stay byte-for-byte (CRLF). |
| Python | `ruff format`, `ruff check --no-fix`, `ty check`, pytest. Frozen references in `software/tests/reference/` are never edited. |
| Nix | `nix fmt` (alejandra, statix, deadnix), run from `software/`. |
| Files | `.editorconfig`: LF, final newline, 4 spaces for C and Python, 2 for Nix, tabs in Makefiles. |
| Quality gates | Run by hand before every commit; commands in CLAUDE.md. See [Quality Requirements](10-Quality-Requirements). |
| Docs | English, short sentences, tables over prose. This wiki uses the status labels from [Home](Home). |
