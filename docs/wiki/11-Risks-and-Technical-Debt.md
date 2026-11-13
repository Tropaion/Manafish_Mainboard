# 11. Risks and Technical Debt

Ordered roughly by impact on safety and on the board schedule. "Owner step" is where the
risk is retired or decided: a plan phase, a bring-up step (B0-B16, Phase 6), or a hardware
revision. BOM and PCB changes are recommendations only; the owner decides. Status labels as in
[Crosscutting Concepts](08-Crosscutting-Concepts). Matching tests:
[Quality Requirements](10-Quality-Requirements).

## 11.1 Hardware risks

| Risk / debt | Impact | Mitigation | Owner step |
|---|---|---|---|
| GP14 behaviour through a reset is unknown (watchdog reboot, RUN pulse, BOOTSEL entry; RP2350 pad isolation) | ESC power could stay on while the MCU is in reset or BOOTSEL, with no DShot signal | Host rule: send OFF and wait for state 1/2 before any flash or reset; MCU tools refuse until the rov_firmware service (manafish-firmware) has been inactive for 35 s. Scope GP14 at B11; if it does not fall, the rule becomes mandatory in the protocol docs | B11 |
| Y1 slow start (12 MHz, ESR 60 Ω, 1 kΩ in series); cold BOOTSEL may fail | MCU fails to boot or to enter BOOTSEL when cold | Board target sets `PICO_XOSC_STARTUP_DELAY_MULTIPLIER 64` **[planned: Phase 2]**. Test 20 reboots and cold cycles at 0-5 °C plus cold BOOTSEL. If cold BOOTSEL fails: SWD, a change to R10, or an OTP `BOOTSEL_XOSC_CFG` write (irreversible; check the datasheet first) | B7 |
| LTC4162 charger defaults while the magnet switch is OFF: 4.0 A input, 3.2 A charge (en_jeita = 1, fixed 25 °C divider) | CM5 is off and never configures the charger: PSE trip or charge above the pack limit | Measure iin and ibat in the defaults window with Type 4 (and Type 2/3 if available). Only if a PSE trips or 3.2 A exceeds the pack limit: recommend R1 16 mΩ (2.0 A input default) and/or R20 16 mΩ (2.0 A charge default) | B10 |
| Input limit caps only charge current; VSYS load passes through | With a small PSE (Type 1: 10.5 W at 24 V) the system load alone may exceed the budget | Type-based limits plus hull derating **[planned: Phase 4]**; measure with each available PSE type | B9, B10 |
| No pack NTC (fixed 10 k / 10 k divider reads 25 °C) | No battery temperature qualification; a hot or cold pack still charges | `en_jeita = 0`; hull derating from SHT45 (> 45 °C halve, > 55 °C suspend) and LTC4162 die > 100 °C **[planned: Phase 4]**; next-revision note | Phase 4; next revision |
| `suspend_charger` very likely persists on battery power | If the CM5 suspends charging and the magnet then cuts the CM5, PoE does not charge the pack until the CM5 clears the bit or PoE and pack are removed | Suspend only on over-temperature, never on `chem`/cell checks; avoid needless toggles | Phase 4; B10 |
| No ESD protection on J1, J3, J4, J8, J12 | ESD damage on connectors handled during assembly and service | Handle with ESD precautions; next-revision note | Phase 1 README; next revision |
| Bare ESC lines GP6-9 / GP18-21: no series resistors, no TVS | Ringing, ESD or fault current from the ESC cable reaches the RP2354A pins | Park lines on SIO low while ESC power is off **[planned: Phase 2]**; check telemetry quality and back-powering at B12; series R/TVS as a next-revision recommendation | B12; next revision |
| Floating CD4013 inputs (U1 on the magnet switch board) | Floating CMOS inputs: extra off-state drain or a spurious MAG_ON toggle is possible | B2: record the off-state drain, toggle 10 times with the magnet; next-revision note | B2; next revision |
| SMBALERT, PGOOD and MAG_ON not wired to the CM5 | No interrupt for charger alerts and no warning before a magnet-off cut | Everything polled (charger at 1 Hz); hard-cut robustness (tmpfs root, atomic config save) | Phase 4; next revision |
| +5V can reach 5.25 V (R25 is 1 %) | CM5 supply at the upper limit when warm | Measure cold at B4, warm at B14 (`EXT5V_V` must stay at most 5.25 V) | B4, B14 |
| J8 USB-C has no VBUS | rpiboot needs the board powered; flashing can fail | Power the board for rpiboot; flash the eMMC on the CM5 IO Board if rpiboot fails | B3 |

## 11.2 Software and image risks

| Risk / debt | Impact | Mitigation | Owner step |
|---|---|---|---|
| NixOS spike on the CM5 fails (boot, config.txt, cameras, libav H.264) | Phase 3 image approach blocked | Spike checks 1-4 on the IO Board in one day; boot troubleshooting (`os_check=0`, dtb check); fallback Raspberry Pi OS Lite with overlayroot (about 2-3 days), only with owner approval | Phase 3 |
| Software H.264 on the CPU (BCM2712 has no hardware encoder) | CPU and thermal load in a closed hull; throttling, latency | libav encoder; go2rtc `Nice=10`, `CPUWeight=50`; service `CPUWeight=200`; camera 2 on demand; defaults 1280×960@30, 4-5 Mbit/s; 60 min soak | Phase 3; B14 |
| LTC4162 facts come from datasheet Rev A (10/18); analog.com was unreachable, the current ADI revision was not compared | A later erratum or register change could be missed | Cross-checked with the Linux driver. Re-check against the current ADI revision before enabling charger writes | Before B9 |
| CM5-side BMI270 code still in `software/` **[upstream]** | Running "Test IMU" writes the BMI270 config blob into the charger at 0x68 | Do not run it on the board; removal **[planned: Phase 4]** | Phase 4 |
| `config.json` save renames a temp file but does not fsync; unreadable JSON silently loads and saves defaults **[upstream]** | A cut at the wrong moment could reset the operator's config | tmpfs root keeps other writes off the disk; verify with 50 cuts including during a save | B15 |
| SemVer orders `-mb.N` below upstream `-rc` (`1.0.4-mb.1 < 1.0.4-rc.3 < 1.0.4`) | An update path that refuses "downgrades" would block mainboard releases; mixing `mainboard-v1.0.4.uf2` and `mainboard-v1.0.4-mb.1.uf2` picks the plain one | MCU mismatch uses version equality **[upstream]**; check every MCU/ESC update path in Phase 4; never mix plain and `-mb` UF2s | Phase 4 |
| Upstream drift and subtree conflicts | Merge conflicts in hot files (`main.c`, `runtime.c`, `pico_control.py`); removed CI paths come back | Board code in new files and NULL-checked hooks; never edit upstream version strings; sync procedure and both gates in CLAUDE.md; keep the test anchors upstream slices | Each subtree pull |
| Single eMMC, no A/B updates | A bad image or eMMC corruption needs a full reflash: open the hull, J7 nRPIBOOT jumper, rpiboot over J8 | Test images in CI and on the IO Board first; tmpfs root limits writes; config kept across image updates | Phase 3; B3 |
| Image runs static 10.10.10.10 and a DHCP server on eth0 **[upstream]** | Connected to a LAN, it hands out addresses and disrupts the network | Direct cable only (owner rule) | Always |
| Dev builds re-flashed by the rov_firmware service (manafish-firmware) | A dev MCU build is replaced by the bundled release when the mismatch is seen within 60 s of service start | Test with the service stopped or with `MANAFISH_PICO_CONTROL_DEVELOPMENT=1` | B7 |
| AM32 ESC flash blocks MCU core 0 for minutes | Host-loss timer could switch ESC power off mid-update | Timer pauses during update or recovery; `esc_power_host_activity()` after it returns **[planned: Phase 2]** | Phase 2; B12 |
| `HOST_LOSS_MS` (30 s), charge voltage (4.2 vs 4.1 V/cell), 2.0 A charge and the 3.4/3.2/3.0 V/cell thresholds are unconfirmed | Wrong limits for the pack or BMS | Owner questions 2 and 3, asked at the step | Phase 2; Phase 4 |
| Camera 2 power relies on `gpio=14=op,dh` | Camera 2 missing if the dtb's `cam1_reg` uses the pin differently | Check against the dtb; `pinctrl get 14` at B8 | Phase 3; B8 |
| Blackbox `COLUMNS` and the mock server list readings by hand **[upstream]** | New readings are not recorded or simulated | Decide in Phase 4 whether to add them | Phase 4 |
