"""CM5 GPIO through lgpio on the RP1 bank (the gpiochip labelled ``pinctrl-rp1``).

Board rules: a line is never driven high, only low or released, because every
line used here has a pull-up on the board or in config.txt. lgpio is installed
only on aarch64, so it is imported when a chip is opened, and tests inject a
fake.

lgpio 0.2.2 maps its flags to the kernel's GPIO_V2_LINE_FLAG_BIAS_* flags;
without a bias flag the kernel leaves the pad bias as it is (config.txt).
pinctrl-rp1 keeps a freed output driving its last level, so ``release``
reclaims the line as an input before freeing it.
"""

from collections.abc import Sequence
import importlib
from pathlib import Path
import re
from types import TracebackType
from typing import Protocol, Self, cast


RP1_CHIP_LABEL = "pinctrl-rp1"
_DEV = Path("/dev")
_CHIP_NAME = re.compile(r"gpiochip(\d+)")
_CHIP_INFO_LABEL = 3


class Rp1GpioError(RuntimeError):
    """The RP1 GPIO chip or lgpio is not available."""


class LgpioModule(Protocol):
    """The part of the ``lgpio`` module this code uses."""

    SET_PULL_NONE: int
    error: type[Exception]

    def gpiochip_open(self, gpiochip: int, /) -> int: ...  # noqa: D102

    def gpiochip_close(self, handle: int, /) -> int: ...  # noqa: D102

    def gpio_get_chip_info(self, handle: int, /) -> Sequence[object]: ...  # noqa: D102

    def gpio_claim_output(  # noqa: D102
        self, handle: int, gpio: int, level: int, flags: int, /
    ) -> int: ...

    def gpio_claim_input(self, handle: int, gpio: int, flags: int, /) -> int: ...  # noqa: D102

    def gpio_free(self, handle: int, gpio: int, /) -> int: ...  # noqa: D102

    def gpio_read(self, handle: int, gpio: int, /) -> int: ...  # noqa: D102


def _import_lgpio() -> LgpioModule:
    try:
        return cast(LgpioModule, importlib.import_module("lgpio"))
    except ModuleNotFoundError as error:
        msg = "lgpio is not installed (only the aarch64 CM5 image has it)"
        raise Rp1GpioError(msg) from error


def _chip_numbers(dev: Path) -> list[int]:
    numbers = []
    for path in dev.glob("gpiochip*"):
        match = _CHIP_NAME.fullmatch(path.name)
        if match is not None:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def _open_rp1_chip(lgpio: LgpioModule, dev: Path) -> int:
    for number in _chip_numbers(dev):
        try:
            handle = lgpio.gpiochip_open(number)
        except lgpio.error:
            continue
        try:
            label = lgpio.gpio_get_chip_info(handle)[_CHIP_INFO_LABEL]
        except lgpio.error:
            label = None
        if label == RP1_CHIP_LABEL:
            return handle
        lgpio.gpiochip_close(handle)
    msg = f"No GPIO chip labelled {RP1_CHIP_LABEL} in {dev}"
    raise Rp1GpioError(msg)


class Rp1Gpio:
    """An open handle on the RP1 chip; ``close`` releases every line it drives."""

    def __init__(self, lgpio: LgpioModule | None = None, *, dev: Path = _DEV) -> None:
        """Open the chip labelled ``pinctrl-rp1``; raises Rp1GpioError otherwise."""
        self._lgpio = _import_lgpio() if lgpio is None else lgpio
        self._handle = _open_rp1_chip(self._lgpio, dev)
        self._driven: set[int] = set()

    def drive_low(self, line: int) -> None:
        """Claim ``line`` as an output that starts low (no high glitch)."""
        self._lgpio.gpio_claim_output(self._handle, line, 0, 0)
        self._driven.add(line)

    def release(self, line: int) -> None:
        """Reclaim ``line`` as an input with bias off, then free it.

        Safe on a line this handle does not hold. It never drives the line.
        """
        self._lgpio.gpio_claim_input(self._handle, line, self._lgpio.SET_PULL_NONE)
        self._driven.discard(line)
        self._lgpio.gpio_free(self._handle, line)

    def read_input(self, line: int) -> int:
        """Read ``line`` as an input without changing the bias config.txt set."""
        if line in self._driven:
            msg = f"GPIO{line} is driven low by this handle; release it first"
            raise ValueError(msg)
        self._lgpio.gpio_claim_input(self._handle, line, 0)
        try:
            return self._lgpio.gpio_read(self._handle, line)
        finally:
            self._lgpio.gpio_free(self._handle, line)

    def close(self) -> None:
        """Release every line still driven (each on its own), then close the chip."""
        failure: Exception | None = None
        for line in sorted(self._driven):
            try:
                self.release(line)
            except Exception as error:  # try the other lines too
                failure = failure or error
        self._lgpio.gpiochip_close(self._handle)
        if failure is not None:
            raise failure

    def __enter__(self) -> Self:
        """Use the open chip in a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release driven lines and close the chip."""
        self.close()
