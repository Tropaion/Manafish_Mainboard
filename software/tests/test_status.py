import time

from rov_firmware.models.battery import BatteryStatus
from rov_firmware.models.charger import ChargerStatus, ChargerTelemetry
from rov_firmware.models.config import ThrusterProtocol
from rov_firmware.models.soc import SocStatus
from rov_firmware.websocket.send import status


def test_status_reports_pi_undervoltage(rov_state):
    # SocMonitor latches the toggling rpi_volt alarm; the status reads the latch.
    now = time.monotonic()
    rov_state.soc = SocStatus(
        updated_at=now, undervoltage=False, undervoltage_asserted_at=now - 30
    )
    message = status.build_status_update(rov_state)
    assert message.payload.pi_undervoltage is True
    assert message.model_dump(by_alias=True)["payload"]["piUndervoltage"] is True


def test_undervoltage_latch_expires_and_unavailable_is_no_alarm(rov_state):
    now = time.monotonic()
    rov_state.soc = SocStatus(
        updated_at=now, undervoltage=False, undervoltage_asserted_at=now - 61
    )
    assert status.build_status_update(rov_state).payload.pi_undervoltage is False
    rov_state.soc = SocStatus(updated_at=now, undervoltage=None)
    assert status.build_status_update(rov_state).payload.pi_undervoltage is False


def _charger(now, vbat_v):
    return ChargerStatus(
        readable=True,
        updated_at=now,
        telemetry=ChargerTelemetry(
            vbat_cell_v=vbat_v / 5,
            vbat_v=vbat_v,
            vin_v=0.0,
            vout_v=vbat_v,
            ibat_a=-0.2,
            iin_a=0.0,
            die_temp_c=30.0,
            charger_state=256,
            charge_status=0,
            system_status=1,
            chem=0,
            cell_count=0,
            telemetry_valid=True,
        ),
        telemetry_problem=None,
        poe_present=False,
    )


def test_battery_percentage_uses_the_ltc4162_ema(rov_state):
    now = time.monotonic()
    rov_state.mcu_telemetry.voltage = [12.0] * 8
    rov_state.charger = _charger(now, 19.0)
    rov_state.battery = BatteryStatus(updated_at=now, voltage_ema_v=18.25)
    # (18.25 - 16) / (20.5 - 16) = 50 %, not the ESC average.
    assert status.build_status_update(rov_state).payload.battery_percentage == 50


def test_battery_percentage_uses_vbat_until_the_ema_exists(rov_state):
    now = time.monotonic()
    rov_state.charger = _charger(now, 20.5)
    assert status.build_status_update(rov_state).payload.battery_percentage == 100


def test_battery_percentage_falls_back_to_esc_voltage_without_the_charger(
    rov_state,
):
    now = time.monotonic()
    rov_state.mcu_telemetry.voltage = [18.25, 18.25, 0, 0, 0, 0, 0, 0]
    rov_state.battery = BatteryStatus(updated_at=now, voltage_ema_v=20.0)
    # No fresh charger telemetry: the stale EMA is not used either.
    rov_state.charger = _charger(now - 10, 20.0)
    assert status.build_status_update(rov_state).payload.battery_percentage == 50


def test_status_reports_thruster_control_readiness(rov_state):
    rov_state.system_status.thruster_control_ready = True
    payload = status.build_status_update(rov_state).model_dump(by_alias=True)["payload"]
    assert payload["thrusterControlReady"] is True


def test_status_reports_read_only_device_versions(rov_state):
    rov_state.device_info.mcu_firmware_version = "1.2.3-rc.1"
    rov_state.device_info.esc_firmware_versions = ["2.20.0-rc.3"] * 8
    payload = status.build_status_update(rov_state).model_dump(by_alias=True)["payload"]
    assert payload["deviceInfo"]["mcuFirmwareVersion"] == "1.2.3-rc.1"
    assert payload["deviceInfo"]["escFirmwareVersions"] == ["2.20.0-rc.3"] * 8
    assert payload["escFirmwareUpdate"]["stage"] == "idle"


def calibrated_state(rov_state):
    rov_state.system_health.mcu_healthy = True
    rov_state.rov_config.thruster_protocol = ThrusterProtocol.DSHOT
    rov_state.mcu_telemetry.board_current_ma = [3250, 7000]
    rov_state.mcu_telemetry.board_current_updated_at = [time.monotonic()] * 2
    return rov_state


def test_status_sums_two_corrected_boards_without_second_correction(rov_state):
    calibrated_state(rov_state)
    rov_state.mcu_telemetry.current = [91] * 8
    rov_state.mcu_telemetry.current_valid = [True] * 8
    assert status.build_status_update(rov_state).payload.current_draw == 10.25


def test_raw_current_is_not_a_fallback_for_missing_calibration(rov_state):
    rov_state.mcu_telemetry.current = [91] * 8
    rov_state.mcu_telemetry.current_valid = [True] * 8
    payload = status.build_status_update(rov_state).model_dump(by_alias=True)["payload"]
    assert payload["currentDraw"] is None


def test_missing_stale_and_zero_are_distinct(rov_state):
    calibrated_state(rov_state)
    rov_state.mcu_telemetry.board_current_ma[1] = None
    assert status._current_draw(rov_state) is None
    rov_state.mcu_telemetry.board_current_ma = [0, 0]
    assert status._current_draw(rov_state) == 0
    rov_state.mcu_telemetry.board_current_updated_at[1] = time.monotonic() - 10
    assert status._current_draw(rov_state) is None


def test_disconnected_pwm_and_flashing_do_not_report_old_current(rov_state):
    calibrated_state(rov_state)
    rov_state.system_health.mcu_healthy = False
    assert status._current_draw(rov_state) is None
    rov_state.system_health.mcu_healthy = True
    rov_state.rov_config.thruster_protocol = ThrusterProtocol.PWM
    assert status._current_draw(rov_state) is None
    rov_state.rov_config.thruster_protocol = ThrusterProtocol.DSHOT
    rov_state.mcu_flashing = True
    assert status._current_draw(rov_state) is None
    rov_state.mcu_flashing = False
    rov_state.esc_firmware_update.active = True
    assert status._current_draw(rov_state) is None
    rov_state.esc_firmware_update.active = False
    rov_state.esc_firmware_recovery_required = True
    assert status._current_draw(rov_state) is None
