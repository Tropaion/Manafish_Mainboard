"""Hull climate from the mainboard SHT45 (0x44) and WSEN-PADS (0x5C) on I2C1.

Board-side state, kept out of ``RovStatus`` (app contract). ``HullSensors``
swaps a new frozen ``HullData`` into ``RovState.hull`` every cycle. Times are
``time.monotonic()`` seconds.
"""

from dataclasses import dataclass


# A reading older than this is not used (the sensors are read at 1 Hz).
HULL_STALE_S = 3.0


@dataclass(frozen=True, slots=True)
class Sht45Reading:
    """SHT45 temperature and relative humidity (clamped to 0..100 %)."""

    temperature_c: float
    humidity_pct: float
    sample_time: float


@dataclass(frozen=True, slots=True)
class PadsReading:
    """WSEN-PADS absolute pressure and its temperature."""

    pressure_hpa: float
    temperature_c: float
    sample_time: float


@dataclass(frozen=True, slots=True)
class HullData:
    """Last good reading of each hull sensor; None while that sensor is unhealthy."""

    sht45: Sht45Reading | None = None
    pads: PadsReading | None = None
    sht45_healthy: bool = False
    pads_healthy: bool = False

    def fresh_sht45(
        self, now: float, max_age_s: float = HULL_STALE_S
    ) -> Sht45Reading | None:
        """The SHT45 reading when it is recent, else None."""
        reading = self.sht45
        if reading is None or now - reading.sample_time > max_age_s:
            return None
        return reading

    def fresh_pads(
        self, now: float, max_age_s: float = HULL_STALE_S
    ) -> PadsReading | None:
        """The WSEN-PADS reading when it is recent, else None."""
        reading = self.pads
        if reading is None or now - reading.sample_time > max_age_s:
            return None
        return reading
