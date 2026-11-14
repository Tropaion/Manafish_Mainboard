"""CM5 SoC monitor at 1 Hz: input undervoltage, SoC temperature, get_throttled.

Every cycle runs in a worker thread (sysfs reads and one subprocess) and swaps
a new frozen ``SocStatus`` into ``state.soc``:

- ``rpi_volt`` hwmon ``in0_lcrit_alarm`` every second. The alarm toggles every
  few seconds under a sagging supply, so ``SocStatus.pi_undervoltage`` latches
  it for 60 s after the last sample that read 1 (``rov.piUndervoltage`` and the
  field diagnostics read the latch).
- ``/sys/class/thermal/thermal_zone0/temp`` every second, the path upstream's
  extended telemetry reads; on the CM5 zone 0 is cpu-thermal
  (``rov.socTemperature``).
- ``vcgencmd get_throttled`` every 10 s, only when vcgencmd was on the PATH at
  start (the image puts raspberrypi-utils on the service PATH, board.nix). Its
  "occurred" bits are sticky until reboot, so 10 s misses nothing.
"""

import asyncio
from collections.abc import Awaitable, Callable
import functools
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Final

from .log import log_error, log_info, log_warn
from .models.soc import SocStatus
from .sensors.pi_power import HWMON_ROOT, read_pi_undervoltage


if TYPE_CHECKING:
    from .rov_state import RovState


SOC_INTERVAL_S: Final = 1.0
THROTTLED_INTERVAL_S: Final = 10.0
VCGENCMD_TIMEOUT_S: Final = 2.0
SOC_TEMPERATURE_PATH: Final = Path("/sys/class/thermal/thermal_zone0/temp")
# vcgencmd get_throttled bits (Raspberry Pi documentation): 0-3 now, 16-19
# since boot.
THROTTLED_FLAGS: Final = {
    0: "undervoltage",
    1: "arm_frequency_capped",
    2: "throttled",
    3: "soft_temperature_limit",
    16: "undervoltage_occurred",
    17: "arm_frequency_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temperature_limit_occurred",
}
THROTTLED_NOW_MASK: Final = 0xF
_THROTTLED_PATTERN: Final = re.compile(r"throttled=(0x[0-9a-fA-F]+)")


def read_temperature(path: Path = SOC_TEMPERATURE_PATH) -> float | None:
    """Return a thermal zone's temperature in °C (sysfs reports milli-°C)."""
    try:
        return int(path.read_text(encoding="ascii")) / 1000
    except (OSError, UnicodeError, ValueError):
        return None


def parse_throttled(text: str) -> int | None:
    """Parse ``throttled=0x50000`` into its integer value."""
    match = _THROTTLED_PATTERN.search(text)
    return int(match[1], 16) if match else None


def throttled_flags(value: int) -> list[str]:
    """Names of the get_throttled bits set in ``value``."""
    return [name for bit, name in THROTTLED_FLAGS.items() if value & (1 << bit)]


def read_throttled(
    vcgencmd: str | None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int | None:
    """Run ``vcgencmd get_throttled``; None when vcgencmd is absent or fails."""
    if vcgencmd is None:
        return None
    try:
        result = run(
            [vcgencmd, "get_throttled"],
            capture_output=True,
            text=True,
            timeout=VCGENCMD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_throttled(result.stdout)


class SocMonitor:
    """1 Hz SoC sampling; publishes ``state.soc``."""

    def __init__(  # noqa: PLR0913 - every I/O dependency is injectable for tests
        self,
        state: "RovState",
        *,
        hwmon_root: Path = HWMON_ROOT,
        temperature_path: Path = SOC_TEMPERATURE_PATH,
        throttled_reader: Callable[[], int | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Create the monitor; vcgencmd is looked up once, nothing else is read."""
        self._state = state
        self._hwmon_root = hwmon_root
        self._temperature_path = temperature_path
        self._throttled_reader = throttled_reader or functools.partial(
            read_throttled, shutil.which("vcgencmd")
        )
        self._clock = clock
        self._async_sleep = async_sleep
        self._throttled_due = float("-inf")
        self._latched = False

    def cycle(self) -> None:
        """One sample (blocking sysfs and subprocess; run it in a worker thread)."""
        now = self._clock()
        previous = self._state.soc
        undervoltage = read_pi_undervoltage(self._hwmon_root)
        asserted_at = now if undervoltage else previous.undervoltage_asserted_at
        throttled = previous.throttled
        if now >= self._throttled_due:
            self._throttled_due = now + THROTTLED_INTERVAL_S
            throttled = self._throttled_reader()
            self._log_throttled(previous.throttled, throttled)
        status = SocStatus(
            updated_at=now,
            temperature_c=read_temperature(self._temperature_path),
            undervoltage=undervoltage,
            undervoltage_asserted_at=asserted_at,
            throttled=throttled,
        )
        self._log_latch(status.pi_undervoltage(now) is True)
        self._state.soc = status

    def _log_latch(self, latched: bool) -> None:
        if latched and not self._latched:
            log_warn("Pi input undervoltage (rpi_volt in0_lcrit_alarm)")
        elif self._latched and not latched:
            log_info("Pi input undervoltage alarm clear for 60 s")
        self._latched = latched

    @staticmethod
    def _log_throttled(previous: int | None, current: int | None) -> None:
        if current is None or current == previous:
            return
        flags = ", ".join(throttled_flags(current)) or "none"
        message = f"vcgencmd get_throttled = {current:#x} ({flags})"
        if current & THROTTLED_NOW_MASK:
            log_warn(message)
        else:
            log_info(message)

    async def run(self) -> None:
        """Sample once per second; the reads run in a worker thread."""
        while True:
            started = self._clock()
            try:
                await asyncio.to_thread(self.cycle)
            except Exception as error:
                # A SoC probe fault must not end the service's task group.
                log_error(f"SoC monitor cycle failed: {error}")
            elapsed = self._clock() - started
            await self._async_sleep(min(max(SOC_INTERVAL_S - elapsed, 0.0), 1.0))
