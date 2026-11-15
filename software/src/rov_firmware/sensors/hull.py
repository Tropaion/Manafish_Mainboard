"""Hull climate sensors on CM5 I2C1, read at 1 Hz: SHT45 (0x44) and WSEN-PADS (0x5C).

Facts are from the verified fact sheets: Sensirion SHT4x datasheet V7.3 and the
Wuerth WSEN-PADS 2511020213301 user manual rev3.1.

SHT45: one-byte command 0xFD (high repeatability), then a separate read transfer
of 6 bytes (T MSB, LSB, CRC, RH MSB, LSB, CRC) at least 8.3 ms later; 10 ms is
used. The sensor never stretches the clock: a read header that comes too early
is NACKed and is retried. Data can be read once. CRC-8 poly 0x31, init 0xFF.
0x89 reads the 32-bit serial number in the same 6-byte layout (board-check).

WSEN-PADS: DEVICE_ID 0x0F reads 0xB3. CTRL_1 = 0x12 (ODR 1 Hz, BDU, low-power,
no LPF2) is the only register ever written; nothing else, and no reserved
address, is touched. Pressure and temperature come in one 5-byte burst from 0x28
(auto-increment is on by default, CTRL_2 bit 4). A reset returns CTRL_1 to
power-down, so it is checked every cycle and rewritten.

Each sensor follows the ``PressureSensor`` pattern: after
``SYSTEM_FAILURE_THRESHOLD`` failed cycles it is unhealthy, its reading is
cleared, and it is retried every ``HULL_RECONNECT_INTERVAL_S``. All I2C runs in
a worker thread (``asyncio.to_thread``), never on the event loop.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import ctypes
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Final, Protocol

from smbus2 import SMBus, i2c_msg

from ..constants import SYSTEM_FAILURE_THRESHOLD
from ..log import log_error, log_info, log_warn
from ..models.hull import HullData, PadsReading, Sht45Reading


if TYPE_CHECKING:
    from ..rov_state import RovState


I2C_BUS: Final = 1
HULL_READ_INTERVAL_S: Final = 1.0
HULL_RECONNECT_INTERVAL_S: Final = 10.0

SHT45_ADDRESS: Final = 0x44
SHT45_MEASURE_HIGH_PRECISION: Final = 0xFD
SHT45_MEASURE_WAIT_S: Final = 0.010  # datasheet max 8.3 ms
SHT45_READ_SERIAL: Final = 0x89
SHT45_SERIAL_WAIT_S: Final = 0.010  # the DS gives none; Sensirion's drivers wait 10 ms
SHT45_RESPONSE_LENGTH: Final = 6
SHT45_READ_ATTEMPTS: Final = 3
SHT45_RETRY_WAIT_S: Final = 0.005
SHT45_FULL_SCALE: Final = 65535  # 2^16 - 1
SHT45_HUMIDITY_MAX_PCT: Final = 100.0
CRC8_POLYNOMIAL: Final = 0x31
CRC8_INIT: Final = 0xFF
_WORD_WITH_CRC: Final = 3

PADS_ADDRESS: Final = 0x5C
PADS_DEVICE_ID_REGISTER: Final = 0x0F
PADS_DEVICE_ID: Final = 0xB3
PADS_CTRL_1: Final = 0x10
PADS_CTRL_1_VALUE: Final = 0x12  # ODR[2:0] = 001 (1 Hz), BDU = 1
PADS_CTRL_2: Final = 0x11
PADS_IF_ADD_INC: Final = 1 << 4
PADS_DATA_P_XL: Final = 0x28
PADS_DATA_LENGTH: Final = 5  # P_XL, P_L, P_H, T_L, T_H
PADS_COUNTS_PER_HPA: Final = 4096
PADS_COUNTS_PER_C: Final = 100
_PRESSURE_BYTES: Final = 3

_BYTE_MASK: Final = 0xFF
_TOP_BIT: Final = 0x80


class HullBus(Protocol):
    """The smbus2.SMBus calls the hull sensors use."""

    def i2c_rdwr(self, *i2c_msgs: i2c_msg) -> None: ...  # noqa: D102

    def read_byte_data(self, i2c_addr: int, register: int, /) -> int: ...  # noqa: D102

    def write_byte_data(self, i2c_addr: int, register: int, value: int, /) -> None: ...  # noqa: D102

    def read_i2c_block_data(  # noqa: D102
        self, i2c_addr: int, register: int, length: int, /
    ) -> list[int]: ...


def _open_bus() -> HullBus:
    return SMBus(I2C_BUS)


def crc8(data: bytes) -> int:
    """Sensirion CRC-8: poly 0x31, init 0xFF, no reflection, no final XOR."""
    crc = CRC8_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc << 1) ^ CRC8_POLYNOMIAL if crc & _TOP_BIT else crc << 1
            crc &= _BYTE_MASK
    return crc


def sht45_words(data: bytes) -> tuple[int, int]:
    """Check both CRCs of a 6-byte SHT45 response and return its two words."""
    if len(data) != SHT45_RESPONSE_LENGTH:
        msg = f"SHT45 returned {len(data)} bytes, expected {SHT45_RESPONSE_LENGTH}"
        raise ValueError(msg)
    words = []
    for start in range(0, SHT45_RESPONSE_LENGTH, _WORD_WITH_CRC):
        word, crc = data[start : start + 2], data[start + 2]
        if crc8(word) != crc:
            msg = f"SHT45 CRC mismatch on bytes {start}-{start + 2}"
            raise ValueError(msg)
        words.append(int.from_bytes(word, "big"))
    return words[0], words[1]


def sht45_convert(data: bytes) -> tuple[float, float]:
    """Check both CRCs and convert to (degrees C, %RH clamped to 0..100)."""
    temperature_ticks, humidity_ticks = sht45_words(data)
    temperature = -45.0 + 175.0 * temperature_ticks / SHT45_FULL_SCALE
    humidity = -6.0 + 125.0 * humidity_ticks / SHT45_FULL_SCALE
    return temperature, min(max(humidity, 0.0), SHT45_HUMIDITY_MAX_PCT)


def pads_convert(data: Sequence[int]) -> tuple[float, float]:
    """Convert the 5-byte burst from 0x28 to (hPa, degrees C); both are signed."""
    raw = bytes(data)
    pressure = int.from_bytes(raw[:_PRESSURE_BYTES], "little", signed=True)
    temperature = int.from_bytes(raw[_PRESSURE_BYTES:], "little", signed=True)
    return pressure / PADS_COUNTS_PER_HPA, temperature / PADS_COUNTS_PER_C


def _read_raw(bus: HullBus, address: int, length: int) -> bytes:
    message = i2c_msg.read(address, length)
    bus.i2c_rdwr(message)
    return ctypes.string_at(message.buf, message.len)


def _sht45_command(
    bus: HullBus, command: int, wait_s: float, sleep: Callable[[float], None]
) -> bytes:
    """Write a one-byte command, wait, then read 6 bytes in their own transfer."""
    bus.i2c_rdwr(i2c_msg.write(SHT45_ADDRESS, [command]))
    sleep(wait_s)
    attempt = 1
    while True:
        try:
            return _read_raw(bus, SHT45_ADDRESS, SHT45_RESPONSE_LENGTH)
        except OSError:
            # NACK on the read header: still busy. Retry the read only.
            if attempt >= SHT45_READ_ATTEMPTS:
                raise
            attempt += 1
            sleep(SHT45_RETRY_WAIT_S)


def measure_sht45(bus: HullBus, sleep: Callable[[float], None]) -> tuple[float, float]:
    """One high-precision measurement: write 0xFD, wait, read in its own transfer."""
    data = _sht45_command(
        bus, SHT45_MEASURE_HIGH_PRECISION, SHT45_MEASURE_WAIT_S, sleep
    )
    return sht45_convert(data)


def read_sht45_serial(bus: HullBus, sleep: Callable[[float], None]) -> int:
    """The 32-bit serial number: write 0x89, wait, read in its own transfer."""
    data = _sht45_command(bus, SHT45_READ_SERIAL, SHT45_SERIAL_WAIT_S, sleep)
    high, low = sht45_words(data)
    return (high << 16) | low


def check_pads(bus: HullBus) -> None:
    """Read-only: DEVICE_ID must read 0xB3 and CTRL_2 must have auto-increment on."""
    device_id = bus.read_byte_data(PADS_ADDRESS, PADS_DEVICE_ID_REGISTER)
    if device_id != PADS_DEVICE_ID:
        msg = f"WSEN-PADS DEVICE_ID reads {device_id:#04x}, expected {PADS_DEVICE_ID:#04x}"
        raise OSError(msg)
    if not bus.read_byte_data(PADS_ADDRESS, PADS_CTRL_2) & PADS_IF_ADD_INC:
        msg = "WSEN-PADS CTRL_2 has auto-increment off; the 5-byte burst would fail"
        raise OSError(msg)


def configure_pads(bus: HullBus) -> None:
    """Check the device ID and auto-increment, then start 1 Hz with BDU."""
    check_pads(bus)
    bus.write_byte_data(PADS_ADDRESS, PADS_CTRL_1, PADS_CTRL_1_VALUE)


@dataclass(slots=True)
class _Health[R]:
    """One sensor's last reading, failure count and reconnect timing.

    The ``PressureSensor`` pattern: unhealthy after ``SYSTEM_FAILURE_THRESHOLD``
    failed reads in a row (the reading is cleared), then retried every
    ``HULL_RECONNECT_INTERVAL_S``.
    """

    name: str
    reading: R | None = None
    healthy: bool = False
    failures: int = 0
    next_attempt: float = 0.0
    failure_logged: bool = False

    def due(self, now: float) -> bool:
        return self.healthy or now >= self.next_attempt

    def poll(
        self,
        read: Callable[[HullBus, float], R | None],
        bus: HullBus | None,
        bus_error: Exception | None,
        now: float,
    ) -> None:
        """Read once when due; a None reading keeps the last one."""
        if not self.due(now):
            return
        try:
            if bus is None:
                raise bus_error or OSError("I2C bus 1 is not open")
            reading = read(bus, now)
        except (OSError, ValueError) as error:
            if self.healthy:
                self._read_failed(now, error)
            else:
                self._connect_failed(now, error)
            return
        if not self.healthy:
            log_info(f"{self.name} hull sensor connected.")
        self.healthy = True
        self.failures = 0
        self.failure_logged = False
        if reading is not None:
            self.reading = reading

    def _connect_failed(self, now: float, error: Exception) -> None:
        self.next_attempt = now + HULL_RECONNECT_INTERVAL_S
        if not self.failure_logged:
            log_warn(
                f"{self.name} hull sensor is unavailable; retrying every "
                f"{HULL_RECONNECT_INTERVAL_S:.0f} s. Error: {error}"
            )
            self.failure_logged = True

    def _read_failed(self, now: float, error: Exception) -> None:
        self.failures += 1
        if self.failures < SYSTEM_FAILURE_THRESHOLD:
            return
        log_error(
            f"{self.name} hull sensor failed {self.failures} times, disabled until "
            f"it reconnects. Error: {error}"
        )
        self.reading = None
        self.healthy = False
        self.failures = 0
        self.next_attempt = now + HULL_RECONNECT_INTERVAL_S
        self.failure_logged = True


class HullSensors:
    """SHT45 and WSEN-PADS at 1 Hz; publishes ``state.hull``."""

    def __init__(
        self,
        state: "RovState",
        *,
        bus_factory: Callable[[], HullBus] = _open_bus,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Create the reader; the bus opens on the first cycle."""
        self._state = state
        self._bus_factory = bus_factory
        self._clock = clock
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._bus: HullBus | None = None
        self._sht45 = _Health[Sht45Reading]("SHT45")
        self._pads = _Health[PadsReading]("WSEN-PADS")

    def _bus_for(self, now: float) -> tuple[HullBus | None, Exception | None]:
        if self._bus is not None:
            return self._bus, None
        if not (self._sht45.due(now) or self._pads.due(now)):
            return None, None
        try:
            self._bus = self._bus_factory()
        except OSError as error:
            return None, error
        return self._bus, None

    def cycle(self) -> None:
        """Read both sensors once and publish ``state.hull`` (blocking I2C)."""
        now = self._clock()
        bus, bus_error = self._bus_for(now)
        self._sht45.poll(self._read_sht45, bus, bus_error, now)
        self._pads.poll(self._read_pads, bus, bus_error, now)
        self._state.hull = HullData(
            sht45=self._sht45.reading,
            pads=self._pads.reading,
            sht45_healthy=self._sht45.healthy,
            pads_healthy=self._pads.healthy,
        )

    def _read_sht45(self, bus: HullBus, now: float) -> Sht45Reading:
        temperature, humidity = measure_sht45(bus, self._sleep)
        return Sht45Reading(
            temperature_c=temperature, humidity_pct=humidity, sample_time=now
        )

    def _read_pads(self, bus: HullBus, now: float) -> PadsReading | None:
        if not self._pads.healthy:
            configure_pads(bus)
            return None  # the first 1 Hz sample is not ready yet
        ctrl_1 = bus.read_byte_data(PADS_ADDRESS, PADS_CTRL_1)
        if ctrl_1 != PADS_CTRL_1_VALUE:
            # A reset puts it in power-down, where the data registers are stale.
            log_warn(
                f"WSEN-PADS CTRL_1 reads {ctrl_1:#04x} (reset?); writing "
                f"{PADS_CTRL_1_VALUE:#04x} again"
            )
            bus.write_byte_data(PADS_ADDRESS, PADS_CTRL_1, PADS_CTRL_1_VALUE)
            return None
        data = bus.read_i2c_block_data(PADS_ADDRESS, PADS_DATA_P_XL, PADS_DATA_LENGTH)
        pressure, temperature = pads_convert(data)
        return PadsReading(
            pressure_hpa=pressure, temperature_c=temperature, sample_time=now
        )

    async def run(self) -> None:
        """Read the hull sensors once per second; the I2C runs in a worker thread."""
        while True:
            started = self._clock()
            try:
                await asyncio.to_thread(self.cycle)
            except Exception as error:
                # A sensor fault must not end the service's task group.
                log_error(f"Hull sensor cycle failed: {error}")
            elapsed = self._clock() - started
            await self._async_sleep(min(max(HULL_READ_INTERVAL_S - elapsed, 0.0), 1.0))
