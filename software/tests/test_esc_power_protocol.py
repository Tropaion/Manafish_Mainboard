"""ESC power on the CM5 protocol layer: 0x26, CAPS bit 0x20, ATTITUDE bits 4-6."""

import asyncio
import os
from pathlib import Path
import re
import struct
import time
from unittest.mock import AsyncMock

import pytest

from rov_firmware import esc_power, pico_protocol as wire
from rov_firmware.pico_control import TELEMETRY_TIMEOUT, PicoControl
from rov_firmware.serial import SerialManager


SESSION = 123


def ack(frame, result=wire.APPLIED):
    return wire.Frame(
        kind=wire.ACK,
        session=frame.session,
        sequence=frame.sequence,
        payload=struct.pack("<BBHII", frame.kind, result, 0, 0, 0),
    )


def attitude(sequence, health):
    payload = struct.pack(
        "<Q9f5I8HI",
        1000,
        *([0, 0, 0, 1] * 2),
        0.0,
        0,
        0,
        health,
        0,
        0,
        *([1000] * 8),
        0,
    )
    return wire.Frame(
        kind=wire.ATTITUDE, session=SESSION, sequence=sequence, payload=payload
    )


@pytest.fixture
def endpoint(rov_state):
    control = PicoControl(rov_state, SerialManager(rov_state))
    control._reset_connection()
    control.session = SESSION
    control._negotiated = True
    rov_state.pico = control
    return control


def _firmware_root() -> Path:
    source = os.environ.get("PICO_CONTROLLER_SOURCE")
    if not source:
        pytest.skip("Set PICO_CONTROLLER_SOURCE to compare with the firmware sources")
    return Path(source).resolve().parents[2]


def _define(text: str, name: str) -> int:
    match = re.search(rf"#define {name} (0x[0-9a-fA-F]+|\d+)u", text)
    assert match is not None, name
    return int(match.group(1), 0)


def test_esc_power_constants_match_firmware_sources():
    root = _firmware_root()
    runtime = (root / "src/control/runtime.c").read_text()
    assert _define(runtime, "MSG_ESC_POWER") == wire.ESC_POWER
    results = {
        "RESULT_APPLIED": wire.APPLIED,
        "RESULT_STAGED": wire.STAGED,
        "RESULT_INVALID": wire.INVALID,
        "RESULT_BUSY": wire.BUSY,
        "RESULT_STALE": wire.STALE,
        "RESULT_NOT_READY": wire.NOT_READY,
        "RESULT_UNSUPPORTED": wire.UNSUPPORTED,
    }
    for name, value in results.items():
        assert _define(runtime, name) == value, name
    # Mainboard builds report 0x3f, Pico builds 0x1f: the switch is exactly bit 0x20,
    # and the host never requires it.
    assert "? 0x3fu : 0x1fu" in runtime
    assert wire.FEATURE_ESC_POWER == 0x3F ^ 0x1F
    assert wire.REQUIRED_FEATURES & wire.FEATURE_ESC_POWER == 0
    assert "health |= (uint32_t)(callbacks.esc_power_state() & 7u) << 4;" in runtime
    assert (wire.ESC_POWER_HEALTH_SHIFT, wire.ESC_POWER_HEALTH_MASK) == (4, 7)

    update = (root / "src/esc_firmware/update.h").read_text()
    match = re.search(r"ESC_FIRMWARE_UPDATE_ERROR_ESC_POWER_OFF = (\d+)", update)
    assert match is not None
    assert int(match.group(1)) == wire.ESC_FIRMWARE_UPDATE_ERROR_ESC_POWER_OFF

    source = (root / "src/esc_power.c").read_text()
    table = dict(re.findall(r"\[ESC_POWER_STATE_(\w+)\] = (\d)", source))
    assert table == {
        "ABSENT": str(wire.ESC_POWER_ABSENT),
        "OFF_HOLD": str(wire.ESC_POWER_OFF_HOLD),
        "OFF_READY": str(wire.ESC_POWER_OFF_READY),
        "STARTING": str(wire.ESC_POWER_STARTING),
        "ARMING": str(wire.ESC_POWER_STARTING),
        "ON": str(wire.ESC_POWER_ON),
        "STOPPING": str(wire.ESC_POWER_STOPPING),
    }
    header = (root / "src/esc_power.h").read_text()
    assert _define(header, "ESC_POWER_OFF_HOLD_MS") == esc_power.OFF_HOLD_S * 1000
    assert _define(header, "ESC_POWER_STOP_MS") == esc_power.STOP_S * 1000
    assert _define(header, "ESC_POWER_THRUST_DELAY_MS") == (
        esc_power.THRUST_DELAY_S * 1000
    )


@pytest.mark.parametrize("features", [0x1F, 0x3F])
def test_negotiation_records_features_without_requiring_esc_power(
    endpoint, monkeypatch, features
):
    endpoint._negotiated = False
    endpoint.session = 0

    async def write(packet):
        if packet[0] == 0xC5 and packet[1] == 3:
            payload = struct.pack("<HHHHI", 1, 768, wire.SETTINGS_SIZE, 8, features)
            endpoint.receive(
                wire.Frame(
                    kind=wire.CAPABILITIES,
                    session=0,
                    sequence=packet[2],
                    payload=payload + b"mainboard",
                )
            )
        elif packet[0] == wire.START:
            endpoint.receive(ack(wire.decode(packet)))

    monkeypatch.setattr(endpoint, "_write", write)
    asyncio.run(endpoint._negotiate())
    assert endpoint._negotiated
    assert endpoint.features == features
    assert endpoint.esc_power_supported == bool(features & 0x20)
    endpoint._reset_connection()
    assert endpoint.features == 0


def test_attitude_health_bits_4_to_6_feed_board_state_and_expire(endpoint):
    telemetry = endpoint.state.esc_power_telemetry
    health_before = endpoint.state.system_health.model_dump()
    # IMU healthy (bit 0), output permitted (bit 2), ESC power ON (4) in bits 4-6.
    endpoint.receive(attitude(1, 0b100_0101))
    assert telemetry.wire_state == wire.ESC_POWER_ON
    assert endpoint.state.system_health.imu_healthy
    # SystemHealth keeps its shape.
    assert endpoint.state.system_health.model_dump().keys() == health_before.keys()
    first_change = telemetry.changed_at
    endpoint.receive(attitude(2, 0b100_0001))
    assert telemetry.changed_at == first_change  # same value, no change
    endpoint.receive(attitude(3, 0b101_0001))
    assert telemetry.wire_state == wire.ESC_POWER_STOPPING
    assert telemetry.changed_at >= first_change

    endpoint._expire_health(telemetry.reported_at + TELEMETRY_TIMEOUT / 2)
    assert telemetry.wire_state == wire.ESC_POWER_STOPPING
    endpoint._expire_health(telemetry.reported_at + TELEMETRY_TIMEOUT * 2)
    assert telemetry.wire_state is None
    assert not endpoint.state.system_health.imu_healthy


def test_reconnect_forgets_reported_state_but_keeps_silence_clock(endpoint):
    endpoint.receive(attitude(1, wire.ESC_POWER_ON << 4))
    reported = endpoint.state.esc_power_telemetry.reported_at
    endpoint._reset_connection()
    assert endpoint.state.esc_power_telemetry.wire_state is None
    assert endpoint.state.esc_power_telemetry.reported_at == reported


@pytest.mark.parametrize(
    "result", [wire.APPLIED, wire.BUSY, wire.INVALID, wire.UNSUPPORTED]
)
def test_esc_power_request_returns_result_and_keeps_the_session(
    endpoint, monkeypatch, result
):
    frames = []

    async def write(packet):
        frame = wire.decode(packet)
        frames.append(frame)
        endpoint.receive(ack(frame, result))

    monkeypatch.setattr(endpoint, "_write", write)
    assert asyncio.run(endpoint.request_esc_power(on=True)) == result
    assert asyncio.run(endpoint.request_esc_power(on=False)) == result
    assert [frame.kind for frame in frames] == [wire.ESC_POWER] * 2
    assert [frame.payload for frame in frames] == [
        struct.pack("<I", 1),
        struct.pack("<I", 0),
    ]
    # Each call is a fresh frame: a resent sequence would only repeat a cached BUSY.
    assert frames[1].sequence == frames[0].sequence + 1
    assert endpoint.session == SESSION
    assert endpoint._negotiated
    assert endpoint.state.system_status.thruster_protocol_state != "failed"


def test_lost_esc_power_ack_resends_the_same_frame(endpoint, monkeypatch):
    frames = []

    async def write(packet):
        frame = wire.decode(packet)
        frames.append(frame)
        if len(frames) == 3:
            endpoint.receive(ack(frame))

    monkeypatch.setattr(endpoint, "_write", write)
    monkeypatch.setattr("rov_firmware.pico_control.REQUEST_RETRY", 0.001)
    assert asyncio.run(endpoint.request_esc_power(on=True)) == wire.APPLIED
    assert len(frames) == 3
    assert all(frame == frames[0] for frame in frames)


def test_unanswered_esc_power_request_times_out_without_dropping_session(
    endpoint, monkeypatch
):
    write = AsyncMock()
    monkeypatch.setattr(endpoint, "_write", write)
    monkeypatch.setattr("rov_firmware.pico_control.REQUEST_RETRY", 0.001)
    with pytest.raises(TimeoutError):
        asyncio.run(endpoint.request_esc_power(on=False))
    assert write.await_count == 4
    assert endpoint.session == SESSION
    assert endpoint._negotiated


def test_stale_esc_power_result_is_an_error_not_a_session_loss(endpoint, monkeypatch):
    async def write(packet):
        endpoint.receive(ack(wire.decode(packet), wire.STALE))

    monkeypatch.setattr(endpoint, "_write", write)
    with pytest.raises(RuntimeError, match="result 4"):
        asyncio.run(endpoint.request_esc_power(on=True))
    assert endpoint.session == SESSION


def test_esc_power_requests_go_out_only_before_the_maintenance_gate(
    endpoint, monkeypatch
):
    writes = []

    async def write(packet):
        writes.append(packet)
        frame = wire.decode(packet)
        if frame.kind == wire.ENTER_MAINTENANCE:
            endpoint.receive(ack(frame))

    monkeypatch.setattr(endpoint, "_write", write)
    asyncio.run(endpoint.enter_maintenance())
    writes.clear()
    with pytest.raises(ConnectionError, match="session"):
        asyncio.run(endpoint.request_esc_power(on=False))
    assert writes == []
    assert not endpoint.session_active


@pytest.mark.parametrize("condition", ["unnegotiated", "flashing", "reconnected"])
def test_esc_power_request_needs_a_live_session(endpoint, monkeypatch, condition):
    write = AsyncMock()
    monkeypatch.setattr(endpoint, "_write", write)
    if condition == "unnegotiated":
        endpoint._negotiated = False
    elif condition == "flashing":
        endpoint.state.mcu_flashing = True
    else:
        endpoint.serial._connection_generation += 1
    assert not endpoint.session_active
    with pytest.raises(ConnectionError):
        asyncio.run(endpoint.request_esc_power(on=True))
    write.assert_not_awaited()


def test_attitude_after_expiry_counts_as_a_change(endpoint):
    telemetry = endpoint.state.esc_power_telemetry
    endpoint.receive(attitude(1, wire.ESC_POWER_OFF_HOLD << 4))
    telemetry.expire()
    time.sleep(0.001)
    endpoint.receive(attitude(2, wire.ESC_POWER_OFF_HOLD << 4))
    # A reappearing OFF_HOLD restarts the host's hold estimate (conservative).
    assert telemetry.changed_at == telemetry.reported_at


def test_esc_state_is_recorded_even_while_the_settings_generation_differs(endpoint):
    endpoint._settings_generation = 5  # frames from generation 0 are rejected
    endpoint.receive(attitude(1, wire.ESC_POWER_ON << 4 | 1))
    telemetry = endpoint.state.esc_power_telemetry
    assert telemetry.wire_state == wire.ESC_POWER_ON
    assert not endpoint.state.system_health.imu_healthy
    assert endpoint.diagnostic_snapshot(time.monotonic())["attitude_rejections"] == {
        "generation": 1
    }
    # Expiry follows these reports, not the last accepted attitude frame.
    endpoint._expire_health(telemetry.reported_at + TELEMETRY_TIMEOUT / 2)
    assert telemetry.wire_state == wire.ESC_POWER_ON
