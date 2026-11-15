# Mainboard bring-up

The procedure and record sheet for the first power-up of each Manafish mainboard (plan
Phase 6, steps B0 to B16). This file says what to do, what passes and what to write down.
The reasons live in the wiki under [docs/wiki](docs/wiki/Home.md).

Contents: 1 rules, 2 equipment, 3 tools, 4 procedure B0-B16, 5 before the boards
arrive, 6 record sheet.

## 1. Rules at the bench

| Rule | Why |
|---|---|
| Direct cable only. Never connect a board that runs the image to a LAN. At B9 and B16 the PoE injector or switch connects only the board and the workstation. | The image uses static 10.10.10.10 and runs an authoritative DHCP server on eth0 ([07 Deployment View](docs/wiki/07-Deployment-View.md), 7.6). |
| Stop at the first unexplained failure. Record every result, passes too. | A later step can hide or worsen an earlier fault. |
| Never connect PoE while a non-sinking bench supply is on J6. | With PoE present the LTC4162 charges into J6. |
| ESCs stay unplugged until B12. B11 uses a dummy load on J14. | The switch and the ESC power firmware are untested before B11. |
| Plug cameras in only while the board is unpowered. The HQ camera needs the 22-to-15 cable. | [07 Deployment View](docs/wiki/07-Deployment-View.md), 7.2. |
| Charger writes start at B9. Until then `CHARGER_WRITES_ENABLED = False` (`software/src/rov_firmware/sensors/charger.py`): the service writes only `force_telemetry_on`, and `tools board-check` writes nothing. | Owner rule; registers are verified first ([08 Crosscutting Concepts](docs/wiki/08-Crosscutting-Concepts.md), 8.2). |
| The owner runs every workstation `sudo`, every jumper and every hardware step. On the CM5, `sudo` takes the password `manafish`. | Owner rule (CLAUDE.md). |
| MCU tools and scripts run only while `manafish-firmware` is stopped. `tools mcu-reset` and `tools mcu-recover` refuse until it has been stopped for 35 s. | The service owns the MCU port and re-flashes on a version mismatch ([06 Runtime View](docs/wiki/06-Runtime-View.md), 6.2). |
| Never drive GPIO24 or GPIO25 high. GPIO25 goes low only while GPIO24 holds the MCU in reset. | GPIO25 is the MCU's flash chip select ([06 Runtime View](docs/wiki/06-Runtime-View.md), 6.3). |
| Report every pin remap. BOM changes are recommendations; the owner decides. | Owner rule. |

## 2. Equipment per step

Plan open question 5 (what is on hand) is still open. This table lists what each step
needs, not what exists.

| Step | Needs |
|---|---|
| B0 | Magnifier or microscope, multimeter (resistance) |
| B1 | 1500 V 10/700 µs impulse tester, insulation tester at 500 Vdc |
| B2 | Bench supply (18.5 V, 100 mA limit) with an XT60 lead for J6, multimeter with a low current range, magnet, scope |
| B3 | CM5 with eMMC, workstation with Nix and `~/usbboot`, USB-C data cable for J8, two jumpers, bench supply with a higher current limit than B2 |
| B4 | Ethernet cable straight to the workstation (for example a USB-Ethernet adapter that does 1000 Mb/s), multimeter |
| B5 | Two-channel scope |
| B6 | MS5837 depth sensor on J1, scope |
| B7 | Multimeter or scope on GP14 and J14, a cold chamber or freezer for 0-5 °C, thermometer; optional Raspberry Pi Debug Probe for SWD |
| B8 | Two HQ cameras (imx477) with 22-to-15 cables, a way to measure CM5_3.3V current (shunt or current probe), scope |
| B9 | Type 4 PoE injector or switch, Type 2 if available; iperf3 on the workstation (`nix shell nixpkgs#iperf3`) |
| B10 | Real 5S pack, PoE PSE (Type 4; Type 2/3 if available), DC clamp meter, thermometer or thermal camera |
| B11 | Dummy load for J14 (XT60), pack or bench supply on J6, scope, cold chamber |
| B12 | Two 4-in-1 AM32 ESCs, eight thrusters without props, the app |
| B13 | Thrusters in a safe test setup, the app, a level surface |
| B14 | Closed hull, water tank, both cameras, pack, PoE PSE |
| B15 | Scope, bench supply on J6 that can be set below 15 V |
| B16 | Water tank, thrusters, iperf3 |

## 3. Tools

### 3.1 `tools board-check`

A read-only check of everything the CM5 can see. Code:
`software/src/tools/board_check.py`. Run it in an SSH session on the CM5 as `pi`, with
the service stopped:

```sh
sudo systemctl stop manafish-firmware
tools board-check
tools board-check --json > board-check.json
sudo systemctl start manafish-firmware
```

Then copy the JSON to the workstation and name it after the board and the step:

```sh
scp pi@10.10.10.10:board-check.json board-check-<board>-<step>.json
```

| Exit code | Meaning |
|---|---|
| 0 | No FAIL. WARN is allowed. |
| 1 | At least one FAIL. |
| 2 | Refused: `manafish-firmware` is not stopped (any state but inactive or failed), or its state is unreadable. Nothing was checked. The reason goes to stderr, so a `--json` file stays empty. Stop the service and run again. |

- Output: one line per check, `STATUS  id  summary`. Status is PASS, WARN or FAIL.
- `--json` writes `{"checks": [{"id", "status", "summary", "details": {...}}], "exit_code": N}`.
  Anything a check or a library prints then goes to stderr, so stdout holds only the JSON.
- Each check catches its own errors and reports them as FAIL. One broken check never stops
  the others.
- It writes no register. The only I2C writes are the command or pointer bytes a read needs:
  SHT45 0x89, the WSEN-PADS pointers 0x0F and 0x11, LTC4162 SMBus word reads, the MS5837
  reset and PROM reads. It never writes the LTC4162, not even `force_telemetry_on`.
- It claims no GPIO: one `pinctrl get 6,12,14,24,25` serves `poe.type`, `gpio.mcu_lines` and
  `gpio.camera2_enable`; if it fails, all three FAIL with its error. It never opens the MCU
  serial port, never runs picotool and never pulses GPIO24/25.
- The LTC4162 telemetry is read once and serves `i2c.ltc4162`, `poe.type` and
  `charger.telemetry`.

| id | Reads | PASS when | WARN or FAIL means, what to do |
|---|---|---|---|
| `system.model` | `/proc/device-tree/model` | contains "Compute Module 5" | FAIL: not a CM5, or the wrong device tree. Stop. |
| `system.kernel` | `uname` release | always (info) | Record it. |
| `system.config_txt` | `/boot/firmware/config.txt` | every line in `CONFIG_TXT_LINES` (the FAIL summary lists missing ones; the generated file is in [07](docs/wiki/07-Deployment-View.md) 7.2) | FAIL: wrong or old image. Reflash (B3). |
| `i2c.sht45` | SHT45 at 0x44 on bus 1: serial number (0x89, 10 ms, 6 bytes, CRC per word) | serial read, CRCs match | FAIL (NACK or CRC): IC12 soldering, SDA/SCL, pull-ups (B6). |
| `i2c.pads` | WSEN-PADS at 0x5C: DEVICE_ID (0x0F) and the auto-increment bit in CTRL_2 (0x11) | reads 0xB3, auto-increment on (its default) | FAIL NACK: IC10 (pin 1, SAO strapped to GND, CS to VDD). Another ID: wrong part. Auto-increment off (on by default): something wrote CTRL_2, power-cycle; or a wrong part. The service's 5-byte burst needs it. |
| `i2c.ltc4162` | LTC4162 at 0x68: `chem_cells` (0x43), `en_chg` in `system_status` | chem = LAD, `en_chg` 1 and cell_count 5 | WARN `en_chg` 0 or cell_count 0: chem and count cannot be checked (chem 0 is also the register default); normal without input power, the charger reports them only while it runs (B2-B8). FAIL another chem or count while `en_chg` is 1: wrong part or CELLS strap. FAIL NACK: check DVCC (IC8 pin 15 on +3.3V). |
| `i2c.ms5837` | MS5837 at 0x76: PROM with CRC check (ms5837 library) | PROM CRC OK | FAIL: sensor or cable on J1. |
| `i2c.unexpected` | every address 0x08-0x77 on bus 1 except the four above and 0x0C (the LTC4162 SMBus alert response address: a read there would acknowledge an alert), receive-byte probe only | nothing answers | WARN lists the addresses: an address-pin bridge or a wrong part. FAIL on a bus error (timeout instead of a NACK) at the first such address: SDA/SCL stuck low or pull-ups missing (B6). |
| `poe.type` | GPIO6 and GPIO12 from `pinctrl get`, LTC4162 `system_status` | both pins `ip pu`; type decoded with `power_policy.poe_type`: "no PoE" or the PSE type | FAIL: a pin is not `ip pu` (config.txt). WARN: charger unreadable, raw pin levels in details (see `i2c.ltc4162`). |
| `gpio.mcu_lines` | GPIO24 and GPIO25 from `pinctrl get` | both `ip pn` and high | FAIL low: RUN or QSPI_SS held low (R9/R11, R12/R13, MCU), or the lines are not wired. Wrong mode: config.txt. |
| `gpio.camera2_enable` | GPIO14 from `pinctrl get` | `op dh` and high | FAIL: config.txt, or a short on camera 2 power (J11). |
| `usb.mcu` | sysfs USB devices with idVendor 2e8a | exactly one 0009; details: sysfs USB path, product, whether `/dev/manafish-mcu` exists | WARN: product not "Manafish Mainboard" or no symlink (wrong firmware, udev), or a 000f next to the 0009. FAIL: no 0009 (blank, in BOOTSEL, unpowered, Y1, USB lines; flash at B7), or more than one 0009 (a second RP2 device on the USB). |
| `charger.telemetry` | vbat, vin, vout, ibat, iin, die temperature, `charger_state`, `charge_status`, `system_status` flags, `telemetry_valid` | readable and `telemetry_valid` set | WARN `telemetry_valid` clear: on battery only the charger measures only after the service has set `force_telemetry_on`; start the service, wait 10 s, stop it, run again. FAIL: see `i2c.ltc4162`. Compare the values with the step. |
| `charger.settings` | `vcharge_setting` (V/cell), `charge_current_setting` (A), `iin_limit_target` (A), `config_bits`, `charger_config_bits` (`en_jeita`, `en_c_over_x_term`); `matches` in the details names the set they equal | readable and a known set | FAIL: unreadable. WARN when `matches` is "neither": something other than the service wrote the charger. `matches`: "power-on defaults", "power-on defaults + force_telemetry_on", "service targets (<PoE type>, <thermal level>)" ("suspended" means heat or a chemistry mismatch; "normal, chemistry fault" when only the chemistry suspended it), or "neither". Before B9 expect the defaults, from B9 the service targets (both below). |
| `camera.count` | `rpicam-hello --list-cameras` (20 s timeout) | 2 cameras | WARN 1: camera 2 cable, `pinctrl get 14`. FAIL 0 or error: cables, overlays. FAIL is expected before B8. |
| `soc.temperature` | `/sys/class/thermal/thermal_zone0/temp` | below 75 °C | WARN 75-85 °C, FAIL from 85 °C: cooling. |
| `soc.throttled` | `vcgencmd get_throttled` | 0x0 | FAIL: a "now" bit (mask 0xF). WARN: only "occurred" bits since boot. Flags named as in `soc_monitor`. Check `soc.ext5v`, `soc.rpi_volt` and the temperature. |
| `soc.ext5v` | `vcgencmd pmic_read_adc EXT5V_V` | 4.90-5.25 V | WARN 4.75-4.90 V. FAIL below 4.75 V (weak supply or +5V rail) or above 5.25 V (+5V too high for the CM5). |
| `soc.rpi_volt` | hwmon `rpi_volt` `in0_lcrit_alarm`, `journalctl -k -b` | alarm 0 and no "Undervoltage detected" this boot | FAIL: alarm 1 now. WARN: undervoltage events this boot (count in details). If the journal is unreadable, the alarm decides and the details say so. |
| `eth0.link` | `/sys/class/net/eth0/speed` and `duplex` | 1000 and full | FAIL: cable, workstation adapter, MagJack IC3. |
| `eth0.errors` | eth0 statistics: `rx_errors`, `rx_crc_errors`, `rx_frame_errors`, `rx_missed_errors`, `rx_over_errors`, `rx_fifo_errors`, `tx_errors`, `tx_carrier_errors` | all 0 | WARN lists the non-zero counters: cable, PoE (B9), EMI (B16). |
| `service.states` | `manafish-setup`, `dnsmasq`, `go2rtc` | all active | FAIL names them: `systemctl status <unit>`, `journalctl -b -u <unit>`. |
| `service.failed` | `systemctl --failed --no-legend --plain` | empty | FAIL lists the units. |

Charger settings; the steps refer to them as "the defaults (3.1)" and "the service targets":

- Power-on defaults (`ltc4162.DEFAULT_SETTINGS`): vcharge 31 = 4.20 V/cell, charge current
  31 = 3.2 A, input limit 63 = 4.0 A, `en_jeita` 1, C/x 0, `config_bits` empty. Once the
  service has run, `config_bits` also has `force_telemetry_on` (its one write before B9), so
  `matches` reads "power-on defaults + force_telemetry_on". Both count as the defaults.
- Service targets from B9 (`power_policy.charger_targets`): 4.20 V/cell, 2.0 A, the PSE
  type's input limit (halved while the hull reads above 45 °C or unknown), `en_jeita` 0,
  C/x 0, `config_bits` = `force_telemetry_on`.

On the CM5 IO Board (section 5) exit code 1 is normal: `i2c.sht45`, `i2c.pads`,
`i2c.ltc4162`, `i2c.ms5837`, `charger.*` and `usb.mcu` FAIL (nothing on I2C1, no MCU),
`i2c.unexpected` PASSes, `poe.type` WARNs, `camera.count` WARNs with one camera, and
`gpio.mcu_lines` FAILs unless the QR-5 pull-ups are wired.

### 3.2 MCU bench scripts

`firmware/scripts/control_smoke.py` and `mainboard_bringup.py` are not in the image. Copy
both together (the second imports the first), from the repo root on the workstation:

```sh
scp firmware/scripts/control_smoke.py firmware/scripts/mainboard_bringup.py pi@10.10.10.10:
```

Run them on the CM5 with `python3` (the image's system Python 3.13 has pyserial 3.5),
while `manafish-firmware` is stopped. Both refuse without `--execute`. Put `--port` and
`--execute` before the subcommand of `mainboard_bringup.py`.

| Command | Does | Pass |
|---|---|---|
| `python3 control_smoke.py --port /dev/manafish-mcu --execute` | Neutral-output smoke test: CAPS, a zero-power settings image, 12 s of CONTROL and PRESSURE (`--seconds N`, at least 6), then host expiry. ESCs must be disconnected. Its default port is `/dev/ttyACM0`, so `--port` is needed | `CAPABILITIES` shows features 0x3f; `STATS` lines show `ahrs_hz` and `pid_hz` about 500 (its own backpressure test accepts 495-505), `misses` 0, `queue_overflows` 0; it ends with `RESULT`. Any exception is a FAIL ("IMU is not healthy" names the IMU) |
| `... control_smoke.py ... --backpressure` | Also pauses host reads for 4 s and checks the 500 Hz timing under USB backpressure | ends with `BACKPRESSURE_RESULT` |
| `python3 mainboard_bringup.py --port /dev/manafish-mcu --execute info` | CAPS and decoded feature bits; opens no session | `FEATURES` lists "ESC power" |
| `... mainboard_bringup.py ... imu --seconds 5` | 1 s means of the raw IMU frames (0x92) | `healthy True`; level reads about (0, 0, -9.81) m/s² in FRD once the axis map is set (B13) |
| `... mainboard_bringup.py ... esc on` | Applies zero-power settings, then requests ESC power on (0x26) and prints the result | see B11 |
| `... mainboard_bringup.py ... esc off` | Requests ESC power off | see B11 |
| `... mainboard_bringup.py ... esc watch --seconds 60` | Prints each ESC power state change with timestamps (default 40 s); sends nothing after HELLO | see B11 |

`control_smoke.py` leaves the MCU in sticky neutral maintenance. Starting the service
negotiates a new session. ESC power stays on after `esc on` only while a host keeps
talking: the MCU switches it off 30 s after the last host packet.

ESC power states (ATTITUDE health bits 4-6): 0 absent, 1 off (hold), 2 off (ready),
3 starting or arming, 4 on, 5 stopping
([06 Runtime View](docs/wiki/06-Runtime-View.md), 6.4).

### 3.3 MCU reset, recovery and BOOTSEL by hand

`tools mcu-reset` (10 ms RUN pulse) and `tools mcu-recover [UF2]` (hardware BOOTSEL, then
`picotool load -v -x` of the given or the bundled image) are in the image. Both refuse until
`manafish-firmware` has been stopped for 35 s, and while the ESC recovery journal is set
([08](docs/wiki/08-Crosscutting-Concepts.md), 8.4).

The BOOTSEL sequence by hand (`firmware/docs/PICO_CONTROL_HANDOFF.md`), service stopped for
35 s:

```sh
pinctrl set 24 op dl
pinctrl set 25 op dl
sleep 0.01
pinctrl set 24 ip pn
sleep 0.1
pinctrl set 25 ip pn
lsusb -d 2e8a:000f
```

The MCU then waits in BOOTSEL. `tools mcu-recover` flashes it, `tools mcu-reset` restarts
the firmware already in flash.

## 4. Procedure B0-B16

| Step | Content | Power | board-check |
|---|---|---|---|
| B0 | Inspection | none | |
| B1 | Insulation | none (tester) | |
| B2 | Power path and magnet switch | bench supply on J6, no CM5 | |
| B3 | EEPROM and eMMC | bench supply, CM5 fitted | |
| B4 | First boot | bench supply | yes |
| B5 | GPIO24/25 | bench supply | partly |
| B6 | I2C1 | bench supply | yes |
| B7 | MCU | bench supply | `usb.mcu` |
| B8 | Cameras | bench supply | `camera.count` |
| B9 | PoE without battery, charger writes on | PoE only | yes |
| B10 | Charger with a 5S pack | pack, PoE | `charger.*`, `i2c.ltc4162` |
| B11 | ESC switch, dummy load | pack or bench supply | |
| B12 | ESCs, no props | pack | |
| B13 | DShot, failsafe, IMU axes | pack | |
| B14 | Thermal soak | pack, PoE | `soc.*` |
| B15 | Power loss | pack or bench supply | `service.failed` |
| B16 | EMI | pack, PoE | `eth0.errors` |

Each step below gives its preconditions, then actions with pass criteria. Fill the record
sheet (section 6) as you go.

### B0 Inspection, no power

Preconditions: no CM5, nothing connected.

| # | Action | Pass |
|---|---|---|
| 1 | Inspect the hand-soldered IC5 (PoE module, bottom side) and IC3 (MagJack) | joints complete, no bridges |
| 2 | Check pin 1 of IC10 and of D2-D5 | orientation matches the silkscreen |
| 3 | Check the polarity of D1, D6, D7, C20, C39 | correct |
| 4 | Look for bridges on the CM5 connectors | none |
| 5 | Measure BAT_IN, VSYS, 24V, +5V_AON, +5V and ESC_PWR to GND | no short |

### B1 Insulation, no CM5

Use the 1500 V 10/700 µs impulse test only, at normal air pressure. Never a 1500 Vrms or
2250 Vdc hipot: the Ag59824 is rated for the impulse test only, and 2250 V exceeds the
Y-capacitors' 2 kV (README, "Testing the isolation").

| # | Action | Pass |
|---|---|---|
| 1 | Impulse test, primary side (PoE input) to GND | no breakdown |
| 2 | Then insulation resistance, primary side to GND, at 500 Vdc | at least 2 MΩ |

### B2 Bench supply on J6, no CM5

Preconditions: bench supply at 18.5 V with a 100 mA limit on J6 (pin 2 = BAT_IN). No
CM5, no PoE.

| # | Action | Pass |
|---|---|---|
| 1 | Supply on, magnet switch OFF: supply current | record (off-state drain) |
| 2 | +5V_AON | about 5.115 V |
| 3 | +5V at power-up | 0 V |
| 4 | Magnet swipe, scope +5V | +5V up in about 18 ms |
| 5 | Toggle with the magnet 10 times | +5V follows every time |
| 6 | J14 throughout | 0 V |

The drain here is without `force_telemetry_on`; B10 measures it with the bit set. The
floating CD4013 inputs on the magnet switch board may raise it
([11 Risks](docs/wiki/11-Risks-and-Technical-Debt.md), 11.1).

### B3 CM5 fitted: EEPROM, then eMMC

Preconditions: CM5 fitted; bench supply on J6 with a current limit raised for the CM5 (the
100 mA of B2 is too low; the plan gives no value); J8 cabled to the workstation; image
`manafish-cm5-mainboard-<rev>.img.zst` from `nix build` or the `software-image` workflow.
Details and the IO Board variant: [07 Deployment View](docs/wiki/07-Deployment-View.md), 7.3.

| # | Action | Pass |
|---|---|---|
| 1 | Once, with internet: `git -C ~/usbboot submodule update --init --depth 1` | submodule `rpi-eeprom` present |
| 2 | Edit `~/usbboot/recovery5/boot.conf`: `PSU_MAX_CURRENT=5000`, `POWER_OFF_ON_HALT=1`, `BOOT_UART=0`. Then `cd ~/usbboot/recovery5 && ./update-pieeprom.sh` | script completes |
| 3 | Jumpers on J7: 9-10 (`nRPIBOOT`) fitted, 11-12 (`EEPROM_nWP`) open, 1-2 (`USB_OTG_ID`) open. Power the board (supply on, magnet on); J8 has no VBUS | |
| 4 | EEPROM: `nix shell nixpkgs#rpiboot -c sh -c 'sudo "$(command -v rpiboot)" -d ~/usbboot/recovery5'` | rpiboot completes |
| 5 | Magnet off and on (jumper stays). eMMC: `nix shell nixpkgs#rpiboot -c sh -c 'r=$(command -v rpiboot); sudo "$r" -d "${r%/bin/rpiboot}/share/rpiboot/mass-storage-gadget64"'` | a new USB disk appears |
| 6 | `lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINTS`: use only the new USB disk. If the desktop mounted it: `sudo umount /dev/sdX1 /dev/sdX2`. Then `zstd -dc <image>.img.zst \| sudo dd of=/dev/sdX bs=4M conv=fsync` | dd completes |
| 7 | Magnet off, remove the J7 9-10 jumper, magnet on | boots (B4) |

If rpiboot over J8 fails, flash the EEPROM and the eMMC on the CM5 IO Board and move the
CM5 over. The EEPROM settings matter here: `POWER_OFF_ON_HALT=1` makes `poweroff` drop
CM5 3.3 V, the MCU and GP14 (QR-3, B15).

### B4 First boot

Preconditions: direct cable; bench supply on J6, no PoE; no cameras, no ESCs; the MS5837 on
J1 if it is available (B6 needs it).

The image flashes a blank MCU by itself: the service sees 2e8a:000f, pulses RUN once and,
after 5 failed port lookups, runs its first-boot flash of the bundled UF2
([06](docs/wiki/06-Runtime-View.md), 6.2). So the MCU usually runs firmware before B7.

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | `ssh pi@10.10.10.10` (password `manafish`) | login works | |
| 2 | `vcgencmd bootloader_version`, `vcgencmd bootloader_config` | the config shows the three B3 settings | |
| 3 | `journalctl -b -u manafish-firmware` | record whether the first-boot flash ran and succeeded | |
| 4 | `tools board-check --json` (3.1), save as `board-check-<board>-B4.json` | every check PASS except: `camera.count` FAIL (no cameras until B8), `i2c.ltc4162` WARN (cell_count 0 without PoE), `i2c.ms5837` FAIL if J1 is empty, `usb.mcu` FAIL if the first-boot flash failed (then do the B7 flash before B5). The PASSes include the failed units (`service.failed`), the link (`eth0.link`) and throttling (`soc.throttled`) | all |
| 5 | +5V cold, multimeter; compare with EXT5V | at most 5.25 V (R25 is 1 %); `soc.ext5v` PASS | `soc.ext5v` |
| 6 | `cat /proc/cmdline` | contains `console=ttyAMA10,115200n8` | |
| 7 | `charger.telemetry` and `charger.settings` details | vbat matches the bench supply; `charger.settings` shows the defaults (3.1) | `charger.*` |

### B5 GPIO24/25

Preconditions: the MCU runs the bundled UF2 (`usb.mcu` PASS). Then the service neither
resets nor flashes it at start, so any low on these lines is a fault. A low that matches a
journal line about an MCU reset or flash is that intended pulse
([06](docs/wiki/06-Runtime-View.md), 6.2): find why it ran and repeat.

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | Scope the MCU side of R9 (RUN) and R12 (QSPI_SS) over 10 cold boots | never low | |
| 2 | `pinctrl get 6,12,14,15,24,25` | 6 and 12 `ip pu`; 14 `op dh`; 24 and 25 `ip pn`; neither 14 nor 15 `TXD0`/`RXD0` | `poe.type`, `gpio.camera2_enable`, `gpio.mcu_lines` |

### B6 I2C1

Preconditions: MS5837 on J1. Background: [03 Context and Scope](docs/wiki/03-Context-and-Scope.md).

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | `tools board-check --json`, save as `board-check-<board>-B6.json` | all four addresses answer: `i2c.sht45`, `i2c.pads`, `i2c.ms5837` and `i2c.unexpected` PASS; `i2c.ltc4162` WARNs (LAD, cell_count 0 without input power; the 5-cell check moves to B9/B10). A FAIL's hint is in 3.1 | `i2c.*` |
| 2 | SDA and SCL idle with the module's 1.8 kΩ pull-ups | idle high | |
| 3 | Scope SCL rise time | under 300 ns | |

### B7 MCU

Preconditions: ESCs unplugged; service stopped for at least 35 s; scripts copied (3.2).

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | Read the RP2354A marking (possible from B0 on) | stepping A4 | |
| 2 | `tools board-check` | a running MCU: one 0009, product "Manafish Mainboard", `/dev/manafish-mcu`. A blank part shows 000f | `usb.mcu` |
| 3 | BOOTSEL by hand (3.3) | `lsusb -d 2e8a:000f` lists it | |
| 4 | `tools mcu-recover` (bundled UF2) | then 0009, iProduct "Manafish Mainboard", `/dev/manafish-mcu` | `usb.mcu` |
| 5 | GP14 (at R31) and J14 for 60 s after the MCU boots | 0 V | |
| 6 | `python3 control_smoke.py --port /dev/manafish-mcu --execute` | features 0x3f, IMU healthy, `ahrs_hz` and `pid_hz` about 500, `misses` 0, `queue_overflows` 0, ends with `RESULT` | |
| 7 | `python3 mainboard_bringup.py --port /dev/manafish-mcu --execute info` | `FEATURES` lists "ESC power"; record the identity | |
| 8 | `python3 mainboard_bringup.py --port /dev/manafish-mcu --execute imu --seconds 5` | `healthy True`, accel magnitude about 9.81 m/s² | |
| 9 | Y1: 20 reboots (`tools mcu-reset` or a magnet power cycle; record which) | `lsusb -d 2e8a:0009` after each | |
| 10 | Y1: cold power cycles at 0-5 °C | the MCU enumerates every time | |
| 11 | Y1: BOOTSEL when cold (`tools mcu-recover`) | succeeds | |

If cold BOOTSEL fails: SWD, a change to R10, or an OTP `BOOTSEL_XOSC_CFG` write
(irreversible; check the datasheet first). The owner decides; R10 is a BOM recommendation.

SWD, the last resort: a Debug Probe on J2 with raspberrypi/openocd (branch `sdk-2.x`, which
has `rp2350.cfg`), or that OpenOCD built with `--enable-linuxgpiod` on the CM5 (swclk 11,
swdio 8). Stock OpenOCD 0.12 has no RP2350 support. Record `pinctrl get 8,11` before and
restore both lines afterwards.

Dev builds: test them with the service stopped, or with
`Environment=MANAFISH_PICO_CONTROL_DEVELOPMENT=1`. Otherwise the service re-flashes the
bundled release within 60 s of its start. Load a dev build with `tools mcu-recover <UF2>`.

### B8 Cameras

Preconditions: board unpowered while plugging; camera 1 on J5, camera 2 on J11, each with
a 22-to-15 cable.

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | `tools board-check --json`, save as `board-check-<board>-B8.json` | 2 cameras; camera 1 (J5) is index 0, camera 2 (J11) index 1 | `camera.count` |
| 2 | Camera 2 missing | `pinctrl get 14` reads `op dh`, high | `gpio.camera2_enable` |
| 3 | Play `cam` and `cam2` at `http://10.10.10.10:1984` | both play | |
| 4 | CM5_3.3V current with both streaming | under 600 mA | |
| 5 | Scope CM5_3.3V with both streaming | record the droop | |

Camera order and encoder load: [08](docs/wiki/08-Crosscutting-Concepts.md), 8.11.

### B9 PoE, no battery

Preconditions: J6 empty (no bench supply, no pack). The PoE injector or switch connects
only the workstation and the board. Before enabling writes, re-check the LTC4162 facts
against the current ADI datasheet revision; the code follows Rev A
([11 Risks](docs/wiki/11-Risks-and-Technical-Debt.md), 11.2).

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | Baseline without PoE, still on the bench supply (end of B8): CM5 `iperf3 -s`; workstation `nix shell nixpkgs#iperf3 -c iperf3 -c 10.10.10.10 -t 600`, then the same with `-R`; `ethtool -S eth0` before and after | record throughput and counters | `eth0.errors` |
| 2 | Power off, remove the bench supply, connect the Type 4 PSE | board boots from PoE | |
| 3 | `tools board-check --json`, save as `board-check-<board>-B9-before.json` | type 4 (GPIO6 low, GPIO12 low) | `poe.type` |
| 4 | vin in the details | about 24 V (Ag59824: 23.25-24.8 V) | `charger.telemetry` |
| 5 | `chem_cells` | LAD and 5 cells (PASS) while the charger is enabled (`en_chg`, `system_status` bit 8). Without a pack it may stay at 0 cells (WARN); then check at B10 | `i2c.ltc4162` |
| 6 | Register check | the defaults (3.1) | `charger.settings` |
| 7 | Optional: VOL on GPIO6 and GPIO12 with the Type 4 PSE | record | |
| 8 | Owner: set `CHARGER_WRITES_ENABLED = True` in `software/src/rov_firmware/sensors/charger.py`, run the software gate, sync with the dev loop (CLAUDE.md), restart the service. Commit it (owner) so the next image keeps it | the journal logs the rewrites; the PSE does not trip | |
| 9 | Stop the service; `tools board-check --json`, save as `board-check-<board>-B9-after.json` | `matches` "service targets (type4, normal)": input limit 2.625 A (code 41) | `charger.settings` |
| 10 | PoE off and on (full power cycle), then the journal | the service rewrites all five settings (`en_jeita` reads 1 after the reset) | |
| 11 | Type 2 PSE, if available: repeat 3 and 9 | type 2 (GPIO6 low, GPIO12 high); `matches` "service targets (type2, normal)": input limit 0.9375 A (code 14) | `poe.type`, `charger.settings` |
| 12 | iperf3 10 min each way on PoE, `ethtool -S eth0` before and after | the PSE does not trip; compare with row 1 | `eth0.errors` |

The input limit halves while the hull reads above 45 °C or unknown
([08](docs/wiki/08-Crosscutting-Concepts.md), 8.2). PoE type table and presence rule: same
page, "PoE type limits".

### B10 Charger with a real 5S pack

Preconditions: writes enabled (B9); the pack's charge limits checked against 4.2 V/cell
and 2.0 A; J14 open.

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | Pack and PoE, magnet on: charge | CC then CV (`charge_status` constant_current, then constant_voltage); ibat at most 2.0 A; vbat towards 21.0 V | `charger.telemetry` |
| 2 | Watch the inductor, Q1/Q2 and the hull (SHT45) temperatures | record; README R1 note: "limit if hull gets too hot". The service halves the limit above 45 °C and suspends above 55 °C (die above 100 °C) | |
| 3 | `chem_cells` with pack and PoE | LAD and 5 cells | `i2c.ltc4162` |
| 4 | Pull and replug PoE, then the pack | charging resumes each time | |
| 5 | PoE on, pack out: `rov.escPower.on` in the app | refused: "No battery on the charger" | |
| 6 | Pack pulled on PoE: vbat, `charger_state`, time until the pack counts as missing; with ESC power on (J14 open) it must switch off with one warning toast | record | |
| 7 | Magnet OFF, no PoE, after the CM5 has set `force_telemetry_on`: pack drain | record; compare with B2 | |
| 8 | Defaults window: (1) unplug the pack with no PoE, replug it; (2) leave the magnet OFF, so the CM5 never configures the charger (the defaults (3.1)); (3) connect PoE to a part-discharged pack; (4) clamp-measure iin and ibat | a Type 4 PSE, and a Type 2/3 if available, does not trip | |

If a PSE trips in row 8, or 3.2 A exceeds the pack limit, recommend R1 16 mΩ (2.0 A input
default) and/or R20 16 mΩ (2.0 A charge default). The owner decides. Background:
[06](docs/wiki/06-Runtime-View.md) 6.6, [11](docs/wiki/11-Risks-and-Technical-Debt.md) 11.1.

### B11 ESC switch with a dummy load on J14

Preconditions: dummy load on J14, no ESCs; pack or bench supply on J6 (never PoE with a
bench supply); service stopped; scripts copied. Timing:
[06 Runtime View](docs/wiki/06-Runtime-View.md), 6.4.

| # | Action | Pass |
|---|---|---|
| 1 | `esc on` within 5 s of an MCU boot, or within 7 s of an off (2 s stop plus 5 s hold) | BUSY |
| 2 | After the hold: `esc on`, then at once `esc watch --seconds 60` | APPLIED; state 3, then 4 about 15 s after the on; then 5 and 1 about 30 s after the last host packet (host-loss auto-off) |
| 3 | I(R31) while on | about 9-10 mA (1.6-1.8 V across R31, 180 Ω) |
| 4 | Scope ESC_PWR at switch-on, warm and cold | record the ramp (1.5 s typical, 12 s worst case) |
| 5 | Scope a motor line on J3 or J4 | 0 V until 5 s after the on |
| 6 | `esc on`, wait for state 4, `esc off`, `esc watch` | state 5 for 2 s (neutral), then 1 for 5 s (hold), then 2 |
| 7 | Reset with ESC power on: scope GP14 through a RUN pulse (`pinctrl set 24 op dl`, `sleep 0.01`, `pinctrl set 24 ip pn`), a BOOTSEL entry (3.3) and a watchdog reboot | record whether GP14 falls and stays low |

Row 7: run each within 30 s of `esc on`, before the host-loss auto-off. `tools mcu-reset`
cannot be used here: it waits 35 s, and by then ESC power is off. After the BOOTSEL entry,
`tools mcu-reset` restarts the firmware. No tool in this repo triggers a watchdog reboot;
agree a method with the owner before B11. If GP14 does not fall, the "off before reset"
host rule becomes mandatory and goes into `firmware/docs/PICO_CONTROL_PROTOCOL.md`.

### B12 ESCs connected, no props

Preconditions: both ESCs on J3/J4 and J14, eight thrusters without props, pack on J6.

| # | Action | Pass |
|---|---|---|
| 1 | ESC power off: ESC 3.3 V rail | dead (no back-powering through the parked lines) |
| 2 | ESC power on: ESC rail-up time | record; set `SIGNAL_DELAY_MS` to at least that plus 1 s (firmware change, next `-mb.N`) |
| 3 | ESC power on with the app (`rov.escPower.on`): ESC telemetry | all 8 report, quality above 95 % |
| 4 | The slow ramp | AM32 cell detection and low-voltage cutoff behave normally |
| 5 | AM32 passthrough with ESC power off | refused with error 11 (plain message) |
| 6 | AM32 passthrough with ESC power on | works |

### B13 DShot and failsafe

Preconditions: thrusters in a safe test setup; pack on J6; the app.

| # | Action | Pass |
|---|---|---|
| 1 | 10 % on each motor in turn | J3 drives M1-M4, J4 drives M5-M8 as mapped; spin direction right |
| 2 | Kill the host while spinning: `sudo systemctl kill -s KILL manafish-firmware` | neutral within 200 ms |
| 3 | Magnet off while spinning | AM32 stops within 0.5 s |
| 4 | IMU, service stopped: `mainboard_bringup.py ... imu --seconds 3` level, nose up, starboard down | record the three means |

From row 4 set `MANAFISH_IMU_AXIS_*` and `MANAFISH_IMU_SIGN_*` in
`firmware/boards/manafish_mainboard.h` so that the frame is FRD: level reads about
(0, 0, -9.81); by the same convention nose up reads about (+9.81, 0, 0) and starboard down
about (0, -9.81, 0). Ship it as the next `-mb.N` (ask the owner; tag rules in CLAUDE.md).
The board's orientation in the hull is plan open question 7.

### B14 Thermal soak

Preconditions: closed hull; both streams watched; charging on PoE. 60 minutes in air, then
60 minutes in water.

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | Log SoC temperature, throttling, CPU, SHT45, LTC4162 die/iin/ibat and `EXT5V_V` | +5V warm at most 5.25 V (`EXT5V_V`); record the maxima and any throttling | |
| 2 | Camera defaults | set from this run | |
| 3 | Video latency | measure and record | |
| 4 | After the soak, service stopped: `tools board-check --json`, save as `board-check-<board>-B14.json` | `soc.*` PASS; a WARN in `soc.throttled` or `soc.rpi_volt` means throttling or undervoltage during the soak: record it | `soc.*` |

A log loop from the workstation (works in fish and zsh):

```sh
ssh pi@10.10.10.10 'while true; do date -Is; vcgencmd measure_temp; vcgencmd get_throttled; vcgencmd pmic_read_adc EXT5V_V; cat /proc/loadavg; sleep 10; done' | tee soak-<board>-air.log
```

SHT45 and LTC4162 values come from the app readings or the blackbox custom action
([08](docs/wiki/08-Crosscutting-Concepts.md), 8.10). Plan the cooling first: on the IO Board
without a heatsink the SoC went from 50 to 74 °C in 26 s of a 4-core load.

### B15 Power loss

| # | Action | Pass |
|---|---|---|
| 1 | Cut +5V under load (magnet off), scope CM5_3.3V | stays above 1.8 V |
| 2 | 50 abrupt cuts, some during a config save from the app | boots every time; `config.json` intact (`python3 -m json.tool ~/firmware/src/rov_firmware/config.json` parses; old or new values); `service.failed` PASS |
| 3 | `systemctl poweroff` | CM5 3.3 V, the MCU and ESC_PWR drop |
| 4 | Shutdown chain: bench supply on J6, no PoE, motors neutral, service running; lower the supply below 17.0 V, 16.0 V, then 15.0 V | "Battery low" after 10 s below 17.0 V; "Battery critical" after 10 s below 16.0 V, ESC power stays on; power-off after 30 s below 15.0 V |

Row 4 checks the 3.0 V/cell chain of [06 Runtime View](docs/wiki/06-Runtime-View.md), 6.7.

### B16 EMI

Preconditions: board in the tank, thrusters running, PoE.

| # | Action | Pass | board-check |
|---|---|---|---|
| 1 | Counters before: `ethtool -S eth0` | record | `eth0.errors` |
| 2 | `iperf3` for 30 minutes (`-t 1800`) with the thrusters running | Ethernet error counters stay 0 | `eth0.errors` |

### Final acceptance

With B0-B16 recorded, the app at 10.10.10.10:9000 shows camera 1, the new battery, PoE and
hull readings, `rov.escPower.on` turning on with the 15 s arming, all eight thrusters
responding with telemetry, and PoE charging limited by the PSE type
([10 Quality Requirements](docs/wiki/10-Quality-Requirements.md), 10.7).

## 5. Before the boards arrive (CM5 IO Board)

Still open on the CM5 IO Board. Flashing it: [07](docs/wiki/07-Deployment-View.md), 7.3.

| Item | Steps | Pass |
|---|---|---|
| EEPROM update rehearsal | The EEPROM steps of [07](docs/wiki/07-Deployment-View.md) 7.3, with the IO Board's jumpers: J2 pins 1-2 fitted (`nRPIBOOT`), J2 pins 3-4 (`EEPROM_nWP`) open, USB-C J11 to the workstation. Then remove the jumper and boot the image | `vcgencmd bootloader_config` shows the three settings. Record `vcgencmd bootloader_version` before (2026/05/11 on the owner's unit) and after. Optional: `systemctl poweroff`, then the header's 3.3 V pin reads 0 V |
| GPIO24/25 scope (QR-5) | Wire 4.7 kΩ from GPIO24 (header pin 18) to 3V3 and 10 kΩ from GPIO25 (pin 22) to 3V3. Scope both through power-up and boot, 10 cold boots | Never low before the service starts. The service's first-boot flash drives both lines low once per run a few seconds after it starts, because no MCU answers on the IO Board (6.2); the journal line "MCU not found after 5 attempts" marks that intended pulse. With the pull-ups wired, `gpio.mcu_lines` PASSes |
| Pico 2 rehearsal (optional, plan open question 6) | Buy a Pico 2 (about €5). Wire GPIO24 → 330 Ω → RUN and GPIO25 → 1 kΩ → QSPI_SS (not a header pin; find its pad in the Pico 2 datasheet), GND to GND, its USB to the IO Board. Keep the QR-5 pull-ups | The service flashes the bundled UF2 through hardware BOOTSEL; then `usb.mcu` PASS ("Manafish Mainboard", `/dev/manafish-mcu`); `tools mcu-reset` and `tools mcu-recover` work; `mainboard_bringup.py info` shows "ESC power"; `esc on`/`off`/`watch` show the B11 timing on Pico 2 GP14. `control_smoke.py` stops with "IMU is not healthy" (no BMI270) |

## 6. Record sheet

Copy this section once per board.

| Field | Value |
|---|---|
| Board id / serial | |
| CM5 (RAM, eMMC, wireless) | |
| Date(s) | |
| Operator | |
| Image file (`manafish-cm5-mainboard-<rev>.img.zst`) | |
| NixOS generation (`readlink /nix/var/nix/profiles/system`) | |
| MCU firmware (bundled UF2 in `~/mcu-firmware`, identity from `mainboard_bringup.py info`) | |
| Bootloader version (`vcgencmd bootloader_version`) | |

| Step | Measurement | Expected | Value | Unit | Pass/Fail | Note |
|---|---|---|---|---|---|---|
| B0 | IC5, IC3 joints | no bridges | | | | |
| B0 | IC10, D2-D5 pin 1 | matches silkscreen | | | | |
| B0 | D1, D6, D7, C20, C39 polarity | correct | | | | |
| B0 | CM5 connectors | no bridges | | | | |
| B0 | BAT_IN to GND | no short | | Ω | | |
| B0 | VSYS to GND | no short | | Ω | | |
| B0 | 24V to GND | no short | | Ω | | |
| B0 | +5V_AON to GND | no short | | Ω | | |
| B0 | +5V to GND | no short | | Ω | | |
| B0 | ESC_PWR to GND | no short | | Ω | | |
| B1 | Impulse 1500 V 10/700 µs | no breakdown | | | | |
| B1 | Primary to GND at 500 Vdc | ≥ 2 | | MΩ | | |
| B2 | Off-state drain (magnet OFF) | record | | mA | | |
| B2 | +5V_AON | ≈ 5.115 | | V | | |
| B2 | +5V at power-up | 0 | | V | | |
| B2 | +5V rise after magnet swipe | ≈ 18 | | ms | | |
| B2 | Magnet toggles | 10/10 | | | | |
| B2 | J14 | 0 | | V | | |
| B3 | recovery5 EEPROM update | completes | | | | |
| B3 | eMMC write (rpiboot over J8 or IO Board) | completes | | | | |
| B4 | SSH login | works | | | | |
| B4 | Bootloader config | 3 settings | | | | |
| B4 | First-boot MCU flash (journal) | record | | | | |
| B4 | board-check (also `service.failed`, `eth0.link`, `soc.throttled`) | expected per B4 row 4 | `board-check-<board>-B4.json` | | | |
| B4 | +5V cold (meter) | ≤ 5.25 | | V | | |
| B4 | EXT5V_V (`soc.ext5v`) | PASS | | V | | |
| B4 | `/proc/cmdline` console | ttyAMA10 | | | | |
| B4 | Charger settings | the defaults (3.1) | | | | |
| B5 | GPIO24 low events, 10 cold boots | 0 | | | | |
| B5 | GPIO25 low events, 10 cold boots | 0 | | | | |
| B5 | `pinctrl get 6,12,14,15,24,25` | as B5 row 2 | | | | |
| B6 | board-check | i2c.* as B6 | `board-check-<board>-B6.json` | | | |
| B6 | SHT45 serial | read | | | | |
| B6 | chem / cell_count | LAD / 0 | | | | |
| B6 | SDA / SCL idle (pull-ups) | high / high | | | | |
| B6 | SCL rise time | < 300 | | ns | | |
| B7 | RP2354A marking | A4 | | | | |
| B7 | USB before flash | record (0009 or 000f) | | | | |
| B7 | BOOTSEL by hand | 000f | | | | |
| B7 | After `tools mcu-recover` | 0009, "Manafish Mainboard", symlink | | | | |
| B7 | GP14 / J14 for 60 s | 0 | | V | | |
| B7 | control_smoke features | 0x3f | | | | |
| B7 | control_smoke ahrs_hz / pid_hz | ≈ 500 | | Hz | | |
| B7 | control_smoke misses / queue_overflows | 0 / 0 | | | | |
| B7 | `info` FEATURES | ESC power listed | | | | |
| B7 | IMU healthy, accel magnitude | True, ≈ 9.81 | | m/s² | | |
| B7 | 20 reboots (method) | 20/20 | | | | |
| B7 | Cold power cycles, temperature | all OK, 0-5 | | °C | | |
| B7 | Cold BOOTSEL | succeeds | | | | |
| B8 | board-check | 2 cameras | `board-check-<board>-B8.json` | | | |
| B8 | Camera order | J5 = 0, J11 = 1 | | | | |
| B8 | `cam` and `cam2` play | both | | | | |
| B8 | CM5_3.3V current, 2 streams | < 600 | | mA | | |
| B8 | CM5_3.3V droop | record | | mV | | |
| B9 | iperf3 without PoE, each way | record | | Mbit/s | | |
| B9 | Boots from PoE (Type 4) | yes | | | | |
| B9 | Type 4: GPIO6 / GPIO12 | low / low | | | | |
| B9 | vin | ≈ 24 | | V | | |
| B9 | chem / cell_count | LAD / 5 (or 0 without pack) | | | | |
| B9 | Settings before writes | the defaults (3.1) | `board-check-<board>-B9-before.json` | | | |
| B9 | GPIO6 / GPIO12 VOL (optional) | record | | V | | |
| B9 | Settings after writes, Type 4 | service targets (type4, normal) | `board-check-<board>-B9-after.json` | | | |
| B9 | Rewrite after power cycle | all five | | | | |
| B9 | Type 2: GPIO6 / GPIO12, limit | low / high, 0.9375 A | | | | |
| B9 | PSE trips | none | | | | |
| B9 | iperf3 on PoE, each way | record | | Mbit/s | | |
| B9 | `ethtool -S` with vs without PoE | compare | | | | |
| B10 | ibat max during CC | ≤ 2.0 | | A | | |
| B10 | CC → CV, final vbat | ≈ 21.0 | | V | | |
| B10 | Inductor / Q1 / Q2 / hull max | record | | °C | | |
| B10 | chem / cell_count | LAD / 5 | | | | |
| B10 | PoE replug / pack replug: charging resumes | yes / yes | | | | |
| B10 | No pack on PoE: ESC on refused | refused | | | | |
| B10 | Pack pulled: time to detection | record | | s | | |
| B10 | Pack pulled with ESC on: auto-off, one toast | yes | | | | |
| B10 | Drain with force_telemetry_on, magnet OFF | record | | mA | | |
| B10 | Defaults window iin / ibat | record | | A | | |
| B10 | Defaults window PSE trips | none | | | | |
| B11 | `esc on` in the hold | BUSY | | | | |
| B11 | State 3 → 4 | ≈ 15 | | s | | |
| B11 | I(R31) | ≈ 9-10 | | mA | | |
| B11 | ESC_PWR ramp warm / cold | record | | s | | |
| B11 | Motor line before 5 s | 0 | | V | | |
| B11 | Off: stop / hold | 2 / 5 | | s | | |
| B11 | Host-loss auto-off | ≈ 30 | | s | | |
| B11 | GP14 through RUN pulse | record | | | | |
| B11 | GP14 through BOOTSEL entry | record | | | | |
| B11 | GP14 through watchdog reboot | record | | | | |
| B12 | ESC 3.3 V rail, power off | 0 | | V | | |
| B12 | ESC rail-up time | record | | s | | |
| B12 | SIGNAL_DELAY_MS set to | ≥ rail-up + 1 s | | ms | | |
| B12 | Telemetry quality, 8 ESCs | > 95 | | % | | |
| B12 | AM32 cell detection / LVC | normal | | | | |
| B12 | Passthrough off / on | error 11 / works | | | | |
| B13 | Motor map and direction M1-M8 | as mapped | | | | |
| B13 | Host killed → neutral | ≤ 200 | | ms | | |
| B13 | Magnet off → AM32 stop | ≤ 0.5 | | s | | |
| B13 | IMU level / nose up / starboard down | record | | m/s² | | |
| B14 | SoC max, air / water | record | | °C | | |
| B14 | Throttling | record | | | | |
| B14 | EXT5V_V warm | ≤ 5.25 | | V | | |
| B14 | SHT45 / die max | record | | °C | | |
| B14 | Latency | record | | ms | | |
| B14 | Camera defaults chosen | record | | | | |
| B14 | board-check after the soak | `soc.*` as B14 row 4 | `board-check-<board>-B14.json` | | | |
| B15 | CM5_3.3V min during +5V cut | > 1.8 | | V | | |
| B15 | 50 cuts: boots / config.json intact | 50 / yes | | | | |
| B15 | `poweroff`: 3.3 V, MCU, ESC_PWR | all drop | | | | |
| B15 | Warn / critical / power-off | 10 s / 10 s / 30 s | | s | | |
| B16 | Ethernet errors after 30 min iperf3 | 0 | | | | |
| — | Final acceptance (app) | all five items | | | | |
