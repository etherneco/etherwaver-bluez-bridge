# Architecture

## Components

The bridge is a single Python process with three responsibilities:

1. It registers a Bluetooth Low Energy HID application and advertisement with
   BlueZ through the system D-Bus.
2. It accepts EtherWaver's line-oriented keyboard and mouse event protocol on
   TCP port `24810` by default.
3. It converts those events into BLE HID keyboard and mouse input reports and
   publishes them through BlueZ GATT characteristics.

The process also exposes a local diagnostic control socket on port `8765`.

## Runtime dependencies

| Component | Debian package | Purpose |
| --- | --- | --- |
| BlueZ | `bluez` | Bluetooth daemon, GATT and advertising APIs |
| D-Bus Python bindings | `python3-dbus` | Access to the BlueZ system-bus API |
| PyGObject | `python3-gi` | GLib main loop used by D-Bus callbacks |
| Python | `python3` | Bridge runtime |

These libraries are installed from Raspberry Pi OS rather than copied into the
repository. Both Python bindings contain native modules compiled for the OS and
CPU architecture, while BlueZ must match the system daemon and D-Bus policy.

## Startup sequence

```text
systemd
  -> starts bluetooth.service
  -> powers on and exposes the adapter
  -> starts etherwaver_bluez_bridge.py
       -> connects to the system D-Bus
       -> removes remembered Bluetooth peers
       -> configures the adapter as pairable and discoverable
       -> registers the pairing agent
       -> registers GATT services and BLE advertisement
       -> starts control and EtherWaver TCP listeners
       -> runs the GLib event loop
```

The current implementation removes remembered peers during every startup to
force fresh pairing. This is useful during development but should become a
configuration option before a stable release.

## Privileges

The systemd unit runs as root because the bridge changes adapter properties,
removes paired devices, registers a pairing agent, and controls the system
BlueZ daemon. The service applies systemd hardening, including a read-only
filesystem, private temporary directory, protected home directories, and a
restricted address-family list.

## Network security

The EtherWaver event protocol currently has no authentication or encryption.
The listener must only be exposed on a trusted LAN or restricted to the
EtherWaver server with a firewall. The local diagnostic control listener binds
to `127.0.0.1` by default.
