"""The CM5 has no BMI270: 0x68 on I2C1 is the LTC4162 charger.

The BMI270 sits on the MCU's SPI1. A Pi-side BMI270 driver would write its
config blob into the charger, so no Pi-side code may import the bmi270
package. This also catches a subtree pull that brings back upstream's imu.py
or test_imu.py.
"""

from pathlib import Path
import re


SRC = Path(__file__).resolve().parents[1] / "src"


def test_no_pi_side_code_imports_bmi270():
    importing = re.compile(r"^\s*(from|import)\s+bmi270\b", re.MULTILINE)

    offenders = [
        path.relative_to(SRC).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if importing.search(path.read_text())
    ]

    assert offenders == []
