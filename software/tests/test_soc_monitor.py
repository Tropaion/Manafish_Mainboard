"""SocMonitor: latched rpi_volt alarm, thermal_zone0 and get_throttled (8.10)."""

import asyncio
import subprocess
import threading

import pytest

from rov_firmware import soc_monitor
from rov_firmware.models.soc import UNDERVOLTAGE_LATCH_S, SocStatus
from rov_firmware.soc_monitor import (
    THROTTLED_INTERVAL_S,
    SocMonitor,
    parse_throttled,
    read_temperature,
    read_throttled,
    throttled_flags,
)


@pytest.fixture(autouse=True)
def logs(monkeypatch):
    captured = []
    for level in ("info", "warn", "error"):
        monkeypatch.setattr(
            soc_monitor,
            f"log_{level}",
            lambda message, level=level: captured.append((level, message)),
        )
    return captured


@pytest.fixture
def hwmon(tmp_path):
    """The CM5's hwmon devices: cpu_thermal, rp1_adc, rpi_volt."""
    for index, name in enumerate(("cpu_thermal", "rp1_adc", "rpi_volt")):
        device = tmp_path / f"hwmon{index}"
        device.mkdir()
        (device / "name").write_text(f"{name}\n", encoding="ascii")
    (tmp_path / "hwmon2" / "in0_lcrit_alarm").write_text("0\n", encoding="ascii")
    return tmp_path


@pytest.fixture
def thermal_zone(tmp_path):
    """/sys/class/thermal/thermal_zone0/temp (cpu-thermal on the CM5)."""
    path = tmp_path / "thermal_zone0" / "temp"
    path.parent.mkdir()
    path.write_text("51234\n", encoding="ascii")
    return path


def set_alarm(hwmon, value):
    (hwmon / "hwmon2" / "in0_lcrit_alarm").write_text(f"{value}\n", encoding="ascii")


class Rig:
    def __init__(self, state, hwmon, thermal_zone):
        self.state = state
        self.now = 1000.0
        self.throttled: list[int | None] = []
        self.throttled_reads = 0
        self.monitor = SocMonitor(
            state,
            hwmon_root=hwmon,
            temperature_path=thermal_zone,
            throttled_reader=self.read_throttled,
            clock=lambda: self.now,
        )

    def read_throttled(self):
        self.throttled_reads += 1
        return self.throttled.pop(0) if self.throttled else 0

    def tick(self, seconds=1):
        for _ in range(int(seconds)):
            self.monitor.cycle()
            self.now += 1.0
        return self.state.soc


@pytest.fixture
def rig(rov_state, hwmon, thermal_zone):
    return Rig(rov_state, hwmon, thermal_zone)


def test_reads_thermal_zone0_in_millidegrees(thermal_zone):
    assert read_temperature(thermal_zone) == pytest.approx(51.234)
    assert read_temperature(thermal_zone.parent / "missing") is None


def test_unreadable_temperature_is_unavailable(thermal_zone):
    thermal_zone.write_text("garbage", encoding="ascii")
    assert read_temperature(thermal_zone) is None


def test_a_toggling_alarm_latches_for_60_s(rig, hwmon, logs):
    status = rig.tick()
    assert status.pi_undervoltage(rig.now) is False
    set_alarm(hwmon, 1)
    rig.tick()
    set_alarm(hwmon, 0)
    asserted = rig.now - 1
    status = rig.tick(3)
    # The instantaneous sample reads 0 again, the latch holds.
    assert status.undervoltage is False
    assert status.pi_undervoltage(rig.now) is True
    rig.now = asserted + UNDERVOLTAGE_LATCH_S - 1
    status = rig.tick()
    assert status.pi_undervoltage(rig.now - 1) is True
    status = rig.tick()
    assert status.pi_undervoltage(rig.now) is False
    assert ("warn", "Pi input undervoltage (rpi_volt in0_lcrit_alarm)") in logs
    assert ("info", "Pi input undervoltage alarm clear for 60 s") in logs


def test_a_missing_alarm_is_unavailable_not_an_alarm(rig, hwmon):
    (hwmon / "hwmon2" / "in0_lcrit_alarm").unlink()
    status = rig.tick()
    assert status.undervoltage is None
    assert status.pi_undervoltage(rig.now) is None


def test_a_stale_status_is_unavailable_but_the_latch_holds():
    status = SocStatus(updated_at=100.0, undervoltage=False)
    assert status.pi_undervoltage(100.5) is False
    assert status.pi_undervoltage(110.0) is None
    latched = SocStatus(
        updated_at=100.0, undervoltage=True, undervoltage_asserted_at=100.0
    )
    assert latched.pi_undervoltage(150.0) is True
    assert latched.pi_undervoltage(161.0) is None


def test_temperature_and_its_freshness(rig):
    status = rig.tick()
    assert status.fresh_temperature(rig.now) == pytest.approx(51.234)
    assert status.fresh_temperature(rig.now + 5) is None


def test_get_throttled_every_10_s_and_logged_on_change(rig, logs):
    rig.throttled = [0x50000, 0x50005]
    status = rig.tick()
    assert status.throttled == 0x50000
    rig.tick(int(THROTTLED_INTERVAL_S) - 1)
    assert rig.throttled_reads == 1
    status = rig.tick()
    assert rig.throttled_reads == 2
    assert status.throttled == 0x50005
    assert (
        "info",
        "vcgencmd get_throttled = 0x50000 (undervoltage_occurred, throttled_occurred)",
    ) in logs
    assert any(
        level == "warn" and message.startswith("vcgencmd get_throttled = 0x50005")
        for level, message in logs
    )


def test_parse_and_decode_throttled():
    assert parse_throttled("throttled=0x50000\n") == 0x50000
    assert parse_throttled("error") is None
    assert throttled_flags(0x5) == ["undervoltage", "throttled"]


def test_read_throttled_tolerates_a_missing_vcgencmd():
    assert read_throttled(None) is None


def test_vcgencmd_is_looked_up_once(rov_state, hwmon, thermal_zone, monkeypatch):
    lookups = []

    def which(name):
        lookups.append(name)

    monkeypatch.setattr(soc_monitor.shutil, "which", which)
    monitor = SocMonitor(rov_state, hwmon_root=hwmon, temperature_path=thermal_zone)
    for _ in range(int(THROTTLED_INTERVAL_S) * 3):
        monitor.cycle()
    assert lookups == ["vcgencmd"]
    assert rov_state.soc.throttled is None


def test_read_throttled_runs_vcgencmd_with_a_timeout():
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return subprocess.CompletedProcess(args, 0, stdout="throttled=0x0\n")

    assert read_throttled("/bin/vcgencmd", run=run) == 0
    assert calls == [(["/bin/vcgencmd", "get_throttled"], 2.0)]


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 255, stdout=""),
        subprocess.TimeoutExpired("vcgencmd", 2.0),
        OSError("no /dev/vcio"),
    ],
)
def test_read_throttled_failures_are_unavailable(outcome):
    def run(_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    assert read_throttled("/bin/vcgencmd", run=run) is None


def test_run_samples_off_the_event_loop(rov_state, hwmon, thermal_zone):
    threads = []

    def reader():
        threads.append(threading.current_thread())

    async def stop(_seconds):
        raise asyncio.CancelledError

    monitor = SocMonitor(
        rov_state,
        hwmon_root=hwmon,
        temperature_path=thermal_zone,
        throttled_reader=reader,
        async_sleep=stop,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.run())
    assert threads
    assert threads[0] is not threading.main_thread()
    assert rov_state.soc.temperature_c == pytest.approx(51.234)
