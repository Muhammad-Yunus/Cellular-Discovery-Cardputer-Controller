# Cellular Discovery Cardputer Controller

Subnet scanner for the M5Cardputer. Sweeps every host on the local LAN and calls the `/health` API (port `8001`) on each one, rendering live results on the device's 240×135 display.

Built for **M5Cardputer** running **UIFlow 2 / MicroPython**.

---

## Application Architecture

```
┌──────────────────────────────────────────────────────┐
│  M5Cardputer (UIFlow 2 / MicroPython)               │
│                                                      │
│  ┌──────────────────────────────────────────────┐     │
│  │  Main Loop                                   │     │
│  │                                              │     │
│  │  1. WiFi connect (saved / hard-coded SSID)  │     │
│  │  2. Derive host range from ifconfig()        │     │
│  │  3. Sweep: one host per loop pass            │     │
│  │     ├── TCP probe on :8001 (250 ms budget)  │     │
│  │     └── HTTP GET /health (2 s budget)       │     │
│  │  4. Render results page (5 rows, scroll)    │     │
│  │  5. BtnA → stop / restart scan              │     │
│  └──────────────────────────────────────────────┘     │
│                                                      │
│  ┌────────────┐  ┌───────────┐  ┌──────────────┐    │
│  │ M5.Lcd     │  │ network   │  │ requests2    │    │
│  │ (display)  │  │ (WiFi)    │  │ (HTTP GET)   │    │
│  └────────────┘  └───────────┘  └──────────────┘    │
└──────────────────────────────────────────────────────┘
```

- **No hard-coded IP ranges** — the sweep range is derived at runtime from the device's own `ifconfig()` (IP + netmask), so the same program works on `192.168.1.x`, `10.0.0.x`, etc.
- **Two-phase probe** — a fast TCP connect (250 ms) gates the full HTTP call, keeping the sweep responsive.
- **One host per loop pass** — the UI stays alive and BtnA can interrupt at any time.

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
| `setup()` | Initialize M5, WiFi, matrix keyboard |
| `loop()` | Main event loop: state machine + one scan step per pass |
| `derive_range()` | Compute host range from current IP / netmask |
| `probe_host()` | TCP open → HTTP GET → classify result |
| `draw_dynamic()` | Render progress bar, status line, result list, footer |
| `on_key()` | Matrix-keyboard input (any key = rescan) |

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
PORT = 8001          # target API port
PATH = "/health"     # endpoint to call
PROBE_TIMEOUT = 0.25 # TCP connect budget (seconds)
HTTP_TIMEOUT = 2     # HTTP call budget (seconds)
MAX_HOSTS = 512      # safety cap for wide subnets
WIFI_SSID = ""       # leave empty to use WiFi saved in UIFlow launcher
WIFI_PASS = ""
```

If `WIFI_SSID` is empty, the Cardputer connects to whatever network is saved in the **UIFlow 2 Launcher** on the device.

---

## Requirements

- M5Stack **M5Cardputer** (or M5Cardputer (2023))
- **UIFlow 2** firmware (flashed via M5Burner)
- A WiFi network the Cardputer can join
- Target services listening on port `8001` with a `GET /health` endpoint
