#!/usr/bin/env python3
"""
DCS-BIOS / Tek Creations Panel Viewer
Reads button/switch events from all connected DCS panels simultaneously.
Supports serial DCS-BIOS mode (labelled controls) and USB HID mode.
"""

import argparse
import json
import os
import queue as _queue_mod
import struct
import subprocess
import sys
import threading
import time
import ssl
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Dependency checks ────────────────────────────────────────────────────────

try:
    import usb.core
    import usb.util
except ImportError:
    print("Error: pyusb is not installed.  Run: pip3 install pyusb")
    sys.exit(1)

try:
    import serial
    import serial.tools.list_ports
    _SERIAL_AVAILABLE = True
except ImportError:
    _SERIAL_AVAILABLE = False

try:
    import hid as _hid
    _HID_AVAILABLE = True
except ImportError:
    _HID_AVAILABLE = False


# ── ANSI colours ─────────────────────────────────────────────────────────────

ANSI_RESET   = "\033[0m"
ANSI_BOLD    = "\033[1m"
ANSI_DIM     = "\033[2m"
ANSI_GREEN   = "\033[92m"
ANSI_RED     = "\033[91m"
ANSI_YELLOW  = "\033[93m"
ANSI_CYAN    = "\033[96m"
ANSI_GRAY    = "\033[90m"
ANSI_MAGENTA = "\033[95m"
ANSI_ORANGE  = "\033[38;5;208m"

USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def colorize(text: str, *codes: str) -> str:
    if not USE_COLOR:
        return text
    return "".join(codes) + text + ANSI_RESET


# ── Known Tek Creations USB IDs ───────────────────────────────────────────────

TEK_DEVICES = {
    (0x16c0, 0x28dc): "Tek F18 Right Panel",
    (0x16c0, 0x33dc): "Tek F18 Left",
    (0x16c0, 0x33dd): "Tek F18 Right",
    (0x16c0, 0x33de): "Tek F18 Centre",
    (0x16c0, 0x33df): "Tek F16 Left",
    (0x16c0, 0x58dc): "Tek F18 ECM Control",
    (0x16c0, 0x65dc): "Tek KY58 Control",
    (0x16c0, 0x70dc): "Tek F18 Gear Lever",
    (0x16c0, 0x70dd): "Tek F18 Throttle",
}

DEFAULT_VID = 0       # 0 = any vendor; pass --vid 0x16c0 to restrict to Tek
DEFAULT_PID = None   # auto-detect

# ── Helpers ───────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hex_row(data: bytes, cols: int = 16) -> str:
    hex_part = " ".join(f"{b:02x}" for b in data)
    asc_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)
    return f"{hex_part:<{cols * 3}}  {colorize(asc_part, ANSI_CYAN)}"


# ── Panel name aliases ────────────────────────────────────────────────────────

# panel_names.json lives next to dcs_viewer.py so it stays with the project.
_NAMES_FILE = Path(__file__).with_name("panel_names.json")


def _load_aliases() -> dict:
    """Return {key: friendly_name} where key is serial_number or port_device."""
    if _NAMES_FILE.exists():
        try:
            return json.loads(_NAMES_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_aliases(aliases: dict) -> None:
    _NAMES_FILE.write_text(json.dumps(aliases, indent=2) + "\n")


def _port_stable_key(pt) -> str:
    """
    Return the most stable unique key for a serial port, in preference order:

    1. USB location (e.g. '1-1.4.3') — always unique per physical USB slot,
       stable as long as the panel stays in the same port.
    2. Port device name — fallback (less stable across replugs).
    """
    return pt.location or pt.device


def _get_panel_config(key: str, aliases: dict) -> dict:
    """Return {'name': str|None, 'mode': 'serial'|'hid'} for a location key."""
    val = aliases.get(key)
    if val is None:
        return {"name": None, "mode": "serial"}
    if isinstance(val, str):
        return {"name": val, "mode": "serial"}
    if isinstance(val, dict):
        return {"name": val.get("name"), "mode": val.get("mode", "serial")}
    return {"name": None, "mode": "serial"}


def _set_panel_config(key: str, name: "str | None", mode: str, aliases: dict) -> None:
    """Write panel name + mode back to the aliases dict (in-place)."""
    existing = aliases.get(key, {})
    if isinstance(existing, str):
        existing = {"name": existing}
    cfg: dict = dict(existing)
    if name is not None:
        cfg["name"] = name
    cfg["mode"] = mode
    aliases[key] = cfg


def _alias_for_port(pt, aliases: dict) -> "str | None":
    """Return the user-assigned name for a port, or None."""
    return _get_panel_config(_port_stable_key(pt), aliases)["name"]


def _mode_for_port(pt, aliases: dict) -> str:
    """Return 'serial' or 'hid' for a port (default: 'serial')."""
    return _get_panel_config(_port_stable_key(pt), aliases)["mode"]


def _port_display_name(pt, aliases: dict) -> str:
    """Human-readable label for a serial port entry."""
    alias = _alias_for_port(pt, aliases)
    if alias:
        return colorize(alias, ANSI_GREEN, ANSI_BOLD)
    return pt.description or pt.device


def _port_id_hint(pt, aliases: dict) -> str:
    """Short identifier + mode tag shown after the port name."""
    parts = []
    loc = pt.location
    if loc:
        parts.append(f"USB: {loc}")
    mode = _mode_for_port(pt, aliases)
    if mode == "hid":
        parts.append(colorize("mode: HID", ANSI_MAGENTA))
    return colorize(f"  [{', '.join(parts)}]", ANSI_DIM) if parts else ""


def assign_name_interactive(pt, aliases: dict) -> None:
    """Prompt the user to assign a friendly name and mode to a port."""
    key = _port_stable_key(pt)
    cfg = _get_panel_config(key, aliases)
    current_name = cfg["name"]
    current_mode = cfg["mode"]

    print(f"\n  Panel: {colorize(pt.description or pt.device, ANSI_CYAN)}  "
          f"[USB location: {colorize(key, ANSI_CYAN)}]")

    try:
        name_ans = input(
            f"  Friendly name "
            f"[{colorize(current_name, ANSI_GREEN) if current_name else 'none'}]"
            " (Enter to keep / '-' to clear): "
        ).strip()
        mode_ans = input(
            f"  Mode (serial/hid) "
            f"[{colorize(current_mode, ANSI_GREEN)}]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    new_name = current_name
    if name_ans == "-":
        new_name = None
    elif name_ans:
        new_name = name_ans

    new_mode = current_mode
    if mode_ans in ("serial", "hid"):
        new_mode = mode_ans

    if new_name is None and new_mode == "serial":
        aliases.pop(key, None)
    else:
        _set_panel_config(key, new_name, new_mode, aliases)
    _save_aliases(aliases)

    label = colorize(new_name or "(no alias)", ANSI_GREEN if new_name else ANSI_DIM)
    print(colorize(f"  Saved: name={label}  mode={new_mode}  → {_NAMES_FILE.name}", ANSI_DIM))


# ── HID device discovery ──────────────────────────────────────────────────────

def find_hid_device(vid: int, pid: int, retries: int = 5, delay: float = 0.4):
    """
    Return the first matching pyusb device, or None.

    Uses find_all=True to force a fresh bus scan on every call — the filtered
    variant has a race condition on macOS immediately after a replug.
    Retries a few times to handle the brief enumeration window after plugging in.
    """
    for attempt in range(retries):
        matches = list(usb.core.find(find_all=True, idVendor=vid, idProduct=pid))
        if matches:
            return matches[0]
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _reset_for_hid(dev) -> "list[dict]":
    """
    Some panels (e.g. Tek F18 Right Panel) have a firmware bug that STALLs
    the USB SET_CONFIGURATION request, so macOS never configures them and
    IOHIDFamily never claims their interface.

    A USB bus reset forces re-enumeration.  macOS then often succeeds in
    attaching IOHIDFamily on the fresh enumeration, making the device
    visible to hid.enumerate().

    Returns the list of hid.enumerate() entries for this VID/PID after the
    reset, or [] if IOHIDFamily still did not claim it.
    """
    vid, pid = dev.idVendor, dev.idProduct
    try:
        dev.reset()
    except Exception:
        pass  # "Entity not found" is normal — reset still fires on the bus

    # Wait for re-enumeration (IOHIDFamily needs time to match the interface)
    for _ in range(8):
        time.sleep(0.4)
        entries = _hid.enumerate(vid, pid) if _HID_AVAILABLE else []
        if entries:
            return entries
    return []


def list_hid_devices():
    """Print all connected USB devices (not just HID) for discovery."""
    devices = list(usb.core.find(find_all=True))
    if not devices:
        print("No USB devices found.")
        return
    print(colorize("Connected USB devices:", ANSI_BOLD))
    for dev in devices:
        try:
            mfr = dev.manufacturer or ""
            prd = dev.product or ""
        except Exception:
            mfr, prd = "", ""
        is_tek = dev.idVendor == TEK_VID
        known_name = TEK_DEVICES.get((dev.idVendor, dev.idProduct), "")
        if is_tek:
            label = known_name if known_name else (prd or f"0x{dev.idProduct:04x}")
            tag = f"  {colorize(f'← Tek panel  [{label}]', ANSI_GREEN)}"
        else:
            tag = ""
        print(f"  VID=0x{dev.idVendor:04x}  PID=0x{dev.idProduct:04x}  "
              f"{mfr!r:<20} {prd!r}{tag}")


# ── HID report descriptor parser ─────────────────────────────────────────────

def _parse_hid_descriptor(raw: bytes) -> dict:
    """
    Minimal HID Report Descriptor parser.

    Returns a dict with keys:
      has_report_id  : bool
      reports        : list of {id, buttons, axes, report_bytes}
      raw_bytes      : the original descriptor bytes
    """
    # Global state that persists across items
    g: dict = {
        "usage_page": 0, "log_min": 0, "log_max": 0,
        "report_size": 0, "report_count": 0, "report_id": 0,
    }
    # Per-report accumulation
    reports: dict = {}      # id → {"buttons": 0, "bits": 0}
    local_usages: list = []
    local_usage_min = local_usage_max = None
    has_report_id = False

    i = 0
    while i < len(raw):
        b = raw[i]
        tag  = (b >> 4) & 0xF
        typ  = (b >> 2) & 0x3
        size = b & 0x3
        if size == 3:
            size = 4
        val = int.from_bytes(raw[i+1 : i+1+size], "little")

        if typ == 1:   # Global
            if tag == 0x0: g["usage_page"] = val
            elif tag == 0x1: g["log_min"] = val
            elif tag == 0x2: g["log_max"] = val
            elif tag == 0x7: g["report_size"] = val
            elif tag == 0x8:
                g["report_id"] = val
                has_report_id = True
            elif tag == 0x9: g["report_count"] = val
        elif typ == 2:  # Local
            if tag == 0x0: local_usages.append(val)
            elif tag == 0x1: local_usage_min = val
            elif tag == 0x2: local_usage_max = val
        elif typ == 0:  # Main
            if tag == 0x8:  # Input
                rid = g["report_id"]
                if rid not in reports:
                    reports[rid] = {"buttons": 0, "axes": 0, "bits": 0}
                r = reports[rid]
                bits = g["report_count"] * g["report_size"]
                r["bits"] += bits
                if g["usage_page"] == 0x09:   # Button page
                    u_count = g["report_count"]
                    if local_usage_min is not None and local_usage_max is not None:
                        u_count = local_usage_max - local_usage_min + 1
                    r["buttons"] += u_count
                elif g["usage_page"] == 0x01 and g["report_size"] > 1:  # Generic Desktop axes
                    r["axes"] += g["report_count"]
            # Reset local state after any Main item
            local_usages.clear()
            local_usage_min = local_usage_max = None

        i += 1 + size

    result_reports = []
    for rid, r in sorted(reports.items()):
        result_reports.append({
            "id": rid,
            "buttons": r["buttons"],
            "axes": r["axes"],
            "report_bytes": (r["bits"] + 7) // 8 + (1 if has_report_id else 0),
        })

    return {
        "has_report_id": has_report_id,
        "reports": result_reports,
        "raw_bytes": raw,
    }


def hid_info(path: bytes, name: str) -> None:
    """Fetch and display the HID report descriptor for a device."""
    if not _HID_AVAILABLE:
        print(colorize("hidapi not available — cannot read report descriptor.", ANSI_RED))
        return

    h = _hid.device()
    try:
        h.open_path(path)
    except OSError as exc:
        print(colorize(f"  Could not open device: {exc}", ANSI_RED))
        return

    try:
        desc_raw = bytes(h.get_report_descriptor())
    except Exception as exc:
        print(colorize(f"  Could not read report descriptor: {exc}", ANSI_RED))
        h.close()
        return
    finally:
        h.close()

    info = _parse_hid_descriptor(desc_raw)

    print(colorize(f"\nHID Report Descriptor — {name}", ANSI_BOLD, ANSI_CYAN))
    print(colorize("─" * 60, ANSI_DIM))
    print(f"  Descriptor size : {len(desc_raw)} bytes")
    print(f"  Uses Report IDs : {'yes' if info['has_report_id'] else 'no'}")
    print()

    if not info["reports"]:
        print(colorize("  (no Input reports found in descriptor)", ANSI_YELLOW))
    else:
        print(f"  {'Report ID':<12}  {'Buttons':>8}  {'Axes':>6}  {'Bytes/report':>12}")
        print(colorize(f"  {'─'*12}  {'─'*8}  {'─'*6}  {'─'*12}", ANSI_DIM))
        total_btns = 0
        for r in info["reports"]:
            rid_s = f"0x{r['id']:02x}" if r["id"] else "(none)"
            print(f"  {rid_s:<12}  {r['buttons']:>8}  {r['axes']:>6}  {r['report_bytes']:>12}")
            total_btns += r["buttons"]
        print()
        print(f"  Total HID buttons defined : {colorize(str(total_btns), ANSI_GREEN, ANSI_BOLD)}")
        if total_btns <= 4:
            print(colorize(
                "\n  ⚠  Very few buttons are exposed via HID.\n"
                "     Most button data likely travels over the serial (CDC) interface.\n"
                "     Try:  python3 dcs_viewer.py --serial",
                ANSI_YELLOW,
            ))
        elif total_btns >= 32:
            print(colorize(
                f"\n  ✔  {total_btns} buttons are in the HID report — this panel should work\n"
                "     fully in HID mode.",
                ANSI_GREEN,
            ))

    print()
    print(colorize("  Raw descriptor hex:", ANSI_DIM))
    chunk = 16
    for off in range(0, len(desc_raw), chunk):
        row = desc_raw[off:off+chunk]
        print(f"  {off:04x}:  {' '.join(f'{b:02x}' for b in row)}")


# ── HID reader ────────────────────────────────────────────────────────────────

def _beep() -> None:
    """Play a short macOS system sound asynchronously (non-blocking)."""
    subprocess.Popen(
        ["afplay", "/System/Library/Sounds/Tink.aiff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def chrome_is_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-x", "Google Chrome"],
                                capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def _display_reports(
    read_fn,           # callable() → bytes | None (None = timeout)
    report_size: int,
    *,
    raw: bool,
    show_timestamp: bool,
    tone: bool,
) -> None:
    """Shared display loop: decodes bit-diffs or dumps raw hex.

    Auto-detects a leading Report ID byte (constant non-zero first byte) and
    skips it from the bit-diff so button numbers are labelled from byte[00]
    of actual payload rather than byte[01] of the wire data.
    """
    prev: "bytes | None" = None
    total = 0
    report_id: "int | None" = None   # detected once from first report

    print(colorize("  Waiting for reports… (press buttons, Ctrl-C to quit)\n", ANSI_DIM))

    try:
        while True:
            data = read_fn()
            if data is None:
                continue

            total += 1
            prefix = f"{colorize(ts(), ANSI_GRAY)}  " if show_timestamp else ""

            # Detect Report ID on the very first report.
            # A Report ID is a constant non-zero leading byte; all subsequent
            # reports from the same collection share the same value.
            if report_id is None and data and data[0] != 0:
                report_id = data[0]
                payload_slice = slice(1, None)
                print(colorize(
                    f"  Report ID detected: 0x{report_id:02x} "
                    f"— skipping first byte, showing payload only.\n",
                    ANSI_DIM,
                ))
            elif report_id is not None:
                payload_slice = slice(1, None)
            else:
                payload_slice = slice(None)

            display_data = data[payload_slice]

            if raw:
                if tone:
                    _beep()
                print(prefix + hex_row(display_data))
                prev = data
                continue

            if prev is None:
                print(colorize("  Baseline (initial state):", ANSI_DIM))
                print("  " + hex_row(display_data))
                prev = data
                continue

            if data == prev:
                continue

            prev_payload = prev[payload_slice]

            changed = False
            for i, (old_b, new_b) in enumerate(zip(prev_payload, display_data)):
                if old_b == new_b:
                    continue
                diff = old_b ^ new_b
                for bit in range(8):
                    if diff & (1 << bit):
                        new_val = (new_b >> bit) & 1
                        btn_num = i * 8 + bit + 1
                        name = f"Button {btn_num:<4} (byte[{i:02d}] bit{bit})"
                        state = (colorize("PRESSED ", ANSI_GREEN, ANSI_BOLD)
                                 if new_val else
                                 colorize("RELEASED", ANSI_RED, ANSI_BOLD))
                        print(f"{prefix}{colorize(f'{name:<30}', ANSI_BOLD)}  {state}")
                        changed = True

            if tone and changed:
                _beep()

            prev = data

    except KeyboardInterrupt:
        print(colorize(f"\n\nStopped. Total reports received: {total}", ANSI_DIM))


def open_and_read_via_hid(
    path: bytes,
    report_size: int,
    *,
    raw: bool,
    show_timestamp: bool,
    tone: bool,
) -> None:
    """Read HID reports via hidapi (IOHIDManager on macOS).

    Opens by IOKit service path (from hid.enumerate) so the exact physical
    device is always targeted — critical when multiple panels share a PID.
    """
    h = _hid.device()
    try:
        h.open_path(path)
    except OSError as exc:
        print(colorize(f"\n  ✖  hidapi could not open device: {exc}", ANSI_RED, ANSI_BOLD))
        sys.exit(1)

    h.set_nonblocking(0)
    print(colorize("  Opened via IOHIDManager.", ANSI_DIM))

    def read_fn():
        data = h.read(report_size, timeout_ms=1000)
        if not data:
            return None
        return bytes(data)

    try:
        _display_reports(read_fn, report_size, raw=raw, show_timestamp=show_timestamp, tone=tone)
    finally:
        h.close()


def open_and_read_via_pyusb(
    dev,
    ep_address: int,
    report_size: int,
    *,
    raw: bool,
    show_timestamp: bool,
    tone: bool,
) -> None:
    """Read HID reports via pyusb/libusb backend.
    Used as fallback when IOHIDFamily has NOT claimed the interface.
    """
    try:
        dev.set_configuration(1)
    except usb.core.USBError:
        pass  # May already be configured — proceed anyway

    backend = dev._ctx.backend
    dev._ctx.managed_open()
    handle = dev._ctx.handle

    try:
        backend.claim_interface(handle, 0)
    except usb.core.USBError as exc:
        if chrome_is_running():
            print(colorize(
                "\n  ✖  Chrome is running and holding the USB device.\n"
                "     Quit Chrome completely (⌘Q) then unplug/replug the panel.\n",
                ANSI_RED, ANSI_BOLD,
            ))
        else:
            print(colorize(
                f"\n  ✖  Cannot claim USB interface: {exc}\n"
                "     Unplug the panel → replug → run this tool again.\n",
                ANSI_RED, ANSI_BOLD,
            ))
        sys.exit(1)

    def read_fn():
        try:
            raw_data = backend.interrupt_read(handle, ep_address, report_size, timeout=1000)
            return bytes(raw_data)
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as exc:
            raise RuntimeError(str(exc))

    try:
        _display_reports(read_fn, report_size, raw=raw, show_timestamp=show_timestamp, tone=tone)
    except RuntimeError as exc:
        print(colorize(f"\n  USB error: {exc}", ANSI_RED))
    finally:
        try:
            backend.release_interface(handle, 0)
        except Exception:
            pass


# ── DCS-BIOS aircraft definitions ────────────────────────────────────────────

BIOS_MODULES: "dict[str, dict]" = {
    "fa18c": {
        "name":     "F/A-18C Hornet",
        "url":      "https://raw.githubusercontent.com/dcs-bios/module-fa-18c-hornet/master/FA-18C_hornet.json",
        "filename": "FA-18C_hornet.json",
    },
    "a10c": {
        "name":     "A-10C Warthog",
        "url":      "https://raw.githubusercontent.com/dcs-bios/module-a-10c/master/A-10C.json",
        "filename": "A-10C.json",
    },
    "f16c": {
        "name":     "F-16C Viper",
        "url":      "https://raw.githubusercontent.com/dcs-bios/module-f-16c-50/master/F-16C_50.json",
        "filename": "F-16C_50.json",
    },
    "f14b": {
        "name":     "F-14B Tomcat",
        "url":      "https://raw.githubusercontent.com/dcs-bios/module-f-14b/master/F-14B.json",
        "filename": "F-14B.json",
    },
}

_BIOS_CACHE_DIR = Path(__file__).with_name("bios_defs")


def _urlopen_bytes(url: str, timeout: int = 15) -> bytes:
    """
    Fetch URL bytes, falling back to unverified TLS if the system Python
    lacks macOS root certificates (common with python.org installers).

    To fix properly:  /Applications/Python\ 3.x/Install\ Certificates.command
    """
    def _fetch(ctx: "ssl.SSLContext | None") -> bytes:
        kw = {"timeout": timeout}
        if ctx is not None:
            kw["context"] = ctx  # type: ignore[assignment]
        with urllib.request.urlopen(url, **kw) as resp:  # type: ignore[arg-type]
            return resp.read()

    try:
        return _fetch(None)
    except Exception as exc:
        # urllib wraps ssl errors in URLError; check the string to be safe
        if "CERTIFICATE" not in str(exc).upper() and "SSL" not in str(exc).upper():
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print(colorize(
            "\n  ⚠  SSL cert verification failed — retrying without verification.\n"
            "     Fix permanently:  open /Applications/Python\\ 3.*/Install\\ Certificates.command",
            ANSI_YELLOW,
        ), flush=True)
        return _fetch(ctx)


class BiosDefs:
    """
    Loads a DCS-BIOS aircraft module JSON (downloaded + cached from GitHub).

    Provides lookup by control identifier so we can annotate incoming serial
    events with human-readable descriptions and drive outputs via BiosWriter.
    """

    def __init__(self, aircraft_key: str = "fa18c", silent: bool = False) -> None:
        self._index: dict = {}
        mod = BIOS_MODULES.get(aircraft_key)
        if not mod:
            keys = ", ".join(BIOS_MODULES)
            raise ValueError(f"Unknown aircraft '{aircraft_key}'. Available: {keys}")

        cache_path = _BIOS_CACHE_DIR / mod["filename"]

        if not cache_path.exists():
            _BIOS_CACHE_DIR.mkdir(exist_ok=True)
            if not silent:
                print(colorize(
                    f"  ↓  Downloading {mod['name']} definitions from GitHub…",
                    ANSI_DIM,
                ), end="", flush=True)
            try:
                data = _urlopen_bytes(mod["url"])
                cache_path.write_bytes(data)
                if not silent:
                    print(colorize(" done.", ANSI_DIM))
            except Exception as exc:
                if not silent:
                    print(colorize(f" failed: {exc}", ANSI_RED))
                return

        try:
            with open(cache_path, encoding="utf-8") as f:
                raw = json.load(f)
            # JSON structure: { "Category Name": { "IDENTIFIER": {...}, ... }, ... }
            for _category, controls in raw.items():
                for ident, ctrl in controls.items():
                    self._index[ident] = ctrl
        except Exception as exc:
            print(colorize(f"  ✖  Failed to parse {cache_path.name}: {exc}", ANSI_RED))

    def __bool__(self) -> bool:
        return bool(self._index)

    def __len__(self) -> int:
        return len(self._index)

    def get(self, identifier: str) -> "dict | None":
        return self._index.get(identifier)

    def description(self, identifier: str) -> str:
        ctrl = self._index.get(identifier)
        return ctrl.get("description", "") if ctrl else ""

    def category(self, identifier: str) -> str:
        ctrl = self._index.get(identifier)
        return ctrl.get("category", "") if ctrl else ""

    def search(self, query: str) -> "list[tuple[str, dict]]":
        q = query.lower()
        return [
            (ident, ctrl) for ident, ctrl in self._index.items()
            if q in ident.lower() or q in ctrl.get("description", "").lower()
        ]

    def output_controls(self) -> "list[tuple[str, dict]]":
        """Controls that have integer outputs — can be driven with BiosWriter."""
        return [
            (ident, ctrl) for ident, ctrl in sorted(self._index.items())
            if any(o["type"] == "integer" for o in ctrl.get("outputs", []))
        ]


# ── DCS-BIOS binary output writer ─────────────────────────────────────────────

class BiosWriter:
    """
    Sends DCS-BIOS binary export frames to one or more serial panels.

    Protocol (PC → panel):
        [0x55 0x55 0x55 0x55]         sync
        [address uint16 LE]            chunk start
        [count   uint16 LE]            bytes that follow (always 2 for one u16)
        [data    uint16 LE]            the 16-bit word at address
        ...                            more chunks
        [0xFFFE  uint16 LE]            end-of-frame sentinel

    Each output control in the JSON has: address, mask, shift_by, max_value.
    We maintain a shadow register per address so we can set/clear individual
    bits without corrupting other controls sharing the same 16-bit word.
    """

    _SYNC = b'\x55\x55\x55\x55'
    _EOF  = struct.pack('<H', 0xFFFE)   # end-of-frame sentinel address

    def __init__(self, defs: BiosDefs,
                 write_queues: "list[_queue_mod.Queue]") -> None:
        self._defs = defs
        self._write_qs = write_queues
        self._shadow: "dict[int, int]" = {}   # {address: uint16}

    @staticmethod
    def _build_frame(*chunks: "tuple[int, int]") -> bytes:
        """Encode sync + one or more (address, word) pairs + EOF sentinel."""
        out = bytearray(BiosWriter._SYNC)
        for addr, word in chunks:
            out += struct.pack('<HHH', addr, 2, word)   # addr, count=2, data
        out += BiosWriter._EOF
        return bytes(out)

    def send(self, control_name: str, value: int) -> "tuple[bool, str]":
        """
        Set a control's output value and push binary frames to all write queues.
        Returns (success: bool, human_readable_message: str).
        """
        ctrl = self._defs.get(control_name)
        if ctrl is None:
            # Case-insensitive fallback search
            matches = [
                (ident, c) for ident, c in self._defs.search(control_name)
                if ident.lower() == control_name.lower()
            ]
            if not matches:
                matches = self._defs.search(control_name)
            if len(matches) == 1:
                control_name, ctrl = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m[0] for m in matches[:5])
                return False, f"Ambiguous — did you mean: {names}?"
            else:
                return False, f"Unknown control: '{control_name}'"

        int_outputs = [o for o in ctrl.get("outputs", []) if o["type"] == "integer"]
        if not int_outputs:
            return False, f"'{control_name}' has no integer outputs (outputs-only LEDs/indicators)"

        chunks: "list[tuple[int,int]]" = []
        for out in int_outputs:
            addr     = out["address"]
            mask     = out["mask"]
            shift    = out["shift_by"]
            max_val  = out.get("max_value", 1)
            clamped  = max(0, min(int(value), max_val))
            current  = self._shadow.get(addr, 0)
            new_word = (current & (~mask & 0xFFFF)) | ((clamped << shift) & mask)
            self._shadow[addr] = new_word
            chunks.append((addr, new_word))

        frame = self._build_frame(*chunks)
        for q in self._write_qs:
            q.put(frame)

        desc = ctrl.get("description", control_name)
        return True, f"{control_name}  ({desc})  →  {value}"

    def toggle(self, control_name: str) -> "tuple[bool, str]":
        """Toggle a binary (max_value=1) control between 0 and 1."""
        ctrl = self._defs.get(control_name)
        if not ctrl:
            return False, f"Unknown control: '{control_name}'"
        outs = [o for o in ctrl.get("outputs", []) if o["type"] == "integer"]
        if not outs:
            return False, f"'{control_name}' has no integer outputs"
        out = outs[0]
        current_val = (self._shadow.get(out["address"], 0) & out["mask"]) >> out["shift_by"]
        return self.send(control_name, 0 if current_val else 1)


# ── Interactive command reader thread ─────────────────────────────────────────

def _cmd_reader_thread(writer: "BiosWriter", out_q: "_queue_mod.Queue") -> None:
    """
    Background thread: read commands from stdin and dispatch them.

    Syntax:
        CTRL_NAME 0|1      — set output to 0 or 1
        CTRL_NAME toggle   — toggle between 0 and 1
        search QUERY       — find controls by name / description
        help               — show command syntax
    """
    def post(msg: str) -> None:
        out_q.put(("info", "", msg))

    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        parts = line.split(None, 2)
        cmd = parts[0].upper()

        if cmd in ("HELP", "?", "H"):
            post(colorize(
                "  Commands:\n"
                "    CTRL_NAME 0|1       set an output (LED/indicator) on or off\n"
                "    CTRL_NAME toggle    toggle between 0 and 1\n"
                "    search QUERY        find controls by name or description\n"
                "    help                show this message",
                ANSI_DIM,
            ))

        elif cmd in ("SEARCH", "S") and len(parts) >= 2:
            results = writer._defs.search(parts[1])[:12]
            if results:
                for ident, ctrl in results:
                    desc = ctrl.get("description", "")
                    cat  = ctrl.get("category", "")
                    post(f"  {colorize(ident, ANSI_CYAN):<46}  {desc}  "
                         f"{colorize(f'[{cat}]', ANSI_DIM)}")
            else:
                post(colorize(f"  No controls match '{parts[1]}'", ANSI_DIM))

        elif len(parts) == 2:
            ctrl_name, val_str = parts[0], parts[1].lower()
            if val_str == "toggle":
                ok, msg = writer.toggle(ctrl_name)
            else:
                try:
                    val = int(val_str)
                except ValueError:
                    post(colorize("  Usage:  CTRL_NAME 0|1  |  CTRL_NAME toggle  |  search QUERY", ANSI_DIM))
                    continue
                ok, msg = writer.send(ctrl_name, val)
            icon  = "✔" if ok else "✖"
            color = ANSI_GREEN if ok else ANSI_RED
            post(colorize(f"  {icon}  {msg}", color))

        elif len(parts) == 1 and cmd not in ("SEARCH", "S"):
            post(colorize("  Usage:  CTRL_NAME 0|1  |  CTRL_NAME toggle  |  search QUERY  |  help", ANSI_DIM))


# ── Multi-panel background reader threads ────────────────────────────────────

# Events placed on the shared queue are tuples:
#   ("event",  panel_name, control, label, ANSI_color)
#   ("error",  panel_name, message)
#   ("connect",panel_name, detail)

def _serial_event_reader(panel_name: str, port: str, baud: int,
                          out_q: "_queue_mod.Queue",
                          write_q: "_queue_mod.Queue | None" = None) -> None:
    """
    Background thread: read one serial DCS-BIOS port, emit events.

    If write_q is provided, the thread also drains it and writes the binary
    frames to the serial port — this is how BiosWriter sends output commands
    without a second open() on the same port.
    """
    try:
        # Short timeout so the write queue is drained promptly (~100 ms latency)
        ser = serial.Serial(port, baud, timeout=0.1, dsrdtr=False, rtscts=False)
        ser.dtr = False
        ser.rts = False
        ser.write(bytes([0x55, 0x55, 0x55, 0x55]))
        ser.flush()
    except Exception as exc:
        out_q.put(("error", panel_name, str(exc)))
        return

    out_q.put(("connect", panel_name, f"{port} @ {baud}"))
    last: dict = {}
    with ser:
        while True:
            # ── Drain write queue (output commands from BiosWriter) ──────────
            if write_q is not None:
                while True:
                    try:
                        frame = write_q.get_nowait()
                        ser.write(frame)
                        ser.flush()
                    except _queue_mod.Empty:
                        break
                    except Exception:
                        pass

            # ── Read one line from the panel ─────────────────────────────────
            try:
                raw = ser.readline()
            except Exception as exc:
                out_q.put(("error", panel_name, str(exc)))
                break
            if not raw:
                continue
            parsed = parse_dcsbios_line(raw.decode("utf-8", errors="replace"))
            if parsed is None:
                continue
            control, value = parsed
            if last.get(control) == value:
                continue          # deduplicate — ignore pot spam
            last[control] = value
            lbl, color = interpret_value(value)
            out_q.put(("event", panel_name, control, lbl, color))


def _hid_event_reader(panel_name: str, hid_path: bytes,
                       report_size: int, out_q: "_queue_mod.Queue") -> None:
    """Background thread: read one HID panel, emit button events."""
    if not _HID_AVAILABLE:
        out_q.put(("error", panel_name, "hidapi not available"))
        return

    h = _hid.device()
    try:
        h.open_path(hid_path)
    except OSError as exc:
        out_q.put(("error", panel_name, str(exc)))
        return

    h.set_nonblocking(0)
    out_q.put(("connect", panel_name, "HID"))
    prev: "bytes | None" = None
    report_id: "int | None" = None

    try:
        while True:
            data = h.read(report_size, timeout_ms=1000)
            if not data:
                continue
            data = bytes(data)
            if report_id is None and data[0] != 0:
                report_id = data[0]
            sl = slice(1, None) if report_id else slice(None)
            payload = data[sl]

            if prev is None:
                prev = data
                continue
            if data == prev:
                continue

            prev_payload = prev[sl]
            for i, (ob, nb) in enumerate(zip(prev_payload, payload)):
                if ob == nb:
                    continue
                diff = ob ^ nb
                for bit in range(8):
                    if diff & (1 << bit):
                        new_val = (nb >> bit) & 1
                        btn = f"Button {i * 8 + bit + 1}"
                        lbl = "PRESSED" if new_val else "RELEASED"
                        color = ANSI_GREEN if new_val else ANSI_RED
                        out_q.put(("event", panel_name, btn, lbl, color))
            prev = data
    except Exception:
        pass
    finally:
        h.close()


# ── HID main flow ─────────────────────────────────────────────────────────────

TEK_VID = 0x16c0

# VIDs that represent macOS internal / Apple-only HID services.
# These are never cockpit panels, so skip them in the auto-picker.
# The user can still target them explicitly with --vid if needed.
_SKIP_VIDS = {
    0x0000,  # macOS virtual HID services (keyboard, trackpad, BTM…)
    0x05AC,  # Apple USB devices
    0x004C,  # Apple Bluetooth devices
}

# HID usage pages that are definitely NOT cockpit panels.
_SKIP_USAGE_PAGES = {0x0C, 0x0D, 0x0F}  # Consumer, Digitizer, PID/Force-feedback


def _build_hid_entries(vid: int, pid: "int | None") -> list:
    """
    Return a list of dicts describing every accessible input panel.

    Enumerates ALL HID devices (not just Tek VID) so panels with custom
    firmware or non-standard branding are still discoverable.  Mice,
    keyboards and consumer devices are silently skipped.

    Each dict has:
      name       – human-readable label
      vid, pid   – USB IDs
      hid_path   – bytes path for hid.open_path(), or None
      pyusb_dev  – pyusb Device object for fallback, or None
      is_tek     – True if this is a known Tek Creations device

    HID entries use the IOKit service path so even four identical ECM panels
    (same VID/PID) each get their own unique path and can be opened individually.
    """
    entries: list = []
    seen_paths: set = set()

    if _HID_AVAILABLE:
        for h in _hid.enumerate(0, 0):   # 0,0 → enumerate every HID device
            h_vid = h["vendor_id"]
            h_pid = h["product_id"]

            # Apply explicit VID/PID filters when the user passed them
            if vid != 0 and h_vid != vid:
                continue
            if pid is not None and h_pid != pid:
                continue

            # Skip Apple / macOS-internal devices (unless user asked for them)
            if vid == 0 and h_vid in _SKIP_VIDS:
                continue

            # Skip non-panel usage pages
            if h.get("usage_page", 0) in _SKIP_USAGE_PAGES:
                continue

            path = h["path"]
            if path in seen_paths:
                continue
            seen_paths.add(path)

            key = (h_vid, h_pid)
            name = (TEK_DEVICES.get(key)
                    or h.get("product_string")
                    or f"VID=0x{h_vid:04x} PID=0x{h_pid:04x}")
            entries.append({
                "name": name,
                "vid": h_vid,
                "pid": h_pid,
                "hid_path": path,
                "pyusb_dev": None,
                "is_tek": h_vid == TEK_VID,
            })

    # Sort: Tek devices first, then alphabetically by name
    entries.sort(key=lambda e: (0 if e["is_tek"] else 1, e["name"].lower()))

    # Add any pyusb-visible Tek devices that IOHIDFamily did NOT claim.
    # For panels with the SET_CONFIGURATION STALL bug (e.g. Tek F18 Right Panel),
    # a USB reset forces macOS to re-enumerate and IOHIDFamily usually succeeds
    # on the fresh attempt.
    hid_key_set = {(e["vid"], e["pid"]) for e in entries}
    search_vid = TEK_VID if vid == 0 else vid   # only pyusb-scan for Tek (or explicit VID)
    for dev in usb.core.find(find_all=True, idVendor=search_vid):
        if pid is not None and dev.idProduct != pid:
            continue
        key = (dev.idVendor, dev.idProduct)
        if key in hid_key_set:
            continue  # already covered by a HID entry
        try:
            name = dev.product or f"0x{dev.idProduct:04x}"
        except Exception:
            name = f"0x{dev.idProduct:04x}"

        dev_name = TEK_DEVICES.get(key) or name

        # Try a USB reset so IOHIDFamily can claim the interface after re-enumeration
        print(colorize(
            f"\n  ↻  {dev_name} not yet claimed by macOS — sending USB reset…",
            ANSI_YELLOW,
        ), flush=True)
        recovered = _reset_for_hid(dev)
        if recovered:
            for h in recovered:
                path = h["path"]
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                entries.append({
                    "name": dev_name,
                    "vid": h["vendor_id"],
                    "pid": h["product_id"],
                    "hid_path": path,
                    "pyusb_dev": None,
                    "is_tek": h["vendor_id"] == TEK_VID,
                })
            print(colorize("  ✔  IOHIDManager claimed it after reset.", ANSI_GREEN))
        else:
            # Still unclaimed — add as pyusb-only fallback entry
            entries.append({
                "name": dev_name,
                "vid": dev.idVendor,
                "pid": dev.idProduct,
                "hid_path": None,
                "pyusb_dev": dev,
                "is_tek": dev.idVendor == TEK_VID,
            })
            print(colorize(
                "  ✖  Reset did not help — will try pyusb fallback (may not work).",
                ANSI_RED,
            ))

    return entries


def run_hid(
    vid: int,
    pid: "int | None",
    ep: int,
    report_size: int,
    *,
    raw: bool,
    changes_only: bool,
    show_timestamp: bool,
    tone: bool,
    hid_info_only: bool,
    no_color: bool,
) -> None:
    global USE_COLOR
    if no_color:
        USE_COLOR = False

    print(colorize("\nDCS Panel Viewer  [HID mode]", ANSI_BOLD, ANSI_CYAN))
    print(colorize("─" * 60, ANSI_DIM))

    print(colorize("  Searching for device…", ANSI_DIM), end=" ", flush=True)

    entries = _build_hid_entries(vid, pid)

    if not entries:
        print()
        print(colorize(
            "\n  ✖  No input panels found.\n"
            "     • Make sure the panel is plugged in\n"
            "     • Try: python3 dcs_viewer.py --list-devices\n"
            "     • Or specify: python3 dcs_viewer.py --vid 0xXXXX --pid 0xYYYY\n",
            ANSI_RED,
        ))
        sys.exit(1)

    if len(entries) == 1:
        entry = entries[0]
        print(colorize("found.", ANSI_GREEN))
    else:
        print()
        print(colorize("\n  Multiple panels found — choose one:", ANSI_BOLD))
        for i, e in enumerate(entries):
            badge = colorize(" [Tek]", ANSI_GREEN) if e["is_tek"] else ""
            tag = colorize("  (no HID driver — may not work)", ANSI_YELLOW) if e["hid_path"] is None else ""
            print(f"    [{colorize(str(i+1), ANSI_CYAN)}] {e['name']}{badge}  "
                  f"(VID=0x{e['vid']:04x} PID=0x{e['pid']:04x}){tag}")
        print()
        while True:
            try:
                choice = input(f"Select panel [1–{len(entries)}]: ").strip()
                idx = int(choice) - 1
                if 0 <= idx < len(entries):
                    entry = entries[idx]
                    break
                print(f"  Enter 1–{len(entries)}.")
            except (ValueError, EOFError):
                print("  Invalid input.")

    selected_vid = entry["vid"]
    selected_pid = entry["pid"]
    dev_name = entry["name"]

    if hid_info_only:
        if entry["hid_path"]:
            hid_info(entry["hid_path"], dev_name)
        else:
            print(colorize(
                "\n  ✖  This device is not accessible via IOHIDManager.\n"
                "     Cannot read the report descriptor.\n",
                ANSI_RED,
            ))
        sys.exit(0)

    print(f"  Device        : {colorize(dev_name, ANSI_CYAN)}")
    print(f"  VID/PID       : 0x{selected_vid:04x} / 0x{selected_pid:04x}")
    print(f"  Endpoint      : 0x{ep:02x}")
    print(f"  Report size   : {report_size} bytes")
    print(f"  Mode          : {'raw hex' if raw else 'decoded (bit diff)'}")
    print(colorize("─" * 60, ANSI_DIM))

    if entry["hid_path"]:
        print(colorize("  Using IOHIDManager (hidapi).", ANSI_DIM))
        open_and_read_via_hid(
            entry["hid_path"], report_size,
            raw=raw, show_timestamp=show_timestamp, tone=tone,
        )
    else:
        pyusb_dev = entry["pyusb_dev"]
        if pyusb_dev is None:
            pyusb_dev = find_hid_device(selected_vid, selected_pid)
        if pyusb_dev is None:
            print(colorize(
                f"\n  ✖  Device VID=0x{selected_vid:04x} PID=0x{selected_pid:04x} not found.\n"
                "     Make sure the panel is plugged in.\n",
                ANSI_RED,
            ))
            sys.exit(1)
        print(colorize("  IOHIDManager did not find device — trying pyusb/libusb.", ANSI_DIM))
        open_and_read_via_pyusb(
            pyusb_dev, ep, report_size,
            raw=raw, show_timestamp=show_timestamp, tone=tone,
        )


# ── All-panels mode (default) ────────────────────────────────────────────────

DCS_SERIAL_VIDS = {0x16c0, 0x303a, 0x2341}


def run_all(aliases: dict, *, tone: bool, no_color: bool, show_timestamp: bool,
            aircraft: str = "fa18c") -> None:
    """
    Start a background reader thread for every connected DCS panel and print
    events as they arrive.

    Serial panels:  labelled  CONTROL_NAME  PRESSED / RELEASED / value
    HID panels:     numbered  Button N       PRESSED / RELEASED

    If DCS-BIOS definitions can be loaded (downloaded or cached), events are
    annotated with their human-readable description and an interactive command
    line is enabled for sending output commands (LEDs / indicators).
    """
    global USE_COLOR
    if no_color:
        USE_COLOR = False

    out_q: "_queue_mod.Queue" = _queue_mod.Queue()
    threads: list = []
    panel_names: list = []
    write_qs: "list[_queue_mod.Queue]" = []   # one per serial panel, for BiosWriter

    # ── Load DCS-BIOS definitions (best-effort) ────────────────────────────────
    try:
        defs: "BiosDefs | None" = BiosDefs(aircraft)
        if not defs:
            defs = None
    except Exception:
        defs = None

    # ── Serial panels ──────────────────────────────────────────────────────────
    if _SERIAL_AVAILABLE:
        all_ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

        # Auto-reset any panels visible on USB but not yet showing a serial port
        usb_dcs = [d for d in usb.core.find(find_all=True) if d.idVendor in DCS_SERIAL_VIDS]
        serial_vids_seen = {pt.vid for pt in all_ports if pt.vid}
        for d in usb_dcs:
            if d.idVendor not in serial_vids_seen:
                try:
                    name = d.product or f"0x{d.idProduct:04x}"
                except Exception:
                    name = f"0x{d.idProduct:04x}"
                print(colorize(
                    f"  ↻  {name} — sending USB reset to initialise CDC driver…",
                    ANSI_YELLOW,
                ), flush=True)
                _reset_for_hid(d)
        if any(d.idVendor not in serial_vids_seen for d in usb_dcs):
            all_ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

        dcs_ports = [
            pt for pt in all_ports
            if (pt.vid in DCS_SERIAL_VIDS)
            or "usbmodem" in pt.device.lower()
            or "usbserial" in pt.device.lower()
        ]
        for pt in dcs_ports:
            cfg = _get_panel_config(_port_stable_key(pt), aliases)
            if cfg["mode"] == "hid":
                continue   # handled below via HID path
            panel_name = cfg["name"] or pt.description or pt.device
            baud = 250000 if (pt.vid in DCS_SERIAL_VIDS) else 115200
            wq: "_queue_mod.Queue" = _queue_mod.Queue()
            write_qs.append(wq)
            t = threading.Thread(
                target=_serial_event_reader,
                args=(panel_name, pt.device, baud, out_q, wq),
                daemon=True,
            )
            threads.append(t)
            panel_names.append((colorize("serial", ANSI_CYAN), panel_name))

    # ── HID panels (explicitly configured or HID-only) ─────────────────────────
    if _HID_AVAILABLE:
        hid_entries = _build_hid_entries(TEK_VID, None)
        serial_descs: set = set()
        if _SERIAL_AVAILABLE:
            for pt in dcs_ports:  # type: ignore[possibly-undefined]
                if _get_panel_config(_port_stable_key(pt), aliases)["mode"] != "hid":
                    serial_descs.add((pt.description or "").lower())

        seen_hid_paths: set = set()
        for entry in hid_entries:
            if not entry["hid_path"]:
                continue
            if entry["hid_path"] in seen_hid_paths:
                continue

            desc_lower = entry["name"].lower()
            is_hid_only = desc_lower not in serial_descs
            has_hid_config = any(
                isinstance(v, dict) and v.get("mode") == "hid"
                and (v.get("name", "").lower() in desc_lower
                     or desc_lower in v.get("name", "").lower())
                for v in aliases.values()
            )
            if not (is_hid_only or has_hid_config):
                continue

            seen_hid_paths.add(entry["hid_path"])
            panel_name = entry["name"]
            t = threading.Thread(
                target=_hid_event_reader,
                args=(panel_name, entry["hid_path"], 64, out_q),
                daemon=True,
            )
            threads.append(t)
            panel_names.append((colorize("HID", ANSI_MAGENTA), panel_name))

    if not threads:
        print(colorize(
            "\n  ✖  No DCS panels found.\n"
            "     Make sure panels are plugged in.\n"
            "     Run:  python3 dcs_viewer.py --list-devices\n",
            ANSI_RED,
        ))
        return

    # ── Banner ─────────────────────────────────────────────────────────────────
    print(colorize("\nDCS Panel Viewer  [all-panels mode]", ANSI_BOLD, ANSI_CYAN))
    print(colorize("─" * 60, ANSI_DIM))
    name_w = max(len(n) for _, n in panel_names)
    for mode_tag, pname in panel_names:
        print(f"  {mode_tag}  {colorize(pname, ANSI_BOLD)}")
    if defs:
        mod_name = BIOS_MODULES.get(aircraft, {}).get("name", aircraft)
        print(colorize(f"  defs: {mod_name}  ({len(defs)} controls)", ANSI_DIM))
    print(colorize("─" * 60, ANSI_DIM))

    # ── Start writer + command thread (serial panels only) ─────────────────────
    writer: "BiosWriter | None" = None
    if defs and write_qs:
        writer = BiosWriter(defs, write_qs)
        cmd_t = threading.Thread(
            target=_cmd_reader_thread, args=(writer, out_q), daemon=True
        )
        cmd_t.start()
        print(colorize(
            "  Output commands enabled.  Type:  CTRL_NAME 0|1  |  search QUERY  |  help",
            ANSI_DIM,
        ))
    print(colorize("  Waiting for input… (Ctrl-C to quit)\n", ANSI_DIM))

    for t in threads:
        t.start()

    # ── Output loop ────────────────────────────────────────────────────────────
    name_w = max(name_w, 16)
    total = 0
    try:
        while True:
            try:
                msg = out_q.get(timeout=0.2)
            except _queue_mod.Empty:
                continue

            kind = msg[0]
            if kind == "connect":
                _, pname, detail = msg
                print(colorize(f"  ✔  {pname}  connected ({detail})", ANSI_DIM))
            elif kind == "error":
                _, pname, err = msg
                print(colorize(f"  ✖  {pname}: {err}", ANSI_RED))
            elif kind == "info":
                _, _, text = msg
                print(text)
            elif kind == "event":
                _, pname, control, lbl, color = msg
                total += 1
                if tone:
                    _beep()
                prefix    = f"{colorize(ts(), ANSI_GRAY)}  " if show_timestamp else ""
                panel_col = colorize(f"{pname:<{name_w}}", ANSI_CYAN, ANSI_BOLD)
                ctrl_col  = colorize(f"{control:<35}", ANSI_BOLD)
                state_col = colorize(lbl, color, ANSI_BOLD)
                desc_col  = (colorize(f"  {defs.description(control)}", ANSI_DIM)
                             if defs else "")
                print(f"{prefix}{panel_col}  {ctrl_col}  {state_col}{desc_col}")
    except KeyboardInterrupt:
        print(colorize(f"\n\nStopped. Total events: {total}", ANSI_DIM))


# ── Serial DCS-BIOS mode ──────────────────────────────────────────────────────

DCSBIOS_SYNC = bytes([0x55, 0x55, 0x55, 0x55])


def interpret_value(value: str) -> "tuple[str, str]":
    if value == "1":
        return "PRESSED", ANSI_GREEN
    if value == "0":
        return "RELEASED", ANSI_RED
    return value, ANSI_YELLOW


def parse_dcsbios_line(line: str) -> "tuple[str, str] | None":
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def run_serial(
    port: str,
    baud: int,
    *,
    show_timestamp: bool,
    changes_only: bool,
    sniff: bool,
    no_sync: bool,
    dtr: bool,
    tone: bool,
    no_color: bool,
) -> None:
    global USE_COLOR
    if no_color:
        USE_COLOR = False

    if not _SERIAL_AVAILABLE:
        print("pyserial not installed.  Run: pip3 install pyserial")
        sys.exit(1)

    mode_label = colorize("SNIFF (raw hex)", ANSI_MAGENTA) if sniff else colorize("text protocol", ANSI_GREEN)

    print(colorize("\nDCS Panel Viewer  [serial mode]", ANSI_BOLD, ANSI_CYAN))
    print(colorize("─" * 60, ANSI_DIM))
    print(f"  Port          : {colorize(port, ANSI_CYAN)}")
    print(f"  Baud          : {colorize(str(baud), ANSI_CYAN)}")
    print(f"  Mode          : {mode_label}")
    print(f"  DTR           : {'yes (may reset board)' if dtr else 'no'}")
    print(f"  Send sync     : {'no' if no_sync else 'yes (0x55×4)'}")
    print(colorize("─" * 60, ANSI_DIM))

    try:
        ser = serial.Serial(port, baud, timeout=1, dsrdtr=False, rtscts=False)
        ser.dtr = dtr
        ser.rts = False
        if dtr:
            time.sleep(0.5)
        if not no_sync:
            ser.write(DCSBIOS_SYNC)
            ser.flush()
            print(colorize("  Sent sync preamble.", ANSI_DIM))
        print(colorize("  Waiting for data… (Ctrl-C to quit)\n", ANSI_DIM))
    except serial.SerialException as exc:
        print(colorize(f"\nCould not open {port}: {exc}", ANSI_RED, ANSI_BOLD))
        sys.exit(1)

    with ser:
        if sniff:
            _serial_sniff(ser)
            return

        last_values: dict[str, str] = {}
        try:
            while True:
                try:
                    raw = ser.readline()
                except serial.SerialException as exc:
                    print(colorize(f"\nSerial error: {exc}", ANSI_RED))
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                parsed = parse_dcsbios_line(line)
                if parsed is None:
                    continue
                control, value = parsed
                if changes_only and last_values.get(control) == value:
                    continue
                last_values[control] = value
                label, color = interpret_value(value)
                prefix = f"{colorize(ts(), ANSI_GRAY)}  " if show_timestamp else ""
                print(f"{prefix}{colorize(f'{control:<40}', ANSI_BOLD)}  {colorize(label, color, ANSI_BOLD)}")
                if tone:
                    _beep()
        except KeyboardInterrupt:
            print(colorize("\n\nStopped.", ANSI_DIM))


def _serial_sniff(ser) -> None:
    print(colorize("  [SNIFF] Raw hex dump. Press Ctrl-C to stop.\n", ANSI_DIM))
    buf = bytearray()
    total = 0

    def flush():
        if buf:
            print("  " + hex_row(bytes(buf)))
            buf.clear()

    try:
        while True:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                flush()
                continue
            total += len(chunk)
            for b in chunk:
                buf.append(b)
                if b == ord('\n') or len(buf) >= 16:
                    flush()
    except KeyboardInterrupt:
        flush()
        print(colorize(f"\n\nSniff stopped. Total bytes: {total}", ANSI_DIM))


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dcs_viewer",
        description="View button/switch activity from DCS cockpit panels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Default behaviour (no flags)
────────────────────────────
  Reads ALL connected panels at the same time:
    • DCS-BIOS serial ports  → labelled control names
    • HID-only panels        → Button N  PRESSED / RELEASED

  Output format:
    Panel name              CONTROL_NAME           PRESSED

Panel firmware modes
────────────────────
  DCS mode  — panel runs DCS-BIOS firmware; serial text protocol.
              Baud rate is 250 000 for Tek/DCS-BIOS panels.
              This is the default for all serial-capable panels.

  HID mode  — panel acts as a USB game-controller.
              Use --name to mark a panel as HID mode, then it will
              be read via HID in the all-panels default run.

  Gear Lever tip: this panel has both serial AND HID interfaces, but
  button data only comes via HID.  Run --name and set mode=hid.

Naming panels
─────────────
  python3 dcs_viewer.py --serial --name
    → pick a port, assign friendly name and mode (serial/hid)
    → saved to panel_names.json, used by all modes

Examples:
  # Read ALL panels simultaneously (recommended):
  python3 dcs_viewer.py

  # Single serial panel (picker):
  python3 dcs_viewer.py --serial

  # Single HID panel (picker):
  python3 dcs_viewer.py --hid

  # Assign a name / set HID mode for a panel:
  python3 dcs_viewer.py --serial --name

  # Check HID button layout:
  python3 dcs_viewer.py --hid-info

  # List all connected USB devices:
  python3 dcs_viewer.py --list-devices
""",
    )

    # Mode selection
    mode_grp = p.add_argument_group("Mode (default: all panels simultaneously)")
    mode_grp.add_argument("--serial", action="store_true",
                          help="Single-panel serial/DCS-BIOS mode (picker → one port).")
    mode_grp.add_argument("--hid", action="store_true",
                          help="Single-panel HID mode (picker → one HID device).")

    # HID options
    hid = p.add_argument_group("HID mode options")
    hid.add_argument("--vid", type=lambda x: int(x, 0), default=DEFAULT_VID,
                     help="USB Vendor ID filter (default: 0 = any vendor)")
    hid.add_argument("--pid", type=lambda x: int(x, 0), default=DEFAULT_PID,
                     help="USB Product ID (default: show picker for all found panels)")
    hid.add_argument("--ep", type=lambda x: int(x, 0), default=0x83,
                     help="Interrupt IN endpoint address (default: 0x83)")
    hid.add_argument("--report-size", type=int, default=64,
                     help="HID report size in bytes (default: 64)")
    hid.add_argument("--raw", action="store_true",
                     help="Dump every raw HID report as hex instead of decoding bits.")
    hid.add_argument("--list-devices", "-l", action="store_true",
                     help="List all connected USB devices and exit.")
    hid.add_argument("--hid-info", "-i", action="store_true",
                     help="Show the HID report descriptor (button/axis layout) and exit.")

    # Serial options
    ser_grp = p.add_argument_group("Serial mode options (--serial)")
    ser_grp.add_argument("--port", "-p", default=None,
                         help="Serial port (e.g. /dev/cu.usbserial-0001).")
    ser_grp.add_argument("--baud", "-b", type=int, default=None,
                         help="Baud rate (default: 250000 for Tek/DCS-BIOS, 115200 otherwise).")
    ser_grp.add_argument("--sniff", "-s", action="store_true",
                         help="Serial sniff: dump raw bytes as hex.")
    ser_grp.add_argument("--no-sync", action="store_true",
                         help="Do not send the DCS-BIOS sync preamble (0x55×4).")
    ser_grp.add_argument("--dtr", action="store_true",
                         help="Assert DTR on open (resets most ESP32/Arduino boards).")
    ser_grp.add_argument("--name", action="store_true",
                         help="Assign a friendly name to the selected serial port and exit.")

    # DCS-BIOS definitions / output commands
    aircraft_keys = list(BIOS_MODULES.keys())
    bios_grp = p.add_argument_group("DCS-BIOS definitions & output commands")
    bios_grp.add_argument(
        "--aircraft", "-a", default="fa18c", metavar="KEY",
        help=f"Aircraft module for DCS-BIOS definitions. "
             f"Options: {', '.join(aircraft_keys)}  (default: fa18c)",
    )
    bios_grp.add_argument(
        "--send", nargs=2, metavar=("CONTROL", "VALUE"),
        help="Send a one-shot output command and exit.  Example: --send MASTER_CAUTION_LT 1",
    )
    bios_grp.add_argument(
        "--bios-list", action="store_true",
        help="List all output-capable controls for the selected aircraft and exit.",
    )
    bios_grp.add_argument(
        "--bios-search", metavar="QUERY",
        help="Search DCS-BIOS controls by name or description and exit.",
    )
    bios_grp.add_argument(
        "--bios-refresh", action="store_true",
        help="Force re-download of the aircraft JSON (clears cache) and exit.",
    )

    # Common options
    common = p.add_argument_group("Common options")
    common.add_argument("--timestamp", "-t", action="store_true",
                        help="Show HH:MM:SS.mmm timestamp on each line.")
    common.add_argument("--changes-only", "-c", action="store_true", default=True,
                        help="Only print when value changes (default: on; use --all-values to disable).")
    common.add_argument("--all-values", action="store_true",
                        help="Print every event even if value has not changed (overrides --changes-only).")
    common.add_argument("--tone", action="store_true",
                        help="Play a beep through Mac speakers on every button event.")
    common.add_argument("--no-color", action="store_true",
                        help="Disable ANSI colour output.")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    aliases = _load_aliases()

    if args.list_devices:
        list_hid_devices()
        sys.exit(0)

    if args.hid_info and args.serial:
        print(colorize("--hid-info is for HID mode only.", ANSI_YELLOW))
        sys.exit(1)

    # ── BIOS utility commands (no panel connection needed) ─────────────────────
    if args.bios_refresh:
        mod = BIOS_MODULES.get(args.aircraft)
        if not mod:
            print(colorize(f"Unknown aircraft '{args.aircraft}'", ANSI_RED))
            sys.exit(1)
        cache_path = _BIOS_CACHE_DIR / mod["filename"]
        cache_path.unlink(missing_ok=True)
        print(colorize(f"  Cache cleared for {mod['name']}.  Re-downloading…", ANSI_DIM))
        BiosDefs(args.aircraft)
        sys.exit(0)

    if args.bios_list:
        defs = BiosDefs(args.aircraft)
        if not defs:
            sys.exit(1)
        mod_name = BIOS_MODULES[args.aircraft]["name"]
        print(colorize(f"\nDCS-BIOS output controls — {mod_name}\n", ANSI_BOLD))
        for ident, ctrl in defs.output_controls():
            desc = ctrl.get("description", "")
            cat  = ctrl.get("category", "")
            outs = [o for o in ctrl.get("outputs", []) if o["type"] == "integer"]
            max_v = outs[0].get("max_value", 1) if outs else 1
            print(f"  {colorize(ident, ANSI_CYAN):<46}  {desc:<50}  "
                  f"{colorize(f'[{cat}]', ANSI_DIM)}  "
                  f"{colorize(f'max={max_v}', ANSI_DIM)}")
        print(colorize(f"\n  {len(list(defs.output_controls()))} controls total", ANSI_DIM))
        sys.exit(0)

    if args.bios_search:
        defs = BiosDefs(args.aircraft)
        if not defs:
            sys.exit(1)
        results = defs.search(args.bios_search)
        if not results:
            print(colorize(f"  No controls match '{args.bios_search}'", ANSI_DIM))
            sys.exit(0)
        for ident, ctrl in results:
            desc = ctrl.get("description", "")
            cat  = ctrl.get("category", "")
            outs = ctrl.get("outputs", [])
            int_outs = [o for o in outs if o["type"] == "integer"]
            out_tag = colorize(f"  addr=0x{int_outs[0]['address']:04X} mask=0x{int_outs[0]['mask']:04X}", ANSI_DIM) if int_outs else ""
            print(f"  {colorize(ident, ANSI_CYAN):<46}  {desc:<50}  "
                  f"{colorize(f'[{cat}]', ANSI_DIM)}{out_tag}")
        sys.exit(0)

    if args.send:
        # One-shot: connect to first available serial panel, send command, exit
        ctrl_name, val_str = args.send
        try:
            value = int(val_str)
        except ValueError:
            print(colorize(f"VALUE must be an integer, got '{val_str}'", ANSI_RED))
            sys.exit(1)

        defs = BiosDefs(args.aircraft)
        if not defs:
            sys.exit(1)

        if not _SERIAL_AVAILABLE:
            print(colorize("pyserial not installed.  Run: pip3 install pyserial", ANSI_RED))
            sys.exit(1)

        all_ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        dcs_ports = [pt for pt in all_ports if (pt.vid in DCS_SERIAL_VIDS)
                     or "usbmodem" in pt.device.lower()
                     or "usbserial" in pt.device.lower()]
        if not dcs_ports:
            print(colorize("No DCS serial panels found.", ANSI_RED))
            sys.exit(1)

        import serial as _serial_mod
        write_qs: "list[_queue_mod.Queue]" = []
        conns: list = []
        for pt in dcs_ports:
            cfg = _get_panel_config(_port_stable_key(pt), aliases)
            if cfg["mode"] == "hid":
                continue
            baud = 250000 if pt.vid in DCS_SERIAL_VIDS else 115200
            try:
                ser = _serial_mod.Serial(pt.device, baud, timeout=1, dsrdtr=False, rtscts=False)
                ser.dtr = False
                ser.rts = False
            except Exception as exc:
                print(colorize(f"  ✖  {pt.device}: {exc}", ANSI_RED))
                continue
            wq: "_queue_mod.Queue" = _queue_mod.Queue()
            write_qs.append(wq)
            conns.append(ser)

        if not write_qs:
            print(colorize("Could not open any serial panels.", ANSI_RED))
            sys.exit(1)

        writer = BiosWriter(defs, write_qs)
        ok, msg = writer.send(ctrl_name, value)
        icon = "✔" if ok else "✖"
        color = ANSI_GREEN if ok else ANSI_RED
        print(colorize(f"  {icon}  {msg}", color))

        if ok:
            # Drain all write queues to the actual serial ports
            for wq, ser in zip(write_qs, conns):
                while True:
                    try:
                        frame = wq.get_nowait()
                        ser.write(frame)
                        ser.flush()
                    except _queue_mod.Empty:
                        break
        for ser in conns:
            ser.close()
        sys.exit(0 if ok else 1)

    # ── Single-panel serial mode ───────────────────────────────────────────────
    if args.serial:
        port = args.port
        selected_pt = None

        if port is None:
            if not _SERIAL_AVAILABLE:
                print("pyserial not installed.  Run: pip3 install pyserial")
                sys.exit(1)

            all_ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

            dcs_usb_devs = [d for d in usb.core.find(find_all=True)
                            if d.idVendor in DCS_SERIAL_VIDS]
            serial_vids_seen = {pt.vid for pt in all_ports if pt.vid}
            usb_dcs_vids = {d.idVendor for d in dcs_usb_devs}
            missing_vids = usb_dcs_vids - serial_vids_seen
            if missing_vids:
                for d in dcs_usb_devs:
                    if d.idVendor not in missing_vids:
                        continue
                    try:
                        name = d.product or f"0x{d.idProduct:04x}"
                    except Exception:
                        name = f"0x{d.idProduct:04x}"
                    print(colorize(
                        f"\n  ↻  {name} is on USB but has no serial port yet — "
                        "sending USB reset to initialise CDC driver…",
                        ANSI_YELLOW,
                    ), flush=True)
                    _reset_for_hid(d)
                all_ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)

            if not all_ports:
                print(colorize("No serial ports found.", ANSI_RED, ANSI_BOLD))
                sys.exit(1)

            dcs_ports = [
                pt for pt in all_ports
                if (pt.vid in DCS_SERIAL_VIDS)
                or "usbmodem" in pt.device.lower()
                or "usbserial" in pt.device.lower()
            ]
            candidate_ports = dcs_ports if dcs_ports else all_ports

            if len(candidate_ports) == 1:
                selected_pt = candidate_ports[0]
                port = selected_pt.device
                label = _port_display_name(selected_pt, aliases)
                hint  = _port_id_hint(selected_pt, aliases)
                print(colorize(f"\n  Auto-selected: {selected_pt.device}  —  ", ANSI_DIM)
                      + label + hint)
            else:
                print(colorize("\nAvailable serial ports:", ANSI_BOLD))
                for i, pt in enumerate(candidate_ports):
                    label = _port_display_name(pt, aliases)
                    hint  = _port_id_hint(pt, aliases)
                    print(f"  [{colorize(str(i+1), ANSI_CYAN)}] {pt.device}  —  {label}{hint}")
                print(colorize(
                    "  (tip: run with --name to assign a friendly label and mode)\n",
                    ANSI_DIM,
                ))
                while True:
                    try:
                        choice = input(f"Select port [1–{len(candidate_ports)}]: ").strip()
                        idx = int(choice) - 1
                        if 0 <= idx < len(candidate_ports):
                            selected_pt = candidate_ports[idx]
                            port = selected_pt.device
                            break
                        print(f"  Enter 1–{len(candidate_ports)}.")
                    except (ValueError, EOFError):
                        print("  Invalid input.")

            if args.name:
                assign_name_interactive(selected_pt, aliases)
                sys.exit(0)

        baud = args.baud
        if baud is None:
            if selected_pt is not None and selected_pt.vid in DCS_SERIAL_VIDS:
                baud = 250000
                print(colorize(f"  Auto-baud: {baud} (Tek/DCS-BIOS device detected).", ANSI_DIM))
            else:
                baud = 115200

        run_serial(
            port, baud,
            show_timestamp=args.timestamp,
            changes_only=(not args.all_values),
            sniff=args.sniff,
            no_sync=args.no_sync,
            dtr=args.dtr,
            tone=args.tone,
            no_color=args.no_color,
        )

    # ── Single-panel HID mode (explicit --hid flag) ────────────────────────────
    elif args.hid or args.hid_info or args.raw:
        run_hid(
            args.vid, args.pid, args.ep, args.report_size,
            raw=args.raw,
            changes_only=args.changes_only,
            show_timestamp=args.timestamp,
            tone=args.tone,
            hid_info_only=args.hid_info,
            no_color=args.no_color,
        )

    # ── Default: all panels simultaneously ────────────────────────────────────
    else:
        run_all(
            aliases,
            tone=args.tone,
            no_color=args.no_color,
            show_timestamp=args.timestamp,
            aircraft=args.aircraft,
        )


if __name__ == "__main__":
    main()
