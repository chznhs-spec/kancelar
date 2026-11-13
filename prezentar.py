#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prezentář — editor prezentací ze sady VLČR Kancelář.

Prezentace se ukládají do vlastního binárního formátu .vlcr (VLCRPREZ),
nikoli jako ZIP, HTML nebo běžný PPTX soubor.

Požadavky: Python 3.9+ a PyQt5 >= 5.15
Spuštění: python prezentar.py [prezentace.vlcr]
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import os
import struct
import sys
import tempfile
import unicodedata
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, QSize, QRectF, QPointF, QBuffer, QIODevice, QPropertyAnimation, QByteArray, QProcess, pyqtSignal
from PyQt5.QtGui import (
    QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QBrush,
    QPixmap, QTextCursor, QTextCharFormat, QTransform
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QAction, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QPushButton, QToolBar, QToolButton,
    QSplitter, QListWidget, QListWidgetItem, QGraphicsView, QGraphicsScene,
    QGraphicsTextItem, QGraphicsPixmapItem, QGraphicsRectItem,
    QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsItem, QComboBox,
    QFontComboBox, QSpinBox, QPlainTextEdit, QColorDialog, QFileDialog,
    QMessageBox, QInputDialog, QStatusBar, QSizePolicy, QFrame, QGroupBox,
    QCheckBox, QShortcut
)
from PyQt5.QtPrintSupport import QPrinter

# =============================================================================
# VZHLED — hlavní hodnoty vzhledu lze měnit na jednom místě.
# =============================================================================
class Theme:
    WINDOW = "#F3F3F3"
    SURFACE = "#FFFFFF"
    WORKSPACE = "#E6E6E6"
    ACCENT = "#2D89EF"
    ACCENT_DARK = "#1769AA"
    TEXT = "#202020"
    MUTED = "#666666"
    BORDER = "#D0D0D0"
    TOOLBAR = "#FAFAFA"
    SELECTION = "#CDE5FF"
    FONT = "Segoe UI"
    DEFAULT_FONT = "Aptos"
    WINDOW_W = 1420
    WINDOW_H = 900
    MIN_W = 1050
    MIN_H = 680

APP_NAME = "Prezentář"
APP_VERSION = "0.3.2"
APP_ID = "prezentar"
MAGIC = b"VLCRPREZ"                 # 8 bytes — identifikátor vlastního kontejneru
FORMAT_VERSION = 1
HEADER_SIZE = 14                    # magic (8) + version (2) + metadata length (4)
MAX_FILE_BYTES = 250 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_ASSETS_BYTES = 150 * 1024 * 1024
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(APP_DIR, "loga", "prezenter.png")

APP_KEYS = ("app", "application", "aplikace", "editor", "typ", "type", "format", "kind")
APP_ALIASES = {"prezentar", "presentation", "prezentace", "slides", "pptx"}


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _norm(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(c for c in value if not unicodedata.combining(c))


def _metadata_values() -> dict:
    return {
        "app": "prezentar",
        "application": "presentation",
        "aplikace": "prezentace",
        "type": "slides",
        "format": "pptx",
        "container_format": "VLCRPREZ",
        "container_version": FORMAT_VERSION,
        "generator": f"{APP_NAME} {APP_VERSION}",
    }


def _is_our_metadata(meta: dict) -> bool:
    if not isinstance(meta, dict):
        return False
    inner = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else meta
    return any(_norm(inner.get(key, "")) in APP_ALIASES for key in APP_KEYS)


def read_presentation(path: str) -> Tuple[dict, List[dict], Dict[str, bytes], int, int]:
    path = os.path.abspath(path)
    if not path.lower().endswith(".vlcr"):
        raise ValueError("Prezentář otevírá pouze soubory s příponou .vlcr.")
    size = os.path.getsize(path)
    if size < HEADER_SIZE or size > MAX_FILE_BYTES:
        raise ValueError("Soubor je prázdný nebo překračuje povolenou velikost.")
    with open(path, "rb") as handle:
        header = handle.read(HEADER_SIZE)
        if header[:8] != MAGIC:
            raise ValueError("Toto není binární prezentace Prezentáře (.vlcr).")
        version, meta_len = struct.unpack(">HI", header[8:14])
        if version != FORMAT_VERSION:
            raise ValueError(f"Tato verze prezentace ({version}) není podporována.")
        if meta_len <= 0 or meta_len > MAX_METADATA_BYTES:
            raise ValueError("Hlavička prezentace obsahuje neplatnou délku metadat.")
        meta_raw = handle.read(meta_len)
        if len(meta_raw) != meta_len:
            raise ValueError("Soubor je poškozený — neúplná metadata.")
        meta = json.loads(meta_raw.decode("utf-8"))
        if not isinstance(meta, dict) or not _is_our_metadata(meta):
            raise ValueError("Soubor nepatří Prezentáři.")
        compressed = handle.read(MAX_FILE_BYTES + 1)
    decoder = zlib.decompressobj()
    payload_raw = decoder.decompress(compressed, MAX_FILE_BYTES + 1)
    if len(payload_raw) > MAX_FILE_BYTES or decoder.unconsumed_tail:
        raise ValueError("Rozbalený obsah prezentace je příliš velký.")
    payload_raw += decoder.flush()
    if len(payload_raw) > MAX_FILE_BYTES or not decoder.eof:
        raise ValueError("Obsah prezentace je poškozený nebo neúplný.")
    payload = json.loads(payload_raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("slides"), list):
        raise ValueError("Prezentace nemá platnou strukturu snímků.")
    slides = payload["slides"]
    if not slides:
        slides = [_blank_slide(1)]
    width = int(payload.get("width", 1920))
    height = int(payload.get("height", 1080))
    assets: Dict[str, bytes] = {}
    asset_data = payload.get("assets", {})
    total_assets = 0
    if isinstance(asset_data, dict):
        for name, encoded in asset_data.items():
            if not isinstance(name, str) or not isinstance(encoded, str):
                continue
            try:
                raw = base64.b64decode(encoded.encode("ascii"), validate=True)
            except Exception:
                continue
            total_assets += len(raw)
            if total_assets > MAX_ASSETS_BYTES:
                raise ValueError("Vložené obrázky překračují povolenou velikost.")
            assets[name] = raw
    return meta, slides, assets, width, height


def write_presentation(path: str, slides: List[dict], assets: Dict[str, bytes], title: str,
                       width: int, height: int, created: Optional[str] = None) -> dict:
    path = os.path.abspath(path)
    if not path.lower().endswith(".vlcr"):
        path += ".vlcr"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    title = (title or "Nepojmenovaná prezentace").strip()
    meta = _metadata_values()
    meta.update({
        "title": title,
        "created": created or _now(),
        "modified": _now(),
        "slide_count": len(slides),
        "content_encoding": "zlib+json",
    })
    meta_raw = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(meta_raw) > MAX_METADATA_BYTES:
        raise ValueError("Metadata prezentace jsou příliš rozsáhlá.")
    serial_assets = {name: base64.b64encode(data).decode("ascii") for name, data in assets.items()}
    payload_dict = {
        "width": width,
        "height": height,
        "slides": slides,
        "assets": serial_assets
    }
    payload = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("Prezentace je příliš velká.")
    compressed = zlib.compress(payload, level=8)
    header = MAGIC + struct.pack(">HI", FORMAT_VERSION, len(meta_raw)) + meta_raw
    fd, tmp = tempfile.mkstemp(prefix=".prezentar-", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(header)
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return meta


def _blank_slide(number: int) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": f"Snímek {number}",
        "background": "#FFFFFF",
        "transition": "none",
        "notes": "",
        "elements": [
            {"type": "text", "x": 100, "y": 95, "w": 1580, "text": "Nadpis snímku",
             "font_family": Theme.DEFAULT_FONT, "font_size": 52, "color": "#18395B", "bold": True},
            {"type": "text", "x": 120, "y": 230, "w": 1500, "text": "Dvojklikem upravte text.\nZ panelu Vložení přidejte obrázky a tvary.",
             "font_family": Theme.DEFAULT_FONT, "font_size": 30, "color": "#333333", "bold": False},
        ],
    }


def _render_slide_pixmap(slide: dict, assets: Dict[str, bytes], slide_w: int, slide_h: int, size: QSize) -> QPixmap:
    scene = QGraphicsScene(0, 0, slide_w, slide_h)
    slide_color = QColor(slide.get("background", "#FFFFFF"))
    scene.setBackgroundBrush(slide_color)
    bg_item = QGraphicsRectItem(QRectF(0, 0, slide_w, slide_h))
    bg_item.setPen(QPen(Qt.NoPen))
    bg_item.setBrush(QBrush(slide_color))
    bg_item.setZValue(-100000)
    scene.addItem(bg_item)
    for data in slide.get("elements", []):
        kind = data.get("type")
        x, y = float(data.get("x", 0)), float(data.get("y", 0))
        if kind == "text":
            item = QGraphicsTextItem()
            if data.get("html"):
                item.setHtml(data["html"])
            else:
                item.setPlainText(str(data.get("text", "")))
                f = QFont(str(data.get("font_family", Theme.DEFAULT_FONT)), int(data.get("font_size", 30)))
                f.setBold(bool(data.get("bold", False)))
                item.setFont(f)
                item.setDefaultTextColor(QColor(data.get("color", "#222222")))
            item.setTextWidth(float(data.get("w", 800)))
            item.setTransform(QTransform.fromScale(float(data.get("scale_x", 1.0)),
                                                    float(data.get("scale_y", 1.0))))
            item.setPos(x, y)
            scene.addItem(item)
        elif kind == "image":
            raw = assets.get(str(data.get("asset", "")))
            if raw:
                pix = QPixmap()
                if pix.loadFromData(raw):
                    item = QGraphicsPixmapItem(pix)
                    sx = float(data.get("scale_x", float(data.get("w", pix.width())) / max(1, pix.width())))
                    sy = float(data.get("scale_y", float(data.get("h", pix.height())) / max(1, pix.height())))
                    item.setTransform(QTransform.fromScale(sx, sy))
                    item.setPos(x, y)
                    scene.addItem(item)
        elif kind in ("rectangle", "ellipse"):
            rect = QRectF(0, 0, float(data.get("w", 300)), float(data.get("h", 150)))
            item = QGraphicsEllipseItem(rect) if kind == "ellipse" else QGraphicsRectItem(rect)
            item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 3))))
            item.setBrush(QBrush(QColor(data.get("fill", "#DCEEFF"))))
            item.setTransform(QTransform.fromScale(float(data.get("scale_x", 1.0)),
                                                   float(data.get("scale_y", 1.0))))
            item.setPos(x, y)
            scene.addItem(item)
        elif kind == "line":
            item = QGraphicsLineItem(0, 0, float(data.get("w", 300)), float(data.get("h", 0)))
            item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 3))))
            item.setTransform(QTransform.fromScale(float(data.get("scale_x", 1.0)),
                                                   float(data.get("scale_y", 1.0))))
            item.setPos(x, y)
            scene.addItem(item)
    pix = QPixmap(size)
    pix.fill(QColor(slide.get("background", "#FFFFFF")))
    painter = QPainter(pix)
    scene.render(painter, QRectF(0, 0, size.width(), size.height()), scene.sceneRect())
    painter.end()
    return pix


# =============================================================================
# INTEGRACE PRŮZKUMNÍKU VLČR
# =============================================================================
class ExplorerOpenDialog(QDialog):
    """Výběr prezentace přes samostatný executable VLČR Průzkumníka."""
    def __init__(self, start_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Otevřít prezentaci — Průzkumník VLČR")
        self.setMinimumSize(520, 190)
        self.resize(620, 220)
        self.setModal(True)
        self.selected_path: Optional[str] = None
        self._process = QProcess(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        title = QLabel("Vyberte prezentaci v Průzkumníku VLČR")
        title.setStyleSheet(f"color:{Theme.ACCENT}; font-size:20px;")
        root.addWidget(title)

        self.status = QLabel("Spouštím Průzkumníka…")
        self.status.setWordWrap(True)
        root.addWidget(self.status, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = QPushButton("Zrušit")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        self.setStyleSheet("QDialog{background:#F3F3F3;} QPushButton{padding:6px 18px;}")

        # Průzkumník je distribuován v podsložce dist/apps.
        # APP_DIR je adresář spuštěného Prezentáře (typicky kancelar/dist).
        # Průzkumník je ve stejné složce jako Prezentář.
        executable_name = "pruzkumnik" if os.name == "nt" else "pruzkumnik"

        self.explorer_dir = APP_DIR
        self.explorer_path = os.path.join(self.explorer_dir, executable_name)

        if not os.path.isfile(self.explorer_path):
            self.status.setText(
                "Průzkumník nebyl nalezen.\n"
                f"Očekávané umístění:\n{self.explorer_path}"
            )
            self.close_button.setText("Zavřít")
            return

        if os.name != "nt" and not os.access(self.explorer_path, os.X_OK):
            self.status.setText(
                "Průzkumník nemá právo ke spuštění.\n"
                f"V terminálu spusťte: chmod +x {self.explorer_path}"
            )
            self.close_button.setText("Zavřít")
            return

        self._process.readyReadStandardOutput.connect(self._read_process_output)
        self._process.finished.connect(self._process_finished)
        self._process.errorOccurred.connect(self._process_error)

        args = ["--select"]
        if start_path:
            args.extend(["--start-path", os.path.abspath(os.path.expanduser(start_path))])

        self._process.setWorkingDirectory(self.explorer_dir)
        self._process.start(self.explorer_path, args)

    def _read_process_output(self):
        # Výstup se načítá průběžně, finální cesta se vyhodnotí po ukončení procesu.
        pass

    def _process_finished(self, exit_code, exit_status):
        output = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        if exit_code == 0 and output:
            selected = output.splitlines()[-1].strip()
            if os.path.isfile(selected) and selected.lower().endswith(".vlcr"):
                try:
                    read_presentation(selected)
                    self.selected_path = os.path.abspath(selected)
                    self.accept()
                    return
                except Exception as exc:
                    self.status.setText(f"Vybraný soubor není platná prezentace:\n{exc}")
                    self.close_button.setText("Zavřít")
                    return
        if exit_code == 0:
            self.status.setText("Nebyl vybrán žádný soubor.")
        else:
            self.status.setText(f"Průzkumník byl ukončen s chybovým kódem {exit_code}.")
        self.close_button.setText("Zavřít")

    def _process_error(self, error):
        self.status.setText(f"Průzkumníka se nepodařilo spustit:\n{self._process.errorString()}")
        self.close_button.setText("Zavřít")

    def reject(self):
        if self._process.state() != QProcess.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(1000):
                self._process.kill()
        super().reject()

    def closeEvent(self, event):
        if self._process.state() != QProcess.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(1000):
                self._process.kill()
        super().closeEvent(event)


# =============================================================================
# GRAFICKÉ OBJEKTY S MOŽNOSTÍ RESIZOVÁNÍ PŘETAŽENÍM ZA OKRAJE
# =============================================================================
def _handle_mouse_press(item, event, super_func):
    if event.button() == Qt.LeftButton:
        p = event.pos()
        br = item.boundingRect()
        if p.x() >= br.right() - 18 or p.y() >= br.bottom() - 18:
            item._resizing = True
            item._resize_start_pos = event.scenePos()
            tr = item.transform()
            item._start_sx = tr.m11() if tr.m11() != 0 else 1.0
            item._start_sy = tr.m22() if tr.m22() != 0 else 1.0
            item._orig_w = max(10.0, br.width())
            item._orig_h = max(10.0, br.height())
            event.accept()
            return
    super_func(event)

def _handle_mouse_move(item, event, super_func):
    if getattr(item, "_resizing", False):
        delta = event.scenePos() - item._resize_start_pos
        current_w = item._orig_w * item._start_sx
        current_h = item._orig_h * item._start_sy
        new_w = max(20.0, current_w + delta.x())
        new_h = max(20.0, current_h + delta.y())

        sx = max(0.05, new_w / item._orig_w)
        sy = max(0.05, new_h / item._orig_h)
        item.setTransform(QTransform.fromScale(sx, sy))

        if item.scene():
            for v in item.scene().views():
                if hasattr(v, "selection_changed"):
                    v.selection_changed.emit()
        event.accept()
        return
    super_func(event)

def _handle_mouse_release(item, event, super_func):
    if getattr(item, "_resizing", False):
        item._resizing = False
        event.accept()
        if item.scene():
            for v in item.scene().views():
                if hasattr(v, "changed"):
                    v.changed.emit()
        return
    super_func(event)


class ResizableTextItem(QGraphicsTextItem):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._resizing = False
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

    def mouseDoubleClickEvent(self, event):
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        super().mouseDoubleClickEvent(event)
        self.setFocus(Qt.MouseFocusReason)

    def focusOutEvent(self, event):
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        _handle_mouse_press(self, event, super().mousePressEvent)

    def mouseMoveEvent(self, event):
        _handle_mouse_move(self, event, super().mouseMoveEvent)

    def mouseReleaseEvent(self, event):
        _handle_mouse_release(self, event, super().mouseReleaseEvent)


class ResizablePixmapItem(QGraphicsPixmapItem):
    def __init__(self, pixmap=None, parent=None):
        super().__init__(pixmap, parent) if pixmap else super().__init__(parent)
        self._resizing = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)

    def mousePressEvent(self, event):
        _handle_mouse_press(self, event, super().mousePressEvent)

    def mouseMoveEvent(self, event):
        _handle_mouse_move(self, event, super().mouseMoveEvent)

    def mouseReleaseEvent(self, event):
        _handle_mouse_release(self, event, super().mouseReleaseEvent)


class ResizableRectItem(QGraphicsRectItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resizing = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)

    def mousePressEvent(self, event):
        _handle_mouse_press(self, event, super().mousePressEvent)

    def mouseMoveEvent(self, event):
        _handle_mouse_move(self, event, super().mouseMoveEvent)

    def mouseReleaseEvent(self, event):
        _handle_mouse_release(self, event, super().mouseReleaseEvent)


class ResizableEllipseItem(QGraphicsEllipseItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resizing = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)

    def mousePressEvent(self, event):
        _handle_mouse_press(self, event, super().mousePressEvent)

    def mouseMoveEvent(self, event):
        _handle_mouse_move(self, event, super().mouseMoveEvent)

    def mouseReleaseEvent(self, event):
        _handle_mouse_release(self, event, super().mouseReleaseEvent)


class ResizableLineItem(QGraphicsLineItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resizing = False
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)

    def mousePressEvent(self, event):
        _handle_mouse_press(self, event, super().mousePressEvent)

    def mouseMoveEvent(self, event):
        _handle_mouse_move(self, event, super().mouseMoveEvent)

    def mouseReleaseEvent(self, event):
        _handle_mouse_release(self, event, super().mouseReleaseEvent)


# =============================================================================
# PLÁTNO SNÍMKU
# =============================================================================
class SlideCanvas(QGraphicsView):
    changed = pyqtSignal()
    selection_changed = pyqtSignal()

    def __init__(self, slide_w=1920, slide_h=1080, parent=None):
        self.slide_w = slide_w
        self.slide_h = slide_h
        workspace_rect = QRectF(-260, -180, slide_w + 520, slide_h + 360)
        self._scene = QGraphicsScene(workspace_rect)
        super().__init__(self._scene, parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QColor(Theme.WORKSPACE))
        self.setAlignment(Qt.AlignCenter)
        self._scene.setBackgroundBrush(QColor(Theme.WORKSPACE))
        self._scene.selectionChanged.connect(self.selection_changed.emit)
        self._scene.changed.connect(lambda *_: self.changed.emit())
        self._loading = False
        self._fit = True
        self._slide_background_item = None

    def update_resolution(self, w: int, h: int):
        if self.slide_w == w and self.slide_h == h:
            return
        self.slide_w = w
        self.slide_h = h
        workspace_rect = QRectF(-260, -180, w + 520, h + 360)
        self._scene.setSceneRect(workspace_rect)
        if self._slide_background_item is not None:
            self._slide_background_item.setRect(QRectF(0, 0, w, h))
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self.changed.emit()

    @staticmethod
    def _set_saved_scale(item, data, legacy_scale_x=1.0, legacy_scale_y=1.0):
        sx = float(data.get("scale_x", legacy_scale_x))
        sy = float(data.get("scale_y", legacy_scale_y))
        sx = max(0.01, min(sx, 100.0))
        sy = max(0.01, min(sy, 100.0))
        item.setTransform(QTransform.fromScale(sx, sy))

    def _create_slide_background(self, color):
        background = QGraphicsRectItem(QRectF(0, 0, self.slide_w, self.slide_h))
        background.setPen(QPen(QColor("#B7B7B7"), 1))
        background.setBrush(QBrush(QColor(color)))
        background.setZValue(-100000)
        background.setData(0, "__slide_background__")
        background.setFlag(QGraphicsItem.ItemIsSelectable, False)
        background.setFlag(QGraphicsItem.ItemIsMovable, False)
        self._scene.addItem(background)
        self._slide_background_item = background

    def slide_background_color(self) -> QColor:
        if self._slide_background_item is not None:
            return self._slide_background_item.brush().color()
        return QColor("#FFFFFF")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def load_slide(self, slide: dict, assets: Dict[str, bytes]):
        self._loading = True
        self._scene.blockSignals(True)
        self._scene.clear()
        workspace_rect = QRectF(-260, -180, self.slide_w + 520, self.slide_h + 360)
        self._scene.setSceneRect(workspace_rect)
        self._scene.setBackgroundBrush(QColor(Theme.WORKSPACE))
        self._slide_background_item = None
        self._create_slide_background(slide.get("background", "#FFFFFF"))
        for data in slide.get("elements", []):
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            x, y = float(data.get("x", 80)), float(data.get("y", 80))
            if kind == "text":
                item = ResizableTextItem()
                if isinstance(data.get("html"), str) and data.get("html"):
                    item.setHtml(data["html"])
                else:
                    item.setPlainText(str(data.get("text", "Textové pole")))
                    font = QFont(str(data.get("font_family", Theme.DEFAULT_FONT)), int(data.get("font_size", 30)))
                    font.setBold(bool(data.get("bold", False)))
                    font.setItalic(bool(data.get("italic", False)))
                    item.setFont(font)
                    item.setDefaultTextColor(QColor(str(data.get("color", "#202020"))))
                item.setTextWidth(float(data.get("w", 800)))
                self._set_saved_scale(item, data)
                item.setData(0, "text")
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 10)))
                item.document().contentsChanged.connect(self.changed.emit)
                self._scene.addItem(item)
            elif kind == "image":
                asset_id = str(data.get("asset", ""))
                raw = assets.get(asset_id)
                if not raw:
                    continue
                pix = QPixmap()
                if not pix.loadFromData(raw):
                    continue
                item = ResizablePixmapItem(pix)
                target_w = max(1.0, float(data.get("w", pix.width())))
                target_h = max(1.0, float(data.get("h", pix.height())))
                legacy_x = target_w / max(1, pix.width()) if "scale_x" not in data else 1.0
                legacy_y = target_h / max(1, pix.height()) if "scale_y" not in data else 1.0
                self._set_saved_scale(item, data, legacy_x, legacy_y)
                item.setData(0, "image")
                item.setData(1, asset_id)
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 5)))
                self._scene.addItem(item)
            elif kind in ("rectangle", "ellipse"):
                rect = QRectF(0, 0, float(data.get("w", 320)), float(data.get("h", 180)))
                item = ResizableEllipseItem(rect) if kind == "ellipse" else ResizableRectItem(rect)
                item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 3))))
                item.setBrush(QBrush(QColor(data.get("fill", "#DCEEFF"))))
                self._set_saved_scale(item, data)
                item.setData(0, kind)
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 2)))
                self._scene.addItem(item)
            elif kind == "line":
                w, h = float(data.get("w", 350)), float(data.get("h", 0))
                item = ResizableLineItem(0, 0, w, h)
                item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 4))))
                self._set_saved_scale(item, data)
                item.setData(0, "line")
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 2)))
                self._scene.addItem(item)
        self._scene.blockSignals(False)
        self._loading = False
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        self.changed.emit()

    def add_text(self, text="Dvojklikem upravte text", x=140, y=140, font_size=32):
        item = ResizableTextItem(text)
        font = QFont(Theme.DEFAULT_FONT, font_size)
        item.setFont(font)
        item.setDefaultTextColor(QColor("#202020"))
        item.setTextWidth(800)
        item.setData(0, "text")
        item.setPos(x, y)
        item.setZValue(20)
        item.document().contentsChanged.connect(self.changed.emit)
        self._scene.addItem(item)
        item.setSelected(True)
        item.setFocus()
        self.changed.emit()
        return item

    def add_shape(self, kind: str):
        if kind == "line":
            item = ResizableLineItem(0, 0, 350, 0)
            item.setPen(QPen(QColor(Theme.ACCENT), 5))
        else:
            rect = QRectF(0, 0, 350, 200)
            item = ResizableEllipseItem(rect) if kind == "ellipse" else ResizableRectItem(rect)
            item.setPen(QPen(QColor(Theme.ACCENT), 3))
            item.setBrush(QBrush(QColor("#DCEEFF")))
        item.setData(0, kind)
        item.setPos(220, 220)
        item.setZValue(3)
        self._scene.addItem(item)
        item.setSelected(True)
        self.changed.emit()

    def add_image(self, raw: bytes, asset_id: str):
        pix = QPixmap()
        if not pix.loadFromData(raw):
            raise ValueError("Obrázek se nepodařilo načíst.")
        item = ResizablePixmapItem(pix)
        scale = min(700 / max(1, pix.width()), 500 / max(1, pix.height()), 1.0)
        item.setTransform(QTransform.fromScale(scale, scale))
        item.setData(0, "image")
        item.setData(1, asset_id)
        item.setPos(200, 180)
        item.setZValue(5)
        self._scene.addItem(item)
        item.setSelected(True)
        self.changed.emit()

    def selected_graphics_item(self):
        selected = self._scene.selectedItems()
        return selected[0] if selected else None

    def get_selected_element_data(self) -> Optional[dict]:
        item = self.selected_graphics_item()
        if item is None:
            return None
        kind = item.data(0)
        if kind == "text" and isinstance(item, QGraphicsTextItem):
            font = item.font()
            color = item.defaultTextColor().name()
            tr = item.transform()
            return {
                "type": "text", "x": item.pos().x(), "y": item.pos().y(),
                "w": item.textWidth(), "text": item.toPlainText(), "html": item.toHtml(),
                "font_family": font.family(), "font_size": font.pointSize() if font.pointSize() > 0 else 30,
                "color": color, "bold": font.bold(), "italic": font.italic(),
                "scale_x": tr.m11(), "scale_y": tr.m22(), "z": item.zValue(),
            }
        elif kind == "image" and isinstance(item, QGraphicsPixmapItem):
            asset_id = str(item.data(1) or "")
            rect = item.boundingRect()
            tr = item.transform()
            return {"type": "image", "x": item.pos().x(), "y": item.pos().y(),
                    "w": rect.width(), "h": rect.height(),
                    "scale_x": tr.m11(), "scale_y": tr.m22(),
                    "asset": asset_id, "z": item.zValue()}
        elif kind in ("rectangle", "ellipse") and isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem)):
            rect = item.rect()
            tr = item.transform()
            return {"type": kind, "x": item.pos().x(), "y": item.pos().y(),
                    "w": rect.width(), "h": rect.height(),
                    "scale_x": tr.m11(), "scale_y": tr.m22(),
                    "line": item.pen().color().name(), "line_width": item.pen().widthF(),
                    "fill": item.brush().color().name(), "z": item.zValue()}
        elif kind == "line" and isinstance(item, QGraphicsLineItem):
            line = item.line()
            tr = item.transform()
            return {"type": "line", "x": item.pos().x(), "y": item.pos().y(),
                    "w": line.x2() - line.x1(), "h": line.y2() - line.y1(),
                    "scale_x": tr.m11(), "scale_y": tr.m22(),
                    "line": item.pen().color().name(), "line_width": item.pen().widthF(),
                    "z": item.zValue()}
        return None

    def collect_elements(self) -> List[dict]:
        result = []
        for item in sorted(self._scene.items(), key=lambda obj: obj.zValue()):
            kind = item.data(0)
            if kind == "text" and isinstance(item, QGraphicsTextItem):
                font = item.font()
                color = item.defaultTextColor().name()
                tr = item.transform()
                result.append({
                    "type": "text", "x": item.pos().x(), "y": item.pos().y(),
                    "w": item.textWidth(), "text": item.toPlainText(), "html": item.toHtml(),
                    "font_family": font.family(), "font_size": font.pointSize() if font.pointSize() > 0 else 30,
                    "color": color, "bold": font.bold(), "italic": font.italic(),
                    "scale_x": tr.m11(), "scale_y": tr.m22(), "z": item.zValue(),
                })
            elif kind == "image" and isinstance(item, QGraphicsPixmapItem):
                asset_id = str(item.data(1) or "")
                rect = item.boundingRect()
                tr = item.transform()
                result.append({"type": "image", "x": item.pos().x(), "y": item.pos().y(),
                               "w": rect.width(), "h": rect.height(),
                               "scale_x": tr.m11(), "scale_y": tr.m22(),
                               "asset": asset_id, "z": item.zValue()})
            elif kind in ("rectangle", "ellipse") and isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem)):
                rect = item.rect()
                tr = item.transform()
                result.append({"type": kind, "x": item.pos().x(), "y": item.pos().y(),
                               "w": rect.width(), "h": rect.height(),
                               "scale_x": tr.m11(), "scale_y": tr.m22(),
                               "line": item.pen().color().name(), "line_width": item.pen().widthF(),
                               "fill": item.brush().color().name(), "z": item.zValue()})
            elif kind == "line" and isinstance(item, QGraphicsLineItem):
                line = item.line()
                tr = item.transform()
                result.append({"type": "line", "x": item.pos().x(), "y": item.pos().y(),
                               "w": line.x2() - line.x1(), "h": line.y2() - line.y1(),
                               "scale_x": tr.m11(), "scale_y": tr.m22(),
                               "line": item.pen().color().name(), "line_width": item.pen().widthF(),
                               "z": item.zValue()})
        return result

    def rendered_pixmap(self, size: QSize = QSize(320, 180)) -> QPixmap:
        pix = QPixmap(size)
        pix.fill(self.slide_background_color())
        painter = QPainter(pix)
        self._scene.render(painter, QRectF(0, 0, size.width(), size.height()),
                           QRectF(0, 0, self.slide_w, self.slide_h))
        painter.end()
        return pix

    def set_background(self, color: QColor):
        if self._slide_background_item is not None:
            self._slide_background_item.setBrush(QBrush(color))
        self.changed.emit()


# =============================================================================
# REŽIM PREZENTOVÁNÍ A OKNA
# =============================================================================
class SlideShowViewWindow(QMainWindow):
    """Divácké / prezentační okno zobrazené na plátně nebo druhé obrazovce."""
    def __init__(self, slides: List[dict], assets: Dict[str, bytes], slide_w=1920, slide_h=1080,
                 start_index=0, parent=None):
        super().__init__(parent)
        self.slides = copy.deepcopy(slides)
        self.assets = dict(assets)
        self.slide_w = slide_w
        self.slide_h = slide_h
        self.index = max(0, min(start_index, len(self.slides) - 1))
        self.setWindowTitle("Prezentace — Divácké okno")
        if os.path.isfile(LOGO_PATH):
            self.setWindowIcon(QIcon(LOGO_PATH))
        self.setStyleSheet("QMainWindow{background:#080D15;}")
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.setCentralWidget(self.label)
        self.resize(1280, 720)
        self.show_slide(self.index)

    def show_slide(self, index: int):
        self.index = max(0, min(index, len(self.slides) - 1))
        slide = self.slides[self.index]
        pix = _render_slide_pixmap(slide, self.assets, self.slide_w, self.slide_h, QSize(self.slide_w, self.slide_h))
        available = self.label.size()
        if available.width() < 50 or available.height() < 50:
            available = QSize(1280, 720)
        self.label.setPixmap(pix.scaled(available, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.show_slide(self.index)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
        elif key in (Qt.Key_Right, Qt.Key_Space, Qt.Key_PageDown, Qt.Key_Down):
            if self.index < len(self.slides) - 1:
                self.index += 1
                self.show_slide(self.index)
        elif key in (Qt.Key_Left, Qt.Key_PageUp, Qt.Key_Up):
            if self.index > 0:
                self.index -= 1
                self.show_slide(self.index)
        else:
            super().keyPressEvent(event)


class PresenterModeWindow(QMainWindow):
    """Propojené okno přednášejícího s náhledem, poznámkami a ovládáním."""
    def __init__(self, slides: List[dict], assets: Dict[str, bytes], slide_w=1920, slide_h=1080,
                 start_index=0, audience_window=None, parent=None):
        super().__init__(parent)
        self.slides = copy.deepcopy(slides)
        self.assets = dict(assets)
        self.slide_w = slide_w
        self.slide_h = slide_h
        self.index = max(0, min(start_index, len(self.slides) - 1))
        self.audience_window = audience_window
        self.setWindowTitle("Režim přednášejícího — Prezentář")
        if os.path.isfile(LOGO_PATH):
            self.setWindowIcon(QIcon(LOGO_PATH))
        self.setStyleSheet("QMainWindow{background:#080D15;} QLabel{color:white;}")

        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Levá část: Aktuální snímek a ovládací tlačítka
        left_widget = QWidget()
        left_lay = QVBoxLayout(left_widget)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(QLabel("AKTUÁLNÍ SNÍMEK"))
        self.current_label = QLabel()
        self.current_label.setAlignment(Qt.AlignCenter)
        self.current_label.setMinimumSize(400, 240)
        self.current_label.setStyleSheet("background:#080D15;border:1px solid #3B4B5E;")
        left_lay.addWidget(self.current_label, 3)

        nav_layout = QHBoxLayout()
        prev_btn = QPushButton("◀ Předchozí")
        prev_btn.clicked.connect(self.previous_slide)
        next_btn = QPushButton("Další ▶")
        next_btn.clicked.connect(self.next_slide)
        nav_layout.addWidget(prev_btn)
        nav_layout.addWidget(next_btn)
        left_lay.addLayout(nav_layout)
        layout.addWidget(left_widget, 3)

        # Pravá část: Poznámky a další snímek
        self.notes_panel = QWidget()
        self.notes_layout = QVBoxLayout(self.notes_panel)
        self.notes_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_layout.addWidget(QLabel("ŘEČNICKÉ POZNÁMKY"))
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setStyleSheet("background:#172231;color:#FFFFFF;border:1px solid #3B4B5E;font-size:17px;padding:10px;")
        self.notes_layout.addWidget(self.notes, 3)

        self.notes_layout.addWidget(QLabel("DALŠÍ SNÍMEK"))
        self.next_label = QLabel()
        self.next_label.setAlignment(Qt.AlignCenter)
        self.next_label.setMinimumHeight(120)
        self.next_label.setStyleSheet("background:#172231;border:1px solid #3B4B5E;")
        self.notes_layout.addWidget(self.next_label, 1)
        self.notes_layout.addWidget(QLabel("← / → nebo mezerník: navigace   •   Esc: konec"))
        self.notes_panel.setMinimumWidth(280)
        self.notes_panel.setMaximumWidth(450)
        layout.addWidget(self.notes_panel, 2)

        self.setCentralWidget(root)
        self.resize(1100, 700)
        self.update_view()

    def set_index(self, index: int):
        self.index = max(0, min(index, len(self.slides) - 1))
        self.update_view()
        if self.audience_window and not self.audience_window.isHidden():
            self.audience_window.show_slide(self.index)

    def next_slide(self):
        if self.index < len(self.slides) - 1:
            self.set_index(self.index + 1)

    def previous_slide(self):
        if self.index > 0:
            self.set_index(self.index - 1)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            if self.audience_window:
                self.audience_window.close()
            self.close()
        elif key in (Qt.Key_Right, Qt.Key_Space, Qt.Key_PageDown, Qt.Key_Down):
            self.next_slide()
        elif key in (Qt.Key_Left, Qt.Key_PageUp, Qt.Key_Up):
            self.previous_slide()
        elif key == Qt.Key_Home:
            self.set_index(0)
        elif key == Qt.Key_End:
            self.set_index(len(self.slides) - 1)
        else:
            super().keyPressEvent(event)

    def update_view(self):
        slide = self.slides[self.index]
        pix = _render_slide_pixmap(slide, self.assets, self.slide_w, self.slide_h, QSize(self.slide_w, self.slide_h))
        available = self.current_label.size()
        if available.width() < 50 or available.height() < 50:
            available = QSize(800, 450)
        self.current_label.setPixmap(pix.scaled(available, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        self.notes.setPlainText(str(slide.get("notes", "")) or "Pro tento snímek nejsou žádné poznámky.")
        if self.index + 1 < len(self.slides):
            next_pix = _render_slide_pixmap(self.slides[self.index + 1], self.assets, self.slide_w, self.slide_h, QSize(480, 270))
            self.next_label.setPixmap(next_pix.scaled(self.next_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.next_label.setText("Konec prezentace")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_view()

    def closeEvent(self, event):
        if self.audience_window:
            self.audience_window.close()
        super().closeEvent(event)


class SlideShowWindow(QMainWindow):
    """Standardní okno prezentace pro spuštění bez režimu přednášejícího."""
    def __init__(self, slides: List[dict], assets: Dict[str, bytes], slide_w=1920, slide_h=1080,
                 start_index=0, parent=None):
        super().__init__(parent)
        self.slides = copy.deepcopy(slides)
        self.assets = dict(assets)
        self.slide_w = slide_w
        self.slide_h = slide_h
        self.index = max(0, min(start_index, len(self.slides) - 1))
        self.setWindowTitle("Prezentace — Prezentář")
        if os.path.isfile(LOGO_PATH):
            self.setWindowIcon(QIcon(LOGO_PATH))
        self.setStyleSheet("QMainWindow{background:#080D15;} QLabel{color:white;}")
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        self.current_label = QLabel()
        self.current_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.current_label)
        self.setCentralWidget(root)
        self.resize(1280, 720)
        self._show_slide()

    def _show_slide(self):
        slide = self.slides[self.index]
        available = self.current_label.size()
        if available.width() < 50 or available.height() < 50:
            available = QSize(1280, 720)
        pix = _render_slide_pixmap(slide, self.assets, self.slide_w, self.slide_h, QSize(self.slide_w, self.slide_h))
        self.current_label.setPixmap(pix.scaled(available, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show_slide()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
        elif key in (Qt.Key_Right, Qt.Key_Space, Qt.Key_PageDown, Qt.Key_Down):
            if self.index < len(self.slides) - 1:
                self.index += 1
                self._show_slide()
            else:
                self.close()
        elif key in (Qt.Key_Left, Qt.Key_PageUp, Qt.Key_Up):
            if self.index > 0:
                self.index -= 1
                self._show_slide()
        elif key == Qt.Key_Home:
            self.index = 0
            self._show_slide()
        elif key == Qt.Key_End:
            self.index = len(self.slides) - 1
            self._show_slide()
        else:
            super().keyPressEvent(event)


# =============================================================================
# HLAVNÍ OKNO
# =============================================================================
class PrezentarWindow(QMainWindow):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Prezentář — VLČR Kancelář")
        self.setMinimumSize(Theme.MIN_W, Theme.MIN_H)
        self.resize(Theme.WINDOW_W, Theme.WINDOW_H)
        if os.path.isfile(LOGO_PATH):
            self.setWindowIcon(QIcon(LOGO_PATH))
        self.slides: List[dict] = []
        self.active_index = 0
        self.assets: Dict[str, bytes] = {}
        self.path: Optional[str] = None
        self.created: Optional[str] = None
        self.title = "Nepojmenovaná prezentace"
        self.slide_width = 1920
        self.slide_height = 1080
        self._loading = False
        self._dirty = False
        self._clipboard_element: Optional[dict] = None
        self._build_actions()
        self._build_menus()
        self._build_toolbars()
        self._build_ui()
        self._build_status()
        self._apply_theme()
        if initial_path:
            self.open_presentation(initial_path)
        else:
            self.new_presentation()

    def _action(self, label, shortcut, slot, checkable=False):
        action = QAction(label, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        action.setCheckable(checkable)
        return action

    def _build_actions(self):
        self.act_new = self._action("Nová prezentace", "Ctrl+N", self.new_presentation)
        self.act_open = self._action("Otevřít…", "Ctrl+O", self.open_dialog)
        self.act_save = self._action("Uložit", "Ctrl+S", self.save)
        self.act_save_as = self._action("Uložit jako…", "Ctrl+Shift+S", self.save_as)
        self.act_add_slide = self._action("Přidat snímek", "Ctrl+M", self.add_slide)
        self.act_duplicate = self._action("Duplikovat snímek", None, self.duplicate_slide)
        self.act_delete = self._action("Odstranit snímek", None, self.delete_slide)
        self.act_text = self._action("Textové pole", None, self.insert_text)
        self.act_image = self._action("Obrázek…", None, self.insert_image)
        self.act_rect = self._action("Obdélník", None, lambda: self.canvas.add_shape("rectangle"))
        self.act_ellipse = self._action("Elipsa", None, lambda: self.canvas.add_shape("ellipse"))
        self.act_line = self._action("Čára", None, lambda: self.canvas.add_shape("line"))
        self.act_copy = self._action("Kopírovat", "Ctrl+C", self.copy_object)
        self.act_paste = self._action("Vložit", "Ctrl+V", self.paste_object)
        self.act_delete_object = self._action("Odstranit objekt", "Delete", self.delete_object)
        self.act_front = self._action("Přenést dopředu", None, lambda: self.adjust_z(1))
        self.act_back = self._action("Přenést dozadu", None, lambda: self.adjust_z(-1))
        self.act_background = self._action("Pozadí snímku…", None, self.choose_background)
        self.act_present = self._action("Spustit prezentaci", "F5", lambda: self.start_presentation(False))
        self.act_presenter = self._action("Režim přednášejícího", "Ctrl+F5", lambda: self.start_presentation(True))
        self.act_pdf = self._action("Exportovat do PDF…", "Ctrl+P", self.export_pdf)
        self.act_rename = self._action("Přejmenovat snímek…", None, self.rename_slide)

    def _build_menus(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("Soubor")
        for act in (self.act_new, self.act_open, self.act_save, self.act_save_as):
            file_menu.addAction(act)
        file_menu.addSeparator()
        file_menu.addAction(self.act_pdf)
        file_menu.addSeparator()
        file_menu.addAction("Konec", self.close)
        slide_menu = bar.addMenu("Snímek")
        for act in (self.act_add_slide, self.act_duplicate, self.act_rename, self.act_delete):
            slide_menu.addAction(act)
        slide_menu.addSeparator()
        slide_menu.addAction("Posunout nahoru", lambda: self.move_slide(-1))
        slide_menu.addAction("Posunout dolů", lambda: self.move_slide(1))
        edit_menu = bar.addMenu("Úpravy")
        edit_menu.addAction(self.act_copy)
        edit_menu.addAction(self.act_paste)
        edit_menu.addAction(self.act_delete_object)
        edit_menu.addAction(self.act_front)
        edit_menu.addAction(self.act_back)
        insert_menu = bar.addMenu("Vložení")
        for act in (self.act_text, self.act_image, self.act_rect, self.act_ellipse, self.act_line):
            insert_menu.addAction(act)
        design_menu = bar.addMenu("Návrh")
        design_menu.addAction(self.act_background)
        present_menu = bar.addMenu("Prezentace")
        present_menu.addAction(self.act_present)
        present_menu.addAction(self.act_presenter)
        help_menu = bar.addMenu("Nápověda")
        help_menu.addAction("O aplikaci Prezentář", self.about)

    def _build_toolbars(self):
        standard = QToolBar("Soubor a snímky")
        standard.setMovable(False)
        standard.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.TopToolBarArea, standard)
        for act in (self.act_new, self.act_open, self.act_save, self.act_pdf):
            standard.addAction(act)
        standard.addSeparator()
        for act in (self.act_add_slide, self.act_duplicate, self.act_delete):
            standard.addAction(act)
        standard.addSeparator()
        for act in (self.act_present, self.act_presenter):
            standard.addAction(act)

        insert = QToolBar("Vložení")
        insert.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, insert)
        for act in (self.act_text, self.act_image, self.act_rect, self.act_ellipse, self.act_line, self.act_copy, self.act_paste, self.act_delete_object):
            insert.addAction(act)
        insert.addSeparator()
        for act in (self.act_front, self.act_back, self.act_background):
            insert.addAction(act)

    def _build_ui(self):
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        splitter = QSplitter(Qt.Horizontal)

        # Levý panel — pořadí snímků.
        left = QFrame()
        left.setMinimumWidth(190)
        left.setMaximumWidth(270)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(6, 6, 6, 6)
        left_lay.addWidget(QLabel("SNÍMKY"))
        self.slide_list = QListWidget()
        self.slide_list.setIconSize(QSize(170, 96))
        self.slide_list.setSpacing(6)
        self.slide_list.setDragDropMode(QListWidget.NoDragDrop)
        self.slide_list.currentRowChanged.connect(self.select_slide)
        left_lay.addWidget(self.slide_list, 1)
        slide_buttons = QGridLayout()
        add_btn = QPushButton("＋ Přidat")
        add_btn.clicked.connect(self.add_slide)
        duplicate_btn = QPushButton("Duplikovat")
        duplicate_btn.clicked.connect(self.duplicate_slide)
        delete_btn = QPushButton("Odstranit")
        delete_btn.clicked.connect(self.delete_slide)
        up_btn = QPushButton("↑")
        up_btn.setToolTip("Posunout snímek nahoru")
        up_btn.clicked.connect(lambda: self.move_slide(-1))
        down_btn = QPushButton("↓")
        down_btn.setToolTip("Posunout snímek dolů")
        down_btn.clicked.connect(lambda: self.move_slide(1))
        slide_buttons.addWidget(add_btn, 0, 0)
        slide_buttons.addWidget(duplicate_btn, 0, 1)
        slide_buttons.addWidget(delete_btn, 1, 0, 1, 2)
        slide_buttons.addWidget(up_btn, 2, 0)
        slide_buttons.addWidget(down_btn, 2, 1)
        left_lay.addLayout(slide_buttons)
        splitter.addWidget(left)

        # Střed — plátno a poznámky řečníka.
        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(6)
        self.canvas = SlideCanvas(self.slide_width, self.slide_height)
        self.canvas.changed.connect(self._canvas_changed)
        self.canvas.selection_changed.connect(self._selection_changed)
        center_lay.addWidget(self.canvas, 1)
        note_head = QHBoxLayout()
        note_head.addWidget(QLabel("POZNÁMKY PŘEDNÁŠEJÍCÍHO"))
        note_head.addStretch(1)
        note_head.addWidget(QLabel("Zobrazí se v režimu přednášejícího"))
        center_lay.addLayout(note_head)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Sem napište řečnické poznámky k aktuálnímu snímku…")
        self.notes_edit.setMaximumHeight(145)
        self.notes_edit.textChanged.connect(self._notes_changed)
        center_lay.addWidget(self.notes_edit)
        splitter.addWidget(center)

        # Pravý panel — formát, vlastnosti tvaru, rozlišení a přechod snímku.
        right = QFrame()
        right.setMinimumWidth(245)
        right.setMaximumWidth(340)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 8, 8, 8)
        right_lay.addWidget(QLabel("FORMÁTOVÁNÍ TEXTU"))
        self.font_combo = QFontComboBox()
        self.font_combo.currentFontChanged.connect(self.apply_font_family)
        right_lay.addWidget(QLabel("Písmo"))
        right_lay.addWidget(self.font_combo)
        self.font_size = QSpinBox()
        self.font_size.setRange(8, 150)
        self.font_size.setValue(32)
        self.font_size.valueChanged.connect(self.apply_font_size)
        right_lay.addWidget(QLabel("Velikost písma"))
        right_lay.addWidget(self.font_size)
        style_row = QHBoxLayout()
        self.bold_check = QCheckBox("Tučně")
        self.bold_check.toggled.connect(self.apply_bold)
        self.italic_check = QCheckBox("Kurzíva")
        self.italic_check.toggled.connect(self.apply_italic)
        style_row.addWidget(self.bold_check)
        style_row.addWidget(self.italic_check)
        right_lay.addLayout(style_row)
        color_btn = QPushButton("Barva textu…")
        color_btn.clicked.connect(self.choose_text_color)
        right_lay.addWidget(color_btn)

        right_lay.addSpacing(10)
        right_lay.addWidget(QLabel("VLASTNOSTI TVARU"))
        shape_color_row = QHBoxLayout()
        self.btn_fill_color = QPushButton("Výplň…")
        self.btn_fill_color.clicked.connect(self.choose_shape_fill_color)
        self.btn_line_color = QPushButton("Ohraničení…")
        self.btn_line_color.clicked.connect(self.choose_shape_line_color)
        shape_color_row.addWidget(self.btn_fill_color)
        shape_color_row.addWidget(self.btn_line_color)
        right_lay.addLayout(shape_color_row)

        line_w_row = QHBoxLayout()
        line_w_row.addWidget(QLabel("Šířka ohraničení"))
        self.spin_line_width = QSpinBox()
        self.spin_line_width.setRange(1, 40)
        self.spin_line_width.setValue(3)
        self.spin_line_width.setSuffix(" px")
        self.spin_line_width.valueChanged.connect(self.apply_shape_line_width)
        line_w_row.addWidget(self.spin_line_width)
        right_lay.addLayout(line_w_row)

        right_lay.addSpacing(10)
        right_lay.addWidget(QLabel("VELIKOST VYBRANÉHO OBJEKTU"))
        size_w_row = QHBoxLayout()
        size_w_row.addWidget(QLabel("Šířka"))
        self.object_width = QSpinBox()
        self.object_width.setRange(1, 5000)
        self.object_width.setSuffix(" px")
        self.object_width.valueChanged.connect(self._resize_selected_object)
        size_w_row.addWidget(self.object_width)
        right_lay.addLayout(size_w_row)
        size_h_row = QHBoxLayout()
        size_h_row.addWidget(QLabel("Výška"))
        self.object_height = QSpinBox()
        self.object_height.setRange(1, 4000)
        self.object_height.setSuffix(" px")
        self.object_height.valueChanged.connect(self._resize_selected_object)
        size_h_row.addWidget(self.object_height)
        right_lay.addLayout(size_h_row)
        self._updating_object_controls = False

        right_lay.addSpacing(12)
        right_lay.addWidget(QLabel("NÁVRH SNÍMKU"))
        bg_btn = QPushButton("Změnit pozadí…")
        bg_btn.clicked.connect(self.choose_background)
        right_lay.addWidget(bg_btn)

        right_lay.addWidget(QLabel("Rozlišení prezentace"))
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("HD (1280 × 720 px)", (1280, 720))
        self.resolution_combo.addItem("Full HD (1920 × 1080 px)", (1920, 1080))
        self.resolution_combo.addItem("2K QHD (2560 × 1440 px)", (2560, 1440))
        self.resolution_combo.addItem("4K UHD (3840 × 2160 px)", (3840, 2160))
        self.resolution_combo.setCurrentIndex(1)  # Default Full HD
        self.resolution_combo.currentIndexChanged.connect(self.change_resolution)
        right_lay.addWidget(self.resolution_combo)

        right_lay.addWidget(QLabel("Přechod při prezentaci"))
        self.transition_combo = QComboBox()
        self.transition_combo.addItem("Bez efektu", "none")
        self.transition_combo.addItem("Prolnutí", "fade")
        self.transition_combo.addItem("Zleva", "slide_right")
        self.transition_combo.addItem("Zprava", "slide_left")
        self.transition_combo.addItem("Zdola", "slide_up")
        self.transition_combo.addItem("Shora", "slide_down")
        self.transition_combo.addItem("Přiblížení", "zoom")
        self.transition_combo.addItem("Rychlé prolnutí", "wipe")
        self.transition_combo.currentIndexChanged.connect(self.change_transition)
        right_lay.addWidget(self.transition_combo)
        right_lay.addStretch(1)
        self.slide_info = QLabel("Vyberte objekt na snímku. Velikost můžete měnit přetažením pravého/dolního okraje.")
        self.slide_info.setWordWrap(True)
        self.slide_info.setStyleSheet(f"color:{Theme.MUTED};")
        right_lay.addWidget(self.slide_info)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 900, 310])
        outer.addWidget(splitter, 1)
        self.setCentralWidget(root)

    def _build_status(self):
        status = QStatusBar()
        self.setStatusBar(status)
        self.status_text = QLabel("Připraveno")
        self.slide_count_label = QLabel("")
        status.addWidget(self.status_text, 1)
        status.addPermanentWidget(self.slide_count_label)
        status.addPermanentWidget(QLabel(f"{APP_NAME} {APP_VERSION}"))

    def _apply_theme(self):
        self.setStyleSheet(f"""
            QMainWindow,QWidget{{background:{Theme.WINDOW};color:{Theme.TEXT};font-family:'{Theme.FONT}';}}
            QMenuBar{{background:{Theme.SURFACE};border-bottom:1px solid {Theme.BORDER};padding:3px;}}
            QMenuBar::item{{padding:6px 10px;}}
            QMenuBar::item:selected{{background:{Theme.SELECTION};}}
            QMenu{{background:white;border:1px solid {Theme.BORDER};}}
            QMenu::item{{padding:6px 22px;}}
            QMenu::item:selected{{background:{Theme.SELECTION};}}
            QToolBar{{background:{Theme.TOOLBAR};border:0;border-bottom:1px solid {Theme.BORDER};spacing:5px;padding:5px;}}
            QToolButton{{padding:5px 8px;border:1px solid transparent;}}
            QToolButton:hover{{background:{Theme.SELECTION};border:1px solid {Theme.ACCENT};}}
            QPushButton,QComboBox,QFontComboBox,QSpinBox{{background:white;border:1px solid {Theme.BORDER};padding:5px;min-height:23px;}}
            QPushButton:hover{{border-color:{Theme.ACCENT};}}
            QListWidget{{background:#E9E9E9;border:1px solid {Theme.BORDER};padding:5px;}}
            QListWidget::item{{background:white;border:1px solid {Theme.BORDER};padding:4px;margin:3px;}}
            QListWidget::item:selected{{border:2px solid {Theme.ACCENT};background:#EAF4FF;}}
            QPlainTextEdit{{background:white;border:1px solid {Theme.BORDER};padding:7px;}}
            QGroupBox{{border:1px solid {Theme.BORDER};margin-top:8px;padding:8px;}}
            QStatusBar{{background:#747474;color:white;}}
            QStatusBar QLabel{{color:white;padding:0 8px;}}
        """)

    # --- Model / dokument ---------------------------------------------------
    def new_presentation(self):
        if not self._confirm_discard():
            return
        self.path = None
        self.active_index = 0
        self.created = _now()
        self.title = "Moje nová prezentace VLČR"
        self.slide_width = 1920
        self.slide_height = 1080
        self.slides = [_blank_slide(1)]
        self.assets = {}
        self._dirty = True
        self.canvas.update_resolution(self.slide_width, self.slide_height)
        self._load_current_slide()
        self._refresh_slide_list(0)
        self._refresh_title()
        self._update_resolution_control()

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(self, "Neuložené změny", "Prezentace obsahuje neuložené změny. Chcete je uložit?",
                                      QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Yes:
            return self.save()
        return True

    def _current_index(self) -> int:
        return max(0, min(self.active_index, len(self.slides) - 1)) if self.slides else 0

    def _current_slide(self) -> Optional[dict]:
        return self.slides[self._current_index()] if self.slides else None

    def _write_current(self):
        if self._loading or not self.slides:
            return
        slide = self._current_slide()
        if slide is None:
            return
        slide["elements"] = self.canvas.collect_elements()
        slide["notes"] = self.notes_edit.toPlainText()
        slide["background"] = self.canvas.slide_background_color().name()

    def _load_current_slide(self):
        if not self.slides:
            return
        self._loading = True
        slide = self._current_slide()
        self.canvas.load_slide(slide, self.assets)
        self.notes_edit.blockSignals(True)
        self.notes_edit.setPlainText(str(slide.get("notes", "")))
        self.notes_edit.blockSignals(False)
        self._loading = False
        self._update_transition_control()
        self._update_slide_counter()
        self._update_preview_for_current()

    def _refresh_slide_list(self, selected: Optional[int] = None):
        self._loading = True
        current = self._current_index() if selected is None else selected
        current = max(0, min(current, len(self.slides) - 1)) if self.slides else 0
        self.active_index = current
        self.slide_list.clear()
        for i, slide in enumerate(self.slides):
            title = str(slide.get("name") or f"Snímek {i+1}")
            item = QListWidgetItem()
            item.setText(f"{i+1}. {title}")
            item.setSizeHint(QSize(188, 125))
            self.slide_list.addItem(item)
        if self.slides:
            self.slide_list.setCurrentRow(max(0, min(current, len(self.slides)-1)))
        self._loading = False
        self._update_slide_counter()
        self._update_all_previews()

    def _render_slide_preview(self, slide: dict) -> QPixmap:
        scene = self._scene_from_slide(slide)
        pix = QPixmap(340, 190)
        pix.fill(QColor(slide.get("background", "#FFFFFF")))
        painter = QPainter(pix)
        scene.render(painter, QRectF(0, 0, 340, 190), QRectF(0, 0, self.slide_width, self.slide_height))
        painter.end()
        scene.clear()
        return pix

    def _update_all_previews(self):
        for i, slide in enumerate(self.slides):
            item = self.slide_list.item(i)
            if item:
                item.setIcon(QIcon(self._render_slide_preview(slide)))
                item.setText(f"{i+1}. {slide.get('name', f'Snímek {i+1}')}")

    def _update_preview_for_current(self):
        self._write_current()
        self._update_all_previews()

    def _update_slide_counter(self):
        self.slide_count_label.setText(f"Snímek {self._current_index()+1} z {len(self.slides)}" if self.slides else "0 snímků")

    def _refresh_title(self):
        filename = os.path.basename(self.path) if self.path else self.title
        self.setWindowTitle(f"{'● ' if self._dirty else ''}{filename} — {APP_NAME} — VLČR Kancelář")

    def _mark_dirty(self):
        if self._loading:
            return
        self._dirty = True
        self._refresh_title()

    def _canvas_changed(self):
        if self._loading or self.canvas._loading:
            return
        self._mark_dirty()
        self._write_current()
        self._update_slide_counter()
        preview_item = self.slide_list.item(self._current_index())
        current_slide = self._current_slide()
        if preview_item is not None and current_slide is not None:
            preview_item.setIcon(QIcon(self._render_slide_preview(current_slide)))

    def _notes_changed(self):
        if self._loading:
            return
        self._mark_dirty()
        self._write_current()

    def _selection_changed(self):
        item = self.canvas.selected_graphics_item()
        if item is None:
            self.slide_info.setText("Vyberte objekt na snímku. Velikost můžete měnit přetažením pravého/dolního okraje.")
            self.font_combo.setEnabled(False)
            self.font_size.setEnabled(False)
            self.bold_check.setEnabled(False)
            self.italic_check.setEnabled(False)
            self.object_width.setEnabled(False)
            self.object_height.setEnabled(False)
            self.btn_fill_color.setEnabled(False)
            self.btn_line_color.setEnabled(False)
            self.spin_line_width.setEnabled(False)
            return

        self.object_width.setEnabled(True)
        self.object_height.setEnabled(True)
        bounds = item.sceneBoundingRect()
        self._updating_object_controls = True
        self.object_width.blockSignals(True)
        self.object_height.blockSignals(True)
        self.object_width.setValue(max(1, round(bounds.width())))
        self.object_height.setValue(max(1, round(bounds.height())))
        self.object_width.blockSignals(False)
        self.object_height.blockSignals(False)
        self._updating_object_controls = False

        kind = item.data(0) or "objekt"
        self.slide_info.setText(
            f"Vybrán objekt: {kind}. Přetažením pravého/dolního okraje měníte velikost."
        )

        is_text = isinstance(item, QGraphicsTextItem)
        is_shape = kind in ("rectangle", "ellipse", "line")

        for control in (self.font_combo, self.font_size, self.bold_check, self.italic_check):
            control.setEnabled(is_text)

        self.btn_fill_color.setEnabled(kind in ("rectangle", "ellipse"))
        self.btn_line_color.setEnabled(is_shape)
        self.spin_line_width.setEnabled(is_shape)

        if is_shape:
            pen = item.pen()
            self.spin_line_width.blockSignals(True)
            self.spin_line_width.setValue(max(1, round(pen.widthF())))
            self.spin_line_width.blockSignals(False)

        if is_text:
            cursor = item.textCursor()
            fmt = cursor.charFormat()
            family = fmt.fontFamily() or item.font().family() or Theme.DEFAULT_FONT
            point_size = fmt.fontPointSize()
            if point_size <= 0:
                point_size = item.font().pointSize() if item.font().pointSize() > 0 else 30

            self.font_combo.blockSignals(True)
            self.font_combo.setCurrentFont(QFont(family))
            self.font_combo.blockSignals(False)
            self.font_size.blockSignals(True)
            self.font_size.setValue(max(8, min(150, round(point_size))))
            self.font_size.blockSignals(False)
            self.bold_check.blockSignals(True)
            self.bold_check.setChecked(fmt.fontWeight() >= QFont.Bold)
            self.bold_check.blockSignals(False)
            self.italic_check.blockSignals(True)
            self.italic_check.setChecked(fmt.fontItalic())
            self.italic_check.blockSignals(False)

    def _resize_selected_object(self, _value=None):
        if self._updating_object_controls:
            return
        item = self.canvas.selected_graphics_item()
        if item is None:
            return
        base = item.boundingRect()
        if base.width() <= 0 or base.height() <= 0:
            return
        target_w = float(self.object_width.value())
        target_h = float(self.object_height.value())
        sx = max(0.01, min(target_w / base.width(), 100.0))
        sy = max(0.01, min(target_h / base.height(), 100.0))
        item.setTransform(QTransform.fromScale(sx, sy))
        self._mark_dirty()
        self._write_current()
        self.slide_info.setText(f"Velikost objektu: {round(item.sceneBoundingRect().width())} × "
                                f"{round(item.sceneBoundingRect().height())} px")

    def select_slide(self, row: int):
        if self._loading or row < 0 or row >= len(self.slides) or row == self.active_index:
            return
        self._write_current()
        self.active_index = row
        self._load_current_slide()

    def add_slide(self):
        self._write_current()
        self.slides.append(_blank_slide(len(self.slides) + 1))
        self._mark_dirty()
        self._refresh_slide_list(len(self.slides) - 1)
        self._load_current_slide()

    def duplicate_slide(self):
        if not self.slides:
            return
        self._write_current()
        new_slide = copy.deepcopy(self._current_slide())
        new_slide["id"] = str(uuid.uuid4())
        new_slide["name"] = f"{new_slide.get('name', 'Snímek')} – kopie"
        self.slides.insert(self._current_index() + 1, new_slide)
        self._mark_dirty()
        self._refresh_slide_list(self._current_index() + 1)
        self._load_current_slide()

    def delete_slide(self):
        if len(self.slides) <= 1:
            QMessageBox.information(self, "Odstranění snímku", "Prezentace musí obsahovat alespoň jeden snímek.")
            return
        index = self._current_index()
        answer = QMessageBox.question(self, "Odstranit snímek", "Opravdu chcete odstranit vybraný snímek?",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._write_current()
        self.slides.pop(index)
        self._mark_dirty()
        new_index = min(index, len(self.slides) - 1)
        self._refresh_slide_list(new_index)
        self._load_current_slide()

    def move_slide(self, direction: int):
        index = self._current_index()
        target = index + direction
        if target < 0 or target >= len(self.slides):
            return
        self._write_current()
        self.slides[index], self.slides[target] = self.slides[target], self.slides[index]
        self._mark_dirty()
        self._refresh_slide_list(target)
        self._load_current_slide()

    def rename_slide(self):
        slide = self._current_slide()
        if not slide:
            return
        name, ok = QInputDialog.getText(self, "Přejmenovat snímek", "Název snímku:", text=str(slide.get("name", "")))
        if ok and name.strip():
            slide["name"] = name.strip()
            self._mark_dirty()
            self._refresh_slide_list(self._current_index())

    def change_transition(self, _index):
        if self._loading:
            return
        slide = self._current_slide()
        if slide:
            slide["transition"] = self.transition_combo.currentData() or "none"
            self._mark_dirty()

    def change_resolution(self, _index):
        if self._loading:
            return
        res = self.resolution_combo.currentData()
        if res and isinstance(res, tuple) and len(res) == 2:
            w, h = res
            if self.slide_width != w or self.slide_height != h:
                self.slide_width = w
                self.slide_height = h
                self.canvas.update_resolution(w, h)
                self._mark_dirty()
                self._update_all_previews()

    def _update_transition_control(self):
        slide = self._current_slide()
        if not slide:
            return
        self.transition_combo.blockSignals(True)
        idx = self.transition_combo.findData(slide.get("transition", "none"))
        self.transition_combo.setCurrentIndex(max(0, idx))
        self.transition_combo.blockSignals(False)

    def _update_resolution_control(self):
        self.resolution_combo.blockSignals(True)
        for i in range(self.resolution_combo.count()):
            data = self.resolution_combo.itemData(i)
            if data == (self.slide_width, self.slide_height):
                self.resolution_combo.setCurrentIndex(i)
                break
        self.resolution_combo.blockSignals(False)

    # --- Kopírování a vkládání objektů (Ctrl+C / Ctrl+V) ------------------
    def copy_object(self):
        data = self.canvas.get_selected_element_data()
        if data is not None:
            self._clipboard_element = copy.deepcopy(data)
            self.status_text.setText("Objekt zkopírován do schránky.")

    def paste_object(self):
        if not self._clipboard_element:
            return
        data = copy.deepcopy(self._clipboard_element)
        data["x"] = float(data.get("x", 80)) + 30
        data["y"] = float(data.get("y", 80)) + 30
        data["z"] = float(data.get("z", 10)) + 1

        kind = data.get("type")
        x, y = data["x"], data["y"]
        item = None
        if kind == "text":
            item = ResizableTextItem()
            if isinstance(data.get("html"), str) and data.get("html"):
                item.setHtml(data["html"])
            else:
                item.setPlainText(str(data.get("text", "Textové pole")))
                font = QFont(str(data.get("font_family", Theme.DEFAULT_FONT)), int(data.get("font_size", 30)))
                font.setBold(bool(data.get("bold", False)))
                font.setItalic(bool(data.get("italic", False)))
                item.setFont(font)
                item.setDefaultTextColor(QColor(str(data.get("color", "#202020"))))
            item.setTextWidth(float(data.get("w", 800)))
            SlideCanvas._set_saved_scale(item, data)
            item.setData(0, "text")
            item.setPos(x, y)
            item.setZValue(data["z"])
            item.document().contentsChanged.connect(self.canvas.changed.emit)
            self.canvas._scene.addItem(item)
        elif kind == "image":
            asset_id = str(data.get("asset", ""))
            raw = self.assets.get(asset_id)
            if raw:
                pix = QPixmap()
                if pix.loadFromData(raw):
                    item = ResizablePixmapItem(pix)
                    target_w = max(1.0, float(data.get("w", pix.width())))
                    target_h = max(1.0, float(data.get("h", pix.height())))
                    legacy_x = target_w / max(1, pix.width()) if "scale_x" not in data else 1.0
                    legacy_y = target_h / max(1, pix.height()) if "scale_y" not in data else 1.0
                    SlideCanvas._set_saved_scale(item, data, legacy_x, legacy_y)
                    item.setData(0, "image")
                    item.setData(1, asset_id)
                    item.setPos(x, y)
                    item.setZValue(data["z"])
                    self.canvas._scene.addItem(item)
        elif kind in ("rectangle", "ellipse"):
            rect = QRectF(0, 0, float(data.get("w", 320)), float(data.get("h", 180)))
            item = ResizableEllipseItem(rect) if kind == "ellipse" else ResizableRectItem(rect)
            item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 3))))
            item.setBrush(QBrush(QColor(data.get("fill", "#DCEEFF"))))
            SlideCanvas._set_saved_scale(item, data)
            item.setData(0, kind)
            item.setPos(x, y)
            item.setZValue(data["z"])
            self.canvas._scene.addItem(item)
        elif kind == "line":
            w, h = float(data.get("w", 350)), float(data.get("h", 0))
            item = ResizableLineItem(0, 0, w, h)
            item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 4))))
            SlideCanvas._set_saved_scale(item, data)
            item.setData(0, "line")
            item.setPos(x, y)
            item.setZValue(data["z"])
            self.canvas._scene.addItem(item)

        if item is not None:
            self.canvas._scene.clearSelection()
            item.setSelected(True)
            self._clipboard_element = data
            self._mark_dirty()
            self._write_current()
            self.status_text.setText("Objekt vložen.")

    # --- Vložení / formátování / vlastnosti tvarů ---------------------------
    def insert_text(self):
        item = self.canvas.add_text()
        self._selection_changed()

    def insert_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Vložit obrázek", os.path.expanduser("~"),
                                               "Obrázky (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;Všechny soubory (*)")
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
            if len(raw) > 50 * 1024 * 1024:
                raise ValueError("Obrázek je větší než 50 MB.")
            pix = QPixmap()
            if not pix.loadFromData(raw):
                raise ValueError("Soubor není podporovaný obrázek.")
            asset_id = str(uuid.uuid4())
            self.assets[asset_id] = raw
            self.canvas.add_image(raw, asset_id)
            self._mark_dirty()
        except Exception as exc:
            QMessageBox.warning(self, "Vložení obrázku", str(exc))

    def delete_object(self):
        item = self.canvas.selected_graphics_item()
        if item is not None:
            self.canvas.scene().removeItem(item)
            self._mark_dirty()
            self._write_current()

    def adjust_z(self, direction: int):
        item = self.canvas.selected_graphics_item()
        if item is not None:
            item.setZValue(item.zValue() + direction)
            self._mark_dirty()
            self._write_current()

    def choose_background(self):
        slide = self._current_slide()
        if not slide:
            return
        current = self.canvas.slide_background_color()
        color = QColorDialog.getColor(current, self, "Barva pozadí snímku")
        if color.isValid():
            self.canvas.set_background(color)
            self._write_current()

    def choose_shape_fill_color(self):
        item = self.canvas.selected_graphics_item()
        if not isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem)):
            return
        current = item.brush().color()
        color = QColorDialog.getColor(current, self, "Barva výplně tvaru")
        if color.isValid():
            item.setBrush(QBrush(color))
            self._mark_dirty()
            self._write_current()

    def choose_shape_line_color(self):
        item = self.canvas.selected_graphics_item()
        if not isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem, QGraphicsLineItem)):
            return
        current = item.pen().color()
        color = QColorDialog.getColor(current, self, "Barva ohraničení")
        if color.isValid():
            pen = item.pen()
            pen.setColor(color)
            item.setPen(pen)
            self._mark_dirty()
            self._write_current()

    def apply_shape_line_width(self, width: int):
        item = self.canvas.selected_graphics_item()
        if not isinstance(item, (QGraphicsRectItem, QGraphicsEllipseItem, QGraphicsLineItem)):
            return
        pen = item.pen()
        pen.setWidthF(float(width))
        item.setPen(pen)
        self._mark_dirty()
        self._write_current()

    def _format_selected_text(self, char_format: QTextCharFormat):
        item = self.canvas.selected_graphics_item()
        if not isinstance(item, QGraphicsTextItem):
            return
        cursor = item.textCursor()
        old_position = cursor.position()
        had_selection = cursor.hasSelection()
        if not had_selection:
            cursor.select(QTextCursor.Document)
        cursor.mergeCharFormat(char_format)
        item.setTextCursor(cursor)
        if not had_selection:
            cursor.clearSelection()
            cursor.setPosition(min(old_position, max(0, item.document().characterCount() - 1)))
            item.setTextCursor(cursor)
        item.setFocus()
        self._mark_dirty()
        self._write_current()
        self._selection_changed()

    def apply_font_family(self, font: QFont):
        fmt = QTextCharFormat()
        fmt.setFontFamily(font.family())
        self._format_selected_text(fmt)

    def apply_font_size(self, size: int):
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(size))
        self._format_selected_text(fmt)

    def apply_bold(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold if checked else QFont.Normal)
        self._format_selected_text(fmt)

    def apply_italic(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontItalic(bool(checked))
        self._format_selected_text(fmt)

    def choose_text_color(self):
        item = self.canvas.selected_graphics_item()
        if not isinstance(item, QGraphicsTextItem):
            QMessageBox.information(self, "Barva textu", "Nejprve vyberte textové pole.")
            return
        current = item.textCursor().charFormat().foreground().color()
        if not current.isValid():
            current = item.defaultTextColor()
        color = QColorDialog.getColor(current, self, "Barva textu")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._format_selected_text(fmt)

    # --- Soubor -------------------------------------------------------------
    def open_dialog(self):
        dialog = ExplorerOpenDialog(parent=self)
        if dialog.exec_() == QDialog.Accepted and dialog.selected_path:
            self.open_presentation(dialog.selected_path)

    def open_presentation(self, path: str):
        if not self._confirm_discard():
            return
        try:
            meta, slides, assets, width, height = read_presentation(path)
            self._loading = True
            self.path = os.path.abspath(path)
            self.active_index = 0
            self.created = meta.get("created")
            self.title = str(meta.get("title") or Path(path).stem)
            self.slide_width = width
            self.slide_height = height
            self.slides = slides
            self.assets = assets
            self._dirty = False
            self._loading = False
            self.canvas.update_resolution(self.slide_width, self.slide_height)
            self._refresh_slide_list(0)
            self._load_current_slide()
            self._update_resolution_control()
            self._refresh_title()
            self.status_text.setText(f"Otevřeno: {self.path}")
        except Exception as exc:
            self._loading = False
            QMessageBox.critical(self, "Otevření prezentace", str(exc))

    def save(self) -> bool:
        self._write_current()
        if not self.path:
            return self.save_as()
        return self._save_to(self.path)

    def save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Uložit prezentaci", os.path.expanduser("~/Moje nová prezentace VLČR.vlcr"),
                                               "Prezentace VLČR (*.vlcr)")
        if not path:
            return False
        if not path.lower().endswith(".vlcr"):
            path += ".vlcr"
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        try:
            self._write_current()
            meta = write_presentation(path, self.slides, self.assets, self.title,
                                      self.slide_width, self.slide_height, self.created)
            self.path = os.path.abspath(path)
            self.active_index = 0
            self.created = meta.get("created")
            self._dirty = False
            self._refresh_title()
            self._refresh_slide_list(self._current_index())
            self.status_text.setText(f"Uloženo: {self.path}")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Uložení prezentace", str(exc))
            return False

    def export_pdf(self):
        if not self.slides:
            return
        self._write_current()
        path, _ = QFileDialog.getSaveFileName(self, "Exportovat prezentaci do PDF", "Prezentace.pdf", "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(path)
            printer.setOrientation(QPrinter.Landscape)
            painter = QPainter(printer)
            for i, slide in enumerate(self.slides):
                if i:
                    printer.newPage()
                scene = self._scene_from_slide(slide)
                target = printer.pageRect(QPrinter.DevicePixel)
                scene.render(painter, target, scene.sceneRect(), Qt.KeepAspectRatio)
            painter.end()
            self.status_text.setText(f"PDF exportováno: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export PDF", str(exc))

    def _scene_from_slide(self, slide):
        scene = QGraphicsScene(0, 0, self.slide_width, self.slide_height)
        slide_color = QColor(slide.get("background", "#FFFFFF"))
        scene.setBackgroundBrush(slide_color)
        bg_item = QGraphicsRectItem(QRectF(0, 0, self.slide_width, self.slide_height))
        bg_item.setPen(QPen(Qt.NoPen))
        bg_item.setBrush(QBrush(slide_color))
        bg_item.setZValue(-100000)
        scene.addItem(bg_item)
        for data in slide.get("elements", []):
            kind, x, y = data.get("type"), float(data.get("x", 0)), float(data.get("y", 0))
            if kind == "text":
                item = QGraphicsTextItem()
                if data.get("html"):
                    item.setHtml(data["html"])
                else:
                    item.setPlainText(str(data.get("text", "")))
                    font = QFont(str(data.get("font_family", Theme.DEFAULT_FONT)), int(data.get("font_size", 30)))
                    font.setBold(bool(data.get("bold", False)))
                    font.setItalic(bool(data.get("italic", False)))
                    item.setFont(font)
                    item.setDefaultTextColor(QColor(data.get("color", "#202020")))
                item.setTextWidth(float(data.get("w", 800)))
                item.setTransform(QTransform.fromScale(float(data.get("scale_x", 1.0)),
                                                        float(data.get("scale_y", 1.0))))
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 10)))
                scene.addItem(item)
            elif kind == "image":
                raw = self.assets.get(str(data.get("asset", "")))
                if raw:
                    pix = QPixmap()
                    if pix.loadFromData(raw):
                        item = QGraphicsPixmapItem(pix)
                        sx = float(data.get("scale_x", float(data.get("w", pix.width())) / max(1, pix.width())))
                        sy = float(data.get("scale_y", float(data.get("h", pix.height())) / max(1, pix.height())))
                        item.setTransform(QTransform.fromScale(sx, sy))
                        item.setPos(x, y)
                        item.setZValue(float(data.get("z", 5)))
                        scene.addItem(item)
            elif kind in ("rectangle", "ellipse"):
                rect = QRectF(0, 0, float(data.get("w", 300)), float(data.get("h", 150)))
                item = QGraphicsEllipseItem(rect) if kind == "ellipse" else QGraphicsRectItem(rect)
                item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 3))))
                item.setBrush(QBrush(QColor(data.get("fill", "#DCEEFF"))))
                item.setTransform(QTransform.fromScale(float(data.get("scale_x", 1.0)),
                                                       float(data.get("scale_y", 1.0))))
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 2)))
                scene.addItem(item)
            elif kind == "line":
                item = QGraphicsLineItem(0, 0, float(data.get("w", 300)), float(data.get("h", 0)))
                item.setPen(QPen(QColor(data.get("line", "#2D89EF")), float(data.get("line_width", 3))))
                item.setTransform(QTransform.fromScale(float(data.get("scale_x", 1.0)),
                                                       float(data.get("scale_y", 1.0))))
                item.setPos(x, y)
                item.setZValue(float(data.get("z", 2)))
                scene.addItem(item)
        return scene

    # --- Prezentování -------------------------------------------------------
    def start_presentation(self, presenter_mode=False):
        if not self.slides:
            return
        self._write_current()
        if self._dirty:
            answer = QMessageBox.question(self, "Neuložené změny", "Před prezentováním uložit změny?",
                                          QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Yes and not self.save():
                return

        start_idx = self._current_index()
        if presenter_mode:
            # Otevření propojeného diváckého okna a okna přednášejícího naráz
            audience_win = SlideShowViewWindow(self.slides, self.assets, self.slide_width, self.slide_height, start_idx, self)
            presenter_win = PresenterModeWindow(self.slides, self.assets, self.slide_width, self.slide_height, start_idx, audience_win, self)
            self._audience_window = audience_win
            self._presenter_window = presenter_win
            audience_win.show()
            presenter_win.show()
        else:
            window = SlideShowWindow(self.slides, self.assets, self.slide_width, self.slide_height, start_idx, self)
            self._slideshow = window
            window.show()

    def about(self):
        QMessageBox.about(self, "O aplikaci Prezentář",
                          f"<b>Prezentář {APP_VERSION}</b><br>Editor prezentací ze sady VLČR Kancelář.<br><br>"
                          "Vlastní binární formát .vlcr, řečnické poznámky, prezentování a export do PDF.")

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("VLCR")
    app.setFont(QFont(Theme.FONT, 9))
    if os.path.isfile(LOGO_PATH):
        app.setWindowIcon(QIcon(LOGO_PATH))
    initial = argv[0] if argv else None
    window = PrezentarWindow(initial)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())