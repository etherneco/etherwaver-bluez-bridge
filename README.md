# EtherWaver BlueZ Bridge

Turn a Raspberry Pi into a Bluetooth keyboard and mouse bridge for
[EtherWaver](https://github.com/etherneco/etherwaver) using BlueZ.

The initial bridge implementation was developed and tested on a Raspberry Pi.
It exposes a BLE HID keyboard and mouse through BlueZ and accepts EtherWaver
input events over TCP.

The project includes the bridge source, systemd service, environment
configuration, installer, uninstaller, and system diagnostic script. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the internal design and
runtime dependency rationale.

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
docs/     Architecture and operational documentation
scripts/  Raspberry Pi diagnostics
systemd/  systemd unit for Raspberry Pi deployment
src/      BlueZ bridge implementation
install.sh / uninstall.sh  Deployment scripts
```

## Installation

Clone the repository on the Raspberry Pi and run the installer:

```bash
git clone https://github.com/etherneco/etherwaver-bluez-bridge.git
cd etherwaver-bluez-bridge
sudo ./install.sh
```

The installer:

- installs BlueZ, Python, D-Bus bindings, and PyGObject from Raspberry Pi OS;
- validates that the required Python libraries load;
- installs the bridge under `/opt/etherwaver-bluez-bridge`;
- creates the default configuration only when one does not already exist;
- installs, enables, starts, and verifies the systemd service.

Check startup and pairing activity with:

```bash
sudo journalctl -u etherwaver-bluez-bridge.service -f
```

Run a system check independently with:

```bash
./scripts/check-system.sh
```

To uninstall the service while preserving its configuration:

```bash
sudo ./uninstall.sh
```

Add `--purge-config` to remove `/etc/etherwaver/etherwaver-bluez-bridge.env`
as well. System packages are intentionally left installed because they may be
used by other Bluetooth applications.

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
