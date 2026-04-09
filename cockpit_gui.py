#!/usr/bin/env python3
"""
DCS Cockpit GUI
- Home view  : full cockpit image with clickable panel hotspots
- Panel view : panel image with live DCS-BIOS control overlays
- Control editor : drag control dots to correct positions (per panel)

Run `python3 layout_wizard.py` first to position the panel cutouts.
All file paths come from dcs_config.py — nothing is hardcoded here.
"""

import json
import queue as _queue_mod
import struct
import sys
import threading
from pathlib import Path

import dcs_config

# ── Optional hardware libraries (graceful degradation if missing) ──────────
try:
    import serial
    import serial.tools.list_ports
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False

try:
    import hid as _hid
    _HID_OK = True
except ImportError:
    _HID_OK = False

try:
    import usb.core
    _USB_OK = True
except ImportError:
    _USB_OK = False

from PyQt5.QtCore import (
    Qt, QPointF, QRectF, QTimer, pyqtSignal, QObject, QThread, QSize,
    QPoint, QRect, QMimeData
)
from PyQt5.QtGui import (
    QPixmap, QPainter, QColor, QPen, QFont, QBrush, QCursor,
    QRadialGradient, QFontMetrics, QPainterPath, QIcon, QDrag
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QStatusBar, QStackedWidget,
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem,
    QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsTextItem,
    QGraphicsItem, QSizePolicy, QScrollArea, QListWidget,
    QListWidgetItem, QSplitter, QMessageBox, QComboBox, QDialog,
    QDialogButtonBox, QSpinBox, QFormLayout, QMenu, QAction,
    QTreeWidget, QTreeWidgetItem, QLineEdit, QCheckBox, QInputDialog,
    QSlider, QTextEdit
)

# All paths come from dcs_config — edit config/app_config.json or
# config/panel_categories.json to change images without touching this file.
BASE_DIR      = dcs_config.BASE_DIR
CONFIG_DIR    = dcs_config.CONFIG_DIR
LAYOUT_FILE   = dcs_config.LAYOUT_FILE
CTRL_POS_FILE = dcs_config.CTRL_POS_FILE

# ── Colour palette ────────────────────────────────────────────────────────────
BG_DARK        = "#111118"
PANEL_BG       = "#1a1a2a"
ACCENT         = "#00c8ff"
ACCENT_HOVER   = "#ffcc00"
TEXT_PRIMARY   = "#e0e0f0"
TEXT_DIM       = "#666680"
GREEN_ON       = QColor(0, 255, 80)
GREEN_OFF      = QColor(0, 60, 20)
AMBER_ON       = QColor(255, 180, 0)
AMBER_OFF      = QColor(60, 40, 0)
RED_ON         = QColor(255, 40, 40)
RED_OFF        = QColor(60, 0, 0)
BLUE_ON        = QColor(0, 180, 255)
LED_ON         = QColor(255, 100, 255)
LED_OFF        = QColor(60, 20, 60)
KNOB_COLOR     = QColor(80, 80, 100)
TOGGLE_ON      = QColor(0, 200, 80)
TOGGLE_OFF     = QColor(80, 80, 80)

# Distinct per-position colours for multi-position selectors/switches.
# Index 0 = position 0 (off/first), then successive positions up to index 11.
# Wraps around via modulo for controls with >12 positions.
MULTI_POS_COLORS = [
    QColor( 80,  80,  80),   # 0  grey       (off / first)
    QColor(  0, 200,  80),   # 1  green
    QColor(255, 180,   0),   # 2  amber
    QColor(  0, 160, 255),   # 3  sky blue
    QColor(255,  80,  80),   # 4  red
    QColor(180,  80, 255),   # 5  purple
    QColor(255, 120, 200),   # 6  pink
    QColor(  0, 210, 210),   # 7  cyan
    QColor(255, 220, 100),   # 8  yellow
    QColor(100, 255, 180),   # 9  mint
    QColor(255, 160,  60),   # 10 orange
    QColor(160, 200, 255),   # 11 light blue
]

CTRL_DOT_RADIUS      = 10
CTRL_DOT_EDIT_RADIUS = 14

# Control types that get a text-box widget instead of a dot
DISPLAY_TYPES = {"display"}
GAUGE_TYPES   = {"analog_gauge", "analog_dial", "fixed_step_dial"}

# Staging tray — fixed QWidget to the left of the QGraphicsView (never zooms)
TRAY_W    = 180  # pixel width of the tray panel
TRAY_HDR_H = 28  # height of the "Drag onto panel →" header strip
ITEM_H    = 36   # height per row in the tray list

# ── Known DCS/Tek panel USB identifiers ───────────────────────────────────
TEK_VID = 0x16c0
DCS_SERIAL_VIDS = {0x16c0, 0x303a, 0x2341}   # Tek + ESP32 + Arduino
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
_PANEL_NAMES_FILE = Path(__file__).with_name("panel_names.json")
_PANEL_SELECTIONS_FILE = Path(__file__).parent / "config" / "panel_selections.json"
_SKIP_VIDS = {0x0000, 0x05AC, 0x004C}
_SKIP_USAGE_PAGES = {0x0C, 0x0D, 0x0F}


def _load_panel_aliases() -> dict:
    """Load panel_names.json {location_key: name_or_config}."""
    if _PANEL_NAMES_FILE.exists():
        try:
            return json.loads(_PANEL_NAMES_FILE.read_text())
        except Exception:
            pass
    return {}


def _panel_cfg(key: str, aliases: dict) -> dict:
    """Return {'name': str|None, 'mode': 'serial'|'hid'} for a location key."""
    val = aliases.get(key)
    if val is None:
        return {"name": None, "mode": "serial"}
    if isinstance(val, str):
        return {"name": val, "mode": "serial"}
    if isinstance(val, dict):
        return {"name": val.get("name"), "mode": val.get("mode", "serial")}
    return {"name": None, "mode": "serial"}


def _panel_unique_key(p: dict) -> str:
    """Stable unique key for identifying a panel across sessions."""
    if p["mode"] == "hid":
        return f"hid:{p['vid']:04x}:{p['pid']:04x}"
    return p.get("location") or p.get("port") or ""


def _load_panel_selections() -> dict:
    if _PANEL_SELECTIONS_FILE.exists():
        try:
            return json.loads(_PANEL_SELECTIONS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_panel_selections(data: dict):
    _PANEL_SELECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PANEL_SELECTIONS_FILE.write_text(json.dumps(data, indent=2))


def _save_panel_aliases(aliases: dict):
    _PANEL_NAMES_FILE.write_text(json.dumps(aliases, indent=2))


# ═════════════════════════════════════════════════════════════════════════════
# DCS-BIOS data model
# ═════════════════════════════════════════════════════════════════════════════

class BiosState:
    """Holds the current decoded value of every DCS-BIOS output address."""

    def __init__(self):
        self._memory: dict[int, int] = {}  # address -> raw 16-bit word
        self._lock = threading.Lock()

    def update(self, address: int, value: int):
        with self._lock:
            self._memory[address] = value

    def read(self, address: int, mask: int, shift_by: int) -> int:
        with self._lock:
            raw = self._memory.get(address, 0)
        return (raw & mask) >> shift_by


BIOS_STATE = BiosState()


# ═════════════════════════════════════════════════════════════════════════════
# DCS-BIOS reader thread
# ═════════════════════════════════════════════════════════════════════════════

class BiosSignals(QObject):
    connected    = pyqtSignal(str)
    disconnected = pyqtSignal()
    state_changed = pyqtSignal(int, int)   # address, new_value
    error        = pyqtSignal(str)


class BiosReaderThread(QThread):
    """
    Reads the DCS-BIOS binary export stream over UDP (default) or serial.
    Protocol: sync word 0x55 0x55 0x55 0x55, then frames of
              [address:2][count:2][data:count bytes] ...
    UDP multicast default: 239.255.50.10:5010
    """
    SYNC = bytes([0x55, 0x55, 0x55, 0x55])

    def __init__(self, mode: str = "udp", port: str = "", baud: int = 250000):
        super().__init__()
        self.mode  = mode
        self.port  = port
        self.baud  = baud
        self.sig   = BiosSignals()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        if self.mode == "udp":
            self._run_udp()
        else:
            self._run_serial()

    # ── UDP mode ──────────────────────────────────────────────────────────────

    def _run_udp(self):
        import socket, struct
        MCAST_GRP  = "239.255.50.10"
        MCAST_PORT = 5010
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", MCAST_PORT))
            mreq = struct.pack("4sL", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(0.5)
            self.sig.connected.emit(f"UDP {MCAST_GRP}:{MCAST_PORT}")
        except Exception as e:
            self.sig.error.emit(f"UDP setup failed: {e}")
            return

        buf = b""
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(65536)
                buf += data
                buf = self._parse_buf(buf)
            except socket.timeout:
                continue
            except Exception as e:
                self.sig.error.emit(str(e))
                break
        sock.close()
        self.sig.disconnected.emit()

    # ── Serial mode ───────────────────────────────────────────────────────────

    def _run_serial(self):
        try:
            import serial as _serial
        except ImportError:
            self.sig.error.emit("pyserial not installed")
            return
        try:
            ser = _serial.Serial(self.port, self.baud, timeout=1)
            self.sig.connected.emit(f"Serial {self.port} @ {self.baud}")
        except Exception as e:
            self.sig.error.emit(f"Serial open failed: {e}")
            return

        buf = b""
        while not self._stop.is_set():
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                buf = self._parse_buf(buf)
        ser.close()
        self.sig.disconnected.emit()

    # ── Frame parser ──────────────────────────────────────────────────────────

    def _parse_buf(self, buf: bytes) -> bytes:
        while len(buf) >= 4:
            idx = buf.find(self.SYNC)
            if idx == -1:
                return buf[-3:]
            if idx > 0:
                buf = buf[idx:]
            buf = buf[4:]        # consume sync
            while len(buf) >= 4:
                if buf[:4] == self.SYNC:
                    break        # next sync
                address = buf[0] | (buf[1] << 8)
                count   = buf[2] | (buf[3] << 8)
                if count == 0 or len(buf) < 4 + count:
                    break
                payload = buf[4:4 + count]
                for i in range(0, count - 1, 2):
                    val = payload[i] | (payload[i + 1] << 8)
                    BIOS_STATE.update(address + i, val)
                    self.sig.state_changed.emit(address + i, val)
                buf = buf[4 + count:]
        return buf


# ═════════════════════════════════════════════════════════════════════════════
# DCS-BIOS command sender
# ═════════════════════════════════════════════════════════════════════════════

class BiosSender:
    """Sends DCS-BIOS commands over UDP or serial."""

    def __init__(self):
        self._serial = None
        self._sock   = None
        self._mode   = "udp"

    def set_serial(self, ser):
        self._serial = ser
        self._mode   = "serial"

    def set_udp(self):
        import socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._mode = "udp"

    def send(self, identifier: str, argument: str):
        cmd = f"{identifier} {argument}\n".encode()
        try:
            if self._mode == "serial" and self._serial:
                self._serial.write(cmd)
            elif self._mode == "udp" and self._sock:
                self._sock.sendto(cmd, ("127.0.0.1", 7778))
        except Exception as e:
            print(f"[sender] {e}")


SENDER = BiosSender()
SENDER.set_udp()


# ═════════════════════════════════════════════════════════════════════════════
# Multi-panel connection manager
# ═════════════════════════════════════════════════════════════════════════════

class PanelManagerSignals(QObject):
    """Qt signals emitted by PanelManager (must live on the GUI thread)."""
    panel_connected  = pyqtSignal(str, str)      # panel_name, detail
    panel_event      = pyqtSignal(str, str, str)  # panel_name, control, value
    panel_error      = pyqtSignal(str, str)       # panel_name, error_msg
    output_sent      = pyqtSignal(str, str, str)  # ctrl_id, description, value_str
    status_changed   = pyqtSignal()


class PanelManager:
    """
    Discovers and connects to multiple physical DCS panels (serial + HID)
    simultaneously.  Reader threads push events onto a shared queue;
    call poll() from a QTimer on the GUI thread to drain and emit Qt signals.
    """

    _SYNC = b'\x55\x55\x55\x55'

    def __init__(self):
        self.sig = PanelManagerSignals()
        self._out_q: _queue_mod.Queue = _queue_mod.Queue()
        self._write_qs: list[_queue_mod.Queue] = []
        self._threads: list[threading.Thread] = []
        self._stop_events: list[threading.Event] = []   # one per thread
        self._connected: dict[str, str] = {}   # name → detail
        self._shadow: dict[int, int] = {}   # address → uint16 shadow register

    # ── Discovery ──────────────────────────────────────────────────────────

    @staticmethod
    def discover(show_all: bool = False) -> list[dict]:
        """
        Auto-detect connected serial and HID panels.

        When *show_all* is True every USB device is included — even Apple
        internals, consumer HID, USB hubs, etc.  Each entry carries
        ``hidden`` (bool) and ``hidden_reason`` (str) so the UI can
        visually distinguish normally-filtered devices.

        Returns a list of dicts with keys:
            name, mode ('serial'|'hid'|'usb'), port, hid_path, vid, pid,
            location, baud, hidden, hidden_reason
        """
        panels: list[dict] = []
        aliases = _load_panel_aliases()

        # ── Serial (DCS-BIOS / ESP32 / Arduino) ──────────────────────
        if _SERIAL_OK:
            for pt in sorted(serial.tools.list_ports.comports(),
                             key=lambda p: p.device):
                is_dcs = ((pt.vid or 0) in DCS_SERIAL_VIDS
                          or "usbmodem" in pt.device.lower()
                          or "usbserial" in pt.device.lower())
                if not is_dcs and not show_all:
                    continue
                key = pt.location or pt.device
                cfg = _panel_cfg(key, aliases)
                if cfg["mode"] == "hid" and not show_all:
                    continue
                tek_name = TEK_DEVICES.get(((pt.vid or 0), (pt.pid or 0)))
                panels.append({
                    "name": cfg["name"] or tek_name
                            or pt.description or pt.device,
                    "mode": "serial",
                    "port": pt.device,
                    "hid_path": None,
                    "vid": pt.vid or 0,
                    "pid": pt.pid or 0,
                    "location": key,
                    "baud": 250000 if (pt.vid or 0) in DCS_SERIAL_VIDS
                            else 115200,
                    "hidden": not is_dcs,
                    "hidden_reason": "" if is_dcs else "non-DCS serial",
                })

        # ── HID ───────────────────────────────────────────────────────
        if _HID_OK:
            _SKIP_REASONS = {0x05AC: "Apple", 0x004C: "Apple BT",
                             0x0000: "system virtual"}
            _UP_REASONS = {0x0C: "consumer", 0x0D: "digitizer",
                           0x0F: "force-feedback"}
            seen_paths: set[bytes] = set()
            serial_vidpids = {(p["vid"], p["pid"]) for p in panels}
            for entry in _hid.enumerate(0, 0):
                vid, pid = entry["vendor_id"], entry["product_id"]
                path = entry["path"]
                if path in seen_paths:
                    continue
                seen_paths.add(path)

                hidden = False
                hidden_reason = ""
                if vid in _SKIP_VIDS:
                    hidden = True
                    hidden_reason = _SKIP_REASONS.get(vid, "system")
                elif entry.get("usage_page", 0) in _SKIP_USAGE_PAGES:
                    hidden = True
                    hidden_reason = _UP_REASONS.get(
                        entry["usage_page"], f"UP 0x{entry['usage_page']:02x}")
                elif vid != TEK_VID:
                    hidden = True
                    hidden_reason = "non-Tek"
                elif (vid, pid) in serial_vidpids:
                    hidden = True
                    hidden_reason = "serial duplicate"

                if hidden and not show_all:
                    continue

                hid_key = f"hid:{vid:04x}:{pid:04x}"
                alias = aliases.get(hid_key)
                alias_name = (alias.get("name")
                              if isinstance(alias, dict) else alias)
                name = (alias_name
                        or TEK_DEVICES.get((vid, pid))
                        or entry.get("product_string")
                        or f"HID 0x{vid:04x}:0x{pid:04x}")
                panels.append({
                    "name": name,
                    "mode": "hid",
                    "port": None,
                    "hid_path": path,
                    "vid": vid,
                    "pid": pid,
                    "location": hid_key,
                    "baud": 0,
                    "hidden": hidden,
                    "hidden_reason": hidden_reason,
                })

        # ── USB-only devices (pyusb) — visible only in show-all mode ──
        if show_all and _USB_OK:
            covered = {(p["vid"], p["pid"]) for p in panels}
            for dev in usb.core.find(find_all=True):
                if (dev.idVendor, dev.idProduct) in covered:
                    continue
                covered.add((dev.idVendor, dev.idProduct))
                try:
                    name = (dev.product
                            or f"USB 0x{dev.idVendor:04x}:0x{dev.idProduct:04x}")
                except Exception:
                    name = f"USB 0x{dev.idVendor:04x}:0x{dev.idProduct:04x}"
                panels.append({
                    "name": name,
                    "mode": "usb",
                    "port": None,
                    "hid_path": None,
                    "vid": dev.idVendor,
                    "pid": dev.idProduct,
                    "location": f"usb:{dev.idVendor:04x}:{dev.idProduct:04x}",
                    "baud": 0,
                    "hidden": True,
                    "hidden_reason": "USB only",
                })

        panels.sort(key=lambda p: (p.get("hidden", False),
                                   p["name"].lower()))
        return panels

    # ── Connect / disconnect ───────────────────────────────────────────────

    def _stop_all_threads(self):
        """
        Signal every running reader thread to stop.
        Non-blocking: we set each thread's own Event and drop our references.
        Threads have a 0.1 s readline timeout so they exit within ~200 ms.
        New threads use retry logic when opening ports, which handles any
        brief overlap between old close and new open.
        """
        for ev in self._stop_events:
            ev.set()
        self._stop_events.clear()
        self._threads.clear()
        self._write_qs.clear()

    def _drain_queue(self):
        """Discard all pending messages from (now-stopping) old threads."""
        while True:
            try:
                self._out_q.get_nowait()
            except _queue_mod.Empty:
                break

    def connect_panels(self, panels: list[dict]):
        """
        Stop current readers and start fresh ones for each selected panel.
        A 250 ms pause after signalling the old threads lets them close
        their serial ports before new threads try to open the same devices.
        """
        self._stop_all_threads()
        self._connected.clear()

        # Brief pause so old threads can close their ports (readline
        # timeout is 0.1 s, so 250 ms is safely longer).
        import time as _time
        _time.sleep(0.25)

        # Drain any stale messages the old threads wrote before exiting.
        self._drain_queue()

        for p in panels:
            stop = threading.Event()
            self._stop_events.append(stop)
            if p["mode"] == "serial" and p.get("port"):
                wq: _queue_mod.Queue = _queue_mod.Queue()
                self._write_qs.append(wq)
                t = threading.Thread(
                    target=self._serial_reader,
                    args=(p["name"], p["port"],
                          p.get("baud", 250000), wq, stop),
                    daemon=True,
                )
            elif p["mode"] == "hid" and p.get("hid_path"):
                t = threading.Thread(
                    target=self._hid_reader,
                    args=(p["name"], p["hid_path"], stop),
                    daemon=True,
                )
            else:
                self._stop_events.pop()
                continue
            self._threads.append(t)
            t.start()

    def disconnect_all(self):
        self._stop_all_threads()
        self._connected.clear()
        self._drain_queue()
        self.sig.status_changed.emit()

    @property
    def is_connected(self) -> bool:
        return bool(self._connected)

    @property
    def connected_names(self) -> list[str]:
        return list(self._connected.keys())

    def send_output(self, ctrl_id: str, ctrl_def: dict, value: int) -> bool:
        """
        Send a DCS-BIOS binary output frame to all connected serial panels.
        Uses a shadow register so controls sharing an address word don't
        clobber each other.  Returns True if at least one queue was written to.
        """
        int_outputs = [o for o in ctrl_def.get("outputs", [])
                       if o.get("type") == "integer"]
        if not int_outputs:
            return False

        if not self._write_qs:
            desc = ctrl_def.get("description", ctrl_id)
            self.sig.panel_error.emit(
                "OUTPUT", f"{ctrl_id} ({desc}) — no serial panels connected")
            return False

        chunks: list[tuple[int, int]] = []
        for out in int_outputs:
            addr  = out["address"]
            mask  = out["mask"]
            shift = out["shift_by"]
            clamped  = max(0, min(value, out.get("max_value", 1)))
            current  = self._shadow.get(addr, 0)
            new_word = (current & (~mask & 0xFFFF)) | ((clamped << shift) & mask)
            self._shadow[addr] = new_word
            chunks.append((addr, new_word))

        frame = bytearray(self._SYNC)
        for addr, word in chunks:
            frame += struct.pack('<HHH', addr, 2, word)
        frame += self._SYNC

        for q in self._write_qs:
            q.put(bytes(frame))

        desc = ctrl_def.get("description", ctrl_id)
        hex_dump = ' '.join(f'{b:02x}' for b in frame)
        self.sig.output_sent.emit(ctrl_id, desc, f"{value}  [{hex_dump}]")
        return True

    def send_string_output(self, ctrl_id: str, ctrl_def: dict, text: str) -> bool:
        """
        Send a string value to all connected serial panels using a
        DCS-BIOS export-data frame.  Each 16-bit word is sent as a
        separate chunk (addr, count=2, word) to match the standard
        integer-output frame format that all firmware parsers support.
        """
        str_outputs = [o for o in ctrl_def.get("outputs", [])
                       if o.get("type") == "string"]
        if not str_outputs:
            return False

        if not self._write_qs:
            desc = ctrl_def.get("description", ctrl_id)
            self.sig.panel_error.emit(
                "OUTPUT", f"{ctrl_id} ({desc}) — no serial panels connected")
            return False

        frame = bytearray(self._SYNC)
        for out in str_outputs:
            addr = out["address"]
            max_len = out.get("max_length", len(text))
            raw = text.encode("ascii", errors="replace")[:max_len]
            raw = raw.ljust(max_len, b'\x00')
            if len(raw) % 2:
                raw += b'\x00'
            for i in range(0, len(raw), 2):
                word = raw[i] | (raw[i + 1] << 8)
                frame += struct.pack('<HHH', addr + i, 2, word)

        frame += self._SYNC

        for q in self._write_qs:
            q.put(bytes(frame))

        desc = ctrl_def.get("description", ctrl_id)
        hex_dump = ' '.join(f'{b:02x}' for b in frame)
        self.sig.output_sent.emit(
            ctrl_id, desc,
            f"{repr(text) if text else '(blank)'}  [{hex_dump}]")
        return True

    def send_address_sweep(self, start_addr: int, num_words: int) -> bool:
        """
        Diagnostic: write a numbered pattern to consecutive 16-bit words
        starting at start_addr.  Each word gets two printable ASCII chars
        derived from its word index (00, 01, 02, ... 99, AA, AB, ...).
        This reveals the firmware's actual address-to-display mapping.
        """
        if not self._write_qs:
            self.sig.panel_error.emit(
                "SWEEP", "No serial panels connected")
            return False

        frame = bytearray(self._SYNC)
        legend_lines = []
        for i in range(num_words):
            addr = start_addr + i * 2
            if i < 100:
                tag = f"{i:02d}"
            else:
                tag = chr(65 + (i - 100) // 26) + chr(65 + (i - 100) % 26)
            lo, hi = ord(tag[0]), ord(tag[1])
            word = lo | (hi << 8)
            frame += struct.pack('<HHH', addr, 2, word)
            legend_lines.append(f"  {tag}  →  addr {addr} (0x{addr:04X})")

        frame += self._SYNC

        for q in self._write_qs:
            q.put(bytes(frame))

        self.sig.output_sent.emit(
            "SWEEP",
            f"addrs 0x{start_addr:04X}–0x{start_addr + num_words*2:04X}",
            f"{num_words} words sent")
        return True, legend_lines

    def poll(self):
        """Drain event queue from the GUI thread (call via QTimer)."""
        for _ in range(100):
            try:
                msg = self._out_q.get_nowait()
            except _queue_mod.Empty:
                break
            kind = msg[0]
            if kind == "connect":
                self._connected[msg[1]] = msg[2]
                self.sig.panel_connected.emit(msg[1], msg[2])
                self.sig.status_changed.emit()
            elif kind == "error":
                self.sig.panel_error.emit(msg[1], msg[2])
            elif kind == "event":
                self.sig.panel_event.emit(msg[1], msg[2], msg[3])

    # ── Reader threads ─────────────────────────────────────────────────────

    def _serial_reader(self, name: str, port: str, baud: int,
                       write_q: _queue_mod.Queue,
                       stop: threading.Event):
        import time as _time
        ser = None
        for attempt in range(6):             # up to ~1.5 s of retries
            if stop.is_set():
                return
            try:
                ser = serial.Serial(port, baud, timeout=0.1,
                                    dsrdtr=False, rtscts=False)
                ser.dtr = False
                ser.rts = False
                break
            except Exception as exc:
                if attempt < 5:
                    _time.sleep(0.25)
                else:
                    self._out_q.put(("error", name, f"open failed: {exc}"))
                    return
        if ser is None:
            return
        try:
            ser.write(bytes([0x55, 0x55, 0x55, 0x55]))
            ser.flush()
        except Exception as exc:
            self._out_q.put(("error", name, str(exc)))
            ser.close()
            return

        self._out_q.put(("connect", name, f"serial {port} @ {baud}"))
        last: dict[str, str] = {}
        try:
            while not stop.is_set():
                # Drain write queue (output frames for LEDs / displays)
                while True:
                    try:
                        frame = write_q.get_nowait()
                    except _queue_mod.Empty:
                        break
                    try:
                        ser.write(frame)
                        ser.flush()
                    except Exception as exc:
                        self._out_q.put(("error", name,
                                         f"write failed: {exc}"))
                        break
                try:
                    raw = ser.readline()   # returns after timeout=0.1 s max
                except Exception as exc:
                    self._out_q.put(("error", name, str(exc)))
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                control, value = parts
                if last.get(control) == value:
                    continue
                last[control] = value
                self._out_q.put(("event", name, control, value))
        finally:
            ser.close()

    def _hid_reader(self, name: str, hid_path: bytes,
                    stop: threading.Event):
        if not _HID_OK:
            self._out_q.put(("error", name, "hidapi not installed"))
            return
        h = _hid.device()
        try:
            h.open_path(hid_path)
        except Exception as exc:
            self._out_q.put(("error", name, str(exc)))
            return

        h.set_nonblocking(0)
        self._out_q.put(("connect", name, "HID"))
        prev: bytes | None = None
        report_id: int | None = None

        try:
            while not stop.is_set():
                data = h.read(64, timeout_ms=200)
                if not data:
                    continue
                data = bytes(data)
                if report_id is None and data[0] != 0:
                    report_id = data[0]
                sl = slice(1, None) if report_id else slice(None)

                if prev is None:
                    prev = data
                    continue
                if data == prev:
                    continue

                prev_payload = prev[sl]
                payload = data[sl]
                for i, (ob, nb) in enumerate(zip(prev_payload, payload)):
                    if ob == nb:
                        continue
                    diff = ob ^ nb
                    for bit in range(8):
                        if diff & (1 << bit):
                            new_val = (nb >> bit) & 1
                            btn = f"Button {i * 8 + bit + 1}"
                            value = "1" if new_val else "0"
                            self._out_q.put((
                                "event", name, btn, value))
                prev = data
        finally:
            h.close()


# ═════════════════════════════════════════════════════════════════════════════
# Control overlay widgets (drawn on the panel QGraphicsScene)
# ═════════════════════════════════════════════════════════════════════════════

class ControlDot(QGraphicsEllipseItem):
    """
    A circular overlay representing one DCS-BIOS control on a panel image.
    Visual changes based on current state; clickable for interactive controls.
    """

    def __init__(self, ctrl_id: str, ctrl_def: dict, x: float, y: float,
                 edit_mode: bool = False, panel_view=None):
        r = CTRL_DOT_EDIT_RADIUS if edit_mode else CTRL_DOT_RADIUS
        super().__init__(-r, -r, r * 2, r * 2)
        self.ctrl_id   = ctrl_id
        self.ctrl_def  = ctrl_def
        self.edit_mode = edit_mode
        self.panel_view = panel_view
        self._r        = r
        self.setPos(x, y)
        self.setZValue(2)   # always above the panel image (Z=0)
        self._output   = (ctrl_def.get("outputs") or [{}])[0]
        self._address  = self._output.get("address", -1)
        self._mask     = self._output.get("mask", 0xFFFF)
        self._shift    = self._output.get("shift_by", 0)
        self._max      = self._output.get("max_value", 1)
        self._ctype    = ctrl_def.get("control_type", "selector")
        self._inputs   = ctrl_def.get("inputs", [])
        self._current_val = 0
        self._hovered  = False

        self.setAcceptHoverEvents(True)
        _cat = ctrl_def.get("_category", "")
        _has_int_out = any(o.get("type") == "integer" for o in ctrl_def.get("outputs", []))
        _dblclick_hint = ("<br><span style='color:#666;'>Double-click to send value</span>"
                          if _has_int_out else "")
        self.setToolTip(
            f"<b>{ctrl_id}</b><br>"
            f"{ctrl_def.get('description', '')}<br>"
            + (f"<span style='color:#7ab4e8;'>{_cat}</span>" if _cat else "")
            + _dblclick_hint
        )

        if edit_mode:
            self.setFlags(
                QGraphicsItem.ItemIsMovable |
                QGraphicsItem.ItemIsSelectable |
                QGraphicsItem.ItemSendsGeometryChanges
            )
            self.setCursor(QCursor(Qt.SizeAllCursor))
        else:
            clickable = self._inputs or self._ctype in ("led", "indicator") or _has_int_out
            self.setCursor(
                QCursor(Qt.PointingHandCursor) if clickable else QCursor(Qt.ArrowCursor)
            )

        self._inactive_overlay: QGraphicsEllipseItem | None = None
        self._make_label()
        self._refresh()

    def set_inactive(self, inactive: bool):
        """Show/hide a semi-transparent grey overlay indicating no USB panel association."""
        if inactive and not self._inactive_overlay:
            r = self._r + 4
            ov = QGraphicsEllipseItem(-r, -r, r * 2, r * 2, self)
            ov.setBrush(QBrush(QColor(60, 60, 60, 160)))
            ov.setPen(QPen(QColor(100, 100, 100, 100), 1))
            ov.setZValue(3)
            self._inactive_overlay = ov
        elif not inactive and self._inactive_overlay:
            if self._inactive_overlay.scene():
                self._inactive_overlay.scene().removeItem(self._inactive_overlay)
            self._inactive_overlay = None

    def _make_label(self):
        self._label = QGraphicsTextItem(self.ctrl_id, self)
        self._label.setDefaultTextColor(QColor(255, 255, 180, 200))
        f = QFont("Courier New", 0)
        f.setPixelSize(max(8, self._r - 2))
        self._label.setFont(f)
        self._label.setPos(self._r + 2, -self._r)

    def update_state(self, address: int, value: int):
        if address != self._address:
            return
        new_val = (value & self._mask) >> self._shift
        if new_val != self._current_val:
            self._current_val = new_val
            self._refresh()
            self._update_tooltip()

    def _refresh(self):
        v = self._current_val
        t = self._ctype

        if t in ("led", "indicator"):
            color = LED_ON if v else LED_OFF
        elif t in ("selector", "toggle_switch", "mission_computer_switch"):
            if self._max <= 1:
                color = TOGGLE_ON if v else TOGGLE_OFF
            else:
                color = MULTI_POS_COLORS[v % len(MULTI_POS_COLORS)]
        elif t in ("limited_dial", "analog_dial", "fixed_step_dial"):
            color = BLUE_ON
        elif t == "analog_gauge":
            color = QColor(100, 100, 200)
        elif t == "display":
            color = QColor(0, 180, 80)
        else:
            color = QColor(120, 120, 120)

        if self._hovered:
            color = color.lighter(150)

        self.setBrush(QBrush(color))
        clickable = self._inputs or self._ctype in ("led", "indicator")
        pen_color = QColor(255, 255, 255, 180) if clickable else QColor(100, 100, 100, 120)
        self.setPen(QPen(pen_color, 1.5))

    def _update_tooltip(self):
        _cat = self.ctrl_def.get("_category", "")
        desc = self.ctrl_def.get("description", "")
        val_line = f"<br><b>value: {self._current_val}"
        if self._max > 1:
            val_line += f" / {self._max}"
        val_line += "</b>"
        self.setToolTip(
            f"<b>{self.ctrl_id}</b><br>{desc}{val_line}<br>"
            + (f"<span style='color:#7ab4e8;'>{_cat}</span>" if _cat else "")
        )

    def hoverEnterEvent(self, event):
        self._hovered = True
        self._refresh()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self._refresh()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if self.edit_mode:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            if self._inputs:
                self._send_click()
            elif self._ctype in ("led", "indicator"):
                self._send_output_toggle()
        super().mousePressEvent(event)

    def _send_click(self):
        iface_types = {i.get("interface") for i in self._inputs}
        if "action" in iface_types:
            SENDER.send(self.ctrl_id, "TOGGLE")
        elif "fixed_step" in iface_types:
            SENDER.send(self.ctrl_id, "INC")
        elif "set_state" in iface_types:
            new_val = 0 if self._current_val else 1
            SENDER.send(self.ctrl_id, str(new_val))

    def _send_output_toggle(self):
        """Toggle an output-only control (LED/indicator) on the physical panel."""
        mgr = getattr(self.panel_view, "panel_mgr", None) if self.panel_view else None
        if mgr is None:
            return
        new_val = 0 if self._current_val else 1
        if mgr.send_output(self.ctrl_id, self.ctrl_def, new_val):
            self._current_val = new_val
            self._refresh()
            self._update_tooltip()

    def mouseDoubleClickEvent(self, event):
        if self.edit_mode:
            super().mouseDoubleClickEvent(event)
            return
        int_outputs = [o for o in self.ctrl_def.get("outputs", [])
                       if o.get("type") == "integer"]
        if int_outputs and event.button() == Qt.LeftButton:
            self._open_send_dialog()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _open_send_dialog(self):
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        mgr = getattr(self.panel_view, "panel_mgr", None) if self.panel_view else None
        dlg = DisplayInputDialog(self.ctrl_id, self.ctrl_def,
                                 self._current_val, parent=view,
                                 panel_mgr=mgr)
        if dlg.exec_() == QDialog.Accepted:
            val, cleared = dlg.get_value()
            if not cleared and val.isdigit():
                int_val = int(val)
                if mgr:
                    mgr.send_output(self.ctrl_id, self.ctrl_def, int_val)
                self._current_val = int_val
                self._refresh()
                self._update_tooltip()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.panel_view:
            self.panel_view.mark_positions_dirty()
        return super().itemChange(change, value)

    def contextMenuEvent(self, event):
        if not self.edit_mode or not self.panel_view:
            super().contextMenuEvent(event)
            return
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu { background:#1e1e2e; color:#e0e0f0; border:1px solid #333; }"
            "QMenu::item:selected { background:#2a4a7a; }"
        )
        menu.addAction(
            f"📋  {self.ctrl_id}  [{self.ctrl_def.get('control_type','')}]"
        ).setEnabled(False)
        menu.addSeparator()

        move_menu = menu.addMenu("Move to panel →")
        move_menu.setStyleSheet(menu.styleSheet())
        for pname in dcs_config.panel_filenames():
            if pname != self.panel_view.panel_name:
                act = move_menu.addAction(pname)
                act.triggered.connect(
                    lambda checked, p=pname: self.panel_view.reassign_control(self.ctrl_id, p)
                )

        excl_act = menu.addAction("🚫  Exclude from all panels")
        excl_act.triggered.connect(
            lambda: self.panel_view.exclude_control(self.ctrl_id)
        )

        menu.addSeparator()
        _add_display_as_menu(menu, self.ctrl_id, self.ctrl_def, self.panel_view)

        menu.exec_(event.screenPos())


# ═════════════════════════════════════════════════════════════════════════════
# Display / gauge text-box widget
# ═════════════════════════════════════════════════════════════════════════════

class DisplayInputDialog(QDialog):
    """
    Dialog for manually entering a value into a display rectangle.
    Shows DCS-BIOS metadata about the expected output format.
    For integer outputs (servos, gauges, LEDs) includes a slider with
    optional live-send — drag the slider and the panel updates in real time.
    """

    STYLE = (
        "QDialog { background:#1a1a2a; color:#e0e0f0; }"
        "QLabel  { color:#e0e0f0; }"
        "QLineEdit { background:#111118; color:#00dc50; border:1px solid #0a6030;"
        "            padding:6px; font-family:'Courier New'; font-size:14px; }"
        "QPushButton { background:#0a3020; color:#00dc50; border:1px solid #0a6030;"
        "              padding:6px 18px; font-weight:bold; }"
        "QPushButton:hover { background:#0e4030; }"
        "QPushButton:pressed { background:#083018; }"
        "QSlider::groove:horizontal { background:#111118; height:8px;"
        "    border:1px solid #0a6030; border-radius:4px; }"
        "QSlider::handle:horizontal { background:#00dc50; width:16px;"
        "    margin:-5px 0; border-radius:8px; }"
        "QSlider::handle:horizontal:hover { background:#00ff60; }"
        "QCheckBox { color:#e0e0f0; spacing:6px; }"
        "QCheckBox::indicator { width:14px; height:14px; }"
        "QCheckBox::indicator:unchecked { background:#111118; border:1px solid #0a6030; }"
        "QCheckBox::indicator:checked { background:#00dc50; border:1px solid #0a6030; }"
    )

    def __init__(self, ctrl_id: str, ctrl_def: dict, current_val,
                 parent=None, panel_mgr: "PanelManager | None" = None):
        super().__init__(parent)
        self.setWindowTitle(f"Set Value — {ctrl_id}")
        self.setStyleSheet(self.STYLE)
        self.setMinimumWidth(420)

        self._ctrl_id   = ctrl_id
        self._ctrl_def  = ctrl_def
        self._panel_mgr = panel_mgr
        self._syncing   = False     # guard against slider↔text feedback loop

        output = (ctrl_def.get("outputs") or [{}])[0]
        out_type = output.get("type", "unknown")
        description = ctrl_def.get("description", "")
        category = ctrl_def.get("_category", ctrl_def.get("category", ""))

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        hdr = QLabel(f"<b style='color:#00c8ff;font-size:13px'>{ctrl_id}</b>")
        layout.addWidget(hdr)
        if description:
            layout.addWidget(QLabel(f"<i>{description}</i>"))

        info_parts = []
        if category:
            info_parts.append(f"Category: {category}")
        info_parts.append(f"Control type: {ctrl_def.get('control_type', '?')}")
        info_parts.append(f"Output type: <b>{out_type}</b>")

        if out_type == "string":
            max_len = output.get("max_length", "?")
            info_parts.append(f"Max length: <b>{max_len}</b> characters")
            self._max_len = max_len if isinstance(max_len, int) else None
            self._max_val = None
        elif out_type == "integer":
            max_val = output.get("max_value", "?")
            mask = output.get("mask", "?")
            shift = output.get("shift_by", 0)
            info_parts.append(f"Max value: <b>{max_val}</b>")
            info_parts.append(f"Mask: 0x{mask:04X}, Shift: {shift}" if isinstance(mask, int) else f"Mask: {mask}")
            self._max_val = max_val if isinstance(max_val, int) else None
            self._max_len = None
        else:
            self._max_len = None
            self._max_val = None

        has_inputs = bool(ctrl_def.get("inputs"))
        if not has_inputs:
            info_parts.append("<span style='color:#aa8800'>No DCS-BIOS inputs "
                              "(read-only in sim — value set locally only)</span>")

        info_label = QLabel("<br>".join(info_parts))
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color:#b0b0c0; font-size:11px; margin:4px 0;")
        layout.addWidget(info_label)

        # ── Text input ────────────────────────────────────────────────────
        form = QFormLayout()
        self._input = QLineEdit()
        is_bool_string = (self._max_len == 1
                          and "1 = yes" in description.lower()
                          or "visible" in description.lower()
                          and self._max_len == 1)
        if is_bool_string:
            placeholder = "1 = on, 0 = off (or blank)"
        elif self._max_len:
            placeholder = f"max {self._max_len} chars (or blank)"
        elif self._max_val is not None:
            placeholder = f"0 \u2013 {self._max_val}"
        else:
            placeholder = "enter value (or blank)"
        self._input.setPlaceholderText(placeholder)
        if current_val != "" and current_val is not None:
            self._input.setText(str(current_val))
        self._input.selectAll()

        if self._max_len:
            self._input.setMaxLength(self._max_len)

        form.addRow("Value:", self._input)
        layout.addLayout(form)

        # ── Slider + live-send (integer outputs only) ─────────────────────
        self._slider: QSlider | None = None
        self._live_cb: QCheckBox | None = None
        self._val_label: QLabel | None = None

        if out_type == "integer" and isinstance(self._max_val, int) and self._max_val > 1:
            slider_row = QHBoxLayout()
            self._slider = QSlider(Qt.Horizontal)
            self._slider.setRange(0, self._max_val)
            self._slider.setTickInterval(max(1, self._max_val // 10))
            init_val = 0
            if current_val != "" and current_val is not None:
                try:
                    init_val = int(current_val)
                except (ValueError, TypeError):
                    pass
            self._slider.setValue(init_val)

            self._val_label = QLabel(str(init_val))
            self._val_label.setStyleSheet(
                "color:#00dc50; font-family:'Courier New'; font-size:13px;"
                " min-width:55px; qproperty-alignment:'AlignRight | AlignVCenter';")
            self._val_label.setFixedWidth(60)

            slider_row.addWidget(self._slider, 1)
            slider_row.addWidget(self._val_label)
            layout.addLayout(slider_row)

            # Percentage hint
            self._pct_label = QLabel("")
            self._pct_label.setStyleSheet("color:#666680; font-size:10px;")
            layout.addWidget(self._pct_label)
            self._update_pct(init_val)

            # Live-send checkbox
            live_row = QHBoxLayout()
            self._live_cb = QCheckBox("Live send to panel")
            self._live_cb.setToolTip(
                "When checked, every slider movement immediately sends\n"
                "the value to connected panels (servos, LEDs, displays).")
            self._live_cb.setChecked(True)
            live_row.addWidget(self._live_cb)
            live_row.addStretch()
            layout.addLayout(live_row)

            self._slider.valueChanged.connect(self._on_slider_changed)
            self._input.textChanged.connect(self._on_text_changed)

        # ── Buttons ───────────────────────────────────────────────────────
        btns = QHBoxLayout()
        ok_btn = QPushButton("Set")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        clear_btn = QPushButton("Clear")
        clear_btn.setStyleSheet(
            "QPushButton { background:#2a1a1a; color:#ff6060; border:1px solid #603030; }"
            "QPushButton:hover { background:#3a2020; }"
        )
        clear_btn.clicked.connect(self._clear_and_accept)
        btns.addWidget(clear_btn)
        btns.addStretch()
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        layout.addLayout(btns)

        self._cleared = False
        self._input.returnPressed.connect(self.accept)
        self._input.setFocus()

    # ── Slider / text sync ────────────────────────────────────────────────

    def _on_slider_changed(self, value: int):
        if self._syncing:
            return
        self._syncing = True
        self._input.setText(str(value))
        if self._val_label:
            self._val_label.setText(str(value))
        self._update_pct(value)
        self._syncing = False
        if self._live_cb and self._live_cb.isChecked():
            self._live_send(value)

    def _on_text_changed(self, text: str):
        if self._syncing or self._slider is None:
            return
        if text.strip().isdigit():
            val = min(int(text.strip()), self._max_val)
            self._syncing = True
            self._slider.setValue(val)
            if self._val_label:
                self._val_label.setText(str(val))
            self._update_pct(val)
            self._syncing = False

    def _update_pct(self, value: int):
        if self._max_val and self._max_val > 0:
            pct = value / self._max_val * 100
            self._pct_label.setText(f"{pct:.1f}%")

    def _live_send(self, value: int):
        """Push value to physical panels immediately (servo / LED / display)."""
        if self._panel_mgr is None:
            return
        self._panel_mgr.send_output(self._ctrl_id, self._ctrl_def, value)

    # ── Result ────────────────────────────────────────────────────────────

    def _clear_and_accept(self):
        self._cleared = True
        self.accept()

    def get_value(self):
        """Return (value_str, was_cleared)."""
        if self._cleared:
            return "", True
        txt = self._input.text().strip()
        if self._max_val is not None and txt.isdigit():
            val = min(int(txt), self._max_val)
            return str(val), False
        return txt, False


class DisplayRect(QGraphicsRectItem):
    """
    Rectangular overlay for display and gauge control types.
    - Shows live text value in view mode
    - Draggable + resizable (bottom-right handle) in edit mode
    - Size is persisted in control_positions.json alongside x/y
    """
    DEFAULT_W = 120
    DEFAULT_H = 24
    _HANDLE   = 10   # resize handle zone size in pixels

    def __init__(self, ctrl_id: str, ctrl_def: dict, x: float, y: float,
                 w: float = 0, h: float = 0,
                 edit_mode: bool = False, panel_view=None):
        _w = w if w > 0 else self.DEFAULT_W
        _h = h if h > 0 else self.DEFAULT_H
        # Define rect in LOCAL space starting at (0,0); position via setPos
        super().__init__(0, 0, _w, _h)
        self.setPos(x, y)

        self.ctrl_id    = ctrl_id
        self.ctrl_def   = ctrl_def
        self.edit_mode  = edit_mode
        self.panel_view = panel_view
        self._output    = (ctrl_def.get("outputs") or [{}])[0]
        self._address   = self._output.get("address", -1)
        self._mask      = self._output.get("mask", 0xFFFF)
        self._shift     = self._output.get("shift_by", 0)
        self._inputs    = ctrl_def.get("inputs", [])
        self._current_val = ""
        self._manual_override = False
        self._resizing  = False
        self._resize_start_pos   = None
        self._resize_start_size  = None
        self.setZValue(2)

        self.setBrush(QBrush(QColor(10, 30, 10, 200)))
        self.setPen(QPen(QColor(0, 160, 60, 200), 1))
        _cat = ctrl_def.get("_category", "")
        _out_type = self._output.get("type", "")
        _hint = ""
        if _out_type == "string":
            _hint = f"String, max {self._output.get('max_length','?')} chars"
        elif _out_type == "integer":
            _hint = f"Integer, 0\u2013{self._output.get('max_value','?')}"
        self.setToolTip(
            f"<b>{ctrl_id}</b><br>"
            f"{ctrl_def.get('description', '')}<br>"
            + (f"<span style='color:#7ab4e8;'>{_cat}</span><br>" if _cat else "")
            + (f"<span style='color:#88aa88;'>{_hint}</span><br>" if _hint else "")
            + "<span style='color:#666;'>Double-click to set value</span>"
        )
        self.setAcceptHoverEvents(True)

        if edit_mode:
            self.setFlags(
                QGraphicsItem.ItemIsMovable |
                QGraphicsItem.ItemIsSelectable |
                QGraphicsItem.ItemSendsGeometryChanges
            )

        # Text child — local (3, 3) is the rect's top-left + padding
        self._text = QGraphicsTextItem(ctrl_id, self)
        self._text.setDefaultTextColor(QColor(0, 220, 80))
        f = QFont("Courier New", 0)
        f.setPixelSize(max(9, int(_h * 0.5)))
        self._text.setFont(f)
        self._text.setPos(3, 2)

        # Resize handle indicator (bottom-right corner, edit mode only)
        self._handle_item = QGraphicsRectItem(
            _w - self._HANDLE, _h - self._HANDLE,
            self._HANDLE, self._HANDLE, self
        )
        self._handle_item.setBrush(QBrush(QColor(0, 180, 60, 200)))
        self._handle_item.setPen(QPen(Qt.NoPen))
        self._handle_item.setVisible(edit_mode)
        self._inactive_overlay: QGraphicsRectItem | None = None

    def set_inactive(self, inactive: bool):
        """Show/hide a semi-transparent grey overlay indicating no USB panel association."""
        if inactive and not self._inactive_overlay:
            r = self.rect()
            ov = QGraphicsRectItem(-2, -2, r.width() + 4, r.height() + 4, self)
            ov.setBrush(QBrush(QColor(60, 60, 60, 160)))
            ov.setPen(QPen(QColor(100, 100, 100, 100), 1))
            ov.setZValue(3)
            self._inactive_overlay = ov
        elif not inactive and self._inactive_overlay:
            if self._inactive_overlay.scene():
                self._inactive_overlay.scene().removeItem(self._inactive_overlay)
            self._inactive_overlay = None

    # ── Size helpers ──────────────────────────────────────────────────────────

    def set_size(self, w: float, h: float):
        w = max(40, w)
        h = max(14, h)
        self.setRect(0, 0, w, h)
        self._handle_item.setRect(w - self._HANDLE, h - self._HANDLE,
                                  self._HANDLE, self._HANDLE)
        f = self._text.font()
        f.setPixelSize(max(9, int(h * 0.5)))
        self._text.setFont(f)
        if self.panel_view:
            self.panel_view.mark_positions_dirty()

    def get_size(self) -> tuple[float, float]:
        r = self.rect()
        return r.width(), r.height()

    # ── Resize mouse handling ─────────────────────────────────────────────────

    def _in_resize_zone(self, local_pos) -> bool:
        r = self.rect()
        return (local_pos.x() >= r.width()  - self._HANDLE * 2 and
                local_pos.y() >= r.height() - self._HANDLE * 2)

    def mousePressEvent(self, event):
        if self.edit_mode and event.button() == Qt.LeftButton:
            if self._in_resize_zone(event.pos()):
                self._resizing = True
                self._resize_start_pos  = event.scenePos()
                self._resize_start_size = self.get_size()
                event.accept()
                return
        self._resizing = False
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.edit_mode:
            super().mouseDoubleClickEvent(event)
            return
        if event.button() == Qt.LeftButton:
            self._open_input_dialog()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _open_input_dialog(self):
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        mgr = getattr(self.panel_view, "panel_mgr", None) if self.panel_view else None
        dlg = DisplayInputDialog(self.ctrl_id, self.ctrl_def,
                                 self._current_val, parent=view,
                                 panel_mgr=mgr)
        if dlg.exec_() == QDialog.Accepted:
            val, cleared = dlg.get_value()
            if cleared:
                self._manual_override = False
                self._text.setDefaultTextColor(QColor(0, 220, 80))
                self._text.setPlainText(self.ctrl_id)
                self._current_val = ""
                self._send_to_panels("")
            else:
                self._manual_override = True
                self._text.setDefaultTextColor(QColor(0, 200, 255))
                self._text.setPlainText(val if val else "(blank)")
                self._current_val = val
                self._send_to_panels(val)
                self._send_to_dcs(val)

    def _send_to_panels(self, val: str):
        """Push the value to connected physical panels via PanelManager."""
        mgr = getattr(self.panel_view, "panel_mgr", None) if self.panel_view else None
        if mgr is None:
            return
        out_type = self._output.get("type", "")
        if out_type == "string":
            mgr.send_string_output(self.ctrl_id, self.ctrl_def, val)
        elif out_type == "integer" and val.isdigit():
            mgr.send_output(self.ctrl_id, self.ctrl_def, int(val))

    def _send_to_dcs(self, val: str):
        """Send to DCS via BiosSender if the control has inputs."""
        if not self._inputs:
            return
        iface_types = {i.get("interface") for i in self._inputs}
        if "set_state" in iface_types:
            SENDER.send(self.ctrl_id, val)
        elif "variable_step" in iface_types:
            SENDER.send(self.ctrl_id, val)

    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.scenePos() - self._resize_start_pos
            new_w = self._resize_start_size[0] + delta.x()
            new_h = self._resize_start_size[1] + delta.y()
            self.set_size(new_w, new_h)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hoverMoveEvent(self, event):
        if self.edit_mode:
            if self._in_resize_zone(event.pos()):
                self.setCursor(QCursor(Qt.SizeFDiagCursor))
            else:
                self.setCursor(QCursor(Qt.SizeAllCursor))
        super().hoverMoveEvent(event)

    # ── State / display ───────────────────────────────────────────────────────

    def update_state(self, address: int, value: int):
        if address != self._address:
            return
        decoded = (value & self._mask) >> self._shift
        self._text.setPlainText(f"{self.ctrl_id}: {decoded}")
        self._current_val = decoded
        if self._manual_override:
            self._manual_override = False
            self._text.setDefaultTextColor(QColor(0, 220, 80))

    def hoverEnterEvent(self, event):
        self.setBrush(QBrush(QColor(10, 60, 10, 220)))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setBrush(QBrush(QColor(10, 30, 10, 200)))
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.panel_view:
            self.panel_view.mark_positions_dirty()
        return super().itemChange(change, value)

    def contextMenuEvent(self, event):
        if not self.edit_mode or not self.panel_view:
            super().contextMenuEvent(event)
            return
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu { background:#1e1e2e; color:#e0e0f0; border:1px solid #333; }"
            "QMenu::item:selected { background:#2a4a7a; }"
        )
        menu.addAction(
            f"📋  {self.ctrl_id}  [{self.ctrl_def.get('control_type','')}]"
        ).setEnabled(False)
        menu.addSeparator()
        move_menu = menu.addMenu("Move to panel →")
        move_menu.setStyleSheet(menu.styleSheet())
        for pname in dcs_config.panel_filenames():
            if pname != self.panel_view.panel_name:
                act = move_menu.addAction(pname)
                act.triggered.connect(
                    lambda checked, p=pname: self.panel_view.reassign_control(self.ctrl_id, p)
                )
        excl_act = menu.addAction("🚫  Exclude from all panels")
        excl_act.triggered.connect(
            lambda: self.panel_view.exclude_control(self.ctrl_id)
        )

        menu.addSeparator()
        _add_display_as_menu(menu, self.ctrl_id, self.ctrl_def, self.panel_view)

        menu.exec_(event.screenPos())


def _add_display_as_menu(parent_menu: QMenu, ctrl_id: str, ctrl_def: dict, panel_view):
    """
    Adds a 'Display as →' submenu explaining what DCS-BIOS says about the control
    and letting the user override the visual type.
    """
    ctype    = ctrl_def.get("control_type", "?")
    has_in   = bool(ctrl_def.get("inputs"))
    override = dcs_config.load_display_overrides().get(ctrl_id)

    # Info header (not clickable)
    io_str   = "input + output" if has_in and ctrl_def.get("outputs") else \
               "output only"    if ctrl_def.get("outputs") else "input only"
    sub = parent_menu.addMenu(f"Display as  [{ctype} / {io_str}]")
    sub.setStyleSheet(parent_menu.styleSheet())

    act_dot = sub.addAction("● Dot  (button / indicator)")
    act_dot.setCheckable(True)
    act_dot.setChecked(override == "dot" or
                       (override is None and ctype not in DISPLAY_TYPES and ctype not in GAUGE_TYPES))

    act_rect = sub.addAction("▬ Rectangle  (display / gauge)")
    act_rect.setCheckable(True)
    act_rect.setChecked(override == "rect" or
                        (override is None and (ctype in DISPLAY_TYPES or ctype in GAUGE_TYPES)))

    sub.addSeparator()
    act_reset = sub.addAction("↺  Reset to DCS-BIOS default")
    act_reset.setEnabled(override is not None)

    def _apply(new_type: str | None):
        if new_type is None:
            dcs_config.remove_display_override(ctrl_id)
        else:
            dcs_config.save_display_override(ctrl_id, new_type)
        if panel_view:
            panel_view._swap_control_item(ctrl_id)

    act_dot.triggered.connect(lambda: _apply("dot"))
    act_rect.triggered.connect(lambda: _apply("rect"))
    act_reset.triggered.connect(lambda: _apply(None))


# ═════════════════════════════════════════════════════════════════════════════
# Staging tray — fixed left panel (not part of the scene, never zooms)
# ═════════════════════════════════════════════════════════════════════════════

def _ctrl_type_color(ctrl_def: dict) -> QColor:
    """Return the resting-state dot colour for a control (mirrors ControlDot._refresh)."""
    t = ctrl_def.get("control_type", "")
    if t in ("led", "indicator"):
        return LED_OFF
    if t in ("selector", "toggle_switch", "mission_computer_switch"):
        return TOGGLE_OFF
    if t in ("limited_dial", "analog_dial", "fixed_step_dial"):
        return BLUE_ON
    if t == "analog_gauge":
        return QColor(100, 100, 200)
    if t in DISPLAY_TYPES:
        return QColor(0, 180, 80)
    return QColor(120, 120, 120)


def _ctrl_type_icon(ctrl_def: dict) -> QIcon:
    """16×16 icon: circle for dots, small rect for display/gauge types."""
    color  = _ctrl_type_color(ctrl_def)
    ctype  = ctrl_def.get("control_type", "")
    has_in = bool(ctrl_def.get("inputs"))

    pix = QPixmap(16, 16)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)

    rim = QColor(255, 255, 255, 180) if has_in else QColor(100, 100, 100, 120)

    if ctype in DISPLAY_TYPES or ctype in GAUGE_TYPES:
        # Small rounded rectangle for screens / gauges
        p.setBrush(QBrush(color))
        p.setPen(QPen(rim, 1))
        p.drawRoundedRect(1, 3, 14, 10, 2, 2)
    else:
        # Filled circle for everything else
        p.setBrush(QBrush(color))
        p.setPen(QPen(rim, 1))
        p.drawEllipse(1, 1, 14, 14)

    p.end()
    return QIcon(pix)

class _TrayList(QListWidget):
    """QListWidget that starts a QDrag carrying the ctrl_id as plain text."""

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if not item:
            return
        ctrl_id  = item.data(Qt.UserRole)
        ctrl_def = item.data(Qt.UserRole + 1) or {}

        mime = QMimeData()
        mime.setText(ctrl_id)

        # Build a drag pixmap that mirrors the actual control appearance
        ctype   = ctrl_def.get("control_type", "")
        color   = _ctrl_type_color(ctrl_def)
        has_in  = bool(ctrl_def.get("inputs"))
        rim     = QColor(255, 255, 255, 200) if has_in else QColor(100, 100, 100, 140)
        is_rect = ctype in DISPLAY_TYPES or ctype in GAUGE_TYPES

        W, H = 160, 28
        pix = QPixmap(W, H)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)

        if is_rect:
            # Rounded rectangle (mirrors DisplayRect)
            p.setBrush(QBrush(QColor(10, 30, 10, 220)))
            p.setPen(QPen(QColor(0, 160, 60, 200), 1))
            p.drawRoundedRect(1, 1, 26, H - 2, 3, 3)
        else:
            # Filled circle (mirrors ControlDot)
            r = (H - 4) // 2
            cx, cy = 2 + r, H // 2
            p.setBrush(QBrush(color))
            p.setPen(QPen(rim, 1.5))
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # ctrl_id label to the right of the shape
        p.setPen(QColor(220, 220, 255))
        p.setFont(QFont("Courier New", 9))
        p.drawText(QRect(32, 0, W - 34, H), Qt.AlignVCenter, ctrl_id[:22])
        p.end()

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(pix)
        # Hot-spot over the shape (left side)
        drag.setHotSpot(QPoint(H // 2, H // 2))
        drag.exec_(Qt.MoveAction)


class _StagingTray(QWidget):
    """
    Fixed-width panel to the LEFT of the QGraphicsView in edit mode.
    Lists unplaced controls; user drags them onto the panel to place them.
    Because this is a real QWidget (not a scene item), it never zooms.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(TRAY_W)
        self.setStyleSheet(
            f"background:#12121f; border-right:1px solid #3a3a5a;"
        )
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        hdr = QLabel("  Drag onto panel →")
        hdr.setFixedHeight(TRAY_HDR_H)
        hdr.setStyleSheet(
            "background:#1a1a30; color:#8888aa; font-size:11px;"
            "font-style:italic; border-bottom:1px solid #3a3a5a;"
        )
        vl.addWidget(hdr)

        self._list = _TrayList()
        self._list.setStyleSheet(
            "QListWidget { background:#12121f; color:#c8c8e8; border:none;"
            "  font-family:'Courier New'; font-size:11px; }"
            "QListWidget::item { padding:3px 6px;"
            "  border-bottom:1px solid #1e1e38; }"
            "QListWidget::item:selected { background:#2a3a6a; }"
        )
        self._list.setIconSize(QSize(16, 16))
        self._list.setDragEnabled(True)
        self._list.setDefaultDropAction(Qt.MoveAction)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        vl.addWidget(self._list, 1)

        self._count_lbl = QLabel("")
        self._count_lbl.setFixedHeight(22)
        self._count_lbl.setAlignment(Qt.AlignCenter)
        self._count_lbl.setStyleSheet(
            "background:#1a1a30; color:#8888aa; font-size:10px;"
            "border-top:1px solid #3a3a5a;"
        )
        vl.addWidget(self._count_lbl)

    def populate(self, ctrl_ids: list, controls: dict):
        self._list.clear()
        for cid in ctrl_ids:
            ctrl_def = controls.get(cid, {})
            desc     = ctrl_def.get("description", "")
            item     = QListWidgetItem(_ctrl_type_icon(ctrl_def), cid)
            item.setData(Qt.UserRole,     cid)
            item.setData(Qt.UserRole + 1, ctrl_def)   # needed by startDrag
            item.setToolTip(desc)
            self._list.addItem(item)
        self._update_count()

    def remove_item(self, ctrl_id: str):
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == ctrl_id:
                self._list.takeItem(i)
                break
        self._update_count()

    def has_item(self, ctrl_id: str) -> bool:
        return any(
            self._list.item(i).data(Qt.UserRole) == ctrl_id
            for i in range(self._list.count())
        )

    def all_items(self) -> list:
        return [self._list.item(i).data(Qt.UserRole)
                for i in range(self._list.count())]

    def _update_count(self):
        n = self._list.count()
        self._count_lbl.setText(f"{n} unplaced" if n else "all placed ✓")


# ─────────────────────────────────────────────────────────────────────────────

def make_control_item(ctrl_id: str, ctrl_def: dict, x: float, y: float,
                      edit_mode: bool, panel_view,
                      w: float = 0, h: float = 0) -> QGraphicsItem:
    """
    Factory — returns the correct visual widget for a DCS-BIOS control.

    Visual type priority:
      1. Manual override in config/control_display_overrides.json  ("dot" | "rect")
      2. DCS-BIOS control_type:
           display, analog_gauge, analog_dial, fixed_step_dial  → DisplayRect
           everything else                                        → ControlDot
    """
    override = dcs_config.load_display_overrides().get(ctrl_id)
    ctype    = ctrl_def.get("control_type", "")

    use_rect = (override == "rect") or \
               (override is None and (ctype in DISPLAY_TYPES or ctype in GAUGE_TYPES))

    if use_rect:
        return DisplayRect(ctrl_id, ctrl_def, x, y, w, h, edit_mode, panel_view)
    return ControlDot(ctrl_id, ctrl_def, x, y, edit_mode, panel_view)


# ═════════════════════════════════════════════════════════════════════════════
# Panel detail view
# ═════════════════════════════════════════════════════════════════════════════

class PanelView(QWidget):
    """Shows a single panel image with control overlays."""

    back_requested = pyqtSignal()

    def __init__(self, panel_name: str, panel_image_path: Path,
                 controls: dict, all_positions: dict,
                 panel_mgr: "PanelManager | None" = None):
        super().__init__()
        self.panel_name  = panel_name
        self.image_path  = panel_image_path
        self.controls    = controls   # {ctrl_id: ctrl_def}
        self.positions   = all_positions   # shared mutable dict {ctrl_id: {x,y}}
        self.panel_mgr   = panel_mgr
        self.edit_mode   = False
        self._dirty      = False
        self._active_ctrls: set[str] = set()
        self._inactive_shown = False
        self.dots: dict[str, ControlDot] = {}
        self._init_ui()
        self._populate_scene()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        tb = QFrame()
        tb.setFixedHeight(44)
        tb.setStyleSheet(f"background:{PANEL_BG}; border-bottom:1px solid #333;")
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(8, 4, 8, 4)

        btn_back = QPushButton("← Cockpit Map")
        btn_back.setStyleSheet(_btn(PANEL_BG))
        btn_back.clicked.connect(self.back_requested.emit)
        tl.addWidget(btn_back)

        tl.addSpacing(16)
        self.title_label = QLabel(self.panel_name)
        self.title_label.setStyleSheet(f"color:{TEXT_PRIMARY}; font-size:14px; font-weight:bold;")
        tl.addWidget(self.title_label)

        tl.addStretch()

        self.btn_edit = QPushButton("Edit Positions")
        self.btn_edit.setCheckable(True)
        self.btn_edit.setStyleSheet(_btn("#333"))
        self.btn_edit.toggled.connect(self._toggle_edit_mode)
        tl.addWidget(self.btn_edit)

        self.btn_categories = QPushButton("Assign Categories…")
        self.btn_categories.setStyleSheet(_btn("#1a3a5a"))
        self.btn_categories.clicked.connect(self._show_category_dialog)
        tl.addWidget(self.btn_categories)

        self.btn_excluded = QPushButton("Excluded Controls…")
        self.btn_excluded.setStyleSheet(_btn("#4a2a00"))
        self.btn_excluded.clicked.connect(self._show_excluded_dialog)
        tl.addWidget(self.btn_excluded)

        self.btn_save_pos = QPushButton("Save Positions")
        self.btn_save_pos.setStyleSheet(_btn("#2a7a2a"))
        self.btn_save_pos.setEnabled(False)
        self.btn_save_pos.clicked.connect(self._save_positions)
        tl.addWidget(self.btn_save_pos)

        layout.addWidget(tb)

        # Middle: tray (fixed left, edit-mode only) + scene/view (stretches)
        self.scene = QGraphicsScene()
        self.view  = _ZoomView(self.scene)
        self.view._drop_callback = self._on_item_dropped

        self.tray = _StagingTray()
        self.tray.hide()   # shown only in edit mode

        mid = QWidget()
        mid_hl = QHBoxLayout(mid)
        mid_hl.setContentsMargins(0, 0, 0, 0)
        mid_hl.setSpacing(0)
        mid_hl.addWidget(self.tray)
        mid_hl.addWidget(self.view, 1)
        layout.addWidget(mid, 1)

        # Status bar
        self.unpos_bar = QFrame()
        self.unpos_bar.setFixedHeight(28)
        self.unpos_bar.setStyleSheet(f"background:{PANEL_BG};")
        bl = QHBoxLayout(self.unpos_bar)
        bl.setContentsMargins(8, 2, 8, 2)
        self.unpos_label = QLabel("")
        self.unpos_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:11px;")
        bl.addWidget(self.unpos_label)
        layout.addWidget(self.unpos_bar)

    def _populate_scene(self):
        self.scene.clear()
        self.dots.clear()

        pix = QPixmap(str(self.image_path))
        self.scene.addPixmap(pix)
        self.scene.setSceneRect(QRectF(pix.rect()))
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

        unpositioned = []
        for ctrl_id, ctrl_def in self.controls.items():
            pos = self.positions.get(ctrl_id)
            if pos:
                item = make_control_item(ctrl_id, ctrl_def, pos["x"], pos["y"],
                                         edit_mode=False, panel_view=self,
                                         w=pos.get("w", 0), h=pos.get("h", 0))
                self.scene.addItem(item)
                self.dots[ctrl_id] = item
            else:
                unpositioned.append(ctrl_id)

        self._update_status_bar()

    def _toggle_edit_mode(self, on: bool):
        if not on and self._dirty:
            ans = QMessageBox.question(
                self,
                "Unsaved positions",
                "You have unsaved control positions.\n\n"
                "Save now before leaving Edit Mode?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if ans == QMessageBox.Cancel:
                # Re-check the button without re-firing this slot
                self.btn_edit.blockSignals(True)
                self.btn_edit.setChecked(True)
                self.btn_edit.blockSignals(False)
                return
            if ans == QMessageBox.Save:
                self._save_positions()

        self.edit_mode = on
        self.btn_edit.setText("Exit Edit Mode" if on else "Edit Positions")
        self.btn_edit.setStyleSheet(_btn("#7a4a00" if on else "#333"))
        self.tray.setVisible(on)
        self._populate_edit_scene() if on else self._populate_scene()

    def _populate_edit_scene(self):
        self.scene.clear()
        self.dots.clear()
        self._edit_pix_size = (0, 0)

        pix = QPixmap(str(self.image_path))
        pw, ph = pix.width(), pix.height()
        self._edit_pix_size = (pw, ph)

        unpositioned = []

        # Panel image at (0, 0) — no scene offset needed now that the tray
        # is a real QWidget sitting beside the QGraphicsView.
        self.scene.addPixmap(pix)

        for ctrl_id in self.controls:
            pos = self.positions.get(ctrl_id)
            if pos:
                item = make_control_item(
                    ctrl_id, self.controls[ctrl_id],
                    pos["x"], pos["y"],
                    edit_mode=True, panel_view=self,
                    w=pos.get("w", 0), h=pos.get("h", 0)
                )
                self.scene.addItem(item)
                self.dots[ctrl_id] = item
            else:
                unpositioned.append(ctrl_id)

        self.tray.populate(unpositioned, self.controls)
        self.view.fitInView(QRectF(0, 0, pw, ph), Qt.KeepAspectRatio)
        self._update_status_bar()

    def _on_item_dropped(self, ctrl_id: str, scene_pos):
        """Called by _ZoomView.dropEvent when user drops a tray item on the panel."""
        if ctrl_id not in self.controls:
            return
        item = make_control_item(
            ctrl_id, self.controls[ctrl_id],
            scene_pos.x(), scene_pos.y(),
            edit_mode=True, panel_view=self,
        )
        self.scene.addItem(item)
        self.dots[ctrl_id] = item
        self.tray.remove_item(ctrl_id)
        self.mark_positions_dirty()
        self._update_status_bar()

    def _update_status_bar(self):
        n_unplaced = len(self.tray.all_items()) if self.edit_mode else \
                     sum(1 for cid in self.controls if cid not in self.positions)
        total = len(self.controls)
        placed = total - n_unplaced
        if self.edit_mode:
            self.unpos_label.setText(
                f"  Edit mode  ·  {placed}/{total} positioned  ·  "
                f"{n_unplaced} in left tray  ·  "
                "Drag onto panel to place  ·  Right-click to exclude or reassign"
            )
        elif n_unplaced:
            self.unpos_label.setText(
                f"  {n_unplaced} control{'s' if n_unplaced>1 else ''} not yet positioned — "
                "enable Edit Mode to place them"
            )
        else:
            self.unpos_label.setText(f"  All {total} controls positioned")

    def _swap_control_item(self, ctrl_id: str):
        """
        Replace a single control item with the correct new type after a
        display-override change — without touching any other item in the scene.
        """
        if ctrl_id not in self.controls:
            return

        old = self.dots.get(ctrl_id)
        if old is not None:
            # Capture current in-scene position (and size for DisplayRect)
            x, y = old.x(), old.y()
            w = h = 0
            if isinstance(old, DisplayRect):
                w, h = old.get_size()
            self.scene.removeItem(old)
        else:
            # Item was in the tray (unpositioned) — nothing to swap in the scene
            return

        new_item = make_control_item(
            ctrl_id, self.controls[ctrl_id],
            x, y,
            edit_mode=self.edit_mode, panel_view=self,
            w=w, h=h,
        )
        self.scene.addItem(new_item)
        self.dots[ctrl_id] = new_item
        self.mark_positions_dirty()

    def mark_positions_dirty(self):
        if not self._dirty:
            self._dirty = True
            self.btn_save_pos.setEnabled(True)

    def _save_positions(self):
        if self.edit_mode:
            pw, ph = self._edit_pix_size
            for ctrl_id, item in self.dots.items():
                scene_x = item.x()
                scene_y = item.y()
                # Only save if placed within panel image bounds
                if 0 <= scene_x < pw and 0 <= scene_y < ph:
                    entry: dict = {"x": round(scene_x), "y": round(scene_y)}
                    if isinstance(item, DisplayRect):
                        w, h = item.get_size()
                        entry["w"] = round(w)
                        entry["h"] = round(h)
                    self.positions[ctrl_id] = entry
        CONFIG_DIR.mkdir(exist_ok=True)
        with open(CTRL_POS_FILE, "w") as f:
            json.dump(self.positions, f, indent=2)
        self._dirty = False
        self.btn_save_pos.setEnabled(False)

    def exclude_control(self, ctrl_id: str):
        """Permanently hide a control from all panels."""
        dcs_config.save_excluded_control(ctrl_id)
        if ctrl_id in self.dots:
            self.scene.removeItem(self.dots.pop(ctrl_id))
        self.tray.remove_item(ctrl_id)
        self.controls.pop(ctrl_id, None)
        self.positions.pop(ctrl_id, None)
        self.mark_positions_dirty()
        self.unpos_label.setText(f"  {ctrl_id} excluded. Save Positions to persist.")

    def reassign_control(self, ctrl_id: str, target_panel: str):
        """Move a control to a different panel via control_overrides.json."""
        dcs_config.save_control_override(ctrl_id, target_panel)
        if ctrl_id in self.dots:
            self.scene.removeItem(self.dots.pop(ctrl_id))
        self.tray.remove_item(ctrl_id)
        self.controls.pop(ctrl_id, None)
        self.positions.pop(ctrl_id, None)
        self.unpos_label.setText(
            f"  {ctrl_id} moved to '{target_panel}'. "
            "Reopen that panel to position it there."
        )

    def _show_category_dialog(self):
        if self._dirty:
            ans = QMessageBox.question(
                self,
                "Unsaved positions",
                "You have unsaved control positions.\n\n"
                "Save now before opening the category dialog?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Save:
                self._save_positions()

        bios = self._bios_defs_ref()
        dlg  = CategoryAssignmentDialog(self.panel_name, bios, self)
        if dlg.exec_() == QDialog.Accepted:
            # Reload controls for this panel and repopulate
            mw = self.window()
            if hasattr(mw, "_controls_for_panel"):
                self.controls = mw._controls_for_panel(self.panel_name)
            if self.edit_mode:
                self._populate_edit_scene()
            else:
                self._populate_scene()

    def _show_excluded_dialog(self):
        dlg = ExcludedControlsDialog(self._bios_defs_ref(), self)
        dlg.exec_()
        # Refresh scene in case something was un-excluded
        if self.edit_mode:
            self._populate_edit_scene()
        else:
            self._populate_scene()

    def _bios_defs_ref(self) -> dict:
        """Walk up to MainWindow to get the full bios defs dict."""
        w = self.parent()
        while w is not None:
            if hasattr(w, "_bios_defs"):
                return w._bios_defs
            w = w.parent()
        return {}

    def update_control(self, address: int, value: int):
        for item in self.dots.values():
            item.update_state(address, value)

    def show_inactive_overlays(self):
        """Mark all unactivated controls with a grey overlay."""
        self._inactive_shown = True
        for ctrl_id, item in self.dots.items():
            if ctrl_id not in self._active_ctrls:
                item.set_inactive(True)

    def clear_inactive_overlays(self):
        """Remove all grey overlays and reset activation tracking."""
        self._inactive_shown = False
        self._active_ctrls.clear()
        for item in self.dots.values():
            item.set_inactive(False)

    def flash_physical_hit(self, control: str, value: str) -> bool:
        """
        Draw a large transient circle at the placed control's scene position
        when the physical panel reports that control (serial DCS-BIOS name).
        Uses distinct colors for multi-position switch values.
        """
        item = self.dots.get(control)
        matched_id = control
        if item is None:
            c_low = control.lower()
            for cid, dot in self.dots.items():
                if cid.lower() == c_low:
                    item = dot
                    matched_id = cid
                    break
        if item is None:
            return False

        if matched_id not in self._active_ctrls:
            self._active_ctrls.add(matched_id)
            item.set_inactive(False)

        center = item.sceneBoundingRect().center()
        radius = 34.0
        flash = QGraphicsEllipseItem(
            center.x() - radius, center.y() - radius,
            radius * 2, radius * 2,
        )

        if value == "0":
            fill = QColor(255, 52, 52, 210)
            rim  = QColor(255, 200, 200, 255)
        elif value == "1":
            fill = QColor(0, 255, 72, 210)
            rim  = QColor(180, 255, 200, 255)
        else:
            # Multi-position: pick a distinct color from the palette
            try:
                idx = int(value)
            except (ValueError, TypeError):
                idx = 1
            base = MULTI_POS_COLORS[idx % len(MULTI_POS_COLORS)]
            fill = QColor(base.red(), base.green(), base.blue(), 210)
            rim  = QColor(min(base.red() + 80, 255),
                          min(base.green() + 80, 255),
                          min(base.blue() + 80, 255), 255)

        flash.setBrush(QBrush(fill))
        flash.setPen(QPen(rim, 3))
        flash.setZValue(100)
        self.scene.addItem(flash)
        QTimer.singleShot(700, lambda fi=flash: self._remove_flash_ring(fi))
        return True

    def _remove_flash_ring(self, flash: QGraphicsEllipseItem):
        try:
            if flash.scene() is self.scene:
                self.scene.removeItem(flash)
        except RuntimeError:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# Excluded controls dialog
# ═════════════════════════════════════════════════════════════════════════════

class ExcludedControlsDialog(QDialog):
    """
    Shows all excluded controls.  Select one or more and click Restore to
    bring them back to their original panel.
    """

    def __init__(self, bios_defs: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Excluded Controls")
        self.setMinimumSize(620, 420)
        self.setStyleSheet(
            f"QDialog {{ background:{PANEL_BG}; color:{TEXT_PRIMARY}; }}"
            f"QListWidget {{ background:#111120; color:{TEXT_PRIMARY}; "
            f"  border:1px solid #333; font-family:Courier; font-size:12px; }}"
            f"QListWidget::item {{ padding:4px 6px; }}"
            f"QListWidget::item:selected {{ background:#2a4a7a; }}"
        )
        self._bios_defs = bios_defs
        self._build_ctrl_lookup()
        self._init_ui()
        self._refresh()

    def _build_ctrl_lookup(self):
        """Build {ctrl_id: (category, ctrl_def)} from the full bios defs."""
        self._lookup: dict[str, tuple[str, dict]] = {}
        for cat, controls in self._bios_defs.items():
            for ctrl_id, ctrl_def in controls.items():
                self._lookup[ctrl_id] = (cat, ctrl_def)

    def _init_ui(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(8)

        info = QLabel(
            "These controls are hidden from all panels.\n"
            "Select one or more and click Restore to bring them back."
        )
        info.setStyleSheet(f"color:{TEXT_DIM}; font-size:12px;")
        vl.addWidget(info)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.setAlternatingRowColors(True)
        vl.addWidget(self.list_widget, 1)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:11px;")
        vl.addWidget(self.count_label)

        btn_row = QHBoxLayout()
        btn_restore = QPushButton("Restore Selected")
        btn_restore.setStyleSheet(_btn("#2a5a2a"))
        btn_restore.clicked.connect(self._restore_selected)
        btn_row.addWidget(btn_restore)

        btn_row.addStretch()

        btn_close = QPushButton("Close")
        btn_close.setStyleSheet(_btn("#333"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        vl.addLayout(btn_row)

    def _refresh(self):
        self.list_widget.clear()
        excluded = sorted(dcs_config.load_excluded_controls())
        for ctrl_id in excluded:
            cat, ctrl_def = self._lookup.get(ctrl_id, ("unknown", {}))
            ctype = ctrl_def.get("control_type", "?")
            desc  = ctrl_def.get("description", "")
            text  = f"{ctrl_id:<35}  [{ctype:<18}]  {desc}"
            item  = QListWidgetItem(text)
            item.setData(Qt.UserRole, ctrl_id)
            self.list_widget.addItem(item)
        n = len(excluded)
        self.count_label.setText(
            f"  {n} excluded control{'s' if n != 1 else ''}"
            + ("  —  none" if n == 0 else "")
        )

    def _restore_selected(self):
        selected = self.list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            ctrl_id = item.data(Qt.UserRole)
            dcs_config.unexclude_control(ctrl_id)
        self._refresh()


# ═════════════════════════════════════════════════════════════════════════════
# Category assignment dialog
# ═════════════════════════════════════════════════════════════════════════════

class CategoryAssignmentDialog(QDialog):
    """
    Assign DCS-BIOS categories to a panel.

    Left  : all categories not on this panel, with current owner
    Right : QTreeWidget — categories assigned to this panel, expandable to
            show each individual control with placed / unplaced status
    """

    _PLACED_COLOR   = QColor(100, 200, 100)
    _UNPLACED_COLOR = QColor(220, 160, 40)

    def __init__(self, panel_name: str, bios_defs: dict, parent=None):
        super().__init__(parent)
        self.panel_name = panel_name
        self.bios_defs  = bios_defs
        self.setWindowTitle(f"Assign Categories — {panel_name}")
        self.setMinimumSize(900, 600)
        self.setStyleSheet(
            f"QDialog    {{ background:{PANEL_BG}; color:{TEXT_PRIMARY}; }}"
            f"QListWidget {{ background:#111120; color:{TEXT_PRIMARY}; "
            f"  border:1px solid #333; font-size:12px; }}"
            f"QListWidget::item {{ padding:3px 6px; }}"
            f"QListWidget::item:selected {{ background:#2a4a7a; }}"
            f"QTreeWidget {{ background:#111120; color:{TEXT_PRIMARY}; "
            f"  border:1px solid #333; font-size:12px; }}"
            f"QTreeWidget::item {{ padding:2px 4px; }}"
            f"QTreeWidget::item:selected {{ background:#2a4a7a; }}"
            f"QLineEdit   {{ background:#111120; color:{TEXT_PRIMARY}; "
            f"  border:1px solid #333; padding:3px 6px; font-size:12px; }}"
            f"QLabel      {{ color:{TEXT_PRIMARY}; }}"
        )
        self._categories_cfg = dcs_config.load_panel_categories()
        self._all_cats       = sorted(bios_defs.keys())
        self._my_cats: list[str] = sorted(self._categories_cfg.get(panel_name, []))
        # Load saved positions to determine placed/unplaced per control
        self._positions: dict = {}
        if dcs_config.CTRL_POS_FILE.exists():
            with open(dcs_config.CTRL_POS_FILE) as f:
                self._positions = json.load(f)
        self._init_ui()
        self._refresh()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _owner(self, cat: str) -> str:
        for panel, cats in self._categories_cfg.items():
            if cat in cats:
                return panel
        return "— unassigned —"

    def _cat_stats(self, cat: str) -> tuple[int, int, int]:
        """Return (total_controls, placed_count, unplaced_count) for a category."""
        controls = self.bios_defs.get(cat, {})
        placed   = sum(1 for cid in controls if cid     in self._positions)
        unplaced = sum(1 for cid in controls if cid not in self._positions)
        return len(controls), placed, unplaced

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(8)

        info = QLabel(
            f"Assign DCS-BIOS categories to <b>{self.panel_name}</b>. "
            "Moving a category here removes it from its current panel. "
            "Expand a category on the right to see individual controls. "
            "Left list: <span style='color:#50a050'>✓ green</span> = already on this panel, "
            "<span style='color:#a06e1e'>amber</span> = ≥ 50 % placed elsewhere, "
            "<span style='color:#82b4ff'>blue</span> = matched by control name."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{TEXT_DIM}; font-size:12px;")
        vl.addWidget(info)

        hl = QHBoxLayout()
        vl.addLayout(hl, 1)

        # ── Left: available categories ────────────────────────────────────────
        left_col = QVBoxLayout()
        left_col.addWidget(QLabel("Available categories  (current owner in brackets)"))

        self.search_avail = QLineEdit()
        self.search_avail.setPlaceholderText("Filter categories or controls…")
        self.search_avail.textChanged.connect(self._filter_avail)
        left_col.addWidget(self.search_avail)

        self.list_avail = QListWidget()
        self.list_avail.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_avail.itemDoubleClicked.connect(self._add_selected)
        left_col.addWidget(self.list_avail, 1)
        hl.addLayout(left_col, 1)

        # ── Middle: arrows ────────────────────────────────────────────────────
        mid = QVBoxLayout()
        mid.addStretch()
        btn_add = QPushButton("→  Add")
        btn_add.setStyleSheet(_btn("#1a4a1a"))
        btn_add.clicked.connect(self._add_selected)
        mid.addWidget(btn_add)
        btn_rem = QPushButton("←  Remove")
        btn_rem.setStyleSheet(_btn("#4a1a1a"))
        btn_rem.clicked.connect(self._remove_selected)
        mid.addWidget(btn_rem)
        mid.addStretch()
        hl.addLayout(mid)

        # ── Right: this panel — tree ──────────────────────────────────────────
        right_col = QVBoxLayout()
        right_col.addWidget(QLabel(
            f"This panel  ({self.panel_name})  "
            "— expand a category to see controls  "
            "  ● placed  ○ unplaced"
        ))

        self.search_mine = QLineEdit()
        self.search_mine.setPlaceholderText("Filter controls…")
        self.search_mine.textChanged.connect(self._filter_avail)
        right_col.addWidget(self.search_mine)

        self.tree_mine = QTreeWidget()
        self.tree_mine.setHeaderHidden(True)
        self.tree_mine.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree_mine.setIndentation(16)
        self.tree_mine.itemDoubleClicked.connect(self._on_tree_double_click)
        right_col.addWidget(self.tree_mine, 1)
        hl.addLayout(right_col, 1)

        # ── Bottom ────────────────────────────────────────────────────────────
        self.count_label = QLabel("")
        self.count_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:11px;")
        vl.addWidget(self.count_label)

        bb = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(_btn("#333"))
        btn_cancel.clicked.connect(self.reject)
        bb.addWidget(btn_cancel)
        bb.addStretch()
        btn_save = QPushButton("Save & Close")
        btn_save.setStyleSheet(_btn("#1a5a1a"))
        btn_save.clicked.connect(self._save)
        bb.addWidget(btn_save)
        vl.addLayout(bb)

    # ── populate ──────────────────────────────────────────────────────────────

    def _refresh(self):
        my_set = set(self._my_cats)
        filt   = self.search_avail.text().lower() if hasattr(self, "search_avail") else ""

        # Left list — show ALL categories (assigned ones are marked but still visible)
        self.list_avail.clear()
        for cat in self._all_cats:
            is_mine = cat in my_set

            cat_matches = (not filt) or filt in cat.lower()

            # Check individual control IDs and descriptions
            ctrl_hits: list[str] = []
            if filt and not cat_matches:
                for ctrl_id, ctrl_def in self.bios_defs.get(cat, {}).items():
                    desc = ctrl_def.get("description", "")
                    if filt in ctrl_id.lower() or filt in desc.lower():
                        ctrl_hits.append(ctrl_id)

            if not cat_matches and not ctrl_hits:
                continue

            owner          = self._owner(cat)
            n, placed, unp = self._cat_stats(cat)
            pct            = placed / n if n else 0.0

            if n == 0 or pct < 0.5:
                pct_tag = ""
            else:
                pct_tag = f"  {placed}/{n} placed"

            if is_mine:
                label = f"✓  {cat}  ({n}){pct_tag}"
            elif ctrl_hits:
                label = (f"{cat}  [{owner}]  ({n})"
                         f"  ⇠ {len(ctrl_hits)} matching control"
                         f"{'s' if len(ctrl_hits) != 1 else ''}: "
                         f"{', '.join(ctrl_hits[:4])}"
                         f"{'…' if len(ctrl_hits) > 4 else ''}")
            else:
                label = f"{cat}  [{owner}]  ({n}){pct_tag}"

            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, cat)

            if is_mine:
                item.setForeground(QColor(80, 160, 80))
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            elif ctrl_hits:
                item.setForeground(QColor(130, 180, 255))
            elif owner == "— unassigned —":
                item.setForeground(QColor(TEXT_DIM))
            elif pct >= 0.5:
                item.setForeground(QColor(160, 110, 30))
            self.list_avail.addItem(item)

        # Right tree — remember which categories were expanded
        expanded = set()
        root = self.tree_mine.invisibleRootItem()
        for i in range(root.childCount()):
            node = root.child(i)
            if node.isExpanded():
                expanded.add(node.data(0, Qt.UserRole))

        rfilt = self.search_mine.text().lower() if hasattr(self, "search_mine") else ""

        self.tree_mine.clear()
        total_ctrl = 0
        total_unplaced = 0

        for cat in sorted(self._my_cats):
            controls = self.bios_defs.get(cat, {})
            n, _placed, unplaced = self._cat_stats(cat)
            total_ctrl    += n
            total_unplaced += unplaced

            cat_name_matches = (not rfilt) or rfilt in cat.lower()

            # Category header item
            if unplaced:
                hdr_text = f"{cat}  ({n} controls  ·  {unplaced} unplaced ○)"
                hdr_color = self._UNPLACED_COLOR
            else:
                hdr_text = f"{cat}  ({n} controls  ·  all placed ●)"
                hdr_color = self._PLACED_COLOR

            hdr_item = QTreeWidgetItem([hdr_text])
            hdr_item.setData(0, Qt.UserRole, cat)
            hdr_item.setForeground(0, hdr_color)
            f = hdr_item.font(0)
            f.setBold(True)
            hdr_item.setFont(0, f)

            has_child_hit = False

            # Child items — one per control
            for ctrl_id, ctrl_def in sorted(controls.items()):
                placed   = ctrl_id in self._positions
                ctype    = ctrl_def.get("control_type", "?")
                has_in   = bool(ctrl_def.get("inputs"))
                desc     = ctrl_def.get("description", "")[:50]
                io_tag   = "in+out" if has_in else "out"
                status   = "●" if placed else "○"
                child_text = f"  {status}  {ctrl_id:<32}  [{ctype} / {io_tag}]  {desc}"

                ctrl_match = rfilt and (
                    rfilt in ctrl_id.lower() or rfilt in desc.lower()
                )

                if rfilt and not cat_name_matches and not ctrl_match:
                    continue

                child = QTreeWidgetItem([child_text])
                child.setData(0, Qt.UserRole, None)

                if ctrl_match:
                    child.setForeground(0, QColor(130, 180, 255))
                    has_child_hit = True
                else:
                    child.setForeground(0, self._PLACED_COLOR if placed else self._UNPLACED_COLOR)
                hdr_item.addChild(child)

            if rfilt and not cat_name_matches and not has_child_hit:
                continue

            self.tree_mine.addTopLevelItem(hdr_item)
            if cat in expanded or (rfilt and has_child_hit):
                hdr_item.setExpanded(True)

        placed_ctrl = total_ctrl - total_unplaced
        self.count_label.setText(
            f"  {len(self._my_cats)} categories  ·  "
            f"{total_ctrl} controls  ·  "
            f"{placed_ctrl} placed ●  ·  "
            f"{total_unplaced} unplaced ○"
        )

    def _filter_avail(self):
        self._refresh()

    # ── actions ───────────────────────────────────────────────────────────────

    def _add_selected(self):
        for item in self.list_avail.selectedItems():
            cat = item.data(Qt.UserRole)
            if cat not in self._my_cats:
                self._my_cats.append(cat)
        self._refresh()

    def _remove_selected(self):
        """Remove selected top-level (category) items from this panel."""
        to_remove = set()
        for item in self.tree_mine.selectedItems():
            cat = item.data(0, Qt.UserRole)
            if cat:   # only category-level items, not control children
                to_remove.add(cat)
        self._my_cats = [c for c in self._my_cats if c not in to_remove]
        self._refresh()

    def _on_tree_double_click(self, item, _col):
        """Double-click a category row to remove it; double-click a control to expand."""
        cat = item.data(0, Qt.UserRole)
        if cat:
            self._my_cats = [c for c in self._my_cats if c != cat]
            self._refresh()
        else:
            # It's a control child — do nothing (selection/expand handled by Qt)
            pass

    def _save(self):
        dcs_config.assign_categories_to_panel(self.panel_name, self._my_cats)
        self.accept()


# ═════════════════════════════════════════════════════════════════════════════
# Cockpit map (home screen)
# ═════════════════════════════════════════════════════════════════════════════

class CockpitMapView(QWidget):
    """Full cockpit image with clickable panel hotspot overlays."""

    panel_clicked = pyqtSignal(str)

    def __init__(self, layout: dict):
        super().__init__()
        self.layout_data = layout
        self._init_ui()

    def _init_ui(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)

        self.scene = QGraphicsScene()
        self.view  = _ZoomView(self.scene)
        vl.addWidget(self.view)

        self._build_scene()

    def _build_scene(self):
        self.scene.clear()
        img_path = dcs_config.resolve_cockpit_image()
        if img_path is None:
            return
        pix = QPixmap(str(img_path))
        self.scene.addPixmap(pix)
        self.scene.setSceneRect(QRectF(pix.rect()))
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

        for panel_name, pos in self.layout_data.items():
            item = _PanelHotspot(
                panel_name,
                pos["x"], pos["y"],
                pos["w"], pos["h"],
                self
            )
            self.scene.addItem(item)

    def notify_panel_click(self, name: str):
        self.panel_clicked.emit(name)

    def refresh_layout(self, layout: dict):
        self.layout_data = layout
        self._build_scene()


class _PanelHotspot(QGraphicsRectItem):
    """Clickable transparent rectangle over a panel region on the cockpit map."""

    def __init__(self, name: str, x: float, y: float, w: float, h: float, map_view):
        super().__init__(x, y, w, h)
        self.panel_name = name
        self.map_view   = map_view
        self.setAcceptHoverEvents(True)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        normal_pen = QPen(QColor(0, 200, 255, 120), 2)
        self.setPen(normal_pen)
        self.setBrush(QBrush(QColor(0, 0, 0, 0)))
        self._normal_pen  = normal_pen
        self._hover_pen   = QPen(QColor(255, 200, 0, 220), 3)

        label = QGraphicsTextItem(name, self)
        label.setDefaultTextColor(QColor(0, 200, 255, 180))
        f = QFont("Arial", 0)
        f.setPixelSize(max(14, int(h * 0.08)))
        f.setBold(True)
        label.setFont(f)
        label.setPos(x + 4, y + 4)

    def hoverEnterEvent(self, event):
        self.setPen(self._hover_pen)
        self.setBrush(QBrush(QColor(255, 200, 0, 20)))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setPen(self._normal_pen)
        self.setBrush(QBrush(QColor(0, 0, 0, 0)))
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.map_view.notify_panel_click(self.panel_name)
        super().mousePressEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
# Panel connection dialog
# ═════════════════════════════════════════════════════════════════════════════

class PanelConnectionDialog(QDialog):
    """
    Auto-discovers all serial (DCS-BIOS) and HID (Tek game-controller)
    panels.  User selects which panels to connect and whether to enable
    UDP multicast for receiving cockpit state from DCS.
    """

    def __init__(self, panel_manager: PanelManager,
                 udp_active: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Panel Connections")
        self.setMinimumSize(740, 520)
        self.setStyleSheet(
            f"QDialog {{ background:{PANEL_BG}; color:{TEXT_PRIMARY}; }}"
        )
        self._mgr = panel_manager
        self._udp_active = udp_active
        self._panels: list[dict] = []
        self._checks: list = []
        self._name_labels: list[QLabel] = []
        self._saved_sel = _load_panel_selections()
        self._init_ui()
        self._scan()

    # ── UI ─────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ── DCS-BIOS export (UDP) ─────────────────────────────────────
        udp_frame = QFrame()
        udp_frame.setStyleSheet(
            "QFrame { background:#111128; border:1px solid #2a2a4a;"
            "  border-radius:6px; }"
        )
        uf = QVBoxLayout(udp_frame)
        uf.setContentsMargins(14, 10, 14, 10)
        lbl_udp_hdr = QLabel(
            "DCS-BIOS Export  "
            "<span style='color:#666680; font-size:11px;'>"
            "(cockpit state from the simulator)</span>"
        )
        lbl_udp_hdr.setStyleSheet(
            f"color:{TEXT_PRIMARY}; font-size:13px; font-weight:bold;"
        )
        uf.addWidget(lbl_udp_hdr)
        self._udp_cb = QCheckBox(
            "UDP Multicast  239.255.50.10 : 5010"
        )
        self._udp_cb.setChecked(self._saved_sel.get("udp_enabled", True))
        self._udp_cb.setStyleSheet(
            f"QCheckBox {{ color:{TEXT_PRIMARY}; font-size:12px; }}"
        )
        uf.addWidget(self._udp_cb)
        root.addWidget(udp_frame)

        # ── Panels header ─────────────────────────────────────────────
        hdr = QHBoxLayout()
        lbl_panels = QLabel("Physical Panels")
        lbl_panels.setStyleSheet(
            f"color:{TEXT_PRIMARY}; font-size:13px; font-weight:bold;"
        )
        hdr.addWidget(lbl_panels)
        hdr.addStretch()
        self._show_hidden_cb = QCheckBox("Show Hidden Devices")
        self._show_hidden_cb.setStyleSheet(
            f"QCheckBox {{ color:{TEXT_DIM}; font-size:11px; }}"
        )
        self._show_hidden_cb.toggled.connect(self._scan)
        hdr.addWidget(self._show_hidden_cb)
        hdr.addSpacing(8)
        btn_scan = QPushButton("Scan")
        btn_scan.setStyleSheet(_btn("#1a3a5a"))
        btn_scan.setFixedWidth(80)
        btn_scan.clicked.connect(self._scan)
        hdr.addWidget(btn_scan)
        root.addLayout(hdr)

        # ── Scrollable panel list ─────────────────────────────────────
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(4, 4, 4, 4)
        self._scroll_layout.setSpacing(2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._scroll_content)
        scroll.setStyleSheet(
            "QScrollArea { background:#111120; border:1px solid #2a2a4a;"
            "  border-radius:6px; }"
        )
        root.addWidget(scroll, 1)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"color:{TEXT_DIM}; font-size:11px;")
        root.addWidget(self._count_label)

        # ── Button row ────────────────────────────────────────────────
        btns = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(_btn("#333"))
        btn_cancel.clicked.connect(self.reject)
        btns.addWidget(btn_cancel)
        btns.addStretch()

        btn_all = QPushButton("Select All")
        btn_all.setStyleSheet(_btn("#1a3a2a"))
        btn_all.clicked.connect(lambda: self._set_all(True))
        btns.addWidget(btn_all)

        btn_none = QPushButton("Select None")
        btn_none.setStyleSheet(_btn("#3a2a1a"))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btns.addWidget(btn_none)

        btns.addSpacing(16)
        btn_connect = QPushButton("  Connect  ")
        btn_connect.setStyleSheet(
            f"QPushButton {{ background:#1a6a1a; color:{TEXT_PRIMARY};"
            "  border:none; padding:7px 22px; border-radius:4px;"
            "  font-size:13px; font-weight:bold; }"
            "QPushButton:hover { background:#2a8a2a; }"
        )
        btn_connect.clicked.connect(self.accept)
        btns.addWidget(btn_connect)
        root.addLayout(btns)

    # ── Scan & populate ────────────────────────────────────────────────────

    def _scan(self):
        show_all = self._show_hidden_cb.isChecked()
        self._panels = PanelManager.discover(show_all=show_all)
        self._populate()

    def _populate(self):
        while self._scroll_layout.count():
            child = self._scroll_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._checks.clear()
        self._name_labels.clear()

        if not self._panels:
            lbl = QLabel(
                "No panels detected.\n\n"
                "Make sure panels are plugged in and drivers are loaded.\n"
                "Quit Chrome if it is running (it can lock USB devices)."
            )
            lbl.setStyleSheet(
                f"color:{TEXT_DIM}; padding:24px; font-size:12px;"
            )
            lbl.setAlignment(Qt.AlignCenter)
            self._scroll_layout.addWidget(lbl)
            self._count_label.setText("0 panels found")
            return

        saved_panels = self._saved_sel.get("panels", {})
        n_visible = 0

        for idx, p in enumerate(self._panels):
            key = _panel_unique_key(p)
            is_hidden = p.get("hidden", False)
            reason = p.get("hidden_reason", "")

            if not is_hidden:
                n_visible += 1

            row = QFrame()
            if is_hidden:
                row.setStyleSheet(
                    "QFrame { background:#111118;"
                    "  border-bottom:1px solid #1a1a28; }"
                    "QFrame:hover { background:#18182a; }"
                )
            else:
                row.setStyleSheet(
                    "QFrame { background:#161630;"
                    "  border-bottom:1px solid #222244; }"
                    "QFrame:hover { background:#1c1c40; }"
                )
            hl = QHBoxLayout(row)
            hl.setContentsMargins(10, 8, 10, 8)

            cb = QCheckBox()
            # Hidden devices default to unchecked unless explicitly saved
            default_checked = not is_hidden
            cb.setChecked(saved_panels.get(key, default_checked))
            cb.setStyleSheet("QCheckBox { border:none; }")
            self._checks.append(cb)
            hl.addWidget(cb)

            name_color = "#555570" if is_hidden else TEXT_PRIMARY
            name_lbl = QLabel(f"<b>{p['name']}</b>")
            name_lbl.setStyleSheet(
                f"color:{name_color}; font-size:13px; border:none;"
            )
            name_lbl.setMinimumWidth(180)
            self._name_labels.append(name_lbl)
            hl.addWidget(name_lbl)

            port = p.get("port") or ""
            if port:
                port_color = "#445566" if is_hidden else "#88aadd"
                port_lbl = QLabel(port)
                port_lbl.setStyleSheet(
                    f"color:{port_color}; font-size:12px; font-weight:bold;"
                    " border:none;"
                )
                hl.addWidget(port_lbl)

            if is_hidden and reason:
                tag = QLabel(reason)
                tag.setStyleSheet(
                    "color:#887744; font-size:9px; font-style:italic;"
                    " border:none; padding-left:4px;"
                )
                hl.addWidget(tag)

            hl.addStretch()

            mode = p["mode"]
            if is_hidden:
                mode_color = "#444455"
            elif mode == "serial":
                mode_color = ACCENT
            elif mode == "hid":
                mode_color = "#e080ff"
            else:
                mode_color = "#666680"
            mode_lbl = QLabel(mode.upper())
            mode_lbl.setStyleSheet(
                f"color:{mode_color}; font-weight:bold;"
                "  font-size:11px; border:none;"
            )
            mode_lbl.setFixedWidth(56)
            hl.addWidget(mode_lbl)

            vid_pid = f"0x{p['vid']:04x}:0x{p['pid']:04x}"
            id_lbl = QLabel(vid_pid)
            id_lbl.setStyleSheet(
                "color:#555570; font-size:10px; border:none;"
            )
            hl.addWidget(id_lbl)

            btn_rename = QPushButton("Rename")
            btn_rename.setFixedWidth(60)
            btn_rename.setStyleSheet(
                "QPushButton { background:#1a1a3a; color:#8888bb;"
                "  border:1px solid #333355; border-radius:3px;"
                "  font-size:10px; padding:2px 6px; }"
                "QPushButton:hover { background:#2a2a5a; color:#aaaadd; }"
            )
            btn_rename.clicked.connect(
                lambda _, i=idx: self._rename_panel(i))
            hl.addWidget(btn_rename)

            self._scroll_layout.addWidget(row)

        self._scroll_layout.addStretch()
        n = len(self._panels)
        n_hidden = n - n_visible
        if n_hidden:
            self._count_label.setText(
                f"{n} device{'s' if n != 1 else ''} found  "
                f"({n_visible} panels, {n_hidden} hidden)")
        else:
            self._count_label.setText(
                f"{n} panel{'s' if n != 1 else ''} found"
            )

    def _set_all(self, on: bool):
        for cb in self._checks:
            cb.setChecked(on)

    # ── Rename ─────────────────────────────────────────────────────────────

    def _rename_panel(self, idx: int):
        p = self._panels[idx]
        key = _panel_unique_key(p)
        new_name, ok = QInputDialog.getText(
            self, "Rename Panel", f"Alias for panel ({key}):",
            QLineEdit.Normal, p["name"],
        )
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        p["name"] = new_name
        self._name_labels[idx].setText(f"<b>{new_name}</b>")
        aliases = _load_panel_aliases()
        aliases[key] = new_name
        _save_panel_aliases(aliases)

    # ── Persist selections on accept ───────────────────────────────────────

    def accept(self):
        panels_sel = {}
        for p, cb in zip(self._panels, self._checks):
            panels_sel[_panel_unique_key(p)] = cb.isChecked()
        _save_panel_selections({
            "udp_enabled": self._udp_cb.isChecked(),
            "panels": panels_sel,
        })
        super().accept()

    # ── Results ────────────────────────────────────────────────────────────

    def get_selected_panels(self) -> list[dict]:
        return [p for p, cb in zip(self._panels, self._checks)
                if cb.isChecked()]

    def is_udp_enabled(self) -> bool:
        return self._udp_cb.isChecked()


# ═════════════════════════════════════════════════════════════════════════════
# Main window
# ═════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DCS-BIOS Cockpit Viewer — FA-18C Hornet")
        self.resize(1300, 860)
        self.setStyleSheet(f"background:{BG_DARK}; color:{TEXT_PRIMARY};")

        self._bios_thread: BiosReaderThread | None = None
        self._panel_mgr = PanelManager()
        self._layout_data: dict  = {}
        self._categories: dict   = {}
        self._bios_defs: dict    = {}
        self._ctrl_positions: dict = {}
        self._panel_views: dict[str, PanelView] = {}

        self._load_configs()
        self._init_ui()
        self._setup_panel_manager()
        self._setup_refresh_timer()

    # ── Config loading ────────────────────────────────────────────────────────

    def _load_configs(self):
        bios_path = dcs_config.bios_file()
        if bios_path.exists():
            with open(bios_path) as f:
                self._bios_defs = json.load(f)

        self._categories = dcs_config.load_panel_categories()

        if LAYOUT_FILE.exists():
            with open(LAYOUT_FILE) as f:
                self._layout_data = json.load(f)

        if CTRL_POS_FILE.exists():
            with open(CTRL_POS_FILE) as f:
                self._ctrl_positions = json.load(f)

    def _controls_for_panel(self, panel_filename: str) -> dict:
        """
        Return {ctrl_id: ctrl_def} for controls belonging to this panel.
        Applies control_overrides.json (per-control reassignments) and
        filters out anything in the _excluded list.
        Always reads panel_categories.json fresh so edits via the dialog
        take effect immediately without restarting.
        """
        self._categories = dcs_config.load_panel_categories()   # always fresh
        excluded  = dcs_config.load_excluded_controls()
        overrides = dcs_config.load_control_overrides()

        # Build reverse lookup: ctrl_id → category name
        ctrl_to_cat: dict[str, str] = {}
        for cat, controls in self._bios_defs.items():
            for cid in controls:
                ctrl_to_cat[cid] = cat

        # Base assignment from categories
        cat_names = self._categories.get(panel_filename, [])
        out: dict = {}
        for cat in cat_names:
            for ctrl_id, ctrl_def in self._bios_defs.get(cat, {}).items():
                out[ctrl_id] = {**ctrl_def, "_category": ctrl_to_cat.get(ctrl_id, "")}

        # Remove controls overridden to another panel
        out = {cid: v for cid, v in out.items()
               if overrides.get(cid, panel_filename) == panel_filename}

        # Add controls from other panels that were overridden to this one
        for ctrl_id, target in overrides.items():
            if target == panel_filename and ctrl_id not in out:
                for bios_cat, bios_controls in self._bios_defs.items():
                    if ctrl_id in bios_controls:
                        out[ctrl_id] = {**bios_controls[ctrl_id],
                                        "_category": ctrl_to_cat.get(ctrl_id, "")}
                        break

        # Remove excluded controls
        out = {cid: v for cid, v in out.items() if cid not in excluded}

        return out

    # ── UI setup ──────────────────────────────────────────────────────────────

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        vl = QVBoxLayout(central)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # Top bar
        topbar = QFrame()
        topbar.setFixedHeight(46)
        topbar.setStyleSheet(f"background:{PANEL_BG}; border-bottom:1px solid #2a2a3a;")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(10, 4, 10, 4)

        aircraft = dcs_config.load_app_config().get("aircraft", "DCS Cockpit")
        logo = QLabel(f"⚓ {aircraft}")
        logo.setStyleSheet(f"color:{ACCENT}; font-size:15px; font-weight:bold;")
        tl.addWidget(logo)

        tl.addSpacing(24)

        self.btn_home = QPushButton("Cockpit Map")
        self.btn_home.setStyleSheet(_btn(PANEL_BG))
        self.btn_home.clicked.connect(self._go_home)
        tl.addWidget(self.btn_home)

        self.btn_wizard = QPushButton("Layout Wizard")
        self.btn_wizard.setStyleSheet(_btn(PANEL_BG))
        self.btn_wizard.clicked.connect(self._open_wizard)
        tl.addWidget(self.btn_wizard)

        tl.addStretch()

        self.conn_label = QLabel("● No connections")
        self.conn_label.setStyleSheet("color:#ff4444; font-size:12px;")
        tl.addWidget(self.conn_label)

        self.btn_connect = QPushButton("Connections…")
        self.btn_connect.setStyleSheet(_btn("#1a5a1a"))
        self.btn_connect.clicked.connect(self._open_connections)
        tl.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("Disconnect All")
        self.btn_disconnect.setStyleSheet(_btn("#5a1a1a"))
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._disconnect)
        tl.addWidget(self.btn_disconnect)

        tl.addSpacing(12)
        self.btn_sweep = QPushButton("Sweep IFEI Addresses")
        self.btn_sweep.setStyleSheet(_btn("#4a3a1a"))
        self.btn_sweep.setToolTip(
            "Send numbered test pattern (00–54) to every IFEI address.\n"
            "Read the 2-digit codes on each physical display to discover\n"
            "the firmware's actual address mapping.")
        self.btn_sweep.clicked.connect(self._run_ifei_sweep)
        tl.addWidget(self.btn_sweep)

        vl.addWidget(topbar)

        # Stacked pages
        self.stack = QStackedWidget()
        vl.addWidget(self.stack, 1)

        # Page 0: no layout warning
        if not self._layout_data:
            self._build_no_layout_page()
        else:
            self._build_cockpit_map_page()

        # ── Event history drawer ──────────────────────────────────────
        self._history_open = True
        self._history_max  = 200
        self._analog_threshold = 0
        self._analog_last: dict[str, int] = {}

        self._history_frame = QFrame()
        self._history_frame.setStyleSheet(
            f"QFrame {{ background:#0e0e1a; border-top:1px solid #2a2a3a; }}"
        )
        hf_vl = QVBoxLayout(self._history_frame)
        hf_vl.setContentsMargins(0, 0, 0, 0)
        hf_vl.setSpacing(0)

        # Header row: toggle button + analog filter slider
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(0)

        self._history_hdr = QPushButton("▼ Event History")
        self._history_hdr.setStyleSheet(
            f"QPushButton {{ background:{PANEL_BG}; color:{TEXT_DIM};"
            "  border:none; padding:3px 12px; font-size:11px; }"
            "QPushButton:hover { background:#222238; }"
        )
        self._history_hdr.setCursor(QCursor(Qt.PointingHandCursor))
        self._history_hdr.clicked.connect(self._toggle_history)
        hdr_row.addWidget(self._history_hdr)

        hdr_row.addStretch()

        self._analog_lbl = QLabel("Analog filter: 0")
        self._analog_lbl.setStyleSheet(
            f"color:{TEXT_DIM}; font-size:10px; padding:0 4px;")
        hdr_row.addWidget(self._analog_lbl)

        self._analog_slider = QSlider(Qt.Horizontal)
        self._analog_slider.setRange(0, 5000)
        self._analog_slider.setSingleStep(50)
        self._analog_slider.setPageStep(500)
        self._analog_slider.setValue(0)
        self._analog_slider.setFixedWidth(140)
        self._analog_slider.setStyleSheet(
            "QSlider { height:16px; }"
            "QSlider::groove:horizontal { background:#222238; height:4px;"
            "  border-radius:2px; }"
            "QSlider::handle:horizontal { background:#5580cc; width:12px;"
            "  margin:-4px 0; border-radius:6px; }"
        )
        self._analog_slider.valueChanged.connect(self._on_analog_threshold)
        hdr_row.addWidget(self._analog_slider)

        hdr_row.addSpacing(8)
        hf_vl.addLayout(hdr_row)

        self._history_list = QListWidget()
        self._history_list.setStyleSheet(
            "QListWidget { background:#0e0e1a; color:#c0c0d0;"
            "  border:none; font-family:'Courier New'; font-size:11px; }"
            "QListWidget::item { padding:1px 8px;"
            "  border-bottom:1px solid #161628; }"
        )
        self._history_list.setVerticalScrollMode(
            QListWidget.ScrollPerPixel
        )
        self._history_list.setFixedHeight(160)
        self._history_list.itemClicked.connect(self._history_item_clicked)
        self._history_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._history_list.customContextMenuRequested.connect(
            self._history_context_menu)
        hf_vl.addWidget(self._history_list)

        vl.addWidget(self._history_frame)

        # Status bar
        self.status = QStatusBar()
        self.status.setStyleSheet(f"background:{PANEL_BG}; color:{TEXT_DIM}; font-size:11px;")
        self.setStatusBar(self.status)
        self._update_status()

    def _build_no_layout_page(self):
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setAlignment(Qt.AlignCenter)
        lbl = QLabel(
            "No panel layout found.\n\n"
            "Run the Layout Wizard first to position your panel cutouts\n"
            "over the cockpit image, then come back here."
        )
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color:{TEXT_DIM}; font-size:14px;")
        vl.addWidget(lbl)
        btn = QPushButton("Open Layout Wizard")
        btn.setStyleSheet(_btn("#1a5a1a"))
        btn.setFixedWidth(220)
        btn.clicked.connect(self._open_wizard)
        vl.addWidget(btn, 0, Qt.AlignCenter)
        self.stack.addWidget(w)

    def _build_cockpit_map_page(self):
        self.map_view = CockpitMapView(self._layout_data)
        self.map_view.panel_clicked.connect(self._open_panel)
        self.stack.addWidget(self.map_view)

    def _open_panel(self, panel_filename: str):
        if panel_filename not in self._panel_views:
            path = dcs_config.panels_dir() / panel_filename
            if not path.exists():
                self.status.showMessage(f"Image not found: {path}", 4000)
                return
            controls = self._controls_for_panel(panel_filename)
            pv = PanelView(
                panel_name       = panel_filename,
                panel_image_path = path,
                controls         = controls,
                all_positions    = self._ctrl_positions,
                panel_mgr        = self._panel_mgr,
            )
            pv.back_requested.connect(self._go_home)
            self._panel_views[panel_filename] = pv
            self.stack.addWidget(pv)
        self.stack.setCurrentWidget(self._panel_views[panel_filename])
        self.status.showMessage(f"Panel: {panel_filename}", 3000)

    def _go_home(self):
        if not self._layout_data and self.stack.count() == 0:
            return
        # Purge cached panel views so overrides/exclusions take effect on re-open
        for pv in self._panel_views.values():
            self.stack.removeWidget(pv)
            pv.deleteLater()
        self._panel_views.clear()
        self.stack.setCurrentIndex(0)

    def _open_wizard(self):
        import subprocess
        subprocess.Popen([sys.executable, str(BASE_DIR / "layout_wizard.py")])
        self.status.showMessage("Layout Wizard opened. Reload after saving.", 5000)

    # ── Panel manager setup ──────────────────────────────────────────────────

    def _setup_panel_manager(self):
        mgr = self._panel_mgr
        mgr.sig.panel_connected.connect(self._on_panel_connected)
        mgr.sig.panel_event.connect(self._on_panel_event)
        mgr.sig.panel_error.connect(self._on_panel_error)
        mgr.sig.output_sent.connect(self._on_output_sent)
        mgr.sig.status_changed.connect(self._update_conn_label)
        self._panel_poll = QTimer(self)
        self._panel_poll.setInterval(50)
        self._panel_poll.timeout.connect(mgr.poll)
        self._panel_poll.start()

    # ── Connections dialog & management ────────────────────────────────────

    def _open_connections(self):
        udp_active = self._bios_thread is not None
        dlg = PanelConnectionDialog(self._panel_mgr, udp_active, self)
        if dlg.exec_() != QDialog.Accepted:
            return

        # Stop only the BIOS/UDP reader – panel threads are stopped inside
        # connect_panels() so it can own the full stop→sleep→drain→start cycle.
        if self._bios_thread:
            self._bios_thread.stop()
            self._bios_thread.wait(3000)
            self._bios_thread = None

        selected = dlg.get_selected_panels()
        if selected:
            self._panel_mgr.connect_panels(selected)
        else:
            # Nothing to connect; still stop any previously running panels.
            self._panel_mgr.disconnect_all()

        if dlg.is_udp_enabled():
            self._start_bios_reader("udp", "", 250000)

        self._update_conn_label()

    def _start_bios_reader(self, mode: str, port: str, baud: int):
        if self._bios_thread:
            self._bios_thread.stop()
            self._bios_thread.wait(3000)
            self._bios_thread = None
        t = BiosReaderThread(mode=mode, port=port, baud=baud)
        t.sig.connected.connect(self._on_bios_connected)
        t.sig.disconnected.connect(self._on_bios_disconnected)
        t.sig.state_changed.connect(self._on_state_changed)
        t.sig.error.connect(self._on_bios_error)
        t.start()
        self._bios_thread = t

    def _disconnect(self):
        if self._bios_thread:
            self._bios_thread.stop()
            self._bios_thread.wait(3000)
            self._bios_thread = None
        self._panel_mgr.disconnect_all()
        for pv in self._panel_views.values():
            pv.clear_inactive_overlays()
        self._update_conn_label()

    def _run_ifei_sweep(self):
        """Send a numbered test pattern to every word in the IFEI address range."""
        IFEI_START = 29792
        NUM_WORDS = 56        # covers 29792–29903 (all IFEI controls + textures)
        result = self._panel_mgr.send_address_sweep(IFEI_START, NUM_WORDS)
        if not result:
            self.status.showMessage("Sweep failed — no panels connected", 5000)
            return
        ok, legend = result
        legend_text = (
            "IFEI Address Sweep — read the 2-digit code on each physical display:\n\n"
            + "\n".join(legend)
            + "\n\nReport which code appears on which display."
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("IFEI Address Sweep Legend")
        dlg.setMinimumSize(500, 600)
        dlg.setStyleSheet(
            f"QDialog {{ background:{PANEL_BG}; color:{TEXT_PRIMARY}; }}"
            "QTextEdit { background:#111118; color:#00dc50; font-family:'Courier New'; "
            "            font-size:12px; border:1px solid #333; }"
            "QPushButton { background:#0a3020; color:#00dc50; border:1px solid #0a6030; "
            "              padding:6px 18px; }"
        )
        lo = QVBoxLayout(dlg)
        lo.addWidget(QLabel(
            "<b>Sweep sent!</b> Read the 2-digit numbers on each physical display.<br>"
            "Match them to the legend below to find the real addresses."))
        te = QTextEdit()
        te.setReadOnly(True)
        te.setPlainText(legend_text)
        lo.addWidget(te)
        btn = QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        lo.addWidget(btn)
        dlg.exec_()

    # ── Panel event handlers ──────────────────────────────────────────────

    def _on_panel_connected(self, name: str, detail: str):
        self._push_history(name, "CONNECTED", detail, "#44ff44")
        self.status.showMessage(f"Panel connected: {name} ({detail})", 3000)
        for pv in self._panel_views.values():
            pv.show_inactive_overlays()

    def _on_panel_event(self, name: str, control: str, value: str):
        is_press = value in ("1", "INC", "DEC") or (
            value.isdigit() and int(value) > 0)
        color = "#00ff50" if is_press else "#ff3030"
        label = "PRESSED" if is_press else "RELEASED"
        if value not in ("0", "1"):
            label = value

        is_analog = value not in ("0", "1", "INC", "DEC")

        if is_analog and self._analog_threshold > 0:
            try:
                int_val = int(value)
            except (ValueError, TypeError):
                int_val = None
            if int_val is not None:
                prev = self._analog_last.get(control)
                self._analog_last[control] = int_val
                if prev is not None and abs(int_val - prev) < self._analog_threshold:
                    self.status.showMessage(f"  {name}  ▸  {control}  {label}")
                    return

        for pv in self._panel_views.values():
            pv.flash_physical_hit(control, value)

        if is_analog:
            self._coalesce_history(name, control, label, color)
        else:
            self._push_history(name, control, label, color)
        self.status.showMessage(f"  {name}  ▸  {control}  {label}")

    def _on_output_sent(self, ctrl_id: str, desc: str, value_str: str):
        self._push_history("OUTPUT →", ctrl_id, f"{value_str}  ({desc})", "#ff80ff")
        self.status.showMessage(f"  OUTPUT →  {ctrl_id}  {value_str}")

    def _on_panel_error(self, name: str, msg: str):
        self._push_history(name, "ERROR", msg, "#ff4444")
        self.status.showMessage(f"Panel error [{name}]: {msg}", 5000)

    # ── Event history drawer ──────────────────────────────────────────────

    def _history_item_clicked(self, item: QListWidgetItem):
        cursor_x = self._history_list.viewport().mapFromGlobal(
            QCursor.pos()).x()
        mid = self._history_list.viewport().width() // 3
        if cursor_x > mid:
            full_text = item.text()
            QApplication.clipboard().setText(full_text)
            self.status.showMessage(f"Copied full line", 2000)
        else:
            control = item.data(Qt.UserRole)
            if control:
                QApplication.clipboard().setText(control)
                self.status.showMessage(f"Copied: {control}", 2000)

    def _history_context_menu(self, pos):
        item = self._history_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background:#1e1e2e; color:#e0e0f0; border:1px solid #333; }"
            "QMenu::item:selected { background:#2a4a7a; }"
        )
        act_ctrl = menu.addAction("Copy control name")
        act_full = menu.addAction("Copy full line")
        chosen = menu.exec_(self._history_list.viewport().mapToGlobal(pos))
        if chosen == act_ctrl:
            control = item.data(Qt.UserRole)
            if control:
                QApplication.clipboard().setText(control)
                self.status.showMessage(f"Copied: {control}", 2000)
        elif chosen == act_full:
            QApplication.clipboard().setText(item.text())
            self.status.showMessage("Copied full line", 2000)

    def _on_analog_threshold(self, value: int):
        self._analog_threshold = value
        self._analog_lbl.setText(f"Analog filter: {value}")

    def _toggle_history(self):
        self._history_open = not self._history_open
        if self._history_open:
            self._history_list.setFixedHeight(180)
            self._history_hdr.setText("▼ Event History")
            self._history_list.scrollToBottom()
        else:
            self._history_list.setFixedHeight(0)
            self._history_hdr.setText("▲ Event History")

    def _coalesce_history(self, panel: str, control: str, detail: str,
                          color: str):
        """Update the last history row in-place if it's the same analog control."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        count = self._history_list.count()
        if count:
            last_item = self._history_list.item(count - 1)
            if last_item and last_item.data(Qt.UserRole) == control:
                text = f"{ts}   {panel:<20}  {control:<30}  {detail}"
                last_item.setText(text)
                last_item.setForeground(QColor(color))
                return
        self._push_history(panel, control, detail, color)

    def _push_history(self, panel: str, control: str, detail: str,
                      color: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        text = f"{ts}   {panel:<20}  {control:<30}  {detail}"
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, control)
        item.setForeground(QColor(color))
        self._history_list.addItem(item)
        if self._history_list.count() > self._history_max:
            self._history_list.takeItem(0)
        self._history_list.scrollToBottom()
        n = self._history_list.count()
        arrow = "▼" if self._history_open else "▲"
        self._history_hdr.setText(f"{arrow} Event History  ({n})")

    # ── DCS-BIOS export handlers ──────────────────────────────────────────

    def _on_bios_connected(self, desc: str):
        self._update_conn_label()
        self.status.showMessage(f"DCS-BIOS connected: {desc}")

    def _on_bios_disconnected(self):
        self._update_conn_label()

    def _on_state_changed(self, address: int, value: int):
        cur = self.stack.currentWidget()
        if isinstance(cur, PanelView):
            cur.update_control(address, value)

    def _on_bios_error(self, msg: str):
        self.status.showMessage(f"BIOS error: {msg}", 5000)

    # ── Connection label ──────────────────────────────────────────────────

    def _update_conn_label(self):
        parts: list[str] = []
        n = len(self._panel_mgr._connected)
        if n:
            parts.append(f"{n} panel{'s' if n > 1 else ''}")
        if self._bios_thread:
            parts.append("UDP")
        if parts:
            self.conn_label.setText(f"● {' + '.join(parts)}")
            self.conn_label.setStyleSheet("color:#44ff44; font-size:12px;")
            self.btn_disconnect.setEnabled(True)
        else:
            self.conn_label.setText("● No connections")
            self.conn_label.setStyleSheet("color:#ff4444; font-size:12px;")
            self.btn_disconnect.setEnabled(False)

    # ── Periodic status refresh ───────────────────────────────────────────────

    def _setup_refresh_timer(self):
        t = QTimer(self)
        t.setInterval(500)
        t.timeout.connect(self._update_status)
        t.start()

    def _update_status(self):
        cur = self.stack.currentWidget()
        if isinstance(cur, PanelView):
            n = len(cur.controls)
            pos = sum(1 for cid in cur.controls if cid in self._ctrl_positions)
            conn_parts = []
            np = len(self._panel_mgr._connected)
            if np:
                conn_parts.append(f"{np} panel{'s' if np > 1 else ''}")
            if self._bios_thread:
                conn_parts.append("UDP")
            conn_str = " + ".join(conn_parts) if conn_parts else "not connected"
            self.status.showMessage(
                f"Panel: {cur.panel_name}  |  "
                f"{pos}/{n} controls positioned  |  {conn_str}"
            )

    def closeEvent(self, event):
        self._disconnect()
        event.accept()


# ═════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═════════════════════════════════════════════════════════════════════════════

def _btn(bg: str) -> str:
    return (
        f"QPushButton {{ background:{bg}; color:{TEXT_PRIMARY}; border:none; "
        f"padding:5px 14px; border-radius:4px; font-size:12px; }}"
        f"QPushButton:hover {{ background:#3a3a5a; }}"
        f"QPushButton:disabled {{ color:#555; }}"
    )


class _ZoomView(QGraphicsView):
    _ZOOM_MIN = 0.05   # 5 %
    _ZOOM_MAX = 8.0    # 800 %

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        # Disable scrollbar-driven panning so wheel events don't fight the zoom
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setStyleSheet(f"background:{BG_DARK};")
        self._panning   = False
        self._pan_start = None
        self._pan_moved = False   # distinguishes right-drag-pan from right-click context menu
        # Set by PanelView to handle tray → scene drag-drops
        self._drop_callback = None   # callable(ctrl_id: str, scene_pos: QPointF)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if self._drop_callback and event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._drop_callback and event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if self._drop_callback and event.mimeData().hasText():
            ctrl_id  = event.mimeData().text()
            scene_pt = self.mapToScene(event.pos())
            self._drop_callback(ctrl_id, scene_pt)
            event.acceptProposedAction()
        else:
            event.ignore()

    def wheelEvent(self, event):
        # Use angleDelta (mouse wheel) or pixelDelta (macOS trackpad pinch).
        # pixelDelta fires continuously with tiny values on trackpads — normalise it.
        angle = event.angleDelta().y()
        if angle == 0:
            # Trackpad two-finger scroll: treat pixel delta as zoom intent
            angle = event.pixelDelta().y() * 3
        if angle == 0:
            event.ignore()
            return

        # Clamp to avoid huge jumps from high-resolution trackpads
        angle = max(-120, min(120, angle))
        factor = 1.0 + angle / 1200.0   # smooth: ±120 → ±10% per step

        current_scale = self.transform().m11()
        new_scale = current_scale * factor
        if new_scale < self._ZOOM_MIN or new_scale > self._ZOOM_MAX:
            event.accept()
            return

        self.scale(factor, factor)
        event.accept()   # prevent the event reaching the scrollbars

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning   = True
            self._pan_start = event.pos()
            self._pan_moved = False
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            event.accept()
            return
        if event.button() == Qt.RightButton:
            # Record start but don't consume — let scene items still receive it.
            # Pan activates only once movement is detected in mouseMoveEvent.
            self._pan_start = event.pos()
            self._pan_moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        # Activate right-drag pan once the cursor has moved enough
        if (event.buttons() & Qt.RightButton) and self._pan_start is not None:
            delta = event.pos() - self._pan_start
            if delta.manhattanLength() > 4:
                self._panning   = True
                self._pan_moved = True
                self.setCursor(QCursor(Qt.ClosedHandCursor))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(QCursor(Qt.ArrowCursor))
            event.accept()
            return
        if event.button() == Qt.RightButton:
            if self._panning:
                self._panning = False
                self.setCursor(QCursor(Qt.ArrowCursor))
                event.accept()
                return
            self._pan_start = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        # Suppress the context menu if this right-press became a pan drag
        if self._pan_moved:
            self._pan_moved = False
            event.accept()
            return
        super().contextMenuEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # Dark palette
    from PyQt5.QtGui import QPalette
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(17, 17, 24))
    pal.setColor(QPalette.WindowText,      QColor(220, 220, 240))
    pal.setColor(QPalette.Base,            QColor(26, 26, 42))
    pal.setColor(QPalette.AlternateBase,   QColor(30, 30, 48))
    pal.setColor(QPalette.Text,            QColor(220, 220, 240))
    pal.setColor(QPalette.Button,          QColor(40, 40, 60))
    pal.setColor(QPalette.ButtonText,      QColor(220, 220, 240))
    pal.setColor(QPalette.Highlight,       QColor(0, 120, 200))
    pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(pal)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
