# Tek Creations DCS Panels — Technical Reference

This document captures everything learned about Tek Creations cockpit panels:
how they enumerate on USB, how to open them, how they communicate, and how to
send commands back to them. Written for agents and developers working with
`dcs_viewer.py` or building new tools on top of these devices.

---

## 1. What They Are

Tek Creations panels are physical cockpit replicas (F/A-18C UFC, gear lever, ECM
panel, etc.) that communicate with a PC over USB. They run either:

- **DCS mode** — firmware based on DCS-BIOS; the panel connects as a USB CDC
  serial device and sends button events as text lines (`CONTROL_NAME VALUE\n`).
  This is the default for most panels.
- **HID mode** — firmware presents the panel as a USB HID game-controller.
  Button events arrive as binary interrupt reports. A few panels (notably the
  Gear Lever) **only** expose buttons via HID even though they also have a
  serial interface.

Some panels (composite devices) expose **both** a CDC serial interface and a
HID interface simultaneously.

---

## 2. USB Identifiers

All Tek Creations panels share **Vendor ID `0x16c0`** (VOTI / Van Ooijen
Technische Informatica — a shared VID used by many open-source firmware
projects).

| Product ID | Panel name |
|---|---|
| `0x28dc` | Tek F18 Right Panel |
| `0x33dc` | Tek F18 Left |
| `0x33dd` | Tek F18 Right |
| `0x33de` | Tek F18 Centre |
| `0x33df` | Tek F16 Left |
| `0x58dc` | Tek F18 ECM Control |
| `0x65dc` | Tek KY58 Control |
| `0x70dc` | Tek F18 Gear Lever |
| `0x70dd` | Tek F18 Throttle |

Use `pyusb` to enumerate all devices and filter by `idVendor == 0x16c0`:

```python
import usb.core
tek_panels = list(usb.core.find(find_all=True, idVendor=0x16c0))
```

---

## 3. Operating Modes in Detail

### 3a. DCS Mode (serial / DCS-BIOS firmware)

The panel appears as a USB CDC device. On macOS it shows up as:

```
/dev/cu.usbmodem<number>
```

**Detection via pyserial:**

```python
import serial.tools.list_ports

DCSBIOS_VIDS = {0x16c0, 0x303a, 0x2341}   # Tek + ESP32 + Arduino

ports = serial.tools.list_ports.comports()
dcs_ports = [
    pt for pt in ports
    if pt.vid in DCSBIOS_VIDS
    or "usbmodem" in pt.device.lower()
    or "usbserial" in pt.device.lower()
]
```

**Opening the serial port:**

```python
import serial

ser = serial.Serial(
    port="/dev/cu.usbmodem5",
    baudrate=250000,       # DCS-BIOS / Tek panels always use 250 000 baud
    timeout=0.1,
    dsrdtr=False,
    rtscts=False,
)
ser.dtr = False   # do NOT assert DTR — it resets ESP32-based panels
ser.rts = False

# Send the DCS-BIOS sync preamble; this primes the panel to accept
# binary output frames (cockpit state from DCS / our BiosWriter).
ser.write(bytes([0x55, 0x55, 0x55, 0x55]))
ser.flush()
```

> **Baud rate**: Always **250 000** for Tek/DCS-BIOS panels.
> Plain Arduino projects default to 115 200.
> Do NOT use 9600, 57600, or any other rate.

**Reading events:**

```python
while True:
    raw = ser.readline()          # blocks up to `timeout` seconds
    if not raw:
        continue
    line = raw.decode("utf-8", errors="replace").strip()
    parts = line.split(None, 1)
    if len(parts) == 2:
        control_name, value = parts
        # e.g. "UFC_COMM1_PULL 1"  or  "GEAR_HANDLE 0"
```

**Text protocol format** (`panel → PC`):

```
CONTROL_NAME VALUE\n
```

- `VALUE = "1"` → button/switch PRESSED or ON
- `VALUE = "0"` → button/switch RELEASED or OFF
- `VALUE = "INC"` / `"DEC"` → rotary encoder step
- `VALUE = <integer 0–65535>` → potentiometer or multi-position selector

Each line is self-contained; no framing or checksums. Malformed lines (no
space, empty) are safe to discard.

**Potentiometer spam**: Pots send a new value every ~5 ms while moving.
Deduplicate by tracking `last_value[control_name]` and skipping unchanged values.

---

### 3b. HID Mode (game-controller firmware)

The panel appears as a USB HID device. `hidapi` is the primary access library
on macOS.

**Detection:**

```python
import hid
entries = hid.enumerate(0x16c0, 0)   # 0 = any PID
# Each entry: {'vendor_id', 'product_id', 'product_string', 'path', ...}
```

**Opening by path (required when multiple identical panels are connected):**

```python
h = hid.device()
h.open_path(entry["path"])   # use path, NOT open(vid, pid) — that opens the first match
h.set_nonblocking(0)          # blocking reads
```

**Reading reports:**

```python
REPORT_SIZE = 64   # bytes; Tek panels all use 64-byte reports

prev = None
report_id = None

while True:
    data = h.read(REPORT_SIZE, timeout_ms=1000)
    if not data:
        continue
    data = bytes(data)

    # Detect Report ID prefix (constant non-zero first byte)
    if report_id is None and data[0] != 0:
        report_id = data[0]

    payload_slice = slice(1, None) if report_id else slice(None)
    payload = data[payload_slice]

    if prev is None:
        prev = data
        continue
    if data == prev:
        continue   # no change

    prev_payload = prev[payload_slice]
    for i, (old_b, new_b) in enumerate(zip(prev_payload, payload)):
        if old_b == new_b:
            continue
        diff = old_b ^ new_b
        for bit in range(8):
            if diff & (1 << bit):
                new_val = (new_b >> bit) & 1
                btn_num = i * 8 + bit + 1
                state = "PRESSED" if new_val else "RELEASED"
                print(f"Button {btn_num}  {state}")

    prev = data
```

**Report ID**: Some panels (ECM Control seen with `0x01`) prepend a constant
Report ID byte to every report. Detect it by checking if the first byte is
always non-zero; if so, skip it before bit-diffing.

**Button numbering**: `Button N = byte_index × 8 + bit_position + 1`
(1-based, bit 0 of byte 0 = Button 1).

---

## 4. The SET_CONFIGURATION STALL Bug

Several panels (notably **Tek F18 Right Panel**, PID `0x28dc`) have a firmware
bug where they STALL the host's `SET_CONFIGURATION` USB request.

**Symptom**: `pyusb` can see the device (`usb.core.find` returns it), but
`hid.enumerate()` returns nothing and no serial port appears in
`serial.tools.list_ports.comports()`.

**Fix — USB bus reset**:

```python
import usb.core, hid, time

def reset_and_wait(dev, timeout_sec=3.2) -> list:
    """Reset device and wait for IOHIDFamily to claim it."""
    vid, pid = dev.idVendor, dev.idProduct
    try:
        dev.reset()
    except Exception:
        pass   # "Entity not found" is normal — the reset still fires

    for _ in range(8):
        time.sleep(0.4)
        entries = hid.enumerate(vid, pid)
        if entries:
            return entries
    return []
```

Call this at startup for any Tek VID device not already visible as HID or serial.
On success, IOHIDFamily claims the interface and the device is usable.

---

## 5. Composite Devices (CDC + HID)

The **Gear Lever** (PID `0x70dc`) is composite: it exposes both a CDC serial
interface AND a HID interface.

- The **serial** interface is real and openable, but sends no button data.
- All button events arrive via **HID**.

Rule of thumb: if `--hid-info` reports ≥ 32 buttons, use HID mode.
If it reports ≤ 4 buttons, the panel is in DCS mode — use serial.

The `panel_names.json` file (see §8) records `"mode": "hid"` for such panels
so `run_all()` knows to skip their serial port and open the HID path instead.

---

## 6. Sending Output to Panels (Binary Protocol)

DCS-BIOS also defines an **output protocol**: the PC sends cockpit state
(indicator lights, display text, switch positions) to the panel firmware.
This allows driving LEDs, 7-segment displays, etc. on the panel.

### Protocol format (PC → panel, over serial)

```
[0x55 0x55 0x55 0x55]         4-byte sync / start-of-frame
[address  : uint16 LE]         chunk: starting address
[count    : uint16 LE]         number of data bytes that follow (always 2 for one word)
[data     : uint16 LE]         16-bit value at that address
... repeat for more chunks ...
[0xFFFE   : uint16 LE]         end-of-frame sentinel (no count/data follows)
```

**Python construction:**

```python
import struct

SYNC = b'\x55\x55\x55\x55'
EOF  = struct.pack('<H', 0xFFFE)

def build_frame(*updates):
    """
    updates: list of (address: int, word_value: int)
    """
    out = bytearray(SYNC)
    for addr, word in updates:
        out += struct.pack('<HHH', addr, 2, word)   # addr, count=2, data
    out += EOF
    return bytes(out)

# Example: turn on MASTER CAUTION (address=0x740C, mask=0x0200, shift=9)
frame = build_frame((0x740C, 0x0200))
ser.write(frame)
ser.flush()
```

### Address / mask / shift values

Each cockpit output is defined in the DCS-BIOS aircraft JSON files
(see §7). The relevant fields per output entry:

```json
{
  "address": 29708,    // decimal; the 16-bit word address
  "mask":     256,     // bitmask within that word
  "shift_by": 8,       // right-shift to recover the value: (word & mask) >> shift_by
  "max_value": 1       // 1 = binary on/off; higher = multi-state
}
```

**Shadow register**: Multiple controls can share the same 16-bit address word.
Always maintain a per-address shadow dict and merge changes before writing:

```python
shadow = {}   # {address: uint16}

def set_control(address, mask, shift_by, value):
    current = shadow.get(address, 0)
    new_word = (current & (~mask & 0xFFFF)) | ((value << shift_by) & mask)
    shadow[address] = new_word
    return new_word
```

### Thread safety

The reader thread continuously calls `ser.readline()`. The writer sends frames
between reads. Use a **write queue**: the reader thread drains it between each
`readline()` call. This avoids opening the same serial port twice and avoids
byte-level interleaving of read/write.

```python
write_q = queue.Queue()

# In reader thread:
while True:
    while True:                          # drain write queue first
        try:
            frame = write_q.get_nowait()
            ser.write(frame)
            ser.flush()
        except queue.Empty:
            break
    raw = ser.readline()                 # then read
    ...

# In main / command thread:
write_q.put(build_frame((address, new_word)))
```

---

## 7. DCS-BIOS Aircraft Control Definitions

The full set of control names, descriptions, categories, and output addresses
for every aircraft is available as JSON files hosted on GitHub by the
`dcs-bios` organisation:

| Aircraft | Repository | JSON filename |
|---|---|---|
| F/A-18C Hornet | `dcs-bios/module-fa-18c-hornet` | `FA-18C_hornet.json` |
| A-10C Warthog | `dcs-bios/module-a-10c` | `A-10C.json` |
| F-16C Viper | `dcs-bios/module-f-16c-50` | `F-16C_50.json` |
| F-14B Tomcat | `dcs-bios/module-f-14b` | `F-14B.json` |

**Raw URL pattern:**
```
https://raw.githubusercontent.com/dcs-bios/module-<name>/master/<File>.json
```

**JSON structure:**

```json
{
  "Category Name": {
    "CONTROL_IDENTIFIER": {
      "category":     "Up Front Controller (UFC)",
      "description":  "COMM 1 Channel Selector Knob Pull",
      "control_type": "selector",
      "inputs": [
        { "interface": "set_state", "max_value": 1 }
      ],
      "outputs": [
        {
          "type":      "integer",
          "address":   29716,
          "mask":      256,
          "shift_by":  8,
          "max_value": 1
        }
      ]
    }
  }
}
```

- **inputs** = commands the panel sends *to* DCS (button presses → `CONTROL_NAME VALUE\n`)
- **outputs** = cockpit state DCS sends *to* the panel (LED/display → binary protocol)

**macOS SSL note**: The python.org Python installer does not include macOS root
certificates. `urllib.request.urlopen` will raise
`[SSL: CERTIFICATE_VERIFY_FAILED]`. Workaround:

```python
import ssl, urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
with urllib.request.urlopen(url, context=ctx, timeout=15) as r:
    data = r.read()
```

Permanent fix: run `/Applications/Python 3.x/Install Certificates.command`.

---

## 8. Panel Name Aliases (`panel_names.json`)

Panels are identified by their USB **location** (physical hub slot path, e.g.
`1-1.4.3`), not serial number. Tek panels often share the same serial number
(`SN-0006`), making serial numbers useless for identification.

```json
{
  "1-1.3":   { "name": "Gear Lever",  "mode": "hid" },
  "1-1.4.3": { "name": "F18 LIP",     "mode": "serial" },
  "1-1.4.4": { "name": "KY58 Control","mode": "serial" }
}
```

- `"mode": "serial"` (default) — panel is read via DCS-BIOS serial protocol
- `"mode": "hid"` — panel is read via HID (e.g. Gear Lever)

Retrieve the location via `pyserial`:

```python
import serial.tools.list_ports
for pt in serial.tools.list_ports.comports():
    print(pt.device, pt.location, pt.description, pt.vid, pt.pid)
```

`pt.location` returns a string like `"1-1.4.3"` that is stable as long as the
panel is plugged into the same physical USB port.

---

## 9. macOS-Specific Notes

### Chrome / WebUSB

Chrome holds USB devices via WebUSB and blocks `hidapi` and `pyusb` access.
**Quit Chrome completely (⌘Q)** before running any panel tool.

### IOHIDFamily driver

macOS's HID stack (`IOHIDFamily`) must claim a HID device's interface before
`hidapi` can open it. If a panel has the SET_CONFIGURATION stall bug (§4),
IOHIDFamily never loads. The USB reset workaround (§4) forces
re-enumeration and usually succeeds.

### pyusb backend

`pyusb` on macOS uses the `libusb` backend. Install via:

```bash
brew install libusb
pip3 install pyusb
```

### Filtering system HID devices

When enumerating all HID devices (to build a picker), filter out macOS
internal/Apple devices to avoid cluttering the list:

```python
SKIP_VIDS        = {0x0000, 0x05AC, 0x004C}   # Apple, internal
SKIP_USAGE_PAGES = {0x0C, 0x0D, 0x0F}          # Consumer, Digitizer, PID

for entry in hid.enumerate(0, 0):
    if entry["vendor_id"] in SKIP_VIDS:
        continue
    if entry["usage_page"] in SKIP_USAGE_PAGES:
        continue
    # ... process panel entry
```

---

## 10. Required Python Packages

```
pyserial>=3.5     # serial port access + port listing
pyusb>=1.2.1      # USB enumeration + bus reset
hidapi>=0.14.0    # HID device access (wraps libhidapi)
```

`libusb` must be installed at the OS level:

```bash
brew install libusb
```

---

## 11. Quick-Start Checklist

1. Quit Chrome (⌘Q).
2. Plug in all panels.
3. Run `python3 dcs_viewer.py --list-devices` to confirm panels appear as
   `VID=0x16c0`.
4. Run `python3 dcs_viewer.py` (all-panels mode). The tool auto-detects serial
   ports, loads DCS-BIOS definitions, and starts reading all panels.
5. For a panel that shows nothing: run `python3 dcs_viewer.py --hid-info` to
   inspect its HID report descriptor. ≥ 32 buttons → use HID mode; ≤ 4 buttons
   → use DCS serial mode.
6. Assign friendly names and modes: `python3 dcs_viewer.py --serial --name`.
7. Send an output command: `python3 dcs_viewer.py --send MASTER_CAUTION_LT 1`.

---

## 12. Known Panel Quirks

| Panel | Issue | Solution |
|---|---|---|
| F18 Right Panel (`0x28dc`) | SET_CONFIGURATION STALL — invisible to HID and serial | Auto USB reset on startup |
| Gear Lever (`0x70dc`) | Composite CDC+HID; serial has no button data | Set `mode: hid` in `panel_names.json` |
| ECM Control (`0x58dc`) | HID exposes only 1 button; all others via serial DCS mode | Use serial mode |
| Multiple identical panels | All report `SN-0006` — serial number useless | Use `pt.location` (USB slot) as key |
| Potentiometers | Send continuous value stream while moving | Deduplicate: skip if same value as last |
| Any panel | Chrome holds the USB device via WebUSB | Quit Chrome before running tool |
