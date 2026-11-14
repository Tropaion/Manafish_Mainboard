"""CM5 side of the mainboard ESC power switch (MCU GP14, request 0x26).

The MCU owns the switch and its timers (``firmware/src/esc_power.c``). This side
decides when to ask, shows the reported state and never asks by itself: not at
service start, not after a reconnect, not after an MCU restart. A reported
ramping or on state is adopted without sending on.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
import time
from typing import TYPE_CHECKING

from . import pico_protocol as wire
from .esc_recovery import MCU_RESET_REFUSAL
from .log import log_error, log_info, log_warn
from .models.esc_power import EscPowerTelemetry
from .models.toast import ToastContent, ToastVariant
from .motor_safety import disruptive_motor_operation_blocker
from .pico_control import TELEMETRY_TIMEOUT
from .toast import toast_content


if TYPE_CHECKING:
    from .rov_state import RovState


# MCU timers (firmware/src/esc_power.h), used for the host's own retry timer.
OFF_HOLD_S = 5.0
STOP_S = 2.0
THRUST_DELAY_S = 15.0
# BUSY: a fresh request after the estimated remaining hold plus a margin, else
# after BUSY_RETRY_S; give up after BUSY_DEADLINE_S (stopping, then a full hold).
BUSY_RETRY_S = 0.5
HOLD_MARGIN_S = 0.2
BUSY_DEADLINE_S = STOP_S + OFF_HOLD_S + 3.0
# Before an MCU flash or reset: wait this long for off; no ATTITUDE for SILENCE_S
# counts as a silent (hung or absent) MCU.
OFF_WAIT_S = 5.0
SILENCE_S = 5.0
POLL_S = 0.05
MONITOR_S = 0.2
ESC_POWER_TOAST_ID = "esc-power"

RECOVERY_PENDING_ON = (
    "ESC firmware recovery is pending, so ESC power is not switched on. Retry the "
    "ESC firmware update first, and keep the ROV powered until it succeeds."
)
RECOVERY_PENDING_OFF = (
    "ESC power stays on while ESC firmware recovery is pending: the ESCs wait in "
    "their bootloader for the recovery flash. Retry the ESC firmware update."
)
OFF_IN_FLIGHT = "ESC power is switching off; switch it on again once it reads off."


class EscPowerState(StrEnum):
    """ESC power as shown to the operator (reading ``rov.escPower``)."""

    UNAVAILABLE = "unavailable"
    OFF = "off"
    RAMPING = "ramping"
    ON = "on"
    STOPPING = "stopping"


_WIRE_STATES = {
    wire.ESC_POWER_ABSENT: EscPowerState.UNAVAILABLE,
    wire.ESC_POWER_OFF_HOLD: EscPowerState.OFF,
    wire.ESC_POWER_OFF_READY: EscPowerState.OFF,
    wire.ESC_POWER_STARTING: EscPowerState.RAMPING,
    wire.ESC_POWER_ON: EscPowerState.ON,
    wire.ESC_POWER_STOPPING: EscPowerState.STOPPING,
}
# ABSENT counts as off for a reset: a build without the switch never drives GP14.
_OFF_WIRE_STATES = frozenset(
    {wire.ESC_POWER_ABSENT, wire.ESC_POWER_OFF_HOLD, wire.ESC_POWER_OFF_READY}
)
_POWERED = (EscPowerState.RAMPING, EscPowerState.ON)

type EnableBlocker = Callable[[], str | None]
type Notifier = Callable[[ToastVariant, str, str | None], None]


def plain_toast(identifier: str) -> Notifier:
    """A ``Notifier`` that shows plain-text toasts under ``identifier``."""

    def notify(variant: ToastVariant, message: str, description: str | None) -> None:
        toast_content(
            identifier=identifier,
            variant=variant,
            content=ToastContent(message=message, description=description),
            action=None,
        )

    return notify


def fresh_wire_state(telemetry: EscPowerTelemetry, now: float) -> int | None:
    """Return the reported 0..5 value, or None when no fresh ATTITUDE carries it."""
    if telemetry.wire_state is None or now - telemetry.reported_at > TELEMETRY_TIMEOUT:
        return None
    return telemetry.wire_state


def esc_power_state(telemetry: EscPowerTelemetry, now: float) -> EscPowerState:
    """Map ATTITUDE health bits 4-6 to the operator state; stale reads unavailable."""
    wire_state = fresh_wire_state(telemetry, now)
    if wire_state is None:
        return EscPowerState.UNAVAILABLE
    return _WIRE_STATES.get(wire_state, EscPowerState.UNAVAILABLE)


_toast = plain_toast(ESC_POWER_TOAST_ID)


def _result_reason(result: int, *, on: bool) -> str:
    if result == wire.BUSY and on:
        return (
            f"The MCU stayed busy for {BUSY_DEADLINE_S:.0f} s (off hold after boot or "
            "off, stopping, settings not applied yet, a protocol change or an ESC "
            "firmware update). Try again."
        )
    if result == wire.BUSY:
        return (
            f"The MCU stayed busy for {BUSY_DEADLINE_S:.0f} s: it keeps ESC power on "
            "during ESC firmware recovery. Retry the ESC firmware update."
        )
    if result == wire.UNSUPPORTED:
        return "This MCU firmware has no ESC power switch."
    if result == wire.INVALID:
        return "The MCU rejected the request as invalid."
    return f"The MCU answered with result {result}."


def _error_reason(error: Exception) -> str:
    return f"The ESC power request failed: {str(error) or 'no answer in time'}."


class _RequestEndedError(Exception):
    """A newer request replaced this one, or an enable check failed meanwhile."""

    def __init__(self, reason: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason


class EscPowerController:
    """Operator ESC power requests, enable checks and the reported state."""

    def __init__(
        self,
        state: "RovState",
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        notify: Notifier = _toast,
        blockers: Iterable[EnableBlocker] = (),
    ) -> None:
        """Create the controller; it sends nothing until an operator request."""
        self._state = state
        self._clock = clock
        self._sleep = sleep
        self._notify = notify
        self._blockers: tuple[EnableBlocker, ...] = (
            self._recovery_blocker,
            self._update_blocker,
            self._connection_blocker,
            self._feature_blocker,
            self._motor_blocker,
            *blockers,
        )
        self._intent = 0
        # Off requests still waiting for their answer; an on press is refused
        # meanwhile, so it never cancels an off the MCU may already apply.
        self._offs_in_flight = 0
        # The host expects ESC power to be, or to become, on.
        self._wants_on = False
        self._last_seen = EscPowerState.UNAVAILABLE
        self._last_known = EscPowerState.UNAVAILABLE

    @property
    def power_state(self) -> EscPowerState:
        """The reported state, unavailable without a fresh ATTITUDE frame."""
        return esc_power_state(self._state.esc_power_telemetry, self._clock())

    @property
    def reading(self) -> str:
        """Value of the ``rov.escPower`` reading."""
        return self.power_state.value

    @property
    def supported(self) -> bool:
        """The connected MCU reported CAPS feature 0x20."""
        pico = self._state.pico
        return pico is not None and pico.esc_power_supported

    def enable_blocker(self) -> str | None:
        """Return the first reason that forbids switching on, or None."""
        for blocker in self._blockers:
            reason = blocker()
            if reason is not None:
                return reason
        return None

    def _recovery_blocker(self) -> str | None:
        return (
            RECOVERY_PENDING_ON if self._state.esc_firmware_recovery_required else None
        )

    def _update_blocker(self) -> str | None:
        # Flash and ESC update paths hold the lock before mcu_flashing is set,
        # and the low-battery power-off holds it to the end.
        if self._state.mcu_flash_lock.locked():
            return "A firmware update or the low-battery power-off is running."
        return None

    def _connection_blocker(self) -> str | None:
        pico = self._state.pico
        telemetry = self._state.esc_power_telemetry
        if (
            pico is None
            or not pico.session_active
            or fresh_wire_state(telemetry, self._clock()) is None
        ):
            return (
                "The thruster MCU is not connected, or its control session is not "
                "ready yet."
            )
        return None

    def _feature_blocker(self) -> str | None:
        if self.supported:
            return None
        return (
            "This MCU firmware has no ESC power switch (CAPS feature 0x20 is missing)."
        )

    def _motor_blocker(self) -> str | None:
        blocker = disruptive_motor_operation_blocker(self._state)
        if blocker is None:
            return None
        return f"Switch ESC power on only with the thrusters stopped: {blocker}."

    def _fail(self, message: str, reason: str, *, notify: bool = True) -> None:
        log_warn(f"{message}: {reason}")
        if notify:
            self._notify(ToastVariant.ERROR, message, reason)

    async def request_on(self) -> bool:
        """Operator request: switch ESC power on when every enable check passes.

        Returns True once the MCU applied it, or when it already ramps or runs.
        Failures are toasted as plain text. Nothing calls this automatically.
        Refused while an off request waits for its answer: the off wins. Only a
        request that passes the checks replaces an earlier one; ``_send`` checks
        again right before the frame goes out.
        """
        if self._offs_in_flight:
            reason = OFF_IN_FLIGHT
        elif self.power_state in _POWERED:
            self._wants_on = True
            return True
        else:
            reason = self.enable_blocker()
        if reason is not None:
            self._fail("ESC power stays off", reason)
            return False
        self._intent += 1
        intent = self._intent
        self._wants_on = True
        try:
            result = await self._send(on=True, intent=intent)
        except _RequestEndedError as ended:
            if ended.reason is not None:
                self._wants_on = False
                self._fail("ESC power stays off", ended.reason)
            return False
        except (ConnectionError, TimeoutError, RuntimeError) as error:
            if intent == self._intent:
                self._wants_on = False
            self._fail("ESC power stays off", _error_reason(error))
            return False
        if result != wire.APPLIED:
            self._wants_on = False
            self._fail("ESC power stays off", _result_reason(result, on=True))
            return False
        log_info("ESC power on applied by the MCU")
        self._notify(
            ToastVariant.INFO,
            "ESC power switching on",
            f"Thrust is possible about {THRUST_DELAY_S:.0f} s after switch-on, once "
            "ESC power reads on.",
        )
        return True

    async def request_off(self, *, notify: bool = True) -> bool:
        """Operator or host request: switch ESC power off (the MCU stops for 2 s).

        Replaces a pending on request. Returns True when the MCU applied it or
        has no switch. ``notify=False`` only logs failures.
        """
        self._intent += 1
        intent = self._intent
        self._wants_on = False
        if self._state.esc_firmware_recovery_required:
            self._fail("ESC power stays on", RECOVERY_PENDING_OFF, notify=notify)
            return False
        self._offs_in_flight += 1
        try:
            result = await self._send(on=False, intent=intent)
        except _RequestEndedError:
            return False
        except (ConnectionError, TimeoutError, RuntimeError) as error:
            self._fail("ESC power off failed", _error_reason(error), notify=notify)
            return False
        finally:
            self._offs_in_flight -= 1
        if result in (wire.APPLIED, wire.UNSUPPORTED):
            log_info("ESC power off applied by the MCU")
            return True
        self._fail(
            "ESC power off failed", _result_reason(result, on=False), notify=notify
        )
        return False

    async def _send(self, *, on: bool, intent: int) -> int:
        """Ask until the answer is not BUSY, each time with a fresh 0x26 request.

        An on request is checked again once it holds the control gate, right
        before each frame: a newer request or a new enable blocker cancels it.
        """
        pico = self._state.pico
        if pico is None:
            msg = "Pico control is unavailable"
            raise ConnectionError(msg)

        def recheck_on() -> None:
            if intent != self._intent:
                raise _RequestEndedError
            reason = self.enable_blocker()
            if reason is not None:
                raise _RequestEndedError(reason)

        deadline = self._clock() + BUSY_DEADLINE_S
        while True:
            result = await pico.request_esc_power(
                on=on, before_send=recheck_on if on else None
            )
            if intent != self._intent:
                raise _RequestEndedError
            if result != wire.BUSY:
                return result
            delay = self._busy_delay()
            if self._clock() + delay > deadline:
                return result
            log_info(
                f"ESC power {'on' if on else 'off'} answered BUSY; "
                f"new request in {delay:.1f} s"
            )
            await self._sleep(delay)
            if intent != self._intent:
                raise _RequestEndedError

    def _busy_delay(self) -> float:
        """Remaining MCU hold, estimated from when the reported state last changed."""
        now = self._clock()
        telemetry = self._state.esc_power_telemetry
        wire_state = fresh_wire_state(telemetry, now)
        elapsed = now - telemetry.changed_at
        remaining = 0.0
        if wire_state == wire.ESC_POWER_OFF_HOLD:
            remaining = OFF_HOLD_S - elapsed
        elif wire_state == wire.ESC_POWER_STOPPING:
            remaining = max(0.0, STOP_S - elapsed) + OFF_HOLD_S
        return remaining + HOLD_MARGIN_S if remaining > 0 else BUSY_RETRY_S

    async def wait_until_off(self, timeout_s: float = OFF_WAIT_S) -> bool:
        """Wait until the MCU reports off (1/2, or 0 without a switch) or is silent.

        Returns False when the MCU still reports ramping, on or stopping at the
        timeout. Past the timeout it waits on only while no report arrives, until
        the silence reaches ``SILENCE_S`` (a hung MCU sends nothing).
        """
        deadline = self._clock() + timeout_s
        while True:
            now = self._clock()
            telemetry = self._state.esc_power_telemetry
            wire_state = fresh_wire_state(telemetry, now)
            if (
                wire_state in _OFF_WIRE_STATES
                or now - telemetry.reported_at >= SILENCE_S
            ):
                return True
            if wire_state is not None and now >= deadline:
                return False
            await self._sleep(POLL_S)

    async def prepare_for_mcu_reset(self) -> str | None:
        """Host duties before any MCU flash or reset (GPIO24/25 or picotool).

        Run it while the session is live, before ``state.mcu_flashing`` is set:
        clear pilot input (STOPPING sends neutral on the MCU anyway), 0x26 off,
        then wait for off or for a silent MCU. Returns why the MCU must not be
        reset now, or None.
        """
        if self._state.esc_firmware_recovery_required:
            return MCU_RESET_REFUSAL
        self._state.thrusters.direction_vector = None
        await self.request_off(notify=False)
        if await self.wait_until_off():
            return None
        return (
            f"ESC power did not switch off within {OFF_WAIT_S:.0f} s (it reads "
            f"{self.reading}), so the MCU was not reset."
        )

    def observe(self) -> None:
        """Log reported changes, adopt a running switch, warn on an unrequested off."""
        current = self.power_state
        if current == self._last_seen:
            return
        self._last_seen = current
        log_info(f"ESC power reads {current.value}")
        if current == EscPowerState.UNAVAILABLE:
            return
        previous, self._last_known = self._last_known, current
        if current in _POWERED and not self._wants_on:
            # Service start or reconnect: adopt it, never send on.
            log_info(f"Adopted ESC power {current.value} from the MCU; nothing sent")
            self._wants_on = True
        elif previous in _POWERED and current not in _POWERED and self._wants_on:
            self._wants_on = False
            message = "ESC power switched off"
            reason = (
                "The MCU switched ESC power off without an off request from this host "
                "(host loss or an MCU restart). It is never switched on again "
                "automatically."
            )
            log_warn(f"{message}: {reason}")
            self._notify(ToastVariant.WARN, message, reason)

    async def run(self) -> None:
        """Watch the reported state; this loop never sends a request."""
        while True:
            try:
                self.observe()
            except Exception as error:
                # A monitor fault must not end the service's task group.
                log_error(f"ESC power monitor failed: {error}")
            await self._sleep(MONITOR_S)
