"""Mainboard values for the capability catalog and the field diagnostics.

The ``rov.*`` readings below join upstream's built-ins in
``custom_actions/builtins.py`` (sampled at the status rate); ``None`` means
unavailable. Every value comes from board-side state that its owner replaces
whole each cycle (``state.charger``, ``state.hull``, ``state.soc``,
``state.battery``, ``state.esc_power_telemetry``), so nothing here does I/O.
"""

import time
from typing import TYPE_CHECKING, Final

from .esc_power import esc_power_state
from .models.battery import BatteryLevel
from .models.charger import NOT_READ_YET
from .sensors import ltc4162
from .soc_monitor import THROTTLED_NOW_MASK, throttled_flags


if TYPE_CHECKING:
    from .rov_state import RovState


# Reading id (without "rov.") -> (name, value type, unit), like READING_DEFINITIONS.
BOARD_READING_DEFINITIONS: Final = {
    "batteryVoltage": ("Battery voltage", "number", "V"),
    # Charger and system current only: Q3 taps the pack ahead of R20, so the
    # thrusters are not in it. Positive while charging.
    "batteryCurrent": ("Battery current", "number", "A"),
    "chargerState": ("Charger state", "string", None),
    "poeType": ("PoE type", "string", None),
    "poeInputVoltage": ("PoE input voltage", "number", "V"),
    "poeInputCurrent": ("PoE input current", "number", "A"),
    "chargerTemperature": ("Charger temperature", "number", "°C"),
    "hullTemperature": ("Hull temperature", "number", "°C"),
    "hullHumidity": ("Hull humidity", "number", "%"),
    "hullPressure": ("Hull pressure", "number", "hPa"),
    "socTemperature": ("SoC temperature", "number", "°C"),
    # unavailable, off, ramping, on or stopping (esc_power.EscPowerState).
    "escPower": ("ESC power", "string", None),
}


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def board_readings(state: "RovState", now: float | None = None) -> dict[str, object]:
    """Current mainboard readings, keyed like ``BOARD_READING_DEFINITIONS``."""
    now = time.monotonic() if now is None else now
    charger = state.charger
    fresh = charger.is_fresh(now)
    telemetry = charger.fresh_telemetry(now)
    sht45 = state.hull.fresh_sht45(now)
    pads = state.hull.fresh_pads(now)
    return {
        "batteryVoltage": None if telemetry is None else round(telemetry.vbat_v, 2),
        "batteryCurrent": None if telemetry is None else round(telemetry.ibat_a, 3),
        "chargerState": (
            ltc4162.charger_state_name(charger.charger_state)
            if fresh and charger.charger_state is not None
            else None
        ),
        "poeType": (
            charger.poe_type.value if fresh and charger.poe_type is not None else None
        ),
        "poeInputVoltage": None if telemetry is None else round(telemetry.vin_v, 2),
        "poeInputCurrent": None if telemetry is None else round(telemetry.iin_a, 3),
        "chargerTemperature": (
            None if telemetry is None else round(telemetry.die_temp_c, 1)
        ),
        "hullTemperature": None if sht45 is None else round(sht45.temperature_c, 1),
        "hullHumidity": None if sht45 is None else round(sht45.humidity_pct, 1),
        "hullPressure": None if pads is None else round(pads.pressure_hpa, 2),
        "socTemperature": _rounded(state.soc.fresh_temperature(now), 1),
        "escPower": esc_power_state(state.esc_power_telemetry, now).value,
    }


def board_state(state: "RovState", now: float) -> dict[str, object]:
    """Categorical board state for the diagnostics "state" event (logged on change)."""
    charger = state.charger
    battery = state.battery
    return {
        "esc_power": esc_power_state(state.esc_power_telemetry, now).value,
        "charger_readable": charger.is_fresh(now),
        "charger_state": (
            None
            if charger.charger_state is None
            else ltc4162.charger_state_name(charger.charger_state)
        ),
        "charger_writes_enabled": charger.writes_enabled,
        "poe_type": None if charger.poe_type is None else charger.poe_type.value,
        "thermal": charger.thermal.name.lower(),
        "battery_level": battery.level.name.lower(),
        "battery_presence": battery.presence.value,
        "sht45_healthy": state.hull.sht45_healthy,
        "pads_healthy": state.hull.pads_healthy,
    }


def board_diagnostics(state: "RovState", now: float) -> dict[str, object]:
    """Power, hull, ESC power and SoC block of the 1 Hz diagnostics snapshot."""
    readings = board_readings(state, now)
    charger = state.charger
    battery = state.battery
    throttled = state.soc.throttled
    return {
        **readings,
        "telemetry_problem": charger.telemetry_problem,
        "poe_present": charger.poe_present if charger.is_fresh(now) else None,
        "poe_pins": charger.poe_pins,
        "battery_ema_v": _rounded(battery.fresh_voltage(now), 3),
        "battery_cell_v": _rounded(battery.cell_voltage_v, 3),
        "battery_evaluating": battery.evaluating,
        "esc_power_wire": state.esc_power_telemetry.wire_state,
        "throttled": None if throttled is None else f"{throttled:#x}",
        "throttled_flags": None if throttled is None else throttled_flags(throttled),
    }


def board_faults(state: "RovState", now: float) -> frozenset[str]:
    """Board conditions that freeze a diagnostics fault history."""
    faults: set[str] = set()
    charger = state.charger
    if not charger.is_fresh(now) and charger.telemetry_problem != NOT_READ_YET:
        faults.add("charger_unreadable")
    if state.battery.level >= BatteryLevel.CRITICAL:
        faults.add("battery_critical")
    throttled = state.soc.throttled
    if throttled is not None and throttled & THROTTLED_NOW_MASK:
        faults.add("soc_throttled")
    return frozenset(faults)
