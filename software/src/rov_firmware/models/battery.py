"""Battery state on the CM5 as ``LowBatteryMonitor`` sees it (mainboard LTC4162).

Board-side state, kept out of ``RovStatus`` (app contract). The monitor swaps a
new frozen ``BatteryStatus`` into ``RovState.battery`` every cycle, so readers on
the event loop never see half of a cycle. Times are ``time.monotonic()`` seconds.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum


# A status older than this is not used (the monitor runs at 1 Hz).
BATTERY_STALE_S = 3.0


class BatteryLevel(IntEnum):
    """Low-battery level; it changes only while vbat is evaluated."""

    NORMAL = 0
    WARN = 1
    CRITICAL = 2
    # Below the shutdown threshold long enough; powers off when enabled.
    SHUTDOWN = 3


class BatteryPresence(StrEnum):
    """Whether a pack is on the charger, for the ESC power rules."""

    PRESENT = "present"
    ABSENT = "absent"
    # Charger unreadable, or telemetry not settled yet.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BatteryStatus:
    """What the CM5 knows about the pack after the last monitor cycle.

    ``voltage_ema_v`` is the 10 s EMA of the LTC4162 pack voltage (None while the
    telemetry is invalid). ``cell_voltage_v`` is the last vbat per cell that the
    level logic evaluated, that is with the motors neutral for 2 s.
    """

    updated_at: float = 0.0
    level: BatteryLevel = BatteryLevel.NORMAL
    presence: BatteryPresence = BatteryPresence.UNKNOWN
    present_since: float | None = None
    voltage_ema_v: float | None = None
    cell_voltage_v: float | None = None
    evaluating: bool = False

    def is_fresh(self, now: float, max_age_s: float = BATTERY_STALE_S) -> bool:
        """The monitor published this status no longer than ``max_age_s`` ago."""
        return self.updated_at > 0 and now - self.updated_at <= max_age_s

    def fresh_voltage(
        self, now: float, max_age_s: float = BATTERY_STALE_S
    ) -> float | None:
        """The EMA pack voltage from a recent cycle, else None."""
        return self.voltage_ema_v if self.is_fresh(now, max_age_s) else None

    def present_for(self, now: float) -> float:
        """Seconds the pack has been present without a break, 0 when it is not."""
        if self.presence != BatteryPresence.PRESENT or self.present_since is None:
            return 0.0
        return max(0.0, now - self.present_since)
