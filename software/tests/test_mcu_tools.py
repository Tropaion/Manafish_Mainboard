"""tools mcu-reset / mcu-recover and the tools CLI subcommands."""

import subprocess

import pytest

import tools
from tools import mcu_recovery


SHOW_INACTIVE = "ActiveState=inactive\nInactiveEnterTimestampMonotonic=100000000\n"


def _refusal(text: str, now_s: float) -> str | None:
    return mcu_recovery.service_refusal(show=lambda: text, now=lambda: now_s)


def test_parse_systemctl_show():
    assert mcu_recovery.parse_systemctl_show(SHOW_INACTIVE + "\nJunk\n") == {
        "ActiveState": "inactive",
        "InactiveEnterTimestampMonotonic": "100000000",
    }


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", ""])
def test_refuses_while_the_service_is_not_stopped(state):
    reason = _refusal(
        f"ActiveState={state}\nInactiveEnterTimestampMonotonic=1\n", 1000.0
    )

    assert reason is not None
    assert "systemctl stop manafish-firmware" in reason


def test_refuses_until_35_s_after_the_stop():
    reason = _refusal(SHOW_INACTIVE, 100.0 + 34.0)

    assert reason is not None
    assert "34 s ago" in reason
    assert _refusal(SHOW_INACTIVE, 100.0 + 35.0) is None


def test_failed_counts_like_inactive():
    text = "ActiveState=failed\nInactiveEnterTimestampMonotonic=100000000\n"

    assert _refusal(text, 136.0) is None


def test_never_started_since_boot_counts_from_boot():
    text = "ActiveState=inactive\nInactiveEnterTimestampMonotonic=0\n"

    assert _refusal(text, 20.0) is not None
    assert _refusal(text, 40.0) is None


def test_unreadable_timestamp_refuses():
    text = "ActiveState=inactive\nInactiveEnterTimestampMonotonic=n/a\n"

    assert "InactiveEnterTimestampMonotonic" in (_refusal(text, 1000.0) or "")


def test_systemctl_failure_refuses():
    def broken():
        raise subprocess.CalledProcessError(1, ["systemctl"])

    reason = mcu_recovery.service_refusal(show=broken, now=lambda: 1000.0)

    assert reason is not None
    assert "systemctl" in reason


def test_esc_recovery_journal_refuses_first(monkeypatch):
    monkeypatch.setattr(mcu_recovery, "mcu_reset_refusal", lambda: "journal set")

    assert mcu_recovery.refusal(show=lambda: SHOW_INACTIVE, now=lambda: 1e6) == (
        "journal set"
    )


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(mcu_recovery, "refusal", lambda: None)


@pytest.mark.usefixtures("allowed")
def test_reset_pulses_run_when_allowed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(mcu_recovery.mcu_gpio, "reset", lambda: calls.append("reset"))
    monkeypatch.setattr(mcu_recovery, "_report_usb", lambda: None)

    assert mcu_recovery.reset_main() == 0
    assert calls == ["reset"]


def test_reset_refused_touches_no_line(monkeypatch, capsys):
    monkeypatch.setattr(mcu_recovery, "refusal", lambda: "stop the service first")
    monkeypatch.setattr(
        mcu_recovery.mcu_gpio, "reset", lambda: pytest.fail("reset must not run")
    )

    assert mcu_recovery.reset_main() == 1
    assert "stop the service first" in capsys.readouterr().err


@pytest.mark.usefixtures("allowed")
def test_recover_enters_bootsel_then_loads_the_bundled_image(monkeypatch, tmp_path):
    image = tmp_path / "mainboard-v1.0.4-mb.1.uf2"
    image.touch()
    events: list[object] = []
    monkeypatch.setattr(
        mcu_recovery, "resolve_mcu_firmware", lambda _board: (image, "1.0.4-mb.1")
    )
    monkeypatch.setattr(
        mcu_recovery,
        "picotool_load_command",
        lambda path: ["picotool", "load", "-v", "-x", str(path)],
    )
    monkeypatch.setattr(
        mcu_recovery.mcu_gpio, "enter_bootsel", lambda: events.append("bootsel")
    )

    def run(args, **_kwargs):
        events.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(mcu_recovery.subprocess, "run", run)

    assert mcu_recovery.recover_main(None) == 0
    assert events == ["bootsel", ["picotool", "load", "-v", "-x", str(image)]]


@pytest.mark.usefixtures("allowed")
def test_recover_loads_a_given_image(monkeypatch, tmp_path):
    image = tmp_path / "dev.uf2"
    image.touch()
    loaded: list[object] = []
    monkeypatch.setattr(
        mcu_recovery, "picotool_load_command", lambda path: ["picotool", str(path)]
    )
    monkeypatch.setattr(mcu_recovery.mcu_gpio, "enter_bootsel", lambda: None)
    monkeypatch.setattr(
        mcu_recovery.subprocess,
        "run",
        lambda args, **_kw: loaded.append(args) or subprocess.CompletedProcess(args, 3),
    )

    assert mcu_recovery.recover_main(image) == 3
    assert loaded == [["picotool", str(image)]]


@pytest.mark.usefixtures("allowed")
def test_recover_without_an_image_touches_no_line(monkeypatch, capsys):
    monkeypatch.setattr(mcu_recovery, "resolve_mcu_firmware", lambda _board: None)
    monkeypatch.setattr(
        mcu_recovery.mcu_gpio,
        "enter_bootsel",
        lambda: pytest.fail("no BOOTSEL without an image"),
    )

    assert mcu_recovery.recover_main(None) == 1
    assert "mainboard-v" in capsys.readouterr().err


@pytest.mark.usefixtures("allowed")
def test_recover_reports_a_missing_bootsel_device(monkeypatch, tmp_path):
    image = tmp_path / "dev.uf2"
    image.touch()
    monkeypatch.setattr(mcu_recovery, "picotool_load_command", lambda _path: ["p"])

    def no_bootsel():
        msg = "did not enumerate as BOOTSEL (2e8a:000f)"
        raise mcu_recovery.McuGpioError(msg)

    monkeypatch.setattr(mcu_recovery.mcu_gpio, "enter_bootsel", no_bootsel)
    monkeypatch.setattr(
        mcu_recovery.subprocess, "run", lambda *_a, **_k: pytest.fail("no picotool")
    )

    assert mcu_recovery.recover_main(image) == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["mcu-reset"], ("reset",)),
        (["mcu-recover"], ("recover", None)),
        (["mcu-recover", "dev.uf2"], ("recover", "dev.uf2")),
    ],
)
def test_cli_dispatches_subcommands(monkeypatch, argv, expected):
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        mcu_recovery, "reset_main", lambda: calls.append(("reset",)) or 0
    )
    monkeypatch.setattr(
        mcu_recovery,
        "recover_main",
        lambda uf2: calls.append(("recover", None if uf2 is None else str(uf2))) or 4,
    )

    with pytest.raises(SystemExit) as exited:
        tools.cli(argv)

    assert calls == [expected]
    assert exited.value.code == (0 if expected[0] == "reset" else 4)


@pytest.mark.usefixtures("capsys")
def test_cli_rejects_an_unknown_subcommand():
    with pytest.raises(SystemExit) as exited:
        tools.cli(["flash-everything"])

    assert exited.value.code == 2


def test_cli_without_arguments_shows_the_menu(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "6")
    monkeypatch.setattr(mcu_recovery, "reset_main", lambda: calls.append("r") or 0)

    with pytest.raises(SystemExit) as exited:
        tools.cli([])

    assert calls == ["r"]
    assert exited.value.code == 0
    assert "Reset the MCU" in capsys.readouterr().out


def test_cli_reads_sys_argv_by_default(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(tools.sys, "argv", ["-c", "mcu-reset"])
    monkeypatch.setattr(mcu_recovery, "reset_main", lambda: calls.append("r") or 0)

    with pytest.raises(SystemExit):
        tools.cli()

    assert calls == ["r"]
