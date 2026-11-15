"""tools board-check: a read-only bring-up check of the mainboard and the CM5.

Run it on the CM5 as pi, with the service stopped (BRINGUP.md)::

    sudo systemctl stop manafish-firmware
    tools board-check        # or: tools board-check --json > board-check.json
    sudo systemctl start manafish-firmware

Strictly read-only. The only I2C writes are the command and pointer bytes a read
needs (SHT45 0x89, the WSEN-PADS register pointers 0x0F and 0x11, the LTC4162
SMBus word reads, the ms5837 library's reset and PROM reads); nothing is written
to the LTC4162, not even ``force_telemetry_on``. The address scan uses
receive-byte probes only. GPIO is read with one ``pinctrl get``, never claimed;
the MCU's serial port, picotool and GPIO24/25 are never touched.

Every check catches its own errors and reports FAIL. Exit 2: refused because
manafish-firmware is in any state but inactive or failed, or its state is
unreadable (nothing checked); 1: a check FAILed; else 0. With ``--json`` every
print of a check or library goes to stderr, so stdout holds only the JSON.
"""

from collections.abc import Callable, Iterator, Sequence
import contextlib
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
import errno
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Final, Protocol

from ms5837 import MS5837_30BA
from smbus2 import SMBus

from rov_firmware.mcu_gpio import QSPI_SS_LINE, RUN_LINE
from rov_firmware.mcu_usb import (
    BOOTSEL_PRODUCT,
    CDC_PRODUCT,
    MCU_PORT_SYMLINK,
    MCU_USB_PRODUCT_NAME,
    USB_DEVICES_PATH,
    read_text,
    rp_usb_devices,
)
from rov_firmware.models.charger import (
    ChargerSettings,
    ChargerTelemetry,
    PoeType,
    ThermalLevel,
)
from rov_firmware.power_policy import (
    charger_targets,
    chemistry_mismatch,
    poe_present,
    poe_type,
)
from rov_firmware.sensors import ltc4162
from rov_firmware.sensors.charger import POE_TYP2_LINE, POE_TYP3_LINE
from rov_firmware.sensors.hull import (
    I2C_BUS,
    PADS_ADDRESS,
    PADS_DEVICE_ID,
    SHT45_ADDRESS,
    HullBus,
    check_pads,
    read_sht45_serial,
)
from rov_firmware.sensors.pi_power import HWMON_ROOT, read_pi_undervoltage
from rov_firmware.soc_monitor import (
    SOC_TEMPERATURE_PATH,
    THROTTLED_NOW_MASK,
    parse_throttled,
    read_temperature,
    throttled_flags,
)

from .mcu_recovery import SERVICE, STOPPED_STATES, parse_systemctl_show


SYS_ROOT: Final = Path("/sys")
CONFIG_TXT: Final = Path("/boot/firmware/config.txt")
CONFIG_TXT_LINES: Final = (
    "camera_auto_detect=0",
    "display_auto_detect=0",
    "force_eeprom_read=0",
    "enable_uart=0",
    "dtparam=i2c_arm=on",
    "dtparam=i2c_arm_baudrate=400000",
    "gpio=6,12=ip,pu",
    "gpio=24,25=ip,pn",
    "gpio=14=op,dh",
    "dtoverlay=imx477,cam0",
    "dtoverlay=imx477",
)
COMMAND_TIMEOUT_S: Final = 10.0
CAMERA_TIMEOUT_S: Final = 20.0
# What a command or a sysfs read raises (Board.output: RuntimeError on exit != 0).
READ_ERRORS: Final = (OSError, RuntimeError, subprocess.SubprocessError)

CAMERA2_ENABLE_LINE: Final = 14  # J11 IO0, driven high by config.txt
PINCTRL_LINES: Final = (
    POE_TYP2_LINE,
    POE_TYP3_LINE,
    CAMERA2_ENABLE_LINE,
    RUN_LINE,
    QSPI_SS_LINE,
)
MS5837_ADDRESS: Final = 0x76  # fixed in the ms5837 library
KNOWN_ADDRESSES: Final = frozenset(
    {SHT45_ADDRESS, PADS_ADDRESS, ltc4162.ADDRESS, MS5837_ADDRESS}
)
# A read at the SMBus Alert Response Address would acknowledge a pending
# LTC4162 alert (ltc4162.md L01), so the scan never probes it.
SMBUS_ALERT_RESPONSE_ADDRESS: Final = 0x0C
# What an absent address returns (Linux Documentation/i2c/fault-codes.rst).
I2C_NACK_ERRNOS: Final = frozenset({errno.ENXIO, errno.EREMOTEIO})
SCAN_ADDRESSES: Final = [
    address
    for address in range(0x08, 0x78)
    if address not in KNOWN_ADDRESSES | {SMBUS_ALERT_RESPONSE_ADDRESS}
]

SOC_TEMPERATURE_WARN_C: Final = 75.0
SOC_TEMPERATURE_FAIL_C: Final = 85.0
EXT5V_WARN_MIN_V: Final = 4.75
EXT5V_PASS_MIN_V: Final = 4.90
EXT5V_MAX_V: Final = 5.25  # plan B4/B14: +5V at or below 5.25 V
EXPECTED_CAMERAS: Final = 2
ETH0_PATH: Final = Path("/sys/class/net/eth0")
ETH0_ERROR_COUNTERS: Final = (
    "rx_errors",
    "rx_crc_errors",
    "rx_frame_errors",
    "rx_missed_errors",
    "rx_over_errors",
    "rx_fifo_errors",
    "tx_errors",
    "tx_carrier_errors",
)
REQUIRED_SERVICES: Final = ("manafish-setup", "dnsmasq", "go2rtc")

# pinctrl: " 6: ip    pu | hi // GPIO6 = input", "14: op dh pd | hi // ...".
_PINCTRL_LINE: Final = re.compile(r"^\s*(\d+):([^|]*)\|\s*(hi|lo)\b", re.MULTILINE)
# rpicam-hello --list-cameras: "0 : imx477 [4056x3040 12-bit RGGB] (...)".
_CAMERA_LINE: Final = re.compile(r"^(\d+) : (\S+)", re.MULTILINE)
_EXT5V: Final = re.compile(r"EXT5V_V volt\(\d+\)=([0-9.]+)V")


class Status(StrEnum):
    """Verdict of one check."""

    PASS = "PASS"  # noqa: S105 - a verdict, not a password
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Result:
    """One check's verdict, a one-line summary and JSON-ready details."""

    status: Status
    summary: str
    details: dict[str, object] = field(default_factory=dict)


class I2cBus(ltc4162.WordBus, HullBus, Protocol):
    """The smbus2.SMBus calls the checks use (the write methods are never called)."""

    def read_byte(self, i2c_addr: int, /) -> int: ...  # noqa: D102

    def close(self) -> None: ...  # noqa: D102


class Ms5837(Protocol):
    """The ms5837 library's sensor: ``init()`` resets it and checks the PROM CRC."""

    def init(self) -> bool: ...  # noqa: D102


type Run = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def run_command(
    args: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and capture its text output."""
    return subprocess.run(  # noqa: S603 - fixed argument lists from this module
        list(args), capture_output=True, text=True, timeout=timeout, check=False
    )


def _open_bus() -> I2cBus:
    return SMBus(I2C_BUS)


@dataclass(frozen=True, slots=True)
class Pin:
    """One ``pinctrl get`` line: its fields ("ip", "pu") and its level."""

    fields: tuple[str, ...]
    high: bool

    def __str__(self) -> str:
        """The pinctrl fields and level, as in "ip pn | hi"."""
        return f"{' '.join(self.fields)} | {'hi' if self.high else 'lo'}"


@dataclass(slots=True)
class Board:
    """Where the checks read from; every field is injectable for tests."""

    run: Run = run_command
    sys_root: Path = SYS_ROOT
    proc_root: Path = Path("/proc")
    config_txt: Path = CONFIG_TXT
    mcu_symlink: Path = MCU_PORT_SYMLINK
    bus_factory: Callable[[], I2cBus] = _open_bus
    ms5837_factory: Callable[[], Ms5837] = MS5837_30BA
    sleep: Callable[[float], None] = time.sleep
    _bus: I2cBus | None = field(default=None, init=False)
    _telemetry: ChargerTelemetry | None = field(default=None, init=False)
    _pins: dict[int, Pin] | None = field(default=None, init=False)

    def bus(self) -> I2cBus:
        """I2C1, opened on first use and shared by the checks."""
        if self._bus is None:
            self._bus = self.bus_factory()
        return self._bus

    def telemetry(self) -> ChargerTelemetry:
        """One LTC4162 telemetry read shared by the checks, kept only if it worked."""
        if self._telemetry is None:
            self._telemetry = ltc4162.Ltc4162(self.bus()).read_telemetry()
        return self._telemetry

    def pins(self) -> dict[int, Pin]:
        """One ``pinctrl get`` of ``PINCTRL_LINES``, kept only if it worked."""
        if self._pins is None:
            self._pins = read_pins(self, PINCTRL_LINES)
        return self._pins

    def sys_path(self, absolute: Path) -> Path:
        """A /sys path of the rov_firmware modules under ``sys_root``."""
        return self.sys_root / absolute.relative_to(SYS_ROOT)

    def output(self, args: Sequence[str], timeout: float = COMMAND_TIMEOUT_S) -> str:
        """Stdout of a command that must exit 0."""
        result = self.run(args, timeout)
        if result.returncode != 0:
            msg = (
                f"{' '.join(args)} exited {result.returncode}: {result.stderr.strip()}"
            )
            raise RuntimeError(msg)
        return result.stdout

    def close(self) -> None:
        """Close I2C1 if a check opened it, and forget the shared reads."""
        if self._bus is not None:
            self._bus.close()
        self._bus = None
        self._telemetry = None
        self._pins = None


def read_pins(board: Board, gpios: Sequence[int]) -> dict[int, Pin]:
    """Parse ``pinctrl get`` for the given GPIOs; it claims nothing."""
    text = board.output(["pinctrl", "get", ",".join(map(str, gpios))])
    pins = {
        int(match[1]): Pin(tuple(match[2].split()), match[3] == "hi")
        for match in _PINCTRL_LINE.finditer(text)
    }
    if missing := [gpio for gpio in gpios if gpio not in pins]:
        msg = f"pinctrl printed no line for GPIO {missing}"
        raise ValueError(msg)
    return {gpio: pins[gpio] for gpio in gpios}


# --- system ---


def _model(board: Board) -> Result:
    raw = (board.proc_root / "device-tree/model").read_bytes()
    model = raw.decode(errors="replace").rstrip("\x00\n")
    status = Status.PASS if "Compute Module 5" in model else Status.FAIL
    return Result(status, model, {"model": model})


def _kernel(board: Board) -> Result:
    release = (board.proc_root / "sys/kernel/osrelease").read_text().strip()
    return Result(Status.PASS, release, {"release": release})


def _config_txt(board: Board) -> Result:
    lines = {line.strip() for line in board.config_txt.read_text().splitlines()}
    missing = [line for line in CONFIG_TXT_LINES if line not in lines]
    details: dict[str, object] = {"path": str(board.config_txt), "missing": missing}
    if missing:
        return Result(Status.FAIL, "missing: " + ", ".join(missing), details)
    return Result(Status.PASS, f"all {len(CONFIG_TXT_LINES)} lines present", details)


# --- I2C1 ---


def _sht45(board: Board) -> Result:
    # A NACK (OSError) or a CRC mismatch (ValueError) is a FAIL from run_check.
    serial = f"{read_sht45_serial(board.bus(), board.sleep):#010x}"
    return Result(Status.PASS, f"0x44 serial {serial}", {"serial": serial})


def _pads(board: Board) -> Result:
    bus = board.bus()
    try:
        check_pads(bus)  # a NACK, a wrong DEVICE_ID or auto-increment off
    except OSError as error:
        return Result(Status.FAIL, f"0x5C: {error}; check IC10")
    summary = f"0x5C DEVICE_ID {PADS_DEVICE_ID:#04x}, auto-increment on"
    return Result(Status.PASS, summary)


def _ltc4162_chem(board: Board) -> Result:
    board.bus()  # an I2C1 that cannot be opened is not a DVCC fault
    try:
        telemetry = board.telemetry()
    except OSError as error:
        return Result(Status.FAIL, f"no answer at 0x68 ({error}); check DVCC")
    chem, cells = telemetry.chem, telemetry.cell_count
    en_chg = bool(telemetry.system_status & ltc4162.EN_CHG)
    details: dict[str, object] = {"chem": chem, "cell_count": cells, "en_chg": en_chg}
    # ltc4162.md L13 and correction 3: chem 0 is also the register default and
    # cell_count reads 0 while the charger is off, so both count only with en_chg.
    mismatch = chemistry_mismatch(telemetry.system_status, chem=chem, cell_count=cells)
    if mismatch is None:
        hint = (
            "cell_count not reported yet (no pack?)"
            if en_chg
            else "chem and count mean something only while the charger runs (PoE)"
        )
        summary = f"chem {chem}, cell_count {cells} with en_chg {int(en_chg)}: {hint}"
        return Result(Status.WARN, summary, details)
    if mismatch:
        summary = (
            f"chem {chem}, {cells} cells, expected 0 (LAD) and {ltc4162.CELL_COUNT}"
        )
        return Result(Status.FAIL, summary, details)
    return Result(Status.PASS, f"LAD, {cells} cells", details)


def _ms5837(board: Board) -> Result:
    # The library prints its errors; main() sends them to stderr with --json.
    if not board.ms5837_factory().init():
        return Result(Status.FAIL, "0x76 PROM unreadable or CRC mismatch")
    return Result(Status.PASS, "0x76 PROM read, CRC ok")


def _unexpected(board: Board) -> Result:
    bus = board.bus()
    answered = []
    for address in SCAN_ADDRESSES:
        try:
            bus.read_byte(address)  # receive byte: a read, never a quick write
        except OSError as error:
            if error.errno == errno.EBUSY:
                answered.append(f"{address:#04x} (kernel driver)")
            elif error.errno not in I2C_NACK_ERRNOS:
                # A timeout or bus error is no NACK: stop instead of reporting
                # an empty bus (and waiting out one timeout per address).
                summary = (
                    f"bus error at {address:#04x} ({error}); "
                    "SDA/SCL stuck low or pull-ups missing?"
                )
                return Result(Status.FAIL, summary, {"addresses": answered})
            continue
        answered.append(f"{address:#04x}")
    details: dict[str, object] = {"addresses": answered}
    if answered:
        summary = f"{', '.join(answered)} answered; address-pin bridge or wrong part?"
        return Result(Status.WARN, summary, details)
    return Result(Status.PASS, "no other address answers", details)


# --- GPIO (pinctrl get only) ---


def _pick(board: Board, *gpios: int) -> dict[int, Pin]:
    """The given lines from the one shared ``pinctrl get``."""
    pins = board.pins()
    return {gpio: pins[gpio] for gpio in gpios}


def _pin_details(pins: dict[int, Pin]) -> dict[str, object]:
    return {f"gpio{gpio}": str(pin) for gpio, pin in pins.items()}


def _pin_summary(pins: dict[int, Pin]) -> str:
    return ", ".join(f"GPIO{gpio} {pin}" for gpio, pin in pins.items())


def _poe_levels(pins: dict[int, Pin]) -> tuple[int, int]:
    """(TYP2, TYP3) levels as ``power_policy.poe_type`` takes them."""
    return int(pins[POE_TYP2_LINE].high), int(pins[POE_TYP3_LINE].high)


def _poe_type(board: Board) -> Result:
    pins = _pick(board, POE_TYP2_LINE, POE_TYP3_LINE)
    details = _pin_details(pins)
    if any(pin.fields != ("ip", "pu") for pin in pins.values()):
        return Result(Status.FAIL, "GPIO6/GPIO12 are not ip pu", details)
    try:
        system_status = board.telemetry().system_status
    except OSError as error:
        typ2, typ3 = pins[POE_TYP2_LINE], pins[POE_TYP3_LINE]
        summary = f"charger unreadable ({error}); TYP2 {typ2}, TYP3 {typ3}"
        return Result(Status.WARN, summary, details)
    poe = poe_type(_poe_levels(pins), present=poe_present(system_status))
    details["poe_type"] = poe.value
    summary = "no PoE" if poe == PoeType.NONE else f"PSE {poe.value}"
    return Result(Status.PASS, summary, details)


def _mcu_lines(board: Board) -> Result:
    pins = _pick(board, RUN_LINE, QSPI_SS_LINE)
    details = _pin_details(pins)
    summary = _pin_summary(pins)
    if all(pin.fields == ("ip", "pn") and pin.high for pin in pins.values()):
        return Result(Status.PASS, summary, details)
    hint = "expected ip pn | hi; low = RUN or QSPI_SS held low, or not wired"
    return Result(Status.FAIL, f"{summary} ({hint})", details)


def _camera2_enable(board: Board) -> Result:
    pins = _pick(board, CAMERA2_ENABLE_LINE)
    pin = pins[CAMERA2_ENABLE_LINE]
    status = Status.PASS if pin.fields[:2] == ("op", "dh") and pin.high else Status.FAIL
    return Result(status, _pin_summary(pins), _pin_details(pins))


# --- MCU on USB ---


def _usb_mcu(board: Board) -> Result:
    found = rp_usb_devices(board.sys_path(USB_DEVICES_PATH))
    cdc, bootsel = found.get(CDC_PRODUCT, []), found.get(BOOTSEL_PRODUCT, [])
    details: dict[str, object] = {
        "cdc_0009": [device.name for device in cdc],
        "bootsel_000f": [device.name for device in bootsel],
    }
    notes = [f"BOOTSEL 2e8a:000f at USB {device.name}" for device in bootsel]
    if len(cdc) != 1:
        summary = "; ".join([f"{len(cdc)} devices 2e8a:0009", *notes])
        return Result(Status.FAIL, summary, details)
    device = cdc[0]
    product = read_text(device / "product") or ""
    symlink = board.mcu_symlink.exists()
    details.update(usb_path=device.name, product=product, symlink=symlink)
    if product != MCU_USB_PRODUCT_NAME:
        notes.append(f'product "{product}", expected "{MCU_USB_PRODUCT_NAME}"')
    if not symlink:
        notes.append(f"{board.mcu_symlink} missing")
    summary = "; ".join([f'2e8a:0009 at USB {device.name} "{product}"', *notes])
    return Result(Status.WARN if notes else Status.PASS, summary, details)


# --- LTC4162 (reads only) ---


def _charger_telemetry(board: Board) -> Result:
    telemetry = board.telemetry()
    state = ltc4162.charger_state_name(telemetry.charger_state)
    charge = ltc4162.charge_status_name(telemetry.charge_status)
    details: dict[str, object] = {
        "vbat_v": round(telemetry.vbat_v, 3),
        "vbat_cell_v": round(telemetry.vbat_cell_v, 4),
        "vin_v": round(telemetry.vin_v, 3),
        "vout_v": round(telemetry.vout_v, 3),
        "ibat_a": round(telemetry.ibat_a, 4),
        "iin_a": round(telemetry.iin_a, 4),
        "die_temp_c": round(telemetry.die_temp_c, 2),
        "charger_state": state,
        "charge_status": charge,
        "system_status": ltc4162.flag_names(
            telemetry.system_status, ltc4162.SYSTEM_STATUS_FLAGS
        ),
        "telemetry_valid": telemetry.telemetry_valid,
    }
    summary = (
        f"vbat {telemetry.vbat_v:.2f} V, vin {telemetry.vin_v:.2f} V, "
        f"ibat {telemetry.ibat_a:.3f} A, iin {telemetry.iin_a:.3f} A, "
        f"die {telemetry.die_temp_c:.1f} C, {state}, {charge}"
    )
    if not telemetry.telemetry_valid:
        return Result(Status.WARN, f"{summary}; telemetry_valid clear", details)
    return Result(Status.PASS, summary, details)


def _service_poe(board: Board) -> PoeType:
    """The PoE type as the service decodes it: UNKNOWN when the pins fail."""
    present = poe_present(board.telemetry().system_status)
    try:
        levels = _poe_levels(board.pins())
    except (*READ_ERRORS, ValueError):
        levels = None
    return poe_type(levels, present=present)


def settings_match(settings: ChargerSettings, poe: PoeType) -> str:
    """Which known set ``settings`` equals: the defaults, a service target, neither.

    Before B9 the service sets only ``force_telemetry_on``, so after it has run
    the defaults read with that bit. From B9 it writes ``charger_targets`` for
    the PoE type and thermal level; "suspended" is heat or a chemistry fault.
    """
    defaults = ltc4162.DEFAULT_SETTINGS
    known = {
        "power-on defaults": defaults,
        "power-on defaults + force_telemetry_on": replace(
            defaults, config_bits=ltc4162.FORCE_TELEMETRY_ON
        ),
    }
    for thermal in ThermalLevel:
        name = f"service targets ({poe.value}, {thermal.name.lower()})"
        known[name] = charger_targets(poe=poe, thermal=thermal, chemistry_fault=False)
    # A chemistry suspend with a hot hull reads like the heat suspend above.
    known[f"service targets ({poe.value}, normal, chemistry fault)"] = charger_targets(
        poe=poe, thermal=ThermalLevel.NORMAL, chemistry_fault=True
    )
    return next((name for name, value in known.items() if value == settings), "neither")


def _charger_settings(board: Board) -> Result:
    settings = ltc4162.Ltc4162(board.bus()).read_settings()
    matches = settings_match(settings, _service_poe(board))
    charger_config = settings.charger_config_bits
    details: dict[str, object] = {
        "vcharge_setting": settings.vcharge_setting,
        "vcharge_v_per_cell": round(
            ltc4162.charge_voltage_per_cell(settings.vcharge_setting), 4
        ),
        "charge_current_setting": settings.charge_current_setting,
        "charge_current_a": round(
            ltc4162.charge_current_amps(settings.charge_current_setting), 4
        ),
        "iin_limit_target": settings.iin_limit_target,
        "iin_limit_a": round(ltc4162.input_limit_amps(settings.iin_limit_target), 4),
        "config_bits": ltc4162.flag_names(
            settings.config_bits, ltc4162.CONFIG_BITS_FLAGS
        ),
        "en_jeita": bool(charger_config & ltc4162.EN_JEITA),
        "en_c_over_x_term": bool(charger_config & ltc4162.EN_C_OVER_X_TERM),
        "matches": matches,
    }
    summary = f"{ltc4162.describe_settings(settings)}; matches {matches}"
    if matches == "neither":
        # Before B9 only force_telemetry_on may be written (owner rule).
        summary += ": written outside the service?"
        return Result(Status.WARN, summary, details)
    return Result(Status.PASS, summary, details)


# --- cameras, SoC, Ethernet, services ---


def _cameras(board: Board) -> Result:
    result = board.run(["rpicam-hello", "--list-cameras"], CAMERA_TIMEOUT_S)
    cameras = _CAMERA_LINE.findall(f"{result.stdout}\n{result.stderr}")
    names = [f"{index}: {sensor}" for index, sensor in cameras]
    details: dict[str, object] = {"cameras": names, "returncode": result.returncode}
    summary = f"{len(cameras)} camera(s)" + (f": {', '.join(names)}" if names else "")
    if len(cameras) == EXPECTED_CAMERAS:
        return Result(Status.PASS, summary, details)
    if len(cameras) == 1:
        hint = "check the camera 2 cable and pinctrl get 14"
        return Result(Status.WARN, f"{summary}; {hint}", details)
    return Result(Status.FAIL, summary, details)


def _soc_temperature(board: Board) -> Result:
    celsius = read_temperature(board.sys_path(SOC_TEMPERATURE_PATH))
    if celsius is None:
        return Result(Status.FAIL, "thermal_zone0 unreadable")
    if celsius < SOC_TEMPERATURE_WARN_C:
        status = Status.PASS
    elif celsius < SOC_TEMPERATURE_FAIL_C:
        status = Status.WARN
    else:
        status = Status.FAIL
    return Result(status, f"{celsius:.1f} C", {"temperature_c": celsius})


def _throttled(board: Board) -> Result:
    text = board.output(["vcgencmd", "get_throttled"])
    value = parse_throttled(text)
    if value is None:
        return Result(Status.FAIL, f"cannot parse {text.strip()!r}")
    flags = throttled_flags(value)
    details: dict[str, object] = {"throttled": f"{value:#x}", "flags": flags}
    if value == 0:
        return Result(Status.PASS, "throttled=0x0", details)
    status = Status.FAIL if value & THROTTLED_NOW_MASK else Status.WARN
    return Result(status, f"throttled={value:#x} ({', '.join(flags)})", details)


def _ext5v(board: Board) -> Result:
    text = board.output(["vcgencmd", "pmic_read_adc", "EXT5V_V"])
    match = _EXT5V.search(text)
    if match is None:
        return Result(Status.FAIL, f"cannot parse {text.strip()!r}")
    volts = float(match[1])
    if EXT5V_PASS_MIN_V <= volts <= EXT5V_MAX_V:
        status = Status.PASS
    elif EXT5V_WARN_MIN_V <= volts < EXT5V_PASS_MIN_V:
        status = Status.WARN
    else:
        status = Status.FAIL
    return Result(status, f"EXT5V {volts:.3f} V", {"ext5v_v": volts})


def _undervoltage_lines(board: Board) -> int:
    journal = board.output(["journalctl", "-k", "-b", "--no-pager"])
    return sum("Undervoltage detected" in line for line in journal.splitlines())


def _rpi_volt(board: Board) -> Result:
    alarm = read_pi_undervoltage(board.sys_path(HWMON_ROOT))
    if alarm is None:
        return Result(Status.FAIL, "rpi_volt hwmon not found or unreadable")
    if alarm:
        return Result(Status.FAIL, "in0_lcrit_alarm 1: input undervoltage now")
    try:
        count = _undervoltage_lines(board)
    except READ_ERRORS as error:
        summary = "in0_lcrit_alarm 0; journal unreadable"
        return Result(Status.PASS, summary, {"journal_error": str(error)})
    details: dict[str, object] = {"undervoltage_detected_this_boot": count}
    if count:
        summary = f"in0_lcrit_alarm 0, but {count} 'Undervoltage detected' this boot"
        return Result(Status.WARN, summary, details)
    return Result(Status.PASS, "in0_lcrit_alarm 0, no undervoltage this boot", details)


def _eth0_link(board: Board) -> Result:
    eth0 = board.sys_path(ETH0_PATH)
    try:
        speed = (eth0 / "speed").read_text().strip()
        duplex = (eth0 / "duplex").read_text().strip()
    except OSError as error:
        return Result(Status.FAIL, f"no link ({error})")
    status = Status.PASS if (speed, duplex) == ("1000", "full") else Status.FAIL
    details: dict[str, object] = {"speed_mbps": speed, "duplex": duplex}
    return Result(status, f"{speed} Mb/s {duplex}", details)


def _eth0_errors(board: Board) -> Result:
    statistics = board.sys_path(ETH0_PATH) / "statistics"
    counters: dict[str, object] = {
        name: int((statistics / name).read_text()) for name in ETH0_ERROR_COUNTERS
    }
    nonzero = [f"{name} {count}" for name, count in counters.items() if count]
    if nonzero:
        return Result(Status.WARN, ", ".join(nonzero), counters)
    return Result(Status.PASS, "all error counters 0", counters)


def _service_states(board: Board) -> Result:
    # is-active exits non-zero when a unit is not active; read its lines anyway.
    lines = board.run(
        ["systemctl", "is-active", *REQUIRED_SERVICES], COMMAND_TIMEOUT_S
    ).stdout.split()
    states: dict[str, object] = {
        unit: lines[index] if index < len(lines) else "unknown"
        for index, unit in enumerate(REQUIRED_SERVICES)
    }
    down = [f"{unit} {state}" for unit, state in states.items() if state != "active"]
    if down:
        return Result(Status.FAIL, "not active: " + ", ".join(down), states)
    return Result(Status.PASS, ", ".join(REQUIRED_SERVICES) + " active", states)


def _failed_units(board: Board) -> Result:
    text = board.output(["systemctl", "--failed", "--no-legend", "--plain"])
    units = [line.split()[0] for line in text.splitlines() if line.strip()]
    if units:
        return Result(Status.FAIL, "failed: " + ", ".join(units), {"units": units})
    return Result(Status.PASS, "no failed units", {"units": units})


type Check = Callable[[Board], Result]

CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("system.model", _model),
    ("system.kernel", _kernel),
    ("system.config_txt", _config_txt),
    ("i2c.sht45", _sht45),
    ("i2c.pads", _pads),
    ("i2c.ltc4162", _ltc4162_chem),
    ("i2c.ms5837", _ms5837),
    ("i2c.unexpected", _unexpected),
    ("poe.type", _poe_type),
    ("gpio.mcu_lines", _mcu_lines),
    ("gpio.camera2_enable", _camera2_enable),
    ("usb.mcu", _usb_mcu),
    ("charger.telemetry", _charger_telemetry),
    ("charger.settings", _charger_settings),
    ("camera.count", _cameras),
    ("soc.temperature", _soc_temperature),
    ("soc.throttled", _throttled),
    ("soc.ext5v", _ext5v),
    ("soc.rpi_volt", _rpi_volt),
    ("eth0.link", _eth0_link),
    ("eth0.errors", _eth0_errors),
    ("service.states", _service_states),
    ("service.failed", _failed_units),
)


def service_refusal(board: Board) -> str | None:
    """Why board-check must not run now (manafish-firmware not stopped), or None."""
    try:
        text = board.output(["systemctl", "show", "-p", "ActiveState", SERVICE])
    except READ_ERRORS as error:
        state = f"in an unknown state ({error})"
    else:
        state = parse_systemctl_show(text).get("ActiveState") or "in an unknown state"
    if state in STOPPED_STATES:
        return None
    return (
        f"{SERVICE} is {state}. Stop it first: sudo systemctl stop {SERVICE}, "
        f"then tools board-check, then sudo systemctl start {SERVICE}"
    )


def run_check(board: Board, check: Check) -> Result:
    """Run one check; one that raises becomes FAIL."""
    try:
        return check(board)
    except Exception as error:  # one broken check never stops the rest
        return Result(Status.FAIL, f"{type(error).__name__}: {error}")


def run_checks(board: Board) -> Iterator[tuple[str, Result]]:
    """Run every check in ``CHECKS`` order."""
    for check_id, check in CHECKS:
        yield check_id, run_check(board, check)


def main(*, json_output: bool = False, board: Board | None = None) -> int:
    """``tools board-check [--json]``: print the checks, return the exit code."""
    board = Board() if board is None else board
    reason = service_refusal(board)
    if reason is not None:
        _ = sys.stderr.write(f"board-check refused: {reason}\n")
        return 2
    checks = []
    # With --json any print (the ms5837 library prints its errors) goes to stderr.
    redirect = (
        contextlib.redirect_stdout(sys.stderr)
        if json_output
        else contextlib.nullcontext()
    )
    with redirect:
        try:
            for check_id, result in run_checks(board):
                checks.append((check_id, result))
                if not json_output:
                    line = f"{result.status}  {check_id}  {result.summary}\n"
                    _ = sys.stdout.write(line)
                    sys.stdout.flush()
        finally:
            board.close()
    exit_code = int(any(result.status == Status.FAIL for _, result in checks))
    if json_output:
        report = {
            "checks": [
                {"id": check_id, **asdict(result)} for check_id, result in checks
            ],
            "exit_code": exit_code,
        }
        _ = sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return exit_code
