#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Písař — textový editor ze sady VLČR Kancelář.

Nové dokumenty ukládá do vlastního binárního kontejneru .vlcr (VLCRPISR):
metadata jsou v hlavičce a obsah dokumentu je komprimovaný. Starší ZIP/JSON
dokumenty zůstávají podporované pro zpětnou kompatibilitu.

Závislost: PyQt5 >= 5.15.9
Spuštění:  python pisar.py [dokument.vlcr]
"""

from __future__ import annotations

import base64
import datetime as _datetime
import importlib.util
import json
import os
import re
import struct
import sys
import tempfile
import uuid
import zipfile
import zlib
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from PyQt5.QtCore import Qt, QSize, QUrl, QByteArray, QBuffer, QIODevice, QSettings, pyqtSignal
from PyQt5.QtGui import (
    QColor, QFont, QIcon, QImage, QKeySequence,
    QPainter, QPen, QBrush, QTextCharFormat, QTextBlockFormat, QTextCursor,
    QTextDocument, QTextImageFormat, QTextListFormat, QPixmap
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QAction, QFontComboBox, QVBoxLayout, QHBoxLayout,
    QLabel, QToolBar, QTabWidget, QTextEdit, QComboBox, QPushButton,
    QColorDialog, QFileDialog, QMessageBox, QInputDialog, QStatusBar,
    QProgressDialog, QFrame, QStyle, QMenu, QSizePolicy
)
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog


# =============================================================================
#  KONFIGURACE A VZHLED — upravte hodnoty v této třídě podle vizuální identity
#  VLČR Kancelář. Změny barev, fontů, rozměrů i rozestupů se promítnou do UI.
# =============================================================================
class Theme:
    # Barvy
    WINDOW = "#F4F4F4"          # pozadí hlavního okna
    SURFACE = "#FFFFFF"         # panely a stránka dokumentu
    WORKSPACE = "#E9E9E9"       # plocha za dokumentem
    ACCENT = "#00A300"          # zelená Písaře z palety průzkumníku VLČR
    ACCENT_HOVER = "#078A07"
    TEXT = "#171717"
    TEXT_MUTED = "#666666"
    BORDER = "#D4D4D4"
    TOOLBAR = "#FAFAFA"
    TOOL_HOVER = "#E8F2E8"
    SELECTION = "#0004FF"
    TAB_BACKGROUND = "#E5E5E5"
    TAB_HOVER = "#F1F1F1"
    DEFAULT_HIGHLIGHT = "#FFF176"
    STATUS = "#767676"
    STATUS_TEXT = "#FFFFFF"
    PAGE_TEXT = "#202020"

    # Typografie
    UI_FONT = "Segoe UI"
    DOCUMENT_FONT = "Aptos"
    UI_SIZE = 9
    DOCUMENT_SIZE = 11
    TITLE_SIZE = 26
    H1_SIZE = 22
    H2_SIZE = 17
    H3_SIZE = 14

    # Rozměry a rozestupy (px)
    WINDOW_MIN_WIDTH = 900
    WINDOW_MIN_HEIGHT = 620
    WINDOW_DEFAULT_WIDTH = 1280
    WINDOW_DEFAULT_HEIGHT = 820
    TOOLBAR_HEIGHT = 38
    STATUS_HEIGHT = 25
    WORKSPACE_PADDING = 26
    PAGE_PADDING = 38
    TAB_HEIGHT = 34
    CONTROL_HEIGHT = 28
    CONTROL_GAP = 4
    STYLE_COMBO_WIDTH = 126
    FONT_COMBO_WIDTH = 150
    SIZE_COMBO_WIDTH = 62
    DIALOG_WIDTH = 1040
    DIALOG_HEIGHT = 660
    DIALOG_MIN_WIDTH = 850
    DIALOG_MIN_HEIGHT = 540
    DIALOG_TITLE_FONT_SIZE = 20
    DIALOG_TITLE_PADDING_Y = 18
    DIALOG_TITLE_PADDING_X = 24
    DIALOG_BUTTON_HEIGHT = 32

    # Dokument
    DEFAULT_PAGE_WIDTH = 820
    DEFAULT_IMAGE_MAX_WIDTH = 620
    MAX_VLCR_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
    MAX_ASSET_BYTES = 80 * 1024 * 1024
MAX_CONTAINER_BYTES = 150 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
BINARY_MAGIC = b"VLCRPISR"
BINARY_FORMAT_VERSION = 1
BINARY_HEADER_SIZE = 14  # 8-byte magic + uint16 version + uint32 metadata length


APP_NAME = "Písař"
APP_VERSION = "0.2.0"
APP_ID = "pisar"
APP_DESCRIPTION = "Textový editor ze sady VLČR Kancelář"
META_FILENAME = "metadata.json"
DOCUMENT_FILENAME = "document.html"
ALLOWED_APP_VALUES = {
    "pisar", "writer", "document", "dokument", "text", "doc", "docx"
}
APP_META_KEYS = ("app", "application", "aplikace", "editor", "typ", "type", "format", "kind")
TITLE_META_KEYS = ("title", "nazev", "název", "name")


def _now_iso() -> str:
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(value: object) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _meta_app(meta: dict) -> Optional[str]:
    """Vrátí 'pisar', pokud metadata odpovídají aliasům průzkumníku VLČR."""
    for key in APP_META_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and _normalize(value) in ALLOWED_APP_VALUES:
            return APP_ID
    return None


def _metadata_from_archive(zf: zipfile.ZipFile) -> dict:
    names = {name.lower(): name for name in zf.namelist()}
    meta_name = names.get(META_FILENAME)
    if not meta_name:
        # Kompatibilita se soubory, které používají alternativní názvy průzkumníku.
        for candidate in ("manifest.json", "meta.json"):
            if candidate in names:
                meta_name = names[candidate]
                break
    if not meta_name:
        raise ValueError("Soubor .vlcr neobsahuje metadata.json.")
    raw = json.loads(zf.read(meta_name).decode("utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Metadata souboru mají neplatnou strukturu.")
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else raw
    if _meta_app(meta) != APP_ID:
        raise ValueError("Tento soubor není dokument Písaře (v metadatech chybí rozpoznaný typ aplikace).")
    return meta


def _validate_pisar_metadata(raw_meta: object) -> dict:
    if not isinstance(raw_meta, dict):
        raise ValueError("Metadata souboru mají neplatnou strukturu.")
    meta = raw_meta.get("metadata") if isinstance(raw_meta.get("metadata"), dict) else raw_meta
    if _meta_app(meta) != APP_ID:
        raise ValueError("Tento soubor není dokument Písaře (v metadatech chybí rozpoznaný typ aplikace).")
    return meta


def _decode_binary_document(path: str) -> Tuple[str, Dict[str, bytes], dict]:
    """Načte nativní neprůhledný kontejner VLCRPISR."""
    size = os.path.getsize(path)
    if size < BINARY_HEADER_SIZE or size > MAX_CONTAINER_BYTES:
        raise ValueError("Soubor je prázdný nebo překračuje povolenou velikost.")

    with open(path, "rb") as handle:
        header = handle.read(BINARY_HEADER_SIZE)
        if len(header) != BINARY_HEADER_SIZE or header[:8] != BINARY_MAGIC:
            raise ValueError("Soubor nemá platnou hlavičku Písaře.")
        version, meta_len = struct.unpack(">HI", header[8:14])
        if version != BINARY_FORMAT_VERSION:
            raise ValueError(f"Nepodporovaná verze dokumentu Písaře: {version}.")
        if meta_len <= 0 or meta_len > MAX_METADATA_BYTES:
            raise ValueError("Metadata dokumentu mají nepovolenou velikost.")
        meta_raw = handle.read(meta_len)
        if len(meta_raw) != meta_len:
            raise ValueError("Soubor je poškozený — neúplná metadata.")
        compressed = handle.read(MAX_CONTAINER_BYTES + 1)

    try:
        meta = _validate_pisar_metadata(json.loads(meta_raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Metadata dokumentu nejsou platná ({exc}).") from exc

    decoder = zlib.decompressobj()
    try:
        payload_raw = decoder.decompress(compressed, Theme.MAX_VLCR_UNCOMPRESSED_BYTES + 1)
        if len(payload_raw) > Theme.MAX_VLCR_UNCOMPRESSED_BYTES or decoder.unconsumed_tail:
            raise ValueError("Rozbalený dokument překračuje povolenou velikost.")
        payload_raw += decoder.flush()
    except zlib.error as exc:
        raise ValueError(f"Obsah dokumentu je poškozený ({exc}).") from exc
    if (len(payload_raw) > Theme.MAX_VLCR_UNCOMPRESSED_BYTES or not decoder.eof
            or decoder.unused_data):
        raise ValueError("Obsah dokumentu je poškozený, neúplný nebo obsahuje přebytečná data.")

    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Obsah dokumentu nemá platnou strukturu ({exc}).") from exc
    if not isinstance(payload, dict):
        raise ValueError("Obsah dokumentu má neplatnou strukturu.")

    html = payload.get("document_html")
    if not isinstance(html, str):
        raise ValueError("V souboru chybí obsah dokumentu.")

    assets: Dict[str, bytes] = {}
    asset_data = payload.get("assets", {})
    if isinstance(asset_data, dict):
        total_assets = 0
        for name, encoded in asset_data.items():
            if not isinstance(name, str) or not isinstance(encoded, str):
                continue
            normalized = name.replace("\\", "/")
            parts = Path(normalized).parts
            if (not normalized.startswith("assets/") or normalized.startswith("/")
                    or ".." in parts or len(parts) != 2):
                continue
            try:
                data = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (ValueError, UnicodeEncodeError):
                continue
            total_assets += len(data)
            if total_assets > Theme.MAX_ASSET_BYTES:
                raise ValueError("Vložené soubory v dokumentu jsou příliš velké.")
            assets[normalized] = data
    return html, assets, meta


def read_vlcr_document(path: str) -> Tuple[str, Dict[str, bytes], dict]:
    """Otevře nativní VLCRPISR; starší ZIP/JSON dokumenty načte kvůli kompatibilitě."""
    path = os.path.abspath(path)
    if not path.lower().endswith(".vlcr"):
        raise ValueError("Písař otevírá pouze soubory s příponou .vlcr.")

    with open(path, "rb") as handle:
        magic = handle.read(8)
    if magic == BINARY_MAGIC:
        return _decode_binary_document(path)

    # Zpětná kompatibilita: dřívější Písař ukládal dokumenty jako ZIP.
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            total = sum(info.file_size for info in infos)
            if total > Theme.MAX_VLCR_UNCOMPRESSED_BYTES:
                raise ValueError("Dokument je příliš velký a nelze jej bezpečně otevřít.")
            for info in infos:
                normalized = info.filename.replace("\\", "/")
                if normalized.startswith("/") or ".." in Path(normalized).parts:
                    raise ValueError("Archiv obsahuje nepovolenou cestu k souboru.")

            meta = _metadata_from_archive(zf)
            names = {name.replace("\\", "/"): name for name in zf.namelist()}
            html_name = names.get(DOCUMENT_FILENAME)
            if not html_name:
                raise ValueError("V souboru chybí document.html.")
            html = zf.read(html_name).decode("utf-8-sig")
            assets: Dict[str, bytes] = {}
            asset_total = 0
            for normalized, actual in names.items():
                if not normalized.startswith("assets/") or normalized.endswith("/"):
                    continue
                relative = normalized[len("assets/"):]
                if not relative or "/" in relative or "\\" in relative:
                    raise ValueError("Archiv obsahuje nepovolený název vloženého souboru.")
                data = zf.read(actual)
                asset_total += len(data)
                if asset_total > Theme.MAX_ASSET_BYTES:
                    raise ValueError("Vložené soubory v dokumentu jsou příliš velké.")
                assets[normalized] = data
        return html, assets, meta

    # Starší samostatné JSON dokumenty také zůstávají čitelné.
    if os.path.getsize(path) > Theme.MAX_VLCR_UNCOMPRESSED_BYTES:
        raise ValueError("Dokument je příliš velký a nelze jej bezpečně otevřít.")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        meta = _validate_pisar_metadata(raw)
        html = raw.get("document_html") or raw.get("html") or meta.get("document_html") or meta.get("html")
        if not isinstance(html, str):
            plain = raw.get("text") or raw.get("content") or meta.get("text") or meta.get("content")
            if isinstance(plain, str):
                import html as _html
                html = "<html><body><pre>" + _html.escape(plain) + "</pre></body></html>"
        if not isinstance(html, str):
            raise ValueError("JSON soubor obsahuje metadata Písaře, ale chybí mu obsah dokumentu.")
        return html, {}, meta
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Soubor .vlcr nemá rozpoznaný formát Písaře ({exc}).") from exc


def write_vlcr_document(path: str, html: str, assets: Dict[str, bytes], title: str,
                        created: Optional[str] = None) -> dict:
    """Zapíše dokument do vlastního binárního formátu VLCRPISR (nikoli ZIP/HTML)."""
    path = os.path.abspath(path)
    if not path.lower().endswith(".vlcr"):
        path += ".vlcr"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    meta = {
        "app": "pisar",
        "application": "pisar",
        "title": title.strip() or "Nepojmenovaný dokument",
        "format": "vlcr-rich-text",
        "format_version": BINARY_FORMAT_VERSION,
        "container_format": BINARY_MAGIC.decode("ascii"),
        "container_version": BINARY_FORMAT_VERSION,
        "created": created or _now_iso(),
        "modified": _now_iso(),
        "content_encoding": "zlib+json",
        "generator": f"{APP_NAME} {APP_VERSION}",
    }

    meta_raw = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(meta_raw) > MAX_METADATA_BYTES:
        raise ValueError("Metadata dokumentu jsou příliš rozsáhlá.")

    serial_assets = {}
    total_assets = 0
    for name, data in assets.items():
        safe_name = str(name).replace("\\", "/")
        parts = Path(safe_name).parts
        if (not safe_name.startswith("assets/") or safe_name.startswith("/")
                or ".." in parts or len(parts) != 2):
            continue
        if not isinstance(data, (bytes, bytearray)):
            continue
        total_assets += len(data)
        if total_assets > Theme.MAX_ASSET_BYTES:
            raise ValueError("Vložené soubory v dokumentu jsou příliš velké.")
        serial_assets[safe_name] = base64.b64encode(bytes(data)).decode("ascii")

    payload_raw = json.dumps(
        {"document_html": html, "assets": serial_assets},
        ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload_raw) > Theme.MAX_VLCR_UNCOMPRESSED_BYTES:
        raise ValueError("Dokument je příliš velký.")
    compressed = zlib.compress(payload_raw, level=8)
    header = BINARY_MAGIC + struct.pack(">HI", BINARY_FORMAT_VERSION, len(meta_raw)) + meta_raw

    fd, temp_path = tempfile.mkstemp(prefix=".pisar-", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(header)
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.getsize(temp_path) > MAX_CONTAINER_BYTES:
            raise ValueError("Výsledný dokument překračuje povolenou velikost souboru.")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return meta


# =============================================================================
#  PANEL VLOŽENÍ / OTEVŘENÍ — využívá dodaný FileExplorerWidget
# =============================================================================
def _resource_directory() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class ExplorerOpenDialog:
    """Spustí samostatný Průzkumník VLČR v režimu výběru dokumentu."""

    @staticmethod
    def select_document(parent=None, start_path=None) -> Optional[str]:
        explorer_path = os.path.join(
            _resource_directory(),
            "pruzkumnik"
        )

        if not os.path.isfile(explorer_path):
            QMessageBox.critical(
                parent,
                "Průzkumník nebyl nalezen",
                "Ve složce Písaře chybí pruzkumnik.\n\n"
                f"Očekávané umístění:\n{explorer_path}"
            )
            return None

        try:
            result = subprocess.run(
                [explorer_path, "--select"],
                cwd=_resource_directory(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace"
            )

            if result.returncode != 0:
                error = result.stderr.strip() or "Průzkumník skončil s chybou."
                QMessageBox.warning(
                    parent,
                    "Chyba Průzkumníku",
                    error
                )
                return None

            selected_path = result.stdout.strip()

            # Uživatel zavřel Průzkumníka bez výběru.
            if not selected_path:
                return None

            selected_path = os.path.abspath(selected_path)

            if not selected_path.lower().endswith(".vlcr"):
                QMessageBox.warning(
                    parent,
                    "Neplatný soubor",
                    "Vybraný soubor není dokument VLČR."
                )
                return None

            if not os.path.isfile(selected_path):
                QMessageBox.warning(
                    parent,
                    "Soubor nebyl nalezen",
                    f"Vybraný soubor neexistuje:\n{selected_path}"
                )
                return None

            return selected_path

        except Exception as exc:
            QMessageBox.critical(
                parent,
                "Průzkumník nelze spustit",
                str(exc)
            )
            return None


# =============================================================================
#  JEDEN DOKUMENT / KARTA
# =============================================================================
class DocumentTab(QWidget):
    dirty_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: Optional[str] = None
        self.created: Optional[str] = None
        self.assets: Dict[str, bytes] = {}
        self.title = "Nepojmenovaný dokument"
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        workspace = QWidget()
        workspace.setObjectName("documentWorkspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(Theme.WORKSPACE_PADDING, Theme.WORKSPACE_PADDING,
                                            Theme.WORKSPACE_PADDING, Theme.WORKSPACE_PADDING)
        workspace_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(True)
        self.editor.setTabChangesFocus(False)
        self.editor.setLineWrapMode(QTextEdit.WidgetWidth)
        self.editor.setMinimumWidth(500)
        self.editor.setMaximumWidth(Theme.DEFAULT_PAGE_WIDTH)
        self.editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.editor.setFont(QFont(Theme.DOCUMENT_FONT, Theme.DOCUMENT_SIZE))
        self.editor.setStyleSheet(
            f"QTextEdit{{background:{Theme.SURFACE}; color:{Theme.PAGE_TEXT};"
            f"border:1px solid {Theme.BORDER}; padding:{Theme.PAGE_PADDING}px;"
            f"selection-background-color:{Theme.SELECTION};}}"
        )
        self.editor.document().setDocumentMargin(0)
        self.editor.setPlaceholderText("Začněte psát…")
        self.editor.document().modificationChanged.connect(self._on_modified)
        workspace_layout.addWidget(self.editor, 1)
        outer.addWidget(workspace, 1)
        self._dirty = False

    def _on_modified(self, modified: bool):
        self._dirty = bool(modified)
        self.dirty_changed.emit(self._dirty)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def load(self, path: str):
        html, assets, meta = read_vlcr_document(path)
        self._loading = True
        try:
            self.editor.setHtml(html)
            self.assets = assets
            for name, raw in self.assets.items():
                image = QImage.fromData(raw)
                if not image.isNull():
                    self.editor.document().addResource(QTextDocument.ImageResource, QUrl(name), image)
            self.editor.document().setModified(False)
            self.path = os.path.abspath(path)
            self.created = meta.get("created")
            self.title = next((meta.get(k) for k in TITLE_META_KEYS if isinstance(meta.get(k), str)),
                              Path(path).stem)
            self.editor.document().setModified(False)
        finally:
            self._loading = False
        self._dirty = False

    def save(self, path: Optional[str] = None):
        target = path or self.path
        if not target:
            raise ValueError("Nejdříve zvolte název a umístění dokumentu.")
        if not target.lower().endswith(".vlcr"):
            target += ".vlcr"
        title = Path(target).stem if path else self.title
        html = self.editor.document().toHtml()
        write_vlcr_document(target, html, self.assets, title, self.created)
        self.path = os.path.abspath(target)
        self.title = title
        if not self.created:
            self.created = _now_iso()
        self.editor.document().setModified(False)
        self._dirty = False

    def add_image_bytes(self, data: bytes, width: Optional[int] = None):
        image = QImage.fromData(data)
        if image.isNull():
            raise ValueError("Obrázek má nepodporovaný nebo poškozený formát.")
        buffer = QByteArray()
        stream = QBuffer(buffer)
        stream.open(QIODevice.WriteOnly)
        image.save(stream, "PNG")
        stream.close()
        normalized = bytes(buffer)
        name = f"assets/image_{uuid.uuid4().hex[:12]}.png"
        self.assets[name] = normalized
        self.editor.document().addResource(QTextDocument.ImageResource, QUrl(name), image)

        fmt = QTextImageFormat()
        fmt.setName(name)
        display_width = min(image.width(), width or Theme.DEFAULT_IMAGE_MAX_WIDTH)
        fmt.setWidth(float(display_width))
        fmt.setHeight(float(image.height() * display_width / max(1, image.width())))
        cursor = self.editor.textCursor()
        cursor.insertImage(fmt)
        self.editor.setTextCursor(cursor)

    def add_shape(self, shape: str):
        pix = QPixmap(520, 180)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(Theme.ACCENT), 5)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QColor(Theme.ACCENT).lighter(185))
        if shape == "Obdélník":
            painter.drawRoundedRect(55, 35, 400, 110, 8, 8)
        elif shape == "Elipsa":
            painter.drawEllipse(125, 20, 270, 140)
        elif shape == "Čára":
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(55, 90, 465, 90)
        elif shape == "Šipka":
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(55, 90, 430, 90)
            painter.drawLine(430, 90, 375, 50)
            painter.drawLine(430, 90, 375, 130)
        elif shape == "Bublina":
            painter.drawRoundedRect(65, 20, 385, 115, 24, 24)
            painter.drawLine(125, 135, 95, 165)
            painter.drawLine(95, 165, 155, 135)
        painter.end()
        buffer = QByteArray()
        stream = QBuffer(buffer)
        stream.open(QIODevice.WriteOnly)
        pix.save(stream, "PNG")
        stream.close()
        self.add_image_bytes(bytes(buffer), width=520)


# =============================================================================
#  HLAVNÍ APLIKACE
# =============================================================================
class PisarWindow(QMainWindow):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.settings = QSettings("VLCR", "Pisar")
        self.setWindowTitle(f"{APP_NAME} — VLČR Kancelář")
        self.setMinimumSize(Theme.WINDOW_MIN_WIDTH, Theme.WINDOW_MIN_HEIGHT)
        self.resize(Theme.WINDOW_DEFAULT_WIDTH, Theme.WINDOW_DEFAULT_HEIGHT)
        self.setAcceptDrops(True)
        self._building_toolbar = False

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_tabs()
        self._build_status()
        self._apply_theme()
        self._restore_window()

        if initial_path:
            self.open_document(initial_path)
        else:
            self.new_document()

    # --- Actions -------------------------------------------------------------
    def _make_action(self, text: str, shortcut: Optional[str], slot, checkable=False) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        action.triggered.connect(slot)
        return action

    def _build_actions(self):
        self.act_new = self._make_action("Nový dokument", "Ctrl+N", self.new_document)
        self.act_open = self._make_action("Otevřít…", "Ctrl+O", self.open_dialog)
        self.act_save = self._make_action("Uložit", "Ctrl+S", self.save_document)
        self.act_save_as = self._make_action("Uložit jako…", "Ctrl+Shift+S", self.save_as)
        self.act_close_tab = self._make_action("Zavřít kartu", "Ctrl+W", self.close_current_tab)
        self.act_print = self._make_action("Tisk…", "Ctrl+P", self.print_document)
        self.act_pdf = self._make_action("Exportovat do PDF…", None, self.export_pdf)
        self.act_undo = self._make_action("Zpět", "Ctrl+Z", self._undo)
        self.act_redo = self._make_action("Znovu", "Ctrl+Y", self._redo)
        self.act_cut = self._make_action("Vyjmout", "Ctrl+X", self._cut)
        self.act_copy = self._make_action("Kopírovat", "Ctrl+C", self._copy)
        self.act_paste = self._make_action("Vložit", "Ctrl+V", self._paste)
        self.act_select_all = self._make_action("Vybrat vše", "Ctrl+A", self._select_all)
        self.act_bold = self._make_action("Tučně", "Ctrl+B", self._toggle_bold, True)
        self.act_italic = self._make_action("Kurzíva", "Ctrl+I", self._toggle_italic, True)
        self.act_underline = self._make_action("Podtržení", "Ctrl+U", self._toggle_underline, True)
        self.act_strike = self._make_action("Přeškrtnutí", None, self._toggle_strike, True)
        self.act_bullets = self._make_action("Odrážky", None, self._insert_bullets)
        self.act_numbers = self._make_action("Číslování", None, self._insert_numbers)
        self.act_image = self._make_action("Obrázek…", None, self.insert_image)
        self.act_shape = self._make_action("Tvar…", None, self.insert_shape)
        self.act_table = self._make_action("Tabulka…", None, self.insert_table)

    # --- Menu ---------------------------------------------------------------
    def _build_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("Soubor")
        for action in (self.act_new, self.act_open, self.act_save, self.act_save_as):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(self.act_print)
        file_menu.addAction(self.act_pdf)
        file_menu.addSeparator()
        file_menu.addAction(self.act_close_tab)

        edit_menu = bar.addMenu("Úpravy")
        for action in (self.act_undo, self.act_redo):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        for action in (self.act_cut, self.act_copy, self.act_paste, self.act_select_all):
            edit_menu.addAction(action)

        format_menu = bar.addMenu("Formát")
        for action in (self.act_bold, self.act_italic, self.act_underline, self.act_strike):
            format_menu.addAction(action)
        format_menu.addSeparator()
        format_menu.addAction("Barva textu…", self.choose_text_color)
        format_menu.addAction("Zvýraznění…", self.choose_highlight)
        format_menu.addAction("Zarovnat vlevo", lambda: self.set_alignment(Qt.AlignLeft))
        format_menu.addAction("Na střed", lambda: self.set_alignment(Qt.AlignHCenter))
        format_menu.addAction("Zarovnat vpravo", lambda: self.set_alignment(Qt.AlignRight))
        format_menu.addAction("Do bloku", lambda: self.set_alignment(Qt.AlignJustify))

        insert_menu = bar.addMenu("Vložení")
        insert_menu.addAction(self.act_image)
        insert_menu.addAction(self.act_shape)
        insert_menu.addAction(self.act_table)
        insert_menu.addSeparator()
        insert_menu.addAction(self.act_bullets)
        insert_menu.addAction(self.act_numbers)

        help_menu = bar.addMenu("Nápověda")
        help_menu.addAction("O aplikaci Písař", self.about)

    # --- Toolbar ------------------------------------------------------------
    def _build_toolbar(self):
        toolbar = QToolBar("Formátování")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setFixedHeight(Theme.TOOLBAR_HEIGHT + 8)
        self.addToolBar(Qt.TopToolBarArea, toolbar)

        for action in (self.act_new, self.act_open, self.act_save):
            toolbar.addAction(action)
        toolbar.addSeparator()

        self.style_combo = QComboBox()
        self.style_combo.setToolTip("Styl odstavce")
        self.style_combo.addItems(["Normální", "Titulek", "Nadpis 1", "Nadpis 2", "Nadpis 3", "Podnadpis"])
        self.style_combo.setFixedWidth(Theme.STYLE_COMBO_WIDTH)
        self.style_combo.currentTextChanged.connect(self.apply_paragraph_style)
        toolbar.addWidget(self.style_combo)

        self.font_combo = QFontComboBox()
        self.font_combo.setToolTip("Typ písma")
        self.font_combo.setFixedWidth(Theme.FONT_COMBO_WIDTH)
        self.font_combo.currentFontChanged.connect(self.apply_font_family)
        toolbar.addWidget(self.font_combo)

        self.size_combo = QComboBox()
        self.size_combo.setToolTip("Velikost písma")
        self.size_combo.setEditable(True)
        self.size_combo.addItems(["8", "9", "10", "11", "12", "14", "16", "18", "20", "24", "28", "36", "48", "72"])
        self.size_combo.setCurrentText(str(Theme.DOCUMENT_SIZE))
        self.size_combo.setFixedWidth(Theme.SIZE_COMBO_WIDTH)
        self.size_combo.setMaximumHeight(Theme.CONTROL_HEIGHT)
        self.size_combo.activated.connect(lambda *_: self.apply_font_size())
        self.size_combo.lineEdit().editingFinished.connect(self.apply_font_size)
        toolbar.addWidget(self.size_combo)
        toolbar.addSeparator()

        for action in (self.act_bold, self.act_italic, self.act_underline, self.act_strike):
            toolbar.addAction(action)
        toolbar.addSeparator()

        color_btn = QPushButton("A ▾")
        color_btn.setToolTip("Barva textu")
        color_btn.clicked.connect(self.choose_text_color)
        toolbar.addWidget(color_btn)
        highlight_btn = QPushButton("Zvýraznit")
        highlight_btn.setToolTip("Zvýraznit vybraný text")
        highlight_btn.clicked.connect(self.choose_highlight)
        toolbar.addWidget(highlight_btn)
        toolbar.addSeparator()

        align_menu = QMenu(self)
        align_menu.addAction("Vlevo", lambda: self.set_alignment(Qt.AlignLeft))
        align_menu.addAction("Na střed", lambda: self.set_alignment(Qt.AlignHCenter))
        align_menu.addAction("Vpravo", lambda: self.set_alignment(Qt.AlignRight))
        align_menu.addAction("Do bloku", lambda: self.set_alignment(Qt.AlignJustify))
        self.align_button = QPushButton("Zarovnání ▾")
        self.align_button.setMenu(align_menu)
        toolbar.addWidget(self.align_button)
        toolbar.addAction(self.act_bullets)
        toolbar.addAction(self.act_numbers)
        toolbar.addSeparator()
        toolbar.addAction(self.act_image)
        toolbar.addAction(self.act_shape)
        toolbar.addAction(self.act_table)

    # --- Tabs and status ----------------------------------------------------
    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

    def _build_status(self):
        status = QStatusBar()
        status.setFixedHeight(Theme.STATUS_HEIGHT)
        self.setStatusBar(status)
        self.status_label = QLabel("Připraveno")
        self.word_label = QLabel("")
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.word_label)
        status.addPermanentWidget(QLabel(f"{APP_NAME} {APP_VERSION}"))

    def _apply_theme(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget#documentWorkspace {{ background: {Theme.WINDOW}; }}
            QMenuBar {{ background:{Theme.SURFACE}; color:{Theme.TEXT}; border-bottom:1px solid {Theme.BORDER}; padding:3px; }}
            QMenuBar::item {{ padding:6px 10px; background:transparent; }}
            QMenuBar::item:selected {{ background:{Theme.TOOL_HOVER}; }}
            QMenu {{ background:{Theme.SURFACE}; color:{Theme.TEXT}; border:1px solid {Theme.BORDER}; padding:4px; }}
            QMenu::item {{ padding:6px 24px 6px 22px; }}
            QMenu::item:selected {{ background:{Theme.TOOL_HOVER}; }}
            QToolBar {{ background:{Theme.TOOLBAR}; border:0; border-bottom:1px solid {Theme.BORDER}; spacing:{Theme.CONTROL_GAP}px; padding:4px 8px; }}
            QToolButton {{ color:{Theme.TEXT}; padding:5px 7px; border:1px solid transparent; border-radius:2px; }}
            QToolButton:hover {{ background:{Theme.TOOL_HOVER}; border-color:{Theme.BORDER}; }}
            QToolButton:checked {{ background:{Theme.SELECTION}; border-color:{Theme.ACCENT}; }}
            QComboBox, QFontComboBox, QPushButton {{ background:{Theme.SURFACE}; color:{Theme.TEXT}; border:1px solid {Theme.BORDER}; padding:4px 6px; min-height:20px; }}
            QComboBox:hover, QFontComboBox:hover, QPushButton:hover {{ border-color:{Theme.ACCENT}; }}
            QComboBox QAbstractItemView {{ background:{Theme.SURFACE}; selection-background-color:{Theme.SELECTION}; color:{Theme.TEXT}; }}
            QTabWidget::pane {{ border:0; background:{Theme.WORKSPACE}; }}
            QTabBar::tab {{ background:{Theme.TAB_BACKGROUND}; color:{Theme.TEXT}; padding:8px 18px; min-width:100px; height:{Theme.TAB_HEIGHT}px; border-right:1px solid {Theme.BORDER}; }}
            QTabBar::tab:selected {{ background:{Theme.WORKSPACE}; border-top:3px solid {Theme.ACCENT}; }}
            QTabBar::tab:hover:!selected {{ background:{Theme.TAB_HOVER}; }}
            QStatusBar {{ background:{Theme.STATUS}; color:{Theme.STATUS_TEXT}; border:0; }}
            QStatusBar QLabel {{ color:{Theme.STATUS_TEXT}; padding:0 8px; }}
            QScrollBar:vertical {{ background:transparent; width:11px; margin:0; }}
            QScrollBar::handle:vertical {{ background:#B8B8B8; min-height:26px; }}
            QScrollBar::handle:vertical:hover {{ background:#929292; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        """)

    # --- Helpers ------------------------------------------------------------
    def current_tab(self) -> Optional[DocumentTab]:
        widget = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        return widget if isinstance(widget, DocumentTab) else None

    def _tab_caption(self, tab: DocumentTab) -> str:
        name = Path(tab.path).name if tab.path else tab.title
        return ("● " if tab.is_dirty else "") + name

    def _refresh_tab_caption(self, tab: Optional[DocumentTab] = None):
        tab = tab or self.current_tab()
        if tab is None:
            return
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabText(index, self._tab_caption(tab))
            self.tabs.setTabToolTip(index, tab.path or tab.title)
        self._update_window_title()

    def _update_window_title(self):
        tab = self.current_tab()
        if tab is None:
            self.setWindowTitle(f"{APP_NAME} — VLČR Kancelář")
            return
        name = Path(tab.path).name if tab.path else tab.title
        suffix = "*" if tab.is_dirty else ""
        self.setWindowTitle(f"{name}{suffix} — {APP_NAME} — VLČR Kancelář")

    def _connect_tab(self, tab: DocumentTab):
        tab.editor.textChanged.connect(lambda t=tab: self._on_document_changed(t))
        tab.dirty_changed.connect(lambda _dirty, t=tab: self._refresh_tab_caption(t))
        tab.editor.cursorPositionChanged.connect(self._update_format_controls)

    def _on_document_changed(self, tab: DocumentTab):
        if not tab._loading:
            self._refresh_tab_caption(tab)
        if tab is self.current_tab():
            self._update_word_count()

    def _on_tab_changed(self, _index: int):
        self._update_format_controls()
        self._update_word_count()
        self._update_window_title()

    def _update_word_count(self):
        tab = self.current_tab()
        if tab is None:
            self.word_label.setText("")
            return
        text = tab.editor.toPlainText()
        words = len(re.findall(r"\S+", text))
        chars = len(text)
        self.word_label.setText(f"{words} slov  ·  {chars} znaků")

    def _update_format_controls(self):
        tab = self.current_tab()
        if tab is None:
            return
        fmt = tab.editor.currentCharFormat()
        self._building_toolbar = True
        try:
            self.act_bold.setChecked(fmt.fontWeight() >= QFont.Bold)
            self.act_italic.setChecked(fmt.fontItalic())
            self.act_underline.setChecked(fmt.fontUnderline())
            self.act_strike.setChecked(fmt.fontStrikeOut())
            if fmt.fontFamily():
                self.font_combo.setCurrentFont(QFont(fmt.fontFamily()))
            size = float(fmt.fontPointSize())
            if size > 0:
                self.size_combo.setEditText(str(int(size)) if size.is_integer() else f"{size:g}")
        finally:
            self._building_toolbar = False

    def _editor(self) -> Optional[QTextEdit]:
        tab = self.current_tab()
        return tab.editor if tab else None

    def _merge_char_format(self, fmt: QTextCharFormat):
        editor = self._editor()
        if editor is None:
            return
        editor.mergeCurrentCharFormat(fmt)
        editor.setFocus()

    # --- New/open/save ------------------------------------------------------
    def new_document(self):
        tab = DocumentTab()
        self._connect_tab(tab)
        index = self.tabs.addTab(tab, self._tab_caption(tab))
        self.tabs.setCurrentIndex(index)
        tab.editor.setFocus()
        self._update_word_count()

    def open_dialog(self):
        tab = self.current_tab()
        start = os.path.dirname(tab.path) if tab and tab.path else None

        selected_path = ExplorerOpenDialog.select_document(
            parent=self,
            start_path=start
        )

        if selected_path:
            try:
                read_vlcr_document(selected_path)
                self.open_document(selected_path)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Dokument nelze otevřít",
                    str(exc)
                )

    def open_document(self, path: str):
        path = os.path.abspath(path)
        if not path.lower().endswith(".vlcr"):
            QMessageBox.warning(self, "Nelze otevřít", "Písař otevírá pouze soubory .vlcr.")
            return
        for i in range(self.tabs.count()):
            existing = self.tabs.widget(i)
            if isinstance(existing, DocumentTab) and existing.path and os.path.normcase(existing.path) == os.path.normcase(path):
                self.tabs.setCurrentIndex(i)
                return
        try:
            tab = DocumentTab()
            tab.load(path)
            self._connect_tab(tab)
            index = self.tabs.addTab(tab, self._tab_caption(tab))
            self.tabs.setCurrentIndex(index)
            tab.editor.setFocus()
            self.settings.setValue("recent/last_directory", os.path.dirname(path))
            self.status_label.setText(f"Otevřeno: {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Dokument nelze otevřít", str(exc))

    def save_document(self, *_args):
        tab = self.current_tab()
        if tab is None:
            return
        if not tab.path:
            self.save_as()
            return
        try:
            tab.save()
            self._refresh_tab_caption(tab)
            self.status_label.setText(f"Uloženo: {Path(tab.path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Ukládání se nezdařilo", str(exc))

    def save_as(self, *_args):
        tab = self.current_tab()
        if tab is None:
            return
        suggested = tab.path or os.path.join(
            self.settings.value("recent/last_directory", os.path.expanduser("~/Documents")),
            "Můj nový dokument VLČR.vlcr"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Uložit dokument Písaře", suggested, "Dokument VLČR (*.vlcr)"
        )
        if not path:
            return
        if not path.lower().endswith(".vlcr"):
            path += ".vlcr"
        try:
            tab.save(path)
            self.settings.setValue("recent/last_directory", os.path.dirname(path))
            self._refresh_tab_caption(tab)
            self.status_label.setText(f"Uloženo: {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Ukládání se nezdařilo", str(exc))

    def _ask_save(self, tab: DocumentTab) -> bool:
        if not tab.is_dirty:
            return True
        name = Path(tab.path).name if tab.path else tab.title
        answer = QMessageBox.question(
            self, "Neuložené změny",
            f"Dokument „{name}“ obsahuje neuložené změny. Chcete je uložit?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            self.tabs.setCurrentWidget(tab)
            self.save_document()
            return not tab.is_dirty
        return True

    def close_tab(self, index: int):
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocumentTab) or not self._ask_save(tab):
            return
        self.tabs.removeTab(index)
        tab.deleteLater()
        if self.tabs.count() == 0:
            self.new_document()
        self._update_window_title()

    def close_current_tab(self):
        index = self.tabs.currentIndex()
        if index >= 0:
            self.close_tab(index)

    # --- Text formatting ----------------------------------------------------
    def _toggle_bold(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold if checked else QFont.Normal)
        self._merge_char_format(fmt)

    def _toggle_italic(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontItalic(checked)
        self._merge_char_format(fmt)

    def _toggle_underline(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(checked)
        self._merge_char_format(fmt)

    def _toggle_strike(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(checked)
        self._merge_char_format(fmt)

    def apply_font_family(self, font: QFont):
        if self._building_toolbar:
            return
        fmt = QTextCharFormat()
        fmt.setFontFamily(font.family())
        self._merge_char_format(fmt)

    def apply_font_size(self):
        if self._building_toolbar:
            return
        try:
            size = float(self.size_combo.currentText().replace(",", "."))
            if not 4 <= size <= 200:
                raise ValueError
        except ValueError:
            QMessageBox.information(self, "Velikost písma", "Zadejte velikost v rozsahu 4 až 200 bodů.")
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(size)
        self._merge_char_format(fmt)

    def apply_paragraph_style(self, style: str):
        editor = self._editor()
        if editor is None:
            return
        styles = {
            "Normální": (Theme.DOCUMENT_SIZE, QFont.Normal, 0, 8, 5),
            "Titulek": (Theme.TITLE_SIZE, QFont.DemiBold, 0, 16, 8),
            "Nadpis 1": (Theme.H1_SIZE, QFont.Bold, 0, 14, 7),
            "Nadpis 2": (Theme.H2_SIZE, QFont.DemiBold, 0, 12, 6),
            "Nadpis 3": (Theme.H3_SIZE, QFont.DemiBold, 0, 10, 4),
            "Podnadpis": (15, QFont.Normal, 0, 2, 12),
        }
        size, weight, _unused, top, bottom = styles.get(style, styles["Normální"])
        cursor = editor.textCursor()
        had_selection = cursor.hasSelection()
        original_position = cursor.position()
        if not had_selection:
            cursor.select(QTextCursor.BlockUnderCursor)
        block_fmt = QTextBlockFormat()
        block_fmt.setTopMargin(top)
        block_fmt.setBottomMargin(bottom)
        block_fmt.setAlignment(Qt.AlignHCenter if style == "Titulek" else Qt.AlignLeft)
        cursor.mergeBlockFormat(block_fmt)
        char_fmt = QTextCharFormat()
        char_fmt.setFontPointSize(float(size))
        char_fmt.setFontWeight(weight)
        if style == "Podnadpis":
            char_fmt.setForeground(QColor(Theme.TEXT_MUTED))
        elif style == "Normální":
            char_fmt.setForeground(QColor(Theme.PAGE_TEXT))
        cursor.mergeCharFormat(char_fmt)
        if not had_selection:
            cursor.clearSelection()
            cursor.setPosition(original_position)
        editor.setTextCursor(cursor)
        editor.setFocus()

    def choose_text_color(self):
        color = QColorDialog.getColor(QColor(Theme.TEXT), self, "Barva textu")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._merge_char_format(fmt)

    def choose_highlight(self):
        color = QColorDialog.getColor(QColor(Theme.DEFAULT_HIGHLIGHT), self, "Barva zvýraznění")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            self._merge_char_format(fmt)

    def set_alignment(self, alignment):
        editor = self._editor()
        if editor:
            editor.setAlignment(alignment)
            editor.setFocus()

    def _insert_bullets(self):
        editor = self._editor()
        if editor:
            editor.textCursor().insertList(QTextListFormat.ListDisc)
            editor.setFocus()

    def _insert_numbers(self):
        editor = self._editor()
        if editor:
            editor.textCursor().insertList(QTextListFormat.ListDecimal)
            editor.setFocus()

    # --- Insert content -----------------------------------------------------
    def insert_image(self):
        tab = self.current_tab()
        if tab is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Vložit obrázek", "",
            "Obrázky (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;Všechny soubory (*)"
        )
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                tab.add_image_bytes(handle.read())
        except Exception as exc:
            QMessageBox.warning(self, "Obrázek nelze vložit", str(exc))

    def insert_shape(self):
        tab = self.current_tab()
        if tab is None:
            return
        shape, ok = QInputDialog.getItem(
            self, "Vložit tvar", "Vyberte tvar:",
            ["Obdélník", "Elipsa", "Čára", "Šipka", "Bublina"], 0, False
        )
        if ok:
            tab.add_shape(shape)

    def insert_table(self):
        editor = self._editor()
        if editor is None:
            return
        rows, ok = QInputDialog.getInt(self, "Vložit tabulku", "Počet řádků:", 3, 1, 30)
        if not ok:
            return
        cols, ok = QInputDialog.getInt(self, "Vložit tabulku", "Počet sloupců:", 3, 1, 20)
        if not ok:
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(Theme.DOCUMENT_SIZE))
        editor.textCursor().insertTable(rows, cols)
        editor.setFocus()

    # --- Edit / print -------------------------------------------------------
    def _undo(self):
        if self._editor(): self._editor().undo()

    def _redo(self):
        if self._editor(): self._editor().redo()

    def _cut(self):
        if self._editor(): self._editor().cut()

    def _copy(self):
        if self._editor(): self._editor().copy()

    def _paste(self):
        if self._editor(): self._editor().paste()

    def _select_all(self):
        if self._editor(): self._editor().selectAll()

    def print_document(self):
        editor = self._editor()
        if editor is None:
            return
        printer = QPrinter(QPrinter.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec_() == QDialog.Accepted:
            editor.print_(printer)

    def export_pdf(self):
        editor = self._editor()
        if editor is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exportovat do PDF", "Dokument.pdf", "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        try:
            editor.print_(printer)
            self.status_label.setText(f"PDF vytvořeno: {Path(path).name}")
        except Exception as exc:
            QMessageBox.warning(self, "Export PDF se nezdařil", str(exc))

    def about(self):
        QMessageBox.about(
            self, "O aplikaci Písař",
            f"<h2>{APP_NAME}</h2><p>{APP_DESCRIPTION}</p>"
            f"<p>Verze {APP_VERSION}</p><p>Součást balíčku VLČR Kancelář.</p>"
            "<p>Dokumenty se ukládají do formátu .vlcr.</p>"
        )

    # --- Window lifecycle / drag and drop ---------------------------------
    def _restore_window(self):
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def closeEvent(self, event):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, DocumentTab) and not self._ask_save(tab):
                event.ignore()
                return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()
        event.accept()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() and url.toLocalFile().lower().endswith(".vlcr")
                                               for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".vlcr"):
                self.open_document(url.toLocalFile())
        event.acceptProposedAction()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(APP_VERSION)
        return 0

    initial_path = None
    for arg in argv:
        if not arg.startswith("-"):
            initial_path = arg
            break

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("VLCR")
    app.setOrganizationDomain("vlcr.local")
    app.setFont(QFont(Theme.UI_FONT, Theme.UI_SIZE))
    window = PisarWindow(initial_path)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
