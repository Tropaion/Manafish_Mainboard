"""LTC4162-LAD charger registers over SMBus (mainboard IC8, I2C1 address 0x68).

Every address, field, bit and scale below is from the LTC4162-L datasheet Rev A
(10/18, register map pp.36-47), as recorded in the verified fact sheet, and
cross-checked against Linux ``drivers/power/supply/ltc4162-l-charger.c``.
Registers are 16-bit SMBus words, low byte first; smbus2's ``read_word_data``
and ``write_word_data`` are little-endian already.

Register discipline (wiki 8.2):
- Only the five settings registers in ``SETTING_FIELDS`` are ever written, and
  only inside their documented fields: never 0x19 (ship mode, arms on 21325),
  never an undocumented address, never ``run_bsr``.
- ``en_jeita`` = 0 (0x29) is written before 0x1A/0x1B, else JEITA overwrites
  them every 11 ms. ``en_jeita`` reading 1 means the chip lost its registers.
- Telemetry words are signed (sign-extended). The Linux integer maths is not
  ported: it reads vbat and vin unsigned and its iin wraps above code 2929.
- Pack voltage uses the strapped 5 cells (CELLS0 = CELLS1 = VCC2P5), because
  ``cell_count`` reads 0 whenever the charger is not enabled.
"""

from dataclasses import dataclass
from typing import Final, Protocol

from ..models.charger import ChargerSettings, ChargerTelemetry


ADDRESS: Final = 0x68
I2C_BUS: Final = 1
CELL_COUNT: Final = 5
RSNSB_OHM: Final = 0.010
RSNSI_OHM: Final = 0.008

# Settings, R/W.
CONFIG_BITS: Final = 0x14
IIN_LIMIT_TARGET: Final = 0x15
CHARGE_CURRENT_SETTING: Final = 0x1A
VCHARGE_SETTING: Final = 0x1B
CHARGER_CONFIG_BITS: Final = 0x29
# Never written: writing 21325 arms ship mode, which loses every setting.
ARM_SHIP_MODE: Final = 0x19
# Read-only.
CHARGER_STATE: Final = 0x34
CHARGE_STATUS: Final = 0x35
SYSTEM_STATUS: Final = 0x39
VBAT: Final = 0x3A
VIN: Final = 0x3B
VOUT: Final = 0x3C
IBAT: Final = 0x3D
IIN: Final = 0x3E
DIE_TEMP: Final = 0x3F
CHEM_CELLS: Final = 0x43
TELEMETRY_STATUS: Final = 0x4A

# config_bits 0x14, field [5:1].
SUSPEND_CHARGER: Final = 1 << 5
RUN_BSR: Final = 1 << 4  # the chip clears it; writing 1 starts a BSR measurement
TELEMETRY_SPEED_HIGH: Final = 1 << 3  # 0 = tel_low_speed (about 5 s off-charge)
FORCE_TELEMETRY_ON: Final = 1 << 2
MPPT_EN: Final = 1 << 1
CONFIG_BITS_FIELD: Final = 0x3E
# charger_config_bits 0x29, field [2:0]; bit 1 is not documented.
EN_C_OVER_X_TERM: Final = 1 << 2
EN_JEITA: Final = 1 << 0
CHARGER_CONFIG_FIELD: Final = EN_C_OVER_X_TERM | EN_JEITA
# Code fields.
IIN_LIMIT_FIELD: Final = 0x3F  # [5:0]
CHARGE_CURRENT_FIELD: Final = 0x1F  # [4:0]
VCHARGE_FIELD: Final = 0x1F  # [4:0]

# system_status 0x39, field [8:0]; bit 6 is not documented.
EN_CHG: Final = 1 << 8
CELL_COUNT_ERR: Final = 1 << 7  # reads 1 whenever telemetry is off
NO_RT: Final = 1 << 5  # reads 1 whenever the charger is off
THERMAL_SHUTDOWN: Final = 1 << 4
VIN_OVLO: Final = 1 << 3
VIN_GT_VBAT: Final = 1 << 2
VIN_GT_4P2V: Final = 1 << 1
INTVCC_GT_2P8V: Final = 1 << 0
SYSTEM_STATUS_FIELD: Final = 0x1BF
CHARGER_STATE_FIELD: Final = 0x1FFF  # [12:0]
CHARGE_STATUS_FIELD: Final = 0x3F  # [5:0]
TELEMETRY_VALID: Final = 1 << 0  # telemetry_status 0x4A bit 0
# chem_cells 0x43: chem [11:8], cell_count [3:0].
CHEM_SHIFT: Final = 8
CHEM_FIELD: Final = 0xF
CELL_COUNT_FIELD: Final = 0xF
CHEM_LAD: Final = 0

# charger_state 0x34: one-hot enums.
BAT_DETECT_FAILED_FAULT: Final = 4096
BATTERY_DETECTION: Final = 2048
CHARGER_SUSPENDED: Final = 256
MAX_CHARGE_TIME_FAULT: Final = 4
BAT_MISSING_FAULT: Final = 2
BAT_SHORT_FAULT: Final = 1
CHARGER_STATE_FAULTS: Final = (
    BAT_DETECT_FAILED_FAULT
    | MAX_CHARGE_TIME_FAULT
    | BAT_MISSING_FAULT
    | BAT_SHORT_FAULT
)
CHARGER_STATE_NAMES: Final = {
    BAT_DETECT_FAILED_FAULT: "bat_detect_failed_fault",
    BATTERY_DETECTION: "battery_detection",
    CHARGER_SUSPENDED: "charger_suspended",
    128: "precharge",
    64: "cc_cv_charge",
    32: "ntc_pause",
    16: "timer_term",
    8: "c_over_x_term",
    MAX_CHARGE_TIME_FAULT: "max_charge_time_fault",
    BAT_MISSING_FAULT: "bat_missing_fault",
    BAT_SHORT_FAULT: "bat_short_fault",
}
# charge_status 0x35: one-hot enums, 0 = charger_off.
CHARGE_STATUS_NAMES: Final = {
    32: "ilim_reg_active",
    16: "thermal_reg_active",
    8: "vin_uvcl_active",
    4: "iin_limit_active",
    2: "constant_current",
    1: "constant_voltage",
}

# Telemetry scales (all words signed).
VBAT_CELL_V_PER_LSB: Final = 192.4e-6  # per cell; pack = x cell_count
VIN_V_PER_LSB: Final = 1.649e-3
VOUT_V_PER_LSB: Final = 1.653e-3
SENSE_V_PER_LSB: Final = 1.466e-6  # ibat over RSNSB, iin over RSNSI
DIE_C_PER_LSB: Final = 0.0215
DIE_C_OFFSET: Final = -264.4
# Settings scales.
IIN_LIMIT_V_PER_STEP: Final = 500e-6  # (code + 1) x 500 uV / RSNSI
CHARGE_CURRENT_V_PER_STEP: Final = 1e-3  # (code + 1) x 1 mV / RSNSB
VCHARGE_BASE_V: Final = 3.8125  # per cell: 3.8125 V + code x 12.5 mV
VCHARGE_STEP_V: Final = 0.0125

# Register -> its writable field. The only registers this module writes, in the
# order a full rewrite uses: en_jeita off first, config_bits last.
SETTING_FIELDS: Final = {
    CHARGER_CONFIG_BITS: CHARGER_CONFIG_FIELD,
    VCHARGE_SETTING: VCHARGE_FIELD,
    CHARGE_CURRENT_SETTING: CHARGE_CURRENT_FIELD,
    IIN_LIMIT_TARGET: IIN_LIMIT_FIELD,
    CONFIG_BITS: CONFIG_BITS_FIELD & ~RUN_BSR,
}
_WORD: Final = 0xFFFF
_SIGN: Final = 0x8000


class WordBus(Protocol):
    """The SMBus word calls this module uses (smbus2.SMBus has them)."""

    def read_word_data(self, i2c_addr: int, register: int, /) -> int: ...  # noqa: D102

    def write_word_data(self, i2c_addr: int, register: int, value: int, /) -> None: ...  # noqa: D102


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """What ``apply_settings`` wrote, in write order."""

    written: tuple[int, ...]
    reset_detected: bool


def sign_extend_16(word: int) -> int:
    """Interpret the low 16 bits of ``word`` as two's complement."""
    word &= _WORD
    return word - 0x10000 if word & _SIGN else word


def input_limit_amps(code: int) -> float:
    """``iin_limit_target`` code to amps at RSNSI 8 mOhm (62.5 mA per step)."""
    return (code + 1) * IIN_LIMIT_V_PER_STEP / RSNSI_OHM


def charge_current_amps(code: int) -> float:
    """``charge_current_setting`` code to amps at RSNSB 10 mOhm (100 mA per step)."""
    return (code + 1) * CHARGE_CURRENT_V_PER_STEP / RSNSB_OHM


def charge_voltage_per_cell(code: int) -> float:
    """``vcharge_setting`` code to volts per cell (31 = 4.2 V, 23 = 4.1 V)."""
    return VCHARGE_BASE_V + code * VCHARGE_STEP_V


def _one_hot_name(value: int, names: dict[int, str], zero: str | None) -> str:
    if value == 0 and zero is not None:
        return zero
    return names.get(value, f"{value:#06x}")


def charger_state_name(value: int) -> str:
    """Datasheet name of a ``charger_state`` word, or its hex value."""
    return _one_hot_name(value, CHARGER_STATE_NAMES, None)


def charge_status_name(value: int) -> str:
    """Datasheet name of a ``charge_status`` word, or its hex value."""
    return _one_hot_name(value, CHARGE_STATUS_NAMES, "charger_off")


class Ltc4162:
    """Register access to the charger; every call is blocking I2C."""

    def __init__(self, bus: WordBus, address: int = ADDRESS) -> None:
        """Use an open SMBus; nothing is sent until a method is called."""
        self._bus = bus
        self._address = address

    def _read(self, register: int) -> int:
        return self._bus.read_word_data(self._address, register) & _WORD

    def _signed(self, register: int) -> int:
        return sign_extend_16(self._read(register))

    def read_telemetry(self) -> ChargerTelemetry:
        """Read and scale the telemetry and status words (one word each)."""
        vbat_cell_v = self._signed(VBAT) * VBAT_CELL_V_PER_LSB
        chem_cells = self._read(CHEM_CELLS)
        return ChargerTelemetry(
            vbat_cell_v=vbat_cell_v,
            vbat_v=vbat_cell_v * CELL_COUNT,
            vin_v=self._signed(VIN) * VIN_V_PER_LSB,
            vout_v=self._signed(VOUT) * VOUT_V_PER_LSB,
            ibat_a=self._signed(IBAT) * SENSE_V_PER_LSB / RSNSB_OHM,
            iin_a=self._signed(IIN) * SENSE_V_PER_LSB / RSNSI_OHM,
            die_temp_c=self._signed(DIE_TEMP) * DIE_C_PER_LSB + DIE_C_OFFSET,
            charger_state=self._read(CHARGER_STATE) & CHARGER_STATE_FIELD,
            charge_status=self._read(CHARGE_STATUS) & CHARGE_STATUS_FIELD,
            system_status=self._read(SYSTEM_STATUS) & SYSTEM_STATUS_FIELD,
            chem=(chem_cells >> CHEM_SHIFT) & CHEM_FIELD,
            cell_count=chem_cells & CELL_COUNT_FIELD,
            telemetry_valid=bool(self._read(TELEMETRY_STATUS) & TELEMETRY_VALID),
        )

    def _read_setting(self, register: int) -> int:
        """Read a settings register masked to its writable field (no ``run_bsr``)."""
        return self._read(register) & SETTING_FIELDS[register]

    def write_setting(self, register: int, value: int) -> None:
        """Write one settings register; anything else is refused before the bus."""
        field = SETTING_FIELDS.get(register)
        if field is None:
            msg = f"LTC4162 register {register:#04x} is never written"
            raise ValueError(msg)
        if value < 0 or value & ~field:
            msg = f"{value:#06x} is outside the writable field of {register:#04x}"
            raise ValueError(msg)
        self._bus.write_word_data(self._address, register, value)

    def ensure_force_telemetry_on(self) -> bool:
        """Read-modify-write ``force_telemetry_on`` = 1 in 0x14; True if it wrote.

        The one write allowed before bring-up B9: without it the chip stops
        measuring on battery-only power. The rest of the documented field is kept
        except ``run_bsr``, which is cleared; bit 0 and bits 15:6 are written 0.
        """
        current = self._read_setting(CONFIG_BITS)
        if current & FORCE_TELEMETRY_ON:
            return False
        self.write_setting(CONFIG_BITS, current | FORCE_TELEMETRY_ON)
        return True

    def apply_settings(self, targets: ChargerSettings) -> ApplyResult:
        """Read back each setting and rewrite it if it differs from ``targets``.

        0x29 is handled first. ``en_jeita`` reading 1 means the chip reset (or was
        never configured), so every register is rewritten in order: 0x29, 0x1B,
        0x1A, 0x15, 0x14. ``run_bsr`` is ignored in the 0x14 comparison.
        """
        wanted = {
            CHARGER_CONFIG_BITS: targets.charger_config_bits,
            VCHARGE_SETTING: targets.vcharge_setting,
            CHARGE_CURRENT_SETTING: targets.charge_current_setting,
            IIN_LIMIT_TARGET: targets.iin_limit_target,
            CONFIG_BITS: targets.config_bits,
        }
        if targets.charger_config_bits & EN_JEITA:
            msg = "targets must clear en_jeita, else JEITA overwrites 0x1A/0x1B"
            raise ValueError(msg)
        for register, value in wanted.items():
            field = SETTING_FIELDS[register]
            if value < 0 or value & ~field:
                msg = f"{value:#06x} is outside the writable field of {register:#04x}"
                raise ValueError(msg)
        reset = bool(self._read(CHARGER_CONFIG_BITS) & EN_JEITA)
        written: list[int] = []
        for register, value in wanted.items():
            if reset or self._read_setting(register) != value:
                self.write_setting(register, value)
                written.append(register)
        return ApplyResult(written=tuple(written), reset_detected=reset)
