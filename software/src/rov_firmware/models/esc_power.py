"""ESC power state as the MCU last reported it (mainboard, ATTITUDE health bits 4-6)."""

from dataclasses import dataclass


@dataclass(slots=True)
class EscPowerTelemetry:
    """Board-side ESC power report; kept out of ``SystemHealth`` (app contract).

    ``wire_state`` is 0..5 from the last accepted ATTITUDE frame and None until one
    arrives, after its expiry and after a USB reconnect. Times are
    ``time.monotonic()`` seconds.
    """

    wire_state: int | None = None
    reported_at: float = 0.0
    changed_at: float = 0.0

    def record(self, wire_state: int, now: float) -> None:
        """Store a fresh report and remember when the value last changed."""
        if wire_state != self.wire_state:
            self.changed_at = now
        self.wire_state = wire_state
        self.reported_at = now

    def expire(self) -> None:
        """Forget the value; ``reported_at`` keeps measuring MCU silence."""
        self.wire_state = None
