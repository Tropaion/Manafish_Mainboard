"""tools board-check: read-only, one verdict per check, refuses while the service runs."""

import errno
import json
from pathlib import Path
import re
import shutil
import subprocess

from ms5837 import MS5837_30BA
import pytest
import smbus2

import tools
from tools import board_check
from tools.board_check import Status


SHOW = ("systemctl", "show", "-p", "ActiveState", "manafish-firmware")
PINCTRL = ("pinctrl", "get", "6,12,14,24,25")
CAMERAS = ("rpicam-hello", "--list-cameras")
THROTTLED = ("vcgencmd", "get_throttled")
EXT5V = ("vcgencmd", "pmic_read_adc", "EXT5V_V")
JOURNAL = ("journalctl", "-k", "-b", "--no-pager")
IS_ACTIVE = ("systemctl", "is-active", "manafish-setup", "dnsmasq", "go2rtc")
FAILED = ("systemctl", "--failed", "--no-legend", "--plain")

CAMERA_0 = (
    "0 : imx477 [4056x3040 12-bit RGGB] "
    "(/base/axi/pcie@1000120000/rp1/i2c@88000/imx477@1a)\n"
    "    Modes: 'SRGGB10_CSI2P' : 1332x990 [120.05 fps - (696, 528)/2664x1980 crop]\n"
    "           'SRGGB12_CSI2P' : 2028x1080 [50.03 fps - (0, 440)/4056x2160 crop]\n"
)
CAMERA_1 = CAMERA_0.replace("0 : ", "1 : ", 1).replace("i2c@88000", "i2c@80000")
CAMERA_HEADER = "Available cameras\n-----------------\n"
PIN_LINES = {
    6: " 6: ip    pu | hi // GPIO6 = input",
    12: "12: ip    pu | hi // GPIO12 = input",
    14: "14: op dh pd | hi // GPIO14 = output",
    24: "24: ip    pn | hi // GPIO24 = input",
    25: "25: ip    pn | hi // GPIO25 = input",
}


def pinctrl(lines: dict[int, str | None] | None = None) -> str:
    """``pinctrl get 6,12,14,24,25`` output; a line given as None is left out."""
    merged = {**PIN_LINES, **(lines or {})}
    return "".join(f"{line}\n" for line in merged.values() if line is not None)


HEALTHY_OUTPUTS: dict[tuple[str, ...], object] = {
    SHOW: "ActiveState=inactive\n",
    PINCTRL: pinctrl(),
    CAMERAS: CAMERA_HEADER + CAMERA_0 + "\n" + CAMERA_1,
    THROTTLED: "throttled=0x0\n",
    EXT5V: "     EXT5V_V volt(24)=5.03438000V\n",
    JOURNAL: "Nov 15 kernel: Booting Linux on physical CPU 0x0\n",
    IS_ACTIVE: "active\nactive\nactive\n",
    FAILED: "",
}

# LTC4162 system_status with PoE and the charger running (en_chg).
EN_CHG_WITH_POE = 0x0107
SYSTEM_STATUS = 0x39
CHEM_CELLS = 0x43


class FakeRunner:
    """subprocess.run for board-check: canned output per exact argument list."""

    def __init__(self) -> None:
        self.outputs = dict(HEALTHY_OUTPUTS)
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: dict[tuple[str, ...], float] = {}

    def __call__(self, args, timeout):
        command = tuple(args)
        self.calls.append(command)
        self.timeouts[command] = timeout
        output = self.outputs[command]
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, subprocess.CompletedProcess):
            return output
        return subprocess.CompletedProcess(list(args), 0, output, "")


class OkMs5837:
    def init(self):
        return True


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def set_usb(sys_root: Path, devices: list[tuple[str, str, str]]) -> None:
    """Replace the sysfs USB devices: (name, idProduct of 2e8a, product string)."""
    usb = sys_root / "bus/usb/devices"
    shutil.rmtree(usb, ignore_errors=True)
    for name, vendor, product, product_name in [
        ("usb1", "1d6b", "0002", "xHCI Host Controller"),
        *[(name, "2e8a", product, text) for name, product, text in devices],
    ]:
        write(usb / name / "idVendor", f"{vendor}\n")
        write(usb / name / "idProduct", f"{product}\n")
        write(usb / name / "product", f"{product_name}\n")
        (usb / f"{name}:1.0").mkdir()  # an interface: no idVendor


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def make_sys(root: Path) -> Path:
    """A CM5 /sys with a healthy board: 52 C, no alarm, 1000 full, the MCU on USB."""
    write(root / "class/thermal/thermal_zone0/temp", "52000\n")
    write(root / "class/hwmon/hwmon0/name", "cpu_thermal\n")
    write(root / "class/hwmon/hwmon1/name", "rpi_volt\n")
    write(root / "class/hwmon/hwmon1/in0_lcrit_alarm", "0\n")
    write(root / "class/net/eth0/speed", "1000\n")
    write(root / "class/net/eth0/duplex", "full\n")
    for counter in board_check.ETH0_ERROR_COUNTERS:
        write(root / "class/net/eth0/statistics" / counter, "0\n")
    set_usb(root, [("1-1", "0009", "Manafish Mainboard")])
    return root


@pytest.fixture
def board(tmp_path, runner, i2c_bus, fake_ltc4162, fake_clock):
    proc_root = tmp_path / "proc"
    write(proc_root / "device-tree/model", "Raspberry Pi Compute Module 5 Rev 1.0\0")
    write(proc_root / "sys/kernel/osrelease", "6.12.47-v8-16k\n")
    config = tmp_path / "config.txt"
    write(config, "[all]\n" + "\n".join(board_check.CONFIG_TXT_LINES) + "\n")
    symlink = tmp_path / "manafish-mcu"
    symlink.touch()
    # Charging on PoE: telemetry valid, chem_cells LAD with 5 cells.
    fake_ltc4162.set_telemetry(vbat=21830, vin=14554)
    fake_ltc4162.regs[CHEM_CELLS] = 0x0005
    fake_ltc4162.regs[SYSTEM_STATUS] = EN_CHG_WITH_POE
    return board_check.Board(
        run=runner,
        sys_root=make_sys(tmp_path / "sys"),
        proc_root=proc_root,
        config_txt=config,
        mcu_symlink=symlink,
        bus_factory=lambda: i2c_bus,
        ms5837_factory=OkMs5837,
        sleep=fake_clock.sleep,
    )


def result_of(board, check_id: str) -> board_check.Result:
    """One check as a run of its own, so the shared reads are fresh each time."""
    try:
        return board_check.run_check(board, dict(board_check.CHECKS)[check_id])
    finally:
        board.close()


def run_main(board, capsys, *, json_output: bool = False):
    code = board_check.main(json_output=json_output, board=board)
    return code, capsys.readouterr()


# --- the whole run ---


def test_healthy_board_passes_every_check_in_contract_order(board, capsys):
    code, output = run_main(board, capsys)

    lines = output.out.splitlines()
    assert code == 0
    assert [line.split("  ")[1] for line in lines] == [
        "system.model",
        "system.kernel",
        "system.config_txt",
        "i2c.sht45",
        "i2c.pads",
        "i2c.ltc4162",
        "i2c.ms5837",
        "i2c.unexpected",
        "poe.type",
        "gpio.mcu_lines",
        "gpio.camera2_enable",
        "usb.mcu",
        "charger.telemetry",
        "charger.settings",
        "camera.count",
        "soc.temperature",
        "soc.throttled",
        "soc.ext5v",
        "soc.rpi_volt",
        "eth0.link",
        "eth0.errors",
        "service.states",
        "service.failed",
    ]
    assert all(re.fullmatch(r"PASS  [a-z0-9_.]+  .+", line) for line in lines), lines
    assert lines[0] == "PASS  system.model  Raspberry Pi Compute Module 5 Rev 1.0"


def test_json_shape_and_exit_code(board, capsys):
    code, output = run_main(board, capsys, json_output=True)

    report = json.loads(output.out)
    assert set(report) == {"checks", "exit_code"}
    assert report["exit_code"] == code == 0
    assert [check["id"] for check in report["checks"]] == [
        check_id for check_id, _ in board_check.CHECKS
    ]
    for check in report["checks"]:
        assert list(check) == ["id", "status", "summary", "details"]
        assert check["status"] == "PASS"
        assert isinstance(check["details"], dict)
    sht45 = next(c for c in report["checks"] if c["id"] == "i2c.sht45")
    assert sht45["details"] == {"serial": "0x1234abcd"}


def test_warn_only_exits_0_and_a_fail_exits_1(board, runner, capsys):
    runner.outputs[CAMERAS] = CAMERA_HEADER + CAMERA_0
    code, output = run_main(board, capsys, json_output=True)
    assert code == json.loads(output.out)["exit_code"] == 0

    write(board.sys_root / "class/net/eth0/speed", "100\n")
    code, output = run_main(board, capsys, json_output=True)
    assert code == json.loads(output.out)["exit_code"] == 1


@pytest.mark.parametrize("state", ["active", "activating", "reloading", "deactivating"])
def test_refuses_with_exit_2_while_the_service_runs(board, runner, capsys, state):
    runner.outputs[SHOW] = f"ActiveState={state}\n"
    board.bus_factory = lambda: pytest.fail("no I2C while the service runs")

    code, output = run_main(board, capsys, json_output=True)

    assert code == 2
    assert output.out == ""
    assert "sudo systemctl stop manafish-firmware" in output.err
    assert "sudo systemctl start manafish-firmware" in output.err
    assert runner.calls == [SHOW]


def test_refuses_when_the_service_state_is_unreadable(board, runner, capsys):
    runner.outputs[SHOW] = FileNotFoundError("systemctl")

    code, output = run_main(board, capsys)

    assert code == 2
    assert "unknown state" in output.err
    assert runner.calls == [SHOW]


def test_failed_service_state_counts_as_stopped(board, runner, capsys):
    runner.outputs[SHOW] = "ActiveState=failed\n"

    assert run_main(board, capsys)[0] == 0


def test_a_raising_check_becomes_fail_and_the_others_still_run(
    board, capsys, monkeypatch
):
    def broken(_board):
        msg = "boom"
        raise RuntimeError(msg)

    checks = list(board_check.CHECKS)
    checks.insert(1, ("test.broken", broken))
    monkeypatch.setattr(board_check, "CHECKS", tuple(checks))

    code, output = run_main(board, capsys)

    lines = output.out.splitlines()
    assert code == 1
    assert lines[1] == "FAIL  test.broken  RuntimeError: boom"
    assert len(lines) == len(checks)
    assert sum(line.startswith("PASS") for line in lines) == len(checks) - 1


def test_unopenable_i2c_fails_only_the_i2c_checks(board):
    def no_bus():
        raise FileNotFoundError(errno.ENOENT, "No such file", "/dev/i2c-1")

    board.bus_factory = no_bus
    results = dict(board_check.run_checks(board))

    i2c = {"i2c.sht45", "i2c.pads", "i2c.ltc4162", "i2c.unexpected"}
    i2c |= {"charger.telemetry", "charger.settings"}
    assert {k for k, r in results.items() if r.status == Status.FAIL} == i2c
    assert results["poe.type"].status == Status.WARN  # pins known, charger not
    assert "/dev/i2c-1" in results["i2c.pads"].summary


def test_never_writes_to_the_charger_or_any_register(
    board, capsys, i2c_bus, fake_ltc4162, fake_pads
):
    fake_ltc4162.regs[0x14] = 0  # force_telemetry_on clear: the service would set it

    assert run_main(board, capsys)[0] == 0

    assert fake_ltc4162.writes == []
    assert fake_pads.writes == []
    assert {call for call in i2c_bus.calls if call[1] == 0x68} <= {
        ("read_word", 0x68, register) for register in fake_ltc4162.READABLE
    }
    assert {call[0] for call in i2c_bus.calls} == {
        "read_word",
        "read_byte",
        "rdwr",
        "receive_byte",
    }
    assert {call for call in i2c_bus.calls if call[0] == "read_byte"} == {
        ("read_byte", 0x5C, 0x0F),
        ("read_byte", 0x5C, 0x11),
    }
    assert i2c_bus.closed


def test_one_charger_telemetry_read_and_one_pinctrl_serve_every_check(
    board, runner, capsys, i2c_bus
):
    assert run_main(board, capsys)[0] == 0

    assert i2c_bus.calls.count(("read_word", 0x68, 0x3A)) == 1  # vbat
    assert runner.calls.count(PINCTRL) == 1


def test_a_failed_charger_read_is_not_kept(board, fake_ltc4162):
    fake_ltc4162.present = False
    with pytest.raises(OSError, match="Remote I/O"):
        board.telemetry()

    fake_ltc4162.present = True
    assert board.telemetry().telemetry_valid


def test_runs_no_command_that_could_change_the_board(board, runner, capsys):
    run_main(board, capsys)

    assert set(runner.calls) == set(HEALTHY_OUTPUTS)
    assert runner.timeouts[CAMERAS] == 20.0


# --- system ---


def test_model_must_be_a_compute_module_5(board):
    write(board.proc_root / "device-tree/model", "Raspberry Pi 5 Model B Rev 1.0\0")

    result = result_of(board, "system.model")

    assert result.status == Status.FAIL
    assert result.details == {"model": "Raspberry Pi 5 Model B Rev 1.0"}


def test_kernel_release_is_info_only(board):
    assert result_of(board, "system.kernel").summary == "6.12.47-v8-16k"


def test_config_txt_lists_missing_lines(board):
    lines = [
        line
        for line in board_check.CONFIG_TXT_LINES
        if line not in {"gpio=14=op,dh", "dtoverlay=imx477,cam0"}
    ]
    write(board.config_txt, "\n".join([*lines, "#gpio=14=op,dh"]))

    result = result_of(board, "system.config_txt")

    assert result.status == Status.FAIL
    assert result.details["missing"] == ["gpio=14=op,dh", "dtoverlay=imx477,cam0"]


# --- I2C1 ---


def test_sht45_reads_its_serial_10_ms_after_0x89(board, fake_sht45):
    result = result_of(board, "i2c.sht45")

    assert result.status == Status.PASS
    assert result.summary == "0x44 serial 0x1234abcd"
    ((sent, command),) = fake_sht45.commands
    assert command == 0x89
    assert fake_sht45.reads[0] - sent == pytest.approx(0.010)


def test_a_slow_sht45_serial_read_is_retried_like_a_measurement(board, fake_sht45):
    fake_sht45.serial_s = 0.012  # NACKs the read header at 10 ms

    result = result_of(board, "i2c.sht45")

    assert result.status == Status.PASS
    ((sent, _),) = fake_sht45.commands
    assert fake_sht45.reads[0] - sent == pytest.approx(0.015)


def test_sht45_crc_error_and_nack_fail(board, fake_sht45):
    fake_sht45.corrupt_next = True
    assert result_of(board, "i2c.sht45").status == Status.FAIL

    fake_sht45.present = False
    assert result_of(board, "i2c.sht45").status == Status.FAIL


def test_pads_device_id_and_auto_increment(board, fake_pads):
    assert result_of(board, "i2c.pads").status == Status.PASS

    fake_pads.regs[0x11] = 0x00  # CTRL_2 IF_ADD_INC off
    result = result_of(board, "i2c.pads")
    assert result.status == Status.FAIL
    assert "auto-increment off" in result.summary

    fake_pads.regs[0x0F] = 0xB1
    assert result_of(board, "i2c.pads").status == Status.FAIL

    fake_pads.present = False
    result = result_of(board, "i2c.pads")
    assert result.status == Status.FAIL
    assert "IC10" in result.summary


@pytest.mark.parametrize(
    ("chem_cells", "system_status", "expected"),
    [
        (0x0005, EN_CHG_WITH_POE, Status.PASS),  # LAD, 5 cells
        (0x0000, EN_CHG_WITH_POE, Status.WARN),  # LAD, cell_count 0
        (0x0105, EN_CHG_WITH_POE, Status.FAIL),  # L42
        (0x0100, EN_CHG_WITH_POE, Status.WARN),  # cell_count 0: not checkable
        (0x0004, EN_CHG_WITH_POE, Status.FAIL),  # LAD, 4 cells
        (0x0004, 0x0001, Status.WARN),  # the count means nothing without en_chg
        (0x0100, 0x0001, Status.WARN),  # nor does chem (ltc4162.md correction 3)
    ],
)
def test_ltc4162_chem_cells(board, fake_ltc4162, chem_cells, system_status, expected):
    fake_ltc4162.regs[CHEM_CELLS] = chem_cells
    fake_ltc4162.regs[SYSTEM_STATUS] = system_status

    assert result_of(board, "i2c.ltc4162").status == expected


def test_ltc4162_nack_hints_at_dvcc(board, fake_ltc4162):
    fake_ltc4162.present = False

    result = result_of(board, "i2c.ltc4162")

    assert result.status == Status.FAIL
    assert "DVCC" in result.summary


def ms5837_crc4(prom: list[int]) -> int:
    """MS5837 PROM CRC-4 (TE AN520), written here independently of the library."""
    words = [prom[0] & 0x0FFF, *prom[1:7], 0]
    remainder = 0
    for index in range(16):
        word = words[index >> 1]
        remainder ^= word & 0xFF if index % 2 else word >> 8
        for _ in range(8):
            remainder = (
                (remainder << 1) ^ 0x3000 if remainder & 0x8000 else remainder << 1
            )
    return (remainder >> 12) & 0xF


class FakeMs5837:
    """MS5837-30BA: reset 0x1E, then the PROM words 0xA0-0xAC, MSB first."""

    def __init__(self) -> None:
        self.present = True
        prom = [0x0B1A, 34982, 36352, 20328, 22354, 26646, 26146]
        prom[0] |= ms5837_crc4(prom) << 12
        self.prom = prom

    def send_byte(self, value: int) -> None:
        assert value == 0x1E, f"MS5837 command {value:#04x}"

    def read_word(self, register: int) -> int:
        assert register in range(0xA0, 0xAE, 2), f"read of {register:#04x}"
        word = self.prom[(register - 0xA0) // 2]
        return ((word & 0xFF) << 8) | (word >> 8)  # SMBus words are LSB first


@pytest.fixture
def fake_ms5837(board, i2c_bus, monkeypatch) -> FakeMs5837:
    """The real ms5837 library, its SMBus(1) replaced by the fake I2C1."""
    device = FakeMs5837()
    i2c_bus.devices[0x76] = device
    monkeypatch.setattr(smbus2, "SMBus", lambda _bus=1: i2c_bus)
    board.ms5837_factory = MS5837_30BA
    return device


def test_ms5837_library_sends_only_reset_and_prom_reads(board, i2c_bus, fake_ms5837):
    result = result_of(board, "i2c.ms5837")

    assert result.status == Status.PASS
    assert fake_ms5837.prom[0] >> 12  # a non-zero CRC nibble was checked
    assert i2c_bus.calls == [
        ("send_byte", 0x76, 0x1E),
        *[("read_word", 0x76, 0xA0 + 2 * index) for index in range(7)],
    ]


def test_ms5837_crc_error_fails_and_its_print_goes_to_stderr_with_json(
    board, fake_ms5837, capsys
):
    fake_ms5837.prom[0] ^= 0xF000

    code, output = run_main(board, capsys, json_output=True)

    report = json.loads(output.out)  # the library's print did not corrupt it
    ms5837 = next(c for c in report["checks"] if c["id"] == "i2c.ms5837")
    assert code == 1
    assert ms5837["status"] == "FAIL"
    assert "CRC failed" in output.err
    assert "CRC failed" in run_main(board, capsys)[1].out  # text mode prints it


def test_ms5837_nack_fails(board, fake_ms5837):
    fake_ms5837.present = False

    assert result_of(board, "i2c.ms5837").status == Status.FAIL


class AnsweringDevice:
    def __init__(self, error: int | None = None) -> None:
        self.error = error

    def receive_byte(self) -> int:
        if self.error is not None:
            raise OSError(self.error, "busy")
        return 0


def test_scan_probes_with_receive_byte_only_and_skips_known_parts_and_the_ara(
    board, i2c_bus
):
    result = result_of(board, "i2c.unexpected")

    probed = [call[1] for call in i2c_bus.calls]
    assert result.status == Status.PASS
    assert {call[0] for call in i2c_bus.calls} == {"receive_byte"}
    assert probed == [
        address
        for address in range(0x08, 0x78)
        if address not in {0x0C, 0x44, 0x5C, 0x68, 0x76}
    ]


def test_a_bus_error_during_the_scan_fails_and_stops_it(board, i2c_bus):
    i2c_bus.devices[0x20] = AnsweringDevice(errno.ETIMEDOUT)  # SDA held low

    result = result_of(board, "i2c.unexpected")

    assert result.status == Status.FAIL
    assert "0x20" in result.summary
    assert "pull-ups" in result.summary
    assert i2c_bus.calls[-1] == ("receive_byte", 0x20)


def test_scan_warns_about_an_unexpected_address(board, i2c_bus):
    i2c_bus.devices[0x5D] = AnsweringDevice()  # PADS SAO bridged high
    i2c_bus.devices[0x50] = AnsweringDevice(errno.EBUSY)

    result = result_of(board, "i2c.unexpected")

    assert result.status == Status.WARN
    assert result.details == {"addresses": ["0x50 (kernel driver)", "0x5d"]}
    assert "address-pin bridge" in result.summary


# --- GPIO ---


@pytest.mark.parametrize(
    ("pins", "system_status", "expected", "summary"),
    [
        ((" 6: ip    pu | hi", "12: ip    pu | hi"), 0x0001, Status.PASS, "no PoE"),
        ((" 6: ip    pu | hi", "12: ip    pu | hi"), 0x0107, Status.PASS, "PSE type1"),
        ((" 6: ip    pu | lo", "12: ip    pu | hi"), 0x0107, Status.PASS, "PSE type2"),
        ((" 6: ip    pu | hi", "12: ip    pu | lo"), 0x0107, Status.PASS, "PSE type3"),
        ((" 6: ip    pu | lo", "12: ip    pu | lo"), 0x0107, Status.PASS, "PSE type4"),
        ((" 6: ip    pd | hi", "12: ip    pu | hi"), 0x0107, Status.FAIL, None),
        ((" 6: op dl pu | lo", "12: ip    pu | hi"), 0x0107, Status.FAIL, None),
    ],
)
def test_poe_type_decode(  # noqa: PLR0913 - one parameter per table column
    board, runner, fake_ltc4162, pins, system_status, expected, summary
):
    runner.outputs[PINCTRL] = pinctrl({6: f"{pins[0]} // GPIO", 12: f"{pins[1]} //"})
    fake_ltc4162.regs[SYSTEM_STATUS] = system_status

    result = result_of(board, "poe.type")

    assert result.status == expected
    if summary is not None:
        assert result.summary == summary


def test_poe_type_with_unreadable_charger_warns_with_the_pin_levels(
    board, runner, fake_ltc4162
):
    runner.outputs[PINCTRL] = pinctrl({6: " 6: ip    pu | lo // GPIO6"})
    fake_ltc4162.present = False

    result = result_of(board, "poe.type")

    assert result.status == Status.WARN
    assert "TYP2 ip pu | lo, TYP3 ip pu | hi" in result.summary


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ({}, Status.PASS),
        ({24: "24: ip    pn | lo // GPIO24"}, Status.FAIL),
        ({25: "25: ip    pn | lo // GPIO25"}, Status.FAIL),
        ({24: "24: ip    pu | hi // GPIO24"}, Status.FAIL),
        ({24: "24: op dl pn | lo // GPIO24"}, Status.FAIL),
        ({24: None}, Status.FAIL),  # no line for GPIO24
    ],
)
def test_mcu_lines_must_be_floating_inputs_reading_high(board, runner, lines, expected):
    runner.outputs[PINCTRL] = pinctrl(lines)

    assert result_of(board, "gpio.mcu_lines").status == expected


def test_mcu_lines_low_names_run_and_qspi_ss(board, runner):
    runner.outputs[PINCTRL] = pinctrl(
        {24: "24: ip    pn | lo", 25: "25: ip    pn | lo"}
    )

    summary = result_of(board, "gpio.mcu_lines").summary

    assert "GPIO24 ip pn | lo" in summary
    assert "RUN or QSPI_SS" in summary


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("14: op dh pd | hi // GPIO14 = output\n", Status.PASS),
        ("14: op dl pd | lo // GPIO14 = output\n", Status.FAIL),
        ("14: ip    pd | lo // GPIO14 = input\n", Status.FAIL),
    ],
)
def test_camera2_enable(board, runner, text, expected):
    runner.outputs[PINCTRL] = pinctrl({14: text.rstrip()})

    assert result_of(board, "gpio.camera2_enable").status == expected


def test_unreadable_pins_match_the_targets_the_service_writes_for_unknown_poe(
    board, runner, fake_ltc4162
):
    runner.outputs[PINCTRL] = subprocess.CompletedProcess([], 1, "", "no gpio")
    fake_ltc4162.regs.update({0x29: 0, 0x1A: 19, 0x15: 6, 0x14: 0x04})  # PoE present

    result = result_of(board, "charger.settings")

    assert result.status == Status.PASS
    assert result.details["matches"] == "service targets (unknown, normal)"


def test_pinctrl_failure_fails_all_three_gpio_checks(board, runner):
    runner.outputs[PINCTRL] = subprocess.CompletedProcess([], 1, "", "no gpio")

    results = dict(board_check.run_checks(board))

    for check_id in ("poe.type", "gpio.mcu_lines", "gpio.camera2_enable"):
        assert results[check_id].status == Status.FAIL
        assert "no gpio" in results[check_id].summary


# --- MCU on USB ---


@pytest.mark.parametrize(
    ("devices", "expected"),
    [
        ([], Status.FAIL),
        ([("1-1", "0009", "Manafish Mainboard")], Status.PASS),
        (
            [("1-1", "0009", "Manafish Mainboard"), ("1-2", "0009", "Pico")],
            Status.FAIL,
        ),
        ([("1-1", "000f", "RP2350 Boot")], Status.FAIL),
        (
            [("1-1", "0009", "Manafish Mainboard"), ("1-2", "000f", "RP2350 Boot")],
            Status.WARN,
        ),
        ([("1-1", "0009", "Pico")], Status.WARN),
    ],
)
def test_usb_mcu(board, devices, expected):
    set_usb(board.sys_root, devices)

    assert result_of(board, "usb.mcu").status == expected


def test_usb_mcu_reports_its_path_product_and_symlink(board):
    result = result_of(board, "usb.mcu")

    assert result.details == {
        "cdc_0009": ["1-1"],
        "bootsel_000f": [],
        "usb_path": "1-1",
        "product": "Manafish Mainboard",
        "symlink": True,
    }


def test_usb_mcu_without_the_symlink_warns(board):
    board.mcu_symlink.unlink()

    result = result_of(board, "usb.mcu")

    assert result.status == Status.WARN
    assert "manafish-mcu missing" in result.summary


def test_usb_bootsel_only_is_named_in_the_failure(board):
    set_usb(board.sys_root, [("1-1", "000f", "RP2350 Boot")])

    assert "BOOTSEL 2e8a:000f at USB 1-1" in result_of(board, "usb.mcu").summary


# --- LTC4162 telemetry and settings ---


def test_charger_telemetry_values_with_units(board, fake_ltc4162):
    fake_ltc4162.set_telemetry(vbat=21830, vin=14554, ibat=-1000, die=13461)

    result = result_of(board, "charger.telemetry")

    assert result.status == Status.PASS
    assert result.details["vbat_v"] == pytest.approx(21.0, abs=0.01)
    assert result.details["vin_v"] == pytest.approx(24.0, abs=0.01)
    assert result.details["ibat_a"] == pytest.approx(-0.1466, abs=1e-4)
    assert result.details["die_temp_c"] == pytest.approx(25.0, abs=0.1)
    assert result.details["charger_state"] == "charger_suspended"
    assert result.details["charge_status"] == "charger_off"
    assert result.details["system_status"] == [
        "en_chg",
        "vin_gt_vbat",
        "vin_gt_4p2v",
        "intvcc_gt_2p8v",
    ]


def test_charger_telemetry_invalid_warns_unreadable_fails(board, fake_ltc4162):
    fake_ltc4162.set_telemetry(valid=False)
    assert result_of(board, "charger.telemetry").status == Status.WARN

    fake_ltc4162.present = False
    assert result_of(board, "charger.telemetry").status == Status.FAIL


def test_charger_settings_at_power_on_defaults(board, fake_ltc4162):
    result = result_of(board, "charger.settings")

    assert result.status == Status.PASS
    assert result.summary.startswith("vcharge code 31 = 4.2000 V/cell")
    assert result.details == {
        "vcharge_setting": 31,
        "vcharge_v_per_cell": 4.2,
        "charge_current_setting": 31,
        "charge_current_a": 3.2,
        "iin_limit_target": 63,
        "iin_limit_a": 4.0,
        "config_bits": [],
        "en_jeita": True,
        "en_c_over_x_term": False,
        "matches": "power-on defaults",
    }
    fake_ltc4162.present = False
    assert result_of(board, "charger.settings").status == Status.FAIL


@pytest.mark.parametrize(
    ("regs", "matches"),
    [
        ({0x14: 0x04}, "power-on defaults + force_telemetry_on"),
        ({0x29: 0, 0x1A: 19, 0x15: 41, 0x14: 0x04}, "service targets (type4, normal)"),
        ({0x29: 0, 0x1A: 19, 0x15: 20, 0x14: 0x04}, "service targets (type4, halved)"),
        (
            {0x29: 0, 0x1A: 19, 0x15: 20, 0x14: 0x24},
            "service targets (type4, suspended)",
        ),
        (
            {0x29: 0, 0x1A: 19, 0x15: 41, 0x14: 0x24},
            "service targets (type4, normal, chemistry fault)",
        ),
        ({0x29: 0, 0x1A: 19, 0x15: 6, 0x14: 0x04}, "neither"),  # Type 1 limit
    ],
)
def test_charger_settings_name_the_set_they_match(
    board, runner, fake_ltc4162, regs, matches
):
    runner.outputs[PINCTRL] = pinctrl({6: " 6: ip pu | lo", 12: "12: ip pu | lo"})
    fake_ltc4162.regs.update(regs)  # PoE present (vin_gt_4p2v), Type 4 pins

    result = result_of(board, "charger.settings")

    assert result.status == (Status.WARN if matches == "neither" else Status.PASS)
    assert result.details["matches"] == matches


# --- cameras ---


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (CAMERA_HEADER + CAMERA_0 + "\n" + CAMERA_1, Status.PASS),
        (CAMERA_HEADER + CAMERA_0, Status.WARN),
        ("No cameras available!\n", Status.FAIL),
        (subprocess.TimeoutExpired(list(CAMERAS), 20.0), Status.FAIL),
        (FileNotFoundError(errno.ENOENT, "No such file", "rpicam-hello"), Status.FAIL),
    ],
)
def test_camera_count(board, runner, output, expected):
    runner.outputs[CAMERAS] = output

    assert result_of(board, "camera.count").status == expected


def test_one_camera_points_at_the_cable_and_gpio14(board, runner):
    runner.outputs[CAMERAS] = CAMERA_HEADER + CAMERA_0

    result = result_of(board, "camera.count")

    assert result.details["cameras"] == ["0: imx477"]
    assert "pinctrl get 14" in result.summary


# --- SoC ---


@pytest.mark.parametrize(
    ("millidegrees", "expected"),
    [
        ("74999", Status.PASS),
        ("75000", Status.WARN),
        ("84999", Status.WARN),
        ("85000", Status.FAIL),
        ("garbage", Status.FAIL),
    ],
)
def test_soc_temperature_boundaries(board, millidegrees, expected):
    write(board.sys_root / "class/thermal/thermal_zone0/temp", f"{millidegrees}\n")

    assert result_of(board, "soc.temperature").status == expected


@pytest.mark.parametrize(
    ("text", "expected", "flags"),
    [
        ("throttled=0x0", Status.PASS, []),
        (
            "throttled=0x50000",
            Status.WARN,
            ["undervoltage_occurred", "throttled_occurred"],
        ),
        (
            "throttled=0x50005",
            Status.FAIL,
            [
                "undervoltage",
                "throttled",
                "undervoltage_occurred",
                "throttled_occurred",
            ],
        ),
        ("throttled=0x8", Status.FAIL, ["soft_temperature_limit"]),
    ],
)
def test_soc_throttled(board, runner, text, expected, flags):
    runner.outputs[THROTTLED] = text + "\n"

    result = result_of(board, "soc.throttled")

    assert result.status == expected
    assert result.details["flags"] == flags


def test_soc_throttled_unparsable_fails(board, runner):
    runner.outputs[THROTTLED] = "error=1\n"

    assert result_of(board, "soc.throttled").status == Status.FAIL


@pytest.mark.parametrize(
    ("volts", "expected"),
    [
        ("4.73000000", Status.FAIL),
        ("4.74999000", Status.FAIL),
        ("4.75000000", Status.WARN),
        ("4.89999000", Status.WARN),
        ("4.90000000", Status.PASS),
        ("5.03438000", Status.PASS),
        ("5.25000000", Status.PASS),
        ("5.25001000", Status.FAIL),
    ],
)
def test_soc_ext5v_boundaries(board, runner, volts, expected):
    runner.outputs[EXT5V] = f"     EXT5V_V volt(24)={volts}V\n"

    assert result_of(board, "soc.ext5v").status == expected


def test_soc_ext5v_unparsable_fails(board, runner):
    runner.outputs[EXT5V] = 'error=2 error_msg="Invalid argument"\n'

    assert result_of(board, "soc.ext5v").status == Status.FAIL


def test_rpi_volt_alarm_fails(board):
    write(board.sys_root / "class/hwmon/hwmon1/in0_lcrit_alarm", "1\n")

    assert result_of(board, "soc.rpi_volt").status == Status.FAIL


def test_rpi_volt_warns_on_undervoltage_earlier_this_boot(board, runner):
    runner.outputs[JOURNAL] = (
        "kernel: hwmon hwmon1: Undervoltage detected!\n"
        "kernel: hwmon hwmon1: Voltage normalised\n"
        "kernel: hwmon hwmon1: Undervoltage detected!\n"
    )

    result = result_of(board, "soc.rpi_volt")

    assert result.status == Status.WARN
    assert result.details == {"undervoltage_detected_this_boot": 2}


def test_rpi_volt_keeps_the_alarm_verdict_when_the_journal_is_unreadable(board, runner):
    runner.outputs[JOURNAL] = subprocess.CompletedProcess([], 1, "", "no access")

    result = result_of(board, "soc.rpi_volt")

    assert result.status == Status.PASS
    assert "no access" in str(result.details["journal_error"])


def test_rpi_volt_missing_hwmon_fails(board):
    shutil.rmtree(board.sys_root / "class/hwmon/hwmon1")

    assert result_of(board, "soc.rpi_volt").status == Status.FAIL


# --- Ethernet and services ---


def test_eth0_link_must_be_gigabit_full_duplex(board):
    write(board.sys_root / "class/net/eth0/duplex", "half\n")
    assert result_of(board, "eth0.link").status == Status.FAIL

    (board.sys_root / "class/net/eth0/speed").unlink()  # reading it fails without link
    result = result_of(board, "eth0.link")
    assert result.status == Status.FAIL
    assert "no link" in result.summary


def test_eth0_errors_warn_with_the_nonzero_counters(board):
    write(board.sys_root / "class/net/eth0/statistics/rx_crc_errors", "3\n")

    result = result_of(board, "eth0.errors")

    assert result.status == Status.WARN
    assert result.summary == "rx_crc_errors 3"


def test_service_states_name_the_inactive_unit(board, runner):
    runner.outputs[IS_ACTIVE] = subprocess.CompletedProcess(
        [], 3, "active\nactive\ninactive\n", ""
    )

    result = result_of(board, "service.states")

    assert result.status == Status.FAIL
    assert result.summary == "not active: go2rtc inactive"


def test_failed_units_are_listed(board, runner):
    runner.outputs[FAILED] = "foo.service loaded failed failed Foo\n"

    result = result_of(board, "service.failed")

    assert result.status == Status.FAIL
    assert result.details == {"units": ["foo.service"]}


# --- CLI ---


@pytest.mark.parametrize(("argv", "json_output"), [([], False), (["--json"], True)])
def test_cli_dispatches_board_check(monkeypatch, argv, json_output):
    calls: list[bool] = []

    def fake_main(*, json_output: bool = False) -> int:
        calls.append(json_output)
        return 1

    monkeypatch.setattr(board_check, "main", fake_main)

    with pytest.raises(SystemExit) as exit_info:
        tools.cli(["board-check", *argv])

    assert exit_info.value.code == 1
    assert calls == [json_output]
