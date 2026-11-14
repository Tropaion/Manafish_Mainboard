"""The RP2354A MCU on the CM5's USB: its serial port and its Raspberry Pi USB IDs.

Read-only sysfs lookups, no I/O on the device. ``serial.py`` finds the port
with ``find_mcu_port``; ``mcu_gpio.py`` and ``tools mcu-reset`` check what the
MCU enumerates as (BOOTSEL 2e8a:000f or the running firmware's 2e8a:0009).
"""

from pathlib import Path


USB_DEVICES_PATH = Path("/sys/bus/usb/devices")
RPI_USB_VENDOR = "2e8a"
BOOTSEL_PRODUCT = "000f"  # RP2350 boot ROM
CDC_PRODUCT = "0009"  # Pico SDK USB stdio on an RP2350 (the running MCU)
MCU_PORT_SYMLINK = Path("/dev/manafish-mcu")  # udev rule in nix/mcu.nix
MCU_USB_PRODUCT_NAME = "Manafish Mainboard"
_TTY_CLASS_PATH = Path("/sys/class/tty")
_DEV_PATH = Path("/dev")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def usb_ids(device: Path) -> tuple[str, str] | None:
    """Return (idVendor, idProduct) of a sysfs USB device, lower case, or None."""
    try:
        vendor = (device / "idVendor").read_text().strip().lower()
        product = (device / "idProduct").read_text().strip().lower()
    except OSError:
        return None
    return vendor, product


def rp_usb_products(devices_path: Path = USB_DEVICES_PATH) -> set[str]:
    """Product IDs of every attached Raspberry Pi (2e8a) USB device."""
    products = set()
    for device in devices_path.glob("*"):
        ids = usb_ids(device)
        if ids is not None and ids[0] == RPI_USB_VENDOR:
            products.add(ids[1])
    return products


def find_mcu_port(
    *,
    symlink: Path = MCU_PORT_SYMLINK,
    tty_class: Path = _TTY_CLASS_PATH,
    dev: Path = _DEV_PATH,
) -> str | None:
    """Return the MCU's serial port, or None.

    The udev symlink first, then a sysfs scan for a ttyACM backed by 2e8a:0009,
    preferring the product name "Manafish Mainboard". Never any other ttyACM:
    an unrelated CDC device must not receive motor commands.
    """
    if symlink.exists():
        return str(symlink)
    candidates: list[tuple[bool, str]] = []
    for tty in tty_class.glob("ttyACM*"):
        # ttyACMn/device is the USB interface; its parent is the USB device.
        usb_device = (tty / "device").resolve().parent
        if usb_ids(usb_device) != (RPI_USB_VENDOR, CDC_PRODUCT):
            continue
        named = _read_text(usb_device / "product") == MCU_USB_PRODUCT_NAME
        candidates.append((not named, tty.name))
    if not candidates:
        return None
    return str(dev / min(candidates)[1])
