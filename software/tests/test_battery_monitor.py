"""LowBatteryMonitor: EMA, low-battery levels, shutdown chain and ESC rules (8.2)."""

import asyncio
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest

from rov_firmware import battery as battery_module, pico_protocol as wire
from rov_firmware.battery import (
    BATTERY_PRESENT_HOLD_S,
    CHARGER_UNREADABLE,
    CRITICAL_TOAST_INTERVAL_S,
    DETECTION_FAILED,
    INVALID_WARN_S,
    RECOVERY_BLOCKS_POWER_OFF,
    SHUTDOWN_RETRY_S,
    LowBatteryMonitor,
    motor_commands_neutral,
)
from rov_firmware.esc_power import EscPowerController, EscPowerState
from rov_firmware.models.battery import BatteryLevel, BatteryPresence
from rov_firmware.models.charger import ChargerStatus, ChargerTelemetry, PoeType
from rov_firmware.models.toast import ToastVariant
from rov_firmware.pico_control import PicoControl
from rov_firmware.sensors import ltc4162
from rov_firmware.websocket.receive.mcu import flash_mcu_firmware


CHARGING = 64  # cc_cv_charge
SUSPENDED = ltc4162.CHARGER_SUSPENDED


@pytest.fixture(autouse=True)
def logs(monkeypatch):
    captured = []
    for level in ("info", "warn", "error"):
        monkeypatch.setattr(
            battery_module,
            f"log_{level}",
            lambda message, level=level: captured.append((level, message)),
        )
    return captured


def make_charger(  # noqa: PLR0913 - one keyword per charger fact
    now,
    cell_v=3.8,
    *,
    poe=False,
    valid=True,
    charger_state=None,
    vbat_zero=False,
    problem="telemetry_valid reads 0 (warming up or telemetry off)",
):
    state_word = (
        (CHARGING if poe else SUSPENDED) if charger_state is None else charger_state
    )
    telemetry = ChargerTelemetry(
        vbat_cell_v=cell_v,
        vbat_v=cell_v * 5,
        vin_v=24.0 if poe else 0.0,
        vout_v=cell_v * 5,
        ibat_a=0.5 if poe else -0.3,
        iin_a=0.6 if poe else 0.0,
        die_temp_c=35.0,
        charger_state=state_word,
        charge_status=0,
        system_status=ltc4162.VIN_GT_4P2V if poe else 0,
        chem=0,
        cell_count=5 if poe else 0,
        telemetry_valid=True,
    )
    return ChargerStatus(
        readable=True,
        updated_at=now,
        telemetry=telemetry if valid else None,
        telemetry_problem=None if valid else problem,
        poe_present=poe,
        charger_state=state_word,
        vbat_zero=vbat_zero,
        poe_type=PoeType.TYPE_3 if poe else PoeType.NONE,
    )


class FakeEscPower:
    def __init__(self, state):
        self.state = state
        self.calls: list[str] = []
        self.off_result = True
        self.off_wait_result = True

    async def request_off(self, *, notify=True):
        self.calls.append(f"off notify={notify}")
        return self.off_result

    async def wait_until_off(self, timeout_s=5.0):
        self.calls.append(f"wait {timeout_s:.0f} s" if timeout_s != 5.0 else "wait")
        return self.off_wait_result


class Rig:
    """A monitor on a fake clock; ``tick`` sets the charger and runs one cycle."""

    def __init__(self, state, monkeypatch):
        self.state = state
        self.now = 1000.0
        self.wall = 50_000.0
        self.toasts: list[tuple[ToastVariant, str, str | None]] = []
        self.power_offs = 0
        self.power_off_error: Exception | None = None
        self.esc = FakeEscPower(state)
        state.esc_power = cast(EscPowerController, self.esc)
        self.monitor = LowBatteryMonitor(
            state,
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
            notify=lambda variant, message, description: self.toasts.append(
                (variant, message, description)
            ),
            power_off=self.power_off,
        )
        # MCU reports ESC power on unless a test changes it.
        self.report(wire.ESC_POWER_ON)
        self.monkeypatch = monkeypatch

    async def power_off(self):
        self.power_offs += 1
        if self.power_off_error is not None:
            raise self.power_off_error

    def report(self, wire_state):
        telemetry = self.state.esc_power_telemetry
        telemetry.wire_state = wire_state
        telemetry.reported_at = self.now
        telemetry.changed_at = self.now - 60

    def tick(self, seconds=1, **charger):
        for _ in range(int(seconds)):
            self.state.charger = make_charger(self.now, **charger)
            self.state.esc_power_telemetry.reported_at = self.now
            asyncio.run(self.monitor.cycle())
            self.now += 1.0
            self.wall += 1.0
        return self.state.battery

    def messages(self):
        return [message for _, message, _ in self.toasts]


@pytest.fixture
def rig(rov_state, monkeypatch):
    return Rig(rov_state, monkeypatch)


# Motor commands.


def test_motor_commands_are_neutral_without_live_input(rov_state):
    rov_state.thrusters.direction_vector = np.ones(8, dtype=np.float32) * 0.5
    rov_state.thrusters.last_direction_time = 100.0
    # Input older than 0.2 s: pico_control sends input_valid = 0, the MCU neutralises.
    assert motor_commands_neutral(rov_state, wall_now=101.0, power=EscPowerState.ON)
    assert not motor_commands_neutral(rov_state, wall_now=100.1, power=EscPowerState.ON)


def test_stabilization_depth_hold_tests_and_tuning_drive_the_motors(rov_state):
    rov_state.thrusters.direction_vector = np.zeros(8, dtype=np.float32)
    rov_state.thrusters.last_direction_time = 100.0
    live = {"wall_now": 100.05, "power": EscPowerState.ON}
    assert motor_commands_neutral(rov_state, **live)
    rov_state.system_status.depth_hold = True
    assert not motor_commands_neutral(rov_state, **live)
    rov_state.system_status.depth_hold = False
    rov_state.system_status.auto_stabilization = True
    assert not motor_commands_neutral(rov_state, **live)
    rov_state.system_status.auto_stabilization = False
    rov_state.thrusters.test_thruster = 3
    assert not motor_commands_neutral(rov_state, wall_now=999.0, power=EscPowerState.ON)
    rov_state.thrusters.test_thruster = None
    rov_state.regulator.auto_tuning_active = True
    assert not motor_commands_neutral(rov_state, wall_now=999.0, power=EscPowerState.ON)


def test_esc_power_off_means_no_thruster_current(rov_state):
    rov_state.thrusters.test_thruster = 1
    assert motor_commands_neutral(rov_state, wall_now=0.0, power=EscPowerState.OFF)
    assert not motor_commands_neutral(
        rov_state, wall_now=0.0, power=EscPowerState.UNAVAILABLE
    )


# EMA and status.


def test_ema_follows_vbat_with_a_10_s_time_constant(rig):
    rig.tick(cell_v=4.0)
    assert rig.state.battery.voltage_ema_v == pytest.approx(20.0)
    rig.tick(10, cell_v=3.6)
    # After 10 s at 18 V the EMA has moved 1 - 1/e of the step.
    assert rig.state.battery.voltage_ema_v == pytest.approx(18.0 + 2.0 / np.e, abs=0.01)
    assert rig.state.battery.fresh_voltage(rig.now) is not None


def test_ema_restarts_after_invalid_telemetry(rig):
    rig.tick(3, cell_v=4.0)
    rig.tick(valid=False)
    assert rig.state.battery.voltage_ema_v is None
    rig.tick(cell_v=3.6)
    assert rig.state.battery.voltage_ema_v == pytest.approx(18.0)


def test_a_repeated_charger_sample_does_not_move_the_ema(rig):
    rig.tick(cell_v=4.0)
    rig.state.charger = make_charger(rig.now - 1, cell_v=3.0)
    asyncio.run(rig.monitor.cycle())
    # Same updated_at as the first sample would be; here it is older: no step.
    assert rig.state.battery.voltage_ema_v == pytest.approx(20.0)


# Levels.


def test_warn_after_10_s_of_neutral_motors_below_3_4(rig):
    rig.tick(2, cell_v=3.3)  # 2 s settle
    rig.tick(10, cell_v=3.3)
    assert rig.state.battery.level == BatteryLevel.NORMAL
    rig.tick(cell_v=3.3)
    assert rig.state.battery.level == BatteryLevel.WARN
    assert rig.messages() == ["Battery low"]


def test_vbat_under_thrust_never_counts(rig):
    rig.state.thrusters.direction_vector = np.full(8, 0.8, dtype=np.float32)
    for _ in range(60):
        rig.state.thrusters.last_direction_time = rig.wall
        rig.tick(cell_v=3.1)
    assert rig.state.battery.level == BatteryLevel.NORMAL
    assert not rig.state.battery.evaluating
    assert rig.esc.calls == []


def test_thrust_keeps_the_level_and_restarts_the_window(rig):
    rig.tick(15, cell_v=3.3)
    assert rig.state.battery.level == BatteryLevel.WARN
    rig.state.thrusters.direction_vector = np.full(8, 0.5, dtype=np.float32)
    rig.state.thrusters.last_direction_time = rig.wall
    rig.tick(cell_v=3.9)
    assert rig.state.battery.level == BatteryLevel.WARN
    # 9 s neutral below 3.2 V after the input expired: not critical yet.
    rig.tick(2 + 9, cell_v=3.1)
    assert rig.state.battery.level == BatteryLevel.WARN
    rig.tick(2, cell_v=3.1)
    assert rig.state.battery.level == BatteryLevel.CRITICAL


def test_critical_toasts_every_60_s_blocks_enables_and_keeps_esc_power_on(rig):
    rig.tick(13, cell_v=3.1)
    assert rig.state.battery.level == BatteryLevel.CRITICAL
    assert rig.messages().count("Battery critical") == 1
    assert rig.monitor.critical_blocker() is not None
    rig.tick(int(CRITICAL_TOAST_INTERVAL_S) - 1, cell_v=3.1)
    assert rig.messages().count("Battery critical") == 1
    rig.tick(cell_v=3.1)
    assert rig.messages().count("Battery critical") == 2
    assert rig.esc.calls == []


def test_levels_recover_only_with_hysteresis(rig):
    rig.tick(13, cell_v=3.1)
    assert rig.state.battery.level == BatteryLevel.CRITICAL
    # 3.25 V/cell is above 3.2 but below 3.2 + 0.1: still critical.
    rig.tick(30, cell_v=3.25)
    assert rig.state.battery.level == BatteryLevel.CRITICAL
    rig.tick(11, cell_v=3.35)
    assert rig.state.battery.level == BatteryLevel.WARN
    assert "Battery no longer critical" in rig.messages()
    assert rig.monitor.critical_blocker() is None
    rig.tick(30, cell_v=3.45)
    assert rig.state.battery.level == BatteryLevel.WARN
    rig.tick(11, cell_v=3.55)
    assert rig.state.battery.level == BatteryLevel.NORMAL


# Shutdown.


def test_shutdown_chain_runs_neutral_esc_off_wait_then_power_off(rig):
    rig.state.thrusters.direction_vector = np.zeros(8, dtype=np.float32)
    # 2 s settle, then the first counted sample and 29 s more.
    rig.tick(2 + 1 + 29, cell_v=2.9)
    assert rig.power_offs == 0
    rig.tick(cell_v=2.9)
    assert rig.state.battery.level == BatteryLevel.SHUTDOWN
    assert rig.esc.calls == ["off notify=False", "wait"]
    assert rig.state.thrusters.direction_vector is None
    assert rig.power_offs == 1
    assert "Battery empty: powering off" in rig.messages()
    # Not repeated every second while systemd shuts down.
    rig.tick(5, cell_v=2.9)
    assert rig.power_offs == 1


def test_esc_off_failure_does_not_stop_the_power_off(rig, logs):
    rig.esc.off_result = False
    rig.tick(33, cell_v=2.9)
    assert rig.power_offs == 1
    assert any("anyway" in message for _, message in logs)
    # A later off (operator, flash prepare) may have replaced this one: the chain
    # still waits for ESC power to read off before the halt.
    assert rig.esc.calls == ["off notify=False", "wait"]


def test_no_shutdown_on_poe(rig):
    rig.tick(120, cell_v=2.9, poe=True)
    assert rig.power_offs == 0
    assert rig.state.battery.level == BatteryLevel.CRITICAL


def test_no_shutdown_from_invalid_telemetry(rig):
    rig.tick(120, valid=False)
    assert rig.power_offs == 0
    assert rig.state.battery.level == BatteryLevel.NORMAL
    assert rig.messages() == ["Battery voltage unknown"]


def test_invalid_telemetry_warns_once_after_the_start_up_settle(rig, logs):
    rig.tick(int(INVALID_WARN_S) - 1, valid=False)
    assert rig.toasts == []
    rig.tick(30, valid=False)
    assert rig.messages() == ["Battery voltage unknown"]
    rig.tick(cell_v=3.8)
    assert ("info", "Battery voltage measured again") in logs


def test_invalid_telemetry_restarts_the_shutdown_window(rig):
    rig.tick(2 + 20, cell_v=2.9)
    rig.tick(valid=False)
    rig.tick(2 + 20, cell_v=2.9)
    assert rig.power_offs == 0


def test_shutdown_disabled_by_the_owner_switch_only_warns(rig, monkeypatch):
    monkeypatch.setattr(battery_module, "AUTO_POWER_OFF_ENABLED", False)
    rig.tick(120, cell_v=2.9)
    assert rig.power_offs == 0
    assert rig.esc.calls == []
    assert rig.state.battery.level == BatteryLevel.SHUTDOWN
    assert rig.messages().count("Battery critical") == 2


def test_the_shutdown_chain_holds_the_flash_lock_until_the_power_off(rig):
    seen = []

    async def request_off(*, notify=True):
        seen.append(("off", notify, rig.state.mcu_flash_lock.locked()))
        return True

    async def wait_until_off(timeout_s=5.0):
        # An MCU flash pressed while ESC power stops is refused, not started.
        flashed = await flash_mcu_firmware(
            rig.state, rig.state.rov_config.mcu_board, show_toasts=False
        )
        seen.append(("flash", flashed, timeout_s))
        return True

    async def power_off():
        seen.append(("power-off", None, rig.state.mcu_flash_lock.locked()))

    rig.esc.request_off = request_off
    rig.esc.wait_until_off = wait_until_off
    rig.monitor._power_off = power_off
    rig.tick(33, cell_v=2.9)
    assert seen == [
        ("off", False, True),
        ("flash", False, 5.0),
        ("power-off", None, True),
    ]
    assert not rig.state.mcu_flash_lock.locked()


def test_no_power_off_while_the_esc_recovery_journal_is_set(rig, logs):
    rig.state.esc_firmware_recovery_required = True
    rig.state.thrusters.direction_vector = np.zeros(8, dtype=np.float32)
    rig.tick(33, cell_v=2.9)
    assert rig.state.battery.level == BatteryLevel.SHUTDOWN
    assert rig.power_offs == 0
    # No ESC power change (the MCU holds it on for the recovery flash), and the
    # pilot input stays as it is.
    assert rig.esc.calls == []
    assert rig.state.thrusters.direction_vector is not None
    empty = [toast for toast in rig.toasts if toast[1] == "Battery empty"]
    assert len(empty) == 1
    assert empty[0][0] == ToastVariant.ERROR
    assert (empty[0][2] or "").endswith(RECOVERY_BLOCKS_POWER_OFF)
    # The toast repeats every 60 s; the log line only once.
    rig.tick(int(SHUTDOWN_RETRY_S) - 1, cell_v=2.9)
    assert rig.messages().count("Battery empty") == 1
    rig.tick(cell_v=2.9)
    assert rig.messages().count("Battery empty") == 2
    assert sum(RECOVERY_BLOCKS_POWER_OFF in message for _, message in logs) == 1
    assert rig.power_offs == 0
    # The recovery succeeded: the chain runs on the next cycle.
    rig.state.esc_firmware_recovery_required = False
    rig.tick(cell_v=2.9)
    assert rig.power_offs == 1
    assert rig.esc.calls == ["off notify=False", "wait"]


def test_shutdown_waits_for_a_running_firmware_update(rig):
    async def hold_lock_for_cycles():
        async with rig.state.mcu_flash_lock:
            for _ in range(40):
                rig.state.charger = make_charger(rig.now, cell_v=2.9)
                await rig.monitor.cycle()
                rig.now += 1.0
                rig.wall += 1.0

    asyncio.run(hold_lock_for_cycles())
    assert rig.power_offs == 0
    rig.tick(cell_v=2.9)
    assert rig.power_offs == 1


def test_poe_arriving_during_the_chain_cancels_the_power_off(rig):
    async def plug_poe(*, notify=True):
        assert not notify
        rig.state.charger = make_charger(rig.now, cell_v=2.9, poe=True)
        return True

    rig.esc.request_off = plug_poe
    rig.tick(33, cell_v=2.9)
    assert rig.power_offs == 0
    assert not rig.state.mcu_flash_lock.locked()


def test_a_failed_power_off_is_toasted_and_retried_later(rig):
    rig.power_off_error = OSError("polkit refused")
    rig.tick(33, cell_v=2.9)
    assert rig.power_offs == 1
    assert "Power-off failed" in rig.messages()
    assert not rig.state.mcu_flash_lock.locked()
    rig.tick(59, cell_v=2.9)
    assert rig.power_offs == 1
    rig.tick(cell_v=2.9)
    assert rig.power_offs == 2


def test_systemctl_poweroff_runs_systemctl(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create(*args, **_kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(battery_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(battery_module.asyncio, "create_subprocess_exec", create)
    asyncio.run(battery_module.systemctl_poweroff())
    assert calls == [("/bin/systemctl", "poweroff")]


def test_systemctl_poweroff_reports_failures(monkeypatch):
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"Access denied"

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(battery_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(battery_module.asyncio, "create_subprocess_exec", create)
    with pytest.raises(OSError, match="Access denied"):
        asyncio.run(battery_module.systemctl_poweroff())
    monkeypatch.setattr(battery_module.shutil, "which", lambda _name: None)
    with pytest.raises(OSError, match="PATH"):
        asyncio.run(battery_module.systemctl_poweroff())


# Battery presence and the pulled pack on PoE.


def test_without_poe_the_pack_is_present_and_counts_from_the_first_read(rig):
    battery = rig.tick(cell_v=3.8)
    assert battery.presence == BatteryPresence.PRESENT
    assert rig.monitor.presence_blocker() is not None
    # The checks run one second after each cycle: 4 s present, then 5 s.
    rig.tick(int(BATTERY_PRESENT_HOLD_S) - 2, cell_v=3.8)
    assert "4 s ago" in (rig.monitor.presence_blocker() or "")
    rig.tick(cell_v=3.8)
    assert rig.monitor.presence_blocker() is None


def test_on_poe_without_a_pack_esc_power_is_never_enabled(rig):
    rig.tick(30, poe=True, valid=False, vbat_zero=True, problem="vbat reads 0")
    assert rig.state.battery.presence == BatteryPresence.ABSENT
    assert "No battery" in (rig.monitor.pack_blocker() or "")
    assert "No battery" in (rig.monitor.presence_blocker() or "")
    assert rig.monitor.charger_blocker() == f"{CHARGER_UNREADABLE}: vbat reads 0."
    # The operator sees the missing pack, not the invalid telemetry it causes.
    assert rig.monitor.enable_blockers()[0] == rig.monitor.pack_blocker


def test_a_pulled_pack_on_poe_switches_esc_power_off_once(rig):
    rig.tick(10, poe=True, cell_v=4.0)
    assert rig.esc.calls == []
    rig.tick(poe=True, charger_state=ltc4162.BAT_MISSING_FAULT, cell_v=4.0)
    assert rig.esc.calls == ["off notify=False"]
    assert rig.messages() == ["ESC power switched off"]
    # Retried while the MCU still reports on, without toasting again.
    rig.tick(9, poe=True, charger_state=ltc4162.BAT_MISSING_FAULT, cell_v=4.0)
    assert rig.esc.calls == ["off notify=False"]
    rig.tick(poe=True, charger_state=ltc4162.BAT_MISSING_FAULT, cell_v=4.0)
    assert rig.esc.calls == ["off notify=False"] * 2
    assert rig.messages() == ["ESC power switched off"]


def test_low_vbat_under_thrust_on_poe_is_sag_not_a_pulled_pack(rig):
    rig.tick(10, poe=True, cell_v=4.0)
    rig.state.thrusters.direction_vector = np.full(8, 0.8, dtype=np.float32)
    for _ in range(5):
        rig.state.thrusters.last_direction_time = rig.wall
        rig.tick(poe=True, cell_v=2.3)  # 11.5 V at BAT_IN under heavy thrust
    assert rig.state.battery.presence == BatteryPresence.ABSENT
    assert rig.esc.calls == []
    assert rig.toasts == []
    # The pilot lets go: the same vbat after 2 s of neutral motors is a pulled pack.
    rig.state.thrusters.direction_vector = None
    rig.tick(2, poe=True, cell_v=2.3)
    assert rig.esc.calls == []
    rig.tick(poe=True, cell_v=2.3)
    assert rig.esc.calls == ["off notify=False"]
    assert rig.messages() == ["ESC power switched off"]


def test_bat_missing_fault_under_thrust_switches_esc_power_off_at_once(rig):
    rig.tick(10, poe=True, cell_v=4.0)
    rig.state.thrusters.direction_vector = np.full(8, 0.8, dtype=np.float32)
    rig.state.thrusters.last_direction_time = rig.wall
    rig.tick(poe=True, charger_state=ltc4162.BAT_MISSING_FAULT, cell_v=4.0)
    assert rig.esc.calls == ["off notify=False"]


def test_failed_battery_detection_with_the_pack_fitted_keeps_esc_power_on(rig):
    rig.tick(10, poe=True, cell_v=3.8)
    # Input limit or UVCL: no detection current, retried every 30 s (DS p.23, 38).
    rig.tick(30, poe=True, cell_v=3.8, charger_state=ltc4162.BAT_DETECT_FAILED_FAULT)
    assert rig.state.battery.presence == BatteryPresence.UNKNOWN
    assert rig.esc.calls == []
    assert rig.toasts == []
    assert rig.monitor.pack_blocker() is None
    assert rig.monitor.presence_blocker() == DETECTION_FAILED


def test_battery_detection_after_poe_arrives_is_not_a_pulled_pack(rig):
    rig.tick(10, cell_v=3.9)
    rig.tick(4, poe=True, charger_state=ltc4162.BATTERY_DETECTION, cell_v=3.9)
    assert rig.state.battery.presence == BatteryPresence.UNKNOWN
    assert rig.esc.calls == []
    # Detection restarts the 5 s battery-present count.
    rig.tick(poe=True, cell_v=3.9)
    assert rig.monitor.presence_blocker() is not None


def test_an_unreadable_charger_never_switches_esc_power_off(rig):
    rig.tick(5, poe=True, cell_v=4.0)
    rig.state.charger = ChargerStatus(updated_at=rig.now, telemetry_problem="I2C error")
    asyncio.run(rig.monitor.cycle())
    assert rig.state.battery.presence == BatteryPresence.UNKNOWN
    assert rig.esc.calls == []


def test_no_auto_off_while_esc_power_reads_off(rig):
    rig.report(wire.ESC_POWER_OFF_READY)
    rig.tick(5, poe=True, charger_state=ltc4162.BAT_MISSING_FAULT)
    assert rig.esc.calls == []


# ESC enable checks.


def test_charger_blocker_explains_why(rig):
    assert rig.monitor.charger_blocker() == (
        f"{CHARGER_UNREADABLE}: no LTC4162 read in the last 3 s."
    )
    rig.state.charger = make_charger(rig.now, valid=False)
    assert rig.monitor.charger_blocker() == (
        f"{CHARGER_UNREADABLE}: telemetry_valid reads 0 (warming up or telemetry off)."
    )
    rig.state.charger = make_charger(rig.now)
    assert rig.monitor.charger_blocker() is None
    rig.now += 5
    assert rig.monitor.charger_blocker() is not None


def test_a_stale_battery_status_blocks(rig):
    rig.tick(10, cell_v=3.8)
    assert rig.monitor.presence_blocker() is None
    rig.now += 10
    rig.state.charger = make_charger(rig.now)
    assert "battery monitor" in (rig.monitor.presence_blocker() or "")


def _controller(rig):
    class Pico:
        session_active = True
        esc_power_supported = True
        requests: list[bool] = []  # noqa: RUF012

        async def request_esc_power(self, *, on, before_send=None):
            if before_send is not None:
                before_send()
            self.requests.append(on)
            return wire.APPLIED

    pico = Pico()
    rig.state.pico = cast(PicoControl, pico)
    toasts = []
    controller = EscPowerController(
        rig.state,
        clock=lambda: rig.now,
        notify=lambda variant, message, description: toasts.append(
            (variant, message, description)
        ),
        blockers=rig.monitor.enable_blockers(),
    )
    rig.state.esc_power = controller
    return SimpleNamespace(controller=controller, pico=pico, toasts=toasts)


def test_esc_power_on_is_refused_while_the_charger_is_unreadable(rig):
    rig.report(wire.ESC_POWER_OFF_READY)
    esc = _controller(rig)
    assert not asyncio.run(esc.controller.request_on())
    assert esc.pico.requests == []
    assert esc.toasts == [
        (
            ToastVariant.ERROR,
            "ESC power stays off",
            f"{CHARGER_UNREADABLE}: no LTC4162 read in the last 3 s.",
        )
    ]


def test_esc_power_on_passes_once_the_pack_is_known_and_not_critical(rig):
    rig.report(wire.ESC_POWER_OFF_READY)
    esc = _controller(rig)
    rig.tick(6, cell_v=3.8)
    rig.state.charger = make_charger(rig.now, cell_v=3.8)
    rig.report(wire.ESC_POWER_OFF_READY)
    assert asyncio.run(esc.controller.request_on())
    assert esc.pico.requests == [True]


def test_esc_power_on_without_a_pack_on_poe_names_the_pack(rig):
    rig.report(wire.ESC_POWER_OFF_READY)
    esc = _controller(rig)
    rig.tick(10, poe=True, valid=False, vbat_zero=True, problem="vbat reads 0")
    rig.state.charger = make_charger(
        rig.now, poe=True, valid=False, vbat_zero=True, problem="vbat reads 0"
    )
    rig.report(wire.ESC_POWER_OFF_READY)
    assert not asyncio.run(esc.controller.request_on())
    assert esc.pico.requests == []
    assert (esc.toasts[-1][2] or "").startswith("No battery on the charger")


def test_esc_power_on_is_refused_while_critical(rig):
    rig.report(wire.ESC_POWER_OFF_READY)
    esc = _controller(rig)
    rig.tick(13, cell_v=3.1)
    rig.state.charger = make_charger(rig.now, cell_v=3.1)
    rig.report(wire.ESC_POWER_OFF_READY)
    assert not asyncio.run(esc.controller.request_on())
    assert "critical" in (esc.toasts[-1][2] or "")


def test_run_survives_a_failing_cycle(rov_state, monkeypatch, logs):
    monitor = LowBatteryMonitor(rov_state)
    sleeps = []

    async def fail():
        msg = "boom"
        raise RuntimeError(msg)

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "cycle", fail)
    monitor._async_sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.run())
    assert ("error", "Battery monitor cycle failed: boom") in logs
    assert len(sleeps) == 2
