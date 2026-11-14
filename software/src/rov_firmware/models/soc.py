"""CM5 SoC state: temperature, input undervoltage and ``vcgencmd get_throttled``.

Board-side state, kept out of ``RovStatus`` (app contract). ``SocMonitor`` swaps
a new frozen ``SocStatus`` into ``RovState.soc`` every cycle. Times are
``time.monotonic()`` seconds.
"""

from dataclasses import dataclass


# The monitor polls at 1 Hz; older samples are not used.
SOC_STALE_S = 3.0
# rpi_volt in0_lcrit_alarm toggles every few seconds while the supply sags, so
# a single sample usually reads 0. The alarm counts as on this long after the
# last sample that read 1.
UNDERVOLTAGE_LATCH_S = 60.0


@dataclass(frozen=True, slots=True)
class SocStatus:
    """Last SoC samples; None means the source is unavailable."""

    updated_at: float = 0.0
    temperature_c: float | None = None
    # Instantaneous rpi_volt in0_lcrit_alarm of the last sample.
    undervoltage: bool | None = None
    # Time of the last sample that read 1.
    undervoltage_asserted_at: float | None = None
    throttled: int | None = None

    def fresh_temperature(
        self, now: float, max_age_s: float = SOC_STALE_S
    ) -> float | None:
        """SoC temperature from a recent sample, else None."""
        if now - self.updated_at > max_age_s:
            return None
        return self.temperature_c

    def pi_undervoltage(self, now: float) -> bool | None:
        """Latched undervoltage alarm.

        True within ``UNDERVOLTAGE_LATCH_S`` of the last asserted sample. Otherwise
        the last fresh sample: False for a measured clear alarm, None when the
        alarm is unavailable or not sampled recently (unavailable is not an alarm).
        """
        asserted = self.undervoltage_asserted_at
        if asserted is not None and 0 <= now - asserted < UNDERVOLTAGE_LATCH_S:
            return True
        if now - self.updated_at > SOC_STALE_S:
            return None
        return self.undervoltage
