# DCS Panel Viewer

Two tools in one repo:

1. **Cockpit GUI** (`cockpit_gui.py`) — graphical FA-18C cockpit viewer with live DCS-BIOS overlays
2. **Terminal viewer** (`dcs_viewer.py`) — lightweight CLI tool for USB HID / serial panels

---

## Cockpit GUI — Quick Start

### Requirements

```bash
pip3 install -r requirements.txt
```

### First-time setup (3 steps, done once)

**Step 1 — Position the panel cutouts on the cockpit map**

```bash
python3 layout_wizard.py
```

- The full cockpit image opens with your 16 panel cutouts stacked on the left edge.
- Drag each panel to its correct position on the image.
- Scroll wheel to zoom, middle-drag to pan.
- Click **Save Layout** when done → saves to `config/panel_layout.json`.

**Step 2 — Review the category mapping** *(optional)*

Open `config/panel_categories.json` to verify which DCS-BIOS control categories are assigned to each panel image. The file is pre-filled; adjust if needed.

**Step 3 — Position controls on each panel** *(do this per panel, inside the app)*

- Launch the main app: `python3 cockpit_gui.py`
- Click any panel on the cockpit map.
- Click **Edit Positions** in the panel toolbar.
- Drag the control dots (one per DCS-BIOS control) to their correct locations on the panel image.
- Click **Save Positions** → saves to `config/control_positions.json`.

### Running the GUI

```bash
python3 cockpit_gui.py
```

- Click any panel region on the cockpit map to open that panel.
- Click **Connect DCS-BIOS** (top-right) to receive live data:
  - **UDP** (default) — DCS-BIOS running on the same PC, no extra config needed.
  - **Serial** — ESP32/Arduino panel over USB serial.
- Controls update in real time: LEDs light up, toggles show position, dials track value.
- Click an interactive control to send a command back to DCS.

---

Supports two device types:
- **Tek Creations F18 panels** (and similar) — USB HID mode (default)
- **ESP32 / Arduino panels** running DCS-BIOS text protocol — Serial mode

---

## Requirements

- Python 3.10+
- [pyusb](https://pypi.org/project/pyusb/) — USB HID access
- [pyserial](https://pypi.org/project/pyserial/) — Serial mode only

## Setup

```bash
pip3 install -r requirements.txt
```

---

## Tek Creations Panels (HID mode)

### ⚠️ Critical: Quit Chrome first

The Tek Creations panel communicates via **USB HID** with 64-byte interrupt reports.  
Chrome (WebUSB) grabs exclusive access to the device when open.  
**Quit Chrome completely (`⌘Q`) before running this tool**, otherwise no data arrives.

### Basic usage

```bash
# Decoded button presses (default — Tek F18 Left, VID=0x16c0 PID=0x33dc):
python3 dcs_viewer.py

# Raw hex dump of every report — use this first to learn the data format:
python3 dcs_viewer.py --raw

# With timestamps:
python3 dcs_viewer.py --timestamp

# Only print when something changes:
python3 dcs_viewer.py --changes-only

# Different panel (e.g. F18 Right):
python3 dcs_viewer.py --pid 0x33dd
```

### Learning the button layout

Run `--raw` mode and press each button one at a time:

```
  Baseline (initial state):
  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  ................
```

When you press a button, one bit in one byte will flip.  
Example — pressing the gear handle:

```
  byte[02] bit3   PRESSED
  byte[02] bit3   RELEASED
```

Once you've mapped all buttons, you can add a label map to the source (see `TEK_DEVICES` in `dcs_viewer.py`).

### Known Tek Creations VID/PID

| Panel | VID | PID |
|-------|-----|-----|
| F18 Left | `0x16c0` | `0x33dc` |
| F18 Right | `0x16c0` | `0x33dd` |
| F18 Centre | `0x16c0` | `0x33de` |
| F16 Left | `0x16c0` | `0x33df` |

---

## ESP32 / Arduino Serial Mode

For panels running the DCS-BIOS Arduino library over USB serial.

```bash
python3 dcs_viewer.py --serial
```

Auto-detects available serial ports and prompts for selection.

### Serial options

```bash
# Specify port explicitly (DCS-BIOS default baud is 250000):
python3 dcs_viewer.py --serial --port /dev/cu.usbserial-0001 --baud 250000

# Raw hex dump for diagnostics:
python3 dcs_viewer.py --serial --sniff --baud 250000

# With timestamps, changes only:
python3 dcs_viewer.py --serial --port /dev/cu.usbserial-0001 --timestamp --changes-only
```

> **Baud note:** The DCS-BIOS Arduino library defaults to **250000 baud**, not 115200.  
> For USB CDC boards (`/dev/cu.usbmodem*`) baud rate is ignored by the OS.

---

## All options

```
HID mode options:
  --vid VID             USB Vendor ID (default: 0x16c0)
  --pid PID             USB Product ID (default: 0x33dc)
  --ep EP               Interrupt IN endpoint (default: 0x83)
  --report-size N       HID report size in bytes (default: 64)
  --raw                 Dump every HID report as hex
  --list-devices        List all connected USB devices and exit

Serial mode options:
  --serial              Use serial DCS-BIOS text protocol
  --port PORT           Serial port
  --baud BAUD           Baud rate (default: 115200)
  --sniff               Raw hex dump of serial bytes
  --no-sync             Skip the DCS-BIOS 0x55×4 sync preamble
  --dtr                 Assert DTR (resets most Arduino/ESP32 boards)

Common options:
  --timestamp, -t       Show HH:MM:SS.mmm timestamp on each line
  --changes-only, -c    Only print on state changes
  --no-color            Disable ANSI colour output
  --list-devices, -l    List USB devices and exit
```

---

## Notes

- Press `Ctrl-C` to quit cleanly.
- If the device is not found, run `python3 dcs_viewer.py --list-devices` to confirm it's connected and find its VID/PID.
- On macOS, ESP32 boards appear as `/dev/cu.usbserial-*` or `/dev/cu.usbmodem*`.
- If no serial bytes appear, try `--baud 250000`. If still nothing, the firmware likely requires DCS to be actively running.
