# Cardputer - Subnet scanner for the /health API (UIFlow2 / MicroPython)
#
# Sweeps every host address of the subnet this Cardputer is on and calls
#   http://<host>:8001/health
# on each one, then lists the hosts that answered.
#
# Nothing is hardcoded: the address range comes from the device's own
# ifconfig() IP and netmask, so the same program works on 192.168.1.x,
# 10.0.0.x or anything else.
#
# How a host is counted:
#   the TCP port 8001 must accept a connection first (fast, 250 ms budget),
#   and only then is the real HTTP GET issued. A host is listed green when it
#   returns 2xx, yellow when it answers on 8001 with any other status, and is
#   skipped entirely when the port does not answer.
#
# The sweep runs one host per main-loop pass, so the screen keeps updating and
# BtnA can stop it. Expect roughly one minute for a /24 where most hosts are
# silent.

import time
import socket
import network
import requests2
import M5
from M5 import *

# ---------------- configuration ----------------
PORT = 8001
PATH = "/health"
HEADERS = {"Content-Type": "application/json"}
PROBE_TIMEOUT = 0.25     # seconds per TCP connect: only live LAN hosts matter
HTTP_TIMEOUT = 2         # seconds for the /health call on hosts that answered
MAX_HOSTS = 512          # safety cap for wide netmasks (/16 and friends)
WIFI_WAIT_MS = 20000
WIFI_SSID = ""           # empty = use the WiFi saved in the launcher
WIFI_PASS = ""

# ---------------- palette ----------------
C_BG = 0x0E1420
C_HEAD = 0x1B2A41
C_TEXT = 0xFFFFFF
C_DIM = 0x8A97AB
C_OK = 0x2ECC71
C_BAD = 0xE74C3C
C_WAIT = 0xF1C40F
C_TRACK = 0x223046

# 240 x 135 landscape
BAR_Y = 21
BAR_H = 8
STATUS_Y = 32
LIST_Y = 48
LINE_H = 14
VISIBLE = 5
F_SMALL = None
F_BIG = None

# ---------------- state ----------------
wlan = None
kb = None
key_codes = []
state = "wifi"           # wifi | nowifi | run
my_ip = ""
rssi = 0
cache = {}

first_host = 0           # inclusive integer host range of the sweep
last_host = 0
scan_label = ""

scanning = False
scan_next = 0            # next host integer to probe
scan_total = 0
scan_done = 0
own_host = 0             # our own address, skipped during the sweep

hits = []                # (ip, label, is_ok)
list_start = -1          # first entry currently rendered
header_label = None      # range label currently painted in the header


# ---------------- ip helpers ----------------
def ip_to_int(s):
    p = s.split(".")
    if len(p) != 4:
        return 0
    try:
        return (int(p[0]) << 24) | (int(p[1]) << 16) | (int(p[2]) << 8) | int(p[3])
    except Exception:
        return 0


def int_to_ip(v):
    return "%d.%d.%d.%d" % ((v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255)


def prefix_of(mask_i):
    n = 0
    for i in range(32):
        if mask_i & (1 << (31 - i)):
            n += 1
        else:
            break
    return n


def derive_range():
    """Host range to sweep, taken from this device's address and netmask."""
    global first_host, last_host, scan_label, own_host

    cfg = wlan.ifconfig()
    ip_i = ip_to_int(cfg[0])
    # STA_IF reports (ip, netmask, gateway, dns); fall back to /24 if not.
    mask_i = ip_to_int(cfg[1]) if len(cfg) > 1 else 0xFFFFFF00
    own_host = ip_i

    net = ip_i & mask_i
    bcast = net | (0xFFFFFFFF ^ mask_i)
    first = net + 1
    last = bcast - 1
    prefix = prefix_of(mask_i)

    # A /16 or wider would take hours: sweep only the /24 we live in.
    if last < first or (last - first + 1) > MAX_HOSTS:
        net = ip_i & 0xFFFFFF00
        first = net + 1
        last = net + 254
        prefix = 24

    first_host = first
    last_host = last
    scan_label = "%s-%d" % (int_to_ip(first), last & 255)
    return prefix


# ---------------- display helpers ----------------
def put(x, y, s, color, font, clear_w=0, clear_h=0):
    if x < 0:
        x = 0
    key = (x, y, s, color)
    if cache.get((x, y)) == key:
        return False
    if clear_w and clear_h:
        M5.Lcd.fillRect(x, y, clear_w, clear_h, C_BG)
    M5.Lcd.setFont(font)
    M5.Lcd.setTextColor(color, C_BG)
    M5.Lcd.drawString(s, x, y)
    cache[(x, y)] = key
    return True


def text_width(s):
    tw = getattr(M5.Lcd, "textWidth", None)
    return M5.Lcd.textWidth(s) if callable(tw) else len(s) * 7


def put_right(right_x, y, s, color, font, clear_w=0, clear_h=0):
    """Right-aligned text; clear_w wipes a field ending at right_x."""
    x = right_x - text_width(s)
    if x < 0:
        x = 0
    if cache.get((x, y)) == (x, y, s, color):
        return False
    if clear_w and clear_h:
        M5.Lcd.fillRect(right_x - clear_w, y, clear_w, clear_h, C_BG)
    return put(x, y, s, color, font)


def drop_list_cache():
    band = LIST_Y + VISIBLE * LINE_H
    for k in [k for k in cache if LIST_Y <= k[1] < band]:
        del cache[k]


def draw_static():
    M5.Lcd.fillScreen(C_BG)
    M5.Lcd.fillRect(0, 0, 240, 17, C_HEAD)
    M5.Lcd.setFont(F_SMALL)
    M5.Lcd.setTextColor(C_TEXT, C_HEAD)
    M5.Lcd.drawString("SUBNET SCAN", 4, 3)
    cache.clear()


def draw_header_range():
    """Static once the range is known: not worth redrawing every pass."""
    global header_label
    if header_label == scan_label:
        return
    M5.Lcd.setFont(F_SMALL)
    M5.Lcd.fillRect(0, 0, 240, 17, C_HEAD)        # wipe the old range label
    M5.Lcd.setTextColor(C_TEXT, C_HEAD)
    M5.Lcd.drawString("SUBNET SCAN", 4, 3)
    M5.Lcd.setTextColor(C_DIM, C_HEAD)
    M5.Lcd.drawString(scan_label, 236 - text_width(scan_label), 3)
    header_label = scan_label


def draw_progress():
    M5.Lcd.fillRect(4, BAR_Y, 232, BAR_H, C_TRACK)
    if scan_total:
        filled = (232 * scan_done) // scan_total
        if filled > 0:
            M5.Lcd.fillRect(4, BAR_Y, filled, BAR_H, C_WAIT if scanning else C_OK)


def draw_status():
    if scanning:
        left = int_to_ip(scan_next) if scan_next <= last_host else "..."
        right = "%d/%d" % (scan_done, scan_total)
    else:
        left = "me " + (my_ip if my_ip else "-")
        right = "done" if scan_done else ("%d dBm" % rssi if rssi else "")
    put(4, STATUS_Y, left, C_TEXT, F_SMALL, 150, 16)
    put_right(236, STATUS_Y, right, C_DIM, F_SMALL, 80, 16)


def draw_list():
    """Render the newest page of results; only redraws when it changed."""
    global list_start

    if not hits:
        put(4, LIST_Y, "no host answered", C_DIM, F_SMALL, 236, LINE_H)
        return
    start = len(hits) - VISIBLE
    if start < 0:
        start = 0
    if start != list_start:
        M5.Lcd.fillRect(0, LIST_Y, 240, VISIBLE * LINE_H, C_BG)
        drop_list_cache()
        list_start = start
    for i in range(VISIBLE):
        idx = start + i
        y = LIST_Y + i * LINE_H
        if idx >= len(hits):
            put(4, y, "", C_DIM, F_SMALL, 236, LINE_H)
            continue
        ip, label, ok = hits[idx]
        put(4, y, ip, C_TEXT, F_SMALL, 150, LINE_H)
        put_right(236, y, label, C_OK if ok else C_WAIT, F_SMALL, 80, LINE_H)


def draw_footer():
    put(4, 121, "%d hit%s" % (len(hits), "" if len(hits) == 1 else "s"), C_DIM, F_SMALL, 150, 14)
    put_right(236, 121, "btnA=stop" if scanning else "btnA=rescan", C_DIM, F_SMALL, 110, 14)


def draw_dynamic():
    draw_header_range()
    draw_progress()
    draw_status()
    draw_list()
    draw_footer()


def draw_waiting():
    """No usable link yet."""
    waiting = (state == "wifi")
    put(4, STATUS_Y, "me -", C_TEXT, F_SMALL, 150, 16)
    put_right(236, STATUS_Y, "", C_DIM, F_SMALL, 80, 16)
    if waiting:
        put(4, 52, "WIFI...", C_WAIT, F_BIG, 232, 30)
        put(4, 88, "joining saved network", C_DIM, F_SMALL, 232, 16)
    else:
        put(4, 52, "NO WIFI", C_BAD, F_BIG, 232, 30)
        put(4, 88, "check launcher config", C_DIM, F_SMALL, 232, 16)
    put(4, 121, "btnA=retry", C_DIM, F_SMALL, 236, 14)


# ---------------- probes ----------------
def classify(e):
    """Turn an OSError into the plain-language cause."""
    code = e.args[0] if e.args else 0
    msg = str(e).upper()
    if "REFUS" in msg or code in (104, 111):
        return "REFUSED"
    if "TIMEDOUT" in msg or "TIMEOUT" in msg or code in (110, 116):
        return "TIMEOUT"
    if "UNREACH" in msg or code in (101, 113, 118):
        return "UNREACH"
    return "ERR"


def tcp_open(ip):
    """True when something accepts a TCP connection on PORT."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(PROBE_TIMEOUT)
        s.connect((ip, PORT))
        return True
    except Exception:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def http_check(ip):
    """The real API call. Returns (label, is_ok)."""
    r = None
    try:
        try:
            socket.setdefaulttimeout(HTTP_TIMEOUT)
        except Exception:
            pass
        r = requests2.get("http://%s:%d%s" % (ip, PORT, PATH), headers=HEADERS)
        code = getattr(r, "status_code", 0)
        if 200 <= code < 300:
            return "%d OK" % code, True
        return str(code), False
    except OSError as e:
        return classify(e), False
    except Exception as e:
        name = type(e).__name__ if hasattr(e, "__class__") else "?"
        return "ERR %s" % name, False
    finally:
        try:
            socket.setdefaulttimeout(None)
        except Exception:
            pass
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def probe_host(ip):
    """TCP first (cheap), HTTP only for hosts that answer."""
    if not tcp_open(ip):
        return
    label, ok = http_check(ip)
    hits.append((ip, label, ok))
    print("HIT %s %s" % (ip, label))


# ---------------- scan control ----------------
def start_scan():
    global scanning, scan_next, scan_total, scan_done
    global hits, list_start

    derive_range()
    scan_next = first_host
    scan_total = last_host - first_host + 1
    scan_done = 0
    hits = []
    list_start = -1
    scanning = True
    M5.Lcd.fillRect(0, 17, 240, 118, C_BG)
    cache.clear()
    print("SCAN %s .. %s (%d hosts)" % (int_to_ip(first_host), int_to_ip(last_host), scan_total))


def stop_scan():
    global scanning
    scanning = False
    print("SCAN STOPPED at %d/%d, %d hits" % (scan_done, scan_total, len(hits)))
    report()


def finish_scan():
    global scanning
    scanning = False
    print("SCAN DONE %d hosts, %d hits" % (scan_done, len(hits)))
    report()


def report():
    """Full result list on the serial console; the screen only fits a page."""
    if not hits:
        print("  no host answered on port %d" % PORT)
        return
    for ip, label, ok in hits:
        print("  %-16s %s" % (ip, label))


def scan_step():
    """One host per pass, so the UI and the stop button stay alive."""
    global scan_next, scan_done
    if scan_next > last_host:
        finish_scan()
        return
    ip = int_to_ip(scan_next)
    if scan_next != own_host:          # skip ourselves, we know we are up
        probe_host(ip)
    scan_next += 1
    scan_done += 1
    # let the network stack recycle sockets on long sweeps
    if scan_done % 32 == 0:
        time.sleep_ms(30)


# ---------------- input ----------------
def on_key(kb_obj):
    try:
        code = kb_obj.get_key()
    except Exception:
        return
    if code:
        key_codes.append(code)


# ---------------- lifecycle ----------------
def setup():
    global wlan, kb, state, F_SMALL, F_BIG

    M5.begin()
    Widgets.setRotation(1)
    F_SMALL = Widgets.FONTS.Montserrat12
    F_BIG = Widgets.FONTS.Montserrat24

    draw_static()

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if WIFI_SSID:
        try:
            wlan.connect(WIFI_SSID, WIFI_PASS)
        except Exception:
            pass
    state = "wifi"

    try:
        from hardware import MatrixKeyboard
        kb = MatrixKeyboard()
        kb.set_callback(on_key)
    except Exception:
        kb = None


def loop():
    # Every name assigned below must be declared global: otherwise Python
    # treats it as local for the whole function and reads raise UnboundLocalError.
    global state, my_ip, rssi, scanning

    M5.update()

    if state == "wifi" and wlan.isconnected():
        my_ip = wlan.ifconfig()[0]
        state = "run"
        start_scan()
    elif state == "nowifi" and wlan.isconnected():
        my_ip = wlan.ifconfig()[0]
        state = "run"
        start_scan()

    if state == "run":
        if wlan.isconnected():
            try:
                rssi = wlan.status("rssi")
            except Exception:
                rssi = 0
        else:
            state = "wifi"          # link dropped: fall back to waiting
            scanning = False

        # Input first, so a stop takes effect before the next host is probed.
        if M5.BtnA.wasClicked():
            if scanning:
                stop_scan()
            else:
                start_scan()
        if key_codes:
            key_codes[:] = []
            if not scanning:
                start_scan()

        if scanning:
            scan_step()

        draw_dynamic()
    else:
        draw_waiting()

    if not scanning:
        time.sleep_ms(20)


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            from utility import print_error_msg

            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
