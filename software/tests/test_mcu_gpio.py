"""mcu_gpio: hardware RUN pulse and BOOTSEL (Decision 9) on CM5 GPIO24/GPIO25."""

import math
from pathlib import Path

import pytest

from rov_firmware import mcu_gpio
from rov_firmware.mcu_gpio import (
    BOOTSEL_HOLD_S,
    QSPI_SS_LINE,
    RESET_PULSE_S,
    RUN_LINE,
    McuGpioError,
)
from rov_firmware.mcu_usb import rp_usb_products
from rov_firmware.rp1_gpio import Rp1Gpio


class Usb:
    """A fake /sys/bus/usb/devices plus /dev/bus/usb."""

    def __init__(self, root: Path) -> None:
        self.devices = root / "sys-usb"
        self.nodes = root / "dev-usb"
        self.devices.mkdir()
        self.nodes.mkdir()
        self._next = 2

    def add(self, product: str, *, node: bool = True, vendor: str = "2e8a") -> None:
        device = self.devices / f"1-{self._next}"
        device.mkdir()
        (device / "idVendor").write_text(f"{vendor}\n")
        (device / "idProduct").write_text(f"{product}\n")
        (device / "busnum").write_text("1\n")
        (device / "devnum").write_text(f"{self._next}\n")
        (device.parent / f"1-{self._next}:1.0").mkdir()
        if node:
            bus = self.nodes / "001"
            bus.mkdir(exist_ok=True)
            (bus / f"{self._next:03d}").touch()
        self._next += 1


@pytest.fixture
def usb(tmp_path):
    return Usb(tmp_path)


@pytest.fixture
def open_gpio(fake_lgpio, gpio_dev):
    return lambda: Rp1Gpio(fake_lgpio, dev=gpio_dev)


def _bootsel(fake_lgpio, open_gpio, usb, **kwargs):
    mcu_gpio.enter_bootsel(
        gpio=open_gpio,
        sleep=fake_lgpio.sleep,
        clock=lambda: fake_lgpio.now,
        devices_path=usb.devices,
        device_nodes=usb.nodes,
        **kwargs,
    )


def _low_intervals(transitions, line):
    intervals, start = [], None
    for at, which, what in transitions:
        if which != line:
            continue
        if what == "low":
            start = at
        else:
            intervals.append((start, at))
            start = None
    if start is not None:
        intervals.append((start, math.inf))
    return intervals


def _assert_qspi_low_only_with_run_low_or_just_after(transitions):
    run = _low_intervals(transitions, RUN_LINE)
    for start, end in _low_intervals(transitions, QSPI_SS_LINE):
        assert any(
            run_start <= start <= run_end and end <= run_end + BOOTSEL_HOLD_S + 1e-9
            for run_start, run_end in run
        ), (start, end, run)


def test_enter_bootsel_follows_decision_9(fake_lgpio, open_gpio, usb):
    usb.add("000f")

    _bootsel(fake_lgpio, open_gpio, usb)

    assert fake_lgpio.transitions == [
        (0.0, RUN_LINE, "low"),
        (0.0, QSPI_SS_LINE, "low"),
        (pytest.approx(RESET_PULSE_S), RUN_LINE, "released"),
        (pytest.approx(RESET_PULSE_S + BOOTSEL_HOLD_S), QSPI_SS_LINE, "released"),
    ]
    _assert_qspi_low_only_with_run_low_or_just_after(fake_lgpio.transitions)
    assert fake_lgpio.driven_low == set()
    assert fake_lgpio.bias[RUN_LINE] == fake_lgpio.bias[QSPI_SS_LINE] == "none"
    assert fake_lgpio.open_handles == {}


def test_an_interrupted_hold_still_releases_both_lines(fake_lgpio, open_gpio, usb):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        fake_lgpio.sleep(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        mcu_gpio.enter_bootsel(
            gpio=open_gpio,
            sleep=sleep,
            clock=lambda: fake_lgpio.now,
            devices_path=usb.devices,
            device_nodes=usb.nodes,
        )

    assert fake_lgpio.driven_low == set()
    _assert_qspi_low_only_with_run_low_or_just_after(fake_lgpio.transitions)


def test_a_failed_qspi_claim_releases_run(fake_lgpio, open_gpio, usb):
    real_claim = fake_lgpio.gpio_claim_output

    def claim(handle, gpio, level=0, flags=0):
        if gpio == QSPI_SS_LINE:
            msg = "busy"
            raise fake_lgpio.error(msg)
        return real_claim(handle, gpio, level, flags)

    fake_lgpio.gpio_claim_output = claim

    with pytest.raises(fake_lgpio.error):
        _bootsel(fake_lgpio, open_gpio, usb)

    assert fake_lgpio.driven_low == set()
    assert fake_lgpio.transitions == [
        (0.0, RUN_LINE, "low"),
        (0.0, RUN_LINE, "released"),
    ]


def test_enter_bootsel_waits_for_the_bootsel_device(fake_lgpio, open_gpio, usb):
    real_sleep = fake_lgpio.sleep

    def sleep(seconds):
        real_sleep(seconds)
        if fake_lgpio.now >= 1.0 and not any(usb.devices.iterdir()):
            usb.add("000f")

    mcu_gpio.enter_bootsel(
        gpio=open_gpio,
        sleep=sleep,
        clock=lambda: fake_lgpio.now,
        devices_path=usb.devices,
        device_nodes=usb.nodes,
    )

    assert 1.0 <= fake_lgpio.now < 1.2


def test_enter_bootsel_fails_without_the_bootsel_device(fake_lgpio, open_gpio, usb):
    usb.add("0009")

    with pytest.raises(McuGpioError, match="2e8a:000f"):
        _bootsel(fake_lgpio, open_gpio, usb, timeout_s=1.0)

    assert fake_lgpio.driven_low == set()


def test_bootsel_device_counts_only_once_udev_made_its_node_usable(fake_lgpio, usb):
    usb.add("000f", node=False)

    with pytest.raises(McuGpioError):
        mcu_gpio.wait_for_bootsel(
            sleep=fake_lgpio.sleep,
            clock=lambda: fake_lgpio.now,
            devices_path=usb.devices,
            device_nodes=usb.nodes,
            timeout_s=0.5,
        )


def test_reset_is_a_10_ms_run_pulse(fake_lgpio, open_gpio):
    mcu_gpio.reset(gpio=open_gpio, sleep=fake_lgpio.sleep)

    assert fake_lgpio.transitions == [
        (0.0, RUN_LINE, "low"),
        (pytest.approx(RESET_PULSE_S), RUN_LINE, "released"),
    ]
    assert not any(c.endswith(f" {QSPI_SS_LINE}") for c in fake_lgpio.calls)


def test_reset_releases_run_when_the_pulse_is_interrupted(fake_lgpio, open_gpio):
    def sleep(_seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        mcu_gpio.reset(gpio=open_gpio, sleep=sleep)

    assert fake_lgpio.driven_low == set()


def test_usb_products_lists_raspberry_pi_ids(usb):
    usb.add("0009")
    usb.add("000f")
    usb.add("0001", vendor="1d6b")

    assert rp_usb_products(usb.devices) == {"0009", "000f"}


@pytest.mark.parametrize(
    ("products", "recovery_pending", "pulsed"),
    [
        (["000f"], False, True),
        (["000f"], True, False),
        (["0009"], False, False),
        (["000f", "0009"], False, False),
        ([], False, False),
    ],
)
def test_startup_pulses_run_only_for_a_stale_bootsel(  # noqa: PLR0913
    fake_lgpio, open_gpio, usb, products, recovery_pending, pulsed
):
    for product in products:
        usb.add(product)

    message = mcu_gpio.leave_stale_bootsel(
        recovery_pending=recovery_pending,
        gpio=open_gpio,
        sleep=fake_lgpio.sleep,
        devices_path=usb.devices,
    )

    run_pulses = [t for t in fake_lgpio.transitions if t[2] == "low"]
    assert bool(run_pulses) is pulsed
    assert all(line == RUN_LINE for _, line, _ in run_pulses)
    # A message whenever the MCU sat in BOOTSEL: pulsed, or refused during recovery.
    assert (message is not None) is (products == ["000f"])
    assert fake_lgpio.driven_low == set()


def test_startup_releases_lines_a_crashed_process_left_driven(
    fake_lgpio, open_gpio, usb
):
    # A killed process frees its lines; pinctrl-rp1 keeps them driving low.
    handle = fake_lgpio.gpiochip_open(0)
    fake_lgpio.gpio_claim_output(handle, RUN_LINE, 0, 0)
    fake_lgpio.gpiochip_close(handle)
    assert fake_lgpio.driven_low == {RUN_LINE}

    mcu_gpio.leave_stale_bootsel(
        recovery_pending=False,
        gpio=open_gpio,
        sleep=fake_lgpio.sleep,
        devices_path=usb.devices,
    )

    assert fake_lgpio.driven_low == set()
    assert fake_lgpio.bias[RUN_LINE] == fake_lgpio.bias[QSPI_SS_LINE] == "none"
