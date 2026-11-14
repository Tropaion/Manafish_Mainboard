"""Charger, PoE and battery policy for the mainboard: pure functions, no I/O.

``ChargerMonitor`` feeds it the LTC4162 status, the PoE type pins (CM5 GPIO6 =
TYP2, GPIO12 = TYP3) and the SHT45 hull temperature; the low-battery logic reads
its thresholds. Register facts are in ``sensors/ltc4162.py``; the Ag59800 truth
table is datasheet V2.0 Table 5.
"""

from typing import Final

from .models.battery import BatteryPresence
from .models.charger import ChargerSettings, ChargerStatus, PoeType, ThermalLevel
from .sensors import ltc4162


# Pack limits and battery behaviour confirmed by the owner on 2026-11-14 (wiki
# 8.2): vcharge code 31 = 4.20 V/cell (21.0 V for 5S; 23 = 4.10); charge code
# 19 = 2.0 A (0.5 C at 4000 mAh); 3.4 / 3.2 / 3.0 V/cell with automatic power-off;
# critical never cuts thrusters. The hysteresis and the presence threshold below
# are Phase 4 choices.
CHARGE_VOLTAGE_CODE: Final = 31
CHARGE_CURRENT_CODE: Final = 19
BATTERY_WARN_V_PER_CELL: Final = 3.4
BATTERY_CRITICAL_V_PER_CELL: Final = 3.2
BATTERY_SHUTDOWN_V_PER_CELL: Final = 3.0
# Power off at the shutdown threshold (no PoE, motors neutral, 30 s).
AUTO_POWER_OFF_ENABLED: Final = True
# A warn or critical level is left only once vbat is this far above its
# threshold for the recovery window, so the levels do not flap at a boundary.
BATTERY_RECOVERY_HYSTERESIS_V_PER_CELL: Final = 0.1
# With PoE, a pack counts as present only above this pack voltage (2.4 V/cell);
# without a pack the charger output may still read a voltage.
BATTERY_PRESENT_MIN_V: Final = 12.0

# iin_limit_target code per PSE type at RSNSI 8 mOhm (62.5 mA per step): 6 =
# 0.4375 A, 14 = 0.9375 A, 29 = 1.875 A, 41 = 2.625 A (about 63 W at 24 V). It
# throttles only charge current; the VSYS load passes through.
TYPE_1_INPUT_LIMIT_CODE: Final = 6
INPUT_LIMIT_CODES: Final = {
    # No PoE: the limit does nothing, but a PSE plugged in starts low.
    PoeType.NONE: TYPE_1_INPUT_LIMIT_CODE,
    PoeType.UNKNOWN: TYPE_1_INPUT_LIMIT_CODE,
    PoeType.TYPE_1: TYPE_1_INPUT_LIMIT_CODE,
    PoeType.TYPE_2: 14,
    PoeType.TYPE_3: 29,
    PoeType.TYPE_4: 41,
}
# Ag59800 Table 5 by pin name: (GPIO6 TYP2, GPIO12 TYP3) -> PSE; low = asserted.
_PIN_TABLE: Final = {
    (1, 1): PoeType.TYPE_1,
    (0, 1): PoeType.TYPE_2,
    (1, 0): PoeType.TYPE_3,
    (0, 0): PoeType.TYPE_4,
}

# Derating (no pack NTC on this board): hull above 45 C halves the input limit,
# hull above 55 C or LTC4162 die above 100 C suspends charging; each level is
# left only 5 C below its threshold, so noise never toggles the suspend (each
# toggle restarts battery detection and the charge timers).
HULL_HALVE_ABOVE_C: Final = 45.0
HULL_SUSPEND_ABOVE_C: Final = 55.0
DIE_SUSPEND_ABOVE_C: Final = 100.0
HYSTERESIS_C: Final = 5.0

# The charger_state enum that proves no pack is on the charger (the board's fixed
# 10 k / 10 k NTC divider never reads open, so bat_missing_fault means no pack).
# bat_detect_failed_fault is not one: the charger could not source detection
# current (iin_limit_active or vin_uvcl_active, DS p.38) and retries every 30 s
# (DS p.23), with the pack possibly fitted, so it counts as unknown.
_PACK_MISSING_STATES: Final = ltc4162.BAT_MISSING_FAULT
_PRESENCE_UNKNOWN_STATES: Final = (
    ltc4162.BATTERY_DETECTION | ltc4162.BAT_DETECT_FAILED_FAULT
)


def poe_present(system_status: int) -> bool:
    """PoE (24 V on VIN) from ``vin_gt_4p2v``, not from the vin ADC.

    The status bit is real time and does not depend on the battery. vin converts
    only every ~5 s on battery, and ``vin_gt_vbat`` compares VIN with the pack.
    """
    return bool(system_status & ltc4162.VIN_GT_4P2V)


def poe_type(pins: tuple[int, int] | None, *, present: bool) -> PoeType:
    """Decode the TYP2/TYP3 levels (GPIO6, GPIO12) when PoE is present.

    HIGH/HIGH covers Type 1, a non-classifying PSE and one that waits for LLDP;
    ``pins`` None (GPIO unreadable) with PoE present is UNKNOWN.
    """
    if not present:
        return PoeType.NONE
    if pins is None:
        return PoeType.UNKNOWN
    return _PIN_TABLE[(int(pins[0] != 0), int(pins[1] != 0))]


def halved_code(code: int) -> int:
    """The largest input-limit code at or below half the current of ``code``."""
    return max(0, (code + 1) // 2 - 1)


def input_limit_code(poe: PoeType, thermal: ThermalLevel) -> int:
    """Input-limit code for the PSE type, halved while the hull is hot."""
    code = INPUT_LIMIT_CODES[poe]
    return code if thermal == ThermalLevel.NORMAL else halved_code(code)


def _above(value: float | None, limit: float) -> bool:
    return value is not None and value > limit


def _at_or_below(value: float | None, limit: float) -> bool:
    return value is not None and value <= limit


def thermal_level(
    previous: ThermalLevel, hull_c: float | None, die_c: float | None
) -> ThermalLevel:
    """Next derating level from the SHT45 hull and LTC4162 die temperatures.

    An unknown temperature (sensor down, telemetry invalid) is no evidence of
    cooling: it never releases a suspend, and an unknown hull temperature
    halves the limit.
    """
    if _above(hull_c, HULL_SUSPEND_ABOVE_C) or _above(die_c, DIE_SUSPEND_ABOVE_C):
        return ThermalLevel.SUSPENDED
    cooled = _at_or_below(hull_c, HULL_SUSPEND_ABOVE_C - HYSTERESIS_C) and (
        _at_or_below(die_c, DIE_SUSPEND_ABOVE_C - HYSTERESIS_C)
    )
    if previous == ThermalLevel.SUSPENDED and not cooled:
        return ThermalLevel.SUSPENDED
    if hull_c is None or hull_c > HULL_HALVE_ABOVE_C:
        return ThermalLevel.HALVED
    if previous != ThermalLevel.NORMAL and hull_c > HULL_HALVE_ABOVE_C - HYSTERESIS_C:
        return ThermalLevel.HALVED
    return ThermalLevel.NORMAL


def chemistry_mismatch(
    system_status: int, *, chem: int, cell_count: int
) -> bool | None:
    """Whether ``chem_cells`` contradicts the LAD part with 5 strapped cells.

    None when it cannot be checked: ``cell_count`` reads 0 ("unknown") whenever
    the charger is not enabled, so it means something only while ``en_chg`` = 1
    and it is non-zero. Never use a None or False here to release a suspend.
    """
    if not system_status & ltc4162.EN_CHG or cell_count == 0:
        return None
    return chem != ltc4162.CHEM_LAD or cell_count != ltc4162.CELL_COUNT


def pack_missing_by_fault(charger: ChargerStatus) -> bool:
    """``bat_missing_fault`` or a vbat that read 0: no pack, whatever the load.

    The other "absent" reason, a vbat at or below ``BATTERY_PRESENT_MIN_V``, can
    also be thruster sag at BAT_IN, so it means a pulled pack only with the
    motors settled (``LowBatteryMonitor``).
    """
    return bool((charger.charger_state or 0) & _PACK_MISSING_STATES) or (
        charger.vbat_zero
    )


def detection_failed(charger: ChargerStatus) -> bool:
    """``bat_detect_failed_fault``: no detection current (input limit or UVCL)."""
    return bool((charger.charger_state or 0) & ltc4162.BAT_DETECT_FAILED_FAULT)


def battery_presence(charger: ChargerStatus, now: float) -> BatteryPresence:
    """Whether a pack is on the charger, from the last fresh ``ChargerStatus``.

    Without PoE the CM5 runs from the pack, so it is present. With PoE it is
    present only with valid telemetry above ``BATTERY_PRESENT_MIN_V`` and no
    battery detection in progress; absent on ``bat_missing_fault``, a low vbat
    or a vbat that read 0 after a full conversion. Detection in progress (2-4 s
    after PoE arrives or charging resumes), ``bat_detect_failed_fault`` (the
    charger could not source detection current and retries in 30 s) and a
    telemetry that has not settled are unknown, so none of them enables ESC
    power or counts as a pulled pack.
    """
    if not charger.is_fresh(now) or charger.poe_present is None:
        return BatteryPresence.UNKNOWN
    if not charger.poe_present:
        return BatteryPresence.PRESENT
    state = charger.charger_state or 0
    telemetry = charger.telemetry
    if pack_missing_by_fault(charger):
        presence = BatteryPresence.ABSENT
    elif state & _PRESENCE_UNKNOWN_STATES or telemetry is None:
        presence = BatteryPresence.UNKNOWN
    elif telemetry.vbat_v > BATTERY_PRESENT_MIN_V:
        presence = BatteryPresence.PRESENT
    else:
        presence = BatteryPresence.ABSENT
    return presence


def charger_targets(
    *, poe: PoeType, thermal: ThermalLevel, chemistry_fault: bool
) -> ChargerSettings:
    """The five LTC4162 settings the CM5 wants (written only from bring-up B9).

    en_jeita = 0 so that 0x1A/0x1B stick; config_bits keeps force_telemetry_on,
    low-speed telemetry (see ChargerMonitor) and MPPT off, and suspends charging
    only for heat or a confirmed chemistry mismatch.
    """
    suspend = thermal == ThermalLevel.SUSPENDED or chemistry_fault
    config_bits = ltc4162.FORCE_TELEMETRY_ON
    if suspend:
        config_bits |= ltc4162.SUSPEND_CHARGER
    return ChargerSettings(
        charger_config_bits=0,
        vcharge_setting=CHARGE_VOLTAGE_CODE,
        charge_current_setting=CHARGE_CURRENT_CODE,
        iin_limit_target=input_limit_code(poe, thermal),
        config_bits=config_bits,
    )
