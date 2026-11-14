"""Charger and PoE state on the CM5 (mainboard LTC4162 at I2C1 0x68, PoE type pins).

Board-side state, kept out of ``RovStatus`` and ``SystemHealth`` (app contract).
Every ``ChargerMonitor`` cycle builds a new frozen ``ChargerStatus`` and swaps it
into ``RovState.charger`` with one assignment, so a reader on the event loop never
sees half of a cycle. Times are ``time.monotonic()`` seconds.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum


# A status older than this counts as unreadable (the monitor runs at 1 Hz).
CHARGER_STALE_S = 3.0
# ``telemetry_problem`` of the status before the first monitor cycle.
NOT_READ_YET = "not read yet"


class PoeType(StrEnum):
    """PSE type from the Ag59800 TYP2/TYP3 pins, or no PoE at all."""

    NONE = "none"
    # PoE present but the GPIO6/GPIO12 levels could not be read: Type 1 limit.
    UNKNOWN = "unknown"
    # Pins HIGH/HIGH with PoE present: Type 1, a PSE without physical-layer
    # classification, or one that waits for LLDP. All get the Type 1 limit.
    TYPE_1 = "type1"
    TYPE_2 = "type2"
    TYPE_3 = "type3"
    TYPE_4 = "type4"


class ThermalLevel(IntEnum):
    """Charger derating from hull (SHT45) and LTC4162 die temperature."""

    NORMAL = 0
    HALVED = 1
    SUSPENDED = 2


@dataclass(frozen=True, slots=True)
class ChargerTelemetry:
    """One decoded LTC4162 telemetry read (scales in ``sensors/ltc4162.py``).

    Status words are masked to their documented fields. ``chem`` and
    ``cell_count`` mean something only while ``system_status`` has ``en_chg``.
    """

    vbat_cell_v: float
    vbat_v: float
    vin_v: float
    vout_v: float
    ibat_a: float
    iin_a: float
    die_temp_c: float
    charger_state: int
    charge_status: int
    system_status: int
    chem: int
    cell_count: int
    telemetry_valid: bool


@dataclass(frozen=True, slots=True)
class ChargerSettings:
    """The five LTC4162 settings registers as field-masked codes (policy targets)."""

    charger_config_bits: int
    vcharge_setting: int
    charge_current_setting: int
    iin_limit_target: int
    config_bits: int


@dataclass(frozen=True, slots=True)
class ChargerStatus:
    """What the CM5 knows about the charger and PoE after the last monitor cycle.

    ``telemetry`` is None while it is invalid: before ``telemetry_valid`` plus one
    conversion wait after ``force_telemetry_on`` was set, while vbat reads 0, and
    after an I2C error. ``poe_present`` comes from a real-time status bit and is
    known whenever the chip answered, also without valid telemetry.
    """

    readable: bool = False
    updated_at: float = 0.0
    telemetry: ChargerTelemetry | None = None
    telemetry_problem: str | None = NOT_READ_YET
    poe_present: bool | None = None
    # Real-time one-hot charger_state (0x34), known whenever the chip answered,
    # also while the telemetry is invalid (battery_detection, bat_missing_fault).
    charger_state: int | None = None
    # vbat read 0 after a full conversion cycle: no pack voltage at BATSENS+.
    vbat_zero: bool = False
    poe_type: PoeType | None = None
    poe_pins: tuple[int, int] | None = None
    thermal: ThermalLevel = ThermalLevel.NORMAL
    writes_enabled: bool = False
    targets: ChargerSettings | None = None

    def is_fresh(self, now: float, max_age_s: float = CHARGER_STALE_S) -> bool:
        """The chip answered in a cycle no older than ``max_age_s``."""
        return self.readable and now - self.updated_at <= max_age_s

    def fresh_telemetry(
        self, now: float, max_age_s: float = CHARGER_STALE_S
    ) -> ChargerTelemetry | None:
        """Valid telemetry from a recent cycle, else None (charger unhealthy)."""
        return self.telemetry if self.is_fresh(now, max_age_s) else None
