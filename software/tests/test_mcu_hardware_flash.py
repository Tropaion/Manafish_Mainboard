"""Every MCU flash: host duties, then hardware BOOTSEL, then picotool without -f."""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from rov_firmware import board as board_module
from rov_firmware.board import Board
from rov_firmware.esc_power import EscPowerController
from rov_firmware.mcu_gpio import McuGpioError
from rov_firmware.models.config import McuBoard
from rov_firmware.websocket.receive import mcu


FIRMWARE = Path("/home/pi/mcu-firmware/mainboard-v1.0.4-mb.1.uf2")


class FakeProcess:
    def __init__(self, output: str, rc: int = 0) -> None:
        self.stdout = io.StringIO(output)
        self._rc = rc

    def poll(self) -> int:
        return self._rc


@pytest.fixture
def flash(rov_state, monkeypatch):
    """A flash environment that records the order of every step."""
    events: list[tuple[object, ...]] = []
    toasts: list[dict] = []
    boards: list[McuBoard] = []

    def resolve(board):
        boards.append(board)
        return FIRMWARE, "1.0.4-mb.1"

    async def prepare():
        events.append(("prepare", rov_state.mcu_flashing))

    def enter_bootsel():
        events.append(("bootsel", rov_state.mcu_flashing))

    def popen(args, **_kwargs):
        events.append(("picotool", rov_state.mcu_flashing, list(args)))
        return FakeProcess("Loading into Flash: 100%\nVerifying Flash: 100%\n  OK\n")

    controller = EscPowerController(rov_state, notify=lambda *_args: None)
    monkeypatch.setattr(controller, "prepare_for_mcu_reset", prepare)
    rov_state.esc_power = controller
    monkeypatch.setattr(mcu, "resolve_mcu_firmware", resolve)
    monkeypatch.setattr(mcu, "detect_connected_mcu_boards", lambda: {McuBoard.PICO2})
    monkeypatch.setattr(mcu, "_resolve_picotool_path", lambda: "/bin/picotool")
    monkeypatch.setattr(mcu, "enter_bootsel", enter_bootsel)
    monkeypatch.setattr(mcu.subprocess, "Popen", popen)
    monkeypatch.setattr(mcu, "toast_error", lambda **kwargs: toasts.append(kwargs))
    monkeypatch.setattr(mcu, "toast_loading", lambda **_kwargs: None)
    monkeypatch.setattr(mcu, "toast_success", lambda **_kwargs: None)

    return SimpleNamespace(
        state=rov_state,
        events=events,
        toasts=toasts,
        boards=boards,
        controller=controller,
    )


def _flash(env, board=McuBoard.PICO2):
    return asyncio.run(mcu.flash_mcu_firmware(env.state, board, show_toasts=True))


def test_host_duties_then_bootsel_then_picotool_without_force(flash):
    assert _flash(flash)

    assert flash.events == [
        ("prepare", False),
        ("bootsel", True),
        ("picotool", True, ["/bin/picotool", "load", "-v", "-x", str(FIRMWARE)]),
    ]
    assert not flash.state.mcu_flashing
    assert flash.toasts == []


def test_pico_flash_request_flashes_the_mainboard_image(flash):
    assert _flash(flash, McuBoard.PICO)

    assert flash.boards == [McuBoard.PICO2]


def test_refused_reset_sends_no_reset_and_explains_why(flash, monkeypatch):
    reason = "ESC power did not switch off within 5 s (it reads on)."

    async def refuse():
        flash.events.append(("prepare", flash.state.mcu_flashing))
        return reason

    monkeypatch.setattr(flash.controller, "prepare_for_mcu_reset", refuse)

    assert not _flash(flash)

    assert flash.events == [("prepare", False)]
    assert not flash.state.mcu_flashing
    assert flash.toasts[-1]["content"].description == reason


def test_bootsel_failure_skips_picotool(flash, monkeypatch):
    def no_bootsel():
        flash.events.append(("bootsel", flash.state.mcu_flashing))
        msg = "The MCU did not enumerate as BOOTSEL (2e8a:000f) within 5 s."
        raise McuGpioError(msg)

    monkeypatch.setattr(mcu, "enter_bootsel", no_bootsel)

    assert not _flash(flash)

    assert [event[0] for event in flash.events] == ["prepare", "bootsel"]
    assert not flash.state.mcu_flashing
    assert "000f" in flash.toasts[-1]["content"].description


def test_missing_picotool_switches_nothing_off(flash, monkeypatch):
    monkeypatch.setattr(mcu, "_resolve_picotool_path", lambda: None)

    assert not _flash(flash)

    assert flash.events == []


def test_missing_firmware_switches_nothing_off(flash, monkeypatch):
    monkeypatch.setattr(mcu, "resolve_mcu_firmware", lambda _board: None)

    assert not _flash(flash)

    assert flash.events == []


def test_without_a_controller_pilot_input_is_still_cleared(flash):
    flash.state.esc_power = None

    assert _flash(flash)

    assert flash.state.thrusters.direction_vector is None
    assert [event[0] for event in flash.events] == ["bootsel", "picotool"]


def test_esc_recovery_blocks_every_flash_before_any_step(flash):
    flash.state.esc_firmware_recovery_required = True

    assert not _flash(flash)

    assert flash.events == []


def test_service_start_pulses_run_for_a_stale_bootsel(rov_state, monkeypatch):
    calls: list[bool] = []
    warnings: list[str] = []

    def leave(*, recovery_pending):
        calls.append(recovery_pending)
        return "pulsed RUN once"

    monkeypatch.setattr(board_module.mcu_gpio, "leave_stale_bootsel", leave)
    monkeypatch.setattr(board_module, "log_warn", warnings.append)
    rov_state.esc_firmware_recovery_required = True

    asyncio.run(Board(rov_state).prepare())

    assert calls == [True]
    assert warnings == ["pulsed RUN once"]


def test_service_start_survives_a_gpio_failure(rov_state, monkeypatch):
    errors: list[str] = []

    def broken(**_kwargs):
        msg = "No GPIO chip labelled pinctrl-rp1"
        raise RuntimeError(msg)

    monkeypatch.setattr(board_module.mcu_gpio, "leave_stale_bootsel", broken)
    monkeypatch.setattr(board_module, "log_error", errors.append)

    asyncio.run(Board(rov_state).prepare())

    assert "pinctrl-rp1" in errors[0]
