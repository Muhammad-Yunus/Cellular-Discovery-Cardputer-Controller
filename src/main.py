# Cardputer - Subnet scanner + device status viewer (UIFlow2 / MicroPython)
#
# Two views:
#
#   LIST    sweeps every host of this device's own subnet, calls
#           http://<host>:8001/health on the ones that answer, and lists them.
#           UP/DOWN moves the cursor, ENTER opens the selected host.
#
#   DETAIL  calls GET http://<ip>:8001/api/v1/device/status with
#           accept: application/json and shows a short fixed set of fields
#           instead of the whole reply, one field per line:
#
#               sdr.status      sdr.message
#               gps.status      gps.message
#               net.status      net.ip_address      net.hostname
#
#           ("net." is short for "network." so the label column stays narrow
#           enough to leave room for the values.) A value is never wrapped:
#           when it is wider than the value column it keeps its single line
#           and scrolls sideways, pausing on the first characters of every
#           pass. UP/DOWN steps that scroll by hand, ENTER or ESC goes back.
#           A field that is absent from the reply is shown as "not in reply"
#           so nothing is silent.
#
# Nothing is hardcoded: the sweep range comes from this device's ifconfig() IP
# and netmask, and the navigation keys are read from the firmware's own
# unit.KeyCode table at runtime.
#
# Note on the keys: the official documentation only documents
# KeyCode.KEYCODE_ENTER for MatrixKeyboard, so UP/DOWN are bound from KeyCode
# when the firmware exposes them, with letter keys (W/S, K/J, Q) always working
# as a fallback. An unmapped non-printable key is printed on the serial console
# as "KEY <n>" so the real arrow codes can be read off the device.

import time
import json
import socket
import network
import requests2
import M5
from M5 import *

# ---------------- configuration ----------------
PORT = 8001
SCAN_PATH = "/health"
DETAIL_PATH = "/api/v1/device/status"
SCAN_HEADERS = {"Content-Type": "application/json"}
DETAIL_HEADERS = {"accept": "application/json"}
PROBE_TIMEOUT = 0.25     # seconds per TCP connect during the sweep
HTTP_TIMEOUT = 3         # seconds per HTTP call
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
C_SEL = 0x24405E

# 240 x 135 landscape
HEAD_H = 17
BAR_Y = 20
BAR_H = 8
LIST_Y = 32
LIST_ROWS = 6
DETAIL_Y = 20
DETAIL_ROWS = 7
LINE_H = 14
FOOT_Y = 119
RIGHT_X = 236
ROW_X = 14               # list text starts right of the marker triangle
F_SMALL = None
F_BIG = None

# ---------------- detail view fields ----------------
# Only these fields of the device status reply are shown, in this order:
# (label on screen, path inside the JSON, kind)
DETAIL_FIELDS = (
    ("sdr.status",     ("sdr", "status"),         "status"),
    ("sdr.message",    ("sdr", "message"),        "text"),
    ("gps.status",     ("gps", "status"),         "status"),
    ("gps.message",    ("gps", "message"),        "text"),
    ("net.status",     ("network", "status"),     "status"),
    ("net.ip_address", ("network", "ip_address"), "text"),
    ("net.hostname",   ("network", "hostname"),   "text"),
)
MISSING = "not in reply"
DETAIL_X = 6             # label column
DETAIL_VX = 110          # value column (longest label is 14 characters)
DETAIL_AVAIL = RIGHT_X - DETAIL_VX   # pixels a value gets before it scrolls
MARQUEE_GAP = "   "      # shown between the end of a value and its restart
MARQUEE_MS = 260         # ms per character step
MARQUEE_HOLD = 1200      # ms the first characters stay put on every pass

# Words that decide the colour of a status field. Exact matches are checked
# first, then substrings, so "no fix" reads as bad and "fix" as good.
GOOD_VALUES = ("ok", "up", "on", "online", "true", "yes", "healthy", "ready",
               "active", "connected", "running", "good", "fix", "fixed", "locked")
BAD_VALUES = ("error", "fail", "failed", "down", "off", "offline", "false",
              "unhealthy", "disconnected", "stopped", "bad", "timeout", "no fix")
BAD_PARTS = ("error", "fail", "offline", "unavailable", "disconnected", "denied",
             "timeout", "no fix", "not ")
GOOD_PARTS = ("ok", "online", "up", "fix", "ready", "active", "connected", "locked")

# ---------------- state ----------------
wlan = None
kb = None
state = "wifi"           # wifi | nowifi | run
my_ip = ""
rssi = 0
cache = {}
header_key = None

first_host = 0           # inclusive integer host range of the sweep
last_host = 0
scan_label = ""
own_host = 0

scanning = False
scan_next = 0
scan_total = 0
scan_done = 0
hits = []                # (ip, label, is_ok)

sel = 0                  # cursor in the LIST view
manual = False           # True once the user has moved the cursor
list_start = 0           # first hit row currently rendered

view = "list"            # list | detail
detail_ip = ""
detail_http = ""         # last HTTP outcome, shown in the title bar
detail_rows = []         # (label, color, text, vis, cycle) per display row
detail_sig = None        # what the body currently shows; skips redraws
detail_tag = 0           # bumped on every fetch, forces a body repaint
marquee_base = 0         # origin of the sideways scroll, reset on entry
want_detail = None       # ip requested by ENTER, fetched in the main loop

# Navigation keys are matched in both representations the keyboard may use:
# numeric codes from get_key() and strings from get_string(). Whichever the
# firmware returns for a given key, the match succeeds.
KEYS = {"up": set(), "down": set(), "enter": set(), "esc": set()}
KEY_STR = {"up": set(), "down": set(), "enter": set(), "esc": set()}
reported_keys = set()
key_events = []          # (string_or_None, code_or_None) from the callback
empty_events = 0
speaker_ok = False
wifi_t0 = 0

# One tone per key, so every press is audible even when the screen is busy.
TONE = {"up": 2400, "down": 1800, "enter": 3200, "esc": 1200}
TONE_OTHER = 700         # any key the mapping does not recognise
TONE_BTN = 900           # the side button
TONE_LEN = 50


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
    mask_i = ip_to_int(cfg[1]) if len(cfg) > 1 else 0xFFFFFF00
    own_host = ip_i

    net = ip_i & mask_i
    bcast = net | (0xFFFFFFFF ^ mask_i)
    first = net + 1
    last = bcast - 1

    # A /16 or wider would take hours: sweep only the /24 we live in.
    if last < first or (last - first + 1) > MAX_HOSTS:
        net = ip_i & 0xFFFFFF00
        first = net + 1
        last = net + 254

    first_host = first
    last_host = last
    scan_label = "%s-%d" % (int_to_ip(first), last & 255)


# ---------------- display helpers ----------------
def put(x, y, s, color, font, clear_w=0, clear_h=0, bg=C_BG):
    if x < 0:
        x = 0
    key = (x, y, s, color, bg)
    if cache.get((x, y)) == key:
        return False
    if clear_w and clear_h:
        M5.Lcd.fillRect(x, y, clear_w, clear_h, bg)
    M5.Lcd.setFont(font)
    M5.Lcd.setTextColor(color, bg)
    M5.Lcd.drawString(s, x, y)
    cache[(x, y)] = key
    return True


def text_width(s):
    tw = getattr(M5.Lcd, "textWidth", None)
    return M5.Lcd.textWidth(s) if callable(tw) else len(s) * 7


def put_right(right_x, y, s, color, font, clear_w=0, clear_h=0, bg=C_BG):
    """Right-aligned text; clear_w wipes a field ending at right_x."""
    x = right_x - text_width(s)
    if x < 0:
        x = 0
    if cache.get((x, y)) == (x, y, s, color, bg):
        return False
    if clear_w and clear_h:
        M5.Lcd.fillRect(right_x - clear_w, y, clear_w, clear_h, bg)
    return put(x, y, s, color, font, 0, 0, bg)


def put_head(left, right=""):
    """Redraws the title bar only when its contents change."""
    global header_key
    if header_key == (left, right):
        return
    M5.Lcd.setFont(F_SMALL)
    M5.Lcd.fillRect(0, 0, 240, HEAD_H, C_HEAD)
    M5.Lcd.setTextColor(C_TEXT, C_HEAD)
    M5.Lcd.drawString(left, 4, 3)
    if right:
        M5.Lcd.setTextColor(C_DIM, C_HEAD)
        M5.Lcd.drawString(right, RIGHT_X - text_width(right), 3)
    header_key = (left, right)


def clear_body():
    M5.Lcd.fillRect(0, HEAD_H, 240, FOOT_Y - HEAD_H, C_BG)
    cache.clear()
    header_key = None


def beep(freq, ms=TONE_LEN):
    """Short confirmation tone; silently does nothing without a speaker."""
    if not speaker_ok:
        return
    try:
        Speaker.tone(freq, ms)
    except Exception:
        pass


TRI_W = 7
TRI_H = 8
tri_ok = None            # None = untried, True/False = fillTriangle available


def draw_marker(x, y, color):
    """Small right-pointing triangle that marks the selected row."""
    global tri_ok
    if tri_ok is None:
        tri_ok = callable(getattr(M5.Lcd, "fillTriangle", None))
    if tri_ok:
        try:
            M5.Lcd.fillTriangle(x, y, x, y + TRI_H - 1, x + TRI_W - 1, y + (TRI_H // 2), color)
            return
        except Exception:
            tri_ok = False
    # Fallback: stack of growing/shrinking bars makes the same shape.
    for r in range(TRI_H):
        w = r + 1
        if TRI_H - r < w:
            w = TRI_H - r
        M5.Lcd.fillRect(x, y + r, w, 1, color)


# ---------------- json display ----------------
def fmt(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float):
        # 8 significant digits keeps GPS coordinates useful (~1 m) without
        # printing the full binary noise of a double.
        return "%.8g" % v
    return str(v)


def jsonable(v):
    """A dict or list inside a field is shown as compact JSON, not repr()."""
    if hasattr(v, "items") or isinstance(v, (list, tuple)):
        try:
            return json.dumps(v)
        except Exception:
            return fmt(v)
    return fmt(v)


def one_line(text, limit):
    """Body text squeezed onto one line: control characters draw as garbage."""
    out = []
    for ch in text[:limit]:
        out.append(" " if ord(ch) < 32 or ord(ch) == 127 else ch)
    return "".join(out)


def get_path(obj, path):
    """Value at a dotted path; (value, False) when any level is absent."""
    cur = obj
    for k in path:
        if not hasattr(cur, "items") or k not in cur:
            return None, False
        cur = cur[k]
    return cur, True


def value_color(kind, val):
    """Status fields are colour coded; other fields stay plain white."""
    if kind != "status":
        return C_TEXT
    s = fmt(val).strip().lower()
    if s in GOOD_VALUES:
        return C_OK
    if s in BAD_VALUES:
        return C_BAD
    for w in BAD_PARTS:
        if w in s:
            return C_BAD
    for w in GOOD_PARTS:
        if w in s:
            return C_OK
    return C_WAIT


def fit_chars(text, avail):
    """How many leading characters of text fit into avail pixels."""
    n = 0
    while n < len(text) and text_width(text[:n + 1]) <= avail:
        n += 1
    return n


def make_row(label, color, text):
    """One display row.

    label is None for a line without a label. A value wider than the value
    column is never wrapped: the row keeps its single line and scrolls
    sideways. cycle is the number of character steps in one full pass, or 0
    when the value already fits and nothing has to move.
    """
    if text_width(text) <= DETAIL_AVAIL:
        return (label, color, text, 0, 0)

    vis = fit_chars(text, DETAIL_AVAIL)
    cycle = len(text) + len(MARQUEE_GAP) - vis
    if cycle < 2:
        cycle = 2
    return (label, color, text, vis, cycle)


def marquee_offset(cycle):
    """Character offset of a scrolling value right now, pausing at the start.

    Offsets are derived from the clock instead of being stored, so every row
    keeps its own pass length while all of them start together.
    """
    if cycle < 2:
        return 0
    el = time.ticks_diff(time.ticks_ms(), marquee_base)
    if el < MARQUEE_HOLD:
        return 0
    return ((el - MARQUEE_HOLD) // MARQUEE_MS) % cycle


def marquee_nudge(step):
    """Step the sideways scroll by hand: +1 shows earlier text, -1 later."""
    global marquee_base
    if step > 0:
        # Never push the origin into the future, or the value would freeze.
        if time.ticks_diff(time.ticks_ms(), marquee_base) <= MARQUEE_HOLD:
            return
        marquee_base -= MARQUEE_MS
    else:
        marquee_base += MARQUEE_MS


def row_segments(row, offset):
    """(x, text, color) pieces of one row, clipped to the visible window.

    The window is a plain slice, so the text simply walks past the value
    column: no wrapping, and nothing is drawn outside the screen.
    """
    label, color, text, vis, cycle = row
    if cycle >= 2:
        shown = (text + MARQUEE_GAP)[offset:offset + vis]
    else:
        shown = text
    if label is None:
        return [(DETAIL_X, shown, color)]
    return [(DETAIL_X, label, C_DIM), (DETAIL_VX, shown, color)]


def has_scroll():
    for row in detail_rows:
        if row[4] >= 2:
            return True
    return False


def build_detail_rows(obj):
    """The seven requested fields, exactly one display row each."""
    if not hasattr(obj, "items"):
        return [make_row(None, C_BAD, "reply is not an object")]

    rows = []
    for label, path, kind in DETAIL_FIELDS:
        val, found = get_path(obj, path)
        if found:
            text = jsonable(val)
            color = value_color(kind, val)
            if not text:
                text = "(empty)"
                color = C_DIM
        else:
            text = MISSING
            color = C_DIM
        rows.append(make_row(label, color, text))
    return rows


# ---------------- probes ----------------
def classify(e):
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


def http_get(url, headers):
    """Returns (ok, status_or_error, body_text)."""
    r = None
    try:
        try:
            socket.setdefaulttimeout(HTTP_TIMEOUT)
        except Exception:
            pass
        r = requests2.get(url, headers=headers)
        code = getattr(r, "status_code", 0)
        try:
            body = r.text
        except Exception:
            body = ""
        return (200 <= code < 300), code, body
    except OSError as e:
        return False, classify(e), ""
    except Exception as e:
        name = type(e).__name__ if hasattr(e, "__class__") else "?"
        return False, "ERR %s" % name, ""
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


def scan_check(ip):
    """The /health call used while sweeping."""
    ok, status, _ = http_get("http://%s:%d%s" % (ip, PORT, SCAN_PATH), SCAN_HEADERS)
    return ("%d OK" % status if ok else str(status)), ok


def probe_host(ip):
    """TCP first (cheap), HTTP only for hosts that answer."""
    if not tcp_open(ip):
        return
    label, ok = scan_check(ip)
    hits.append((ip, label, ok))
    print("HIT %s %s" % (ip, label))


def fetch_detail(ip):
    """Call the status API and keep only the requested fields."""
    global detail_rows, detail_tag, detail_http

    ok, status, body = http_get("http://%s:%d%s" % (ip, PORT, DETAIL_PATH), DETAIL_HEADERS)
    print("DETAIL %s -> %s (%d bytes)" % (ip, status, len(body)))
    detail_tag += 1

    if not ok:
        detail_http = str(status)
        detail_rows = [make_row(None, C_BAD, "HTTP %s" % status)]
        if body:
            detail_rows.append(make_row(None, C_DIM, one_line(body, 200)))
        return

    detail_http = "%s OK" % status
    parsed = None
    try:
        parsed = json.loads(body)
    except Exception as e:
        print("DETAIL json parse failed: %s" % e)

    if parsed is not None:
        detail_rows = build_detail_rows(parsed)
    else:
        # Not JSON: show the raw body, on one scrolling line, nothing hidden.
        detail_rows = [make_row(None, C_WAIT, "%s OK (raw)" % status)]
        if body:
            detail_rows.append(make_row(None, C_TEXT, one_line(body, 200)))

    for row in detail_rows:
        print("  %-16s %s" % (row[0] or "", row[2]))


# ---------------- scan control ----------------
def start_scan():
    global scanning, scan_next, scan_total, scan_done
    global hits, sel, manual, list_start, view, detail_rows, detail_sig

    derive_range()
    scan_next = first_host
    scan_total = last_host - first_host + 1
    scan_done = 0
    hits = []
    sel = 0
    manual = False
    list_start = 0
    view = "list"
    detail_rows = []
    detail_sig = None
    scanning = True
    clear_body()
    put_head("SCANNING", scan_label)
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
    if scan_next != own_host:          # skip ourselves, we know we are up
        probe_host(int_to_ip(scan_next))
    scan_next += 1
    scan_done += 1
    if scan_done % 32 == 0:            # let the stack recycle sockets
        time.sleep_ms(30)


# ---------------- views ----------------
def window_start(count, rows, cursor):
    """First visible row, keeping the cursor on screen once the user moves it."""
    if not manual:
        start = count - rows
        return start if start > 0 else 0
    start = list_start
    if cursor < start:
        start = cursor
    elif cursor >= start + rows:
        start = cursor - rows + 1
    hi = count - rows
    if start > hi:
        start = hi
    return start if start > 0 else 0


def draw_list_view():
    global list_start, sel

    M5.Lcd.fillRect(4, BAR_Y, 232, BAR_H, C_TRACK)
    if scan_total:
        filled = (232 * scan_done) // scan_total
        if filled > 0:
            M5.Lcd.fillRect(4, BAR_Y, filled, BAR_H, C_WAIT if scanning else C_OK)

    count = len(hits)
    if count:
        if not manual:
            sel = count - 1      # follow the newest hit until the user moves
        if sel > count - 1:
            sel = count - 1
        if sel < 0:
            sel = 0
    else:
        sel = 0
    list_start = window_start(count, LIST_ROWS, sel)

    if scanning:
        put_head("SCANNING %d/%d" % (scan_done, scan_total), scan_label)
    else:
        put_head("%d HIT%s" % (count, "" if count == 1 else "S"), scan_label)

    for i in range(LIST_ROWS):
        idx = list_start + i
        y = LIST_Y + i * LINE_H
        if idx >= count:
            put(0, y, "", C_BG, F_SMALL, 240, LINE_H)
            continue
        ip, label, ok = hits[idx]
        selected = (idx == sel)
        bg = C_SEL if selected else C_BG
        # put() returns True only when the row was actually repainted, which is
        # exactly when the marker has to be redrawn on top of the new fill.
        drawn = put(0, y, "", C_BG, F_SMALL, 240, LINE_H, bg)
        put(ROW_X, y, ip, C_TEXT, F_SMALL, 150, LINE_H, bg)
        put_right(RIGHT_X, y, label, C_OK if ok else C_WAIT, F_SMALL, 80, LINE_H, bg)
        if selected and drawn:
            draw_marker(3, y + 3, C_OK)

    if scanning:
        put(4, FOOT_Y, "btnA=stop", C_DIM, F_SMALL, 232, LINE_H)
        put_right(RIGHT_X, FOOT_Y, "w/s or ^v", C_DIM, F_SMALL, 110, LINE_H)
    elif count:
        put(4, FOOT_Y, "enter=detail  btnA=rescan", C_DIM, F_SMALL, 232, LINE_H)
    else:
        put(4, FOOT_Y, "nothing found  btnA=rescan", C_DIM, F_SMALL, 232, LINE_H)


def draw_detail_view():
    global detail_sig

    put_head(detail_ip, detail_http)

    # There are never more rows than the screen holds, so the only thing that
    # can change is the sideways scroll of the long values.
    offsets = []
    for row in detail_rows:
        offsets.append(marquee_offset(row[4]))

    # The body is repainted only when the scroll actually moves, so a still
    # screen costs nothing while the long values keep walking.
    sig = (detail_tag, tuple(offsets))
    if sig == detail_sig:
        return
    detail_sig = sig
    cache.clear()
    M5.Lcd.fillRect(0, DETAIL_Y, 240, DETAIL_ROWS * LINE_H, C_BG)
    M5.Lcd.setFont(F_SMALL)
    for i in range(len(detail_rows)):
        if i >= DETAIL_ROWS:
            break
        y = DETAIL_Y + i * LINE_H
        for seg in row_segments(detail_rows[i], offsets[i]):
            M5.Lcd.setTextColor(seg[2], C_BG)
            M5.Lcd.drawString(seg[1], seg[0], y)

    put(4, FOOT_Y, "enter/esc=back", C_DIM, F_SMALL, 150, LINE_H)
    # Always redraw the right half: the list view has its own hint there and
    # the body repaint above does not reach the footer row.
    put_right(RIGHT_X, FOOT_Y, "^v=scroll value" if has_scroll() else "",
              C_DIM, F_SMALL, 130, LINE_H)


def draw_waiting():
    waiting = (state == "wifi")
    put_head("SUBNET SCAN")
    if waiting:
        put(4, 40, "WIFI...", C_WAIT, F_BIG, 232, 30)
        put(4, 78, "joining saved network", C_DIM, F_SMALL, 232, 16)
    else:
        put(4, 40, "NO WIFI", C_BAD, F_BIG, 232, 30)
        put(4, 78, "check launcher config", C_DIM, F_SMALL, 232, 16)
    put(4, FOOT_Y, "btnA=retry", C_DIM, F_SMALL, 236, LINE_H)


# ---------------- input ----------------
def resolve_keys():
    """Bind navigation keys from the firmware's own KeyCode table."""
    try:
        from unit import KeyCode
        # Bind every name the table exposes rather than a guessed shortlist,
        # and print the candidates so the real names are visible.
        table = [(n, getattr(KeyCode, n)) for n in dir(KeyCode) if n.startswith("KEYCODE_")]
        for name, v in table:
            if not isinstance(v, int):
                continue
            n = name.upper()
            if "UP" in n:
                KEYS["up"].add(v)
            elif "DOWN" in n:
                KEYS["down"].add(v)
            elif "ENTER" in n or "RETURN" in n:
                KEYS["enter"].add(v)
            elif "ESC" in n:
                KEYS["esc"].add(v)
        cands = [(n, v) for (n, v) in table
                 if any(k in n.upper() for k in ("UP", "DOWN", "ENTER", "RETURN", "ESC", "ARROW"))]
        print("KeyCode: %d entries, nav candidates %s" % (len(table), cands))
    except Exception as e:
        print("KeyCode unavailable: %s" % e)

    # Letter keys always work, whatever the firmware exposes.
    KEYS["up"].update((ord("w"), ord("W"), ord("k"), ord("K")))
    KEYS["down"].update((ord("s"), ord("S"), ord("j"), ord("J")))
    KEYS["enter"].update((13, 10, 40))
    KEYS["esc"].update((27, ord("q"), ord("Q")))

    KEY_STR["up"].update(("up", "w", "k"))
    KEY_STR["down"].update(("down", "s", "j"))
    KEY_STR["enter"].update(("enter", "return", "\r", "\n"))
    KEY_STR["esc"].update(("esc", "escape", "q", "\x1b"))
    print("KEYS num up=%s enter=%s | str up=%s enter=%s" %
          (sorted(KEYS["up"]), sorted(KEYS["enter"]),
           sorted(KEY_STR["up"]), sorted(KEY_STR["enter"])))


kb_ticks = True          # set False if this keyboard has no tick()


def tick_keyboard():
    """Service the keyboard each pass, as the CardKB-style API expects."""
    global kb_ticks
    if kb is None or not kb_ticks:
        return
    tick = getattr(kb, "tick", None)
    if not callable(tick):
        kb_ticks = False
        print("MatrixKeyboard has no tick()")
        return
    try:
        tick()
    except Exception as e:
        kb_ticks = False
        print("kb.tick() failed: %s" % e)


def on_key(kb_obj):
    """Callback: capture the key as a string and as a code, whichever exists."""
    global empty_events
    s = None
    code = None
    try:
        s = kb_obj.get_string()
    except Exception:
        s = None
    try:
        code = kb_obj.get_key()
    except Exception:
        code = None
    if not s and not code:
        # Worth knowing: the driver fired but exposed no usable key.
        if empty_events < 3:
            empty_events += 1
            print("KEY event with empty string and zero code")
        return
    key_events.append((s, code))


def match_key(s, code):
    """Map a key event to up/down/enter/esc, or None."""
    text = s.strip().lower() if isinstance(s, str) else None
    if code is not None:
        for slot in ("up", "down", "enter", "esc"):
            if code in KEYS[slot]:
                return slot
    if not text:
        return None
    for slot in ("up", "down", "enter", "esc"):
        if text in KEY_STR[slot]:
            return slot
    # Tolerate decorated names such as "fn+up" or "arrow_down".
    if "up" in text:
        return "up"
    if "down" in text:
        return "down"
    if "enter" in text or "return" in text:
        return "enter"
    if "esc" in text:
        return "esc"
    return None


def log_key(s, code, slot):
    """Print every distinct key event: this is how the real codes surface."""
    if (s, code) in reported_keys:
        return
    reported_keys.add((s, code))
    print("KEY str=%r int=%r -> %s" % (s, code, slot if slot else "UNMAPPED"))


def handle_slot(slot):
    """Act on a recognised key."""
    global sel, manual, view, want_detail

    if view == "detail":
        # Nothing scrolls up and down here, so the keys step the sideways
        # scroll of the long values instead.
        if slot == "up":
            marquee_nudge(1)
        elif slot == "down":
            marquee_nudge(-1)
        elif slot in ("enter", "esc"):
            view = "list"
            clear_body()
        return

    if slot == "up":
        if hits:
            sel -= 1
            if sel < 0:
                sel = 0
            manual = True
    elif slot == "down":
        if hits:
            sel += 1
            if sel > len(hits) - 1:
                sel = len(hits) - 1
            manual = True
    elif slot == "enter":
        if hits and not scanning:
            want_detail = hits[sel][0]


def drain_keys():
    while key_events:
        s, code = key_events.pop(0)
        slot = match_key(s, code)
        log_key(s, code, slot)
        # Every press gets a tone: silence means the event never reached us,
        # the low 700 Hz blip means it arrived but is still unmapped.
        beep(TONE.get(slot, TONE_OTHER))
        if slot is not None:
            handle_slot(slot)


# ---------------- lifecycle ----------------
def setup():
    global wlan, kb, state, F_SMALL, F_BIG, speaker_ok

    M5.begin()
    Widgets.setRotation(1)
    F_SMALL = Widgets.FONTS.Montserrat12
    F_BIG = Widgets.FONTS.Montserrat24

    try:
        speaker_ok = bool(Speaker.begin())
        if speaker_ok:
            try:
                Speaker.setVolumePercentage(0.5)
            except Exception:
                pass
    except Exception as e:
        speaker_ok = False
        print("Speaker unavailable: %s" % e)
    print("Speaker ready: %s" % speaker_ok)

    resolve_keys()
    clear_body()
    put_head("SUBNET SCAN")

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
    except Exception as e:
        print("MatrixKeyboard unavailable: %s" % e)
        kb = None


def loop():
    global state, my_ip, rssi, scanning, want_detail, view
    global detail_ip, detail_rows, detail_sig, detail_tag, detail_http
    global marquee_base

    M5.update()
    tick_keyboard()
    drain_keys()

    if state == "wifi" and wlan.isconnected():
        my_ip = wlan.ifconfig()[0]
        state = "run"
        start_scan()
    elif state == "nowifi" and wlan.isconnected():
        my_ip = wlan.ifconfig()[0]
        state = "run"
        start_scan()
    elif state == "wifi" and not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), wifi_t0) > WIFI_WAIT_MS:
            state = "nowifi"

    if state != "run":
        if M5.BtnA.wasClicked():
            beep(TONE_BTN)
            retry_wifi()
        draw_waiting()
        time.sleep_ms(20)
        return

    if wlan.isconnected():
        try:
            rssi = wlan.status("rssi")
        except Exception:
            rssi = 0
    else:
        state = "wifi"
        scanning = False
        return

    # BtnA: stop a running sweep, otherwise start a new one.
    if M5.BtnA.wasClicked():
        beep(TONE_BTN)
        if scanning:
            stop_scan()
        else:
            start_scan()

    if want_detail is not None:
        ip = want_detail
        want_detail = None
        view = "detail"
        detail_ip = ip
        detail_http = ""
        detail_rows = []
        detail_sig = None
        detail_tag += 1
        marquee_base = time.ticks_ms()   # long values start from their head
        clear_body()
        put_head(ip, "")
        put(4, DETAIL_Y, "GET %s" % DETAIL_PATH, C_WAIT, F_SMALL, 232, LINE_H)
        fetch_detail(ip)

    if scanning:
        scan_step()

    if view == "detail":
        draw_detail_view()
    else:
        draw_list_view()

    if not scanning:
        time.sleep_ms(20)


def retry_wifi():
    global state, wifi_t0, scanning
    try:
        wlan.active(True)
    except Exception:
        pass
    state = "wifi"
    wifi_t0 = time.ticks_ms()
    scanning = False


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
