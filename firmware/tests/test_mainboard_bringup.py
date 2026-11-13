"""Offline checks of the mainboard bring-up helper; never opens hardware."""

import contextlib
import importlib.util
import io
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "mainboard_bringup", Path(__file__).resolve().parents[1] / "scripts/mainboard_bringup.py"
)
bringup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bringup)
smoke = bringup.smoke


class Port:
    def __init__(self, data=b""):
        self.data = data
        self.written = []

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, count):
        data, self.data = self.data[:count], self.data[count:]
        return data

    def write(self, data):
        self.written.append(bytes(data))


def attitude(health, sequence=1):
    payload = struct.pack(
        "<Q9f5I8HI", 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, health, 0, 0, *([1000] * 8), 0
    )
    return smoke.frame(0x90, 7, sequence, payload)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ArgumentTests(unittest.TestCase):
    def test_refuses_without_execute(self):
        for argv in (["info"], ["imu"], ["esc", "on"], ["esc", "watch"]):
            with self.subTest(argv=argv), patch.object(bringup, "run") as run, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    bringup.main(argv)
                run.assert_not_called()
                self.assertIn("--execute", stderr.getvalue())

    def test_port_defaults_to_udev_symlink(self):
        options = bringup.parser().parse_args(["--execute", "info"])
        self.assertEqual(options.port, "/dev/manafish-mcu")
        self.assertEqual(options.command, "info")

    def test_esc_actions(self):
        for action in ("on", "off", "watch"):
            with self.subTest(action=action):
                options = bringup.parser().parse_args(["--execute", "esc", action])
                self.assertEqual((options.command, options.action), ("esc", action))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bringup.parser().parse_args(["--execute", "esc", "toggle"])

    def test_subcommand_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bringup.main(["--execute"])

    def test_execute_runs_the_subcommand(self):
        with patch.object(bringup, "run") as run:
            bringup.main(["--execute", "--port", "/dev/x", "esc", "off"])
        options = run.call_args.args[0]
        self.assertEqual((options.port, options.command, options.action), ("/dev/x", "esc", "off"))


class DecodingTests(unittest.TestCase):
    def test_features(self):
        self.assertEqual(
            bringup.decode_features(0x3F),
            ["control", "settings", "pressure", "absolute quaternion", "development identity",
             "ESC power"],
        )
        self.assertNotIn("ESC power", bringup.decode_features(0x1F))
        self.assertEqual(bringup.decode_features(0x101), ["control", "0x100"])

    def test_esc_state_from_health_bits_4_to_6(self):
        self.assertEqual(bringup.esc_state(0x45), (4, "on"))
        self.assertEqual(bringup.esc_state(0x0F), (0, "absent"))
        self.assertEqual(bringup.esc_state(0x31), (3, "starting"))
        self.assertEqual(bringup.esc_state(0xD5), (5, "stopping"))  # bit 7 is not ESC state
        self.assertEqual(bringup.esc_state(0x70), (7, "unknown"))

    def test_result_names(self):
        self.assertEqual(bringup.result_name(3), "BUSY")
        self.assertEqual(bringup.result_name(6), "UNSUPPORTED")
        self.assertEqual(bringup.result_name(9), "9")

    def test_imu_means(self):
        payloads = [struct.pack("<7f", 1, 2, -9, 0.5, 0, 0, 30),
                    struct.pack("<7f", 3, 2, -11, -0.5, 0, 1, 32)]
        self.assertEqual(bringup.imu_means(payloads), (2, 2, -10, 0, 0, 0.5, 31))
        self.assertIsNone(bringup.imu_means([]))


class LinkTests(unittest.TestCase):
    def test_capabilities_uses_legacy_request_and_decodes(self):
        caps = struct.pack("<4HI", 1, 768, 628, 8, 0x3F) + b"1.0.4-mb.1"
        port = Port(smoke.frame(0x81, 0, 1, caps))
        link = smoke.Link(port)
        self.assertEqual(bringup.capabilities(link), (1, 768, 628, 8, 0x3F, "1.0.4-mb.1"))
        self.assertEqual(port.written, [smoke.legacy(0xC5, struct.pack("<BBBH", 3, 1, 0, 0))])

    def test_request_returns_busy_instead_of_raising(self):
        port = Port()
        link = smoke.Link(port)
        port.data = smoke.frame(0x80, link.session, 1, struct.pack("<BBHII", 0x26, 3, 0, 0, 0))
        self.assertEqual(bringup.request(link, 0x26, struct.pack("<I", 1)), 3)
        self.assertEqual(port.written, [smoke.frame(0x26, link.session, 1, struct.pack("<I", 1))])

    def test_request_rejects_ack_for_another_request_type(self):
        port = Port()
        link = smoke.Link(port)
        port.data = smoke.frame(0x80, link.session, 1, struct.pack("<BBHII", 0x24, 0, 0, 0, 0))
        with self.assertRaisesRegex(RuntimeError, "0x24"):
            bringup.request(link, 0x26, struct.pack("<I", 0))

    def test_watch_prints_each_state_change(self):
        data = b"".join(attitude(health, n) for n, health in
                        enumerate((0x21, 0x21, 0x31, 0x31, 0x41, 0x51, 0x11), 1))
        link = smoke.Link(Port(data))
        with quiet() as out:
            changes = bringup.watch(link, 0.05)
        self.assertEqual([state for _, state in changes], [2, 3, 4, 5, 1])
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("state 3 starting", lines[1])
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d\.\d{3} \+ *\d+\.\d\d s ")

    def test_frames_keeps_only_the_requested_kind(self):
        imu = struct.pack("<7f", 0, 0, -9.81, 0, 0, 0, 25)
        data = attitude(0x21) + smoke.frame(0x92, 7, 2, imu) + smoke.frame(0x92, 7, 3, imu)
        link = smoke.Link(Port(data))
        self.assertEqual(list(bringup.frames(link, 0.02, 0x92)), [imu, imu])
        self.assertEqual(len(link.messages), 0)
        self.assertEqual(link.last_attitude["health"], 0x21)


class EscCommandTests(unittest.TestCase):
    def run_esc(self, action, result=0):
        calls = []
        link = Mock(last_attitude={"health": 0x31})
        link.settings.side_effect = lambda generation, data: calls.append(("settings", data))
        with patch.object(bringup, "open_session", side_effect=lambda _: calls.append("hello")), \
                patch.object(bringup, "request",
                             side_effect=lambda _, kind, payload: calls.append((kind, payload))
                             or result), quiet() as out:
            bringup.esc(link, action, 0)
        return calls, out.getvalue()

    def test_on_applies_neutral_settings_then_requests_on(self):
        calls, out = self.run_esc("on")
        # The zero-power settings of control_smoke.py: ESC power stays BUSY until settings apply.
        self.assertEqual(calls, ["hello", ("settings", smoke.neutral_settings()),
                                 (0x26, struct.pack("<I", 1))])
        self.assertIn("APPLIED", out)
        self.assertIn("state 3 starting", out)

    def test_off_needs_no_settings(self):
        calls, _ = self.run_esc("off")
        self.assertEqual(calls, ["hello", (0x26, struct.pack("<I", 0))])

    def test_busy_is_reported_not_raised(self):
        _, out = self.run_esc("on", result=3)
        self.assertIn("BUSY", out)

    def test_watch_sends_no_esc_request(self):
        calls = []
        with patch.object(bringup, "open_session", side_effect=lambda _: calls.append("hello")), \
                patch.object(bringup, "request") as request, \
                patch.object(bringup, "watch", return_value=[]) as watch, quiet():
            bringup.esc(Mock(), "watch", 40)
        request.assert_not_called()
        self.assertEqual(calls, ["hello"])
        self.assertEqual(watch.call_args.args[1], 40)


if __name__ == "__main__":
    unittest.main()
