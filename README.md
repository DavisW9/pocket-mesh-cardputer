# Mesh for CardputerZero

A keyboard-operated Meshtastic client for stock 64-bit CardputerZero OS (Debian 13) and the M5Stack Cap LoRa-1262. The radio runs only while the app is open. The Meshtastic API stays on loopback. The package installs an APPLaunch entry and runs as the normal launcher user.

## Install and use

1. With the CardputerZero powered off, fit the LoRa-1262 Cap with its antenna attached. Use the stock OS expansion port GPIO mode.
2. Copy the ARM64 package to the Zero and run `sudo apt install ./cardputerzero-mesh_0.1.0_arm64.deb`. Open **Mesh for CardputerZero** from the launcher.
3. Open **Settings → Region**, choose your local radio region, then match the other node's modem preset and channel. The radio starts with the region unset on a new install.
4. Use **Z/C** or left/right to switch tabs and **F/X** or up/down to move through lists. Press **N** in Inbox to compose to the selected channel, **Enter** on an inbox item to read it, or **Enter** on a node to send it a direct message. **Esc** backs out or exits. **R** refreshes.
5. To add a private secondary channel, use **Settings → Import channel** and enter its Meshtastic share URL. Select an enabled channel under **Compose channel**. The local region and modem preset are configured separately.

Radio state and SQLite history live under `${XDG_STATE_HOME:-~/.local/state}/cardputerzero-mesh`. The latest startup log is in the `radio` subdirectory. The queue confirmation means the local client submitted the message. It does not confirm delivery.

## Build

For the desktop preview (requires SDL2, CMake and Python):

```sh
python3 -m venv build/venv
build/venv/bin/pip install -r requirements.txt
cmake -S . -B build/desktop -DMESH_DESKTOP=ON
cmake --build build/desktop -j
build/venv/bin/python src/mesh.py --demo --ui build/desktop/mesh-ui
```

On Debian 13 ARM64, build with CMake and a C/C++ compiler, or cross-compile for Linux ARM64 with an appropriate toolchain. The release package bundles the checksum-pinned Meshtasticd binary and ARM64 Python client wheels. It uses the device's Debian repositories for the daemon's system library dependencies. On the Mac, pass `-DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64` and the ARM64 Linux C and C++ compilers to CMake.

```sh
cmake -S . -B build/arm64 -DCMAKE_BUILD_TYPE=Release
cmake --build build/arm64 -j
mkdir -p build
curl -fL "$(head -n 1 packaging/meshtasticd.lock)" -o build/meshtasticd.deb
python3 -m pip install --no-deps --no-compile --only-binary=:all: \
  --platform manylinux2014_aarch64 --python-version 3.13 \
  --target build/python -r requirements-device.txt
python3 scripts/package.py --build build/arm64 --vendor build/python --daemon-deb build/meshtasticd.deb
```

## Hardware and submission checks

The app uses the Cap reference wiring and stock Linux GPIO mode. The SPI chip select is handled by the kernel. The launcher enables the Cap power rail if needed and configures only bit 0 of the I²C antenna switch before starting Meshtasticd. It restores those settings at exit. GNSS uses the stock primary UART alias `/dev/serial0`. These mappings require physical validation on the target OS image.

Before submitting to the AppStore, verify stock launcher install/upgrade/removal, Cap detection and permissions, bidirectional channel and direct messaging with a second node, GNSS fix acquisition, and Cap release on both app exit and SIGTERM. Confirm the listing's author and share code in `app-builder.json`. The store requests a video of the first release running on a physical CardputerZero.

## Sources

- [CardputerZero Template](https://github.com/CardputerZero/Template/tree/477fa6d217b683e3084c361a757f03dc8ca9f753) supplied the MIT-licensed keyboard adapter, which was extended for text entry.
- [CardputerZero Cap reference](https://github.com/CardputerZero/Cap-LoRa-1262/tree/b90f86677748a58c2c63071e1d50ef0e191f2ec1) supplied the reference hardware sequence.
- The bundled daemon is Meshtasticd from [Meshtastic firmware v2.7.26.54e0d8d](https://github.com/meshtastic/firmware/tree/v2.7.26.54e0d8d). Its Debian build recipe and source package are available through the [Meshtastic beta OpenSUSE Build Service project](https://build.opensuse.org/project/show/network:Meshtastic:beta). The application source and packaging here are GPL-3.0-only. Third-party notices are included in the package.
