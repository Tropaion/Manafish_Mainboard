"""Mainboard config: pico normalised to pico2, hull camera defaults, durable save."""

import asyncio
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from rov_firmware.models import config as config_module
from rov_firmware.models.config import (
    Camera,
    McuBoard,
    PartialRovConfig,
    RovConfig,
    normalize_mcu_board,
)
from rov_firmware.websocket.receive import config as config_receive


@pytest.fixture
def config_path(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setattr(RovConfig, "_config_path", path)
    return path


def test_mcu_board_defaults_to_pico2():
    assert RovConfig().mcu_board is McuBoard.PICO2


@pytest.mark.parametrize("value", ["pico", McuBoard.PICO, "pico2"])
def test_rov_config_normalises_pico_to_pico2(value):
    assert RovConfig.model_validate({"mcuBoard": value}).mcu_board is McuBoard.PICO2


def test_partial_config_normalises_pico_and_keeps_none():
    assert PartialRovConfig.model_validate({"mcuBoard": "pico"}).mcu_board is (
        McuBoard.PICO2
    )
    assert PartialRovConfig().mcu_board is None


def test_unknown_board_is_still_rejected():
    with pytest.raises(ValueError, match="mcuBoard"):
        RovConfig.model_validate({"mcuBoard": "rp2040"})


def test_stored_pico_config_loads_as_pico2_with_dshot_1200(config_path):
    stored = RovConfig().model_dump(mode="json", by_alias=True)
    stored["mcuBoard"] = "pico"
    stored["dshotSpeed"] = 1200
    config_path.write_text(json.dumps(stored))

    loaded = RovConfig.load()

    assert loaded.mcu_board is McuBoard.PICO2
    assert loaded.dshot_speed == 1200


def test_normalize_mcu_board_helper():
    assert normalize_mcu_board(McuBoard.PICO) is McuBoard.PICO2
    assert normalize_mcu_board(McuBoard.PICO2) is McuBoard.PICO2


def test_hull_camera_defaults():
    camera = Camera()

    assert (camera.width, camera.height, camera.framerate) == (1280, 960, 30)
    assert camera.bitrate == 4_500_000
    # The defaults must survive the model's own clamps (sensor mode, H.264
    # level 4 macroblock rate, bitrate range), or the stream would differ.
    assert Camera.model_validate(camera.model_dump()) == camera


def test_stored_camera_values_win_over_the_new_defaults(config_path):
    stored = RovConfig().model_dump(mode="json", by_alias=True)
    stored["camera"].update(
        {"width": 1024, "height": 768, "framerate": 30, "bitrate": 3_538_944}
    )
    config_path.write_text(json.dumps(stored))

    camera = RovConfig.load().camera

    assert (camera.width, camera.height, camera.bitrate) == (1024, 768, 3_538_944)


def test_save_fsyncs_the_file_before_the_rename_and_the_directory_after(
    config_path, monkeypatch
):
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync-{kind}")
        real_fsync(fd)

    def replace(self: Path, target: Path) -> Path:
        events.append("rename")
        return real_replace(self, target)

    monkeypatch.setattr(config_module.os, "fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)

    RovConfig(rov_name="Durable").save()

    assert events == ["fsync-file", "rename", "fsync-dir"]
    assert json.loads(config_path.read_text())["rovName"] == "Durable"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]


def test_failed_save_removes_the_temp_file_and_keeps_the_old_config(
    config_path, monkeypatch
):
    RovConfig(rov_name="Old").save()

    def failing_fsync(_fd: int) -> None:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(config_module.os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="disk gone"):
        RovConfig(rov_name="New").save()

    assert json.loads(config_path.read_text())["rovName"] == "Old"
    assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]


def test_a_failed_directory_sync_after_the_rename_keeps_the_new_config(
    config_path, monkeypatch
):
    # The new file is already in place; reporting a failure would reject a
    # config the next start loads anyway.
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            msg = "directory sync failed"
            raise OSError(msg)
        real_fsync(fd)

    monkeypatch.setattr(config_module.os, "fsync", fsync)

    RovConfig(rov_name="New").save()

    assert json.loads(config_path.read_text())["rovName"] == "New"
    assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]


def test_a_config_save_runs_off_the_event_loop_thread(
    rov_state, config_path, monkeypatch
):
    # save() fsyncs twice; on the loop it would stall the 60 Hz CONTROL stream.
    threads: list[int] = []
    real_save = RovConfig.save

    def save(config: RovConfig) -> None:
        threads.append(threading.get_ident())
        real_save(config)

    monkeypatch.setattr(RovConfig, "save", save)
    candidate = rov_state.rov_config.model_copy(update={"rov_name": "Off loop"})

    async def run() -> int:
        assert await config_receive._persist_candidate(rov_state, candidate, None)
        return threading.get_ident()

    loop_thread = asyncio.run(run())
    assert len(threads) == 1
    assert threads[0] != loop_thread
    assert rov_state.rov_config is candidate
    assert json.loads(config_path.read_text())["rovName"] == "Off loop"
