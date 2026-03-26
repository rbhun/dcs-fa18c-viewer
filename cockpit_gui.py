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
import sys
import threading
from pathlib import Path

import dcs_config

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
    QTreeWidget, QTreeWidgetItem, QLineEdit
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
KNOB_COLOR     = QColor(80, 80, 100)
TOGGLE_ON      = QColor(0, 200, 80)
TOGGLE_OFF     = QColor(80, 80, 80)

CTRL_DOT_RADIUS      = 10
CTRL_DOT_EDIT_RADIUS = 14

# Control types that get a text-box widget instead of a dot
DISPLAY_TYPES = {"display"}
GAUGE_TYPES   = {"analog_gauge", "analog_dial", "fixed_step_dial"}

# Staging tray — fixed QWidget to the left of the QGraphicsView (never zooms)
TRAY_W    = 180  # pixel width of the tray panel
TRAY_HDR_H = 28  # height of the "Drag onto panel →" header strip
ITEM_H    = 36   # height per row in the tray list


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
            sock.settimeout(2.0)
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
        self.setToolTip(
            f"<b>{ctrl_id}</b><br>"
            f"{ctrl_def.get('description', '')}<br>"
            + (f"<span style='color:#7ab4e8;'>{_cat}</span>" if _cat else "")
        )

        if edit_mode:
            self.setFlags(
                QGraphicsItem.ItemIsMovable |
                QGraphicsItem.ItemIsSelectable |
                QGraphicsItem.ItemSendsGeometryChanges
            )
            self.setCursor(QCursor(Qt.SizeAllCursor))
        else:
            self.setCursor(
                QCursor(Qt.PointingHandCursor) if self._inputs else QCursor(Qt.ArrowCursor)
            )

        self._make_label()
        self._refresh()

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

    def _refresh(self):
        v = self._current_val
        t = self._ctype

        if t == "led":
            color = GREEN_ON if v else GREEN_OFF
        elif t in ("selector", "toggle_switch", "mission_computer_switch"):
            if self._max <= 1:
                color = TOGGLE_ON if v else TOGGLE_OFF
            else:
                # multi-position: colour by fraction
                frac = v / max(self._max, 1)
                r = int(TOGGLE_OFF.red()   + frac * (AMBER_ON.red()   - TOGGLE_OFF.red()))
                g = int(TOGGLE_OFF.green() + frac * (AMBER_ON.green() - TOGGLE_OFF.green()))
                b = int(TOGGLE_OFF.blue()  + frac * (AMBER_ON.blue()  - TOGGLE_OFF.blue()))
                color = QColor(r, g, b)
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
        pen_color = QColor(255, 255, 255, 180) if self._inputs else QColor(100, 100, 100, 120)
        self.setPen(QPen(pen_color, 1.5))

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
        if event.button() == Qt.LeftButton and self._inputs:
            self._send_click()
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
        self._resizing  = False
        self._resize_start_pos   = None
        self._resize_start_size  = None
        self.setZValue(2)

        self.setBrush(QBrush(QColor(10, 30, 10, 200)))
        self.setPen(QPen(QColor(0, 160, 60, 200), 1))
        _cat = ctrl_def.get("_category", "")
        self.setToolTip(
            f"<b>{ctrl_id}</b><br>"
            f"{ctrl_def.get('description', '')}<br>"
            + (f"<span style='color:#7ab4e8;'>{_cat}</span>" if _cat else "")
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
    if t == "led":
        return GREEN_OFF
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
                 controls: dict, all_positions: dict):
        super().__init__()
        self.panel_name  = panel_name
        self.image_path  = panel_image_path
        self.controls    = controls   # {ctrl_id: ctrl_def}
        self.positions   = all_positions   # shared mutable dict {ctrl_id: {x,y}}
        self.edit_mode   = False
        self._dirty      = False
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
            "Left list: amber = ≥ 50 % of controls already placed (likely belongs elsewhere)."
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
        self.search_avail.setPlaceholderText("Filter…")
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

        # Left list
        self.list_avail.clear()
        for cat in self._all_cats:
            if cat in my_set:
                continue
            if filt and filt not in cat.lower():
                continue
            owner          = self._owner(cat)
            n, placed, unp = self._cat_stats(cat)
            pct            = placed / n if n else 0.0

            if n == 0 or pct < 0.5:
                pct_tag = ""
            else:
                pct_tag = f"  {placed}/{n} placed"

            item = QListWidgetItem(f"{cat}  [{owner}]  ({n}){pct_tag}")
            item.setData(Qt.UserRole, cat)

            if owner == "— unassigned —":
                item.setForeground(QColor(TEXT_DIM))
            elif pct >= 0.5:
                # Most controls already placed — dim amber to signal "probably not needed"
                item.setForeground(QColor(160, 110, 30))
            self.list_avail.addItem(item)

        # Right tree — remember which categories were expanded
        expanded = set()
        root = self.tree_mine.invisibleRootItem()
        for i in range(root.childCount()):
            node = root.child(i)
            if node.isExpanded():
                expanded.add(node.data(0, Qt.UserRole))

        self.tree_mine.clear()
        total_ctrl = 0
        total_unplaced = 0

        for cat in sorted(self._my_cats):
            controls = self.bios_defs.get(cat, {})
            n, _placed, unplaced = self._cat_stats(cat)
            total_ctrl    += n
            total_unplaced += unplaced

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

            # Child items — one per control
            for ctrl_id, ctrl_def in sorted(controls.items()):
                placed   = ctrl_id in self._positions
                ctype    = ctrl_def.get("control_type", "?")
                has_in   = bool(ctrl_def.get("inputs"))
                desc     = ctrl_def.get("description", "")[:50]
                io_tag   = "in+out" if has_in else "out"
                status   = "●" if placed else "○"
                child_text = f"  {status}  {ctrl_id:<32}  [{ctype} / {io_tag}]  {desc}"
                child = QTreeWidgetItem([child_text])
                child.setData(0, Qt.UserRole, None)   # not a category
                child.setForeground(0, self._PLACED_COLOR if placed else self._UNPLACED_COLOR)
                hdr_item.addChild(child)

            self.tree_mine.addTopLevelItem(hdr_item)
            if cat in expanded:
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
# Connection dialog
# ═════════════════════════════════════════════════════════════════════════════

class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to DCS-BIOS")
        self.setFixedSize(380, 180)
        self.setStyleSheet(f"background:{PANEL_BG}; color:{TEXT_PRIMARY};")
        fl = QFormLayout(self)
        fl.setContentsMargins(16, 16, 16, 16)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["UDP (default — DCS running on same PC)", "Serial (ESP32/Arduino)"])
        fl.addRow("Mode:", self.mode_combo)

        self.port_label = QLabel("Serial port:")
        self.port_edit  = QComboBox()
        self.port_edit.setEditable(True)
        self._refresh_ports()
        fl.addRow(self.port_label, self.port_edit)

        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(9600, 2000000)
        self.baud_spin.setValue(250000)
        self.baud_label = QLabel("Baud rate:")
        fl.addRow(self.baud_label, self.baud_spin)

        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed(0)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        fl.addRow(bb)

    def _refresh_ports(self):
        try:
            import serial.tools.list_ports
            for pt in serial.tools.list_ports.comports():
                self.port_edit.addItem(pt.device)
        except Exception:
            pass

    def _mode_changed(self, idx: int):
        serial_mode = idx == 1
        self.port_label.setVisible(serial_mode)
        self.port_edit.setVisible(serial_mode)
        self.baud_label.setVisible(serial_mode)
        self.baud_spin.setVisible(serial_mode)

    def get_params(self):
        if self.mode_combo.currentIndex() == 0:
            return "udp", "", 250000
        return "serial", self.port_edit.currentText(), self.baud_spin.value()


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
        self._layout_data: dict  = {}
        self._categories: dict   = {}
        self._bios_defs: dict    = {}
        self._ctrl_positions: dict = {}
        self._panel_views: dict[str, PanelView] = {}

        self._load_configs()
        self._init_ui()
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

        self.conn_label = QLabel("● Not connected")
        self.conn_label.setStyleSheet("color:#ff4444; font-size:12px;")
        tl.addWidget(self.conn_label)

        self.btn_connect = QPushButton("Connect DCS-BIOS")
        self.btn_connect.setStyleSheet(_btn("#1a5a1a"))
        self.btn_connect.clicked.connect(self._connect_dialog)
        tl.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setStyleSheet(_btn("#5a1a1a"))
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._disconnect)
        tl.addWidget(self.btn_disconnect)

        vl.addWidget(topbar)

        # Stacked pages
        self.stack = QStackedWidget()
        vl.addWidget(self.stack, 1)

        # Page 0: no layout warning
        if not self._layout_data:
            self._build_no_layout_page()
        else:
            self._build_cockpit_map_page()

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

    # ── DCS-BIOS connection ───────────────────────────────────────────────────

    def _connect_dialog(self):
        dlg = ConnectDialog(self)
        dlg.setStyleSheet(f"QDialog {{ background:{PANEL_BG}; color:{TEXT_PRIMARY}; }}")
        if dlg.exec_() != QDialog.Accepted:
            return
        mode, port, baud = dlg.get_params()
        self._start_reader(mode, port, baud)

    def _start_reader(self, mode: str, port: str, baud: int):
        self._disconnect()
        t = BiosReaderThread(mode=mode, port=port, baud=baud)
        t.sig.connected.connect(self._on_connected)
        t.sig.disconnected.connect(self._on_disconnected)
        t.sig.state_changed.connect(self._on_state_changed)
        t.sig.error.connect(self._on_error)
        t.start()
        self._bios_thread = t

    def _disconnect(self):
        if self._bios_thread:
            self._bios_thread.stop()
            self._bios_thread.wait(2000)
            self._bios_thread = None
        self._on_disconnected()

    def _on_connected(self, desc: str):
        self.conn_label.setText(f"● {desc}")
        self.conn_label.setStyleSheet(f"color:#44ff44; font-size:12px;")
        self.btn_disconnect.setEnabled(True)
        self.btn_connect.setEnabled(False)
        self.status.showMessage(f"Connected: {desc}")

    def _on_disconnected(self):
        self.conn_label.setText("● Not connected")
        self.conn_label.setStyleSheet("color:#ff4444; font-size:12px;")
        self.btn_disconnect.setEnabled(False)
        self.btn_connect.setEnabled(True)

    def _on_state_changed(self, address: int, value: int):
        # Route to whichever panel view is currently visible
        cur = self.stack.currentWidget()
        if isinstance(cur, PanelView):
            cur.update_control(address, value)

    def _on_error(self, msg: str):
        self.status.showMessage(f"BIOS error: {msg}", 5000)

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
            self.status.showMessage(
                f"Panel: {cur.panel_name}  |  "
                f"{pos}/{n} controls positioned  |  "
                f"DCS-BIOS: {'connected' if self._bios_thread else 'not connected'}"
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
