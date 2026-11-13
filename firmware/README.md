# Manafish MCU Firmware

> **Mainboard fork.** An adapted copy of
> [manafishrov/mcu-firmware](https://github.com/manafishrov/mcu-firmware) at
> `88bd6a0` in the [Manafish Mainboard](../README.md) repository. Added here: a
> board target for the mainboard's RP2354A ([boards/](boards/),
> `make build-mainboard`), the ESC power switch on GP14 (`src/esc_power.*`,
> request 0x26 in [docs/PICO_CONTROL_PROTOCOL.md](docs/PICO_CONTROL_PROTOCOL.md))
> and the bench helper `scripts/mainboard_bringup.py`. The CM5 enters BOOTSEL in
> hardware (GPIO24/GPIO25), so `make flash-mainboard` expects BOOTSEL already
> and has no `-f`. The root [CLAUDE.md](../CLAUDE.md) governs commits, tags
> (`firmware-vX.Y.Z-mb.N`) and releases; upstream sync uses `git subtree`. Run
> the commands below from `firmware/`.

Thruster firmware for the Raspberry Pi Pico and Pico 2 in the Manafish ROV. It
drives the ESCs with DShot or PWM, selected at runtime, and talks to the ROV
firmware over USB.

## Development

`nix develop` (or `direnv allow`) provides the toolchain. CMake downloads the
Pico SDK.

```sh
make build-pico   # or build-pico2, build-mainboard
make flash-pico   # or flash-pico2; flash-mainboard needs BOOTSEL first
make test
```

`make help` lists every target. Debug output is on the USB serial port at
115200 baud, for example `picocom -b 115200 /dev/ttyACM0`.

## License

[AGPL-3.0-or-later](LICENSE)
