#!/usr/bin/env python3
"""
Layout Wizard — drag each panel cutout to its correct position on the full cockpit image.
Run this once, then save.  Positions are written to config/panel_layout.json.

Panel list is read dynamically from config/panel_categories.json — no hardcoded filenames.
To add, rename, or split a panel: edit panel_categories.json and re-run the wizard.
"""

import json
import sys

from pathlib import Path
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import (
    QPixmap, QPainter, QColor, QPen, QFont, QBrush, QCursor
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QGraphicsScene, QGraphicsView,
    QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsTextItem,
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QSplitter, QMessageBox,
    QStatusBar, QFrame
)

import dcs_config
from dcs_config import LAYOUT_FILE, CONFIG_DIR

LABEL_COLOR   = QColor(0, 200, 255, 200)
BORDER_COLOR  = QColor(0, 200, 255, 255)
HOVER_COLOR   = QColor(255, 200, 0, 200)
SELECT_COLOR  = QColor(255, 80, 80, 220)


class PanelItem(QGraphicsPixmapItem):
    """A draggable semi-transparent panel overlay on the cockpit image."""

    def __init__(self, name: str, pixmap: QPixmap, wizard, is_categorized: bool = True):
        super().__init__(pixmap)
        self.panel_name     = name
        self.wizard         = wizard
        self.is_categorized = is_categorized
        self.setOpacity(0.55)
        self.setFlags(
            QGraphicsPixmapItem.ItemIsMovable |
            QGraphicsPixmapItem.ItemIsSelectable |
            QGraphicsPixmapItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(QCursor(Qt.SizeAllCursor))
        self._make_label()

    def _make_label(self):
        # Uncategorized panels get an orange label + "★ NEW" prefix
        if self.is_categorized:
            text  = self.panel_name
            color = QColor(255, 255, 0)
        else:
            text  = f"★ NEW  {self.panel_name}"
            color = QColor(255, 160, 0)

        self.label = QGraphicsTextItem(text, self)
        self.label.setDefaultTextColor(color)
        font = QFont("Arial", 0)
        font.setPixelSize(max(12, self.pixmap().height() // 20))
        font.setBold(True)
        self.label.setFont(font)
        self.label.setPos(4, 4)

    def hoverEnterEvent(self, event):
        self.setOpacity(0.80)
        self.wizard.status.showMessage(
            f"{self.panel_name}  —  x={int(self.x())}  y={int(self.y())}  "
            f"w={self.pixmap().width()}  h={self.pixmap().height()}"
        )
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setOpacity(0.55 if not self.isSelected() else 0.75)
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsPixmapItem.ItemPositionHasChanged:
            self.wizard.status.showMessage(
                f"{self.panel_name}  —  x={int(self.x())}  y={int(self.y())}  "
                f"w={self.pixmap().width()}  h={self.pixmap().height()}"
            )
            self.wizard.mark_dirty()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event):
        self.wizard.select_panel(self.panel_name)
        super().mouseDoubleClickEvent(event)


class CockpitScene(QGraphicsScene):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard


class CockpitView(QGraphicsView):
    _ZOOM_MIN = 0.05
    _ZOOM_MAX = 8.0

    def __init__(self, scene, wizard):
        super().__init__(scene)
        self.wizard = wizard
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._panning = False
        self._pan_start = None

    def wheelEvent(self, event):
        angle = event.angleDelta().y()
        if angle == 0:
            angle = event.pixelDelta().y() * 3
        if angle == 0:
            event.ignore()
            return
        angle = max(-120, min(120, angle))
        factor = 1.0 + angle / 1200.0

        current_scale = self.transform().m11()
        new_scale = current_scale * factor
        if new_scale < self._ZOOM_MIN or new_scale > self._ZOOM_MAX:
            event.accept()
            return

        self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = False
            self.setCursor(QCursor(Qt.ArrowCursor))
            event.accept()
            return
        super().mouseReleaseEvent(event)


class LayoutWizard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DCS Cockpit — Layout Wizard")
        self.resize(1400, 900)
        self._dirty = False
        self.panel_items: dict[str, PanelItem] = {}
        self._init_ui()
        self._load_full_image()
        self._load_panels()
        self._restore_saved_positions()

    # ── UI setup ──────────────────────────────────────────────────────────────

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Top toolbar
        toolbar = QFrame()
        toolbar.setFixedHeight(48)
        toolbar.setStyleSheet("background:#1e1e2e; border-bottom:1px solid #333;")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(8, 4, 8, 4)

        title = QLabel("Layout Wizard — drag panels to their correct positions")
        title.setStyleSheet("color:#aaa; font-size:13px;")
        tl.addWidget(title)
        tl.addStretch()

        hint = QLabel("Scroll wheel = zoom  |  Middle-drag = pan  |  Left-drag panel = move")
        hint.setStyleSheet("color:#666; font-size:11px;")
        tl.addWidget(hint)
        tl.addSpacing(16)

        btn_reset = QPushButton("Reset All Positions")
        btn_reset.setStyleSheet(self._btn_style("#555"))
        btn_reset.clicked.connect(self._reset_positions)
        tl.addWidget(btn_reset)

        self.btn_save = QPushButton("Save Layout")
        self.btn_save.setStyleSheet(self._btn_style("#2a7a2a"))
        self.btn_save.clicked.connect(self._save_layout)
        tl.addWidget(self.btn_save)

        root_layout.addWidget(toolbar)

        # Splitter: canvas | sidebar
        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        # Canvas
        self.scene = CockpitScene(self)
        self.view = CockpitView(self.scene, self)
        self.view.setStyleSheet("background:#111;")
        splitter.addWidget(self.view)

        # Sidebar
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet("background:#1e1e2e; border-left:1px solid #333;")
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(8, 8, 8, 8)

        lbl = QLabel("Panels")
        lbl.setStyleSheet("color:#ccc; font-weight:bold; font-size:13px;")
        sl.addWidget(lbl)

        hint2 = QLabel("Click to jump to panel\nDouble-click panel in canvas\nto highlight it here")
        hint2.setStyleSheet("color:#666; font-size:10px;")
        sl.addWidget(hint2)

        self.panel_list = QListWidget()
        self.panel_list.setStyleSheet("""
            QListWidget { background:#252535; color:#ccc; border:none; font-size:12px; }
            QListWidget::item:selected { background:#2a4a7a; color:white; }
            QListWidget::item:hover { background:#2a3a5a; }
        """)
        self.panel_list.currentTextChanged.connect(self._jump_to_panel)
        sl.addWidget(self.panel_list, 1)

        btn_fit = QPushButton("Fit All in View")
        btn_fit.setStyleSheet(self._btn_style("#333"))
        btn_fit.clicked.connect(self._fit_view)
        sl.addWidget(btn_fit)

        splitter.addWidget(sidebar)
        splitter.setSizes([1180, 220])

        self.status = QStatusBar()
        self.status.setStyleSheet("background:#111; color:#888; font-size:11px;")
        self.setStatusBar(self.status)
        self.status.showMessage("Ready — drag panels into position, then click Save Layout")

    @staticmethod
    def _btn_style(bg: str) -> str:
        return (
            f"QPushButton {{ background:{bg}; color:white; border:none; "
            f"padding:6px 14px; border-radius:4px; font-size:12px; }}"
            f"QPushButton:hover {{ background:#555; }}"
        )

    # ── Image and panel loading ───────────────────────────────────────────────

    def _load_full_image(self):
        img_path = dcs_config.resolve_cockpit_image()
        if img_path is None:
            QMessageBox.critical(
                self, "Cockpit image not found",
                "Could not locate the full cockpit background image.\n\n"
                "Set 'cockpit_image' in config/app_config.json to the path of your image\n"
                "(relative to the project root, e.g. \"panel pics/my_cockpit.jpg\")."
            )
            sys.exit(1)
        # Persist auto-detected path so future runs are instant
        rel = str(img_path.relative_to(dcs_config.BASE_DIR))
        dcs_config.save_cockpit_image_to_config(rel)
        self.status.showMessage(f"Cockpit image: {rel}")
        pix = QPixmap(str(img_path))
        bg = self.scene.addPixmap(pix)
        bg.setZValue(-1)
        self.scene.setSceneRect(bg.boundingRect())
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def _load_panels(self):
        pdir = dcs_config.panels_dir()
        new_count = 0

        for name, is_categorized in dcs_config.all_panel_images():
            path = pdir / name
            if not path.exists():
                print(f"  WARNING: panel image not found: {path}")
                continue
            pix  = QPixmap(str(path))
            item = PanelItem(name, pix, self, is_categorized=is_categorized)
            item.setPos(10, 10 + len(self.panel_items) * 30)
            item.setZValue(len(self.panel_items) + 1)
            self.scene.addItem(item)
            self.panel_items[name] = item

            # Sidebar list entry — orange for uncategorized
            lw_item = QListWidgetItem(("★ " if not is_categorized else "") + name)
            if not is_categorized:
                lw_item.setForeground(QColor(255, 160, 0))
                new_count += 1
            self.panel_list.addItem(lw_item)

        if new_count:
            self.status.showMessage(
                f"{new_count} new image{'s' if new_count > 1 else ''} found in panels folder "
                f"(marked ★). Add {'them' if new_count > 1 else 'it'} to "
                "panel_categories.json to assign DCS-BIOS controls."
            )

    def _restore_saved_positions(self):
        if not LAYOUT_FILE.exists():
            self.status.showMessage(
                "No saved layout found — panels stacked at left edge. Drag them into position."
            )
            return
        with open(LAYOUT_FILE) as f:
            layout = json.load(f)
        for name, pos in layout.items():
            if name in self.panel_items:
                self.panel_items[name].setPos(pos["x"], pos["y"])
        self.status.showMessage(f"Loaded existing layout from {LAYOUT_FILE.name}")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _fit_view(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def _jump_to_panel(self, name: str):
        if name in self.panel_items:
            item = self.panel_items[name]
            self.view.ensureVisible(item)
            self.scene.clearSelection()
            item.setSelected(True)

    def select_panel(self, name: str):
        for i in range(self.panel_list.count()):
            if self.panel_list.item(i).text() == name:
                self.panel_list.setCurrentRow(i)
                break

    def mark_dirty(self):
        if not self._dirty:
            self._dirty = True
            self.btn_save.setStyleSheet(self._btn_style("#7a2a2a"))
            self.btn_save.setText("Save Layout  *")

    def _reset_positions(self):
        reply = QMessageBox.question(
            self, "Reset positions",
            "This will move all panels back to the default stack. Continue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        for i, (name, item) in enumerate(self.panel_items.items()):
            item.setPos(10, 10 + i * 30)
        self.mark_dirty()

    def _save_layout(self):
        CONFIG_DIR.mkdir(exist_ok=True)
        layout = {}
        for name, item in self.panel_items.items():
            layout[name] = {
                "x": round(item.x()),
                "y": round(item.y()),
                "w": item.pixmap().width(),
                "h": item.pixmap().height(),
            }
        with open(LAYOUT_FILE, "w") as f:
            json.dump(layout, f, indent=2)
        self._dirty = False
        self.btn_save.setStyleSheet(self._btn_style("#2a7a2a"))
        self.btn_save.setText("Save Layout")
        self.status.showMessage(f"Saved to {LAYOUT_FILE}  ({len(layout)} panels)")

    def closeEvent(self, event):
        if self._dirty:
            reply = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved positions. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if reply == QMessageBox.Save:
                self._save_layout()
            elif reply == QMessageBox.Cancel:
                event.ignore()
                return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = LayoutWizard()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
