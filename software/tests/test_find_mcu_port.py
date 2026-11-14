"""find_mcu_port: udev symlink, then a sysfs scan for 2e8a:0009, never any ttyACM."""

import asyncio
from pathlib import Path

import pytest

from rov_firmware import serial as serial_module
from rov_firmware.constants import MCU_FIRST_BOOT_RETRY_LIMIT
from rov_firmware.mcu_usb import find_mcu_port
from rov_firmware.serial import SerialManager
from rov_firmware.websocket.receive import mcu as mcu_receive
from tools import test_thrusters


class Sysfs:
    """A fake /dev plus /sys/class/tty with USB-backed ttyACM entries."""

    def __init__(self, root: Path) -> None:
        self.dev = root / "dev"
        self.tty = root / "sys" / "class" / "tty"
        self.usb = root / "sys" / "devices" / "usb1"
        for path in (self.dev, self.tty, self.usb):
            path.mkdir(parents=True)
        self.symlink = self.dev / "manafish-mcu"

    def add_acm(
        self, name: str, vendor: str, product: str, product_name: str | None = None
    ) -> None:
        usb_device = self.usb / f"1-{name[-1]}"
        interface = usb_device / f"{usb_device.name}:1.0"
        interface.mkdir(parents=True)
        (usb_device / "idVendor").write_text(f"{vendor}\n")
        (usb_device / "idProduct").write_text(f"{product}\n")
        if product_name is not None:
            (usb_device / "product").write_text(f"{product_name}\n")
        entry = self.tty / name
        entry.mkdir()
        (entry / "device").symlink_to(interface)
        (self.dev / name).touch()

    def find(self) -> str | None:
        return find_mcu_port(symlink=self.symlink, tty_class=self.tty, dev=self.dev)


@pytest.fixture
def sysfs(tmp_path):
    return Sysfs(tmp_path)


def test_udev_symlink_wins(sysfs):
    sysfs.add_acm("ttyACM0", "2e8a", "0009", "Manafish Mainboard")
    sysfs.symlink.symlink_to(sysfs.dev / "ttyACM0")

    assert sysfs.find() == str(sysfs.symlink)


def test_dangling_symlink_falls_back_to_the_sysfs_scan(sysfs):
    sysfs.symlink.symlink_to(sysfs.dev / "ttyACM9")
    sysfs.add_acm("ttyACM1", "2e8a", "0009")

    assert sysfs.find() == str(sysfs.dev / "ttyACM1")


def test_scan_ignores_other_acm_devices(sysfs):
    sysfs.add_acm("ttyACM0", "1d50", "6018")  # some other CDC device
    sysfs.add_acm("ttyACM1", "2e8a", "000c")  # Raspberry Pi Debug Probe
    sysfs.add_acm("ttyACM2", "2e8a", "0009")

    assert sysfs.find() == str(sysfs.dev / "ttyACM2")


def test_scan_prefers_the_mainboard_product_name(sysfs):
    sysfs.add_acm("ttyACM0", "2e8a", "0009", "Pico")
    sysfs.add_acm("ttyACM1", "2e8a", "0009", "Manafish Mainboard")

    assert sysfs.find() == str(sysfs.dev / "ttyACM1")


def test_no_blind_ttyacm_fallback(sysfs):
    sysfs.add_acm("ttyACM0", "1d50", "6018")
    (sysfs.dev / "ttyACM5").touch()  # a node without a matching sysfs entry

    assert sysfs.find() is None


def test_serial_manager_uses_find_mcu_port(rov_state, monkeypatch):
    monkeypatch.setattr(serial_module, "find_mcu_port", lambda: "/dev/manafish-mcu")

    port = asyncio.run(SerialManager(rov_state)._find_mcu_port())

    assert port == "/dev/manafish-mcu"


def test_serial_manager_logs_a_missing_port(rov_state, monkeypatch):
    errors: list[str] = []
    monkeypatch.setattr(serial_module, "find_mcu_port", lambda: None)
    monkeypatch.setattr(serial_module, "log_error", errors.append)

    assert asyncio.run(SerialManager(rov_state)._find_mcu_port()) is None
    assert errors
    assert asyncio.run(SerialManager(rov_state)._find_mcu_port(log_missing=False)) is (
        None
    )
    assert len(errors) == 1


def test_thruster_tool_uses_find_mcu_port(monkeypatch):
    monkeypatch.setattr(test_thrusters, "find_mcu_port", lambda: None)
    with pytest.raises(RuntimeError, match="No MCU port"):
        test_thrusters._find_port()

    monkeypatch.setattr(test_thrusters, "find_mcu_port", lambda: "/dev/manafish-mcu")
    assert test_thrusters._find_port() == "/dev/manafish-mcu"


class _Writer:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
def first_boot(rov_state, monkeypatch):
    """A SerialManager whose port lookups follow ``ports`` (None when empty)."""
    ports: list[str] = []
    flashes: list[object] = []
    usb_products: set[str] = set()

    async def open_connection(**_kwargs):
        return object(), _Writer()

    async def flash(_state, board, **_kwargs):
        flashes.append(board)
        return True

    monkeypatch.setattr(
        serial_module, "find_mcu_port", lambda: ports.pop(0) if ports else None
    )
    monkeypatch.setattr(serial_module, "rp_usb_products", lambda: usb_products)
    monkeypatch.setattr(serial_module, "open_serial_connection", open_connection)
    monkeypatch.setattr(serial_module, "log_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(serial_module, "log_error", lambda _message: None)
    monkeypatch.setattr(serial_module, "log_warn", lambda _message: None)
    monkeypatch.setattr(mcu_receive, "flash_mcu_firmware", flash)
    return SerialManager(rov_state), ports, flashes, usb_products


def test_first_boot_flash_runs_once_for_an_mcu_never_seen(first_boot, rov_state):
    manager, _ports, flashes, _usb = first_boot

    async def run():
        for _ in range(MCU_FIRST_BOOT_RETRY_LIMIT * 3):
            assert not await manager.ensure_connection()

    asyncio.run(run())
    assert flashes == [rov_state.rov_config.mcu_board]


def test_usb_loss_after_a_connection_never_triggers_the_first_boot_flash(
    first_boot,
):
    # The flash would reset a live MCU through RUN/BOOTSEL while the host has no
    # session to send ESC power off (wiki 6.2).
    manager, ports, flashes, _usb = first_boot
    ports.append("/dev/manafish-mcu")

    async def run():
        assert await manager.initialize(notify=False)
        await manager.handle_connection_lost("USB link dropped")
        for _ in range(MCU_FIRST_BOOT_RETRY_LIMIT * 4):
            assert not await manager.ensure_connection()

    asyncio.run(run())
    assert flashes == []


def test_an_mcu_left_in_bootsel_after_a_connection_gets_one_automatic_flash(
    first_boot, rov_state
):
    # A failed flash leaves the MCU in BOOTSEL (2e8a:000f), where it holds no ESC
    # power; the first-boot flash may retry it once, after 5 misses in a row.
    manager, ports, flashes, usb_products = first_boot
    ports.append("/dev/manafish-mcu")

    async def run():
        assert await manager.initialize(notify=False)
        await manager.handle_connection_lost("Flash failed")
        usb_products.add("000f")
        for _ in range(MCU_FIRST_BOOT_RETRY_LIMIT - 1):
            assert not await manager.ensure_connection()
        assert flashes == []
        for _ in range(MCU_FIRST_BOOT_RETRY_LIMIT * 3):
            assert not await manager.ensure_connection()

    asyncio.run(run())
    assert flashes == [rov_state.rov_config.mcu_board]


def test_the_misses_after_a_flash_count_from_its_end(first_boot, rov_state):
    # Right after `picotool load -x` the boot ROM may still be listed (000f):
    # a miss counted before the flash must not make that an instant reflash.
    manager, ports, flashes, usb_products = first_boot
    ports.append("/dev/manafish-mcu")

    async def run():
        assert await manager.initialize(notify=False)
        await manager.handle_connection_lost("USB link dropped")
        for _ in range(MCU_FIRST_BOOT_RETRY_LIMIT * 2):
            assert not await manager.ensure_connection()
        rov_state.mcu_flashing = True
        usb_products.add("000f")
        assert not await manager.ensure_connection()
        rov_state.mcu_flashing = False
        for _ in range(MCU_FIRST_BOOT_RETRY_LIMIT - 1):
            assert not await manager.ensure_connection()

    asyncio.run(run())
    assert flashes == []
