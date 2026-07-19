#!/bin/sh
set -eu

APP_NAME="etherwaver-bluez-bridge"
APP_DIR="/opt/$APP_NAME"
CONFIG_DIR="/etc/etherwaver"
CONFIG_FILE="$CONFIG_DIR/$APP_NAME.env"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"

say() {
    printf '%s\n' "$*"
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || die "run this installer as root: sudo ./install.sh"
[ -r /etc/os-release ] || die "cannot identify the operating system"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_FILE="$SCRIPT_DIR/src/etherwaver_bluez_bridge.py"
ENV_TEMPLATE="$SCRIPT_DIR/config/etherwaver-bluez-bridge.env"
UNIT_TEMPLATE="$SCRIPT_DIR/systemd/etherwaver-bluez-bridge.service"

[ -f "$SOURCE_FILE" ] || die "missing $SOURCE_FILE"
[ -f "$ENV_TEMPLATE" ] || die "missing $ENV_TEMPLATE"
[ -f "$UNIT_TEMPLATE" ] || die "missing $UNIT_TEMPLATE"
command -v apt-get >/dev/null 2>&1 || die "apt-get is required (use Raspberry Pi OS or Debian)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

say "Installing BlueZ and Python system libraries..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    bluez \
    python3 \
    python3-dbus \
    python3-gi

say "Checking Python libraries..."
python3 -c 'import dbus; from gi.repository import GLib'

say "Installing EtherWaver BlueZ Bridge..."
install -d -m 0755 "$APP_DIR/src" "$CONFIG_DIR"
install -m 0755 "$SOURCE_FILE" "$APP_DIR/src/etherwaver_bluez_bridge.py"

if [ ! -e "$CONFIG_FILE" ]; then
    install -m 0644 "$ENV_TEMPLATE" "$CONFIG_FILE"
else
    say "Keeping existing configuration: $CONFIG_FILE"
fi

install -m 0644 "$UNIT_TEMPLATE" "$SERVICE_FILE"

say "Enabling Bluetooth and EtherWaver services..."
systemctl daemon-reload
systemctl enable --now bluetooth.service
systemctl enable "$APP_NAME.service"
systemctl restart "$APP_NAME.service"

if systemctl is-active --quiet "$APP_NAME.service"; then
    say "EtherWaver BlueZ Bridge is running."
    say "Configuration: $CONFIG_FILE"
    say "Logs: journalctl -u $APP_NAME.service -f"
else
    systemctl --no-pager --full status "$APP_NAME.service" || true
    die "service failed to start; inspect: journalctl -u $APP_NAME.service -n 100"
fi

