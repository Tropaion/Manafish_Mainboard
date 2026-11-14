"""EscPowerController: enable checks, BUSY retries, off waits, adoption and toasts."""

import asyncio
from collections.abc import Awaitable, Callable
import struct
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest

from rov_firmware import esc_power, esc_recovery, pico_protocol as wire
from rov_firmware.esc_power import EscPowerController, EscPowerState
from rov_firmware.models.toast import ToastVariant
from rov_firmware.pico_control import TELEMETRY_TIMEOUT, PicoControl
from rov_firmware.serial import SerialManager


class FakeClock:
    """Monotonic clock whose sleep advances time; the MCU keeps reporting meanwhile."""

    def __init__(self, state, now=1000.0):
        self.state = state
        self.now = now
        self.reporting = True
        self.slept: list[float] = []
        self.during_sleep: Callable[[], Awaitable[object]] | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        telemetry = self.state.esc_power_telemetry
        if self.reporting and telemetry.wire_state is not None:
            telemetry.reported_at = self.now
        hook, self.during_sleep = self.during_sleep, None
        if hook is not None:
            await hook()
        await asyncio.sleep(0)


class FakePico:
    def __init__(self):
        self.session_active = True
        self.esc_power_supported = True
        self.results: list[int | BaseException] = []
        self.requests: list[bool] = []
        self.after_request: Callable[[bool], None] | None = None
        # Holds every request until set, like send_loop holding PicoControl._gate.
        self.gate: asyncio.Event | None = None

    async def request_esc_power(
        self, *, on: bool, before_send: Callable[[], None] | None = None
    ) -> int:
        if self.gate is not None:
            await self.gate.wait()
        if before_send is not None:
            before_send()
        self.requests.append(on)
        if self.after_request is not None:
            self.after_request(on)
        await asyncio.sleep(0)  # the real endpoint awaits its ACK here
        result = self.results.pop(0) if self.results else wire.APPLIED
        if isinstance(result, BaseException):
            raise result
        return result


def report(env, wire_state, *, since=60.0):
    telemetry = env.state.esc_power_telemetry
    telemetry.wire_state = wire_state
    telemetry.reported_at = env.clock.now
    telemetry.changed_at = env.clock.now - since


@pytest.fixture
def env(rov_state):
    clock = FakeClock(rov_state)
    pico = FakePico()
    rov_state.pico = cast(PicoControl, pico)
    toasts: list[tuple[ToastVariant, str, str | None]] = []
    controller = EscPowerController(
        rov_state,
        clock=clock,
        sleep=clock.sleep,
        notify=lambda variant, message, description: toasts.append(
            (variant, message, description)
        ),
    )
    rov_state.esc_power = controller
    result = SimpleNamespace(
        state=rov_state, clock=clock, pico=pico, toasts=toasts, controller=controller
    )
    report(result, wire.ESC_POWER_OFF_READY)
    return result


@pytest.mark.parametrize(
    ("wire_state", "expected"),
    [
        (None, "unavailable"),
        (0, "unavailable"),
        (1, "off"),
        (2, "off"),
        (3, "ramping"),
        (4, "on"),
        (5, "stopping"),
        (6, "unavailable"),
    ],
)
def test_state_map_from_health_bits(env, wire_state, expected):
    report(env, wire_state)
    assert env.controller.reading == expected
    assert env.controller.power_state == EscPowerState(expected)


def test_stale_report_reads_unavailable(env):
    report(env, wire.ESC_POWER_ON)
    env.clock.now += TELEMETRY_TIMEOUT * 2
    assert env.controller.power_state == EscPowerState.UNAVAILABLE


def test_request_on_sends_one_request_and_tells_the_operator_about_arming(env):
    assert asyncio.run(env.controller.request_on())
    assert env.pico.requests == [True]
    assert env.toasts == [
        (
            ToastVariant.INFO,
            "ESC power switching on",
            "Thrust is possible about 15 s after switch-on, once ESC power reads on.",
        )
    ]


@pytest.mark.parametrize("wire_state", [wire.ESC_POWER_STARTING, wire.ESC_POWER_ON])
def test_request_on_while_powered_sends_nothing(env, wire_state):
    report(env, wire_state)
    assert asyncio.run(env.controller.request_on())
    assert env.pico.requests == []
    assert env.toasts == []


def _block_recovery(env):
    env.state.esc_firmware_recovery_required = True


def _block_no_pico(env):
    env.state.pico = None


def _block_no_session(env):
    env.pico.session_active = False


def _block_stale(env):
    env.clock.now += 1


def _block_feature(env):
    env.pico.esc_power_supported = False


def _block_input(env):
    env.state.thrusters.direction_vector = np.ones(8, dtype=np.float32)


def _block_depth_hold(env):
    env.state.system_status.depth_hold = True


def _block_thruster_test(env):
    env.state.thrusters.test_thruster = 2


def _block_flash_lock(env):
    asyncio.run(env.state.mcu_flash_lock.acquire())


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (_block_recovery, "ESC firmware recovery is pending"),
        (_block_no_pico, "not connected"),
        (_block_no_session, "not connected"),
        (_block_stale, "not connected"),
        (_block_feature, "feature 0x20"),
        (_block_input, "thrusters stopped: pilot input is not neutral"),
        (_block_depth_hold, "thrusters stopped: depth hold is active"),
        (_block_thruster_test, "thrusters stopped: a thruster test is active"),
        (_block_flash_lock, "firmware update or the low-battery power-off is running"),
    ],
)
def test_enable_checks_refuse_with_a_plain_text_toast(env, block, reason):
    block(env)
    assert not asyncio.run(env.controller.request_on())
    assert env.pico.requests == []
    assert len(env.toasts) == 1
    variant, message, description = env.toasts[0]
    assert variant == ToastVariant.ERROR
    assert message == "ESC power stays off"
    assert description is not None
    assert reason in description


def test_injected_blockers_run_after_the_builtin_checks(env):
    calls = []

    def charger():
        calls.append("charger")
        return "Charger unreadable, ESC power blocked."

    controller = EscPowerController(
        env.state,
        clock=env.clock,
        sleep=env.clock.sleep,
        notify=lambda variant, message, description: env.toasts.append(
            (variant, message, description)
        ),
        blockers=[charger],
    )
    assert not asyncio.run(controller.request_on())
    assert env.toasts[0][2] == "Charger unreadable, ESC power blocked."
    assert env.pico.requests == []

    env.toasts.clear()
    calls.clear()
    env.state.esc_firmware_recovery_required = True
    assert not asyncio.run(controller.request_on())
    assert calls == []  # the first failing check wins
    assert env.toasts[0][2] == esc_power.RECOVERY_PENDING_ON


def test_constructor_blockers_are_checked(env):
    controller = EscPowerController(
        env.state,
        clock=env.clock,
        sleep=env.clock.sleep,
        notify=lambda *_args: None,
        blockers=[lambda: "Battery critical."],
    )
    assert controller.enable_blocker() == "Battery critical."


def test_busy_during_off_hold_waits_out_the_remaining_hold(env):
    report(env, wire.ESC_POWER_OFF_HOLD, since=3.0)
    env.pico.results = [wire.BUSY, wire.APPLIED]
    assert asyncio.run(env.controller.request_on())
    assert env.pico.requests == [True, True]
    assert env.clock.slept == [
        pytest.approx(esc_power.OFF_HOLD_S - 3.0 + esc_power.HOLD_MARGIN_S)
    ]
    assert env.toasts[-1][1] == "ESC power switching on"


def test_busy_while_stopping_waits_for_stop_and_hold(env):
    report(env, wire.ESC_POWER_STOPPING, since=0.5)
    env.pico.results = [wire.BUSY, wire.APPLIED]
    assert asyncio.run(env.controller.request_on())
    expected = esc_power.STOP_S - 0.5 + esc_power.OFF_HOLD_S + esc_power.HOLD_MARGIN_S
    assert env.clock.slept == [pytest.approx(expected)]


def test_busy_without_a_hold_retries_on_the_short_timer(env):
    env.pico.results = [wire.BUSY, wire.BUSY, wire.APPLIED]
    assert asyncio.run(env.controller.request_on())
    assert env.clock.slept == [esc_power.BUSY_RETRY_S] * 2


def test_busy_until_the_deadline_gives_up_with_a_toast(env):
    env.pico.results = [wire.BUSY] * 100
    assert not asyncio.run(env.controller.request_on())
    assert sum(env.clock.slept) <= esc_power.BUSY_DEADLINE_S
    assert len(env.pico.requests) == len(env.clock.slept) + 1
    assert env.toasts[-1][0] == ToastVariant.ERROR
    assert "busy" in str(env.toasts[-1][2])


def test_new_blocker_during_busy_wait_stops_the_retry(env):
    env.pico.results = [wire.BUSY, wire.APPLIED]

    async def pilot_moves():
        env.state.thrusters.direction_vector = np.ones(8, dtype=np.float32)

    env.clock.during_sleep = pilot_moves
    assert not asyncio.run(env.controller.request_on())
    assert env.pico.requests == [True]
    assert "pilot input is not neutral" in str(env.toasts[-1][2])


def test_off_during_a_busy_wait_replaces_the_pending_on(env):
    report(env, wire.ESC_POWER_OFF_HOLD, since=0.0)
    env.pico.results = [wire.BUSY, wire.APPLIED, wire.APPLIED]
    outcome = {}

    async def operator_presses_off():
        outcome["off"] = await env.controller.request_off()

    env.clock.during_sleep = operator_presses_off
    assert not asyncio.run(env.controller.request_on())
    assert outcome["off"]
    assert env.pico.requests == [True, False]
    assert env.toasts == []


def test_off_queued_behind_an_applied_on_wins(env):
    async def run():
        pressed_off = []

        def queue_off(on):
            if on:
                pressed_off.append(asyncio.create_task(env.controller.request_off()))

        env.pico.after_request = queue_off
        on_result = await env.controller.request_on()
        off_result = await pressed_off[0]
        return on_result, off_result

    on_result, off_result = asyncio.run(run())
    assert env.pico.requests == [True, False]
    assert not on_result
    assert off_result
    # No "switching on" toast for a request the operator already replaced.
    assert env.toasts == []
    assert not env.controller._wants_on


def test_on_press_while_an_off_is_in_flight_is_refused_and_the_off_counts(env):
    report(env, wire.ESC_POWER_ON)
    env.controller.observe()

    async def run():
        env.pico.gate = asyncio.Event()
        off = asyncio.create_task(env.controller.request_off())
        await asyncio.sleep(0)  # the off now waits for the gate
        on_result = await env.controller.request_on()
        env.pico.gate.set()
        return on_result, await off

    on_result, off_result = asyncio.run(run())
    assert not on_result
    assert off_result
    assert env.pico.requests == [False]
    assert env.toasts == [
        (ToastVariant.ERROR, "ESC power stays off", esc_power.OFF_IN_FLIGHT)
    ]
    assert not env.controller._wants_on
    # The MCU stops for the off this host asked for: no "unrequested off" warning.
    report(env, wire.ESC_POWER_STOPPING)
    env.controller.observe()
    assert len(env.toasts) == 1


def test_an_on_request_is_checked_again_once_it_holds_the_gate(env):
    async def run():
        env.pico.gate = asyncio.Event()
        on = asyncio.create_task(env.controller.request_on())
        await asyncio.sleep(0)  # the on passed its checks and waits for the gate
        env.state.thrusters.test_thruster = 2
        env.pico.gate.set()
        return await on

    assert not asyncio.run(run())
    assert env.pico.requests == []
    assert len(env.toasts) == 1
    variant, message, reason = env.toasts[0]
    assert (variant, message) == (ToastVariant.ERROR, "ESC power stays off")
    assert reason is not None
    assert reason.startswith("Switch ESC power on only with the thrusters stopped")
    assert not env.controller._wants_on


def test_an_off_pressed_while_an_on_waits_for_the_gate_keeps_the_on_unsent(env):
    async def run():
        env.pico.gate = asyncio.Event()
        on = asyncio.create_task(env.controller.request_on())
        await asyncio.sleep(0)
        off = asyncio.create_task(env.controller.request_off())
        await asyncio.sleep(0)
        env.pico.gate.set()
        return await on, await off

    assert asyncio.run(run()) == (False, True)
    assert env.pico.requests == [False]
    assert env.toasts == []


def test_a_refused_or_redundant_on_press_replaces_no_request(env):
    intent = env.controller._intent
    env.state.thrusters.test_thruster = 2
    assert not asyncio.run(env.controller.request_on())
    env.state.thrusters.test_thruster = None
    report(env, wire.ESC_POWER_ON)
    assert asyncio.run(env.controller.request_on())
    assert env.controller._intent == intent
    assert env.pico.requests == []


def test_an_off_after_an_off_still_works_once_the_first_ends(env):
    report(env, wire.ESC_POWER_ON)

    async def run():
        env.pico.gate = asyncio.Event()
        first = asyncio.create_task(env.controller.request_off(notify=False))
        await asyncio.sleep(0)
        second = asyncio.create_task(env.controller.request_off())
        await asyncio.sleep(0)
        env.pico.gate.set()
        return await first, await second

    first, second = asyncio.run(run())
    # The newer off replaces the older one; the older returns False, and a
    # caller such as the shutdown chain still waits for off (battery.py).
    assert (first, second) == (False, True)
    assert env.controller._offs_in_flight == 0
    report(env, wire.ESC_POWER_OFF_READY)
    assert asyncio.run(env.controller.request_on())
    assert env.pico.requests == [False, False, True]


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (ConnectionError("session gone"), "session gone"),
        (TimeoutError(), "no answer in time"),
        (RuntimeError("result 4"), "result 4"),
    ],
)
def test_request_on_errors_are_toasted(env, error, detail):
    env.pico.results = [error]
    assert not asyncio.run(env.controller.request_on())
    assert env.toasts[-1][1] == "ESC power stays off"
    assert env.toasts[-1][2] == f"The ESC power request failed: {detail}."
    assert not env.controller._wants_on


@pytest.mark.parametrize(
    ("result", "reason"),
    [(wire.UNSUPPORTED, "no ESC power switch"), (wire.INVALID, "invalid")],
)
def test_request_on_rejections_are_toasted(env, result, reason):
    env.pico.results = [result]
    assert not asyncio.run(env.controller.request_on())
    assert reason in str(env.toasts[-1][2])


def test_request_off_applied(env):
    report(env, wire.ESC_POWER_ON)
    assert asyncio.run(env.controller.request_off())
    assert env.pico.requests == [False]
    assert env.toasts == []


def test_request_off_without_a_switch_counts_as_off(env):
    env.pico.results = [wire.UNSUPPORTED]
    assert asyncio.run(env.controller.request_off())


def test_request_off_during_recovery_is_refused_without_a_request(env):
    env.state.esc_firmware_recovery_required = True
    assert not asyncio.run(env.controller.request_off())
    assert env.pico.requests == []
    assert env.toasts == [
        (ToastVariant.ERROR, "ESC power stays on", esc_power.RECOVERY_PENDING_OFF)
    ]


def test_request_off_busy_until_deadline_and_quiet_failures(env):
    env.pico.results = [wire.BUSY] * 100
    assert not asyncio.run(env.controller.request_off())
    assert "ESC firmware recovery" in str(env.toasts[-1][2])

    env.toasts.clear()
    env.pico.results = [ConnectionError("no session")]
    assert not asyncio.run(env.controller.request_off(notify=False))
    assert env.toasts == []


@pytest.mark.parametrize(
    "wire_state",
    [wire.ESC_POWER_ABSENT, wire.ESC_POWER_OFF_HOLD, wire.ESC_POWER_OFF_READY],
)
def test_wait_until_off_returns_at_once_when_off(env, wire_state):
    report(env, wire_state)
    assert asyncio.run(env.controller.wait_until_off())
    assert env.clock.slept == []


def test_wait_until_off_follows_stopping_to_off(env):
    report(env, wire.ESC_POWER_STOPPING, since=0.0)

    async def run():
        waiter = asyncio.create_task(env.controller.wait_until_off())
        for _ in range(10):
            await asyncio.sleep(0)
        report(env, wire.ESC_POWER_OFF_HOLD, since=0.0)
        return await waiter

    assert asyncio.run(run())
    assert sum(env.clock.slept) < esc_power.OFF_WAIT_S


def test_wait_until_off_times_out_while_the_mcu_reports_on(env):
    report(env, wire.ESC_POWER_ON)
    assert not asyncio.run(env.controller.wait_until_off())
    assert sum(env.clock.slept) == pytest.approx(esc_power.OFF_WAIT_S, abs=0.1)


def test_wait_until_off_accepts_a_silent_mcu(env):
    report(env, wire.ESC_POWER_ON)
    env.clock.now += esc_power.SILENCE_S
    assert asyncio.run(env.controller.wait_until_off())
    assert env.clock.slept == []

    # Never heard from the MCU at all.
    env.state.esc_power_telemetry.wire_state = None
    env.state.esc_power_telemetry.reported_at = 0.0
    assert asyncio.run(env.controller.wait_until_off())


def test_wait_until_off_waits_past_the_timeout_until_silence_is_long_enough(env):
    report(env, wire.ESC_POWER_ON)
    env.clock.reporting = False
    env.clock.now += 1.0  # the MCU went quiet a second ago
    assert asyncio.run(env.controller.wait_until_off(timeout_s=2.0))
    assert sum(env.clock.slept) == pytest.approx(esc_power.SILENCE_S - 1.0, abs=0.1)


def test_prepare_for_mcu_reset_sends_neutral_and_off_then_waits(env):
    report(env, wire.ESC_POWER_ON)

    def mcu_stops(on):
        if not on:
            report(env, wire.ESC_POWER_OFF_HOLD, since=0.0)

    env.pico.after_request = mcu_stops
    assert asyncio.run(env.controller.prepare_for_mcu_reset()) is None
    assert env.state.thrusters.direction_vector is None
    assert env.pico.requests == [False]
    assert env.toasts == []


def test_prepare_for_mcu_reset_refuses_when_the_switch_stays_on(env):
    report(env, wire.ESC_POWER_ON)
    reason = asyncio.run(env.controller.prepare_for_mcu_reset())
    assert reason is not None
    assert "did not switch off" in reason
    assert "reads on" in reason


def test_prepare_for_mcu_reset_proceeds_with_a_silent_mcu(env):
    env.pico.results = [ConnectionError("no session")]
    report(env, wire.ESC_POWER_ON)
    env.clock.now += esc_power.SILENCE_S
    assert asyncio.run(env.controller.prepare_for_mcu_reset()) is None
    assert env.toasts == []


def test_no_mcu_reset_or_flash_while_esc_recovery_is_pending(env):
    env.state.esc_firmware_recovery_required = True
    report(env, wire.ESC_POWER_ON)
    assert (
        asyncio.run(env.controller.prepare_for_mcu_reset())
        == esc_recovery.MCU_RESET_REFUSAL
    )
    assert env.pico.requests == []


def test_tools_see_the_reset_refusal_through_the_journal(rov_state):
    assert not rov_state.esc_firmware_recovery_required
    assert esc_recovery.mcu_reset_refusal() is None
    esc_recovery.mark_recovery_required("2.21.0")
    assert esc_recovery.mcu_reset_refusal() == esc_recovery.MCU_RESET_REFUSAL
    esc_recovery.clear_recovery_required()
    assert esc_recovery.mcu_reset_refusal() is None


@pytest.mark.parametrize("wire_state", [wire.ESC_POWER_STARTING, wire.ESC_POWER_ON])
def test_service_start_adopts_a_running_switch_without_sending_on(env, wire_state):
    report(env, wire_state)

    async def run():
        monitor = asyncio.create_task(env.controller.run())
        for _ in range(20):
            await asyncio.sleep(0)
        monitor.cancel()

    asyncio.run(run())
    assert env.pico.requests == []
    assert env.toasts == []
    assert env.controller._wants_on


def test_unrequested_off_after_on_warns_once(env):
    report(env, wire.ESC_POWER_ON)
    env.controller.observe()
    report(env, wire.ESC_POWER_STOPPING)
    env.controller.observe()
    report(env, wire.ESC_POWER_OFF_HOLD)
    env.controller.observe()
    assert [toast[:2] for toast in env.toasts] == [
        (ToastVariant.WARN, "ESC power switched off")
    ]
    assert env.pico.requests == []


def test_mcu_restart_while_on_warns_and_never_re_requests(env):
    report(env, wire.ESC_POWER_ON)
    env.controller.observe()
    env.state.esc_power_telemetry.expire()  # reconnect
    env.controller.observe()
    report(env, wire.ESC_POWER_OFF_HOLD)
    env.controller.observe()
    assert len(env.toasts) == 1
    assert env.pico.requests == []


def test_reconnect_that_finds_it_still_on_is_quiet(env):
    report(env, wire.ESC_POWER_ON)
    env.controller.observe()
    env.state.esc_power_telemetry.expire()
    env.controller.observe()
    report(env, wire.ESC_POWER_ON)
    env.controller.observe()
    assert env.toasts == []


def test_operator_off_is_not_reported_as_unexpected(env):
    report(env, wire.ESC_POWER_ON)
    env.controller.observe()
    assert asyncio.run(env.controller.request_off())
    report(env, wire.ESC_POWER_STOPPING)
    env.controller.observe()
    assert env.toasts == []


def _ack(frame, result):
    return wire.Frame(
        kind=wire.ACK,
        session=frame.session,
        sequence=frame.sequence,
        payload=struct.pack("<BBHII", frame.kind, result, 0, 0, 0),
    )


def test_busy_from_the_real_endpoint_keeps_the_session_and_uses_fresh_frames(
    rov_state, monkeypatch
):
    control = PicoControl(rov_state, SerialManager(rov_state))
    control._reset_connection()
    control.session = 77
    control._negotiated = True
    control.features = 0x3F
    rov_state.pico = control
    clock = FakeClock(rov_state)
    frames = []

    async def write(packet):
        frame = wire.decode(packet)
        frames.append(frame)
        control.receive(_ack(frame, wire.BUSY if len(frames) == 1 else wire.APPLIED))

    monkeypatch.setattr(control, "_write", write)
    telemetry = rov_state.esc_power_telemetry
    telemetry.wire_state = wire.ESC_POWER_OFF_HOLD
    telemetry.reported_at = clock.now
    telemetry.changed_at = clock.now - 4.0
    controller = EscPowerController(
        rov_state, clock=clock, sleep=clock.sleep, notify=lambda *_args: None
    )
    assert asyncio.run(controller.request_on())
    assert [frame.kind for frame in frames] == [wire.ESC_POWER] * 2
    assert frames[1].sequence == frames[0].sequence + 1
    assert control.session == 77
    assert control._negotiated
    assert clock.slept == [pytest.approx(1.0 + esc_power.HOLD_MARGIN_S)]


def test_monitor_survives_a_fault(env, monkeypatch):
    calls = []

    def broken():
        calls.append(1)
        msg = "injected"
        raise RuntimeError(msg)

    monkeypatch.setattr(env.controller, "observe", broken)

    async def run():
        monitor = asyncio.create_task(env.controller.run())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not monitor.done()
        monitor.cancel()

    asyncio.run(run())
    assert len(calls) > 1
