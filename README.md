# EtherWaver BlueZ Bridge

Turn a Raspberry Pi into a Bluetooth keyboard and mouse bridge for
[EtherWaver](https://github.com/etherneco/etherwaver) using BlueZ.

The initial bridge implementation was developed and tested on a Raspberry Pi.
It exposes a BLE HID keyboard and mouse through BlueZ and accepts EtherWaver
input events over TCP.

## Purpose

The bridge runs on a Raspberry Pi and presents it to another computer as a
Bluetooth HID keyboard and mouse. EtherWaver sends input events to the bridge
over TCP; the bridge translates them into Bluetooth HID reports through BlueZ.

Keeping the bridge in a separate repository gives the Raspberry Pi component
its own dependencies, release cycle, installation procedure, and systemd
service without coupling them to the desktop EtherWaver build.

## Architecture

```text
EtherWaver server        Raspberry Pi                 Target computer
-----------------        --------------------------   ----------------
keyboard/mouse events -> TCP bridge -> BlueZ HID ->   Bluetooth input
                         :24810
```

The initial EtherWaver wire protocol is line-oriented:

| Message | Meaning |
| --- | --- |
| `hello <host>` | Identify the connected EtherWaver server |
| `kd <key> <modifiers> <button>` | Key down |
| `ku <key> <modifiers> <button>` | Key up |
| `kr <key> <modifiers> <count> <button>` | Key repeat |
| `md <button>` | Mouse button down |
| `mu <button>` | Mouse button up |
| `mm <x> <y>` | Absolute pointer movement |
| `mr <dx> <dy>` | Relative pointer movement |
| `mw <x> <y>` | Mouse wheel movement |

Each message ends with a newline. The default bridge address is
`0.0.0.0:24810`.

## Target platform

- Raspberry Pi OS or another Debian-based Linux distribution
- Raspberry Pi with a Bluetooth adapter supporting peripheral mode
- BlueZ and D-Bus
- Python 3
- Python D-Bus and GObject bindings (`python3-dbus`, `python3-gi`)
- root access for installation and Bluetooth configuration

## Repository layout

```text
config/   Environment defaults for the service
systemd/  systemd unit for Raspberry Pi deployment
src/      BlueZ bridge implementation
```

## Installation

Install the operating-system dependencies:

```bash
sudo apt update
sudo apt install bluez python3 python3-dbus python3-gi
```

The service expects the application in `/opt/etherwaver-bluez-bridge`, a
dedicated `etherwaver` system user, and its configuration under
`/etc/etherwaver`.

```bash
sudo useradd --system --home /opt/etherwaver-bluez-bridge \
  --shell /usr/sbin/nologin etherwaver
sudo install -d -o etherwaver -g etherwaver /opt/etherwaver-bluez-bridge
sudo install -d -o etherwaver -g etherwaver /opt/etherwaver-bluez-bridge/src
sudo install -m 0755 src/etherwaver_bluez_bridge.py \
  /opt/etherwaver-bluez-bridge/src/etherwaver_bluez_bridge.py
sudo install -d /etc/etherwaver
sudo install -m 0644 config/etherwaver-bluez-bridge.env \
  /etc/etherwaver/etherwaver-bluez-bridge.env
sudo install -m 0644 systemd/etherwaver-bluez-bridge.service \
  /etc/systemd/system/etherwaver-bluez-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now etherwaver-bluez-bridge.service
```

Check startup and pairing activity with:

```bash
sudo journalctl -u etherwaver-bluez-bridge.service -f
```

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `ETHERWAVER_CONTROL_HOST` | `127.0.0.1` | BlueZ bridge control host |
| `ETHERWAVER_CONTROL_PORT` | `8765` | BlueZ bridge control port |
| `ETHERWAVER_BRIDGE_HOST` | `0.0.0.0` | Address accepting EtherWaver connections |
| `ETHERWAVER_BRIDGE_PORT` | `24810` | Port accepting EtherWaver connections |

## Security

Port `24810` accepts input-control events and must not be exposed to the public
Internet. Bind it to a trusted interface or restrict it with a firewall. A
future protocol revision should add authentication and encryption before the
bridge is used outside a trusted LAN.

## License

License information has not been added yet.
