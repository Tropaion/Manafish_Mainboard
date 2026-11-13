# 1. Introduction and Goals

The Manafish Mainboard puts a CM5 and an RP2354A (MCU) on one board inside the
ROV hull. It replaces upstream's Raspberry Pi 3B, the USB-connected Pico and the
wiring between them. The pilot drives the ROV from the app over an Ethernet
tether that also carries PoE power.

Status labels are explained on [Home](Home).

## 1.1 Requirements overview

| ID | The system must | Status |
|---|---|---|
| R1 | Run the control loop (AHRS, PID, thrust allocation) on the MCU at 500 Hz and drive 8 thrusters over DShot or PWM | **[upstream]** (Pico and Pico 2 builds); mainboard board target **[planned: Phase 2]** |
| R2 | Serve the app over a WebSocket on TCP port 9000: config, status, telemetry, capability catalog | **[upstream]** |
| R3 | Stream camera 1 to the app as go2rtc stream `cam`; camera 2 as `cam2`, on demand | `cam` **[upstream]** (Pi 3 hardware encoder); software H.264 on the CM5 and `cam2` **[planned: Phase 3]** |
| R4 | Switch battery power to the ESCs only on operator request: 5 s hold after any off, thrust no earlier than 15 s after switch-on | MCU side **[planned: Phase 2]**; CM5 side (`rov.escPower.set`) **[planned: Phase 4]** |
| R5 | Publish battery, PoE, hull and SoC readings to the app as `rov.*` capability readings | **[planned: Phase 4]** |
| R6 | Charge the 5S pack from PoE, with the input limit set by the PSE type and derated by hull temperature | **[planned: Phase 4]**; charger writes enabled at **[bring-up]** B9 |
| R7 | Warn on low battery and power off cleanly below 3.0 V/cell (no PoE, motors neutral) | **[planned: Phase 4]**; thresholds are an open owner question |
| R8 | Flash and recover the MCU from the CM5, also when it is blank, hung or bricked (hardware BOOTSEL) | **[planned: Phase 4]**; upstream flashes with `picotool load -f` **[upstream]** |
| R9 | Measure depth with the external MS5837 sensor | **[upstream]** |
| R10 | Survive the magnet switch's hard power cut at any moment | tmpfs root **[upstream]**; 50 cuts at **[bring-up]** B15 |
| R11 | Build the CM5 image and the MCU UF2 reproducibly; publish releases as GitHub release assets | Pi 3 image **[upstream]**; CI **[done]**, first green run **[planned: Phase 1]**; MCU release workflow **[planned: Phase 2]**; CM5 image **[planned: Phase 3]** |
| R12 | Keep the app contract unchanged | **[upstream]** contract; kept by every phase |

Final acceptance **[bring-up]**: with B0 to B16 recorded, the app at
10.10.10.10:9000 shows camera 1, the new battery, PoE and hull readings,
`escPower.set` turning on with the 15 s arming, all eight thrusters responding
with telemetry, and PoE charging limited by the PSE type.

## 1.2 Quality goals

| Priority | Goal | What it means here | Details |
|---|---|---|---|
| 1 | Thrust safety | Thrusters never start unexpectedly. ESC power never switches on by itself, is never persisted, and thrust is permitted only 15 s after switch-on. On host loss the MCU goes neutral (200 ms host gate **[upstream]**) and switches ESC power off after 30 s **[planned: Phase 2]**. | [Crosscutting Concepts](08-Crosscutting-Concepts) |
| 2 | Robust against power loss | The magnet switch cuts 5 V without warning. The CM5 boots after every cut and `config.json` stays intact. | [Deployment View](07-Deployment-View) |
| 3 | Recoverable MCU | One flashing path works for a blank, hung or bricked MCU: hardware BOOTSEL through GPIO24/GPIO25. | [Runtime View](06-Runtime-View) |
| 4 | App compatibility | `RovConfig`, `RovStatus` and `SystemHealth` keep their shapes. New values appear only through the dynamic capability catalog. | [Context and Scope](03-Context-and-Scope) |
| 5 | Upstream maintainability | Upstream changes merge with `git subtree pull --squash`. Upstream version strings and names stay unchanged. | [Architecture Decisions](09-Architecture-Decisions) |

Quality scenarios and their checks: [Quality Requirements](10-Quality-Requirements).

## 1.3 Stakeholders

| Role | Expectations |
|---|---|
| Owner (board designer and maintainer) | A working board and a simple design. Approves every commit, push, subtree pull, tag and release. Runs every `sudo`, jumper and hardware step. Decides BOM changes; the docs only recommend them. |
| ROV pilot | Video, control and readings in the app; a safe ESC power switch; battery warnings in time. |
| app maintainers (separate repo) | An unchanged contract. New readings, the `rov.escPower.set` action and the `cam2` stream arrive through existing mechanisms. App support for `cam2` is their work. |
| Upstream Manafish maintainers ([manafishrov](https://github.com/manafishrov)) | Source of `firmware/` and `software/` (AGPL-3.0). Their changes flow in by subtree pull; nothing flows back automatically. |
| Contributors and coding agents | Clear rules, quality gates and names: [CLAUDE.md](https://github.com/Tropaion/Manafish_Mainboard/blob/main/CLAUDE.md) and this wiki. |
| PCB assembler | Assembly notes from the [README](https://github.com/Tropaion/Manafish_Mainboard/blob/main/README.md), e.g. the PoE module IC5 is hand-soldered on the bottom side. |
