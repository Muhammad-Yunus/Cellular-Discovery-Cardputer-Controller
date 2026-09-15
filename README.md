# Cellular Discovery Cardputer Controller

Subnet scanner + device status viewer for the M5Cardputer. Sweeps every host on the local LAN, calls `GET /health` (port `8001`) on each, and renders live results. Select any host to open its `GET /api/v1/device/status` response, displayed as a grouped key-value list.

Built for **M5Cardputer** running **UIFlow 2 / MicroPython**.

[![MicroPython](https://img.shields.io/badge/MicroPython-UIFlow%202-green.svg)](https://micropython.org/)
[![M5Cardputer](https://img.shields.io/badge/Device-M5Cardputer-blue.svg)](https://docs.m5stack.com/en/cardputer)
[![Python](https://img.shields.io/badge/Language-Python-yellow.svg)](https://www.python.org/)
[![AI-Flow](https://img.shields.io/badge/Build-AI--Flow-orange.svg)](https://aiflow.m5stack.com/)
[![M5Burner](https://img.shields.io/badge/Flash-M5Burner-red.svg)](https://m5stack.com/software/)
[![License](https://img.shields.io/badge/License-MIT-lightgrey.svg)](#)

![](docs/banner.jpg)
---

## Application Architecture

```
┌───────────────────────────────────────────────────────────┐
│  M5Cardputer (UIFlow 2 / MicroPython)                     │
│                                                           │
│  ┌───────────────────────────────────────────────────┐    │
│  │  LIST View (default)                              │    │
│  │                                                   │    │
│  │  1. WiFi connect (saved / hard-coded SSID)        │    │
│  │  2. Derive host range from ifconfig()             │    │
│  │  3. Sweep: one host per loop pass                 │    │
│  │     ├── TCP probe on :8001 (250 ms budget)        │    │
│  │     └── HTTP GET /health (3 s budget)             │    │
│  │  4. Render 6-row results list + progress bar      │    │
│  │  5. Keyboard: UP/DOWN select, ENTER → DETAIL      │    │
│  │  6. BtnA: stop scan / rescan                      │    │
│  └───────────────────────────────────────────────────┘    │
│                          │ ENTER                          │
│                          ▼                                │
│  ┌───────────────────────────────────────────────────┐    │
│  │  DETAIL View                                      │    │
│  │                                                   │    │
│  │  1. GET http://<ip>:8001/api/v1/device/status     │    │
│  │     (accept: application/json, 3 s budget)        │    │
│  │  2. Parse JSON → grouped key-value rows           │    │
│  │  3. Render 7-row scrollable detail                │    │
│  │  4. UP/DOWN scroll, ENTER/ESC → back to LIST      │    │
│  │  5. BtnA → back to LIST (and rescan if needed)    │    │
│  └───────────────────────────────────────────────────┘    │
│                                                           │
│  ┌────────────┐ ┌───────────┐ ┌─────────────┐ ┌────────┐  │
│  │  M5.Lcd    │ │ network   │ │ requests2   │ │ Speaker│  │
│  │  (display) │ │ (WiFi)    │ │ (HTTP GET)  │ │(tones) │  │
│  └────────────┘ └───────────┘ └─────────────┘ └────────┘  │
└───────────────────────────────────────────────────────────┘
```

- **No hard-coded IP ranges** — the sweep range is derived at runtime from the device's own `ifconfig()` (IP + netmask), so the same program works on `192.168.1.x`, `10.0.0.x`, etc.
- **Two-phase probe** — a fast TCP connect (250 ms) gates the full HTTP call, keeping the sweep responsive.
- **One host per loop pass** — the UI stays alive and BtnA can interrupt at any time.
- **Two API endpoints** — `/health` for the sweep, `/api/v1/device/status` for the detail view (JSON body is flattened into grouped key-value rows).
- **Speaker feedback** — every key press and BtnA press emits a short confirmation tone.

---

## Code Structure

```
.
└── src
    └── main.py          # entire application: setup, loop, scanning, UI
```

The project is a single-file MicroPython application. Key sections in `src/main.py`:

| Section | Description |
|---|---|
| `setup()` | Initialize M5, speaker, key resolution, WiFi, matrix keyboard |
| `loop()` | Main event loop: state machine + scan step + view dispatch |
| `resolve_keys()` | Bind navigation keys from `unit.KeyCode` table + letter-key fallback |
| `match_key()` / `handle_slot()` | Map key events to `up` / `down` / `enter` / `esc` and act on them |
| `drain_keys()` | Drain the key-event queue, beep + handle each press |
| `derive_range()` | Compute host range from current IP / netmask |
| `probe_host()` | TCP open → HTTP GET `/health` → classify result |
| `fetch_detail()` | GET `/api/v1/device/status` → parse JSON → build display rows |
| `build_detail_lines()` / `flatten()` / `wrap_text()` | Turn nested JSON into grouped, screen-fitting rows |
| `draw_list_view()` | Render progress bar, 6-row scrollable result list, footer |
| `draw_detail_view()` | Render 7-row scrollable key-value detail, header, footer |
| `beep()` | Short confirmation tone on every key press |

---

## Build & Flash

### 1. Flash UIFlow 2 Firmware

Before running this project, the M5Cardputer **must** be running the **UIFlow 2** firmware (not bare MicroPython).

1. Download **M5Burner** from [m5stack.com](https://m5stack.com/software/)
2. Connect the Cardputer via USB
3. Select **M5Cardputer** as target
4. Flash the **UIFlow 2** firmware image

### 2. Deploy Source

After flashing, use the [**AI-Flow Editor**](https://aiflow.m5stack.com/) to deploy `src/main.py` to the device:

- Open the project in the AI-Flow web editor
- Upload / paste `main.py` into the project
- Click **Flash** to transfer to the Cardputer over USB

The app auto-starts on boot (UIFlow 2 entry-point behaviour).

---

## Configuration

All tunables live at the top of `src/main.py`:

```python
PORT = 8001            # target API port
SCAN_PATH = "/health"               # sweep probe endpoint
DETAIL_PATH = "/api/v1/device/status"  # detail view endpoint
SCAN_HEADERS = {"Content-Type": "application/json"}
DETAIL_HEADERS = {"accept": "application/json"}
PROBE_TIMEOUT = 0.25   # seconds per TCP connect (250 ms)
HTTP_TIMEOUT = 3       # seconds per HTTP call
MAX_HOSTS = 512        # safety cap for wide netmasks (/16 and friends)
WIFI_SSID = ""         # empty = use WiFi saved in the UIFlow launcher
WIFI_PASS = ""
```

If `WIFI_SSID` is empty, the Cardputer connects to whatever network is saved in the **UIFlow 2 Launcher** on the device.

---

## Controls

| Input | LIST view | DETAIL view | Waiting |
|---|---|---|---|
| **W / K / ↑** | Move cursor up | Scroll up | — |
| **S / J / ↓** | Move cursor down | Scroll down | — |
| **Enter / W** | Open selected host | — | — |
| **Q / Esc** | — | Back to LIST | — |
| **BtnA** (side) | Stop scan / rescan | — | Retry WiFi |

Unmapped keys emit a low 700 Hz blip; mapped keys and BtnA emit higher tones (1200–3200 Hz). Every press is logged to the serial console as `KEY str=<s> int=<n>`.

---

## Requirements

- M5Stack **M5Cardputer** (or M5Cardputer (2023))
- **UIFlow 2** firmware (flashed via M5Burner)
- A WiFi network the Cardputer can join
- Target services listening on port `8001` with:
  - `GET /health` — for the subnet sweep
  - `GET /api/v1/device/status` — for the detail view (returns JSON)
