"""ROV Hardware Tools & Diagnostics."""

import argparse
import asyncio
from collections.abc import Sequence
import logging
from pathlib import Path
import sys

from . import (
    mcu_recovery,
    mock_websocket_server,
    test_packages_available,
    test_pressure_sensor,
    test_thrusters,
)


logging.basicConfig(level=logging.INFO)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools",
        description="ROV tools. Without a command: the interactive menu.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "mcu-reset",
        help="pulse the MCU's RUN line (GPIO24); the service must be stopped 35 s",
    )
    recover = commands.add_parser(
        "mcu-recover",
        help="hardware BOOTSEL (GPIO24/GPIO25), then picotool load -v -x",
    )
    recover.add_argument(
        "uf2",
        nargs="?",
        type=Path,
        help="image to load (default: the bundled ~/mcu-firmware/mainboard-v*.uf2)",
    )
    return parser


def _run_command(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    if args.command == "mcu-reset":
        return mcu_recovery.reset_main()
    return mcu_recovery.recover_main(args.uf2)


def cli(argv: Sequence[str] | None = None) -> None:
    """Run a tool subcommand, or the interactive menu without arguments."""
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        sys.exit(_run_command(args))
    _menu()


def _menu() -> None:
    """Interactive CLI menu for ROV tools."""
    _ = sys.stdout.write("\n--- ROV Diagnostic Tools ---\n")
    _ = sys.stdout.write("1. Test Pressure Sensor\n")
    _ = sys.stdout.write("2. Test Thrusters\n")
    _ = sys.stdout.write("3. Check Required Packages\n")
    _ = sys.stdout.write("4. Run Mock Websocket Server\n")
    _ = sys.stdout.write("5. Run Mock Websocket Server Locally\n")
    _ = sys.stdout.write("6. Reset the MCU (mcu-reset)\n")
    _ = sys.stdout.write("7. Recover the MCU with the bundled image (mcu-recover)\n")
    _ = sys.stdout.write("q. Quit\n")

    choice = input("\nSelect a tool > ").strip().lower()

    if choice in ["q", "quit"]:
        sys.exit(0)

    try:
        if choice == "1":
            test_pressure_sensor.main()
        elif choice == "2":
            asyncio.run(test_thrusters.main())
        elif choice == "3":
            test_packages_available.main()
        elif choice == "4":
            asyncio.run(mock_websocket_server.main())
        elif choice == "5":
            original_argv = sys.argv[:]
            sys.argv = ["mock_websocket_server.py", "--local"]
            try:
                asyncio.run(mock_websocket_server.main())
            finally:
                sys.argv = original_argv
        elif choice == "6":
            sys.exit(mcu_recovery.reset_main())
        elif choice == "7":
            sys.exit(mcu_recovery.recover_main(None))
        else:
            _ = sys.stdout.write(f"Invalid selection: {choice}")
            sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)
