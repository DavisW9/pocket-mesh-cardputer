#!/usr/bin/env python3
"""Build a deterministic Debian archive from the ARM64 app and pinned inputs."""
import argparse
import hashlib
import io
import json
import lzma
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
APP = "usr/lib/cardputerzero-mesh/"
DOC = "usr/share/doc/cardputerzero-mesh/"
LAUNCHER = "usr/share/APPLaunch/"


def read_ar(blob):
    if not blob.startswith(b"!<arch>\n"):
        raise ValueError("Invalid Debian archive")
    offset = 8
    while offset < len(blob):
        header = blob[offset:offset + 60]
        size = int(header[48:58])
        name = header[:16].decode().strip().rstrip("/")
        offset += 60
        yield name, blob[offset:offset + size]
        offset += size + size % 2


def arm64(data):
    return data[:6] == b"\x7fELF\x02\x01" and data[18:20] == b"\xb7\x00"


def tar(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, (content, mode) in sorted(files.items()):
            entry = tarfile.TarInfo("./" + name)
            entry.size = len(content)
            entry.mode = mode
            entry.uname = entry.gname = "root"
            archive.addfile(entry, io.BytesIO(content))
    return lzma.compress(buffer.getvalue())


def ar(files):
    archive = bytearray(b"!<arch>\n")
    for name, data in files:
        archive.extend(f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode())
        archive.extend(data)
        if len(data) % 2:
            archive.extend(b"\n")
    return bytes(archive)


def get_member(data, name):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return archive.extractfile("./" + name).read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--daemon-deb", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    binary = args.daemon_deb.read_bytes()
    checksum = (ROOT / "packaging/meshtasticd.lock").read_text().splitlines()[1]
    if hashlib.sha256(binary).hexdigest() != checksum:
        raise SystemExit("Meshtasticd Debian archive does not match the pinned SHA-256")
    parts = dict(read_ar(binary))
    daemon = get_member(parts["data.tar.xz"], "usr/bin/meshtasticd")
    control = get_member(parts["control.tar.xz"], "control").decode()
    depends = next(line[9:] for line in control.splitlines() if line.startswith("Depends: "))
    depends = ", ".join(dep for dep in depends.split(", ") if dep != "adduser")

    manifest = json.loads((ROOT / "app-builder.json").read_text())
    name, version = manifest["package_name"], manifest["version"]
    contents = {}

    def add(source, dest, executable=False):
        contents[dest] = (source.read_bytes(), 0o755 if executable else 0o644)

    contents[APP + "meshtasticd"] = (daemon, 0o755)
    add(args.build / "mesh-ui", APP + "mesh-ui", True)
    add(args.build / "libmesh-loopback.so", APP + "libmesh-loopback.so")
    for name_in_app in ("meshtasticd", "mesh-ui", "libmesh-loopback.so"):
        if not arm64(contents[APP + name_in_app][0]):
            raise SystemExit(f"{name_in_app} must be a Linux ARM64 binary")
    add(ROOT / "src/mesh.py", APP + "mesh.py")
    for file in ("launch", "cardputerzero.yaml"):
        add(ROOT / "packaging" / file, APP + file, file == "launch")
    add(ROOT / "packaging/cardputerzero-mesh.desktop", LAUNCHER + "applications/cardputerzero-mesh.desktop")
    add(ROOT / manifest["store"]["icon"], LAUNCHER + "share/images/cardputerzero-mesh.png")
    for file in (ROOT / "packaging").glob("*.txt"):
        add(file, DOC + file.name)
    add(ROOT / "README.md", DOC + "README.md")
    add(ROOT / "LICENSE", DOC + "LICENSE")
    if not (args.vendor / "meshtastic/protobuf/mesh_pb2.py").is_file():
        raise SystemExit("Missing bundled Python client")
    for file in args.vendor.rglob("*"):
        relative = file.relative_to(args.vendor)
        if file.is_file() and relative.parts[0] != "bin" and "__pycache__" not in relative.parts:
            if file.suffix == ".so" and not arm64(file.read_bytes()):
                raise SystemExit(f"Wrong Python extension architecture: {relative}")
            add(file, APP + "python/" + relative.as_posix())

    size = (sum(len(content) for content, _ in contents.values()) + 1023) // 1024
    metadata = (
        f"Package: {name}\nVersion: {version}\nArchitecture: arm64\n"
        f"Maintainer: Davis <dev@localhost>\nInstalled-Size: {size}\n"
        f"Depends: python3 (>= 3.13), python3 (<< 3.14), {depends}\n"
        "Section: net\nPriority: optional\n"
        f"Description: {manifest['description']}\n"
        " Mesh messaging, node browsing, and GNSS on the CardputerZero.\n"
    ).encode()
    output = args.output / f"{name}_{version}_arm64.deb"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(ar((("debian-binary", b"2.0\n"),
                           ("control.tar.xz", tar({"control": (metadata, 0o644)})),
                           ("data.tar.xz", tar(contents)))))
    print(output)


if __name__ == "__main__":
    main()
