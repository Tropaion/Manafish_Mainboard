import asyncio
import ctypes
import errno
from typing import ClassVar

import numpy as np
import pytest
from smbus2.smbus2 import I2C_M_RD

from rov_firmware import esc_recovery
from rov_firmware.models.battery import BatteryStatus
from rov_firmware.models.charger import ChargerSettings, ChargerStatus
from rov_firmware.models.config import RovConfig
from rov_firmware.models.esc_power import EscPowerTelemetry
from rov_firmware.models.hull import HullData
from rov_firmware.models.regulator import RegulatorData
from rov_firmware.models.sensors import ImuData, McuData, PressureData
from rov_firmware.models.soc import SocStatus
from rov_firmware.models.system import (
    DeviceInfo,
    EscFirmwareUpdate,
    SystemHealth,
    SystemStatus,
)
from rov_firmware.models.thruster import ThrusterData
from rov_firmware.rov_state import RovState
from rov_firmware.sensors.ltc4162 import DEFAULT_SETTINGS


@pytest.fixture
def rov_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        esc_recovery,
        "_RECOVERY_JOURNAL_PATH",
        tmp_path / "esc-firmware-update.json",
    )
    monkeypatch.setenv("MANAFISH_DATA_DIR", str(tmp_path))
    state = RovState.__new__(RovState)
    state.rov_config = RovConfig()
    state.system_health = SystemHealth()
    state.system_status = SystemStatus()
    state.device_info = DeviceInfo()
    state.esc_firmware_update = EscFirmwareUpdate()
    state.imu = ImuData()
    state.pressure = PressureData()
    state.mcu_telemetry = McuData()
    state.regulator = RegulatorData()
    state.thrusters = ThrusterData(direction_vector=np.zeros(8, dtype=np.float32))
    state.mcu_flashing = False
    state.esc_firmware_recovery_required = False
    state.esc_firmware_confirmation_deadline = None
    state.esc_firmware_confirmation_toasts = False
    state.config_ack_waiters = {}
    state.connection_change_task = None
    state.config_confirmation_tasks = set()
    state.mcu_flash_lock = asyncio.Lock()
    state.config_mutation_lock = asyncio.Lock()
    state.pico = None
    state.esc_power_telemetry = EscPowerTelemetry()
    state.esc_power = None
    state.hull = HullData()
    state.charger = ChargerStatus()
    state.battery = BatteryStatus()
    state.soc = SocStatus()
    return state


class FakeLgpioError(Exception):
    """Stands in for ``lgpio.error``."""


class FakeLgpio:
    """lgpio 0.2.2 as the CM5 kernel and pinctrl-rp1 behave, with a fake clock.

    Mirrors what the real library does (lgGpio.c): claiming a line that this
    handle holds in the other direction frees it first; claiming a line already
    held as input is a no-op; bias flags map to GPIO_V2_LINE_FLAG_BIAS_*, and
    without one the kernel leaves the pad bias alone. pinctrl-rp1 keeps a freed
    output driving its last level (``persist_gpio_outputs``).
    """

    SET_PULL_UP = 32
    SET_PULL_DOWN = 64
    SET_PULL_NONE = 128
    error = FakeLgpioError

    def __init__(self, chips: dict[int, str] | None = None) -> None:
        self.chips = {0: "pinctrl-rp1"} if chips is None else chips
        self.now = 0.0
        self.open_handles: dict[int, int] = {}
        self.claims: dict[int, str] = {}
        self.owners: dict[int, int] = {}
        self.input_flags: list[tuple[int, int]] = []
        self.bias: dict[int, str] = {6: "up", 12: "up", 24: "none", 25: "none"}
        self.levels: dict[int, int] = {}
        self.driven_low: set[int] = set()
        self.transitions: list[tuple[float, int, str]] = []
        self.calls: list[str] = []

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def _drive(self, line: int, low: bool) -> None:
        if low and line not in self.driven_low:
            self.driven_low.add(line)
            self.transitions.append((self.now, line, "low"))
        elif not low and line in self.driven_low:
            self.driven_low.discard(line)
            self.transitions.append((self.now, line, "released"))

    def gpiochip_open(self, gpiochip: int) -> int:
        self.calls.append(f"open {gpiochip}")
        if gpiochip not in self.chips:
            msg = "can not open gpiochip"
            raise FakeLgpioError(msg)
        handle = 100 + gpiochip
        self.open_handles[handle] = gpiochip
        return handle

    def gpiochip_close(self, handle: int) -> int:
        self.calls.append(f"close {self.open_handles.pop(handle)}")
        # Closing the handle frees its lines; a freed output keeps driving.
        for gpio in [g for g, owner in self.owners.items() if owner == handle]:
            self.claims.pop(gpio, None)
            self.owners.pop(gpio)
        return 0

    def gpio_get_chip_info(self, handle: int) -> list[object]:
        gpiochip = self.open_handles[handle]
        return [0, 54, f"gpiochip{gpiochip}", self.chips[gpiochip]]

    def gpio_claim_output(
        self, handle: int, gpio: int, level: int = 0, flags: int = 0
    ) -> int:
        assert handle in self.open_handles
        assert level == 0, "a CM5 line is never driven high"
        assert flags == 0
        self.calls.append(f"output {gpio}")
        if self.claims.get(gpio) != "output":
            self.claims[gpio] = "output"
            self.owners[gpio] = handle
            self._drive(gpio, low=True)
        return 0

    def gpio_claim_input(self, handle: int, gpio: int, flags: int = 0) -> int:
        assert handle in self.open_handles
        self.calls.append(f"input {gpio}")
        if self.claims.get(gpio) == "input":
            return 0
        self.claims[gpio] = "input"
        self.owners[gpio] = handle
        self.input_flags.append((gpio, flags))
        self._drive(gpio, low=False)
        if flags & self.SET_PULL_NONE:
            self.bias[gpio] = "none"
        elif flags & self.SET_PULL_UP:
            self.bias[gpio] = "up"
        elif flags & self.SET_PULL_DOWN:
            self.bias[gpio] = "down"
        return 0

    def gpio_free(self, handle: int, gpio: int) -> int:
        assert handle in self.open_handles
        self.calls.append(f"free {gpio}")
        self.claims.pop(gpio, None)
        self.owners.pop(gpio, None)
        return 0

    def gpio_read(self, handle: int, gpio: int) -> int:
        assert handle in self.open_handles
        assert self.claims.get(gpio) == "input"
        return self.levels.get(gpio, 1)


@pytest.fixture
def fake_lgpio() -> FakeLgpio:
    return FakeLgpio()


@pytest.fixture
def gpio_dev(tmp_path):
    """A /dev with the CM5's gpiochip nodes (0 = pinctrl-rp1)."""
    dev = tmp_path / "dev"
    dev.mkdir()
    for number in (0, 10, 11, 12, 13):
        (dev / f"gpiochip{number}").touch()
    return dev


class FakeSMBus:
    """smbus2.SMBus on I2C1: routes each call to the fake device at its address.

    A missing or absent device NACKs its address, which smbus2 reports as
    OSError(EREMOTEIO). Every ``i2c_rdwr`` call is one transfer (S ... P).
    """

    def __init__(self, devices: dict[int, object]) -> None:
        self.devices = devices
        self.calls: list[tuple[object, ...]] = []
        self.closed = False

    def _device(self, address: int):
        device = self.devices.get(address)
        if device is None or not getattr(device, "present", True):
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        return device

    def read_word_data(self, i2c_addr, register, _force=None):
        self.calls.append(("read_word", i2c_addr, register))
        return self._device(i2c_addr).read_word(register)

    def write_word_data(self, i2c_addr, register, value, _force=None):
        self.calls.append(("write_word", i2c_addr, register, value))
        self._device(i2c_addr).write_word(register, value)

    def read_byte_data(self, i2c_addr, register, _force=None):
        self.calls.append(("read_byte", i2c_addr, register))
        return self._device(i2c_addr).read_byte(register)

    def write_byte_data(self, i2c_addr, register, value, _force=None):
        self.calls.append(("write_byte", i2c_addr, register, value))
        self._device(i2c_addr).write_byte(register, value)

    def read_byte(self, i2c_addr, _force=None):
        # SMBus receive byte: a read with no register pointer (scan probe).
        self.calls.append(("receive_byte", i2c_addr))
        return self._device(i2c_addr).receive_byte()

    def write_byte(self, i2c_addr, value, _force=None):
        # SMBus send byte: one command byte, no data (MS5837 reset).
        self.calls.append(("send_byte", i2c_addr, value))
        self._device(i2c_addr).send_byte(value)

    def read_i2c_block_data(self, i2c_addr, register, length, _force=None):
        self.calls.append(("read_block", i2c_addr, register, length))
        return self._device(i2c_addr).read_block(register, length)

    def i2c_rdwr(self, *msgs):
        kinds = tuple("r" if msg.flags & I2C_M_RD else "w" for msg in msgs)
        self.calls.append(("rdwr", msgs[0].addr, kinds))
        for msg in msgs:
            device = self._device(msg.addr)
            if msg.flags & I2C_M_RD:
                data = bytes(device.i2c_read(msg.len))
                ctypes.memmove(msg.buf, data, len(data))
            else:
                device.i2c_write(bytes(msg))

    def close(self):
        self.closed = True


class FakeLtc4162:
    """LTC4162-LAD registers as datasheet Rev A describes them (pp.36-47).

    Settings reset to their defaults on ``power_cycle``. While en_jeita (0x29
    bit 0) is set, JEITA overwrites 0x1A/0x1B every 11 ms, so a write there is
    lost at once. Only the five settings registers the CM5 may write are
    accepted; anything else fails the test.
    """

    DEFAULTS: ClassVar[dict[int, int]] = {
        0x14: DEFAULT_SETTINGS.config_bits,  # 0
        0x15: DEFAULT_SETTINGS.iin_limit_target,  # 63
        0x1A: DEFAULT_SETTINGS.charge_current_setting,  # 31
        0x1B: DEFAULT_SETTINGS.vcharge_setting,  # 31
        0x29: DEFAULT_SETTINGS.charger_config_bits,  # en_jeita
    }
    WRITABLE = frozenset(DEFAULTS)
    READABLE = WRITABLE | frozenset(
        {0x34, 0x35, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x43, 0x4A}
    )
    # JEITA region R3 (the board's fixed 10 k / 10 k divider reads 25 C).
    JEITA: ClassVar[dict[int, int]] = {0x1A: 31, 0x1B: 31}

    def __init__(self) -> None:
        self.present = True
        self.regs: dict[int, int] = {
            **self.DEFAULTS,
            0x34: 256,  # charger_suspended
            0x35: 0,
            0x39: 0x0001,  # intvcc_gt_2p8v
            0x3A: 0,
            0x3B: 0,
            0x3C: 0,
            0x3D: 0,
            0x3E: 0,
            0x3F: 0,
            0x43: 0,
            0x4A: 0,
        }
        self.writes: list[tuple[int, int]] = []
        self.failing: set[int] = set()

    def power_cycle(self) -> None:
        self.regs.update(self.DEFAULTS)

    def settings(self) -> ChargerSettings:
        """The settings registers masked to their writable fields (no run_bsr)."""
        regs = self.regs
        return ChargerSettings(
            charger_config_bits=regs[0x29] & 0b101,
            vcharge_setting=regs[0x1B] & 0x1F,
            charge_current_setting=regs[0x1A] & 0x1F,
            iin_limit_target=regs[0x15] & 0x3F,
            config_bits=regs[0x14] & 0x2E,
        )

    def read_word(self, register: int) -> int:
        assert register in self.READABLE, f"read of {register:#04x}"
        if register in self.failing:
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        return self.regs[register]

    def write_word(self, register: int, value: int) -> None:
        assert register in self.WRITABLE, f"write of {register:#04x}"
        assert 0 <= value <= 0xFFFF
        if register in self.failing:
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        self.writes.append((register, value))
        self.regs[register] = value
        if register in self.JEITA and self.regs[0x29] & 1:
            self.regs[register] = self.JEITA[register]

    def set_telemetry(  # noqa: PLR0913 - one keyword per telemetry word
        self,
        *,
        vbat: int = 21830,
        vin: int = 0,
        vout: int = 0,
        ibat: int = 0,
        iin: int = 0,
        die: int = 13461,
        valid: bool = True,
    ) -> None:
        for register, value in (
            (0x3A, vbat),
            (0x3B, vin),
            (0x3C, vout),
            (0x3D, ibat),
            (0x3E, iin),
            (0x3F, die),
        ):
            self.regs[register] = value & 0xFFFF
        self.regs[0x4A] = 1 if valid else 0


@pytest.fixture
def fake_ltc4162() -> FakeLtc4162:
    return FakeLtc4162()


class FakeClock:
    """Monotonic seconds that advance only when a test or the code sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


def sht45_crc(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class FakeSht45:
    """SHT45 per datasheet V7.3: one-byte commands, no clock stretching.

    0xFD measures (8.3 ms max); 0x89 reads the serial number (the DS gives no
    wait). A read header before the measurement is done, or with no command
    pending, is NACKed; data can be read only once.
    """

    MEASURE = 0xFD
    READ_SERIAL = 0x89

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.present = True
        self.temperature_ticks = 0x6666  # 25.00 C
        self.humidity_ticks = 0x7AE1  # about 54 %RH
        self.serial_number = 0x1234ABCD
        self.measure_s = 0.0083
        self.serial_s = 0.0  # the DS gives none; a test can make the part slow
        self.pending: tuple[int, float] | None = None  # (command, time sent)
        self.corrupt_next = False
        self.commands: list[tuple[float, int]] = []
        self.reads: list[float] = []

    def i2c_write(self, data: bytes) -> None:
        assert data in (bytes([self.MEASURE]), bytes([self.READ_SERIAL])), (
            f"unexpected SHT45 command {data!r}"
        )
        self.commands.append((self.clock.now, data[0]))
        self.pending = (data[0], self.clock.now)

    def i2c_read(self, length: int) -> bytes:
        assert length == 6
        if self.pending is None:
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        command, sent = self.pending
        busy_s = self.measure_s if command == self.MEASURE else self.serial_s
        if self.clock.now - sent < busy_s:
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        self.pending = None
        self.reads.append(self.clock.now)
        values = (self.temperature_ticks, self.humidity_ticks)
        if command == self.READ_SERIAL:
            values = (self.serial_number >> 16, self.serial_number & 0xFFFF)
        words = []
        for ticks in values:
            word = ticks.to_bytes(2, "big")
            words.append(word + bytes([sht45_crc(word)]))
        data = bytearray(b"".join(words))
        if self.corrupt_next:
            self.corrupt_next = False
            data[1] ^= 0x01
        return bytes(data)


class FakePads:
    """WSEN-PADS registers per WE manual rev3.1 (DEVICE_ID 0xB3, CTRL_2 0x10).

    Reserved or unlisted addresses fail the test (writing them "may cause
    permanent damage"); the CM5 writes only CTRL_1.
    """

    LISTED = frozenset(
        [*range(0x0B, 0x17), 0x18, 0x19, *range(0x24, 0x2D), *range(0x78, 0x7D)]
    )

    def __init__(self) -> None:
        self.present = True
        self.regs = dict.fromkeys(self.LISTED, 0)
        self.regs.update({0x0F: 0xB3, 0x11: 0x10})
        self.writes: list[tuple[int, int]] = []
        self.set_raw(pressure=0x3F5400, temperature=0x0E42)

    def set_raw(self, *, pressure: int, temperature: int) -> None:
        for offset, byte in enumerate((pressure & 0xFFFFFF).to_bytes(3, "little")):
            self.regs[0x28 + offset] = byte
        for offset, byte in enumerate((temperature & 0xFFFF).to_bytes(2, "little")):
            self.regs[0x2B + offset] = byte

    def soft_reset(self) -> None:
        self.regs[0x10] = 0x00

    def read_byte(self, register: int) -> int:
        assert register in self.LISTED, f"read of {register:#04x}"
        return self.regs[register]

    def write_byte(self, register: int, value: int) -> None:
        assert register == 0x10, f"write of {register:#04x}"
        self.writes.append((register, value))
        self.regs[register] = value

    def read_block(self, register: int, length: int) -> list[int]:
        assert (register, length) == (0x28, 5), "one 5-byte burst from 0x28"
        return [self.regs[register + offset] for offset in range(length)]


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_sht45(fake_clock) -> FakeSht45:
    return FakeSht45(fake_clock)


@pytest.fixture
def fake_pads() -> FakePads:
    return FakePads()


@pytest.fixture
def i2c_bus(fake_ltc4162, fake_sht45, fake_pads) -> FakeSMBus:
    """I2C1 of the mainboard: charger 0x68, SHT45 0x44, WSEN-PADS 0x5C."""
    return FakeSMBus({0x68: fake_ltc4162, 0x44: fake_sht45, 0x5C: fake_pads})
