"""RP2354A reset and BOOTSEL from the CM5 (Decision 9, wiki 8.4).

- GPIO24 drives RUN (330 ohm series, 4.7 kohm pull-up): low = reset.
- GPIO25 drives QSPI_SS (1 kohm series, 10 kohm pull-up): low while RUN rises =
  BOOTSEL. It is the in-package flash chip select, so it is low only while RUN
  is low or within ``BOOTSEL_HOLD_S`` after RUN's release.

Both lines are only driven low or released (input, bias off), never driven
high. On any exception the ``Rp1Gpio`` context manager releases every line still
driven, GPIO24 before GPIO25. Every function blocks (lgpio, sleeps):
call it through ``asyncio.to_thread`` from the service. The host duties before
a reset (neutral, ESC power off, wait) are the caller's: see
``EscPowerController.prepare_for_mcu_reset``.
"""

from collections.abc import Callable
import os
from pathlib import Path
import time

from .mcu_usb import (
    BOOTSEL_PRODUCT,
    CDC_PRODUCT,
    RPI_USB_VENDOR,
    USB_DEVICES_PATH,
    rp_usb_products,
    usb_ids,
)
from .rp1_gpio import Rp1Gpio


RUN_LINE = 24
QSPI_SS_LINE = 25
RESET_PULSE_S = 0.01
BOOTSEL_HOLD_S = 0.1
BOOTSEL_WAIT_S = 5.0
USB_POLL_S = 0.05

USB_DEVICE_NODES = Path("/dev/bus/usb")

type GpioFactory = Callable[[], Rp1Gpio]
type Sleep = Callable[[float], None]
type Clock = Callable[[], float]


class McuGpioError(RuntimeError):
    """The MCU did not reach the expected USB state."""


def _bootsel_node_usable(devices_path: Path, device_nodes: Path) -> bool:
    """A 2e8a:000f device whose /dev/bus/usb node udev already opened to us."""
    for device in devices_path.glob("*"):
        if usb_ids(device) != (RPI_USB_VENDOR, BOOTSEL_PRODUCT):
            continue
        try:
            bus = int((device / "busnum").read_text())
            number = int((device / "devnum").read_text())
        except (OSError, ValueError):
            continue
        if os.access(device_nodes / f"{bus:03d}" / f"{number:03d}", os.R_OK | os.W_OK):
            return True
    return False


def wait_for_bootsel(
    *,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    devices_path: Path = USB_DEVICES_PATH,
    device_nodes: Path = USB_DEVICE_NODES,
    timeout_s: float = BOOTSEL_WAIT_S,
) -> None:
    """Wait until picotool can open the BOOTSEL device; McuGpioError on timeout.

    The sysfs entry appears before udev gives the node to plugdev, so the node
    itself must be readable and writable.
    """
    deadline = clock() + timeout_s
    while not _bootsel_node_usable(devices_path, device_nodes):
        if clock() >= deadline:
            msg = (
                f"The MCU did not enumerate as BOOTSEL (2e8a:{BOOTSEL_PRODUCT}) "
                f"within {timeout_s:.0f} s of the GPIO24/GPIO25 sequence."
            )
            raise McuGpioError(msg)
        sleep(USB_POLL_S)


def reset(*, gpio: GpioFactory = Rp1Gpio, sleep: Sleep = time.sleep) -> None:
    """Pulse RUN low for ``RESET_PULSE_S``; the MCU restarts its firmware."""
    with gpio() as lines:
        lines.drive_low(RUN_LINE)
        sleep(RESET_PULSE_S)
        lines.release(RUN_LINE)


def enter_bootsel(  # noqa: PLR0913 - every dependency is injectable for tests
    *,
    gpio: GpioFactory = Rp1Gpio,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    devices_path: Path = USB_DEVICES_PATH,
    device_nodes: Path = USB_DEVICE_NODES,
    timeout_s: float = BOOTSEL_WAIT_S,
) -> None:
    """Reset the MCU into the boot ROM's USB BOOTSEL, then wait for 2e8a:000f.

    Works on a blank, hung, bricked or running MCU. Order (Decision 9): GPIO24
    low, GPIO25 low, 10 ms, release GPIO24 (the ROM samples QSPI_SS), keep
    GPIO25 low 100 ms more, release GPIO25.
    """
    with gpio() as lines:
        lines.drive_low(RUN_LINE)
        lines.drive_low(QSPI_SS_LINE)
        sleep(RESET_PULSE_S)
        lines.release(RUN_LINE)
        sleep(BOOTSEL_HOLD_S)
        lines.release(QSPI_SS_LINE)
    wait_for_bootsel(
        sleep=sleep,
        clock=clock,
        devices_path=devices_path,
        device_nodes=device_nodes,
        timeout_s=timeout_s,
    )


def leave_stale_bootsel(
    *,
    recovery_pending: bool,
    gpio: GpioFactory = Rp1Gpio,
    sleep: Sleep = time.sleep,
    devices_path: Path = USB_DEVICES_PATH,
) -> str | None:
    """Service start: release GPIO24/25, then one RUN pulse if the MCU sits in BOOTSEL.

    Releasing undoes a line a killed process left driven low (pinctrl-rp1 keeps
    a freed output). The pulse runs only for 2e8a:000f without 0009 and never
    while ESC firmware recovery is pending. Returns a message to log, or None.
    """
    with gpio() as lines:
        lines.release(RUN_LINE)
        lines.release(QSPI_SS_LINE)
    products = rp_usb_products(devices_path)
    if BOOTSEL_PRODUCT not in products or CDC_PRODUCT in products:
        return None
    if recovery_pending:
        return (
            "The MCU sits in BOOTSEL (2e8a:000f), but ESC firmware recovery is "
            "pending, so it was not reset."
        )
    reset(gpio=gpio, sleep=sleep)
    return "The MCU sat in BOOTSEL (2e8a:000f without 0009); pulsed RUN once."
