"""Exercise the bridge using the pinned Meshtastic TCP client and framed protobufs."""
import contextlib
import queue
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from pubsub import pub

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface
from src.mesh import Bridge, BROADCAST


class FakeRadio:
    def __init__(self, listener):
        self.listener = listener
        self.received = queue.Queue()
        self.connection = None

    def write(self, message):
        raw = message.SerializeToString()
        self.connection.sendall(b"\x94\xc3" + len(raw).to_bytes(2, "big") + raw)

    def run(self):
        self.connection, _ = self.listener.accept()
        with self.connection, self.connection.makefile("rb") as stream:
            while byte := stream.read(1):
                if byte != b"\x94" or stream.read(1) != b"\xc3":
                    continue
                size = int.from_bytes(stream.read(2), "big")
                packet = mesh_pb2.ToRadio.FromString(stream.read(size))
                if packet.want_config_id:
                    info = mesh_pb2.FromRadio()
                    info.my_info.my_node_num = 123
                    self.write(info)
                    node = mesh_pb2.FromRadio()
                    node.node_info.num = 123
                    node.node_info.user.id = "!0000007b"
                    node.node_info.user.long_name = "Test Zero"
                    self.write(node)
                    config = mesh_pb2.FromRadio()
                    config.config.lora.SetInParent()
                    config.config.lora.use_preset = True
                    self.write(config)
                    channel = mesh_pb2.FromRadio()
                    channel.channel.index = 0
                    channel.channel.role = 1
                    self.write(channel)
                    self.write(mesh_pb2.FromRadio(config_complete_id=packet.want_config_id))
                elif packet.HasField("packet"):
                    self.received.put(packet.packet)


class BridgeTest(unittest.TestCase):
    def test_region_gate_text_and_persistence(self):
        with (socket.socket() as server,
              tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as cleanup):
            server.bind(("127.0.0.1", 0))
            server.listen()
            radio = FakeRadio(server)
            worker = threading.Thread(target=radio.run, daemon=True)
            worker.start()
            interface = TCPInterface("127.0.0.1", portNumber=server.getsockname()[1], timeout=3)
            cleanup.callback(interface.close)
            wire, reader = socket.socketpair()
            cleanup.callback(wire.close)
            cleanup.callback(reader.close)
            path = Path(temporary) / "history.sqlite3"
            db = sqlite3.connect(path)
            cleanup.callback(db.close)
            bridge = Bridge(wire, db)
            bridge.interface = interface
            bridge.snapshot()
            self.assertIn(b"settings\tTest Zero\tUNSET", reader.recv(4096))
            with self.assertRaisesRegex(ValueError, "Choose a radio region"):
                bridge.command(f"send\t{BROADCAST}\t0\tBefore")
            bridge.command("region\tUS")
            admin = admin_pb2.AdminMessage.FromString(radio.received.get(timeout=3).decoded.payload)
            self.assertTrue(admin.set_config.lora.tx_enabled)
            self.assertEqual(admin.set_config.lora.region, 1)
            bridge.command(f"send\t{BROADCAST}\t0\tHello mesh")
            packet = radio.received.get(timeout=3)
            self.assertEqual(packet.decoded.portnum, portnums_pb2.TEXT_MESSAGE_APP)
            self.assertEqual(packet.decoded.payload, b"Hello mesh")
            self.assertEqual(packet.to, BROADCAST)
            bridge.command("send\t456\t0\tPrivate")
            packet = radio.received.get(timeout=3)
            self.assertEqual(packet.to, 456)
            self.assertTrue(packet.want_ack)
            pub.subscribe(bridge.receive, "meshtastic.receive.text")
            cleanup.callback(pub.unsubscribe, bridge.receive, "meshtastic.receive.text")
            inbound = mesh_pb2.FromRadio()
            setattr(inbound.packet, "from", 456)
            inbound.packet.to = 123
            inbound.packet.id = 99
            inbound.packet.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
            inbound.packet.decoded.payload = b"Received over TCP"
            radio.write(inbound)
            kind, received = bridge.events.get(timeout=3)
            self.assertEqual(kind, "packet")
            bridge.record_received(received)
            bridge.record_received(received)
            with contextlib.closing(sqlite3.connect(path)) as reopened:
                self.assertEqual(reopened.execute("SELECT body FROM messages ORDER BY rowid").fetchall(),
                                 [("Hello mesh",), ("Private",), ("Received over TCP",)])


if __name__ == "__main__":
    unittest.main()
