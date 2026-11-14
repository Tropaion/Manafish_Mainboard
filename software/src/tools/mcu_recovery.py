"""tools mcu-reset and mcu-recover: the MCU's RUN and BOOTSEL lines by hand.

For a hung or bricked MCU while the rov_firmware service is stopped. The
service's own flash path does the host duties (ESC power off, wait) over the
MCU link; these tools cannot, so they wait for the MCU's host-loss auto-off
instead: they refuse until manafish-firmware has been inactive or failed for
``SERVICE_QUIET_S``, and while the ESC recovery journal is set.
"""

from collections.abc import Callable
from pathlib import Path
import shutil
import subprocess
import sys
import time

from rov_firmware import mcu_gpio
from rov_firmware.esc_recovery import mcu_reset_refusal
from rov_firmware.mcu_gpio import McuGpioError
from rov_firmware.mcu_usb import BOOTSEL_PRODUCT, CDC_PRODUCT, rp_usb_products
from rov_firmware.models.config import McuBoard
from rov_firmware.websocket.receive.mcu import (
    picotool_load_command,
    resolve_mcu_firmware,
)


SERVICE = "manafish-firmware"
# MCU host-loss auto-off (30 s) plus its 2 s neutral stop plus margin.
SERVICE_QUIET_S = 35.0
_STOPPED_STATES = ("inactive", "failed")
_USB_SETTLE_S = 0.5
_USB_WAIT_S = 5.0

type ShowService = Callable[[], str]
type Clock = Callable[[], float]


def parse_systemctl_show(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` Key=Value lines."""
    properties = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def _systemctl_show() -> str:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        msg = "systemctl not found"
        raise FileNotFoundError(msg)
    return subprocess.run(  # noqa: S603 - fixed arguments
        [
            systemctl,
            "show",
            "-p",
            "ActiveState",
            "-p",
            "InactiveEnterTimestampMonotonic",
            SERVICE,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _monotonic_s() -> float:
    # systemd's *TimestampMonotonic values are CLOCK_MONOTONIC microseconds.
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def service_refusal(
    *, show: ShowService = _systemctl_show, now: Clock = _monotonic_s
) -> str | None:
    """Why the service may still own the MCU or its ESC power, or None."""
    try:
        properties = parse_systemctl_show(show())
    except (OSError, subprocess.SubprocessError) as error:
        return f"Cannot read the state of {SERVICE} through systemctl: {error}"
    state = properties.get("ActiveState", "")
    if state not in _STOPPED_STATES:
        return (
            f"{SERVICE} is {state or 'in an unknown state'}. Stop it first "
            f"(sudo systemctl stop {SERVICE}), then wait {SERVICE_QUIET_S:.0f} s: "
            "the MCU switches ESC power off 30 s after its host goes away."
        )
    try:
        stopped_at_s = int(properties["InactiveEnterTimestampMonotonic"]) / 1e6
    except (KeyError, ValueError):
        return f"Cannot read InactiveEnterTimestampMonotonic of {SERVICE}."
    quiet_s = now() - stopped_at_s
    if quiet_s < SERVICE_QUIET_S:
        return (
            f"{SERVICE} stopped {quiet_s:.0f} s ago. Wait "
            f"{SERVICE_QUIET_S - quiet_s:.0f} s more: the MCU switches ESC power "
            "off 30 s after its host goes away, then stops for 2 s."
        )
    return None


def refusal(
    *, show: ShowService = _systemctl_show, now: Clock = _monotonic_s
) -> str | None:
    """Why the MCU must not be reset or flashed by hand now, or None."""
    return mcu_reset_refusal() or service_refusal(show=show, now=now)


def _fail(message: str) -> int:
    _ = sys.stderr.write(f"{message}\n")
    return 1


def _report_usb() -> None:
    """Print what the MCU enumerates as after a reset (bring-up aid)."""
    time.sleep(_USB_SETTLE_S)
    deadline = time.monotonic() + _USB_WAIT_S
    wanted = {CDC_PRODUCT, BOOTSEL_PRODUCT}
    while not (products := rp_usb_products() & wanted):
        if time.monotonic() >= deadline:
            _ = sys.stdout.write("No 2e8a:0009 or 2e8a:000f device enumerated.\n")
            return
        time.sleep(mcu_gpio.USB_POLL_S)
    found = ", ".join(f"2e8a:{product}" for product in sorted(products))
    _ = sys.stdout.write(f"USB: {found}\n")


def reset_main() -> int:
    """``tools mcu-reset``: a 10 ms low pulse on RUN (GPIO24)."""
    reason = refusal()
    if reason is not None:
        return _fail(f"MCU not reset: {reason}")
    mcu_gpio.reset()
    _ = sys.stdout.write("RUN (GPIO24) pulsed low for 10 ms; the MCU restarts.\n")
    _report_usb()
    return 0


def recover_main(uf2: Path | None) -> int:
    """``tools mcu-recover [UF2]``: hardware BOOTSEL, then ``picotool load -v -x``.

    Loads the bundled ``~/mcu-firmware/mainboard-v*.uf2`` unless a UF2 is given.
    """
    reason = refusal()
    if reason is not None:
        return _fail(f"MCU not recovered: {reason}")
    if uf2 is None:
        resolved = resolve_mcu_firmware(McuBoard.PICO2)
        if resolved is None:
            return _fail("No ~/mcu-firmware/mainboard-v*.uf2 found; pass a UF2 path.")
        uf2 = resolved[0]
    if not uf2.is_file():
        return _fail(f"{uf2} is not a file.")
    command = picotool_load_command(uf2)
    if command is None:
        return _fail("picotool not found.")
    try:
        mcu_gpio.enter_bootsel()
    except McuGpioError as error:
        return _fail(str(error))
    _ = sys.stdout.write(f"MCU in BOOTSEL; loading {uf2}\n")
    return subprocess.run(command, check=False).returncode  # noqa: S603
