# 9. Architecture Decisions

ADR-01 to ADR-10 are the plan's ten Decisions. ADR-11 onward come from the plan's phases and
from verified rulings that correct the plan. Status labels as in
[Crosscutting Concepts](08-Crosscutting-Concepts). Background:
[Solution Strategy](04-Solution-Strategy), risks: [Risks and Technical Debt](11-Risks-and-Technical-Debt).

| ADR | Title | Status |
|---|---|---|
| 01 | Import upstreams with `git subtree --squash` | accepted |
| 02 | NixOS on the CM5, gated by a one-day spike | accepted |
| 03 | One target: CM5 image and mainboard UF2 | accepted |
| 04 | Separate MCU board target `manafish_mainboard` | accepted |
| 05 | App contract unchanged; new values through the dynamic catalog | accepted |
| 06 | `mcu_board` keeps `pico`/`pico2`, `pico` normalised to `pico2` | accepted |
| 07 | Userspace smbus2 and lgpio, lazy lgpio import | accepted |
| 08 | Releases as GitHub release assets, `-mb.N` tags | accepted |
| 09 | MCU always flashed through hardware BOOTSEL | accepted |
| 10 | ESC power state in ATTITUDE health bits 4-6 | accepted |
| 11 | ESC power owned by an MCU state machine | accepted |
| 12 | Park motor lines on SIO low with a separate flag | accepted |
| 13 | 0x26 never answers NOT_READY from its handler | accepted |
| 14 | LTC4162 telemetry validity via `telemetry_valid` | accepted |
| 15 | PoE presence via `vin_gt_4p2v`, type via GPIO6/12 | accepted |
| 16 | LTC4162 register write discipline | accepted |
| 17 | Critical battery never cuts ESC power | proposed |
| 18 | Remove the CM5-side BMI270 code | accepted |
| 19 | Software H.264 through libav | accepted |
| 20 | Mainboard config through pydantic defaults | accepted |
| 21 | Auto re-flash only in the first 60 s, once | accepted |
| 22 | Software lint gate is `ruff check --no-fix` | accepted |
| 23 | Image workflow runs on `ubuntu-26.04-arm` | accepted |
| 24 | Firmware releases set `prerelease: false` | accepted |
| 25 | arc42 docs as Gitea wiki source in `docs/wiki/` | accepted |

### ADR-01 Import upstreams with `git subtree --squash`
- **Status:** accepted. **[done]** in Phase 0 (mcu-firmware @88bd6a0, firmware @9e282b5).
- **Context:** two upstream repos must be adapted and later merged again. Both have `v*` tags that trigger release workflows.
- **Decision:** `git subtree add --squash` from latest `main`, through remotes added with `--no-tags`. Upstream "firmware" goes into `software/`.
- **Consequences:** base SHA recorded; `git subtree pull --squash` works. Upstream tags are never fetched. Upstream CI and pre-commit paths were removed and must be removed again after each pull.

### ADR-02 NixOS on the CM5, gated by a one-day spike
- **Status:** accepted, gated by the Phase 3 spike **[planned: Phase 3]**.
- **Context:** upstream already builds the whole image: services, Python env, bundled MCU/ESC firmware and a tmpfs root that survives the magnet switch's hard cut.
- **Decision:** switch from `raspberry-pi-3.base` to nixos-raspberrypi `raspberry-pi-5.base` (same BCM2712). Spike checks 1-4 must pass on the CM5 IO Board.
- **Consequences:** if they fail, stop and report. Fallback, only with owner approval: Raspberry Pi OS Lite with overlayroot, about 2-3 days; Phase 4 unchanged.

### ADR-03 One target: CM5 image and mainboard UF2
- **Status:** accepted. MCU part **[done]** (Phase 2: CI builds pico, pico2 and the mainboard; `firmware-release.yml` ships only the mainboard UF2); CM5 image **[planned: Phase 3]**.
- **Context:** upstream targets a Raspberry Pi 3 and Pico/Pico 2 boards.
- **Decision:** drop the `pi3-imx477` configuration. Keep pico/pico2 builds in CI for host tests, lint and upstream merges; release only the mainboard UF2.
- **Consequences:** less to maintain; pico/pico2 code paths stay compiled so upstream merges keep working.

### ADR-04 Separate MCU board target `manafish_mainboard`
- **Status:** accepted **[done]** (Phase 2); on the board **[bring-up]** (B7).
- **Context:** the RP2354A has a slow-starting crystal (Y1, ESR 60 Ω, 1 kΩ series), 2 MB in-package flash, the GP14 rules and needs its own USB product string.
- **Decision:** a preprocessor-only `firmware/boards/manafish_mainboard.h` (RP2350A, 2 MB flash, `PICO_XOSC_STARTUP_DELAY_MULTIPLIER 64`, `USBD_PRODUCT "Manafish Mainboard"`, `MANAFISH_ESC_POWER_PIN 14`, IMU axis macros); no default UART, LED, VBUS, VSYS or SMPS pins. `make build-mainboard`.
- **Consequences:** stock pico2 images are not used on the board. ESC power code is a no-op stub without the pin macro.

### ADR-05 App contract unchanged; new values through the dynamic catalog
- **Status:** accepted **[planned: Phase 4]**.
- **Context:** the pilot app is a separate repo and out of scope.
- **Decision:** `RovConfig`, `RovStatus`, `SystemHealth` keep their shapes. New values are `rov.*` capability readings; ESC power is the action `rov.escPower.set`. Camera 1 stays go2rtc stream `cam`, camera 2 becomes `cam2`.
- **Consequences:** the app works without changes; camera 2 needs app work elsewhere. Every new key must be in `READING_DEFINITIONS` and the catalog fixture.

### ADR-06 `mcu_board` keeps `pico`/`pico2`, `pico` normalised to `pico2`
- **Status:** accepted **[planned: Phase 4]**.
- **Context:** the RP2354A enumerates as 2e8a:0009, like a Pico 2.
- **Decision:** default `pico2`; a stored or app-sent `pico` becomes `pico2` (`mode="before"` validator, plus normalisation in `flash_mcu_firmware` and `resolve_mcu_firmware`). `_BOARD_PREFIXES = {PICO2: "mainboard"}`.
- **Consequences:** no new enum value for the app. The `PICO` branch of `validate_dshot_speed_for_board` and its test go.

### ADR-07 Userspace smbus2 and lgpio, lazy lgpio import
- **Status:** accepted **[planned: Phase 4]**.
- **Context:** the LTC4162 charger, SHT45 and WSEN-PADS need I2C; recovery needs GPIO. Both libraries are already dependencies. lgpio installs only on aarch64, and `ty` treats an unresolved import as an error.
- **Decision:** smbus2 on I2C1, not kernel drivers. lgpio on the chip labelled `pinctrl-rp1`, imported lazily or injected.
- **Consequences:** all board code is testable on x86 with fakes (FakeSMBus, fake lgpio).

### ADR-08 Releases as GitHub release assets, `-mb.N` tags
- **Status:** accepted; tag scheme approved by the owner on 2026-11-13. Rules **[done]** in CLAUDE.md; workflow `firmware-release.yml` **[done]** (Phase 2); the first tag waits for the owner.
- **Context:** no access to upstream's S3 and minisign secrets. Upstream version strings must stay unedited for merges and config-migration lineage.
- **Decision:** `firmware-vX.Y.Z-mb.N` (X.Y.Z from `version.h`, never `-rc`) and `software-v<pyproject version>-mb.N`. The software flake fetches the UF2 from this repo's release URL; the UF2 is never built in Nix (the Pico SDK is fetched over the network).
- **Consequences:** `releaseVersionFromLock` must accept `firmware-v…` **[planned: Phase 3]**. `-mb.N` sorts below upstream `-rc` in SemVer.

### ADR-09 MCU always flashed through hardware BOOTSEL
- **Status:** accepted **[planned: Phase 4]** (CM5 path); `make flash-mainboard` without `-f` and the GPIO24/GPIO25 steps in `firmware/docs/PICO_CONTROL_HANDOFF.md` **[done]** (Phase 2); ruling: the CM5 path never uses `picotool -f`.
- **Context:** `picotool -f` needs running firmware. A blank, hung or bricked MCU needs another way.
- **Decision:** hardware BOOTSEL (GPIO24 RUN, GPIO25 QSPI_SS): GPIO24 low, GPIO25 low, 10 ms, release GPIO24, keep GPIO25 low 100 ms, release, wait for 2e8a:000f, `picotool load -v -x`. Never drive either line high. `flash-mainboard` has no `-f` and expects BOOTSEL already.
- **Consequences:** one path for every case. The workstation targets `flash-pico`/`flash-pico2` stay as upstream.

### ADR-10 ESC power state in ATTITUDE health bits 4-6
- **Status:** accepted; MCU **[done]** (Phase 2), CM5 parsing **[planned: Phase 4]**.
- **Context:** the CM5 reads only health bits 0 and 3 today.
- **Decision:** `health |= state << 4`.
- **Consequences:** no frame-size change; old hosts ignore the bits.

### ADR-11 ESC power owned by an MCU state machine
- **Status:** accepted **[done]** (Phase 2); `HOST_LOSS_MS 30000` confirmed by the owner on 2026-11-13; timing on the board **[bring-up]** (B11).
- **Context:** the switch has no current limit and a slow soft start; thrust must wait 15 s.
- **Decision:** pure `esc_power` module with time passed in: OFF_HOLD 5 s, SIGNAL_DELAY 5 s, THRUST_DELAY 15 s, STOP 2 s. On only by explicit request, nothing persisted, no force-off.
- **Consequences:** timing is host-testable. The CM5 only requests and adds the battery-plug rule.

### ADR-12 Park motor lines on SIO low with a separate flag
- **Status:** accepted **[done]** (Phase 2); back-powering check **[bring-up]** (B12).
- **Context:** the existing teardowns clear `protocol_initialized`, so settings never reconcile. `outover` does not survive the DShot pin re-init on every channel pass.
- **Decision:** a separate `outputs_parked` flag; pins on SIO, low. `control_outputs_initialized()` = `protocol_initialized && !outputs_parked`. Re-park after every protocol re-init; re-arm the DShot telemetry warning timer when lines go live.
- **Consequences:** no back-powering of unpowered ESCs, no AM32 idle-high bootloader trap. When the lines go live, the configured protocol restarts on the freshly booted ESCs; DShot then skips its blocking start-up preamble, and the idle path enables EDT.

### ADR-13 0x26 never answers NOT_READY from its handler
- **Status:** accepted; MCU **[done]** (Phase 2), CM5 **[planned: Phase 4]**.
- **Context:** the CM5 drops the whole session on any NOT_READY ACK.
- **Decision:** the 0x26 handler answers synchronously (no `pending_sequence`) with APPLIED, BUSY, INVALID or UNSUPPORTED. The host sends ESC requests before the 0x25 gate; BUSY is retried with a fresh frame on a host timer; 0x20 stays out of `REQUIRED_FEATURES`.
- **Consequences:** session-mismatch NOT_READY remains only for a dead session. ESC off after maintenance needs a new HELLO.

### ADR-14 LTC4162 telemetry validity via `telemetry_valid`
- **Status:** accepted (ruling corrects the plan) **[planned: Phase 4]**.
- **Context:** `cell_count` reads 0 without input power, so the plan's rule would mark telemetry invalid on battery only, and "suspend unless LAD 5 cells" could deadlock.
- **Decision:** valid after `telemetry_valid` (0x4A bit 0) plus one conversion wait; fixed 5 cells for scaling; check `chem`/cells only while `en_chg = 1`; never use them to release suspend.
- **Consequences:** the 5-cell check moves from B6 to B9.

### ADR-15 PoE presence via `vin_gt_4p2v`, type via GPIO6/12
- **Status:** accepted (ruling) **[planned: Phase 4]**.
- **Context:** GPIO6/12 high/high means Type 1, a non-classifying PSE or no PoE. The vin ADC is slow.
- **Decision:** present = `system_status` `vin_gt_4p2v` (0x39 bit 1); `vin_gt_vbat` (bit 2) secondary. Type from the Ag59800 PoE module truth table (Table 5).
- **Consequences:** `ok_to_charge` and similar plan names do not exist and are not used.

### ADR-16 LTC4162 register write discipline
- **Status:** accepted (ruling) **[planned: Phase 4]**; writes stay off until B9 **[bring-up]**.
- **Context:** JEITA overwrites 0x1A/0x1B every 11 ms while `en_jeita = 1`; there is no pack NTC.
- **Decision:** write 0x29 (`en_jeita = 0`) before 0x1A/0x1B; RMW of 0x14 clears `run_bsr`; never write 0x19; mask fields; sign-extend; do not port the Linux integer maths. Only `force_telemetry_on` is written before B9.
- **Consequences:** `en_jeita` reading 1 doubles as a chip-reset detector.

### ADR-17 Critical battery never cuts ESC power
- **Status:** proposed (plan open question 2, default "no") **[planned: Phase 4]**.
- **Context:** vbat is measured at BAT_IN and sags under thruster load. The owner has not yet confirmed the 3.4/3.2/3.0 V/cell thresholds or whether critical should ever cut thrusters.
- **Decision:** below 3.2 V/cell for 10 s: toast every 60 s and block new enables only. Below 3.0 V/cell for 30 s with no PoE and motors neutral: neutral, ESC off, `systemctl poweroff`.
- **Consequences:** thrust stays available until the final shutdown, which happens only with motors neutral.

### ADR-18 Remove the CM5-side BMI270 code
- **Status:** accepted **[planned: Phase 4]**.
- **Context:** the IMU is on MCU SPI1. 0x68 on I2C1 is the LTC4162 charger.
- **Decision:** delete `sensors/imu.py`, `tools/test_imu.py` and the bmi270 dependency.
- **Consequences:** running it can no longer write the BMI270 config blob into the charger.

### ADR-19 Software H.264 through libav
- **Status:** accepted **[planned: Phase 3]**.
- **Context:** BCM2712 has no hardware H.264 encoder.
- **Decision:** `withLibavEncoder = true`; go2rtc at `Nice=10`, `CPUWeight=50`, service `CPUWeight=200`; camera 2 on demand.
- **Consequences:** CPU and thermal load, measured at B14.

### ADR-20 Mainboard config through pydantic defaults
- **Status:** accepted **[planned: Phase 4]**.
- **Context:** upstream migrations are keyed by upstream versions, which this repo never edits.
- **Decision:** mainboard-only fields get pydantic defaults, never version-gated migrations.
- **Consequences:** old configs load without a migration step.

### ADR-21 Auto re-flash only in the first 60 s, once
- **Status:** accepted (ruling; corrects the plan's B7 wording). Behaviour is **[upstream]**.
- **Context:** the rov_firmware service (manafish-firmware) would overwrite dev builds on the board with the bundled release.
- **Decision:** keep upstream's 60 s window and one attempt per run; the dev override (`MANAFISH_PICO_CONTROL_DEVELOPMENT=1`) applies only to `pico-control-dev` builds. The new BOOTSEL flash steps go into the same `flash_mcu_firmware` path **[planned: Phase 4]**.
- **Consequences:** test dev builds with the service stopped or with the override.

### ADR-22 Software lint gate is `ruff check --no-fix`
- **Status:** accepted (ruling) **[done]** in CLAUDE.md.
- **Context:** `pyproject.toml` sets `fix = true` and `unsafe-fixes = true`.
- **Decision:** gates and CI run `ruff check --no-fix`.
- **Consequences:** a gate never rewrites files.

### ADR-23 Image workflow runs on `ubuntu-26.04-arm`
- **Status:** accepted (ruling) **[done]** in `software-image.yml`.
- **Context:** upstream uses this label and its actionlint config allows it; the plan said 24.04.
- **Decision:** `software-image.yml` uses `ubuntu-26.04-arm`.
- **Consequences:** none beyond the label.

### ADR-24 Firmware releases set `prerelease: false`
- **Status:** accepted (ruling) **[done]** in `firmware-release.yml`.
- **Context:** upstream's `contains(tag, '-')` would mark every `-mb` tag as a prerelease.
- **Decision:** set `prerelease: false` and `draft: false` explicitly.
- **Consequences:** releases are visible as normal releases.

### ADR-25 arc42 docs as Gitea wiki source in `docs/wiki/`
- **Status:** accepted (owner requirement, ruling) **[done]**; publishing to the Gitea wiki repo waits for the owner.
- **Context:** the owner wants arc42 docs as a Gitea wiki; the wiki is its own git repo.
- **Decision:** flat Gitea-wiki pages in this repo under `docs/wiki/`, in English, Mermaid diagrams. Each later phase updates its sections.
- **Consequences:** publishing to `<repo>.wiki.git` is a push to an external service: owner gives URL and approval first.
