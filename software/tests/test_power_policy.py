"""Pure charger and PoE policy (wiki 8.2, 6.6)."""

import pytest

from rov_firmware import power_policy
from rov_firmware.models.battery import BatteryPresence
from rov_firmware.models.charger import (
    ChargerSettings,
    ChargerStatus,
    ChargerTelemetry,
    PoeType,
    ThermalLevel,
)
from rov_firmware.power_policy import (
    battery_presence,
    charger_targets,
    chemistry_mismatch,
    halved_code,
    input_limit_code,
    poe_present,
    poe_type,
    thermal_level,
)
from rov_firmware.sensors import ltc4162


NORMAL, HALVED, SUSPENDED = (
    ThermalLevel.NORMAL,
    ThermalLevel.HALVED,
    ThermalLevel.SUSPENDED,
)


def test_owner_confirmable_values_are_the_wiki_defaults():
    assert power_policy.CHARGE_VOLTAGE_CODE == 31
    assert ltc4162.charge_voltage_per_cell(power_policy.CHARGE_VOLTAGE_CODE) == (
        pytest.approx(4.2)
    )
    assert power_policy.CHARGE_CURRENT_CODE == 19
    assert ltc4162.charge_current_amps(power_policy.CHARGE_CURRENT_CODE) == (
        pytest.approx(2.0)
    )
    assert power_policy.BATTERY_WARN_V_PER_CELL == 3.4
    assert power_policy.BATTERY_CRITICAL_V_PER_CELL == 3.2
    assert power_policy.BATTERY_SHUTDOWN_V_PER_CELL == 3.0
    assert power_policy.AUTO_POWER_OFF_ENABLED is True


def test_poe_present_is_vin_gt_4p2v_only():
    assert poe_present(ltc4162.VIN_GT_4P2V)
    assert poe_present(ltc4162.VIN_GT_4P2V | ltc4162.VIN_GT_VBAT)
    # vin_gt_vbat alone ("can charge") is not presence; neither is anything else.
    assert not poe_present(ltc4162.VIN_GT_VBAT)
    assert not poe_present(ltc4162.EN_CHG | ltc4162.INTVCC_GT_2P8V)
    assert not poe_present(0)


@pytest.mark.parametrize(
    ("gpio6", "gpio12", "expected"),
    [
        (1, 1, PoeType.TYPE_1),
        (0, 1, PoeType.TYPE_2),
        (1, 0, PoeType.TYPE_3),
        (0, 0, PoeType.TYPE_4),
    ],
)
def test_poe_type_follows_ag59800_table_5(gpio6, gpio12, expected):
    assert poe_type((gpio6, gpio12), present=True) is expected


@pytest.mark.parametrize("pins", [(1, 1), (0, 0), None])
def test_no_poe_wins_over_the_pins(pins):
    assert poe_type(pins, present=False) is PoeType.NONE


def test_unreadable_pins_with_poe_present_are_unknown():
    assert poe_type(None, present=True) is PoeType.UNKNOWN


@pytest.mark.parametrize(
    ("poe", "code", "amps"),
    [
        (PoeType.NONE, 6, 0.4375),
        (PoeType.UNKNOWN, 6, 0.4375),
        (PoeType.TYPE_1, 6, 0.4375),
        (PoeType.TYPE_2, 14, 0.9375),
        (PoeType.TYPE_3, 29, 1.875),
        (PoeType.TYPE_4, 41, 2.625),
    ],
)
def test_input_limit_per_pse_type(poe, code, amps):
    assert input_limit_code(poe, NORMAL) == code
    assert ltc4162.input_limit_amps(code) == pytest.approx(amps)


@pytest.mark.parametrize("code", [6, 14, 29, 41, 63, 0])
def test_halved_code_is_at_most_half_the_current(code):
    halved = halved_code(code)
    half = ltc4162.input_limit_amps(code) / 2
    assert 0 <= halved <= code
    assert halved == 0 or ltc4162.input_limit_amps(halved) <= half + 1e-9
    assert ltc4162.input_limit_amps(halved + 1) > half + 1e-9


def test_derated_limit_codes():
    assert input_limit_code(PoeType.TYPE_4, HALVED) == 20  # 1.3125 A
    assert input_limit_code(PoeType.TYPE_3, HALVED) == 14  # 0.9375 A
    assert input_limit_code(PoeType.TYPE_4, SUSPENDED) == 20


def run_levels(samples, start=NORMAL):
    level = start
    levels = []
    for hull, die in samples:
        level = thermal_level(level, hull, die)
        levels.append(level)
    return levels


def test_hull_above_45_halves_with_5_degrees_of_hysteresis():
    hull = [30, 45, 45.1, 44, 41, 40.1, 40, 44, 45.1]
    assert run_levels((t, 40.0) for t in hull) == [
        NORMAL,
        NORMAL,
        HALVED,
        HALVED,
        HALVED,
        HALVED,
        NORMAL,
        NORMAL,
        HALVED,
    ]


def test_hull_above_55_suspends_until_it_falls_to_50():
    hull = [50, 55, 55.1, 52, 50.1, 50, 46, 41, 40]
    assert run_levels((t, 40.0) for t in hull) == [
        HALVED,
        HALVED,
        SUSPENDED,
        SUSPENDED,
        SUSPENDED,
        HALVED,
        HALVED,
        HALVED,
        NORMAL,
    ]


def test_die_above_100_suspends_until_it_falls_to_95():
    die = [90, 100, 100.5, 97, 95.1, 95]
    assert run_levels((25.0, t) for t in die) == [
        NORMAL,
        NORMAL,
        SUSPENDED,
        SUSPENDED,
        SUSPENDED,
        NORMAL,
    ]


def test_noise_around_a_threshold_does_not_toggle_suspend():
    noisy = [55.2, 54.0, 55.3, 53.1, 55.05, 51.0, 54.9]
    levels = run_levels((t, 40.0) for t in noisy)
    assert levels == [SUSPENDED] * len(noisy)


def test_unknown_hull_temperature_halves_but_never_releases_a_suspend():
    assert thermal_level(NORMAL, None, 40.0) is HALVED
    assert thermal_level(HALVED, None, 40.0) is HALVED
    assert thermal_level(SUSPENDED, None, 40.0) is SUSPENDED
    assert thermal_level(SUSPENDED, 45.0, None) is SUSPENDED
    assert thermal_level(NORMAL, 30.0, None) is NORMAL
    assert thermal_level(NORMAL, None, 101.0) is SUSPENDED


def test_chemistry_is_checked_only_while_the_charger_runs():
    en_chg = ltc4162.EN_CHG
    assert chemistry_mismatch(0, chem=0, cell_count=0) is None
    assert chemistry_mismatch(0, chem=4, cell_count=3) is None
    # cell_count 0 is "unknown", not a mismatch.
    assert chemistry_mismatch(en_chg, chem=0, cell_count=0) is None
    assert chemistry_mismatch(en_chg, chem=0, cell_count=5) is False
    assert chemistry_mismatch(en_chg, chem=0, cell_count=6) is True
    assert chemistry_mismatch(en_chg, chem=4, cell_count=5) is True


def test_targets_charge_to_4v2_per_cell_at_2a_with_jeita_off():
    targets = charger_targets(poe=PoeType.TYPE_4, thermal=NORMAL, chemistry_fault=False)
    assert targets == ChargerSettings(
        charger_config_bits=0,
        vcharge_setting=31,
        charge_current_setting=19,
        iin_limit_target=41,
        config_bits=ltc4162.FORCE_TELEMETRY_ON,
    )


@pytest.mark.parametrize(
    ("thermal", "fault", "suspended"),
    [
        (NORMAL, False, False),
        (HALVED, False, False),
        (SUSPENDED, False, True),
        (NORMAL, True, True),
    ],
)
def test_suspend_only_on_heat_or_a_chemistry_fault(thermal, fault, suspended):
    targets = charger_targets(
        poe=PoeType.TYPE_2, thermal=thermal, chemistry_fault=fault
    )
    assert bool(targets.config_bits & ltc4162.SUSPEND_CHARGER) is suspended
    assert targets.config_bits & ltc4162.FORCE_TELEMETRY_ON
    # Low-speed telemetry, no MPPT, never run_bsr; en_jeita always 0.
    assert not targets.config_bits & (
        ltc4162.TELEMETRY_SPEED_HIGH | ltc4162.MPPT_EN | ltc4162.RUN_BSR
    )
    assert targets.charger_config_bits == 0


def _status(  # noqa: PLR0913 - one keyword per charger fact
    *,
    poe: bool | None = True,
    vbat_v: float | None = 19.0,
    charger_state: int = 64,
    vbat_zero: bool = False,
    readable: bool = True,
    updated_at: float = 100.0,
) -> ChargerStatus:
    telemetry = (
        None
        if vbat_v is None
        else ChargerTelemetry(
            vbat_cell_v=vbat_v / 5,
            vbat_v=vbat_v,
            vin_v=24.0,
            vout_v=vbat_v,
            ibat_a=0.5,
            iin_a=0.6,
            die_temp_c=35.0,
            charger_state=charger_state,
            charge_status=0,
            system_status=0,
            chem=0,
            cell_count=5,
            telemetry_valid=True,
        )
    )
    return ChargerStatus(
        readable=readable,
        updated_at=updated_at,
        telemetry=telemetry,
        telemetry_problem=None if telemetry else "vbat reads 0",
        poe_present=poe if readable else None,
        charger_state=charger_state if readable else None,
        vbat_zero=vbat_zero,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Without PoE the CM5 runs from the pack.
        (_status(poe=False, vbat_v=None), BatteryPresence.PRESENT),
        (_status(), BatteryPresence.PRESENT),
        (_status(vbat_v=11.9), BatteryPresence.ABSENT),
        (_status(vbat_v=None, vbat_zero=True), BatteryPresence.ABSENT),
        (_status(charger_state=ltc4162.BAT_MISSING_FAULT), BatteryPresence.ABSENT),
        # A vbat that really reads 0 still means no pack, whatever the state.
        (
            _status(
                vbat_v=None,
                vbat_zero=True,
                charger_state=ltc4162.BAT_DETECT_FAILED_FAULT,
            ),
            BatteryPresence.ABSENT,
        ),
        # Detection in progress, failed detection (no detection current: input
        # limit or UVCL, DS p.38; retried every 30 s) or telemetry settling:
        # neither present nor pulled.
        (_status(charger_state=ltc4162.BATTERY_DETECTION), BatteryPresence.UNKNOWN),
        (
            _status(charger_state=ltc4162.BAT_DETECT_FAILED_FAULT),
            BatteryPresence.UNKNOWN,
        ),
        (_status(vbat_v=None), BatteryPresence.UNKNOWN),
        (_status(readable=False, vbat_v=None), BatteryPresence.UNKNOWN),
        (_status(updated_at=90.0), BatteryPresence.UNKNOWN),
    ],
)
def test_battery_presence(status, expected):
    assert battery_presence(status, 100.0) is expected


def test_only_bat_missing_fault_or_vbat_zero_is_a_fault_level_missing_pack():
    assert power_policy.pack_missing_by_fault(
        _status(charger_state=ltc4162.BAT_MISSING_FAULT)
    )
    assert power_policy.pack_missing_by_fault(_status(vbat_v=None, vbat_zero=True))
    # A low vbat can be thruster sag; failed detection is not a missing pack.
    assert not power_policy.pack_missing_by_fault(_status(vbat_v=11.5))
    assert not power_policy.pack_missing_by_fault(
        _status(charger_state=ltc4162.BAT_DETECT_FAILED_FAULT)
    )
    assert power_policy.detection_failed(
        _status(charger_state=ltc4162.BAT_DETECT_FAILED_FAULT)
    )
    assert not power_policy.detection_failed(_status())


def test_battery_recovery_hysteresis_and_presence_threshold():
    assert pytest.approx(0.1) == power_policy.BATTERY_RECOVERY_HYSTERESIS_V_PER_CELL
    assert pytest.approx(12.0) == power_policy.BATTERY_PRESENT_MIN_V
