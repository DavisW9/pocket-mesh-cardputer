#!/usr/bin/env python3
"""App supervisor and adapter for the Meshtastic client API."""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import sys

vendored = Path(__file__).resolve().parent / "python"
if vendored.is_dir():
    sys.path.insert(0, str(vendored))

BROADCAST = 0xFFFFFFFF
REGIONS = ("US", "EU_868", "ANZ", "NZ_865", "KR", "TW", "JP", "SG_923", "TH", "MY_919", "PH_915")
PRESETS = ("LONG_FAST", "LONG_MODERATE", "LONG_SLOW", "MEDIUM_FAST", "MEDIUM_SLOW", "SHORT_FAST", "SHORT_SLOW")
EXPANDER = 0x43
I2C_SLAVE = 0x0703
POWER = Path("/sys/class/leds/ext_5v_out/brightness")
MUX = Path("/sys/class/leds/ext_usb_gpio_fun/brightness")


def parent_death():
    """Install before exec, including when the supervisor dies during fork."""
    parent = os.getppid()
    if parent == 1 or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0):
        os._exit(125)
    if os.getppid() != parent:
        os._exit(125)


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def read_register(fd, reg):
    os.write(fd, bytes([reg]))
    return os.read(fd, 1)[0]


@contextlib.contextmanager
def powered_cap():
    # Stock BSP exposes the Cap supply through this LED class device. Preserve
    # the launcher's setting and change only the antenna switch bit on the Cap.
    with open("/dev/spidev0.1", "r+b", buffering=0) as spi:
        fcntl.flock(spi, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for device in ("/dev/gpiochip0", "/dev/i2c-1"):
            with open(device, "r+b", buffering=0):
                pass
        before = POWER.read_text().strip()
        if MUX.read_text().strip() != "0":
            raise OSError(errno.EBUSY, "The expansion port is in USB mode. Set it to GPIO first.", str(MUX))
        changed = before == "0"
        try:
            if changed:
                POWER.write_text("1")
                time.sleep(0.15)
            fd = os.open("/dev/i2c-1", os.O_RDWR | os.O_CLOEXEC)
            try:
                fcntl.ioctl(fd, I2C_SLAVE, EXPANDER)
                read_register(fd, 0)
                original = {reg: read_register(fd, reg) for reg in (2, 1, 3)}
                try:
                    for reg, bit in ((2, 0), (1, 1), (3, 0)):
                        os.write(fd, bytes([reg, (original[reg] & ~1) | bit]))
                    yield
                finally:
                    for reg in (3, 1, 2):
                        with contextlib.suppress(OSError):
                            os.write(fd, bytes([reg, (read_register(fd, reg) & ~1) | (original[reg] & 1)]))
            finally:
                os.close(fd)
        finally:
            if changed:
                POWER.write_text(before)


class Bridge:
    def __init__(self, wire, database, demo=False):
        self.wire = wire
        self.db = database
        self.demo = demo
        self.events = queue.SimpleQueue()
        self.interface = None
        self.demo_region = "US"
        self.demo_preset = "LONG_FAST"
        self.demo_owner = "Field Kit"
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            sender INTEGER, destination INTEGER, channel INTEGER, packet INTEGER,
            time INTEGER, body TEXT, UNIQUE(sender, packet))""")
        if demo:
            self.add_message(0x4FE210D4, BROADCAST, 0, 1, "Trailhead is clear. See you at camp.")
            self.add_message(0xA370C001, BROADCAST, 0, 2, "Copy. I have the ridge in view.")
            self.add_message(0x4FE210D4, 0xA370C001, 0, 3, "I marked the water stop.")

    def emit(self, *fields):
        line = "\t".join(str(value).replace("\n", " ").replace("\r", " ").replace("\t", " ") for value in fields)
        self.wire.sendall((line + "\n").encode())

    def add_message(self, sender, destination, channel, packet, body, timestamp=None):
        self.db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                        (sender, destination, channel, packet, timestamp or int(time.time()), body))
        self.db.commit()

    def receive(self, packet, interface):
        self.events.put(("packet", packet))

    def record_received(self, packet):
        body = packet.get("decoded", {}).get("text")
        if body and "id" in packet:
            self.add_message(packet["from"], packet.get("to", BROADCAST), packet.get("channel", 0),
                             packet["id"], body, packet.get("rxTime"))
            self.snapshot()

    def snapshot(self):
        from datetime import datetime
        from meshtastic.protobuf import config_pb2
        self.emit("reset")
        if self.demo:
            self.emit("settings", self.demo_owner, self.demo_region, self.demo_preset)
            self.emit("gps", "Preview / position fix")
            self.emit("channel", 0, "LongFast")
            self.emit("identity", 0xA370C001)
            self.emit("node", 0x4FE210D4, "Trail Relay", "Heard just now", "35.68, -83.53")
        elif self.interface:
            local = self.interface.localNode
            lora = local.localConfig.lora
            self.emit("identity", local.nodeNum)
            cfg = config_pb2.Config.LoRaConfig
            own = (self.interface.getMyNodeInfo() or {})
            owner = own.get("user", {})
            self.emit("settings", owner.get("longName", ""), cfg.RegionCode.Name(lora.region),
                      cfg.ModemPreset.Name(lora.modem_preset))
            self.emit("gps", position(own) or "No position fix reported")
            for ch in local.channels or []:
                if ch.role:
                    self.emit("channel", ch.index, ch.settings.name or ("Primary" if ch.index == 0 else "Channel " + str(ch.index)))
            nodes = list((self.interface.nodesByNum or {}).items())
            for num, node in sorted(nodes, key=lambda item: (item[1].get("lastHeard") or 0), reverse=True)[:150]:
                if num == local.nodeNum:
                    continue
                last = node.get("lastHeard")
                heard = datetime.fromtimestamp(last).strftime("%b %d %H:%M") if last else "No recent traffic"
                self.emit("node", num, node.get("user", {}).get("longName") or f"!{num:08x}",
                          heard, position(node) or "Position unavailable")
        else:
            self.emit("gps", "Radio offline")
        for sender, dest, chan, timestamp, body in self.db.execute(
                "SELECT sender,destination,channel,time,body FROM messages ORDER BY rowid DESC LIMIT 100"):
            self.emit("message", sender, dest, chan, datetime.fromtimestamp(timestamp).strftime("%H:%M"), body)
        self.emit("ready")

    def command(self, line):
        from meshtastic.protobuf import config_pb2
        op, *args = line.split("\t")
        if op == "refresh":
            self.snapshot()
            return
        if not self.demo and not self.interface:
            raise ValueError("Radio is offline. Check the status and relaunch.")
        if op == "send":
            dest, channel, body = args
            dest, channel = int(dest), int(channel)
            if not body.strip() or len(body.encode()) > 200:
                raise ValueError("Enter a message of 1–200 bytes.")
            if self.demo:
                self.add_message(0xA370C001, dest, channel, time.time_ns(), body)
            else:
                local = self.interface.localNode
                lora = local.localConfig.lora
                if not lora.region or not lora.tx_enabled:
                    raise ValueError("Choose a radio region in Settings before sending.")
                if not any(ch.index == channel and ch.role for ch in local.channels):
                    raise ValueError("This channel is no longer enabled.")
                packet = self.interface.sendText(body, destinationId=dest, channelIndex=channel,
                                                 wantAck=dest != BROADCAST)
                self.add_message(local.nodeNum, dest, channel, packet.id, body)
            self.emit("sent", "Added to radio queue" if not self.demo else "Saved in preview")
        elif op in ("region", "preset"):
            value, = args
            cfg = config_pb2.Config.LoRaConfig
            if value not in (REGIONS if op == "region" else PRESETS):
                raise ValueError("Select a value from the list.")
            if self.demo:
                if op == "region":
                    self.demo_region = value
                else:
                    self.demo_preset = value
            else:
                local = self.interface.localNode
                lora = local.localConfig.lora
                previous = cfg()
                previous.CopyFrom(lora)
                if op == "region":
                    lora.region = cfg.RegionCode.Value(value)
                    lora.tx_enabled = True
                else:
                    lora.use_preset = True
                    lora.modem_preset = cfg.ModemPreset.Value(value)
                try:
                    local.writeConfig("lora")
                except Exception:
                    lora.CopyFrom(previous)
                    raise
            self.emit("notice", "Settings sent to the radio")
        elif op == "owner":
            owner, = args
            if not owner.strip() or len(owner.encode()) > 39:
                raise ValueError("Enter a node name of 1–39 bytes.")
            if self.demo:
                self.demo_owner = owner
            else:
                self.interface.localNode.setOwner(long_name=owner)
            self.emit("notice", "Node name saved")
        elif op == "channel-url":
            url, = args
            match = re.fullmatch(r"https://meshtastic\.org/(?:e|d|v)/(?:\?add=true)?#([A-Za-z0-9_-]+={0,2})", url)
            if not match:
                raise ValueError("Enter a Meshtastic channel share URL.")
            if not self.demo:
                local = self.interface.localNode
                from base64 import urlsafe_b64decode
                from meshtastic.protobuf import apponly_pb2, channel_pb2
                encoded = match.group(1).rstrip("=")
                sharing = apponly_pb2.ChannelSet.FromString(urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                names = {ch.settings.name for ch in local.channels if ch.role}
                free = [ch for ch in local.channels if not ch.role and ch.index != 0]
                additions = []
                for settings in sharing.settings:
                    if settings.name and settings.name not in names:
                        if len(settings.psk) not in (0, 1, 16, 32):
                            raise ValueError("The share URL has an invalid channel key.")
                        names.add(settings.name)
                        additions.append(settings)
                if not additions:
                    raise ValueError("The URL contains no new named channels.")
                if len(additions) > len(free):
                    raise ValueError("There are not enough free channel slots.")
                for ch, settings in zip(free, additions):
                    ch.settings.CopyFrom(settings)
                    ch.role = channel_pb2.Channel.SECONDARY
                    local.writeChannel(ch.index)
            self.emit("notice", "Channel import submitted")
        else:
            raise ValueError("Unknown command")
        self.snapshot()


def position(node):
    value = node.get("position", {})
    if "latitude" in value and "longitude" in value:
        return f"{value['latitude']:.4f}, {value['longitude']:.4f}"
    return ""


def read_wire(wire, events):
    with wire.makefile("r", encoding="utf-8") as stream:
        try:
            for line in stream:
                events.put(("command", line.rstrip("\n")))
        finally:
            events.put(("closed", None))


def connect_radio(port, daemon, events, log_path, simulated):
    from meshtastic.tcp_interface import TCPInterface
    deadline = time.monotonic() + 30
    while daemon.poll() is None and time.monotonic() < deadline:
        log = log_path.read_text(errors="replace")
        if simulated or re.search(r"SX1262.*(init success|initialized)|SX126x.*init success", log, re.I):
            try:
                sock = TCPInterface(hostname="127.0.0.1", portNumber=port, timeout=8)
            except OSError:
                time.sleep(0.25)
                continue
            except Exception as exc:
                events.put(("error", f"Meshtastic connection failed: {exc}"))
                return
            events.put(("connected", sock))
            return
        time.sleep(0.25)
    log = log_path.read_text(errors="replace")
    if re.search(r"device or resource busy|already in use", log, re.I):
        detail = "Cap is busy. Close the other LoRa app and relaunch."
    elif re.search(r"permission denied|operation not permitted", log, re.I):
        detail = "Radio access denied. Check stock OS device permissions."
    else:
        detail = "SX1262 did not start. Check the Cap and relaunch. See radio.log for details."
    events.put(("error", detail))


def hardware_error(exc):
    if exc.errno in (errno.EACCES, errno.EPERM):
        return f"Access denied: {exc.filename or 'Cap bus'}. Check stock OS device permissions."
    if exc.filename == str(MUX) and exc.errno == errno.EBUSY:
        return exc.strerror
    if exc.errno in (errno.EAGAIN, errno.EBUSY):
        return "Cap is busy. Close the other LoRa app and relaunch."
    if exc.errno in (errno.ENXIO, errno.EREMOTEIO):
        return "Cap not detected on I2C. Power off and check the connection."
    return f"Cannot start the Cap: {exc}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui", required=True)
    parser.add_argument("--radio")
    parser.add_argument("--config")
    parser.add_argument("--loopback")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--sim-radio", action="store_true")
    args = parser.parse_args()
    if not args.demo and not (args.radio and args.config and args.loopback):
        parser.error("--radio, --config and --loopback are required outside the desktop preview")
    os.umask(0o077)
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "cardputerzero-mesh"
    if not args.demo and not args.sim_radio:
        root.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as cleanup:
        if not args.demo and not args.sim_radio:
            lock = cleanup.enter_context(open(root / "app.lock", "a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        wire, peer = socket.socketpair()
        cleanup.callback(wire.close)
        # Bound writes if the UI hangs while leaving the reader blocking.
        import struct
        wire.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("@ll", 2, 0))
        with peer:
            ui = subprocess.Popen([args.ui], pass_fds=(peer.fileno(),),
                                  env={**os.environ, "CZ_MESH_FD": str(peer.fileno())},
                                  preexec_fn=parent_death if os.uname().sysname == "Linux" else None)
        cleanup.callback(stop, ui)
        database = sqlite3.connect(":memory:" if args.demo or args.sim_radio else root / "messages.sqlite3")
        cleanup.callback(database.close)
        bridge = Bridge(wire, database, args.demo)
        quitting = False

        def quit_app(signum, frame):
            nonlocal quitting
            quitting = True

        signal.signal(signal.SIGTERM, quit_app)
        signal.signal(signal.SIGINT, quit_app)
        daemon = None
        if args.demo:
            bridge.emit("status", "PREVIEW / NO RADIO")
        elif os.geteuid() == 0 and not args.sim_radio:
            bridge.emit("status", "Launch from a standard CardputerZero user account.")
        else:
            bridge.emit("status", "Starting LoRa Cap…")
            try:
                if not args.sim_radio:
                    cleanup.enter_context(powered_cap())
                import tempfile
                radio_home = Path(cleanup.enter_context(tempfile.TemporaryDirectory())) if args.sim_radio else root / "radio"
                radio_home.mkdir(exist_ok=True)
                log_path = radio_home / "radio.log"
                with open(log_path, "wb") as log:
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                    env = {**os.environ, "HOME": str(radio_home), "XDG_DATA_HOME": str(radio_home),
                           "XDG_CONFIG_HOME": str(radio_home), "LD_PRELOAD": args.loopback}
                    command = [args.radio, "-c", args.config, "-p", str(port), "--fsdir=" + str(radio_home)]
                    if args.sim_radio:
                        command.append("-s")
                    daemon = subprocess.Popen(command, cwd=radio_home, env=env,
                                              stdout=log, stderr=subprocess.STDOUT,
                                              preexec_fn=parent_death)
                cleanup.callback(stop, daemon)
                threading.Thread(target=connect_radio, args=(port, daemon, bridge.events, log_path, args.sim_radio),
                                 daemon=True).start()
            except OSError as exc:
                bridge.emit("status", hardware_error(exc))
        threading.Thread(target=read_wire, args=(wire, bridge.events), daemon=True).start()
        bridge.snapshot()
        last_refresh = time.monotonic()
        try:
            while not quitting and ui.poll() is None:
                try:
                    kind, data = bridge.events.get(timeout=0.25)
                except queue.Empty:
                    if bridge.interface and time.monotonic() - last_refresh > 5:
                        bridge.snapshot()
                        last_refresh = time.monotonic()
                    if daemon and daemon.poll() is not None:
                        daemon = None
                        if bridge.interface:
                            with contextlib.suppress(Exception):
                                bridge.interface.close()
                            bridge.interface = None
                        bridge.emit("status", "Radio stopped. Reopen Mesh to retry.")
                        bridge.snapshot()
                    continue
                if kind == "closed":
                    break
                if kind == "connected":
                    from pubsub import pub
                    bridge.interface = data
                    pub.subscribe(bridge.receive, "meshtastic.receive.text")
                    bridge.emit("status", "RADIO ONLINE")
                    bridge.snapshot()
                elif kind == "error":
                    if daemon:
                        stop(daemon)
                        daemon = None
                    bridge.emit("status", data)
                elif kind == "packet":
                    bridge.record_received(data)
                elif kind == "command":
                    try:
                        bridge.command(data)
                    except Exception as exc:
                        bridge.emit("notice", str(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, BlockingIOError):
            pass
        finally:
            if bridge.interface:
                with contextlib.suppress(Exception):
                    bridge.interface.close()


if __name__ == "__main__":
    main()
