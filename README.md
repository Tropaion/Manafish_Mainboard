# Manafish Mainboard

The Manafish Mainboard is the main electronics board of the
[Manafish ROV](https://github.com/manafishrov). It replaces the Raspberry Pi 3B,
the USB-connected Raspberry Pi Pico and the wiring between them with one board:

- a **Raspberry Pi Compute Module 5** for the tether network, the camera and
  the vehicle firmware ([manafishrov/firmware](https://github.com/manafishrov/firmware)),
- an **RP2354A** microcontroller that drives the eight thrusters over
  bidirectional DShot ([manafishrov/mcu-firmware](https://github.com/manafishrov/mcu-firmware)),
- two camera connectors, Ethernet with PoE-capable magnetics, a magnet power
  switch input, and the on-board sensors.

> [!WARNING]
> Work in progress. The schematic and the 4-layer PCB layout are drawn, but no
> board has been built or tested. Don't manufacture from this repository yet.

## Overview
View the latest [KiCad Design](https://kicanvas.org/?repo=https%3A%2F%2Fgithub.com%2FTropaion%2FManafish_Mainboard%2Fblob%2Fmain%2Fhardware%2Fmanafish.kicad_pro).

```mermaid
flowchart LR
  tether["Tether (Ethernet)"] --> magjack["MagJack + ESD"]
  magjack -->|4 pairs| cm5["Compute Module 5"]
  magjack -.->|center taps| poe["PoE module IC5 (Ag59824, 24 V)"]
  cm5 -->|MIPI0| cam["Camera connector J5"]
  cm5 -->|MIPI1| cam2["Camera connector J11"]
  cm5 -->|"USB 2.0 (data only)"| service["USB-C service port J8"]
  cm5 -->|I2C1| sensors["BMI270, SHT45, MS5837 connector"]
  cm5 -->|"USB 2.0 (USB3-1 port)"| mcu["RP2354A"]
  cm5 -->|"GPIO24 RUN, GPIO25 BOOTSEL"| mcu
  mcu -->|GP6-GP9| escA["ESC A, 4-in-1 (AM32)"]
  mcu -->|GP18-GP21| escB["ESC B, 4-in-1 (AM32)"]
  poe --> chg["Charger IC8 (LTC4162-L)"]
  bat["Battery J6 (5S)"] <--> chg
  chg -->|VSYS| buck["5 V buck IC9 (TPSM53604)"]
  mag["Magnet switch board J12"] -->|MAG_ON| lsw
  buck -->|5 V always on| lsw["Load switch IC15 (TPS22965)"]
  lsw -->|5 V| cm5
```

## Schematic sheets

| Sheet | File | Contents | Origin |
| --- | --- | --- | --- |
| Manafish (top level) | `manafish.kicad_sch` | Sheet hierarchy, battery input J6, USB-C service port J8, magnet switch connector J12 | Original; USB-C port adapted from the CM5 IO Board |
| Raspberry Pi Compute Module 5 | `cm5.kicad_sch` | CM5 module connectors, boot and EEPROM jumper header J7 | Adapted from the CM5 IO Board |
| Camera Serial Interface | `csi.kicad_sch` | 22-pin MIPI camera connectors J5 (MIPI0) and J11 (MIPI1), 3.3 V from CM5_3.3V | Adapted from the CM5 IO Board |
| Ethernet | `ethernet.kicad_sch` | MagJack IC3 (YAGEO JXF0-1012NL, 4-pair PoE center taps), ESD protection IC1/IC2, link LEDs | Adapted from the CM5 IO Board |
| Serial Peripherals | `serial_peripherals.kicad_sch` | BMI270 IMU (SPI to the RP2354A), SHT45 humidity and temperature sensor, WSEN-PADS pressure sensor IC10, MS5837 connector | Original |
| RP2354A | `rp2354a.kicad_sch` | RP2354A with core regulator, USB, SWD connector J2, ESC connectors J3/J4 | Original, following *Hardware design with RP2350* |
| Power Management | `power_management.kicad_sch` | PoE bridges D2–D5, PoE module IC5, LTC4162-L charger IC8, 5 V buck IC9, load switch IC15 | Original |

"Adapted from the CM5 IO Board" means the sheet started from Raspberry Pi's
reference design; see [Origin and attribution](#origin-and-attribution).

## Interfaces

Compute Module 5:

| Signal | CM5 pin | Connects to |
| --- | --- | --- |
| Ethernet pairs 0–3 | 3–12 | MagJack IC3 |
| GPIO2 / GPIO3 (I2C1) | 58 / 56 | BMI270, SHT45, MS5837 connector |
| GPIO24 | 45 | RP2354A `RUN` |
| GPIO25 | 41 | RP2354A `QSPI_SS` (BOOTSEL) |
| USB3-1 D+ / D− (USB 2.0 pair) | 163 / 165 | RP2354A USB |
| MIPI0 data and clock lanes | 115–141 | Camera connector J5 |
| SDA0 / SCL0 | 82 / 80 | Camera connector J5 |
| CAM_GPIO0 / CAM_GPIO1 | 97 / 100 | Camera connector J5 |
| MIPI1 data and clock lanes | 175–196 | Camera connector J11 |
| ID_SD / ID_SC (I2C, 2.2 kΩ pull-ups R4/R5) | 36 / 35 | Camera connector J11 |
| GPIO14 / GPIO15 | 55 / 51 | Camera connector J11 pins 17 / 18 (CAM1_GPIO0 / CAM1_GPIO1) |
| USB_P / USB_N (USB 2.0) | 105 / 103 | USB-C service port J8, data only |
| nRPIBOOT, EEPROM_nWP, SYNC_OUT, USB_OTG_ID, PMIC_ENABLE, PWR_BUT | 93, 20, 18, 101, 99, 92 | Jumper header J7 |
| GPIO_VREF | 78 | CM5_3.3V (3.3 V GPIO levels) |
| +5 V input | 77–87 | Power management, through load switch IC15 |
| CM5_3.3V output | 84 / 86 | RP2354A and sensors |

RP2354A (QFN-60):

| Signal | RP2354A pin | Connects to |
| --- | --- | --- |
| GP6–GP9 | 9, 10, 12, 13 | ESC A connector J3 |
| GP18–GP21 | 29, 31, 32, 33 | ESC B connector J4 |
| USB_DP / USB_DM | 52 / 51 | CM5 USB3-1, 27 Ω in series |
| RUN | 26 | CM5 GPIO24 |
| QSPI_SS | 60 | CM5 GPIO25 |
| SWCLK / SWDIO | 24 / 25 | Debug connector J2 |
| XIN / XOUT | 21 / 22 | 12 MHz crystal Y1 (ABM8-272-T3) |

I2C devices on the CM5's I2C1 bus:

| Device | Address | Notes |
| --- | --- | --- |
| BMI270 IMU | `0x68` | `0x69` if SDO is pulled to VDDIO |
| SHT45-AD1F (with PTFE membrane) | `0x44` | |
| MS5837-30BA pressure sensor (external module) | `0x76` | Fixed address |

## Repository layout

```text
hardware/
├── manafish.kicad_pro      KiCad project
├── *.kicad_sch             schematic sheets
├── manafish.kicad_pcb      PCB, 4 layers
├── manafish.kicad_dru      custom DRC rules for the PoE isolation barriers
├── sym-lib-table           project symbol libraries
├── fp-lib-table            project footprint libraries
└── custom_components/
    ├── symbols/            CM5IO, BMI270, SHT45, ABM8-272-T3, Silvertel, V6K100DUHM3, FDMC8327L, LTC4162, YAGEO symbols
    ├── footprints/         CM5IO, BMI270, SHT45, ABM8-272-T3, RP2350_Minimal, Silvertel, V6K100DUHM3, FDMC86139P, LTC4162, YAGEO footprints
    └── 3d/                 STEP models
```

## PCB

`manafish.kicad_pcb` is a 70 × 100 mm, 4-layer board. It sits centred on the
axis of a tube with a 74 mm inner diameter, so a part at lateral distance *d*
from the axis may stand at most √(37² − *d*²) − 0.8 mm above the board surface.
Every part fits, checked against the 3D models. The tightest are the USB-C port
J8 (1.8 mm to the tube wall) and the SWD connector J2 (2.3 mm).

### Stackup

JLCPCB JLC04161H-7628, 1.6 mm, ENIG finish:

| Layer | Thickness | Use |
| --- | --- | --- |
| F.Cu | 35 µm | Parts, signals, GND and power pours |
| Prepreg 7628 | 0.21 mm, εr 4.4 | |
| In1.Cu | 15 µm | Solid GND reference, no tracks |
| Core | 1.065 mm, εr 4.6 | |
| In2.Cu | 15 µm | GND, power pours (VSYS, +5 V, 3V3), signals |
| Prepreg 7628 | 0.21 mm, εr 4.4 | |
| B.Cu | 35 µm | PoE module IC5, PoE input, signals, GND and 24 V pours |

Order the board with impedance control:

| Net class | Nets | Width / gap | Target |
| --- | --- | --- | --- |
| `DP100` | Ethernet `TRD*`, camera `DPHY0_*` and `DPHY1_*` | 0.21 / 0.19 mm on F.Cu and B.Cu | 100 Ω differential |
| `USB90` | `USB2_*`, RP2354A `USB_D*` | 0.24 / 0.15 mm on F.Cu | 90 Ω differential |

Inside the CM5 connector pin fields the pairs neck down to 0.127 mm for at most
1.6 mm. Pours keep 0.3 mm from the pairs everywhere.

### PoE isolation

The PoE input side (the `Primary` net class: center taps, bridges, IC5 input,
TYP outputs) is isolated from the rest of the board. `manafish.kicad_dru`
enforces:

- 2.0 mm from primary copper to any other net, on every layer and from every
  hole. The only exception is between the pads of each Y-capacitor
  (C21, C24).
- A 3.0 mm warning tier, the Silvertel guideline. 21 warnings remain, all
  between 2.0 and 3.0 mm:
  - the MagJack's own center-tap pads to its shield pins (2.14 mm) and the VC3
    track next to the shield (2.02 mm);
  - the Y-capacitors' own pads (2.7 mm) and C21's GND pad to the ferrite beads
    (2.26 mm);
  - the RP2354A decoupling GND vias next to the ferrite-bead column (2.38 mm);
  - R23 next to IC5's VIN+ pad (2.8 mm).
- Under IC5's primary half (`PRI_ISLAND_U5`), no vias of other nets and no
  other copper on B.Cu or In2. The module's isolation barrier strip stays
  empty on B.Cu and In2.

The inner layers are voided 2 mm around primary copper on the next outer layer
(In2 under primary B.Cu, In1 under primary F.Cu), and In1 stays clear of the
front edge where the VC1 track runs on In2. Only IC5, the optocouplers IC6/IC7 and
the Y-capacitors C21/C24 cross the barrier.

Connector A of the CM5 lies inside the island, so 16 of its GND pins have no
via to the GND planes. They connect through the CM5's own ground: the footprint
`Module1` declares all its GND pads as one jumper pad group.

### Assembly

- IC5 (Ag59824-LPB) is on the bottom and must never pass through reflow hanging
  upside down. Reflow the top side first, then the bottom side with IC5 upright,
  or fit IC5 afterwards with hot air. Glue L2 (1.0–1.6 g) and IC9 before the
  second pass.
- IC5 reflow profile: peak 230–245 °C, 30–90 s above 217 °C, soak 150–180 °C for
  30–90 s, ramp up at most 3 °C/s and down at most 6 °C/s. No wave solder on the
  IC5 side.
- Solder the through-hole parts IC3, J6, J7 and C20 by hand after both reflow
  passes.
- Fit the CM5 with M2.5 screws from below (head at most 4.5 mm across) and the
  nuts on the CM5 side. A nut under the board would hit C20.

The same notes are on the board's User.Comments layer, which isn't part of the
Gerbers: copy them into the order remarks for the assembler.

### Testing the isolation

Use the 1500 V 10/700 µs impulse test only, at normal air pressure. Don't run
a 1500 Vrms or 2250 Vdc hipot: the Ag59824 is rated only for the impulse
test, and 2250 V exceeds the Y-capacitors' 2 kV rating. Afterwards the primary
side must measure at least 2 MΩ to GND at 500 Vdc.

## Flashing the Compute Module

The USB-C port J8 carries data only. Its VBUS isn't connected, so the board
needs its normal supply while you flash it.

1. Power the board.
2. Fit a jumper on J7 pins 1–2 (`nRPIBOOT`).
3. Connect J8 to a computer and run
   [`rpiboot`](https://github.com/raspberrypi/usbboot)
   `-d mass-storage-gadget64`. The eMMC appears as a USB drive.
4. Write the image, then remove the jumper.

Set `PSU_MAX_CURRENT=5000` in the bootloader EEPROM configuration. The CM5
isn't powered over USB-C, so it can't detect the supply's capability itself.

Jumper header J7:

| Pins | Signal | Fitted means |
| --- | --- | --- |
| 1–2 | `nRPIBOOT` | Boot from USB, for flashing |
| 3–4 | `EEPROM_nWP` | Bootloader EEPROM write-protected; blocks EEPROM updates |
| 5–6 | `SYNC_OUT` | Ethernet timing output; never fit a jumper |
| 9–10 | `USB_OTG_ID` | USB 2.0 port in host mode; leave open for flashing |
| 11–12 | `PMIC_ENABLE` | CM5 switched off |
| 13–14 | `PWR_BUT` | For a push button: short press shuts down or wakes, holding over 5 s forces power-off |

## Opening the project

1. Install [KiCad](https://www.kicad.org/) 10.0 or newer. The files are saved
   in the KiCad 10 format and won't open in older versions.
2. Open `hardware/manafish.kicad_pro`. The project libraries in
   `custom_components/` are registered in `sym-lib-table` and `fp-lib-table`;
   everything else comes from KiCad's standard libraries.

Datasheets aren't stored in the repository. Use the links below.

## Datasheets

- [Raspberry Pi Compute Module 5 datasheet](https://datasheets.raspberrypi.com/cm5/cm5-datasheet.pdf)
- [Compute Module 5 IO Board datasheet](https://datasheets.raspberrypi.com/cm5/cm5io-datasheet.pdf)
- [RP2350 datasheet](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf)
- [Hardware design with RP2350](https://datasheets.raspberrypi.com/rp2350/hardware-design-with-rp2350.pdf)
- [RP2350 product brief](https://datasheets.raspberrypi.com/rp2350/rp2350-product-brief.pdf)
- [Bosch BMI270 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmi270-ds000.pdf)
- [Sensirion SHT45 product page and datasheet](https://sensirion.com/products/catalog/SHT45)
- [Silvertel Ag5700-LPB (Ag5724-LPB) PoE module datasheet](https://silvertel.com/images/datasheets/Ag5700LPB-datasheet-minature%20IEEE802_3At%2024W%20power-over-ethernet%20module.pdf)
- [Silvertel Ag59800 (Ag59824-LPB) PoE++ module datasheet](https://silvertel.com/images/datasheets/Ag59800-LPB%20-datasheet-high%20efficiency%20SMT%20IEEE802_3BT%20100W%20power-over-ethernet%20module.pdf)
- [Vishay V6K100DU dual Schottky diode datasheet](https://www.vishay.com/docs/87418/v6k100du.pdf)
- [Analog Devices LTC4162-L battery charger datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/LTC4162-L.pdf)
- [YAGEO (Pulse) JXF0-xx12NL MagJack datasheet J520](https://www.yageogroup.com/content/datasheet/asset/file/DATASHEET_J520)

## Related repositories

- [manafishrov/firmware](https://github.com/manafishrov/firmware): vehicle
  firmware running on the Compute Module
- [manafishrov/mcu-firmware](https://github.com/manafishrov/mcu-firmware):
  thruster firmware running on the RP2354A (Pico 2 build)
- [manafishrov/esc-firmware](https://github.com/manafishrov/esc-firmware):
  AM32 firmware for the ESCs

## Origin and attribution

This board builds on the **Raspberry Pi Compute Module 5 IO Board (CM5IO)**
reference design by Raspberry Pi Ltd.

- **Source:** *CM5 IO Board, revision 2, KiCad files* (Raspberry Pi document
  RP-008099-DD-1), downloaded from the
  [Raspberry Pi Product Information Portal](https://pip.raspberrypi.com/categories/1098-design-files).
  The original files carry the notice "Copyright © 2024 Raspberry Pi Ltd."
- **Copied unchanged:** the symbol library
  `custom_components/symbols/CM5IO.kicad_sym`, all footprints in
  `custom_components/footprints/CM5IO.pretty/`, and eight 3D models in
  `custom_components/3d/`.
- **Adapted:** the circuits in `cm5.kicad_sch`, `csi.kicad_sch` and
  `ethernet.kicad_sch`, including the boot and EEPROM jumper header J7, and
  the USB-C service port J8 in `manafish.kicad_sch`.
- **Terms:** the CM5 IO Board datasheet says the design files "can be used in
  your own reference designs", and Raspberry Pi grants permission to use its
  resources "solely in conjunction with the Raspberry Pi products". These files
  and the parts derived from them remain © Raspberry Pi Ltd under those terms.
  They are **not** covered by this repository's license.

The RP2354A circuit follows Raspberry Pi's
[*Hardware design with RP2350*](https://datasheets.raspberrypi.com/rp2350/hardware-design-with-rp2350.pdf)
(RP-008280-DS). The footprints for the core regulator inductor L1 and the SWD
connector J2 come from Raspberry Pi's RP2350A minimal design, published under
the MIT license.

Raspberry Pi is a trademark of Raspberry Pi Ltd. This project is not affiliated
with or endorsed by Raspberry Pi Ltd.

[NOTICE.md](NOTICE.md) lists every third-party file and its terms.

## License

The original work in this repository is licensed under the GNU Affero General
Public License v3.0 or later; see [LICENSE](LICENSE). Third-party files keep
their own terms, listed in [NOTICE.md](NOTICE.md).
