#!/usr/bin/env python3
# Minimal BLE HID Keyboard for BlueZ / Raspberry Pi
# Tested conceptually against BlueZ GATT/LEAdvertising D-Bus APIs.
# First goal: make Android see the device as a keyboard, not audio.

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib
from datetime import datetime
import socketserver
import threading
import os

BLUEZ_SERVICE_NAME = "org.bluez"

GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
GATT_DESC_IFACE = "org.bluez.GattDescriptor1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"
DEVICE_IFACE = "org.bluez.Device1"

mainloop = None
current_input_report = None
current_mouse_report = None
console_sender_started = False
console_sender_stop = threading.Event()
console_sender_thread = None
input_lock = threading.Lock()
ENABLE_CONSOLE_SENDER = False
ENABLE_AUTO_DEMO = False
CONTROL_HOST = os.environ.get("ETHERWAVER_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("ETHERWAVER_CONTROL_PORT", "8765"))
ETHERWAVER_HOST = os.environ.get("ETHERWAVER_BRIDGE_HOST", "0.0.0.0")
ETHERWAVER_PORT = int(os.environ.get("ETHERWAVER_BRIDGE_PORT", "24810"))
control_server = None
etherwaver_bridge_server = None


def log_evt(*parts):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}]", *parts)


def safe_input(prompt):
    with input_lock:
        return input(prompt)


def start_console_sender(input_report):
    global console_sender_started, console_sender_thread
    if not ENABLE_CONSOLE_SENDER:
        return
    if console_sender_started:
        return
    console_sender_started = True
    console_sender_stop.clear()

    def _send_text(text):
        log_evt("Queue text:", repr(text))
        input_report.type_text(text)
        return False

    def _loop():
        print("Interactive test enabled. Type text and press Enter. Ctrl-C to stop.")
        while not console_sender_stop.is_set():
            try:
                line = safe_input("tekst> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                GLib.idle_add(mainloop.quit)
                break

            if line is None:
                continue
            line = line.strip("\r")
            if line == "/diag":
                GLib.idle_add(input_report.print_diag)
                continue
            GLib.idle_add(_send_text, line + "\n")

        global console_sender_started
        console_sender_started = False

    t = threading.Thread(target=_loop, daemon=True)
    console_sender_thread = t
    t.start()


def stop_console_sender():
    global console_sender_thread
    if not ENABLE_CONSOLE_SENDER:
        return
    console_sender_stop.set()
    t = console_sender_thread
    if t is not None and t.is_alive():
        if threading.current_thread() is not t:
            t.join(timeout=1.0)
    console_sender_thread = None


def queue_text_send(text):
    if current_input_report is None:
        log_evt("Control: input report is not ready yet.")
        return False

    def _send():
        current_input_report.type_text(text)
        return False

    GLib.idle_add(_send)
    return True


def queue_diag_print():
    if current_input_report is None:
        log_evt("Control: input report is not ready yet.")
        return False
    GLib.idle_add(current_input_report.print_diag)
    return True


def queue_key_send(keycode, modifier=0):
    if current_input_report is None:
        log_evt("Control: input report is not ready yet.")
        return False

    def _send():
        current_input_report.send_combo(modifier, keycode)
        return False

    GLib.idle_add(_send)
    return True


def queue_mouse_send(dx=0, dy=0, buttons=0, wheel=0):
    if current_mouse_report is None:
        log_evt("Control: mouse report is not ready yet.")
        return False

    def _send():
        current_mouse_report.send_mouse_event(dx=dx, dy=dy, buttons=buttons, wheel=wheel)
        return False

    GLib.idle_add(_send)
    return True


class ControlRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            raw = self.rfile.readline()
            if not raw:
                return

            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue

            command, _, payload = line.partition(" ")
            command = command.upper()

            if command == "READY":
                ready = current_input_report is not None and current_input_report.can_send()
                self.wfile.write(f"READY {1 if ready else 0}\n".encode("ascii"))
                self.wfile.flush()
                continue

            if command == "MOUSE_READY":
                ready = current_mouse_report is not None and current_mouse_report.can_send()
                self.wfile.write(f"MOUSE_READY {1 if ready else 0}\n".encode("ascii"))
                self.wfile.flush()
                continue

            if command == "TEXT":
                if not payload:
                    self.wfile.write(b"ERR missing text\n")
                else:
                    queue_text_send(payload.encode("utf-8").decode("unicode_escape"))
                    self.wfile.write(b"OK\n")
                self.wfile.flush()
                continue

            if command == "KEY":
                try:
                    modifier, keycode = resolve_key_spec(payload)
                except ValueError as e:
                    self.wfile.write(f"ERR {e}\n".encode("utf-8"))
                else:
                    queue_key_send(keycode, modifier)
                    self.wfile.write(b"OK\n")
                self.wfile.flush()
                continue

            if command == "COMBO":
                try:
                    modifier, keycode = parse_combo_spec(payload)
                except ValueError as e:
                    self.wfile.write(f"ERR {e}\n".encode("utf-8"))
                else:
                    queue_key_send(keycode, modifier)
                    self.wfile.write(b"OK\n")
                self.wfile.flush()
                continue

            if command == "MOUSE":
                try:
                    parts = payload.split()
                    if len(parts) != 4:
                        raise ValueError("mouse requires: dx dy buttons wheel")
                    dx, dy, buttons, wheel = (int(part) for part in parts)
                    if not all(-127 <= value <= 127 for value in (dx, dy, wheel)):
                        raise ValueError("dx dy wheel must be in range -127..127")
                    if not 0 <= buttons <= 7:
                        raise ValueError("buttons must be in range 0..7")
                except ValueError as e:
                    self.wfile.write(f"ERR {e}\n".encode("utf-8"))
                else:
                    queue_mouse_send(dx=dx, dy=dy, buttons=buttons, wheel=wheel)
                    self.wfile.write(b"OK\n")
                self.wfile.flush()
                continue

            if command == "DIAG":
                queue_diag_print()
                self.wfile.write(b"OK\n")
                self.wfile.flush()
                continue

            self.wfile.write(b"ERR unknown command\n")
            self.wfile.flush()


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_control_server():
    global control_server
    if control_server is not None:
        return

    control_server = ControlServer((CONTROL_HOST, CONTROL_PORT), ControlRequestHandler)
    t = threading.Thread(target=control_server.serve_forever, daemon=True)
    t.start()
    log_evt(f"Control server listening on {CONTROL_HOST}:{CONTROL_PORT}")

# ---- HID UUIDs ----
UUID_HID_SERVICE = "00001812-0000-1000-8000-00805f9b34fb"
UUID_PROTOCOL_MODE = "00002a4e-0000-1000-8000-00805f9b34fb"
UUID_REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"
UUID_HID_INFORMATION = "00002a4a-0000-1000-8000-00805f9b34fb"
UUID_HID_CONTROL_POINT = "00002a4c-0000-1000-8000-00805f9b34fb"
UUID_REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
UUID_BOOT_KEYBOARD_INPUT_REPORT = "00002a22-0000-1000-8000-00805f9b34fb"
UUID_BOOT_KEYBOARD_OUTPUT_REPORT = "00002a32-0000-1000-8000-00805f9b34fb"
UUID_BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
UUID_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"
UUID_PNP_ID = "00002a50-0000-1000-8000-00805f9b34fb"

# HID Report Reference descriptor UUID
UUID_REPORT_REFERENCE = "00002908-0000-1000-8000-00805f9b34fb"

# Keyboard input report format: modifiers, reserved, 6 keycodes
# Report ID = 1
# Mouse input report format: buttons, dx, dy, wheel
# Report ID = 2
KEYBOARD_REPORT_MAP = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0xE0,        #   Usage Minimum (224)
    0x29, 0xE7,        #   Usage Maximum (231)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs) ; Modifier byte
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x01,        #   Input (Const,Array,Abs) ; Reserved
    0x95, 0x05,        #   Report Count (5)
    0x75, 0x01,        #   Report Size (1)
    0x05, 0x08,        #   Usage Page (LEDs)
    0x19, 0x01,        #   Usage Minimum (Num Lock)
    0x29, 0x05,        #   Usage Maximum (Kana)
    0x91, 0x02,        #   Output (Data,Var,Abs)
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x03,        #   Report Size (3)
    0x91, 0x01,        #   Output (Const,Array,Abs)
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x65,        #   Logical Maximum (101)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,        #   Usage Minimum (0)
    0x29, 0x65,        #   Usage Maximum (101)
    0x81, 0x00,        #   Input (Data,Array,Abs)
    0xC0,              # End Collection
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (1)
    0x29, 0x03,        #     Usage Maximum (3)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x03,        #     Report Count (3)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data,Var,Abs)
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x05,        #     Report Size (5)
    0x81, 0x01,        #     Input (Const,Array,Abs)
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x03,        #     Report Count (3)
    0x81, 0x06,        #     Input (Data,Var,Rel)
    0xC0,              #   End Collection
    0xC0               # End Collection
])

# Simple HID usage codes
KEY_A = 0x04
KEY_B = 0x05
KEY_C = 0x06
KEY_D = 0x07
KEY_E = 0x08
KEY_F = 0x09
KEY_G = 0x0A
KEY_H = 0x0B
KEY_I = 0x0C
KEY_J = 0x0D
KEY_K = 0x0E
KEY_L = 0x0F
KEY_M = 0x10
KEY_N = 0x11
KEY_O = 0x12
KEY_P = 0x13
KEY_Q = 0x14
KEY_R = 0x15
KEY_S = 0x16
KEY_T = 0x17
KEY_U = 0x18
KEY_V = 0x19
KEY_W = 0x1A
KEY_X = 0x1B
KEY_Y = 0x1C
KEY_Z = 0x1D
KEY_1 = 0x1E
KEY_2 = 0x1F
KEY_3 = 0x20
KEY_4 = 0x21
KEY_5 = 0x22
KEY_6 = 0x23
KEY_7 = 0x24
KEY_8 = 0x25
KEY_9 = 0x26
KEY_0 = 0x27
KEY_ENTER = 0x28
KEY_ESC = 0x29
KEY_BACKSPACE = 0x2A
KEY_TAB = 0x2B
KEY_SPACE = 0x2C
KEY_MINUS = 0x2D
KEY_EQUAL = 0x2E
KEY_LEFTBRACE = 0x2F
KEY_RIGHTBRACE = 0x30
KEY_BACKSLASH = 0x31
KEY_SEMICOLON = 0x33
KEY_APOSTROPHE = 0x34
KEY_GRAVE = 0x35
KEY_COMMA = 0x36
KEY_DOT = 0x37
KEY_SLASH = 0x38
KEY_CAPSLOCK = 0x39
KEY_F1 = 0x3A
KEY_F2 = 0x3B
KEY_F3 = 0x3C
KEY_F4 = 0x3D
KEY_F5 = 0x3E
KEY_F6 = 0x3F
KEY_F7 = 0x40
KEY_F8 = 0x41
KEY_F9 = 0x42
KEY_F10 = 0x43
KEY_F11 = 0x44
KEY_F12 = 0x45
KEY_SYSRQ = 0x46
KEY_SCROLLLOCK = 0x47
KEY_PAUSE = 0x48
KEY_INSERT = 0x49
KEY_HOME = 0x4A
KEY_PAGEUP = 0x4B
KEY_DELETE = 0x4C
KEY_END = 0x4D
KEY_PAGEDOWN = 0x4E
KEY_RIGHT = 0x4F
KEY_LEFT = 0x50
KEY_DOWN = 0x51
KEY_UP = 0x52
KEY_NUMLOCK = 0x53
KEY_KPSLASH = 0x54
KEY_KPASTERISK = 0x55
KEY_KPMINUS = 0x56
KEY_KPPLUS = 0x57
KEY_KPENTER = 0x58
KEY_KP1 = 0x59
KEY_KP2 = 0x5A
KEY_KP3 = 0x5B
KEY_KP4 = 0x5C
KEY_KP5 = 0x5D
KEY_KP6 = 0x5E
KEY_KP7 = 0x5F
KEY_KP8 = 0x60
KEY_KP9 = 0x61
KEY_KP0 = 0x62
KEY_KPDOT = 0x63
KEY_NON_US_BACKSLASH = 0x64
KEY_APPLICATION = 0x65

MOD_LCTRL = 0x01
MOD_LSHIFT = 0x02
MOD_LALT = 0x04
MOD_LGUI = 0x08
MOD_RCTRL = 0x10
MOD_RSHIFT = 0x20
MOD_RALT = 0x40
MOD_RGUI = 0x80

CHAR_TO_HID = {
    'a': (0, KEY_A), 'b': (0, KEY_B), 'c': (0, KEY_C), 'd': (0, KEY_D),
    'e': (0, KEY_E), 'f': (0, KEY_F), 'g': (0, KEY_G), 'h': (0, KEY_H),
    'i': (0, KEY_I), 'j': (0, KEY_J), 'k': (0, KEY_K), 'l': (0, KEY_L),
    'm': (0, KEY_M), 'n': (0, KEY_N), 'o': (0, KEY_O), 'p': (0, KEY_P),
    'q': (0, KEY_Q), 'r': (0, KEY_R), 's': (0, KEY_S), 't': (0, KEY_T),
    'u': (0, KEY_U), 'v': (0, KEY_V), 'w': (0, KEY_W), 'x': (0, KEY_X),
    'y': (0, KEY_Y), 'z': (0, KEY_Z),
    'A': (MOD_LSHIFT, KEY_A), 'B': (MOD_LSHIFT, KEY_B), 'C': (MOD_LSHIFT, KEY_C),
    'D': (MOD_LSHIFT, KEY_D), 'E': (MOD_LSHIFT, KEY_E), 'F': (MOD_LSHIFT, KEY_F),
    'G': (MOD_LSHIFT, KEY_G), 'H': (MOD_LSHIFT, KEY_H), 'I': (MOD_LSHIFT, KEY_I),
    'J': (MOD_LSHIFT, KEY_J), 'K': (MOD_LSHIFT, KEY_K), 'L': (MOD_LSHIFT, KEY_L),
    'M': (MOD_LSHIFT, KEY_M), 'N': (MOD_LSHIFT, KEY_N), 'O': (MOD_LSHIFT, KEY_O),
    'P': (MOD_LSHIFT, KEY_P), 'Q': (MOD_LSHIFT, KEY_Q), 'R': (MOD_LSHIFT, KEY_R),
    'S': (MOD_LSHIFT, KEY_S), 'T': (MOD_LSHIFT, KEY_T), 'U': (MOD_LSHIFT, KEY_U),
    'V': (MOD_LSHIFT, KEY_V), 'W': (MOD_LSHIFT, KEY_W), 'X': (MOD_LSHIFT, KEY_X),
    'Y': (MOD_LSHIFT, KEY_Y), 'Z': (MOD_LSHIFT, KEY_Z),
    '1': (0, KEY_1), '2': (0, KEY_2), '3': (0, KEY_3), '4': (0, KEY_4),
    '5': (0, KEY_5), '6': (0, KEY_6), '7': (0, KEY_7), '8': (0, KEY_8),
    '9': (0, KEY_9), '0': (0, KEY_0),
    ' ': (0, KEY_SPACE), '\n': (0, KEY_ENTER), '\t': (0, KEY_TAB), '\b': (0, KEY_BACKSPACE),
    '-': (0, KEY_MINUS), '_': (MOD_LSHIFT, KEY_MINUS),
    '=': (0, KEY_EQUAL), '+': (MOD_LSHIFT, KEY_EQUAL),
    '[': (0, KEY_LEFTBRACE), '{': (MOD_LSHIFT, KEY_LEFTBRACE),
    ']': (0, KEY_RIGHTBRACE), '}': (MOD_LSHIFT, KEY_RIGHTBRACE),
    '\\': (0, KEY_BACKSLASH), '|': (MOD_LSHIFT, KEY_BACKSLASH),
    ';': (0, KEY_SEMICOLON), ':': (MOD_LSHIFT, KEY_SEMICOLON),
    "'": (0, KEY_APOSTROPHE), '"': (MOD_LSHIFT, KEY_APOSTROPHE),
    '`': (0, KEY_GRAVE), '~': (MOD_LSHIFT, KEY_GRAVE),
    ',': (0, KEY_COMMA), '<': (MOD_LSHIFT, KEY_COMMA),
    '.': (0, KEY_DOT), '>': (MOD_LSHIFT, KEY_DOT),
    '/': (0, KEY_SLASH), '?': (MOD_LSHIFT, KEY_SLASH),
    '!': (MOD_LSHIFT, KEY_1), '@': (MOD_LSHIFT, KEY_2), '#': (MOD_LSHIFT, KEY_3),
    '$': (MOD_LSHIFT, KEY_4), '%': (MOD_LSHIFT, KEY_5), '^': (MOD_LSHIFT, KEY_6),
    '&': (MOD_LSHIFT, KEY_7), '*': (MOD_LSHIFT, KEY_8), '(': (MOD_LSHIFT, KEY_9),
    ')': (MOD_LSHIFT, KEY_0),
}

MOD_TOKENS = {
    "CTRL": MOD_LCTRL,
    "SHIFT": MOD_LSHIFT,
    "ALT": MOD_LALT,
    "WIN": MOD_LGUI,
    "GUI": MOD_LGUI,
    "RCTRL": MOD_RCTRL,
    "RSHIFT": MOD_RSHIFT,
    "RALT": MOD_RALT,
    "RWIN": MOD_RGUI,
}

SPECIAL_KEYS = {
    "ENTER": KEY_ENTER,
    "ESC": KEY_ESC,
    "ESCAPE": KEY_ESC,
    "TAB": KEY_TAB,
    "BACKSPACE": KEY_BACKSPACE,
    "DEL": KEY_DELETE,
    "DELETE": KEY_DELETE,
    "SPACE": KEY_SPACE,
    "CAPSLOCK": KEY_CAPSLOCK,
    "F1": KEY_F1,
    "F2": KEY_F2,
    "F3": KEY_F3,
    "F4": KEY_F4,
    "F5": KEY_F5,
    "F6": KEY_F6,
    "F7": KEY_F7,
    "F8": KEY_F8,
    "F9": KEY_F9,
    "F10": KEY_F10,
    "F11": KEY_F11,
    "F12": KEY_F12,
    "PRINTSCREEN": KEY_SYSRQ,
    "PRTSC": KEY_SYSRQ,
    "SYSRQ": KEY_SYSRQ,
    "SCROLLLOCK": KEY_SCROLLLOCK,
    "PAUSE": KEY_PAUSE,
    "INSERT": KEY_INSERT,
    "HOME": KEY_HOME,
    "PAGEUP": KEY_PAGEUP,
    "PGUP": KEY_PAGEUP,
    "END": KEY_END,
    "PAGEDOWN": KEY_PAGEDOWN,
    "PGDOWN": KEY_PAGEDOWN,
    "RIGHT": KEY_RIGHT,
    "LEFT": KEY_LEFT,
    "DOWN": KEY_DOWN,
    "UP": KEY_UP,
    "NUMLOCK": KEY_NUMLOCK,
    "KPSLASH": KEY_KPSLASH,
    "KPASTERISK": KEY_KPASTERISK,
    "KPMINUS": KEY_KPMINUS,
    "KPPLUS": KEY_KPPLUS,
    "KPENTER": KEY_KPENTER,
    "KP1": KEY_KP1,
    "KP2": KEY_KP2,
    "KP3": KEY_KP3,
    "KP4": KEY_KP4,
    "KP5": KEY_KP5,
    "KP6": KEY_KP6,
    "KP7": KEY_KP7,
    "KP8": KEY_KP8,
    "KP9": KEY_KP9,
    "KP0": KEY_KP0,
    "KPDOT": KEY_KPDOT,
    "NONUSBACKSLASH": KEY_NON_US_BACKSLASH,
    "102ND": KEY_NON_US_BACKSLASH,
    "MENU": KEY_APPLICATION,
    "APPLICATION": KEY_APPLICATION,
}


def resolve_key_spec(spec):
    token = spec.strip()
    if not token:
        raise ValueError("missing key")

    if token in CHAR_TO_HID:
        return CHAR_TO_HID[token]

    upper = token.upper()
    if upper in SPECIAL_KEYS:
        return (0, SPECIAL_KEYS[upper])

    if len(upper) == 1 and upper.isalpha():
        return CHAR_TO_HID[upper.lower()]

    raise ValueError(f"unknown key: {spec}")


def parse_combo_spec(spec):
    parts = [part.strip() for part in spec.split("+") if part.strip()]
    if not parts:
        raise ValueError("missing combo")

    modifier = 0
    for part in parts[:-1]:
        upper = part.upper()
        if upper not in MOD_TOKENS:
            raise ValueError(f"unknown modifier: {part}")
        modifier |= MOD_TOKENS[upper]

    key_modifier, keycode = resolve_key_spec(parts[-1])
    modifier |= key_modifier
    return modifier, keycode


def clamp_hid_delta(value):
    return max(-127, min(127, int(value)))


def modifier_from_etherwaver_mask(mask):
    mods = 0
    if mask & 0x0002:
        mods |= MOD_LCTRL
    if mask & 0x0001:
        mods |= MOD_LSHIFT
    if mask & 0x0004:
        mods |= MOD_LALT
    if mask & 0x0008 or mask & 0x0010:
        mods |= MOD_LGUI
    if mask & 0x0020:
        mods |= MOD_RALT
    return mods


def map_etherwaver_key(key_id):
    if 0 <= key_id <= 0x7F:
        ch = chr(key_id)
        if ch in CHAR_TO_HID:
            modifier, keycode = CHAR_TO_HID[ch]
            return modifier, keycode, 0, False

    special = {
        0xEF08: KEY_BACKSPACE,
        0xEF09: KEY_TAB,
        0xEF0D: KEY_ENTER,
        0xEF13: KEY_PAUSE,
        0xEF14: KEY_SCROLLLOCK,
        0xEF15: KEY_SYSRQ,
        0xEF1B: KEY_ESC,
        0xEFFF: KEY_DELETE,
        0xEF50: KEY_HOME,
        0xEF51: KEY_LEFT,
        0xEF52: KEY_UP,
        0xEF53: KEY_RIGHT,
        0xEF54: KEY_DOWN,
        0xEF55: KEY_PAGEUP,
        0xEF56: KEY_PAGEDOWN,
        0xEF57: KEY_END,
        0xEF61: KEY_SYSRQ,
        0xEF63: KEY_INSERT,
        0xEF67: KEY_APPLICATION,
        0xEF7F: KEY_NUMLOCK,
        0xEF8D: KEY_KPENTER,
        0xEF95: KEY_KP7,
        0xEF96: KEY_KP4,
        0xEF97: KEY_KP8,
        0xEF98: KEY_KP6,
        0xEF99: KEY_KP2,
        0xEF9A: KEY_KP9,
        0xEF9B: KEY_KP3,
        0xEF9C: KEY_KP1,
        0xEF9D: KEY_KP5,
        0xEF9E: KEY_KP0,
        0xEF9F: KEY_KPDOT,
        0xEFAA: KEY_KPASTERISK,
        0xEFAB: KEY_KPPLUS,
        0xEFAD: KEY_KPMINUS,
        0xEFAE: KEY_KPDOT,
        0xEFAF: KEY_KPSLASH,
        0xEFB0: KEY_KP0,
        0xEFB1: KEY_KP1,
        0xEFB2: KEY_KP2,
        0xEFB3: KEY_KP3,
        0xEFB4: KEY_KP4,
        0xEFB5: KEY_KP5,
        0xEFB6: KEY_KP6,
        0xEFB7: KEY_KP7,
        0xEFB8: KEY_KP8,
        0xEFB9: KEY_KP9,
    }
    if key_id in special:
        return 0, special[key_id], 0, False
    if 0xEFBE <= key_id <= 0xEFC9:
        return 0, KEY_F1 + (key_id - 0xEFBE), 0, False
    if 0xEFCA <= key_id <= 0xEFD5:
        return 0, 0x68 + (key_id - 0xEFCA), 0, False

    modifiers = {
        0xEFE1: MOD_LSHIFT,
        0xEFE2: MOD_RSHIFT,
        0xEFE3: MOD_LCTRL,
        0xEFE4: MOD_RCTRL,
        0xEFE7: MOD_LGUI,
        0xEFE8: MOD_RGUI,
        0xEFE9: MOD_LALT,
        0xEFEA: MOD_RALT,
        0xEF7E: MOD_RALT,
        0xEFEB: MOD_LGUI,
        0xEFEC: MOD_RGUI,
    }
    if key_id in modifiers:
        return 0, 0, modifiers[key_id], True

    return 0, 0, 0, False


class EtherWaverHIDState:
    def __init__(self):
        self.keyboard_modifiers = 0
        self.keyboard_keys = []
        self.mouse_buttons = 0
        self.has_last_absolute = False
        self.last_abs_x = 0
        self.last_abs_y = 0

    def _send_keyboard(self):
        if current_input_report is None:
            return
        modifiers = self.keyboard_modifiers
        keys = list(self.keyboard_keys[:6])

        def _send():
            current_input_report.send_keyboard_state(modifiers, keys)
            return False

        GLib.idle_add(_send)

    def _send_mouse(self, dx=0, dy=0, wheel=0):
        if current_mouse_report is None:
            return
        buttons = self.mouse_buttons
        dx = clamp_hid_delta(dx)
        dy = clamp_hid_delta(dy)
        wheel = clamp_hid_delta(wheel)

        def _send():
            current_mouse_report.send_mouse_state(dx=dx, dy=dy, buttons=buttons, wheel=wheel)
            return False

        GLib.idle_add(_send)

    def key_down(self, key_id, mask):
        required, keycode, modifier_bit, is_modifier = map_etherwaver_key(key_id)
        self.keyboard_modifiers = modifier_from_etherwaver_mask(mask)
        if is_modifier:
            self.keyboard_modifiers |= modifier_bit
        elif keycode:
            self.keyboard_modifiers |= required
            if keycode not in self.keyboard_keys:
                if len(self.keyboard_keys) >= 6:
                    self.keyboard_keys[-1] = keycode
                else:
                    self.keyboard_keys.append(keycode)
        self._send_keyboard()

    def key_up(self, key_id, mask):
        _, keycode, modifier_bit, is_modifier = map_etherwaver_key(key_id)
        self.keyboard_modifiers = modifier_from_etherwaver_mask(mask)
        if is_modifier:
            self.keyboard_modifiers &= ~modifier_bit
        elif keycode:
            self.keyboard_keys = [key for key in self.keyboard_keys if key != keycode]
        self._send_keyboard()

    def key_repeat(self, key_id, mask, count):
        _, keycode, _, is_modifier = map_etherwaver_key(key_id)
        if is_modifier or not keycode:
            return
        for _ in range(max(0, int(count))):
            self.key_down(key_id, mask)
            self.key_up(key_id, mask)

    def mouse_down(self, button_id):
        bit = {1: 0x01, 2: 0x04, 3: 0x02}.get(button_id, 0)
        if bit:
            self.mouse_buttons |= bit
            self._send_mouse()

    def mouse_up(self, button_id):
        bit = {1: 0x01, 2: 0x04, 3: 0x02}.get(button_id, 0)
        if bit:
            self.mouse_buttons &= ~bit
            self._send_mouse()

    def mouse_move_absolute(self, x, y):
        if not self.has_last_absolute:
            self.last_abs_x = x
            self.last_abs_y = y
            self.has_last_absolute = True
            return
        dx = x - self.last_abs_x
        dy = y - self.last_abs_y
        self.last_abs_x = x
        self.last_abs_y = y
        self.mouse_relative_move(dx, dy)

    def mouse_relative_move(self, dx, dy):
        while dx != 0 or dy != 0:
            step_x = clamp_hid_delta(dx)
            step_y = clamp_hid_delta(dy)
            self._send_mouse(dx=step_x, dy=step_y)
            dx -= step_x
            dy -= step_y

    def mouse_wheel(self, x_delta, y_delta):
        del x_delta
        steps = int(y_delta / 120) if abs(y_delta) >= 120 else (1 if y_delta > 0 else -1 if y_delta < 0 else 0)
        while steps != 0:
            step = clamp_hid_delta(steps)
            self._send_mouse(wheel=step)
            steps -= step

    def clear(self):
        self.keyboard_modifiers = 0
        self.keyboard_keys = []
        self.mouse_buttons = 0
        self.has_last_absolute = False
        self._send_keyboard()
        self._send_mouse()


etherwaver_state = EtherWaverHIDState()


class EtherWaverBridgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.client_address[0] if self.client_address else "<unknown>"
        log_evt("EtherWaver bridge client connected:", peer)
        while True:
            raw = self.rfile.readline()
            if not raw:
                etherwaver_state.clear()
                log_evt("EtherWaver bridge client disconnected:", peer)
                return

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self._handle_line(line)

    def _handle_line(self, line):
        parts = line.split()
        command = parts[0].lower()
        try:
            if command == "hello" and len(parts) >= 2:
                log_evt("EtherWaver attached bluetooth-host=", parts[1])
            elif command == "enter":
                etherwaver_state.clear()
            elif command == "leave":
                etherwaver_state.clear()
            elif command == "kd" and len(parts) >= 4:
                etherwaver_state.key_down(int(parts[1]), int(parts[2]))
            elif command == "ku" and len(parts) >= 4:
                etherwaver_state.key_up(int(parts[1]), int(parts[2]))
            elif command == "kr" and len(parts) >= 5:
                etherwaver_state.key_repeat(int(parts[1]), int(parts[2]), int(parts[3]))
            elif command == "md" and len(parts) >= 2:
                etherwaver_state.mouse_down(int(parts[1]))
            elif command == "mu" and len(parts) >= 2:
                etherwaver_state.mouse_up(int(parts[1]))
            elif command == "mm" and len(parts) >= 3:
                etherwaver_state.mouse_move_absolute(int(parts[1]), int(parts[2]))
            elif command == "mr" and len(parts) >= 3:
                etherwaver_state.mouse_relative_move(int(parts[1]), int(parts[2]))
            elif command == "mw" and len(parts) >= 3:
                etherwaver_state.mouse_wheel(int(parts[1]), int(parts[2]))
            elif command not in ("screensaver",):
                log_evt("EtherWaver bridge ignored:", line)
        except ValueError as e:
            log_evt("EtherWaver bridge parse error:", line, e)


class EtherWaverBridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_etherwaver_bridge_server():
    global etherwaver_bridge_server
    if etherwaver_bridge_server is not None:
        return

    etherwaver_bridge_server = EtherWaverBridgeServer(
        (ETHERWAVER_HOST, ETHERWAVER_PORT), EtherWaverBridgeRequestHandler
    )
    t = threading.Thread(target=etherwaver_bridge_server.serve_forever, daemon=True)
    t.start()
    log_evt(f"EtherWaver bridge listening on {ETHERWAVER_HOST}:{ETHERWAVER_PORT}")

class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"

class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"

class FailedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Failed"

class Advertisement(dbus.service.Object):
    PATH_BASE = "/org/etherwaver/advertisement"

    def __init__(self, bus, index):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.ad_type = "peripheral"
        self.service_uuids = [UUID_HID_SERVICE]
        self.local_name = "EtherWaver Keyboard"
        self.include_tx_power = True
        self.appearance = 961  # Keyboard
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            LE_ADVERTISEMENT_IFACE: {
                "Type": self.ad_type,
                "ServiceUUIDs": dbus.Array(self.service_uuids, signature="s"),
                "LocalName": dbus.String(self.local_name),
                "IncludeTxPower": dbus.Boolean(self.include_tx_power),
                "Appearance": dbus.UInt16(self.appearance),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        print("Advertisement released")

class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = "/org/etherwaver"
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.characteristics:
                response[chrc.get_path()] = chrc.get_properties()
                for desc in chrc.descriptors:
                    response[desc.get_path()] = desc.get_properties()
        return response

class Service(dbus.service.Object):
    PATH_BASE = "/org/etherwaver/service"

    def __init__(self, bus, index, uuid, primary):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                "UUID": self.uuid,
                "Primary": self.primary,
                "Characteristics": dbus.Array(
                    self.get_characteristic_paths(), signature="o"
                ),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristic_paths(self):
        result = []
        for chrc in self.characteristics:
            result.append(chrc.get_path())
        return result

    def get_characteristics(self):
        return self.characteristics

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_SERVICE_IFACE]

class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + "/char" + str(index)
        self.bus = bus
        self.uuid = uuid
        self.flags = flags
        self.service = service
        self.descriptors = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                "Service": self.service.get_path(),
                "UUID": self.uuid,
                "Flags": self.flags,
                "Descriptors": dbus.Array(
                    self.get_descriptor_paths(), signature="o"
                ),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptor_paths(self):
        result = []
        for desc in self.descriptors:
            result.append(desc.get_path())
        return result

    def get_descriptors(self):
        return self.descriptors

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}", out_signature="")
    def WriteValue(self, value, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="", out_signature="")
    def StartNotify(self):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="", out_signature="")
    def StopNotify(self):
        raise NotSupportedException()

    @dbus.service.signal(DBUS_PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

class Descriptor(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, characteristic):
        self.path = characteristic.path + "/desc" + str(index)
        self.bus = bus
        self.uuid = uuid
        self.flags = flags
        self.chrc = characteristic
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_DESC_IFACE: {
                "Characteristic": self.chrc.get_path(),
                "UUID": self.uuid,
                "Flags": self.flags,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_DESC_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_DESC_IFACE]

    @dbus.service.method(GATT_DESC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        raise NotSupportedException()

class ProtocolModeCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_PROTOCOL_MODE, ["read", "write", "write-without-response"], service)
        self.value = [dbus.Byte(0x01)]  # Report Protocol
        self.input_report = None

    def ReadValue(self, options):
        return self.value

    def WriteValue(self, value, options):
        self.value = value
        print("Protocol mode set:", [int(v) for v in value])
        if self.input_report is not None:
            self.input_report.on_protocol_mode_changed()

class ReportMapCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_REPORT_MAP, ["read"], service)
        self.value = [dbus.Byte(b) for b in KEYBOARD_REPORT_MAP]

    def ReadValue(self, options):
        return self.value

class HIDInformationCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_HID_INFORMATION, ["read"], service)
        # bcdHID=0x0111, country=0, flags=0x03 (remote wake + normally connectable)
        self.value = [dbus.Byte(0x11), dbus.Byte(0x01), dbus.Byte(0x00), dbus.Byte(0x03)]

    def ReadValue(self, options):
        return self.value

class HIDControlPointCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_HID_CONTROL_POINT, ["write", "write-without-response"], service)
        self.value = [dbus.Byte(0x00)]

    def WriteValue(self, value, options):
        self.value = value
        print("HID control point:", [int(v) for v in value])

class ReportReferenceDescriptor(Descriptor):
    def __init__(self, bus, index, characteristic, report_id, report_type):
        super().__init__(bus, index, UUID_REPORT_REFERENCE, ["read"], characteristic)
        self.value = [dbus.Byte(report_id), dbus.Byte(report_type)]  # type=1 input

    def ReadValue(self, options):
        return self.value


class BootKeyboardInputReportCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_BOOT_KEYBOARD_INPUT_REPORT, ["read", "notify"], service)
        self.notifying = False
        self.input_report_ref = None
        self.value = [dbus.Byte(0), dbus.Byte(0), dbus.Byte(0), dbus.Byte(0),
                      dbus.Byte(0), dbus.Byte(0), dbus.Byte(0), dbus.Byte(0)]

    def ReadValue(self, options):
        return self.value

    def StartNotify(self):
        self.notifying = True
        print("Boot keyboard input notifications enabled")
        if self.input_report_ref is not None:
            start_console_sender(self.input_report_ref)

    def StopNotify(self):
        self.notifying = False
        print("Boot keyboard input notifications disabled")

    def send_key(self, modifier, keycode):
        if not self.notifying:
            return
        report = [modifier, 0x00, keycode, 0x00, 0x00, 0x00, 0x00, 0x00]
        self.value = [dbus.Byte(x) for x in report]
        self.PropertiesChanged(
            GATT_CHRC_IFACE,
            {"Value": dbus.Array(self.value, signature="y")},
            [],
        )

    def send_keyboard_state(self, modifier, keycodes):
        if not self.notifying:
            return
        keys = list(keycodes[:6])
        while len(keys) < 6:
            keys.append(0)
        report = [modifier & 0xFF, 0x00] + [key & 0xFF for key in keys]
        self.value = [dbus.Byte(x) for x in report]
        self.PropertiesChanged(
            GATT_CHRC_IFACE,
            {"Value": dbus.Array(self.value, signature="y")},
            [],
        )

    def send_release(self):
        if not self.notifying:
            return
        report = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        self.value = [dbus.Byte(x) for x in report]
        self.PropertiesChanged(
            GATT_CHRC_IFACE,
            {"Value": dbus.Array(self.value, signature="y")},
            [],
        )


class BootKeyboardOutputReportCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(
            bus, index, UUID_BOOT_KEYBOARD_OUTPUT_REPORT,
            ["read", "write", "write-without-response"], service
        )
        self.value = [dbus.Byte(0x00)]

    def ReadValue(self, options):
        return self.value

    def WriteValue(self, value, options):
        self.value = value if value else [dbus.Byte(0x00)]
        print("Boot keyboard output report:", [int(v) for v in self.value])


class OutputReportCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(
            bus, index, UUID_REPORT,
            ["read", "write", "write-without-response"], service
        )
        self.value = [dbus.Byte(0x00)]
        self.add_descriptor(ReportReferenceDescriptor(bus, 0, self, 1, 2))

    def ReadValue(self, options):
        return self.value

    def WriteValue(self, value, options):
        self.value = value if value else [dbus.Byte(0x00)]
        print("Output report (LEDs):", [int(v) for v in self.value])

class InputReportCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_REPORT, ["read", "notify"], service)
        self.notifying = False
        self.value = [dbus.Byte(0), dbus.Byte(0), dbus.Byte(0), dbus.Byte(0),
                      dbus.Byte(0), dbus.Byte(0), dbus.Byte(0), dbus.Byte(0)]
        self.add_descriptor(ReportReferenceDescriptor(bus, 0, self, 1, 1))
        self.demo_text = "hello from etherwaver\n"
        self.demo_index = 0
        self.boot_input = None
        self.protocol_mode = None

    def ReadValue(self, options):
        return self.value

    def StartNotify(self):
        if self.notifying:
            return
        self.notifying = True
        print("Input report notifications enabled")
        start_console_sender(self)
        if ENABLE_AUTO_DEMO:
            GLib.timeout_add(1500, self._send_demo)

    def StopNotify(self):
        self.notifying = False
        print("Input report notifications disabled")

    def _is_boot_mode(self):
        if self.protocol_mode is None or not self.protocol_mode.value:
            return False
        return int(self.protocol_mode.value[0]) == 0x00

    def _report_channel_ready(self):
        return self.notifying

    def _boot_channel_ready(self):
        return self.boot_input is not None and self.boot_input.notifying

    def _active_channel(self):
        if self._is_boot_mode():
            if self._boot_channel_ready():
                return "boot"
            if self._report_channel_ready():
                return "report-fallback"
            return None
        if self._report_channel_ready():
            return "report"
        if self._boot_channel_ready():
            return "boot-fallback"
        return None

    def on_protocol_mode_changed(self):
        log_evt(
            "Protocol mode changed:",
            "boot" if self._is_boot_mode() else "report",
            "report_notify=", self.notifying,
            "boot_notify=", self._boot_channel_ready(),
        )

    def send_key(self, modifier, keycode):
        channel = self._active_channel()
        if channel is None:
            return

        if channel.startswith("report"):
            # Modifiers + reserved + 6 key slots. Report ID is carried by Report Reference.
            report = [modifier, 0x00, keycode, 0x00, 0x00, 0x00, 0x00, 0x00]
            self.value = [dbus.Byte(x) for x in report]
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {"Value": dbus.Array(self.value, signature="y")},
                [],
            )
            return

        if self.boot_input is not None:
            self.boot_input.send_key(modifier, keycode)

    def send_keyboard_state(self, modifier, keycodes):
        channel = self._active_channel()
        if channel is None:
            log_evt("Cannot send keyboard state: notifications are not enabled by host yet.")
            return

        keys = list(keycodes[:6])
        while len(keys) < 6:
            keys.append(0)

        if channel.startswith("report"):
            report = [modifier & 0xFF, 0x00] + [key & 0xFF for key in keys]
            self.value = [dbus.Byte(x) for x in report]
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {"Value": dbus.Array(self.value, signature="y")},
                [],
            )
            return

        if self.boot_input is not None:
            self.boot_input.send_keyboard_state(modifier, keys)

    def send_release(self):
        channel = self._active_channel()
        if channel is None:
            return

        if channel.startswith("report"):
            report = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
            self.value = [dbus.Byte(x) for x in report]
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {"Value": dbus.Array(self.value, signature="y")},
                [],
            )
            return

        if self.boot_input is not None:
            self.boot_input.send_release()

    def type_text(self, text):
        channel = self._active_channel()
        if channel is None:
            log_evt("Cannot send text: notifications are not enabled by host yet.")
            return
        log_evt(
            "Sending text via HID.",
            "channel=", channel,
            "report_notify=", self.notifying,
            "boot_notify=", self._boot_channel_ready(),
            "len=", len(text),
        )
        for ch in text:
            if ch not in CHAR_TO_HID:
                continue
            modifier, keycode = CHAR_TO_HID[ch]
            self.send_combo(modifier, keycode, hold_us=45000, gap_us=25000)

    def send_combo(self, modifier, keycode, hold_us=45000, gap_us=25000):
        if self._active_channel() is None:
            log_evt("Cannot send key combo: notifications are not enabled by host yet.")
            return
        # For non-modifier keys Android is most reliable with simple press->release.
        # For modifier combos, press modifier first, then key, then release.
        if modifier:
            self.send_key(modifier, 0x00)
            while GLib.main_context_default().iteration(False):
                pass
            GLib.usleep(12000)
        self.send_key(modifier, keycode)
        while GLib.main_context_default().iteration(False):
            pass
        GLib.usleep(hold_us)

        if modifier:
            self.send_key(modifier, 0x00)
            while GLib.main_context_default().iteration(False):
                pass
            GLib.usleep(12000)

        self.send_release()
        while GLib.main_context_default().iteration(False):
            pass
        GLib.usleep(gap_us)

    def can_send(self):
        return self._active_channel() is not None

    def print_diag(self):
        log_evt(
            "DIAG:",
            "protocol_mode=", "boot" if self._is_boot_mode() else "report",
            "report_notify=", self.notifying,
            "boot_notify=", self._boot_channel_ready(),
            "channel=", self._active_channel(),
            "report_value=", [int(v) for v in self.value],
        )
        return False

    def _send_demo(self):
        if not self.notifying:
            return False
        if self.demo_index >= len(self.demo_text):
            print("Demo text sent")
            return False

        ch = self.demo_text[self.demo_index]
        self.demo_index += 1

        if ch in CHAR_TO_HID:
            modifier, keycode = CHAR_TO_HID[ch]
            self.send_key(modifier, keycode)
            GLib.timeout_add(40, self._release_after_key)
        return True

    def _release_after_key(self):
        if self.notifying:
            self.send_release()
        return False


class MouseInputReportCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_REPORT, ["read", "notify"], service)
        self.notifying = False
        self.value = [dbus.Byte(0), dbus.Byte(0), dbus.Byte(0), dbus.Byte(0)]
        self.add_descriptor(ReportReferenceDescriptor(bus, 0, self, 2, 1))

    def ReadValue(self, options):
        return self.value

    def StartNotify(self):
        if self.notifying:
            return
        self.notifying = True
        print("Mouse input report notifications enabled")

    def StopNotify(self):
        self.notifying = False
        print("Mouse input report notifications disabled")

    def can_send(self):
        return self.notifying

    def send_mouse_event(self, dx=0, dy=0, buttons=0, wheel=0):
        if not self.notifying:
            log_evt("Cannot send mouse event: notifications are not enabled by host yet.")
            return

        report = [
            buttons & 0x07,
            dx & 0xFF,
            dy & 0xFF,
            wheel & 0xFF,
        ]
        self.value = [dbus.Byte(x) for x in report]
        self.PropertiesChanged(
            GATT_CHRC_IFACE,
            {"Value": dbus.Array(self.value, signature="y")},
            [],
        )

        # Release mouse buttons after click-style events.
        if buttons:
            GLib.usleep(12000)
            self.value = [dbus.Byte(0), dbus.Byte(0), dbus.Byte(0), dbus.Byte(0)]
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {"Value": dbus.Array(self.value, signature="y")},
                [],
            )

    def send_mouse_state(self, dx=0, dy=0, buttons=0, wheel=0):
        if not self.notifying:
            log_evt("Cannot send mouse state: notifications are not enabled by host yet.")
            return

        report = [
            buttons & 0x07,
            dx & 0xFF,
            dy & 0xFF,
            wheel & 0xFF,
        ]
        self.value = [dbus.Byte(x) for x in report]
        self.PropertiesChanged(
            GATT_CHRC_IFACE,
            {"Value": dbus.Array(self.value, signature="y")},
            [],
        )

class BatteryLevelCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_BATTERY_LEVEL, ["read"], service)
        self.value = [dbus.Byte(100)]

    def ReadValue(self, options):
        return self.value

class PnPIDCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UUID_PNP_ID, ["read"], service)
        # Vendor ID source=USB IF (1), vendor=0xFFFF, product=0x0001, version=0x0001
        self.value = [
            dbus.Byte(0x01),
            dbus.Byte(0xFF), dbus.Byte(0xFF),
            dbus.Byte(0x01), dbus.Byte(0x00),
            dbus.Byte(0x01), dbus.Byte(0x00),
        ]

    def ReadValue(self, options):
        return self.value

class HIDService(Service):
    def __init__(self, bus, index):
        super().__init__(bus, index, UUID_HID_SERVICE, True)
        self.protocol_mode = ProtocolModeCharacteristic(bus, 0, self)
        self.add_characteristic(self.protocol_mode)
        self.add_characteristic(ReportMapCharacteristic(bus, 1, self))
        self.add_characteristic(HIDInformationCharacteristic(bus, 2, self))
        self.add_characteristic(HIDControlPointCharacteristic(bus, 3, self))
        self.boot_input = BootKeyboardInputReportCharacteristic(bus, 4, self)
        self.add_characteristic(self.boot_input)
        self.boot_output = BootKeyboardOutputReportCharacteristic(bus, 5, self)
        self.add_characteristic(self.boot_output)
        self.input_report = InputReportCharacteristic(bus, 6, self)
        self.input_report.boot_input = self.boot_input
        self.input_report.protocol_mode = self.protocol_mode
        self.protocol_mode.input_report = self.input_report
        self.boot_input.input_report_ref = self.input_report
        self.add_characteristic(self.input_report)
        self.output_report = OutputReportCharacteristic(bus, 7, self)
        self.add_characteristic(self.output_report)
        self.mouse_input_report = MouseInputReportCharacteristic(bus, 8, self)
        self.add_characteristic(self.mouse_input_report)

class BatteryService(Service):
    def __init__(self, bus, index):
        super().__init__(bus, index, UUID_BATTERY_SERVICE, True)
        self.add_characteristic(BatteryLevelCharacteristic(bus, 0, self))

class DeviceInfoService(Service):
    def __init__(self, bus, index):
        super().__init__(bus, index, UUID_DEVICE_INFO, True)
        self.add_characteristic(PnPIDCharacteristic(bus, 0, self))

class Agent(dbus.service.Object):
    def __init__(self, bus, path):
        self.bus = bus
        self.path = path
        super().__init__(bus, path)

    def _set_trusted(self, device):
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ_SERVICE_NAME, device), DBUS_PROP_IFACE)
            props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        except Exception as e:
            print("Failed to set Trusted for", device, ":", e)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        print("Agent released")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log_evt("Agent RequestPinCode:", device)
        pincode = safe_input("PIN code (empty=0000): ").strip()
        return pincode if pincode else "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        log_evt("Agent RequestPasskey:", device)
        while True:
            raw = safe_input("Passkey from phone (6 digits): ").strip()
            if raw.isdigit() and len(raw) == 6:
                return dbus.UInt32(int(raw))
            print("Invalid passkey, expected exactly 6 digits.")

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log_evt("Agent DisplayPasskey:", device, f"{int(passkey):06d}", "entered:", int(entered))

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log_evt("Agent DisplayPinCode:", device, pincode)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log_evt("Agent RequestConfirmation:", device, f"{int(passkey):06d}")
        self._set_trusted(device)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log_evt("Agent RequestAuthorization:", device)
        self._set_trusted(device)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log_evt("Agent AuthorizeService:", device, uuid)
        self._set_trusted(device)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        log_evt("Agent canceled")

def find_adapter(bus):
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    objects = om.GetManagedObjects()
    for path, interfaces in objects.items():
        if GATT_MANAGER_IFACE in interfaces and LE_ADVERTISING_MANAGER_IFACE in interfaces:
            return path
    return None


def remove_known_devices(bus, adapter_path):
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    adapter = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), "org.bluez.Adapter1")
    objects = om.GetManagedObjects()

    removed = 0
    for path, interfaces in objects.items():
        if not path.startswith(adapter_path + "/dev_"):
            continue
        if DEVICE_IFACE not in interfaces:
            continue

        props = interfaces[DEVICE_IFACE]
        addr = props.get("Address", "?")
        alias = props.get("Alias", "")
        paired = bool(props.get("Paired", False))
        trusted = bool(props.get("Trusted", False))

        try:
            adapter.RemoveDevice(dbus.ObjectPath(path))
            removed += 1
            log_evt("Removed known device:", _dev_name_from_path(path), addr, alias, {"paired": paired, "trusted": trusted})
        except Exception as e:
            log_evt("Failed to remove device:", _dev_name_from_path(path), addr, alias, e)

    log_evt("Known device cleanup done. removed=", removed)

def register_app_cb():
    print("GATT app registered")

def register_app_error_cb(error):
    print("Failed to register GATT app:", error)
    mainloop.quit()

def register_ad_cb():
    print("Advertisement registered")

def register_ad_error_cb(error):
    print("Failed to register advertisement:", error)
    mainloop.quit()


def _dev_name_from_path(path):
    return str(path).split("/")[-1] if path else "<unknown>"


def on_interfaces_added(path, interfaces):
    if DEVICE_IFACE in interfaces:
        props = interfaces.get(DEVICE_IFACE, {})
        addr = props.get("Address", "?")
        alias = props.get("Alias", "")
        log_evt("Device discovered:", _dev_name_from_path(path), addr, alias)


def on_interfaces_removed(path, interfaces):
    if DEVICE_IFACE in interfaces:
        log_evt("Device removed:", _dev_name_from_path(path))


def on_properties_changed(interface, changed=None, invalidated=None, path=None):
    if changed is None:
        changed = {}
    if invalidated is None:
        invalidated = []
    if interface != DEVICE_IFACE:
        return

    interesting = ["Connected", "Paired", "Trusted", "ServicesResolved", "RSSI", "UUIDs"]
    summary = {}
    for key in interesting:
        if key in changed:
            summary[key] = changed[key]
    if summary:
        log_evt("Device state:", _dev_name_from_path(path), summary)

    if "Connected" in changed:
        connected = bool(changed["Connected"])
        if connected:
            if current_input_report is not None:
                start_console_sender(current_input_report)
        else:
            stop_console_sender()

    # Heuristic for common pairing failure pattern.
    if "Connected" in changed and not bool(changed["Connected"]):
        log_evt("Disconnected:", _dev_name_from_path(path),
                "(if Paired=False, pairing/authentication likely failed)")

def main():
    global mainloop, current_input_report, current_mouse_report
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    adapter_path = find_adapter(bus)
    if not adapter_path:
        print("BLE GATT/Advertising adapter not found")
        return

    print("Using adapter:", adapter_path)

    adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter_path)
    props = dbus.Interface(adapter_obj, DBUS_PROP_IFACE)
    remove_known_devices(bus, adapter_path)

    # Power on adapter
    props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(1))
    props.Set("org.bluez.Adapter1", "Alias", dbus.String("EtherWaver Keyboard"))
    props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(1))
    props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(1))
    props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
    props.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))

    bus.add_signal_receiver(
        on_properties_changed,
        dbus_interface=DBUS_PROP_IFACE,
        signal_name="PropertiesChanged",
        path_keyword="path",
    )
    bus.add_signal_receiver(
        on_interfaces_added,
        dbus_interface=DBUS_OM_IFACE,
        signal_name="InterfacesAdded",
    )
    bus.add_signal_receiver(
        on_interfaces_removed,
        dbus_interface=DBUS_OM_IFACE,
        signal_name="InterfacesRemoved",
    )

    # Register agent with both display and keyboard capabilities.
    agent = Agent(bus, "/org/etherwaver/agent")
    agent_manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/org/bluez"), AGENT_MANAGER_IFACE)
    try:
        agent_manager.RegisterAgent(agent.path, "KeyboardDisplay")
        print("Agent registered as KeyboardDisplay")
    except Exception as e:
        print("RegisterAgent:", e)
    try:
        agent_manager.RequestDefaultAgent(agent.path)
        print("Agent set as default")
    except Exception as e:
        print("RequestDefaultAgent:", e)

    app = Application(bus)
    hid_service = HIDService(bus, 0)
    current_input_report = hid_service.input_report
    current_mouse_report = hid_service.mouse_input_report
    start_control_server()
    start_etherwaver_bridge_server()
    app.add_service(hid_service)
    app.add_service(BatteryService(bus, 1))
    app.add_service(DeviceInfoService(bus, 2))

    service_manager = dbus.Interface(adapter_obj, GATT_MANAGER_IFACE)
    managed = app.GetManagedObjects()
    svc_count = sum(1 for _, ifaces in managed.items() if GATT_SERVICE_IFACE in ifaces)
    chrc_count = sum(1 for _, ifaces in managed.items() if GATT_CHRC_IFACE in ifaces)
    print("GATT objects prepared:", len(managed), "services:", svc_count, "characteristics:", chrc_count)
    for path, ifaces in managed.items():
        ch = ifaces.get(GATT_CHRC_IFACE)
        if ch:
            print("CHRC", path, "flags=", ch.get("Flags"))
    service_manager.RegisterApplication(app.get_path(), {},
                                        reply_handler=register_app_cb,
                                        error_handler=register_app_error_cb)

    ad = Advertisement(bus, 0)
    ad_manager = dbus.Interface(adapter_obj, LE_ADVERTISING_MANAGER_IFACE)
    ad_manager.RegisterAdvertisement(ad.get_path(), {},
                                     reply_handler=register_ad_cb,
                                     error_handler=register_ad_error_cb)

    print("Ready.")
    print("Known devices removed. Ready for fresh pairing from Android/PC.")
    if ENABLE_AUTO_DEMO:
        print("When notifications start, demo text should type automatically once.")
    if ENABLE_CONSOLE_SENDER:
        print("Interactive test will start after host enables input notifications.")
    mainloop = GLib.MainLoop()
    mainloop.run()


def run_server(auto_demo=False, interactive_console=False):
    global ENABLE_AUTO_DEMO, ENABLE_CONSOLE_SENDER
    ENABLE_AUTO_DEMO = auto_demo
    ENABLE_CONSOLE_SENDER = interactive_console
    main()

if __name__ == "__main__":
    run_server(auto_demo=False, interactive_console=False)
