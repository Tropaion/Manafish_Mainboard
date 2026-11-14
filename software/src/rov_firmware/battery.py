"""Battery monitor on the CM5: EMA voltage, low-battery levels, shutdown, ESC rules.

``LowBatteryMonitor`` runs once per second on the event loop (it does no
blocking I/O; the LTC4162 is read by ``ChargerMonitor``) and publishes a frozen
``BatteryStatus`` in ``state.battery``:

- **EMA:** 10 s exponential moving average of the LTC4162 pack voltage, the
  source of the battery percentage (``websocket/send/status.py``).
- **Levels:** vbat is measured at BAT_IN and sags under thrust, so it counts
  only after every motor command has been neutral for 2 s (or ESC power reads
  off); otherwise the level stays as it is. Warn below 3.4 V/cell for 10 s,
  critical below 3.2 V/cell for 10 s (a toast every 60 s, new ESC enables
  blocked, ESC power stays on), shutdown below 3.0 V/cell for 30 s without
  PoE: neutral, ESC off, ``systemctl poweroff``. Each window needs continuous
  neutral observation; a level is left only after the recovery window above
  its threshold plus the hysteresis. Invalid telemetry only warns: it never
  changes a level and never powers off.
- **Battery present:** tracked for the ESC rule "GP14 low for 5 s after the
  battery is plugged in" (``power_policy.battery_presence``).
- **ESC rules:** ``enable_blockers()`` for ``EscPowerController`` (charger
  readable, battery present for 5 s, battery not critical); automatic off only
  when the pack is pulled while on PoE, or in the shutdown chain. A pulled pack
  is ``bat_missing_fault`` or a vbat of 0 at once; a vbat at or below 12 V
  counts only with the motors settled (neutral for 2 s), since thrust sags it.
- **Shutdown chain:** holds ``mcu_flash_lock`` from its toast to the
  power-off, so no MCU or ESC flash can start under it; postponed while a
  firmware update holds the lock or the ESC recovery journal is set (a power-off
  would reset the MCU and lose the recovery).

Thresholds and switches are the named constants in ``power_policy.py``.
"""

import asyncio
from collections.abc import Awaitable, Callable
import math
import shutil
import time
from typing import TYPE_CHECKING, Final

from .esc_power import (
    EnableBlocker,
    EscPowerState,
    Notifier,
    esc_power_state,
    plain_toast,
)
from .log import log_error, log_info, log_warn
from .models.battery import BatteryLevel, BatteryPresence, BatteryStatus
from .models.charger import CHARGER_STALE_S, ChargerStatus, ChargerTelemetry
from .models.toast import ToastVariant
from .pico_control import direction_input_valid
from .power_policy import (
    AUTO_POWER_OFF_ENABLED,
    BATTERY_CRITICAL_V_PER_CELL,
    BATTERY_RECOVERY_HYSTERESIS_V_PER_CELL,
    BATTERY_SHUTDOWN_V_PER_CELL,
    BATTERY_WARN_V_PER_CELL,
    battery_presence,
    detection_failed,
    pack_missing_by_fault,
)


if TYPE_CHECKING:
    from .rov_state import RovState


MONITOR_INTERVAL_S: Final = 1.0
# Longest gap one cycle may add to a window (a stalled loop never counts more).
MAX_STEP_S: Final = 2.0 * MONITOR_INTERVAL_S
EMA_TAU_S: Final = 10.0
MOTORS_NEUTRAL_SETTLE_S: Final = 2.0
WARN_WINDOW_S: Final = 10.0
CRITICAL_WINDOW_S: Final = 10.0
SHUTDOWN_WINDOW_S: Final = 30.0
RECOVER_WINDOW_S: Final = 10.0
CRITICAL_TOAST_INTERVAL_S: Final = 60.0
# ESC rule: GP14 low for at least 5 s after the battery is plugged in.
BATTERY_PRESENT_HOLD_S: Final = 5.0
# Invalid telemetry is toasted only after this long (start-up settles in 6 s).
INVALID_WARN_S: Final = 15.0
AUTO_OFF_RETRY_S: Final = 10.0
SHUTDOWN_RETRY_S: Final = 60.0
BATTERY_TOAST_ID: Final = "battery"
CHARGER_UNREADABLE: Final = "Charger unreadable, ESC power blocked"
DETECTION_FAILED: Final = (
    "The charger cannot source power for battery detection (input limit or input "
    "undervoltage); it retries every 30 s."
)
RECOVERY_BLOCKS_POWER_OFF: Final = (
    "ESC firmware recovery is pending, so the CM5 does not power off. Connect PoE "
    "or retry the ESC firmware update now."
)


_toast = plain_toast(BATTERY_TOAST_ID)


async def systemctl_poweroff() -> None:
    """Run ``systemctl poweroff`` (polkit lets user pi do it, board.nix)."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        msg = "systemctl is not on the PATH"
        raise OSError(msg)
    process = await asyncio.create_subprocess_exec(
        systemctl,
        "poweroff",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(30):
        _, stderr = await process.communicate()
    if process.returncode != 0:
        msg = (
            f"systemctl poweroff exited {process.returncode}: {stderr.decode().strip()}"
        )
        raise OSError(msg)


def motor_commands_neutral(
    state: "RovState", *, wall_now: float, power: EscPowerState
) -> bool:
    """No thruster can draw current from the pack right now.

    True while ESC power reads off. Otherwise no thruster test, no auto-tuning,
    and either no live pilot input (the MCU neutralises stale input at once) or
    zero input without stabilization or depth hold, which drive the motors on
    their own.
    """
    if power == EscPowerState.OFF:
        return True
    if state.thrusters.test_thruster is not None or state.regulator.auto_tuning_active:
        return False
    if not direction_input_valid(state.thrusters, wall_now):
        return True
    direction = state.thrusters.direction_vector
    if direction is not None and any(float(value) != 0.0 for value in direction):
        return False
    return not (
        state.system_status.auto_stabilization or state.system_status.depth_hold
    )


def battery_voltage(state: "RovState", now: float) -> float:
    """Pack voltage for the battery %: LTC4162 10 s EMA, else ESC telemetry.

    The ESC voltage average is used only while the charger is unhealthy (no
    fresh valid LTC4162 telemetry). Without a fresh EMA (battery monitor not run
    yet) the latest LTC4162 vbat stands in. 0 means unknown.
    """
    telemetry = state.charger.fresh_telemetry(now)
    if telemetry is not None:
        ema = state.battery.fresh_voltage(now)
        return telemetry.vbat_v if ema is None else ema
    voltages_v = [v for v in state.mcu_telemetry.voltage if v > 0]
    return sum(voltages_v) / len(voltages_v) if voltages_v else 0


def _describe_vbat(telemetry: ChargerTelemetry) -> str:
    return f"{telemetry.vbat_v:.2f} V ({telemetry.vbat_cell_v:.2f} V/cell)"


class _Windows:
    """How long each vbat condition has held during continuous evaluation."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.below_warn = 0.0
        self.below_critical = 0.0
        self.below_shutdown = 0.0
        self.above_warn_recover = 0.0
        self.above_critical_recover = 0.0

    def add(self, cell_v: float, *, step: float, poe: bool) -> None:
        def count(held: float, condition: bool) -> float:
            return held + step if condition else 0.0

        hysteresis = BATTERY_RECOVERY_HYSTERESIS_V_PER_CELL
        self.below_warn = count(self.below_warn, cell_v < BATTERY_WARN_V_PER_CELL)
        self.below_critical = count(
            self.below_critical, cell_v < BATTERY_CRITICAL_V_PER_CELL
        )
        self.below_shutdown = count(
            self.below_shutdown, cell_v < BATTERY_SHUTDOWN_V_PER_CELL and not poe
        )
        self.above_warn_recover = count(
            self.above_warn_recover, cell_v >= BATTERY_WARN_V_PER_CELL + hysteresis
        )
        self.above_critical_recover = count(
            self.above_critical_recover,
            cell_v >= BATTERY_CRITICAL_V_PER_CELL + hysteresis,
        )

    def next_level(self, level: BatteryLevel) -> BatteryLevel:
        if self.below_shutdown >= SHUTDOWN_WINDOW_S:
            return BatteryLevel.SHUTDOWN
        if level == BatteryLevel.SHUTDOWN:
            # PoE arrived or vbat rose: no shutdown, still critical.
            level = BatteryLevel.CRITICAL
        if self.below_critical >= CRITICAL_WINDOW_S:
            level = max(level, BatteryLevel.CRITICAL)
        elif self.below_warn >= WARN_WINDOW_S:
            level = max(level, BatteryLevel.WARN)
        if level == BatteryLevel.CRITICAL and (
            self.above_critical_recover >= RECOVER_WINDOW_S
        ):
            level = BatteryLevel.WARN
        if level == BatteryLevel.WARN and self.above_warn_recover >= RECOVER_WINDOW_S:
            level = BatteryLevel.NORMAL
        return level


class LowBatteryMonitor:
    """1 Hz battery policy; publishes ``state.battery`` and the ESC enable checks."""

    def __init__(  # noqa: PLR0913 - every clock and side effect is injectable
        self,
        state: "RovState",
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        notify: Notifier = _toast,
        power_off: Callable[[], Awaitable[None]] = systemctl_poweroff,
    ) -> None:
        """Create the monitor; it acts only from its first cycle on."""
        self._state = state
        self._clock = clock
        self._wall_clock = wall_clock
        self._async_sleep = async_sleep
        self._notify = notify
        self._power_off = power_off
        self._windows = _Windows()
        self._level = BatteryLevel.NORMAL
        # Time of the last cycle whose vbat counted; None while it does not.
        self._evaluated_at: float | None = None
        self._neutral_since: float | None = None
        self._present_since: float | None = None
        self._ema_v: float | None = None
        self._ema_sample_at = 0.0
        self._cell_v: float | None = None
        self._invalid_since: float | None = None
        self._invalid_warned = False
        self._critical_toast_at = float("-inf")
        self._auto_off_at = float("-inf")
        self._shutdown_retry_at = float("-inf")
        self._shutdown_postponed = False
        self._recovery_toast_at = float("-inf")

    # ESC enable checks, run by EscPowerController after its own five.

    def enable_blockers(self) -> tuple[EnableBlocker, ...]:
        """The battery checks for ``EscPowerController``, in the order they apply.

        A missing pack on PoE comes first: its vbat of 0 also makes the charger
        telemetry invalid, and "no battery" is the reason the operator can fix.
        """
        return (
            self.pack_blocker,
            self.charger_blocker,
            self.presence_blocker,
            self.critical_blocker,
        )

    def pack_blocker(self) -> str | None:
        """Never enable on PoE without a pack."""
        if battery_presence(self._state.charger, self._clock()) != (
            BatteryPresence.ABSENT
        ):
            return None
        return (
            "No battery on the charger: with PoE alone ESC power stays off. "
            "Plug in the battery."
        )

    def charger_blocker(self) -> str | None:
        """Block while the LTC4162 telemetry is not valid and fresh."""
        now = self._clock()
        charger = self._state.charger
        if charger.fresh_telemetry(now) is not None:
            return None
        if now - charger.updated_at <= CHARGER_STALE_S and charger.telemetry_problem:
            detail = charger.telemetry_problem
        else:
            detail = f"no LTC4162 read in the last {CHARGER_STALE_S:.0f} s"
        return f"{CHARGER_UNREADABLE}: {detail}."

    def presence_blocker(self) -> str | None:
        """Block until the pack has been present for 5 s (after ``charger_blocker``)."""
        now = self._clock()
        battery = self._state.battery
        if not battery.is_fresh(now):
            return "The battery monitor has not reported recently."
        presence = battery_presence(self._state.charger, now)
        if presence == BatteryPresence.ABSENT:
            return self.pack_blocker()
        if presence == BatteryPresence.UNKNOWN:
            if detection_failed(self._state.charger):
                return DETECTION_FAILED
            return "Battery presence is not known yet (battery detection running)."
        present_for = battery.present_for(now)
        if present_for < BATTERY_PRESENT_HOLD_S:
            return (
                f"The battery was detected {present_for:.0f} s ago; ESC power can be "
                f"switched on {BATTERY_PRESENT_HOLD_S:.0f} s after it is plugged in."
            )
        return None

    def critical_blocker(self) -> str | None:
        """Block new enables while the battery is critical (ESC power stays on)."""
        battery = self._state.battery
        if battery.level < BatteryLevel.CRITICAL:
            return None
        return (
            f"The battery is critical (below {BATTERY_CRITICAL_V_PER_CELL:.1f} "
            "V/cell with the motors neutral). Charge it first."
        )

    # The 1 Hz cycle.

    async def cycle(self) -> None:
        """One evaluation; awaits only ESC power requests and the power-off."""
        now = self._clock()
        state = self._state
        charger = state.charger
        telemetry = charger.fresh_telemetry(now)
        power = esc_power_state(state.esc_power_telemetry, now)
        self._update_ema(telemetry, charger.updated_at)
        presence = battery_presence(charger, now)
        if presence != BatteryPresence.PRESENT:
            self._present_since = None
        elif self._present_since is None:
            self._present_since = now
        evaluating = self._evaluate(now, telemetry, charger, power)
        self._track_invalid(now, telemetry, charger)
        state.battery = BatteryStatus(
            updated_at=now,
            level=self._level,
            presence=presence,
            present_since=self._present_since,
            voltage_ema_v=self._ema_v,
            cell_voltage_v=self._cell_v,
            evaluating=evaluating,
        )
        await self._act(now, telemetry, presence, power)

    def _update_ema(self, telemetry: ChargerTelemetry | None, sample_at: float) -> None:
        if telemetry is None:
            self._ema_v = None
            return
        if self._ema_v is None:
            self._ema_v = telemetry.vbat_v
        elif sample_at > self._ema_sample_at:
            weight = 1.0 - math.exp(-(sample_at - self._ema_sample_at) / EMA_TAU_S)
            self._ema_v += weight * (telemetry.vbat_v - self._ema_v)
        self._ema_sample_at = sample_at

    def _evaluate(
        self,
        now: float,
        telemetry: ChargerTelemetry | None,
        charger: ChargerStatus,
        power: EscPowerState,
    ) -> bool:
        """Advance the vbat windows and the level; False when vbat does not count."""
        neutral = motor_commands_neutral(
            self._state, wall_now=self._wall_clock(), power=power
        )
        if not neutral:
            self._neutral_since = None
        elif self._neutral_since is None:
            self._neutral_since = now
        if telemetry is None or not self._settled(now):
            # Keep the level; every window starts again from the next sample.
            self._windows.reset()
            self._evaluated_at = None
            return False
        last, self._evaluated_at = self._evaluated_at, now
        self._cell_v = telemetry.vbat_cell_v
        self._windows.add(
            telemetry.vbat_cell_v,
            step=0.0 if last is None else min(max(now - last, 0.0), MAX_STEP_S),
            poe=charger.poe_present is True,
        )
        previous, self._level = self._level, self._windows.next_level(self._level)
        if self._level != previous:
            self._level_changed(previous, telemetry)
        return True

    def _settled(self, now: float) -> bool:
        """Motors neutral for MOTORS_NEUTRAL_SETTLE_S: vbat shows the pack, no sag."""
        return (
            self._neutral_since is not None
            and now - self._neutral_since >= MOTORS_NEUTRAL_SETTLE_S
        )

    def _level_changed(
        self, previous: BatteryLevel, telemetry: ChargerTelemetry
    ) -> None:
        vbat = _describe_vbat(telemetry)
        level = self._level
        if level > previous:
            log_warn(f"Battery {level.name.lower()}: {vbat}")
        else:
            log_info(f"Battery back to {level.name.lower()}: {vbat}")
        if level == BatteryLevel.WARN and previous == BatteryLevel.NORMAL:
            self._notify(
                ToastVariant.WARN,
                "Battery low",
                f"{vbat}, below {BATTERY_WARN_V_PER_CELL:.1f} V/cell with the motors "
                "neutral. Plan to surface and charge.",
            )
        elif level < BatteryLevel.CRITICAL <= previous:
            self._critical_toast_at = float("-inf")
            self._notify(
                ToastVariant.INFO,
                "Battery no longer critical",
                f"{vbat}. ESC power can be switched on again.",
            )

    def _track_invalid(
        self, now: float, telemetry: ChargerTelemetry | None, charger: ChargerStatus
    ) -> None:
        if telemetry is not None:
            if self._invalid_warned:
                log_info("Battery voltage measured again")
            self._invalid_since = None
            self._invalid_warned = False
            return
        if self._invalid_since is None:
            self._invalid_since = now
        if self._invalid_warned or now - self._invalid_since < INVALID_WARN_S:
            return
        self._invalid_warned = True
        problem = charger.telemetry_problem or "charger status is stale"
        description = (
            f"The LTC4162 telemetry is invalid ({problem}). Low-battery warnings and "
            "the automatic power-off are inactive until it is valid again."
        )
        log_warn(f"Battery voltage unknown: {description}")
        self._notify(ToastVariant.WARN, "Battery voltage unknown", description)

    # Side effects: toasts, ESC power off, power-off.

    async def _act(
        self,
        now: float,
        telemetry: ChargerTelemetry | None,
        presence: BatteryPresence,
        power: EscPowerState,
    ) -> None:
        await self._pack_pulled_on_poe(now, presence, power)
        if self._level >= BatteryLevel.CRITICAL:
            self._critical(now)
        if self._level == BatteryLevel.SHUTDOWN and telemetry is not None:
            await self._shutdown(now, telemetry)

    async def _pack_pulled_on_poe(
        self, now: float, presence: BatteryPresence, power: EscPowerState
    ) -> None:
        esc_power = self._state.esc_power
        # A low vbat under thrust is sag, not a pulled pack (wiki 8.1).
        pulled = presence == BatteryPresence.ABSENT and (
            self._settled(now) or pack_missing_by_fault(self._state.charger)
        )
        if (
            not pulled
            or power not in (EscPowerState.RAMPING, EscPowerState.ON)
            or esc_power is None
        ):
            self._auto_off_at = float("-inf")
            return
        if now < self._auto_off_at:
            return
        first = self._auto_off_at == float("-inf")
        self._auto_off_at = now + AUTO_OFF_RETRY_S
        message = "ESC power switched off"
        reason = (
            "The battery was removed while on PoE. PoE alone cannot feed the "
            "thrusters; plug in the battery, then switch ESC power on again."
        )
        log_warn(f"{message}: {reason}")
        if first:
            self._notify(ToastVariant.WARN, message, reason)
        await esc_power.request_off(notify=False)

    def _critical(self, now: float) -> None:
        if now - self._critical_toast_at < CRITICAL_TOAST_INTERVAL_S:
            return
        self._critical_toast_at = now
        cell = "" if self._cell_v is None else f"{self._cell_v:.2f} V/cell, "
        self._notify(
            ToastVariant.ERROR,
            "Battery critical",
            f"{cell}below {BATTERY_CRITICAL_V_PER_CELL:.1f} V/cell with the motors "
            "neutral. Surface and charge now. ESC power cannot be switched on again; "
            "it stays on until you switch it off.",
        )

    async def _shutdown(self, now: float, telemetry: ChargerTelemetry) -> None:
        if not AUTO_POWER_OFF_ENABLED or now < self._shutdown_retry_at:
            return
        lock = self._state.mcu_flash_lock
        if lock.locked():
            if not self._shutdown_postponed:
                log_warn("Battery empty: power-off waits for the firmware update")
                self._shutdown_postponed = True
            return
        self._shutdown_postponed = False
        if self._state.esc_firmware_recovery_required:
            self._postpone_for_recovery(now, telemetry)
            return
        self._recovery_toast_at = float("-inf")
        # Free and uncontended, so this takes the lock without yielding: no flash
        # can start between the check above and the power-off (wiki 8.2).
        async with lock:
            await self._shutdown_chain(now, telemetry)

    def _postpone_for_recovery(self, now: float, telemetry: ChargerTelemetry) -> None:
        """No power-off while the ESC recovery journal is set (wiki 8.1, 11.2).

        A halt drops the MCU's 3.3 V: it forgets its RAM recovery state and GP14,
        and no software path can finish the recovery afterwards. The pack runs
        on to its BMS cut-off instead; PoE or a successful recovery ends this.
        """
        vbat = _describe_vbat(telemetry)
        if self._recovery_toast_at == float("-inf"):
            log_error(f"Battery empty ({vbat}): {RECOVERY_BLOCKS_POWER_OFF}")
        if now < self._recovery_toast_at:
            return
        self._recovery_toast_at = now + SHUTDOWN_RETRY_S
        self._notify(
            ToastVariant.ERROR, "Battery empty", f"{vbat}. {RECOVERY_BLOCKS_POWER_OFF}"
        )

    async def _shutdown_chain(self, now: float, telemetry: ChargerTelemetry) -> None:
        self._shutdown_retry_at = now + SHUTDOWN_RETRY_S
        vbat = _describe_vbat(telemetry)
        description = (
            f"{vbat}, below {BATTERY_SHUTDOWN_V_PER_CELL:.1f} V/cell for "
            f"{SHUTDOWN_WINDOW_S:.0f} s without PoE. Thrusters neutral, ESC power "
            "off, then the CM5 powers off."
        )
        log_error(f"Battery empty, powering off: {description}")
        self._notify(ToastVariant.ERROR, "Battery empty: powering off", description)
        self._state.thrusters.direction_vector = None
        esc_power = self._state.esc_power
        if esc_power is not None:
            if not await esc_power.request_off(notify=False):
                # A later off (operator, flash prepare) may have replaced it.
                log_warn("ESC power off before the power-off failed; waiting anyway")
            if not await esc_power.wait_until_off():
                log_warn("ESC power did not read off in time; powering off anyway")
        if self._state.charger.poe_present:
            log_info("PoE arrived during the shutdown chain; not powering off")
            return
        try:
            await self._power_off()
        except Exception as error:
            log_error(f"Power-off failed: {error}")
            self._notify(
                ToastVariant.ERROR,
                "Power-off failed",
                f"{error}. Switch the ROV off with the magnet switch.",
            )

    async def run(self) -> None:
        """Run the policy once per second."""
        while True:
            started = self._clock()
            try:
                await self.cycle()
            except Exception as error:
                # A battery fault must not end the service's task group.
                log_error(f"Battery monitor cycle failed: {error}")
            elapsed = self._clock() - started
            await self._async_sleep(min(max(MONITOR_INTERVAL_S - elapsed, 0.0), 1.0))
