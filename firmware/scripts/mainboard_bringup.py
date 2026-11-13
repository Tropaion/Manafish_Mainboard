#!/usr/bin/env python3
"""Coordinator-only Manafish mainboard (RP2354A) bring-up helper.

Requires pyserial on the host, explicit --execute and a stopped Pi service (it
owns the port). Imports the helpers of control_smoke.py: copy both files
together. Never flashes and never sends nonneutral motor commands.

info       CAPS and the decoded feature bits (0x20 = ESC power); no session.
imu        1 s means of the raw IMU frames (0x92). Level reads about
           accel (0, 0, -9.81) in the board's FRD frame.
esc on     applies control_smoke's zero-power settings (RAM-only; the MCU
           answers BUSY until settings have applied once since boot), then
           requests ESC power on (0x26) and prints the result.
esc off    requests ESC power off and prints the result.
esc watch  prints the ESC power state (ATTITUDE health bits 4-6) with
           timestamps on every change; sends nothing after HELLO.

Every subcommand but info opens a new session (HELLO); the Pi service
negotiates its own afterwards. The MCU switches ESC power off 30 s after the
last host packet, so ESC power stays on after `esc on` only while a host keeps
talking; `esc watch` shows that auto-off.
"""

import argparse
from pathlib import Path
import struct
import sys
import time

# Reuse the bench helpers; keep scripts/ free of __pycache__.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_smoke as smoke  # noqa: E402

MSG_HELLO = 0x01
MSG_ESC_POWER = 0x26
FRAME_ACK = 0x80
FRAME_CAPS = 0x81
FRAME_ATTITUDE = 0x90
FRAME_IMU = 0x92
FEATURES = ("control", "settings", "pressure", "absolute quaternion", "development identity",
            "ESC power")
RESULTS = ("APPLIED", "STAGED", "INVALID", "BUSY", "STALE", "NOT_READY", "UNSUPPORTED")
# Wire state in ATTITUDE health bits 4-6; 3 covers STARTING (lines parked) and ARMING.
ESC_STATES = ("absent", "off (hold)", "off (ready)", "starting", "on", "stopping")


def decode_features(bits):
    names = [name for bit, name in enumerate(FEATURES) if bits & (1 << bit)]
    unknown = bits & ~((1 << len(FEATURES)) - 1)
    return names + ([hex(unknown)] if unknown else [])


def esc_state(health):
    state = (health >> 4) & 7
    return state, ESC_STATES[state] if state < len(ESC_STATES) else "unknown"


def result_name(result):
    return RESULTS[result] if result < len(RESULTS) else str(result)


def imu_means(payloads):
    if not payloads:
        return None
    samples = [struct.unpack("<7f", payload) for payload in payloads]
    return tuple(sum(column) / len(samples) for column in zip(*samples))


def stamp(start):
    now = time.time()
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    return f"{clock}.{int(now * 1000) % 1000:03d} +{time.monotonic() - start:6.2f} s"


def capabilities(link):
    """Legacy C5 GET_CONTROL_CAPABILITIES, as control_smoke.run sends it."""
    link.port.write(smoke.legacy(0xC5, struct.pack("<BBBH", 3, 1, 0, 0)))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        link.pump()
        for message in tuple(link.messages):
            if message[:3] == (FRAME_CAPS, 0, 1):
                link.messages.remove(message)
                payload = message[3]
                return (*struct.unpack_from("<4HI", payload), payload[12:].decode("ascii"))
    raise RuntimeError("No control capability: old firmware or wrong port")


def open_session(link):
    capabilities(link)
    link.send(MSG_HELLO, reliable=True)


def request(link, kind, payload):
    """Reliable request that returns the ACK result (Link.send raises on all but one)."""
    link.send(kind, payload)
    sequence = link.sequence
    packet = smoke.frame(kind, link.session, sequence, payload)
    deadline = time.monotonic() + 8
    next_retry = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        link.pump()
        for message in tuple(link.messages):
            if message[:3] == (FRAME_ACK, link.session, sequence):
                link.messages.remove(message)
                request_type, result = struct.unpack_from("<BB", message[3])
                if request_type != kind:
                    raise RuntimeError(f"ACK for request {request_type:#x}, expected {kind:#x}")
                return result
        if time.monotonic() >= next_retry:
            # Same sequence: the MCU repeats its ACK without executing again.
            link.port.write(packet)
            next_retry += 0.5
    raise TimeoutError(f"request {kind:#x} outcome unknown")


def frames(link, seconds, kind):
    """Yield the payloads of one frame kind for `seconds`; Link still observes ATTITUDE."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        link.pump()
        while link.messages:
            message = link.messages.popleft()
            if message[0] == kind:
                yield message[3]


def watch(link, seconds):
    start = time.monotonic()
    changes = []
    for payload in frames(link, seconds, FRAME_ATTITUDE):
        state, name = esc_state(struct.unpack_from("<I", payload, 52)[0])
        if not changes or changes[-1][1] != state:
            changes.append((time.monotonic() - start, state))
            print(f"{stamp(start)}  state {state} {name}", flush=True)
    return changes


def info(link):
    schema, maximum, size, vectors, features, identity = capabilities(link)
    print("CAPABILITIES", schema, maximum, size, vectors, hex(features), identity, flush=True)
    print("FEATURES", ", ".join(decode_features(features)), flush=True)


def imu(link, seconds):
    open_session(link)
    for _ in range(seconds):
        payloads = list(frames(link, 1, FRAME_IMU))
        means = imu_means(payloads)
        if means is None:
            print("IMU: no 0x92 frames in 1 s", flush=True)
            continue
        healthy = link.last_attitude is not None and bool(link.last_attitude["health"] & 1)
        print("IMU 1 s mean (n={}): accel {:+.3f} {:+.3f} {:+.3f} m/s^2, "
              "gyro {:+.4f} {:+.4f} {:+.4f} rad/s, {:.1f} C, healthy {}".format(
                  len(payloads), *means, healthy), flush=True)


def esc(link, action, seconds):
    open_session(link)
    if action == "watch":
        watch(link, seconds)
        return
    if action == "on":
        link.settings(1, smoke.neutral_settings())
    start = time.monotonic()
    result = request(link, MSG_ESC_POWER, struct.pack("<I", int(action == "on")))
    print(f"{stamp(start)}  ESC_POWER {action}: {result_name(result)}", flush=True)
    link.drain(0.1)
    if link.last_attitude is not None:
        state, name = esc_state(link.last_attitude["health"])
        print(f"{stamp(start)}  state {state} {name}", flush=True)


def run(options):
    import serial  # Host runtime already supplies pyserial; not a build dependency.

    with serial.Serial(options.port, 115200, timeout=0.005, write_timeout=0.5,
                       exclusive=True) as port:
        link = smoke.Link(port)
        if options.command == "info":
            info(link)
        elif options.command == "imu":
            imu(link, options.seconds)
        else:
            esc(link, options.action, options.seconds)


def parser():
    result = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--port", default="/dev/manafish-mcu")
    result.add_argument("--execute", action="store_true", help="acknowledge serial I/O to the MCU")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="CAPS and decoded feature bits; opens no session")
    imu_parser = commands.add_parser("imu", help="1 s means of the raw IMU frames (0x92)")
    imu_parser.add_argument("--seconds", type=int, default=1, help="number of 1 s means")
    esc_parser = commands.add_parser("esc", help="ESC power: request on or off, or watch it")
    esc_parser.add_argument("action", choices=("on", "off", "watch"))
    esc_parser.add_argument("--seconds", type=float, default=40, help="watch duration")
    return result


def main(argv=None):
    options_parser = parser()
    options = options_parser.parse_args(argv)
    if not options.execute:
        options_parser.error("No hardware access without --execute; stop the Pi service first")
    run(options)


if __name__ == "__main__":
    main()
