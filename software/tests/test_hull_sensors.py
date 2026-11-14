"""SHT45 and WSEN-PADS hull sensors against their datasheet facts (wiki 8.2)."""

import asyncio
import errno
import threading

import pytest

from rov_firmware.constants import SYSTEM_FAILURE_THRESHOLD
from rov_firmware.sensors import hull
from rov_firmware.sensors.hull import (
    HULL_RECONNECT_INTERVAL_S,
    HullSensors,
    crc8,
    pads_convert,
    sht45_convert,
)


@pytest.fixture
def sensors(rov_state, i2c_bus, fake_clock):
    return HullSensors(
        rov_state,
        bus_factory=lambda: i2c_bus,
        clock=fake_clock.monotonic,
        sleep=fake_clock.sleep,
    )


def tick(sensors, fake_clock, cycles=1):
    for _ in range(cycles):
        sensors.cycle()
        fake_clock.sleep(1.0)


def test_addresses_commands_and_registers_match_the_fact_sheets():
    assert hull.SHT45_ADDRESS == 0x44
    assert hull.SHT45_MEASURE_HIGH_PRECISION == 0xFD
    assert hull.SHT45_MEASURE_WAIT_S >= 0.0083
    assert hull.PADS_ADDRESS == 0x5C
    assert (hull.PADS_DEVICE_ID_REGISTER, hull.PADS_DEVICE_ID) == (0x0F, 0xB3)
    assert (hull.PADS_CTRL_1, hull.PADS_CTRL_1_VALUE) == (0x10, 0x12)
    assert (hull.PADS_DATA_P_XL, hull.PADS_DATA_LENGTH) == (0x28, 5)


def test_crc8_matches_the_datasheet_example():
    assert crc8(b"\xbe\xef") == 0x92


def word(ticks):
    raw = ticks.to_bytes(2, "big")
    return raw + bytes([crc8(raw)])


def test_sht45_conversion_and_humidity_clamp():
    temperature, humidity = sht45_convert(word(0x6666) + word(0x7AE1))
    assert temperature == pytest.approx(25.0)
    assert humidity == pytest.approx(54.0, abs=0.01)
    assert sht45_convert(word(0) + word(0))[1] == 0.0
    assert sht45_convert(word(0xFFFF) + word(0xFFFF)) == (
        pytest.approx(130.0),
        100.0,
    )


def test_sht45_crc_error_is_refused():
    data = bytearray(word(0x6666) + word(0x7AE1))
    data[4] ^= 0x10
    with pytest.raises(ValueError, match="CRC"):
        sht45_convert(bytes(data))


@pytest.mark.parametrize(
    ("pressure", "temperature", "hpa", "celsius"),
    [
        (0x3F5400, 0x0E42, 1013.25, 36.50),
        (0x3F5400, 0xFF38, 1013.25, -2.00),
        (0x800000, 0x8000, -2048.0, -327.68),
    ],
)
def test_pads_conversion_is_signed_raw_over_4096_and_100(
    pressure, temperature, hpa, celsius
):
    raw = [*pressure.to_bytes(3, "little"), *temperature.to_bytes(2, "little")]
    assert pads_convert(raw) == (pytest.approx(hpa), pytest.approx(celsius))


def test_sht45_write_and_read_are_separate_transfers_10_ms_apart(
    sensors, rov_state, i2c_bus, fake_sht45
):
    sensors.cycle()

    sht_calls = [call for call in i2c_bus.calls if call[0] == "rdwr"]
    assert sht_calls == [("rdwr", 0x44, ("w",)), ("rdwr", 0x44, ("r",))]
    ((sent, _command),) = fake_sht45.commands
    assert fake_sht45.reads[0] - sent == pytest.approx(0.010)
    reading = rov_state.hull.sht45
    assert reading is not None
    assert reading.temperature_c == pytest.approx(25.0)
    assert rov_state.hull.sht45_healthy


def test_sht45_nack_while_measuring_is_retried(sensors, rov_state, fake_sht45):
    fake_sht45.measure_s = 0.013

    sensors.cycle()

    assert rov_state.hull.sht45 is not None
    assert len(fake_sht45.commands) == 1


def test_sht45_crc_error_measures_again_next_cycle(
    sensors, rov_state, fake_clock, fake_sht45
):
    tick(sensors, fake_clock)
    first = rov_state.hull.sht45
    fake_sht45.corrupt_next = True

    tick(sensors, fake_clock)

    assert rov_state.hull.sht45 == first  # the last good reading stays
    assert rov_state.hull.sht45_healthy
    tick(sensors, fake_clock)
    assert len(fake_sht45.commands) == 3
    assert rov_state.hull.sht45.sample_time > first.sample_time


def test_pads_checks_the_id_then_sets_1_hz_with_bdu(
    sensors, rov_state, fake_clock, fake_pads, i2c_bus
):
    tick(sensors, fake_clock)

    assert fake_pads.writes == [(0x10, 0x12)]
    pads_calls = [call for call in i2c_bus.calls if call[1] == 0x5C]
    assert pads_calls[:2] == [("read_byte", 0x5C, 0x0F), ("read_byte", 0x5C, 0x11)]
    # The first sample after configuring arrives within 1 s: none read yet.
    assert rov_state.hull.pads is None
    assert rov_state.hull.pads_healthy

    tick(sensors, fake_clock)

    reading = rov_state.hull.pads
    assert reading is not None
    assert reading.pressure_hpa == pytest.approx(1013.25)
    assert reading.temperature_c == pytest.approx(36.5)
    assert fake_pads.writes == [(0x10, 0x12)]


def test_pads_with_a_wrong_id_is_never_configured(
    sensors, rov_state, fake_clock, fake_pads
):
    fake_pads.regs[0x0F] = 0xB1

    tick(sensors, fake_clock, cycles=3)

    assert fake_pads.writes == []
    assert not rov_state.hull.pads_healthy
    assert rov_state.hull.pads is None


def test_pads_reset_is_detected_and_ctrl_1_rewritten(
    sensors, rov_state, fake_clock, fake_pads
):
    tick(sensors, fake_clock, cycles=2)
    good = rov_state.hull.pads
    fake_pads.soft_reset()
    fake_pads.set_raw(pressure=0, temperature=0)  # stale in power-down

    tick(sensors, fake_clock)

    assert fake_pads.writes == [(0x10, 0x12), (0x10, 0x12)]
    assert rov_state.hull.pads == good
    assert rov_state.hull.pads_healthy


def test_a_sensor_fails_after_three_cycles_and_reconnects_after_10_s(
    sensors, rov_state, fake_clock, fake_sht45
):
    tick(sensors, fake_clock, cycles=2)
    fake_sht45.present = False

    tick(sensors, fake_clock, cycles=SYSTEM_FAILURE_THRESHOLD - 1)
    assert rov_state.hull.sht45_healthy
    tick(sensors, fake_clock)
    assert not rov_state.hull.sht45_healthy
    assert rov_state.hull.sht45 is None
    # The other sensor is not affected.
    assert rov_state.hull.pads is not None

    fake_sht45.present = True
    attempts = len(fake_sht45.commands)
    tick(sensors, fake_clock, cycles=int(HULL_RECONNECT_INTERVAL_S) - 2)
    assert len(fake_sht45.commands) == attempts
    tick(sensors, fake_clock, cycles=2)
    assert rov_state.hull.sht45_healthy
    assert rov_state.hull.sht45 is not None


def test_a_missing_bus_is_retried(rov_state, i2c_bus, fake_clock):
    opened = []

    def factory():
        if not opened:
            opened.append("failed")
            raise FileNotFoundError(errno.ENOENT, "No such file", "/dev/i2c-1")
        opened.append("ok")
        return i2c_bus

    sensors = HullSensors(
        rov_state,
        bus_factory=factory,
        clock=fake_clock.monotonic,
        sleep=fake_clock.sleep,
    )
    tick(sensors, fake_clock)
    assert not rov_state.hull.sht45_healthy
    assert not rov_state.hull.pads_healthy

    tick(sensors, fake_clock, cycles=int(HULL_RECONNECT_INTERVAL_S) + 1)

    assert opened == ["failed", "ok"]
    assert rov_state.hull.sht45_healthy
    assert rov_state.hull.pads_healthy


def test_fresh_readings_expire(sensors, rov_state, fake_clock):
    tick(sensors, fake_clock, cycles=2)
    now = fake_clock.monotonic()
    assert rov_state.hull.fresh_sht45(now) is not None
    assert rov_state.hull.fresh_pads(now) is not None
    assert rov_state.hull.fresh_sht45(now + 5.0) is None
    assert rov_state.hull.fresh_pads(now + 5.0) is None


def test_run_reads_once_per_second_off_the_event_loop(rov_state, monkeypatch):
    threads = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    sensors = HullSensors(rov_state, async_sleep=fake_sleep)
    monkeypatch.setattr(
        sensors, "cycle", lambda: threads.append(threading.current_thread())
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sensors.run())

    assert len(threads) == 3
    assert all(thread is not threading.main_thread() for thread in threads)
    assert all(0.0 <= seconds <= 1.0 for seconds in sleeps)
