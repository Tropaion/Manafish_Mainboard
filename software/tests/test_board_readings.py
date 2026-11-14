"""Mainboard rov.* readings, the ESC power actions and their catalog entries (8.5)."""

import asyncio
import time
from typing import cast

import pytest

from rov_firmware import pico_protocol as wire
from rov_firmware.board_readings import (
    BOARD_READING_DEFINITIONS,
    board_diagnostics,
    board_faults,
    board_readings,
    board_state,
)
from rov_firmware.custom_actions import builtins
from rov_firmware.custom_actions.builtins import READING_DEFINITIONS, values
from rov_firmware.custom_actions.runtime import CustomActionRuntime
from rov_firmware.esc_power import EscPowerController
from rov_firmware.models.battery import BatteryLevel, BatteryStatus
from rov_firmware.models.charger import (
    NOT_READ_YET,
    ChargerStatus,
    ChargerTelemetry,
    PoeType,
)
from rov_firmware.models.hull import HullData, PadsReading, Sht45Reading
from rov_firmware.models.soc import SocStatus
from rov_firmware.sensors import ltc4162


def fill_board_state(state, now):
    state.charger = ChargerStatus(
        readable=True,
        updated_at=now,
        telemetry=ChargerTelemetry(
            vbat_cell_v=3.81234,
            vbat_v=19.0617,
            vin_v=24.1234,
            vout_v=19.1,
            ibat_a=0.51234,
            iin_a=0.61234,
            die_temp_c=41.234,
            charger_state=64,
            charge_status=2,
            system_status=ltc4162.VIN_GT_4P2V,
            chem=0,
            cell_count=5,
            telemetry_valid=True,
        ),
        telemetry_problem=None,
        poe_present=True,
        charger_state=64,
        poe_type=PoeType.TYPE_3,
        poe_pins=(1, 0),
    )
    state.hull = HullData(
        sht45=Sht45Reading(temperature_c=28.26, humidity_pct=41.04, sample_time=now),
        pads=PadsReading(pressure_hpa=1013.254, temperature_c=28.0, sample_time=now),
        sht45_healthy=True,
        pads_healthy=True,
    )
    state.soc = SocStatus(updated_at=now, temperature_c=55.55, throttled=0x50005)
    state.battery = BatteryStatus(
        updated_at=now, voltage_ema_v=19.05, cell_voltage_v=3.81, evaluating=True
    )
    telemetry = state.esc_power_telemetry
    telemetry.wire_state = wire.ESC_POWER_STARTING
    telemetry.reported_at = now
    telemetry.changed_at = now


def test_every_values_key_is_a_declared_reading(rov_state):
    """``publish`` raises for an undeclared key, so the two sets must match."""
    assert set(values(rov_state)) == {f"rov.{key}" for key in READING_DEFINITIONS}
    fill_board_state(rov_state, time.monotonic())
    assert set(values(rov_state)) == {f"rov.{key}" for key in READING_DEFINITIONS}


def test_board_readings_cover_their_definitions(rov_state):
    assert set(board_readings(rov_state)) == set(BOARD_READING_DEFINITIONS)
    assert set(BOARD_READING_DEFINITIONS) <= set(READING_DEFINITIONS)


def test_board_readings_from_fresh_state(rov_state):
    now = time.monotonic()
    fill_board_state(rov_state, now)
    assert board_readings(rov_state, now) == {
        "batteryVoltage": 19.06,
        "batteryCurrent": 0.512,
        "chargerState": "cc_cv_charge",
        "poeType": "type3",
        "poeInputVoltage": 24.12,
        "poeInputCurrent": 0.612,
        "chargerTemperature": 41.2,
        "hullTemperature": 28.3,
        "hullHumidity": 41.0,
        "hullPressure": 1013.25,
        "socTemperature": 55.5,
        "escPower": "ramping",
    }


def test_stale_or_missing_sources_read_unavailable(rov_state):
    now = time.monotonic()
    fill_board_state(rov_state, now - 10)
    readings = board_readings(rov_state, now)
    assert readings.pop("escPower") == "unavailable"
    assert set(readings.values()) == {None}


def test_charger_state_shows_without_valid_telemetry(rov_state):
    now = time.monotonic()
    rov_state.charger = ChargerStatus(
        readable=True,
        updated_at=now,
        telemetry_problem="vbat reads 0 (no pack, or no conversion yet)",
        poe_present=True,
        charger_state=ltc4162.BAT_MISSING_FAULT,
        vbat_zero=True,
        poe_type=PoeType.TYPE_1,
    )
    readings = board_readings(rov_state, now)
    assert readings["chargerState"] == "bat_missing_fault"
    assert readings["poeType"] == "type1"
    assert readings["batteryVoltage"] is None


def test_readings_publish_through_the_runtime(rov_state, tmp_path):
    fill_board_state(rov_state, time.monotonic())
    runtime = CustomActionRuntime(rov_state, tmp_path / "custom_actions")
    runtime.refresh_builtins()
    assert runtime.samples["rov.hullPressure"].value == 1013.25
    assert runtime.samples["rov.escPower"].value == "ramping"
    assert runtime.samples["rov.poeType"].value == "type3"


@pytest.mark.parametrize("identifier", ["rov.escPower.on", "rov.escPower.off"])
def test_esc_power_actions_are_no_input_once_actions(identifier):
    action = {action.id: action for action in builtins.actions()}[identifier]
    assert action.input_type == "none"
    assert action.modes == ["once"]
    assert action.mode == "once"


class FakeController:
    def __init__(self):
        self.calls: list[str] = []

    async def request_on(self):
        self.calls.append("on")
        return False

    async def request_off(self, *, notify=True):
        self.calls.append(f"off notify={notify}")
        return True


def test_esc_power_actions_act_on_press_only(rov_state, tmp_path):
    controller = FakeController()
    rov_state.esc_power = cast(EscPowerController, controller)
    runtime = CustomActionRuntime(rov_state, tmp_path / "custom_actions")

    async def scenario():
        await runtime.invoke("rov.escPower.on", "press")
        await runtime.invoke("rov.escPower.on", "release")
        await runtime.invoke("rov.escPower.off", "press")
        await runtime.invoke("rov.escPower.off", "stop")

    asyncio.run(scenario())
    # A refused on is toasted by the controller, not raised to the app.
    assert controller.calls == ["on", "off notify=True"]


def test_esc_power_actions_without_a_controller_fail_clearly(rov_state, tmp_path):
    runtime = CustomActionRuntime(rov_state, tmp_path / "custom_actions")
    with pytest.raises(ValueError, match="ESC power control is unavailable"):
        asyncio.run(runtime.invoke("rov.escPower.on"))


def test_board_diagnostics_block(rov_state):
    now = time.monotonic()
    fill_board_state(rov_state, now)
    block = board_diagnostics(rov_state, now)
    assert block["batteryVoltage"] == 19.06
    assert block["battery_ema_v"] == 19.05
    assert block["poe_present"] is True
    assert block["poe_pins"] == (1, 0)
    assert block["esc_power_wire"] == wire.ESC_POWER_STARTING
    # The snapshot's top level carries the latched undervoltage already.
    assert "pi_undervoltage" not in block
    assert block["throttled"] == "0x50005"
    assert block["throttled_flags"] == [
        "undervoltage",
        "throttled",
        "undervoltage_occurred",
        "throttled_occurred",
    ]
    state = board_state(rov_state, now)
    assert state["esc_power"] == "ramping"
    assert state["charger_state"] == "cc_cv_charge"
    assert state["battery_level"] == "normal"


def test_board_faults(rov_state):
    now = time.monotonic()
    # Before the first charger cycle nothing is a fault.
    assert rov_state.charger.telemetry_problem == NOT_READ_YET
    assert board_faults(rov_state, now) == frozenset()
    rov_state.charger = ChargerStatus(updated_at=now, telemetry_problem="I2C error")
    rov_state.battery = BatteryStatus(updated_at=now, level=BatteryLevel.CRITICAL)
    rov_state.soc = SocStatus(updated_at=now, throttled=0x4)
    assert board_faults(rov_state, now) == {
        "charger_unreadable",
        "battery_critical",
        "soc_throttled",
    }
    rov_state.soc = SocStatus(updated_at=now, throttled=0x50000)
    assert "soc_throttled" not in board_faults(rov_state, now)
