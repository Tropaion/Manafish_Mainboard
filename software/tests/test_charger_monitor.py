"""ChargerMonitor: 1 Hz LTC4162 read-back, telemetry validity and PoE (wiki 6.6)."""

import asyncio
import threading

import pytest

from rov_firmware.models.charger import PoeType, ThermalLevel
from rov_firmware.models.hull import HullData, Sht45Reading
from rov_firmware.rp1_gpio import Rp1Gpio, Rp1GpioError
from rov_firmware.sensors import charger as charger_module, ltc4162
from rov_firmware.sensors.charger import (
    CHARGER_WRITES_ENABLED,
    TELEMETRY_SETTLE_S,
    ChargerMonitor,
)


POE = ltc4162.VIN_GT_4P2V | ltc4162.VIN_GT_VBAT | ltc4162.INTVCC_GT_2P8V
FORCE = ltc4162.FORCE_TELEMETRY_ON
SUSPEND = ltc4162.SUSPEND_CHARGER


@pytest.fixture(autouse=True)
def logs(monkeypatch):
    captured = []
    for level in ("info", "warn", "error"):
        monkeypatch.setattr(
            charger_module,
            f"log_{level}",
            lambda message, level=level: captured.append((level, message)),
        )
    return captured


class Rig:
    """A monitor on the fake bus, GPIO and clock; ``tick`` runs 1 s cycles."""

    def __init__(self, state, monitor, clock):
        self.state = state
        self.monitor = monitor
        self.clock = clock

    def tick(self, cycles=1, hull_c=25.0):
        for _ in range(cycles):
            if hull_c is not None:
                self.state.hull = HullData(
                    sht45=Sht45Reading(
                        temperature_c=hull_c,
                        humidity_pct=40.0,
                        sample_time=self.clock.now,
                    ),
                    sht45_healthy=True,
                )
            self.monitor.cycle()
            self.clock.sleep(1.0)
        return self.state.charger


@pytest.fixture
def make_rig(rov_state, i2c_bus, fake_clock, fake_lgpio, gpio_dev):
    def make(**kwargs):
        kwargs.setdefault("bus_factory", lambda: i2c_bus)
        kwargs.setdefault("gpio_factory", lambda: Rp1Gpio(fake_lgpio, dev=gpio_dev))
        monitor = ChargerMonitor(rov_state, clock=fake_clock.monotonic, **kwargs)
        return Rig(rov_state, monitor, fake_clock)

    return make


@pytest.fixture
def rig(make_rig):
    return make_rig()


@pytest.fixture
def writing_rig(make_rig):
    return make_rig(writes_enabled=True)


def written(fake_ltc4162):
    return [register for register, _ in fake_ltc4162.writes]


def test_charger_writes_stay_disabled_until_bring_up_b9():
    assert CHARGER_WRITES_ENABLED is False


def test_before_b9_only_force_telemetry_on_is_ever_written(rig, fake_ltc4162):
    fake_ltc4162.set_telemetry()
    fake_ltc4162.regs[0x39] = POE

    status = rig.tick(cycles=10, hull_c=60.0)

    assert fake_ltc4162.writes == [(0x14, FORCE)]
    assert not status.writes_enabled
    # The policy still says what it would write: suspended for heat.
    assert status.targets.config_bits & SUSPEND
    assert status.thermal is ThermalLevel.SUSPENDED


def test_telemetry_waits_for_one_slow_cycle_after_force_telemetry_on(rig, fake_ltc4162):
    fake_ltc4162.set_telemetry(vbat=18190)

    statuses = [rig.tick() for _ in range(int(TELEMETRY_SETTLE_S) + 1)]

    assert [status.telemetry is None for status in statuses] == [True] * int(
        TELEMETRY_SETTLE_S
    ) + [False]
    assert "conversion" in statuses[0].telemetry_problem
    assert statuses[-1].readable
    assert statuses[-1].telemetry.vbat_v == pytest.approx(17.5, abs=0.01)
    assert statuses[-1].telemetry_problem is None


def test_telemetry_valid_bit_zero_keeps_it_invalid(rig, fake_ltc4162):
    fake_ltc4162.set_telemetry(valid=False)

    status = rig.tick(cycles=10)

    assert status.telemetry is None
    assert "telemetry_valid" in status.telemetry_problem


def test_vbat_zero_is_invalid_but_poe_presence_is_known(rig, fake_ltc4162):
    fake_ltc4162.regs[0x14] = FORCE
    fake_ltc4162.set_telemetry(vbat=0)
    fake_ltc4162.regs[0x39] = POE

    status = rig.tick()

    assert status.telemetry is None
    assert "vbat" in status.telemetry_problem
    assert status.readable
    assert status.poe_present is True
    assert status.poe_type is PoeType.TYPE_1
    assert status.vbat_zero is True
    assert status.charger_state == ltc4162.CHARGER_SUSPENDED


def test_charger_state_is_known_while_telemetry_settles(rig, fake_ltc4162):
    fake_ltc4162.set_telemetry(vbat=0)
    fake_ltc4162.regs[0x34] = ltc4162.BAT_MISSING_FAULT

    status = rig.tick()

    assert status.telemetry is None
    assert "conversion" in status.telemetry_problem
    # vbat 0 before a full conversion proves nothing about the pack.
    assert status.vbat_zero is False
    assert status.charger_state == ltc4162.BAT_MISSING_FAULT


def test_force_bit_already_set_needs_no_wait(rig, fake_ltc4162):
    fake_ltc4162.regs[0x14] = FORCE
    fake_ltc4162.set_telemetry()

    status = rig.tick()

    assert fake_ltc4162.writes == []
    assert status.telemetry is not None
    assert status.fresh_telemetry(rig.clock.now) is status.telemetry
    assert status.fresh_telemetry(rig.clock.now + 5.0) is None


def test_an_i2c_error_makes_the_charger_unreadable_until_it_answers(
    rig, fake_ltc4162, logs
):
    fake_ltc4162.regs[0x14] = FORCE
    fake_ltc4162.set_telemetry()
    rig.tick()
    fake_ltc4162.present = False

    down = rig.tick(cycles=3)

    assert not down.readable
    assert down.telemetry is None
    assert down.poe_present is None
    assert not down.is_fresh(rig.clock.now)
    assert sum("unreadable" in message for _, message in logs) == 1

    fake_ltc4162.present = True
    up = rig.tick()
    assert up.readable
    assert up.telemetry is not None


def test_a_chip_reset_sets_force_telemetry_on_again_and_waits_again(
    rig, fake_ltc4162, logs
):
    fake_ltc4162.set_telemetry()
    status = rig.tick(cycles=int(TELEMETRY_SETTLE_S) + 1)
    assert status.telemetry is not None
    fake_ltc4162.power_cycle()

    status = rig.tick()

    assert fake_ltc4162.writes == [(0x14, FORCE), (0x14, FORCE)]
    assert status.telemetry is None
    assert any(level == "warn" and "lost" in message for level, message in logs)


@pytest.mark.parametrize(
    "case",
    [
        ((1, 1), PoeType.TYPE_1, 6),
        ((0, 1), PoeType.TYPE_2, 14),
        ((1, 0), PoeType.TYPE_3, 29),
        ((0, 0), PoeType.TYPE_4, 41),
    ],
)
def test_poe_type_from_the_pins_sets_the_input_limit(
    rig, fake_ltc4162, fake_lgpio, case
):
    pins, poe, code = case
    fake_ltc4162.regs[0x39] = POE
    fake_lgpio.levels.update({6: pins[0], 12: pins[1]})

    status = rig.tick(cycles=2)

    assert status.poe_type is poe
    assert status.poe_pins == pins
    assert status.targets.iin_limit_target == code
    # Read as inputs without a bias flag: config.txt's pull-ups stay.
    assert fake_lgpio.bias[6] == fake_lgpio.bias[12] == "up"
    assert all(flags == 0 for line, flags in fake_lgpio.input_flags if line in (6, 12))


def test_without_poe_the_pins_do_not_matter(rig, fake_ltc4162, fake_lgpio):
    fake_ltc4162.regs[0x39] = ltc4162.VIN_GT_VBAT | ltc4162.INTVCC_GT_2P8V
    fake_lgpio.levels.update({6: 0, 12: 0})

    status = rig.tick()

    assert status.poe_present is False
    assert status.poe_type is PoeType.NONE


def test_the_input_limit_rises_only_after_two_equal_reads(
    rig, fake_ltc4162, fake_lgpio
):
    fake_ltc4162.regs[0x39] = POE

    limits = []
    for levels in ((1, 1), (0, 0), (0, 0), (1, 1), (0, 0)):
        fake_lgpio.levels.update({6: levels[0], 12: levels[1]})
        limits.append(rig.tick().targets.iin_limit_target)

    assert limits == [6, 6, 41, 6, 6]


def test_unreadable_pins_with_poe_get_the_type_1_limit(make_rig, fake_ltc4162, logs):
    def no_gpio():
        msg = "No GPIO chip labelled pinctrl-rp1 in /dev"
        raise Rp1GpioError(msg)

    rig = make_rig(gpio_factory=no_gpio)
    fake_ltc4162.regs[0x39] = POE

    status = rig.tick(cycles=3)

    assert status.poe_type is PoeType.UNKNOWN
    assert status.poe_pins is None
    assert status.targets.iin_limit_target == 6
    assert sum("pinctrl-rp1" in message for _, message in logs) == 1


def test_from_b9_settings_are_written_jeita_off_first(writing_rig, fake_ltc4162):
    fake_ltc4162.regs[0x39] = POE

    status = writing_rig.tick()

    assert written(fake_ltc4162) == [0x14, 0x29, 0x1B, 0x1A, 0x15, 0x14]
    settings = fake_ltc4162.settings()
    assert settings == status.targets
    assert settings.charge_current_setting == 19
    assert settings.vcharge_setting == 31
    assert settings.charger_config_bits == 0
    assert settings.iin_limit_target == 6


def test_from_b9_a_matching_read_back_writes_nothing(writing_rig, fake_ltc4162):
    writing_rig.tick()
    fake_ltc4162.writes.clear()

    writing_rig.tick(cycles=5)

    assert fake_ltc4162.writes == []


def test_from_b9_a_drifted_register_is_rewritten(writing_rig, fake_ltc4162):
    writing_rig.tick()
    fake_ltc4162.writes.clear()
    fake_ltc4162.regs[0x15] = 63

    writing_rig.tick()

    assert fake_ltc4162.writes == [(0x15, 6)]


def test_from_b9_a_chip_reset_rewrites_everything(writing_rig, fake_ltc4162, logs):
    writing_rig.tick()
    fake_ltc4162.writes.clear()
    fake_ltc4162.power_cycle()

    status = writing_rig.tick()

    assert written(fake_ltc4162) == [0x14, 0x29, 0x1B, 0x1A, 0x15, 0x14]
    assert fake_ltc4162.settings() == status.targets
    assert any("en_jeita" in message for _, message in logs)


def test_from_b9_heat_suspends_once_and_resumes_below_the_hysteresis(
    writing_rig, fake_ltc4162
):
    fake_ltc4162.regs[0x39] = POE
    fake_ltc4162.regs[0x14] = FORCE
    fake_ltc4162.set_telemetry(vbat=0)  # no pack: the die ADC still counts
    writing_rig.tick()
    fake_ltc4162.writes.clear()

    writing_rig.tick(hull_c=56.0)
    writing_rig.tick(cycles=3, hull_c=52.0)
    # Suspended, and the (unused) input limit is halved with it: 6 -> 2.
    assert fake_ltc4162.writes == [(0x15, 2), (0x14, FORCE | SUSPEND)]

    status = writing_rig.tick(hull_c=49.0)
    assert fake_ltc4162.writes[2:] == [(0x14, FORCE)]
    assert status.thermal is ThermalLevel.HALVED


def test_a_heat_suspend_holds_while_the_die_temperature_is_unknown(
    writing_rig, fake_ltc4162
):
    fake_ltc4162.regs[0x39] = POE
    writing_rig.tick(hull_c=56.0)

    # telemetry_valid reads 0: no die temperature, so no evidence of cooling.
    status = writing_rig.tick(cycles=3, hull_c=30.0)

    assert status.thermal is ThermalLevel.SUSPENDED
    assert fake_ltc4162.regs[0x14] & SUSPEND


def test_an_unknown_hull_temperature_halves_the_limit(
    writing_rig, fake_ltc4162, fake_lgpio
):
    fake_ltc4162.regs[0x39] = POE
    fake_lgpio.levels.update({6: 0, 12: 0})

    status = writing_rig.tick(cycles=2, hull_c=None)

    assert status.thermal is ThermalLevel.HALVED
    assert fake_ltc4162.regs[0x15] == 20


def test_a_confirmed_chemistry_mismatch_suspends_and_stays_latched(
    writing_rig, fake_ltc4162, logs
):
    fake_ltc4162.regs[0x39] = POE | ltc4162.EN_CHG
    fake_ltc4162.regs[0x43] = 0x0006  # LAD, 6 cells
    writing_rig.tick(cycles=2)
    assert not fake_ltc4162.regs[0x14] & SUSPEND

    status = writing_rig.tick()
    assert status.targets.config_bits & SUSPEND
    assert status.thermal is ThermalLevel.NORMAL  # suspended for chemistry, not heat
    assert fake_ltc4162.regs[0x14] & SUSPEND
    assert any(level == "error" and "6 cells" in message for level, message in logs)

    # Suspended: en_chg drops and cell_count reads 0. That never releases it.
    fake_ltc4162.regs[0x39] = POE
    fake_ltc4162.regs[0x43] = 0x0000
    status = writing_rig.tick(cycles=5)
    assert status.targets.config_bits & SUSPEND
    assert fake_ltc4162.regs[0x14] & SUSPEND


def test_chemistry_is_not_judged_without_en_chg_or_on_one_odd_read(
    writing_rig, fake_ltc4162
):
    fake_ltc4162.regs[0x39] = POE
    fake_ltc4162.regs[0x43] = 0x0003
    writing_rig.tick(cycles=5)

    fake_ltc4162.regs[0x39] = POE | ltc4162.EN_CHG
    faults = []
    for chem_cells in (0x0006, 0x0006, 0x0005, 0x0006, 0x0006, 0x0000):
        fake_ltc4162.regs[0x43] = chem_cells
        faults.append(bool(writing_rig.tick().targets.config_bits & SUSPEND))

    assert faults == [False] * 6
    assert not fake_ltc4162.regs[0x14] & SUSPEND


def test_state_and_fault_changes_are_logged(rig, fake_ltc4162, logs):
    fake_ltc4162.regs[0x14] = FORCE
    fake_ltc4162.set_telemetry()
    fake_ltc4162.regs[0x34] = 64
    rig.tick(cycles=2)
    logs.clear()

    fake_ltc4162.regs[0x34] = ltc4162.BAT_MISSING_FAULT
    fake_ltc4162.regs[0x39] = POE | ltc4162.THERMAL_SHUTDOWN
    rig.tick(cycles=3)

    warnings = [message for level, message in logs if level == "warn"]
    assert sum("bat_missing_fault" in message for message in warnings) == 1
    assert sum("thermal_shutdown" in message for message in warnings) == 1
    assert any("PoE" in message for _, message in logs)


def test_run_cycles_once_per_second_off_the_event_loop(rov_state, monkeypatch):
    threads = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monitor = ChargerMonitor(rov_state, async_sleep=fake_sleep)
    monkeypatch.setattr(
        monitor, "cycle", lambda: threads.append(threading.current_thread())
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.run())

    assert len(threads) == 3
    assert all(thread is not threading.main_thread() for thread in threads)
    assert all(0.0 <= seconds <= 1.0 for seconds in sleeps)
