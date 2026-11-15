"""Charger and PoE monitor at 1 Hz: LTC4162 at I2C1 0x68, PoE type on CM5 GPIO6/12.

Every cycle runs in a worker thread (``asyncio.to_thread``):

1. Read GPIO6 (TYP2) and GPIO12 (TYP3) as inputs without a bias flag, so the
   pull-ups from config.txt stay.
2. Read-modify-write ``force_telemetry_on`` = 1 in 0x14. This is the one write
   allowed before bring-up B9: without it the chip stops measuring on
   battery-only power, which is all of B2-B8. It never changes charging.
3. Read the telemetry. It is valid only once ``telemetry_valid`` reads 1, one
   full conversion cycle has passed since step 2 last set the bit, and vbat is
   above 0; an I2C error makes the whole charger unreadable.
4. Apply ``power_policy``: PoE presence and type, input limit, hull derating,
   chemistry check.
5. Only with ``CHARGER_WRITES_ENABLED`` (from B9): read back the five settings
   and rewrite what differs, 0x29 first; ``en_jeita`` reading 1 means the chip
   reset, so all of them are rewritten.
6. Log state and fault changes and publish a new ``ChargerStatus`` in
   ``state.charger``.

``telemetry_speed`` stays ``tel_low_speed`` (0x14 bit 3 = 0, the reset value,
which the B2-B8 read-modify-write keeps). While charging, the chip converts
about every 11 ms anyway. Off-charge it converts about every 5 s, which is
enough for the 10 s and 30 s low-battery windows and the 10 s battery EMA.
``tel_high_speed`` costs about 2.5 mA from the pack, and the setting stays
active while the magnet switch has the CM5 off. The price is a wait of one slow
cycle (``TELEMETRY_SETTLE_S``) after ``force_telemetry_on`` is set.
"""

import asyncio
from collections.abc import Awaitable, Callable
import time
from typing import TYPE_CHECKING, Final

from smbus2 import SMBus

from ..constants import SYSTEM_FAILURE_THRESHOLD
from ..log import log_error, log_info, log_warn
from ..models.charger import (
    NOT_READ_YET,
    ChargerSettings,
    ChargerStatus,
    ChargerTelemetry,
    PoeType,
    ThermalLevel,
)
from ..power_policy import (
    INPUT_LIMIT_CODES,
    charger_targets,
    chemistry_mismatch,
    poe_present,
    poe_type,
    thermal_level,
)
from ..rp1_gpio import Rp1Gpio
from . import ltc4162
from .ltc4162 import Ltc4162, WordBus


if TYPE_CHECKING:
    from ..rov_state import RovState


# False until bring-up B9 has checked the registers on the board. Until then the
# only write is the force_telemetry_on read-modify-write.
CHARGER_WRITES_ENABLED: Final = False
CHARGER_INTERVAL_S: Final = 1.0
POE_TYP2_LINE: Final = 6
POE_TYP3_LINE: Final = 12
# tel_low_speed converts about every 5 s off-charge: wait one cycle plus margin.
TELEMETRY_CYCLE_S: Final = 5.0
TELEMETRY_SETTLE_S: Final = TELEMETRY_CYCLE_S + 1.0
# A chemistry or cell-count mismatch must read this many cycles in a row.
CHEMISTRY_MISMATCH_CYCLES: Final = SYSTEM_FAILURE_THRESHOLD
_STATUS_FAULTS: Final = {
    ltc4162.THERMAL_SHUTDOWN: "thermal_shutdown",
    ltc4162.VIN_OVLO: "vin_ovlo",
}


def _open_bus() -> WordBus:
    return SMBus(ltc4162.I2C_BUS)


class ChargerMonitor:
    """1 Hz charger policy loop and read-back; publishes ``state.charger``."""

    def __init__(  # noqa: PLR0913 - every I/O dependency is injectable for tests
        self,
        state: "RovState",
        *,
        bus_factory: Callable[[], WordBus] = _open_bus,
        gpio_factory: Callable[[], Rp1Gpio] = Rp1Gpio,
        clock: Callable[[], float] = time.monotonic,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        writes_enabled: bool = CHARGER_WRITES_ENABLED,
    ) -> None:
        """Create the monitor; nothing is read or written before the first cycle."""
        self._state = state
        self._bus_factory = bus_factory
        self._gpio_factory = gpio_factory
        self._clock = clock
        self._async_sleep = async_sleep
        self._writes_enabled = writes_enabled
        self._bus: WordBus | None = None
        self._telemetry_on_at: float | None = None
        self._force_seen = False
        self._thermal = ThermalLevel.NORMAL
        self._mismatch_cycles = 0
        self._chemistry_fault = False
        self._last_poe: PoeType | None = None
        self._last_raw: ChargerTelemetry | None = None
        self._last_targets: ChargerSettings | None = None
        self._gpio_failure_logged = False

    def _read_poe_pins(self) -> tuple[int, int] | None:
        try:
            with self._gpio_factory() as gpio:
                pins = (
                    gpio.read_input(POE_TYP2_LINE),
                    gpio.read_input(POE_TYP3_LINE),
                )
        except Exception as error:  # no RP1 chip, no lgpio, or an lgpio error
            if not self._gpio_failure_logged:
                log_warn(f"PoE type pins GPIO6/GPIO12 unreadable: {error}")
                self._gpio_failure_logged = True
            return None
        self._gpio_failure_logged = False
        return pins

    def cycle(self) -> None:
        """One monitor cycle (blocking I2C and GPIO; run it in a worker thread)."""
        now = self._clock()
        pins = self._read_poe_pins()
        previous = self._state.charger
        try:
            status = self._read_charger(now, pins)
        except OSError as error:
            status = ChargerStatus(
                updated_at=now,
                telemetry_problem=f"I2C error: {error}",
                poe_pins=pins,
                thermal=self._thermal,
                writes_enabled=self._writes_enabled,
            )
        self._log_status(previous, status)
        self._state.charger = status

    def _chip(self) -> Ltc4162:
        if self._bus is None:
            self._bus = self._bus_factory()
        return Ltc4162(self._bus)

    def _read_charger(self, now: float, pins: tuple[int, int] | None) -> ChargerStatus:
        chip = self._chip()
        if chip.ensure_force_telemetry_on():
            self._telemetry_on_at = now
            if self._force_seen:
                log_warn(
                    "LTC4162 force_telemetry_on read 0: the charger lost its registers "
                    "(power cycle or ship mode). Set it again."
                )
        self._force_seen = True
        raw = chip.read_telemetry()
        self._log_raw(raw)
        converting = self._converting(raw, now)
        problem = self._telemetry_problem(raw, converting=converting)
        telemetry = raw if problem is None else None

        present = poe_present(raw.system_status)
        poe = poe_type(pins, present=present)
        hull = self._state.hull.fresh_sht45(now)
        self._thermal = thermal_level(
            self._thermal,
            None if hull is None else hull.temperature_c,
            # The die ADC is real without a pack too (vbat 0).
            raw.die_temp_c if converting else None,
        )
        self._check_chemistry(raw)
        targets = charger_targets(
            poe=self._limit_type(poe),
            thermal=self._thermal,
            chemistry_fault=self._chemistry_fault,
        )
        self._last_poe = poe
        self._apply(chip, targets)
        return ChargerStatus(
            readable=True,
            updated_at=now,
            telemetry=telemetry,
            telemetry_problem=problem,
            poe_present=present,
            charger_state=raw.charger_state,
            vbat_zero=converting and raw.vbat_v <= 0,
            poe_type=poe,
            poe_pins=pins,
            thermal=self._thermal,
            writes_enabled=self._writes_enabled,
            targets=targets,
        )

    def _converting(self, raw: ChargerTelemetry, now: float) -> bool:
        """The ADC has warmed up and finished a cycle since force_telemetry_on."""
        settled = (
            self._telemetry_on_at is None
            or now - self._telemetry_on_at >= TELEMETRY_SETTLE_S
        )
        return raw.telemetry_valid and settled

    @staticmethod
    def _telemetry_problem(raw: ChargerTelemetry, *, converting: bool) -> str | None:
        if not raw.telemetry_valid:
            return "telemetry_valid reads 0 (warming up or telemetry off)"
        if not converting:
            return "waiting one conversion cycle after force_telemetry_on was set"
        if raw.vbat_v <= 0:
            return "vbat reads 0 (no pack, or no conversion yet)"
        return None

    def _limit_type(self, poe: PoeType) -> PoeType:
        """The input limit drops at once but rises only after two equal reads."""
        if self._last_poe is None:
            return poe
        return min(poe, self._last_poe, key=INPUT_LIMIT_CODES.__getitem__)

    def _check_chemistry(self, raw: ChargerTelemetry) -> None:
        mismatch = chemistry_mismatch(
            raw.system_status, chem=raw.chem, cell_count=raw.cell_count
        )
        self._mismatch_cycles = self._mismatch_cycles + 1 if mismatch else 0
        if self._mismatch_cycles >= CHEMISTRY_MISMATCH_CYCLES and not (
            self._chemistry_fault
        ):
            # Latched until the service restarts: a suspended charger reads
            # cell_count 0, which must never release the suspend.
            self._chemistry_fault = True
            log_error(
                f"LTC4162 reports chem {raw.chem} with {raw.cell_count} cells while "
                f"charging; expected LAD ({ltc4162.CHEM_LAD}) with "
                f"{ltc4162.CELL_COUNT} cells. Charging stays suspended until the "
                "service restarts."
            )

    def _apply(self, chip: Ltc4162, targets: ChargerSettings) -> None:
        if targets != self._last_targets:
            suffix = "" if self._writes_enabled else " (writes disabled until B9)"
            log_info(f"Charger targets{suffix}: {ltc4162.describe_settings(targets)}")
            self._last_targets = targets
        if not self._writes_enabled:
            return
        result = chip.apply_settings(targets)
        if result.reset_detected:
            log_warn(
                "LTC4162 en_jeita reads 1 (chip reset or not configured yet): "
                "rewrote 0x29, 0x1B, 0x1A, 0x15, 0x14"
            )
        elif result.written:
            registers = ", ".join(f"{register:#04x}" for register in result.written)
            log_warn(f"LTC4162 read-back differed; rewrote {registers}")

    def _log_raw(self, raw: ChargerTelemetry) -> None:
        last = self._last_raw
        self._last_raw = raw
        if last is None or raw.charger_state != last.charger_state:
            name = ltc4162.charger_state_name(raw.charger_state)
            if raw.charger_state & ltc4162.CHARGER_STATE_FAULTS:
                log_warn(f"Charger state: {name}")
            else:
                log_info(f"Charger state: {name}")
        if last is None or raw.charge_status != last.charge_status:
            log_info(f"Charge status: {ltc4162.charge_status_name(raw.charge_status)}")
        before = 0 if last is None else last.system_status
        for bit, name in _STATUS_FAULTS.items():
            if raw.system_status & bit and not before & bit:
                log_warn(f"Charger system_status: {name} set")
            elif before & bit and not raw.system_status & bit:
                log_info(f"Charger system_status: {name} cleared")

    def _log_status(self, previous: ChargerStatus, status: ChargerStatus) -> None:
        if not status.readable:
            if previous.readable or previous.telemetry_problem == NOT_READ_YET:
                log_warn(f"LTC4162 charger unreadable: {status.telemetry_problem}")
            return
        if not previous.readable and previous.telemetry_problem != NOT_READ_YET:
            log_info("LTC4162 charger answers again")
        if status.telemetry_problem != previous.telemetry_problem:
            if status.telemetry is not None:
                log_info(
                    f"Charger telemetry valid: vbat {status.telemetry.vbat_v:.2f} V, "
                    f"ibat {status.telemetry.ibat_a:.3f} A"
                )
            else:
                log_info(f"Charger telemetry invalid: {status.telemetry_problem}")
        if status.poe_type != previous.poe_type:
            log_info(f"PoE: {status.poe_type} (TYP2/TYP3 pins {status.poe_pins})")
        if status.thermal != previous.thermal:
            message = f"Charger derating: {status.thermal.name.lower()}"
            if status.thermal > previous.thermal:
                log_warn(message)
            else:
                log_info(message)

    async def run(self) -> None:
        """Run the monitor once per second; the I2C runs in a worker thread."""
        while True:
            started = self._clock()
            try:
                await asyncio.to_thread(self.cycle)
            except Exception as error:
                # A charger fault must not end the service's task group.
                log_error(f"Charger monitor cycle failed: {error}")
            elapsed = self._clock() - started
            await self._async_sleep(min(max(CHARGER_INTERVAL_S - elapsed, 0.0), 1.0))
