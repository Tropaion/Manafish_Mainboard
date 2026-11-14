"""LTC4162 register layer against the datasheet Rev A facts (wiki 8.2)."""

import pytest

from rov_firmware.models.charger import ChargerSettings
from rov_firmware.sensors import ltc4162
from rov_firmware.sensors.ltc4162 import Ltc4162


@pytest.fixture
def chip(i2c_bus):
    return Ltc4162(i2c_bus)


TARGETS = ChargerSettings(
    charger_config_bits=0,
    vcharge_setting=31,
    charge_current_setting=19,
    iin_limit_target=14,
    config_bits=ltc4162.FORCE_TELEMETRY_ON,
)


def test_register_map_matches_the_datasheet():
    assert ltc4162.ADDRESS == 0x68
    assert (
        ltc4162.CONFIG_BITS,
        ltc4162.IIN_LIMIT_TARGET,
        ltc4162.CHARGE_CURRENT_SETTING,
        ltc4162.VCHARGE_SETTING,
        ltc4162.CHARGER_CONFIG_BITS,
    ) == (0x14, 0x15, 0x1A, 0x1B, 0x29)
    assert (
        ltc4162.CHARGER_STATE,
        ltc4162.CHARGE_STATUS,
        ltc4162.SYSTEM_STATUS,
        ltc4162.VBAT,
        ltc4162.VIN,
        ltc4162.VOUT,
        ltc4162.IBAT,
        ltc4162.IIN,
        ltc4162.DIE_TEMP,
        ltc4162.CHEM_CELLS,
        ltc4162.TELEMETRY_STATUS,
    ) == (0x34, 0x35, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x43, 0x4A)
    # config_bits [5:1], charger_config_bits bits 2 and 0, system_status bits.
    assert ltc4162.SUSPEND_CHARGER == 1 << 5
    assert ltc4162.RUN_BSR == 1 << 4
    assert ltc4162.TELEMETRY_SPEED_HIGH == 1 << 3
    assert ltc4162.FORCE_TELEMETRY_ON == 1 << 2
    assert ltc4162.MPPT_EN == 1 << 1
    assert ltc4162.EN_C_OVER_X_TERM == 1 << 2
    assert ltc4162.EN_JEITA == 1 << 0
    assert ltc4162.EN_CHG == 1 << 8
    assert ltc4162.VIN_GT_VBAT == 1 << 2
    assert ltc4162.VIN_GT_4P2V == 1 << 1
    assert ltc4162.TELEMETRY_VALID == 1 << 0


@pytest.mark.parametrize(
    ("word", "value"),
    [(0, 0), (0x7FFF, 32767), (0x8000, -32768), (0xFFFF, -1), (0x1_FFFF, -1)],
)
def test_sign_extend(word, value):
    assert ltc4162.sign_extend_16(word) == value


def test_telemetry_uses_the_datasheet_scales_and_five_cells(fake_ltc4162, chip):
    fake_ltc4162.set_telemetry(
        vbat=21830, vin=14555, vout=12100, ibat=13642, iin=5457, die=17000
    )
    telemetry = chip.read_telemetry()

    assert telemetry.vbat_cell_v == pytest.approx(21830 * 192.4e-6)
    assert telemetry.vbat_v == pytest.approx(21.0, abs=0.01)
    assert telemetry.vin_v == pytest.approx(24.0, abs=0.01)
    assert telemetry.vout_v == pytest.approx(12100 * 1.653e-3)
    assert telemetry.ibat_a == pytest.approx(2.0, abs=0.001)
    # Linux computes (s16) x 1466000u in 32 bits: code 5457 there reads 463 mA.
    assert telemetry.iin_a == pytest.approx(1.0, abs=0.001)
    assert telemetry.die_temp_c == pytest.approx(17000 * 0.0215 - 264.4)
    assert telemetry.telemetry_valid


def test_telemetry_words_are_signed(fake_ltc4162, chip):
    fake_ltc4162.set_telemetry(ibat=-6821, iin=-10, vbat=-5)
    telemetry = chip.read_telemetry()

    assert telemetry.ibat_a == pytest.approx(-1.0, abs=0.001)
    assert telemetry.iin_a < 0
    assert telemetry.vbat_v < 0


def test_status_words_are_masked_to_their_fields(fake_ltc4162, chip):
    fake_ltc4162.regs.update(
        {
            0x34: 0xE000 | 64,
            0x35: 0xFFC0 | 2,
            0x39: 0xFE00 | 0x106,
            0x43: 0xF0F0 | 0x0405,
            0x4A: 0xFFFE,
        }
    )
    telemetry = chip.read_telemetry()

    assert telemetry.charger_state == 64
    assert telemetry.charge_status == 2
    assert telemetry.system_status == 0x106
    assert (telemetry.chem, telemetry.cell_count) == (4, 5)
    assert not telemetry.telemetry_valid


def test_settings_codes_convert_with_the_board_sense_resistors():
    assert [ltc4162.input_limit_amps(code) for code in (0, 6, 14, 29, 41, 63)] == [
        pytest.approx(value) for value in (0.0625, 0.4375, 0.9375, 1.875, 2.625, 4.0)
    ]
    assert ltc4162.charge_current_amps(19) == pytest.approx(2.0)
    assert ltc4162.charge_current_amps(31) == pytest.approx(3.2)
    assert ltc4162.charge_voltage_per_cell(31) == pytest.approx(4.2)
    assert ltc4162.charge_voltage_per_cell(23) == pytest.approx(4.1)
    assert ltc4162.charge_voltage_per_cell(0) == pytest.approx(3.8125)


def test_apply_compares_only_each_writable_field(fake_ltc4162, chip):
    fake_ltc4162.regs.update(
        {0x29: 0xFFFA, 0x1B: 0xFFE0 | 31, 0x1A: 0xFFE0 | 19, 0x15: 0xFFC0 | 14}
    )
    fake_ltc4162.regs[0x14] = 0xFFC1 | ltc4162.RUN_BSR | ltc4162.FORCE_TELEMETRY_ON

    result = chip.apply_settings(TARGETS)

    assert result.written == ()
    assert not result.reset_detected
    assert fake_ltc4162.writes == []


@pytest.mark.parametrize(
    "register", [0x00, 0x01, 0x0D, 0x12, 0x13, 0x16, 0x17, 0x18, 0x19, 0x2A, 0x36]
)
def test_only_the_five_settings_registers_are_ever_written(register, chip, i2c_bus):

    with pytest.raises(ValueError, match="never written"):
        chip.write_setting(register, 0)

    assert not [call for call in i2c_bus.calls if call[0] == "write_word"]


def test_ship_mode_magic_can_not_be_written(chip, i2c_bus):

    with pytest.raises(ValueError, match="never written"):
        chip.write_setting(ltc4162.ARM_SHIP_MODE, 21325)

    assert i2c_bus.calls == []


@pytest.mark.parametrize(
    ("register", "value"),
    [
        (0x1A, 32),
        (0x1B, 0x20),
        (0x15, 64),
        (0x29, 0x0002),  # bit 1 is not documented
        (0x14, 0x0001),  # bit 0 is not documented
        (0x14, 0x0040),
        (0x14, 1 << 4),  # run_bsr would start a BSR measurement
        (0x1A, -1),
    ],
)
def test_values_outside_the_documented_field_are_refused(
    register, value, chip, i2c_bus
):

    with pytest.raises(ValueError, match="outside"):
        chip.write_setting(register, value)

    assert i2c_bus.calls == []


def test_force_telemetry_rmw_keeps_the_field_and_clears_run_bsr(fake_ltc4162, chip):
    fake_ltc4162.regs[0x14] = (
        0xFFC0
        | ltc4162.SUSPEND_CHARGER
        | ltc4162.RUN_BSR
        | ltc4162.TELEMETRY_SPEED_HIGH
        | ltc4162.MPPT_EN
        | 0x0001
    )

    assert chip.ensure_force_telemetry_on()

    assert fake_ltc4162.writes == [
        (
            0x14,
            ltc4162.SUSPEND_CHARGER
            | ltc4162.TELEMETRY_SPEED_HIGH
            | ltc4162.MPPT_EN
            | ltc4162.FORCE_TELEMETRY_ON,
        )
    ]


def test_force_telemetry_rmw_writes_nothing_when_the_bit_is_set(fake_ltc4162, chip):
    fake_ltc4162.regs[0x14] = ltc4162.FORCE_TELEMETRY_ON | ltc4162.RUN_BSR

    assert not chip.ensure_force_telemetry_on()

    assert fake_ltc4162.writes == []


def test_apply_clears_en_jeita_first_then_writes_in_order(fake_ltc4162, chip):

    result = chip.apply_settings(TARGETS)

    assert result.reset_detected
    assert fake_ltc4162.writes == [
        (0x29, 0x0000),
        (0x1B, 31),
        (0x1A, 19),
        (0x15, 14),
        (0x14, ltc4162.FORCE_TELEMETRY_ON),
    ]
    assert result.written == (0x29, 0x1B, 0x1A, 0x15, 0x14)
    assert fake_ltc4162.settings() == TARGETS


def test_apply_writes_nothing_when_the_read_back_matches(fake_ltc4162, chip):
    chip.apply_settings(TARGETS)
    fake_ltc4162.writes.clear()

    result = chip.apply_settings(TARGETS)

    assert result.written == ()
    assert not result.reset_detected
    assert fake_ltc4162.writes == []


def test_apply_rewrites_only_a_register_that_drifted(fake_ltc4162, chip):
    chip.apply_settings(TARGETS)
    fake_ltc4162.writes.clear()
    fake_ltc4162.regs[0x15] = 63

    result = chip.apply_settings(TARGETS)

    assert fake_ltc4162.writes == [(0x15, 14)]
    assert result.written == (0x15,)


def test_apply_ignores_run_bsr_in_the_read_back(fake_ltc4162, chip):
    chip.apply_settings(TARGETS)
    fake_ltc4162.writes.clear()
    fake_ltc4162.regs[0x14] = ltc4162.FORCE_TELEMETRY_ON | ltc4162.RUN_BSR

    assert chip.apply_settings(TARGETS).written == ()


def test_en_jeita_reading_one_means_a_reset_and_rewrites_everything(fake_ltc4162, chip):
    chip.apply_settings(TARGETS)
    fake_ltc4162.writes.clear()
    fake_ltc4162.power_cycle()

    result = chip.apply_settings(TARGETS)

    assert result.reset_detected
    # 0x1B already reads its default 31 but is rewritten all the same.
    assert [register for register, _ in fake_ltc4162.writes] == [
        0x29,
        0x1B,
        0x1A,
        0x15,
        0x14,
    ]
    assert fake_ltc4162.settings() == TARGETS


def test_targets_with_en_jeita_set_are_refused(chip, i2c_bus):
    targets = ChargerSettings(
        charger_config_bits=ltc4162.EN_JEITA,
        vcharge_setting=31,
        charge_current_setting=19,
        iin_limit_target=14,
        config_bits=ltc4162.FORCE_TELEMETRY_ON,
    )

    with pytest.raises(ValueError, match="en_jeita"):
        chip.apply_settings(targets)

    assert i2c_bus.calls == []


def test_state_names_decode_one_hot_words():
    assert ltc4162.charger_state_name(64) == "cc_cv_charge"
    assert ltc4162.charger_state_name(4096) == "bat_detect_failed_fault"
    assert ltc4162.charger_state_name(0x0300) == "0x0300"
    assert ltc4162.charge_status_name(0) == "charger_off"
    assert ltc4162.charge_status_name(2) == "constant_current"
    assert ltc4162.CHARGER_STATE_FAULTS == 4096 | 4 | 2 | 1
