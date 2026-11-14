"""
VLČR Průzkumník souborů
-----------------------
Zobrazuje jen složky a soubory .vlcr. Po otevření souboru přečte jeho metadata
a spustí příslušnou aplikaci (Písař, Prezentář, Počítář...).

Lze ho spustit samostatně (python pruzkumnik.py) nebo vložit do dashboardu
jako widget (FileExplorerWidget).
"""
import sys
import os
import json
import zipfile
import unicodedata
import subprocess
import struct

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QPushButton, QLineEdit, QListWidget, QListWidgetItem, QTreeView,
    QFileSystemModel, QFileIconProvider, QMessageBox, QShortcut, QHeaderView,
    QAbstractItemView, QScrollArea, QGridLayout, QStackedWidget
)
from PyQt5.QtCore import (
    Qt, QDir, QFileInfo, QSize, QStandardPaths, QSettings, QPointF, QTimer,
    QPropertyAnimation, QEasingCurve, pyqtSignal, pyqtProperty
)
from PyQt5.QtGui import (
    QFont, QIcon, QKeySequence, QPainter, QColor, QPen, QRadialGradient,
    QCursor, QPixmap
)

# ---------------------------------------------------------------------------
# Informace o aplikaci a příprava na GitHub releases
# ---------------------------------------------------------------------------
APP_NAME = "VLČR Průzkumník souborů"
APP_VERSION = "0.1.0"                    # při každém releasu zvyšuj (shodně s tagem, např. v0.1.0)
GITHUB_REPO = "uzivatel/vlcr-kancelar"   # TODO: "vlastnik/repozitar" na GitHubu
UPDATES_ENABLED = False                  # zapneš, až budou releasy existovat

def _application_directory():
    """Adresář, ve kterém leží spuštěný Průzkumník / hlavní aplikace."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _resource_directory():
    """Adresář zdrojů; u PyInstaller onefile jde o dočasný _MEIPASS."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _application_directory()
RESOURCE_DIR = _resource_directory()


def _parse_version(text):
    """'v1.2.3' -> (1, 2, 3) pro porovnávání verzí."""
    text = text.strip().lstrip("vV")
    parts = []
    for p in text.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_updates():
    """
    Zatím jen příprava - nikde se nevolá, dokud je UPDATES_ENABLED False.
    Vrací dict {"version", "url", "notes"}, pokud existuje novější release, jinak None.
    """
    if not UPDATES_ENABLED:
        return None
    import urllib.request
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"Kontrola aktualizací selhala: {e}")
        return None
    latest = data.get("tag_name", "")
    if _parse_version(latest) > _parse_version(APP_VERSION):
        return {
            "version": latest,
            "url": data.get("html_url", ""),
            "notes": data.get("body", ""),
        }
    return None


# ---------------------------------------------------------------------------
# Vzhled (shodný s dashboardem)
# ---------------------------------------------------------------------------
BG_COLOR = "#FEFEFE"
SIDEBAR_BG = "#6E6E6E"
FOOTER_BG = "#767676"
ACCENT = "#4359FF"
TEXT = "#000000"
TEXT_DIM = "#010101"
LINE = "#000000"
FOOTER_HEIGHT = 28
COPYRIGHT_TEXT = "© 2026, Vzdělaný lid České republiky"
VLCR_LOGO = "vlcr.png"                   # loga/vlcr.png - ikona souborů .vlcr

# Zobrazení: dlaždice (výchozí) nebo seznam. Volba se ukládá (QSettings)
# a po dalším spuštění se obnoví.
SETTINGS_ORG = "VLCR"
SETTINGS_APP = "Kancelar"
VIEW_TILES, VIEW_LIST = "tiles", "list"
DEFAULT_VIEW = VIEW_TILES

# barvy dlaždic
FOLDER_COLOR = "#4A412A"
INVALID_COLOR = "#949494"
APP_COLORS = {
    "pisar": "#183A09",
    "prezentar": "#2600A5",
    "pocitar": "#DA532C",
}


# ---------------------------------------------------------------------------
# Formát .vlcr a čtení metadat
#
# POZOR: předpokládaný formát - upravte podle skutečné struktury souboru .vlcr:
#   a) ZIP archiv obsahující metadata.json (nebo manifest.json / meta.json), nebo
#   b) samotný JSON soubor.
# V metadatech se hledá klíč "app" (nebo application / aplikace / typ / type ...),
# jehož hodnota určuje aplikaci. Název dokumentu z "title" / "nazev" / "name".
# ---------------------------------------------------------------------------
APP_TITLES = {
    "pisar": "Písař",
    "prezentar": "Prezentář",
    "pocitar": "Počítář",
}

# rozpoznané hodnoty v metadatech (bez diakritiky, malými písmeny)
APP_ALIASES = {
    "pisar": {"pisar", "writer", "document", "dokument", "text", "doc", "docx"},
    "prezentar": {"prezentar", "presentation", "prezentace", "slides", "pptx"},
    "pocitar": {"pocitar", "spreadsheet", "sheet", "tabulka", "xlsx"},
}

META_FILE_NAMES = ("metadata.json", "manifest.json", "meta.json")
APP_KEYS = ("app", "application", "aplikace", "editor", "typ", "type", "format", "kind")
TITLE_KEYS = ("title", "nazev", "název", "name")

# Spustitelné aplikace leží přímo vedle Průzkumníka.
# Na Linuxu jsou bez přípony, na Windows se automaticky doplní .exe.
APP_EXECUTABLES = {
    "pisar": "pisar",
    "prezentar": "prezentar",
    "pocitar": "pocitar",
}

# Záložní režim pro vývoj: pokud Průzkumník spouštíš jako .py a executable
# ještě neexistuje, může použít odpovídající Python skript.
APP_SCRIPTS = {
    "pisar": "pisar.py",
    "prezentar": "prezentar.py",
    "pocitar": "pocitar.py",
}
APP_LAUNCHERS = {}


class VlcrFormatError(Exception):
    """Soubor .vlcr nelze přečíst nebo nemá platná metadata."""


def _normalize(text):
    text = unicodedata.normalize("NFKD", str(text).strip().lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _resolve_app(meta):
    for key in APP_KEYS:
        value = meta.get(key)
        if isinstance(value, str):
            norm = _normalize(value)
            for app_key, aliases in APP_ALIASES.items():
                if norm == app_key or norm in aliases:
                    return app_key
    return None


def _as_meta(data):
    if not isinstance(data, dict):
        raise VlcrFormatError("Metadata mají neočekávanou strukturu.")
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else data
    title = next((meta[k] for k in TITLE_KEYS if isinstance(meta.get(k), str)), None)
    return {"app": _resolve_app(meta), "title": title, "raw": meta}


def read_vlcr_metadata(path):
    """Přečte metadata běžných VLČR souborů a nativních binárních kontejnerů.

    Prezentář používá magic b"VLCRPREZ", Písař b"VLCRPISR". Oba mají
    společnou hlavičku: magic (8 B) + verze (uint16) + délka metadat (uint32)
    + UTF-8 metadata JSON + komprimovaný aplikační obsah. Průzkumník čte jen
    metadata; obsah rozbalí až příslušná aplikace.
    """
    try:
        with open(path, "rb") as f:
            prefix = f.read(8)
            if prefix in (b"VLCRPREZ", b"VLCRPISR"):
                rest = f.read(6)
                if len(rest) != 6:
                    raise VlcrFormatError("Neúplná hlavička binárního souboru .vlcr.")
                version, meta_len = struct.unpack(">HI", rest)
                if version != 1:
                    raise VlcrFormatError(f"Nepodporovaná verze binárního .vlcr: {version}.")
                if meta_len <= 0 or meta_len > 1024 * 1024:
                    raise VlcrFormatError("Neplatná délka metadat binárního .vlcr.")
                meta_raw = f.read(meta_len)
                if len(meta_raw) != meta_len:
                    raise VlcrFormatError("Neúplná metadata binárního souboru .vlcr.")
                return _as_meta(json.loads(meta_raw.decode("utf-8")))

        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                names = {n.lower(): n for n in zf.namelist()}
                for cand in META_FILE_NAMES:
                    if cand in names:
                        return _as_meta(json.loads(zf.read(names[cand]).decode("utf-8-sig")))
            raise VlcrFormatError("V souboru nebyla nalezena metadata.")

        with open(path, "rb") as f:
            raw = f.read(1024 * 1024)
        return _as_meta(json.loads(raw.decode("utf-8-sig")))
    except VlcrFormatError:
        raise
    except Exception as e:
        raise VlcrFormatError(f"Soubor se nepodařilo přečíst ({e}).")


_META_CACHE = {}


def get_metadata_cached(path):
    """Metadata s cache podle času změny (kvůli vykreslování seznamu). Při chybě None."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _META_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        meta = read_vlcr_metadata(path)
    except VlcrFormatError:
        meta = None
    _META_CACHE[path] = (mtime, meta)
    return meta


def _launch_command(app_key, path, base_dir):
    """Vrátí příkaz pro spuštění aplikace ze stejné složky jako Průzkumník."""
    launcher = APP_LAUNCHERS.get(app_key)
    if launcher:
        cmd = list(launcher) if isinstance(launcher, (list, tuple)) else [launcher]
        return cmd + [os.path.abspath(path)]

    executable_name = APP_EXECUTABLES.get(app_key)
    if executable_name:
        if os.name == "nt" and not executable_name.lower().endswith(".exe"):
            executable_name += ".exe"
        executable = os.path.join(base_dir, executable_name)
        if os.path.isfile(executable):
            return [executable, os.path.abspath(path)]

    # Python skripty jsou pouze vývojová záloha. Zkompilovaný Průzkumník
    # vždy vyžaduje přítomnost odpovídajícího executable souboru.
    if not getattr(sys, "frozen", False):
        script_name = APP_SCRIPTS.get(app_key)
        if script_name:
            script = os.path.join(base_dir, script_name)
            if os.path.isfile(script):
                return [sys.executable, script, os.path.abspath(path)]
    return None


def launch_vlcr(path, parent=None, base_dir=None):
    """Přečte metadata a otevře soubor v příslušné aplikaci. Vrací True při úspěchu."""
    base_dir = base_dir or BASE_DIR
    try:
        meta = read_vlcr_metadata(path)
    except VlcrFormatError as e:
        QMessageBox.warning(parent, "Soubor nelze otevřít", str(e))
        return False

    app_key = meta["app"]
    if app_key is None:
        QMessageBox.warning(parent, "Soubor nelze otevřít",
                            "V metadatech chybí nebo není rozpoznána aplikace.")
        return False

    title = APP_TITLES.get(app_key, app_key)
    cmd = _launch_command(app_key, path, base_dir)
    if cmd is None:
        QMessageBox.information(parent, "Aplikace není dostupná",
                                f"Aplikace {title} zatím není nainstalována nebo nastavena.")
        return False
    try:
        subprocess.Popen(cmd, cwd=base_dir)
        return True
    except Exception as e:
        QMessageBox.warning(parent, "Aplikaci se nepodařilo spustit", f"{title}: {e}")
        return False


# ---------------------------------------------------------------------------
# Model souborů: jen složky + .vlcr, ikona VLČR, sloupec "Aplikace"
# ---------------------------------------------------------------------------
class VlcrIconProvider(QFileIconProvider):
    def __init__(self, logo_path):
        super().__init__()
        self._vlcr = QIcon(logo_path) if os.path.exists(logo_path) else QIcon()

    def icon(self, arg):
        if isinstance(arg, QFileInfo) and arg.suffix().lower() == "vlcr" and not self._vlcr.isNull():
            return self._vlcr
        return super().icon(arg)


class VlcrFileModel(QFileSystemModel):
    HEADERS = {0: "Název", 1: "Velikost", 2: "Aplikace", 3: "Datum změny"}

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and section in self.HEADERS:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and index.column() == 2:
            if self.isDir(index):
                return "Složka"
            meta = get_metadata_cached(self.filePath(index))
            if meta is None:
                return "Neplatný soubor"
            return APP_TITLES.get(meta["app"], "VLČR soubor")
        return super().data(index, role)


def _plural(n):
    if n == 1:
        return "1 položka"
    if 2 <= n <= 4:
        return f"{n} položky"
    return f"{n} položek"


# ---------------------------------------------------------------------------
# Zobrazení dlaždic
# ---------------------------------------------------------------------------
def _is_hidden(entry):
    if entry.name.startswith("."):
        return True
    try:
        return bool(getattr(entry.stat(), "st_file_attributes", 0) & 0x2)  # Windows: skrytý
    except OSError:
        return False


def list_entries(path):
    """Složky a soubory .vlcr v adresáři (složky první, abecedně)."""
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if _is_hidden(entry):
                        continue
                    if entry.is_dir():
                        dirs.append(entry)
                    elif entry.is_file() and entry.name.lower().endswith(".vlcr"):
                        files.append(entry)
                except OSError:
                    continue
    except OSError:
        return []

    def key(e):
        return e.name.lower()
    return sorted(dirs, key=key) + sorted(files, key=key)


class ItemTile(QFrame):
    """Dlaždice složky / souboru .vlcr s plynulým gradientem sledujícím kurzor."""
    clicked = pyqtSignal(object)         # výběr (klik)
    activated = pyqtSignal(str, bool)    # otevření (dvojklik): cesta, je_složka

    W, H = 150, 120
    HOVER_MS = 180
    GLOW_RADIUS = 120
    GLOW_ALPHA = 85
    FOLLOW_SPEED = 0.28
    HOVER_BORDER_ALPHA = 170

    def __init__(self, path, is_dir, title, subtitle, color, pixmap=None, parent=None):
        super().__init__(parent)
        self.path = path
        self.is_dir = is_dir
        self.base_color = QColor(color)
        self.is_pressed = False
        self._selected = False
        self._hover = 0.0
        self._pos = QPointF(self.W / 2, self.H / 2)
        self._target = QPointF(self.W / 2, self.H / 2)

        self._anim = QPropertyAnimation(self, b"hover", self)
        self._anim.setDuration(self.HOVER_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

        self.setMouseTracking(True)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip(os.path.basename(path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(0)

        lbl_icon = QLabel()
        lbl_icon.setStyleSheet("background: transparent; color: white;")
        if pixmap is not None and not pixmap.isNull():
            lbl_icon.setPixmap(pixmap)
        else:
            lbl_icon.setText("📁" if is_dir else "📄")
            lbl_icon.setFont(QFont("Segoe UI Emoji", 22))
        lbl_icon.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        lbl_title = QLabel(title)
        lbl_title.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        lbl_title.setWordWrap(True)
        lbl_title.setMaximumHeight(36)
        lbl_title.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        lbl_title.setStyleSheet("color: white; background: transparent;")

        lbl_sub = QLabel(subtitle)
        lbl_sub.setFont(QFont("Segoe UI", 8))
        lbl_sub.setStyleSheet("color: rgba(255,255,255,210); background: transparent;")

        for lbl in (lbl_icon, lbl_title, lbl_sub):
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(lbl_icon)
        layout.addStretch()
        layout.addWidget(lbl_title)
        layout.addWidget(lbl_sub)

    # --- animovaná vlastnost -------------------------------------------------
    def _get_hover(self):
        return self._hover

    def _set_hover(self, value):
        self._hover = value
        self.update()

    hover = pyqtProperty(float, _get_hover, _set_hover)

    def _animate_to(self, target):
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.start()

    def _tick(self):
        delta = self._target - self._pos
        if abs(delta.x()) + abs(delta.y()) < 0.5:
            self._pos = QPointF(self._target)
            self._timer.stop()
        else:
            self._pos += delta * self.FOLLOW_SPEED
        self.update()

    def set_selected(self, flag):
        self._selected = flag
        self.update()

    # --- události ------------------------------------------------------------
    def enterEvent(self, event):
        p = QPointF(self.mapFromGlobal(QCursor.pos()))
        self._pos = QPointF(p)
        self._target = QPointF(p)
        self._animate_to(1.0)

    def leaveEvent(self, event):
        self.is_pressed = False
        self._animate_to(0.0)

    def mouseMoveEvent(self, event):
        self._target = QPointF(event.pos())
        if not self._timer.isActive():
            self._timer.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_pressed = True
            self.clicked.emit(self)
            self.update()

    def mouseReleaseEvent(self, event):
        self.is_pressed = False
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.path, self.is_dir)

    def paintEvent(self, event):
        p = QPainter(self)
        rect = self.rect()
        p.fillRect(rect, self.base_color)

        if self.is_pressed:
            p.fillRect(rect, QColor(0, 0, 0, 45))
        elif self._hover > 0:
            g = QRadialGradient(self._pos, self.GLOW_RADIUS)
            g.setColorAt(0.0, QColor(255, 255, 255, int(self.GLOW_ALPHA * self._hover)))
            g.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(rect, g)

        if self._selected:
            p.setPen(QPen(QColor(TEXT), 3))
            p.drawRect(1, 1, self.width() - 3, self.height() - 3)
        elif self._hover > 0:
            p.setPen(QPen(QColor(255, 255, 255, int(self.HOVER_BORDER_ALPHA * self._hover)), 2))
            p.drawRect(1, 1, self.width() - 2, self.height() - 2)


class TileView(QScrollArea):
    """Posuvná plocha s dlaždicemi, které se podle šířky zalamují do řádků."""
    selection_changed = pyqtSignal(object)   # vybraná dlaždice nebo None
    item_activated = pyqtSignal(str, bool)   # cesta, je_složka

    SPACING = 10
    MARGIN = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles = []
        self._selected = None
        self._cols = 0

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
            QScrollBar::handle:vertical { background: rgba(0,0,0,0.25); min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: rgba(0,0,0,0.4); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        container = QWidget()
        outer = QVBoxLayout(container)
        m = self.MARGIN
        outer.setContentsMargins(m, m, m, m)
        outer.setSpacing(0)

        self._grid = QGridLayout()
        self._grid.setSpacing(self.SPACING)
        self._grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        outer.addLayout(self._grid)

        self._empty = QLabel("V této složce nejsou žádné složky ani soubory .vlcr.")
        self._empty.setFont(QFont("Segoe UI", 10))
        self._empty.setStyleSheet(f"color: {TEXT_DIM}; background: transparent; padding: 8px;")
        self._empty.hide()
        outer.addWidget(self._empty)
        outer.addStretch(1)
        self.setWidget(container)

    def count(self):
        return len(self._tiles)

    def populate(self, entries):
        """entries: seznam dictů path, is_dir, title, subtitle, color, pixmap."""
        for t in self._tiles:
            self._grid.removeWidget(t)
            t.setParent(None)
            t.deleteLater()
        self._tiles = []
        self._selected = None

        for d in entries:
            tile = ItemTile(d["path"], d["is_dir"], d["title"], d["subtitle"],
                            d["color"], d.get("pixmap"))
            tile.clicked.connect(self._select)
            tile.activated.connect(self.item_activated)
            self._tiles.append(tile)

        self._empty.setVisible(not entries)
        self._relayout(force=True)
        self.verticalScrollBar().setValue(0)
        self.selection_changed.emit(None)

    def _select(self, tile):
        if self._selected is not None and self._selected is not tile:
            self._selected.set_selected(False)
        self._selected = tile
        tile.set_selected(True)
        self.setFocus()
        self.selection_changed.emit(tile)

    def _columns(self):
        avail = self.viewport().width() - 2 * self.MARGIN
        return max(1, (avail + self.SPACING) // (ItemTile.W + self.SPACING))

    def _relayout(self, force=False):
        cols = self._columns()
        if not force and cols == self._cols:
            return
        self._cols = cols
        for t in self._tiles:
            self._grid.removeWidget(t)
        for i, t in enumerate(self._tiles):
            self._grid.addWidget(t, i // cols, i % cols)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter) and self._selected is not None:
            self.item_activated.emit(self._selected.path, self._selected.is_dir)
            return
        steps = {Qt.Key_Left: -1, Qt.Key_Right: 1,
                 Qt.Key_Up: -self._cols, Qt.Key_Down: self._cols}
        if self._tiles and key in steps:
            if self._selected in self._tiles:
                new = self._tiles.index(self._selected) + steps[key]
            else:
                new = 0
            if 0 <= new < len(self._tiles):
                self._select(self._tiles[new])
                self.ensureWidgetVisible(self._tiles[new])
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Widget průzkumníku
# ---------------------------------------------------------------------------
class FileExplorerWidget(QWidget):
    def __init__(self, base_dir=None, title="Průzkumník souborů", color=ACCENT,
                 start_path=None, parent=None):
        super().__init__(parent)
        self.base_dir = base_dir or BASE_DIR
        self.accent = color
        self._history = []
        self._hpos = -1
        self._current = ""

        # uložené nastavení zobrazení (dlaždice / seznam)
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        mode = self.settings.value("explorer/view_mode", DEFAULT_VIEW)
        self.view_mode = mode if mode in (VIEW_TILES, VIEW_LIST) else DEFAULT_VIEW

        # ikony pro dlaždice
        folder_icon = QFileIconProvider().icon(QFileIconProvider.Folder)
        self._folder_pm = folder_icon.pixmap(40, 40) if not folder_icon.isNull() else None
        logo = os.path.join(RESOURCE_DIR, "loga", VLCR_LOGO)
        self._vlcr_pm = (QPixmap(logo).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                         if os.path.exists(logo) else None)

        self.setStyleSheet("background: transparent;")
        self._build_ui(title)
        self._build_shortcuts()
        self.set_view_mode(self.view_mode, save=False)

        start = start_path or QStandardPaths.writableLocation(QStandardPaths.HomeLocation)
        self.navigate(start or QDir.homePath())

    # --- UI ------------------------------------------------------------------
    def _build_ui(self, title):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        strip = QFrame()
        strip.setFixedHeight(6)
        strip.setStyleSheet(f"background-color: {self.accent};")
        root.addWidget(strip)

        body = QVBoxLayout()
        body.setContentsMargins(30, 22, 24, 14)
        body.setSpacing(12)
        root.addLayout(body, 1)

        header = QLabel(title)
        header.setFont(QFont("Segoe UI", 22, QFont.Light))
        header.setStyleSheet(f"color: {self.accent}; background: transparent;")
        body.addWidget(header)

        # --- panel nástrojů
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.btn_back = self._nav_button("←", "Zpět (Alt+←)", self.go_back)
        self.btn_forward = self._nav_button("→", "Vpřed (Alt+→)", self.go_forward)
        self.btn_up = self._nav_button("↑", "Nadřazená složka (Alt+↑)", self.go_up)
        self.btn_refresh = self._nav_button("⟳", "Obnovit (F5)", self.refresh)
        self.btn_tiles = self._nav_button("▦", "Zobrazení dlaždic",
                                          lambda: self.set_view_mode(VIEW_TILES), checkable=True)
        self.btn_list = self._nav_button("☰", "Zobrazení seznamu",
                                         lambda: self.set_view_mode(VIEW_LIST), checkable=True)
        for b in (self.btn_back, self.btn_forward, self.btn_up):
            toolbar.addWidget(b)

        self.path_edit = QLineEdit()
        self.path_edit.setFixedHeight(32)
        self.path_edit.setFont(QFont("Segoe UI", 10))
        self.path_edit.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255,255,255,0.08); color: {TEXT};
                border: 1px solid {LINE}; padding: 0 10px;
                selection-background-color: {self.accent};
            }}
            QLineEdit:focus {{ border: 1px solid {self.accent}; }}
        """)
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text()))
        toolbar.addWidget(self.path_edit, 1)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.btn_tiles)
        toolbar.addWidget(self.btn_list)
        body.addLayout(toolbar)

        # --- obsah: místa + seznam souborů
        content = QHBoxLayout()
        content.setSpacing(14)
        body.addLayout(content, 1)

        self.places = QListWidget()
        self.places.setFixedWidth(170)
        self.places.setFocusPolicy(Qt.NoFocus)
        self.places.setFont(QFont("Segoe UI", 10))
        self.places.setStyleSheet(f"""
            QListWidget {{ background: rgba(0,0,0,0.20); border: none; color: {TEXT_DIM}; outline: none; }}
            QListWidget::item {{ height: 36px; padding-left: 12px; border-left: 4px solid transparent; }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.08); color: white; }}
            QListWidget::item:selected {{ background: rgba(255,255,255,0.12); color: white;
                                          border-left: 4px solid {self.accent}; }}
        """)
        self._fill_places()
        self.places.itemClicked.connect(lambda it: self.navigate(it.data(Qt.UserRole)))
        content.addWidget(self.places)

        self.model = VlcrFileModel(self)
        self.model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
        self.model.setNameFilters(["*.vlcr"])
        self.model.setNameFilterDisables(False)      # skryje ostatní soubory, složky zůstanou
        self.icon_provider = VlcrIconProvider(os.path.join(RESOURCE_DIR, "loga", VLCR_LOGO))
        self.model.setIconProvider(self.icon_provider)
        self.model.directoryLoaded.connect(self._on_loaded)

        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setRootIsDecorated(False)
        self.view.setItemsExpandable(False)
        self.view.setUniformRowHeights(True)
        self.view.setAllColumnsShowFocus(True)
        self.view.setIconSize(QSize(22, 22))
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setFont(QFont("Segoe UI", 10))
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        header_view = self.view.header()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 90), (2, 130), (3, 150)):
            header_view.setSectionResizeMode(col, QHeaderView.Fixed)
            self.view.setColumnWidth(col, width)
        self.view.setStyleSheet(f"""
            QTreeView {{ background: rgba(0,0,0,0.12); color: {TEXT}; border: none; outline: none; }}
            QTreeView::item {{ height: 30px; }}
            QTreeView::item:hover {{ background: rgba(255,255,255,0.08); }}
            QTreeView::item:selected {{ background: rgba(255,255,255,0.45); color: black; }}
            QHeaderView::section {{
                background: transparent; color: {TEXT_DIM}; border: none;
                border-bottom: 1px solid {LINE}; padding: 6px 8px;
            }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.25); min-height: 30px; }}
            QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.4); }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
            QScrollBar::handle:horizontal {{ background: rgba(255,255,255,0.25); min-width: 30px; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        """)
        self.view.activated.connect(self._on_activated)    # dvojklik i Enter
        self.view.selectionModel().selectionChanged.connect(self._on_selection)

        self.tile_view = TileView()
        self.tile_view.selection_changed.connect(self._on_tile_selected)
        self.tile_view.item_activated.connect(self._open_path)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tile_view)
        self.stack.addWidget(self.view)
        content.addWidget(self.stack, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setFont(QFont("Segoe UI", 9))
        self.lbl_status.setStyleSheet(f"color: {TEXT_DIM}; background: transparent;")
        body.addWidget(self.lbl_status)

    def _nav_button(self, text, tip, slot, checkable=False):
        btn = QPushButton(text)
        btn.setCheckable(checkable)
        btn.setToolTip(tip)
        btn.setFixedSize(36, 32)
        btn.setCursor(Qt.ArrowCursor)
        btn.setFont(QFont("Segoe UI Symbol", 11))
        btn.setStyleSheet(f"""
            QPushButton {{ background: rgba(255,255,255,0.08); color: {TEXT}; border: 1px solid {LINE}; }}
            QPushButton:hover {{ background: rgba(255,255,255,0.18); color: white; }}
            QPushButton:pressed {{ background: {self.accent}; color: white; }}
            QPushButton:checked {{ background: {self.accent}; color: white; border: 1px solid {self.accent}; }}
            QPushButton:disabled {{ color: rgba(255,255,255,0.25); border-color: rgba(255,255,255,0.08); }}
        """)
        btn.clicked.connect(slot)
        return btn

    def _build_shortcuts(self):
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.go_back)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self.go_forward)
        QShortcut(QKeySequence("Alt+Up"), self, activated=self.go_up)
        for w in (self.view, self.tile_view):
            sc = QShortcut(QKeySequence("Backspace"), w)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self.go_up)
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)

    def _fill_places(self):
        places = []
        for label, loc in (
            ("Domů", QStandardPaths.HomeLocation),
            ("Plocha", QStandardPaths.DesktopLocation),
            ("Dokumenty", QStandardPaths.DocumentsLocation),
            ("Stažené", QStandardPaths.DownloadLocation),
        ):
            path = QStandardPaths.writableLocation(loc)
            if path and os.path.isdir(path):
                places.append((label, path))
        for drive in QDir.drives():
            path = drive.absolutePath()
            places.append((f"Disk {path.rstrip('/' + chr(92)) or path}", path))
        for label, path in places:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, path)
            self.places.addItem(item)

    # --- navigace ------------------------------------------------------------
    def navigate(self, path, record=True):
        path = os.path.normpath(os.path.expanduser(path.strip())) if path else ""
        if not path or not os.path.isdir(path):
            self.path_edit.setText(self._current)
            self._set_status("Cesta neexistuje.")
            return
        if record and (not self._history or self._history[self._hpos] != path):
            del self._history[self._hpos + 1:]
            self._history.append(path)
            self._hpos = len(self._history) - 1
        self._current = path
        self.view.setRootIndex(self.model.setRootPath(path))
        self.path_edit.setText(path)
        self._update_nav_buttons()
        self._reload_tiles()
        self._refresh_count()

    def go_back(self):
        if self._hpos > 0:
            self._hpos -= 1
            self.navigate(self._history[self._hpos], record=False)

    def go_forward(self):
        if self._hpos < len(self._history) - 1:
            self._hpos += 1
            self.navigate(self._history[self._hpos], record=False)

    def go_up(self):
        parent = os.path.dirname(self._current)
        if parent and parent != self._current:
            self.navigate(parent)

    def refresh(self):
        _META_CACHE.clear()
        self.navigate(self._current, record=False)
        self.view.viewport().update()

    def _update_nav_buttons(self):
        self.btn_back.setEnabled(self._hpos > 0)
        self.btn_forward.setEnabled(self._hpos < len(self._history) - 1)
        parent = os.path.dirname(self._current)
        self.btn_up.setEnabled(bool(parent) and parent != self._current)

    # --- události ------------------------------------------------------------
    def _on_activated(self, index):
        self._open_path(self.model.filePath(index), self.model.isDir(index))

    def _open_path(self, path, is_dir):
        if is_dir:
            self.navigate(path)
        else:
            launch_vlcr(path, self.window(), self.base_dir)

    def _on_loaded(self, path):
        if os.path.normpath(path) == self._current:
            self._refresh_count()

    def _on_selection(self, *_):
        rows = self.view.selectionModel().selectedRows(0)
        if not rows:
            self._refresh_count()
            return
        index = rows[0]
        self._set_status(self._describe(self.model.filePath(index), self.model.isDir(index)))

    def _on_tile_selected(self, tile):
        if tile is None:
            self._refresh_count()
        else:
            self._set_status(self._describe(tile.path, tile.is_dir))

    def _describe(self, path, is_dir):
        name = os.path.basename(path)
        if is_dir:
            return f"Složka · {name}"
        meta = get_metadata_cached(path)
        if meta is None:
            return f"{name} · neplatný soubor .vlcr"
        app = APP_TITLES.get(meta["app"], "neznámá aplikace")
        return f"{app} · {meta['title'] or name}"

    # --- zobrazení dlaždice / seznam ---------------------------------------
    def set_view_mode(self, mode, save=True):
        """Přepne zobrazení a (volitelně) uloží volbu pro příští spuštění."""
        if mode not in (VIEW_TILES, VIEW_LIST):
            mode = DEFAULT_VIEW
        self.view_mode = mode
        self.btn_tiles.setChecked(mode == VIEW_TILES)
        self.btn_list.setChecked(mode == VIEW_LIST)
        self.stack.setCurrentWidget(self.tile_view if mode == VIEW_TILES else self.view)
        if mode == VIEW_TILES:
            self._reload_tiles()
        if save:
            self.settings.setValue("explorer/view_mode", mode)
            self.settings.sync()
        if self._current:
            self._refresh_count()

    def _reload_tiles(self):
        if self.view_mode != VIEW_TILES or not self._current:
            return
        entries = []
        for e in list_entries(self._current):
            if e.is_dir():
                entries.append({"path": e.path, "is_dir": True, "title": e.name,
                                "subtitle": "Složka", "color": FOLDER_COLOR,
                                "pixmap": self._folder_pm})
                continue
            meta = get_metadata_cached(e.path)
            if meta is None:
                subtitle, color = "Neplatný soubor", INVALID_COLOR
            else:
                subtitle = APP_TITLES.get(meta["app"], "VLČR soubor")
                color = APP_COLORS.get(meta["app"], INVALID_COLOR)
            entries.append({"path": e.path, "is_dir": False,
                            "title": os.path.splitext(e.name)[0],
                            "subtitle": subtitle, "color": color,
                            "pixmap": self._vlcr_pm})
        self.tile_view.populate(entries)

    def _refresh_count(self):
        if self.view_mode == VIEW_TILES:
            n = self.tile_view.count()
        else:
            n = self.model.rowCount(self.view.rootIndex())
        self._set_status(_plural(n))

    def _set_status(self, text):
        self.lbl_status.setText(text)


# ---------------------------------------------------------------------------
# Samostatné okno
# ---------------------------------------------------------------------------
class ExplorerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(940, 580)
        self.setMinimumSize(760, 480)
        self.setStyleSheet(f"QMainWindow {{ background-color: {BG_COLOR}; }}")

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(FileExplorerWidget(BASE_DIR), 1)

        footer = QFrame()
        footer.setFixedHeight(FOOTER_HEIGHT)
        footer.setStyleSheet(f"QFrame {{ background-color: {FOOTER_BG}; }}")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 0, 16, 0)
        for text, align_right in ((f"Verze {APP_VERSION}", False), (COPYRIGHT_TEXT, True)):
            if align_right:
                fl.addStretch()
            lbl = QLabel(text)
            lbl.setFont(QFont("Segoe UI", 8))
            lbl.setStyleSheet("color: white; background: transparent;")
            fl.addWidget(lbl)
        lay.addWidget(footer)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(APP_VERSION)
        sys.exit(0)
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 9))
    window = ExplorerWindow()
    window.show()
    sys.exit(app.exec_())