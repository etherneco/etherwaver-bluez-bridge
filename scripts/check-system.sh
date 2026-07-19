#!/bin/sh
set -eu

failed=0

check_command() {
    if command -v "$1" >/dev/null 2>&1; then
        printf '[ok] command: %s\n' "$1"
    else
        printf '[missing] command: %s\n' "$1" >&2
        failed=1
    fi
}

check_command bluetoothctl
check_command python3
check_command systemctl

if python3 -c 'import dbus; from gi.repository import GLib' 2>/dev/null; then
    printf '[ok] Python libraries: dbus, gi.repository.GLib\n'
else
    printf '[missing] install python3-dbus and python3-gi\n' >&2
    failed=1
fi

if systemctl is-active --quiet bluetooth.service; then
    printf '[ok] bluetooth.service is active\n'
else
    printf '[error] bluetooth.service is not active\n' >&2
    failed=1
fi

if busctl --system --list 2>/dev/null | grep -q 'org.bluez'; then
    printf '[ok] org.bluez is available on the system bus\n'
else
    printf '[error] org.bluez is unavailable on the system bus\n' >&2
    failed=1
fi

if [ -e /sys/class/bluetooth/hci0 ]; then
    printf '[ok] Bluetooth adapter: hci0\n'
else
    printf '[error] Bluetooth adapter hci0 was not found\n' >&2
    failed=1
fi

exit "$failed"
