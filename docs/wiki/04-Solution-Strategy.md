# 4. Solution Strategy

The port reuses upstream's code and changes only what the board requires.
Upstream already runs the control math (AHRS, PID, allocation) on the MCU, and
the board keeps the Pico's pin wiring. So the port adds a CM5 image, software
H.264, the ESC power switch, new I2C devices with the charger, hardware MCU
recovery, and bring-up. Status labels are explained on [Home](Home).

## 4.1 Goals and problems to approaches

| Goal or problem | Approach | Status | Where documented |
|---|---|---|---|
| Reuse upstream and keep merging it | `git subtree add --squash` of both upstreams through `--no-tags` remotes; later `git subtree pull --squash`. Names, version strings and device paths stay as upstream has them. | **[done]** | [Architecture Decisions](09-Architecture-Decisions), [CLAUDE.md](https://github.com/Tropaion/Manafish_Mainboard/blob/main/CLAUDE.md) |
| Real-time control | Keep AHRS, PID and allocation on the RP2354A (MCU) at 500 Hz. The CM5 sends CONTROL frames and receives ATTITUDE at 60 Hz over USB CDC. | **[upstream]** | [Building Block View](05-Building-Block-View), [Runtime View](06-Runtime-View) |
| One MCU target for the board | Board header `firmware/boards/manafish_mainboard.h`: 2 MB flash, crystal start-up margin, USB product "Manafish Mainboard", ESC power pin 14. Pico/pico2 builds stay in CI for host tests and upstream merges; only the mainboard UF2 is released. | **[done]** | [Building Block View](05-Building-Block-View), [Deployment View](07-Deployment-View) |
| Thrust safety with a static ESC switch | The MCU owns the ESC power switch (GP14 / ESC_PWR_EN) through a pure state machine: off by default, 5 s hold, lines parked low, 15 s to thrust, 2 s neutral before off, auto-off 30 s after host loss. Thrust is gated by `thrust_permitted`. The CM5 adds the conditions it alone can see (battery plug-in, charger health, critical battery). | **[done]** MCU, **[planned: Phase 4]** CM5 | [Runtime View](06-Runtime-View), [Crosscutting Concepts](08-Crosscutting-Concepts) |
| Extend the MCU protocol without breaking hosts | New request ESC_POWER 0x26 that never answers NOT_READY; optional CAPS feature bit 0x20; state in unused ATTITUDE health bits 4-6 (the host reads only bits 0 and 3). | **[done]** | [Building Block View](05-Building-Block-View) |
| Recover a blank, hung or bricked MCU | One flashing path for all cases: neutral, ESC off, then hardware BOOTSEL (GPIO24 RUN, GPIO25 QSPI_SS), then `picotool load -v -x` without `-f`. Tools `mcu-reset` and `mcu-recover` for manual use. | **[planned: Phase 4]** | [Runtime View](06-Runtime-View) |
| Find the MCU reliably | udev symlink `/dev/manafish-mcu` for 2e8a:0009, then a sysfs scan; no blind `ttyACM*` fallback. | **[planned: Phase 3]** udev, **[planned: Phase 4]** Python | [Building Block View](05-Building-Block-View) |
| Survive hard power cuts | NixOS with impermanence: tmpfs root, small persistent ext4 partition for `/home/pi`, logs and `/nix`. | **[upstream]**; CM5 image **[planned: Phase 3]** | [Deployment View](07-Deployment-View), [Crosscutting Concepts](08-Crosscutting-Concepts) |
| CM5 operating system | Keep NixOS and switch to nixos-raspberrypi `raspberry-pi-5.base` (same BCM2712). A one-day spike on the CM5 IO Board decides; fallback Raspberry Pi OS Lite only with owner approval. | **[planned: Phase 3]** | [Deployment View](07-Deployment-View), [Architecture Decisions](09-Architecture-Decisions) |
| No hardware H.264 encoder | `rpicam-vid` with the libav encoder; go2rtc serves `cam` (camera 1) and `cam2` (camera 2, on demand) with lower CPU priority than the service. | **[planned: Phase 3]** | [Deployment View](07-Deployment-View), [Quality Requirements](10-Quality-Requirements) |
| Charger and hull sensors | Userspace smbus2 drivers, not kernel drivers; GPIO through lgpio on the `pinctrl-rp1` chip. Policy as pure functions. All tested with fakes (FakeSMBus, fake lgpio, fake sysfs, injectable clock). | **[planned: Phase 4]** | [Building Block View](05-Building-Block-View), [Crosscutting Concepts](08-Crosscutting-Concepts) |
| Safe charger bring-up | `CHARGER_WRITES_ENABLED` stays false until B9. The only write before that is the `force_telemetry_on` read-modify-write, so telemetry runs on battery only. Telemetry is valid only after `telemetry_valid` (0x4A bit 0) and one conversion. | **[planned: Phase 4]**; enabled at **[bring-up]** B9 | [Crosscutting Concepts](08-Crosscutting-Concepts), [Risks and Technical Debt](11-Risks-and-Technical-Debt) |
| PoE power budget | PSE type from GPIO6/GPIO12 sets the charger input limit; presence from LTC4162 `vin_gt_4p2v`. Hull temperature derates charging. | **[planned: Phase 4]** | [Crosscutting Concepts](08-Crosscutting-Concepts) |
| Battery state despite thruster sag | Battery % from LTC4162 vbat as a 10 s average; low-battery logic evaluates only after 2 s of neutral motors. It switches ESC power off only as part of the 3.0 V/cell shutdown. | **[planned: Phase 4]** | [Runtime View](06-Runtime-View) |
| App contract unchanged | New values are `rov.*` capability readings; ESC power is the action `rov.escPower.set`; both appear through the dynamic catalog. `RovConfig`, `RovStatus`, `SystemHealth` unchanged. | **[planned: Phase 4]** | [Context and Scope](03-Context-and-Scope), [Building Block View](05-Building-Block-View) |
| No upstream secrets | Releases are GitHub release assets in this repo, tags `firmware-vX.Y.Z-mb.N` and `software-v<version>-mb.N`. The software flake fetches the UF2 as a release asset. | tag scheme and release workflow **[done]**; first release waits for the owner | [Deployment View](07-Deployment-View) |
| Keep quality without local aarch64 builds | Root CI: firmware CI, software CI (Python and Nix jobs), image build on `ubuntu-26.04-arm` as a 7-day artifact. Gates by hand before every commit. | gates and CI **[done]** | [Quality Requirements](10-Quality-Requirements) |
| Board risk | Bring-up tooling (`tools board-check`, `firmware/scripts/mainboard_bringup.py`, `BRINGUP.md`), then steps B0 to B16, stop at the first unexplained failure. | bring-up script **[done]**; `board-check` and `BRINGUP.md` **[planned: Phase 5]**; steps **[bring-up]** | [Risks and Technical Debt](11-Risks-and-Technical-Debt) |

## 4.2 Technology choices

| Area | Technology | Status |
|---|---|---|
| MCU firmware | C11, Pico SDK 2.3.1, TinyUSB CDC, PIO DShot, Unity host tests | **[upstream]** |
| CM5 software | Python 3.13 asyncio, websockets, pydantic, pyserial-asyncio-fast, ms5837 | **[upstream]** |
| CM5 hardware access | smbus2 (I2C), lgpio (GPIO) | dependencies **[upstream]**; use **[planned: Phase 4]** |
| CM5 OS | NixOS, nixos-raspberrypi, impermanence, flakes | **[upstream]** (Pi 3); CM5 **[planned: Phase 3]** |
| Video | rpicam-vid, go2rtc (WebRTC) | **[upstream]**; libav and `cam2` **[planned: Phase 3]** |
| Python tooling | uv, ruff, ty, pytest | **[upstream]** |
| CI and releases | GitHub Actions, GitHub release assets | CI and release workflow **[done]** |
| Docs | arc42 in a Gitea wiki, source in `docs/wiki/` | **[done]**; publishing to the Gitea wiki repo waits for the owner |

## 4.3 Organisation

Work runs in phases 0 to 6 (see [Home](Home)). The owner approves every commit,
push and tag and runs every `sudo` and hardware step. Each later phase updates
the wiki sections it touches: building blocks, runtime, deployment, decisions,
risks.
