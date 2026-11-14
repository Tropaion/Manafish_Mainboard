"""rp1_gpio: lgpio on the chip labelled pinctrl-rp1, never driving a line high."""

import pytest

from rov_firmware import rp1_gpio
from rov_firmware.rp1_gpio import Rp1Gpio, Rp1GpioError


def test_opens_the_chip_labelled_pinctrl_rp1(fake_lgpio, gpio_dev):
    fake_lgpio.chips = {0: "gpio-brcmstb@107d508500", 13: "pinctrl-rp1"}

    with Rp1Gpio(fake_lgpio, dev=gpio_dev):
        assert list(fake_lgpio.open_handles.values()) == [13]

    # Chip 0 was opened, checked and closed; 10-12 do not exist in the fake.
    assert fake_lgpio.calls[:2] == ["open 0", "close 0"]
    assert fake_lgpio.open_handles == {}


def test_missing_rp1_chip_is_an_error(fake_lgpio, gpio_dev):
    fake_lgpio.chips = {0: "gpio-brcmstb@107d508500"}

    with pytest.raises(Rp1GpioError, match="pinctrl-rp1"):
        Rp1Gpio(fake_lgpio, dev=gpio_dev)
    assert fake_lgpio.open_handles == {}


def test_chips_are_tried_in_numeric_order(fake_lgpio, tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    for name in ("gpiochip10", "gpiochip2", "gpiochipX", "ttyACM0"):
        (dev / name).touch()
    fake_lgpio.chips = {2: "other", 10: "pinctrl-rp1"}

    with Rp1Gpio(fake_lgpio, dev=dev):
        pass

    assert [c for c in fake_lgpio.calls if c.startswith("open")] == [
        "open 2",
        "open 10",
    ]


def test_drive_low_claims_an_output_at_level_zero(fake_lgpio, gpio_dev):
    with Rp1Gpio(fake_lgpio, dev=gpio_dev) as gpio:
        gpio.drive_low(24)
        assert fake_lgpio.driven_low == {24}
        gpio.release(24)


def test_release_reclaims_as_input_with_bias_off_then_frees(fake_lgpio, gpio_dev):
    with Rp1Gpio(fake_lgpio, dev=gpio_dev) as gpio:
        gpio.drive_low(25)
        gpio.release(25)

        assert fake_lgpio.driven_low == set()
        assert fake_lgpio.input_flags == [(25, fake_lgpio.SET_PULL_NONE)]
        assert fake_lgpio.bias[25] == "none"
        assert 25 not in fake_lgpio.claims
        assert fake_lgpio.calls[-2:] == ["input 25", "free 25"]


def test_plain_free_would_leave_the_line_driven(fake_lgpio):
    # Why release() reclaims as input first: pinctrl-rp1 keeps a freed output.
    handle = fake_lgpio.gpiochip_open(0)
    fake_lgpio.gpio_claim_output(handle, 24, 0, 0)
    fake_lgpio.gpio_free(handle, 24)

    assert fake_lgpio.driven_low == {24}


def test_close_releases_a_line_still_driven(fake_lgpio, gpio_dev):
    gpio = Rp1Gpio(fake_lgpio, dev=gpio_dev)
    gpio.drive_low(24)

    gpio.close()

    assert fake_lgpio.driven_low == set()
    assert fake_lgpio.bias[24] == "none"
    assert fake_lgpio.open_handles == {}


def test_exception_inside_the_context_still_releases(fake_lgpio, gpio_dev):
    with pytest.raises(RuntimeError), Rp1Gpio(fake_lgpio, dev=gpio_dev) as gpio:
        gpio.drive_low(24)
        raise RuntimeError

    assert fake_lgpio.driven_low == set()


def test_read_input_keeps_the_config_txt_pull_up(fake_lgpio, gpio_dev):
    fake_lgpio.levels[6] = 0

    with Rp1Gpio(fake_lgpio, dev=gpio_dev) as gpio:
        assert gpio.read_input(6) == 0
        assert gpio.read_input(12) == 1

    bias_flags = (
        fake_lgpio.SET_PULL_UP | fake_lgpio.SET_PULL_DOWN | fake_lgpio.SET_PULL_NONE
    )
    assert [flags & bias_flags for _, flags in fake_lgpio.input_flags] == [0, 0]
    assert fake_lgpio.bias[6] == "up"
    assert fake_lgpio.bias[12] == "up"
    assert fake_lgpio.claims == {}


def test_read_input_refuses_a_line_it_drives(fake_lgpio, gpio_dev):
    with Rp1Gpio(fake_lgpio, dev=gpio_dev) as gpio:
        gpio.drive_low(24)
        with pytest.raises(ValueError, match="24"):
            gpio.read_input(24)
        assert fake_lgpio.driven_low == {24}


def test_without_lgpio_the_error_says_so(monkeypatch, gpio_dev):
    def missing(_name):
        msg = "No module named 'lgpio'"
        raise ModuleNotFoundError(msg)

    monkeypatch.setattr(rp1_gpio.importlib, "import_module", missing)

    with pytest.raises(Rp1GpioError, match="lgpio"):
        Rp1Gpio(dev=gpio_dev)
