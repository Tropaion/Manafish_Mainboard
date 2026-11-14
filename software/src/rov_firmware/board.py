"""Mainboard parts of the service, wired here to keep upstream ``main.py`` small.

``main`` builds one ``Board`` after ``state.pico``, awaits ``prepare`` before the
MCU serial link opens and appends ``create_tasks`` to its own task list.
"""

import asyncio

from . import mcu_gpio
from .battery import LowBatteryMonitor
from .esc_power import EscPowerController
from .log import log_error, log_warn
from .rov_state import RovState
from .sensors.charger import ChargerMonitor
from .sensors.hull import HullSensors
from .soc_monitor import SocMonitor


class Board:
    """Hull, charger, battery and SoC monitors plus the ESC power controller."""

    def __init__(self, state: RovState) -> None:
        """Build every part and set ``state.esc_power``; nothing runs yet."""
        self._state = state
        self.hull = HullSensors(state)
        self.charger = ChargerMonitor(state)
        self.battery = LowBatteryMonitor(state)
        self.soc = SocMonitor(state)
        self.esc_power = EscPowerController(
            state, blockers=self.battery.enable_blockers()
        )
        state.esc_power = self.esc_power

    async def prepare(self) -> None:
        """Release GPIO24/25 and pulse RUN once if the MCU sits in BOOTSEL (8.4)."""
        try:
            message = await asyncio.to_thread(
                mcu_gpio.leave_stale_bootsel,
                recovery_pending=self._state.esc_firmware_recovery_required,
            )
        except Exception as error:
            # No RP1 chip or no lgpio (not a CM5): the service still starts.
            log_error(f"MCU GPIO check at start failed: {error}")
            return
        if message is not None:
            log_warn(message)

    def create_tasks(self) -> list[asyncio.Task[None]]:
        """Start the five board loops."""
        return [
            asyncio.create_task(self.hull.run(), name="hull.run"),
            asyncio.create_task(self.charger.run(), name="charger.run"),
            asyncio.create_task(self.battery.run(), name="battery.run"),
            asyncio.create_task(self.soc.run(), name="soc.run"),
            asyncio.create_task(self.esc_power.run(), name="esc_power.run"),
        ]
