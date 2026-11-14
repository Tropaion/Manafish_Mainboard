"""ESC firmware updates on the mainboard: ESC power ON preflight and MCU error 11."""

import asyncio
from pathlib import Path
import struct
import time
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from rov_firmware import esc_firmware, pico_protocol as wire
from rov_firmware.constants import ESC_FIRMWARE_USB_STATUS_PACKET_SIZE
from rov_firmware.esc_power import EscPowerController
from rov_firmware.pico_control import PicoControl
from rov_firmware.serial import SerialManager
from rov_firmware.websocket.receive import mcu


RELEASE = (Path("esc-v2.21.0.bin"), "2.21.0", b"validated image")


class ReadySerialManager:
    def __init__(self, state):
        self.mcu_protocol_config = ("dshot", state.rov_config.dshot_speed)

    async def ensure_connection(self):
        return True


@pytest.fixture
def board(rov_state, monkeypatch):
    """A negotiated mainboard MCU (feature 0x20) with ESC power reported as off."""
    control = PicoControl(rov_state, SerialManager(rov_state))
    control._reset_connection()
    control.session = 9
    control._negotiated = control._ready = True
    control.features = 0x3F
    rov_state.pico = control
    rov_state.system_status.thruster_control_ready = True
    rov_state.esc_power = EscPowerController(rov_state, notify=lambda *_args: None)
    rov_state.esc_power_telemetry.record(wire.ESC_POWER_OFF_READY, time.monotonic())
    monkeypatch.setattr(esc_firmware, "_resolve_validated_image", lambda: RELEASE)
    return control


def _preflight(state):
    return esc_firmware._preflight_update(
        state, cast(SerialManager, ReadySerialManager(state))
    )


@pytest.mark.parametrize(
    ("wire_state", "reading"),
    [
        (wire.ESC_POWER_OFF_HOLD, "off"),
        (wire.ESC_POWER_OFF_READY, "off"),
        (wire.ESC_POWER_STARTING, "ramping"),
        (wire.ESC_POWER_STOPPING, "stopping"),
    ],
)
def test_preflight_requires_esc_power_on(board, wire_state, reading):
    board.state.esc_power_telemetry.record(wire_state, time.monotonic())
    with pytest.raises(
        esc_firmware.EscFirmwareUpdateError,
        match=rf"ESC power must be on .*\(it reads {reading}\)",
    ):
        asyncio.run(_preflight(board.state))


def test_preflight_passes_with_esc_power_on(board):
    board.state.esc_power_telemetry.record(wire.ESC_POWER_ON, time.monotonic())
    assert asyncio.run(_preflight(board.state)) == RELEASE


def test_preflight_treats_a_stale_report_as_not_on(board):
    board.state.esc_power_telemetry.record(wire.ESC_POWER_ON, time.monotonic() - 1)
    with pytest.raises(esc_firmware.EscFirmwareUpdateError, match="unavailable"):
        asyncio.run(_preflight(board.state))


def test_pico_builds_without_the_switch_keep_the_upstream_preflight(board):
    board.features = 0x1F
    board.state.esc_power_telemetry.record(wire.ESC_POWER_ABSENT, time.monotonic())
    assert asyncio.run(_preflight(board.state)) == RELEASE


def test_recovery_retry_leaves_the_esc_power_check_to_the_mcu(board):
    # In recovery the host holds no session, so no ATTITUDE: the state is unknown.
    board.state.esc_firmware_recovery_required = True
    board.state.esc_power_telemetry.expire()
    board.state.system_status.thruster_control_ready = False
    assert asyncio.run(_preflight(board.state)) == RELEASE


def test_esc_power_check_runs_before_the_maintenance_gate(board, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(board, "_request", request)
    monkeypatch.setattr(esc_firmware, "_abort_update", AsyncMock())

    assert not asyncio.run(
        esc_firmware.flash_esc_firmware(
            board.state,
            cast(SerialManager, ReadySerialManager(board.state)),
            show_toasts=False,
        )
    )
    request.assert_not_awaited()  # no ENTER_MAINTENANCE (0x25)
    assert not board._maintenance
    assert board.session_active
    assert "ESC power must be on" in str(board.state.esc_firmware_update.error)


def _status(status, *, error=0, motor=0xFF, value=0, transaction=7):
    packet = bytearray(ESC_FIRMWARE_USB_STATUS_PACKET_SIZE)
    packet[0] = esc_firmware.ESC_FIRMWARE_USB_STATUS_START_BYTE
    packet[1] = status
    packet[2] = motor
    packet[3] = error
    struct.pack_into("<I", packet, 4, value)
    packet[8] = transaction
    packet[-1] = esc_firmware._checksum(packet[:-1])
    return bytes(packet)


class RecordingWriter:
    def __init__(self):
        self.packets = []

    def write(self, packet):
        self.packets.append(packet)

    async def drain(self):
        return None


def test_error_11_is_mapped_instead_of_unknown():
    assert esc_firmware._Error.ESC_POWER_OFF == 11
    message = esc_firmware._format_updater_failure(0xFF, 11)
    assert "unknown error" not in message
    assert "ESC power is not on" in message


def _run_refused_update(*, recovery):
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(
            _status(
                esc_firmware._Status.FAILED,
                error=wire.ESC_FIRMWARE_UPDATE_ERROR_ESC_POWER_OFF,
            )
        )
        writer = RecordingWriter()
        await esc_firmware._run_update(
            reader,
            cast(asyncio.StreamWriter, writer),
            b"\0" * 64,
            recovery=recovery,
            transaction_id=7,
        )

    asyncio.run(run())


def test_begin_refused_with_esc_power_off_says_what_to_do():
    with pytest.raises(esc_firmware.EscFirmwareUpdateError) as caught:
        _run_refused_update(recovery=False)
    message = str(caught.value)
    assert "ESC power is not on" in message
    assert "switch ESC power on" in message
    assert "No ESC was modified." in message
    assert not isinstance(caught.value, esc_firmware._PostCommitFailureError)


def test_recover_begin_refused_with_esc_power_off_explains_the_lost_recovery():
    with pytest.raises(esc_firmware.EscFirmwareUpdateError) as caught:
        _run_refused_update(recovery=True)
    message = str(caught.value)
    assert "no longer holds the ESCs in recovery" in message
    assert "No ESC was modified." in message
    assert "ESC power stays blocked" in message
    # A refused RECOVER_BEGIN keeps the host journal (it is not a post-commit result).
    assert not isinstance(caught.value, esc_firmware._PostCommitFailureError)


def test_refused_commit_keeps_the_mcu_recovery_value():
    with pytest.raises(esc_firmware._PostCommitFailureError) as caught:
        esc_firmware._handle_flash_status(
            (
                esc_firmware._Status.FAILED,
                0xFF,
                wire.ESC_FIRMWARE_UPDATE_ERROR_ESC_POWER_OFF,
                1,
                7,
                0,
            ),
            64,
            None,
        )
    assert caught.value.recovery_required
    assert "ESC power is not on" in str(caught.value)


def test_mcu_flash_is_refused_while_esc_recovery_is_pending(rov_state, monkeypatch):
    rov_state.esc_firmware_recovery_required = True
    resolve = Mock()
    monkeypatch.setattr(mcu, "resolve_mcu_firmware", resolve)
    assert not asyncio.run(
        mcu.flash_mcu_firmware(
            rov_state, rov_state.rov_config.mcu_board, show_toasts=False
        )
    )
    resolve.assert_not_called()
    assert not rov_state.mcu_flashing
