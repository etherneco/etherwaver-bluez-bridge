#!/bin/sh
set -eu

APP_NAME="etherwaver-bluez-bridge"
APP_DIR="/opt/$APP_NAME"
CONFIG_FILE="/etc/etherwaver/$APP_NAME.env"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || die "run this uninstaller as root: sudo ./uninstall.sh"

systemctl disable --now "$APP_NAME.service" 2>/dev/null || true
rm -f "$SERVICE_FILE"
rm -rf "$APP_DIR"
systemctl daemon-reload

if [ "${1:-}" = "--purge-config" ]; then
    rm -f "$CONFIG_FILE"
    rmdir /etc/etherwaver 2>/dev/null || true
    printf 'Removed application, service, and configuration.\n'
else
    printf 'Removed application and service. Configuration kept at %s.\n' "$CONFIG_FILE"
fi
