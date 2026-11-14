# Manafish Firmware

> **Mainboard fork.** An adapted copy of
> [manafishrov/firmware](https://github.com/manafishrov/firmware) at `9e282b5`
> in the [Manafish Mainboard](../README.md) repository. Done here: the CM5
> NixOS image `cm5-mainboard` with the mainboard MCU UF2, and the Pi-side
> BMI270 code is gone (0x68 on I2C1 is the charger). Planned: the new I2C
> sensors and charger (SHT45, WSEN-PADS, LTC4162) and MCU recovery over the
> RP2354A's RUN and BOOTSEL lines. The root
> [CLAUDE.md](../CLAUDE.md) governs commits, tags and releases; upstream sync
> uses `git subtree`. Run the commands below from `software/`.

NixOS image and Python service for the Raspberry Pi Compute Module 5 on the
Manafish Mainboard, with two IMX477 cameras. Any CM5 with eMMC (16 GB or more)
works, with or without wireless and with any RAM size; CM5 Lite doesn't, since
the mainboard has no SD card slot. The same image is meant to boot on the
official CM5 IO Board too.

## Build the SD image

Building needs Nix on aarch64-linux, or emulation for it, with the
`nixos-raspberrypi` caches configured:

```sh
nix build .
```

The zstd-compressed image is written to `result/sd-image/`. There are no
software releases yet; the `Software image` workflow keeps each CI build as a
7-day artifact.

## Flash

The image goes onto the CM5's eMMC. Put the CM5 into USB boot mode. On the
CM5 IO Board, fit jumper J2 pins 1-2 ("disable eMMC Boot") and cable its USB-C
port J11 to the computer, which then also powers the board. On the mainboard,
fit jumper J7 pins 9-10 and use J8. Expose the eMMC with
[usbboot](https://github.com/raspberrypi/usbboot) (`rpiboot` and its
`mass-storage-gadget64` come from nixpkgs), find the new USB disk with
`lsblk`, unmount any of its partitions that the desktop mounted, and write the
image:

```sh
nix shell nixpkgs#rpiboot -c sh -c 'r=$(command -v rpiboot); sudo "$r" -d "${r%/bin/rpiboot}/share/rpiboot/mass-storage-gadget64"'
lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINTS
sudo umount /dev/XXX1 /dev/XXX2 # only if lsblk shows them mounted
zstd -dc result/sd-image/*.zst | sudo dd of=/dev/XXX bs=4M status=progress conv=fsync
```

Then remove the jumper and power-cycle (IO Board: a 5 V/5 A supply on J11).
On the IO Board, camera 1 goes on CAM/DISP 0; a camera on CAM/DISP 1 needs
both J6 jumpers.

## Connect

The ROV uses `10.10.10.10` and gives out addresses over DHCP on its Ethernet
port. Connect a computer, or a phone through a USB-C Ethernet adapter, and enter
that address in the app.

## Development

```sh
uv sync
uv run pre-commit install
```

The hooks come from the root
[`.pre-commit-config.yaml`](../.pre-commit-config.yaml), which covers both
`software/` and `firmware/`.

`nix develop` provides the Python version pinned in `pyproject.toml`. Without
Nix, install it first with `uv python install <version>`.

## License

[AGPL-3.0-or-later](LICENSE)
