import sys
import os
import subprocess
import ctypes

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QHBoxLayout,
    QVBoxLayout, QLabel, QFrame, QPushButton, QStackedWidget,
    QButtonGroup, QMessageBox
)

from PyQt5.QtCore import (
    Qt, pyqtSignal, pyqtProperty, QPropertyAnimation,
    QEasingCurve, QPointF, QTimer
)

from PyQt5.QtGui import (
    QFont, QPainter, QColor, QPixmap, QPen,
    QRadialGradient, QCursor, QIcon
)


# ============================================================
# CESTY K SOUBORŮM
# ============================================================

def resource_path(relative_path):
    """
    Najde prostředek při běžném spuštění i po zabalení PyInstallerem.
    Používá se například pro ikony a obrázky.
    """

    if getattr(sys, "frozen", False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)


def application_dir():
    """
    Vrátí adresář hlavního programu.

    Při vývoji:
        adresář main.py

    Po zabalení:
        adresář hlavního executable
    """

    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.path.dirname(os.path.abspath(__file__))


# ============================================================
# IDENTITA APLIKACE
# ============================================================

APP_NAME = "VLČR Kancelář"
APP_VERSION = "0.1.1"

GITHUB_REPO = "chznhs-spec/kancelar"
UPDATES_ENABLED = True

WINDOW_OPACITY = 1.0

BG_COLOR = "#EBEBEB"
SIDEBAR_BG = "#343434"
FOOTER_BG = "#2D2D2D"
OPEN_COLOR = "#949494"

FOOTER_HEIGHT = 28
COPYRIGHT_TEXT = "© 2026, Vzdělaný lid České republiky. Všechna práva vyhrazena."

BASE_DIR = application_dir()

# Ikona zabalená v PyInstalleru
APP_ICON_PATH = resource_path("loga/cervena.png")


# ============================================================
# SPOUŠTĚNÍ PROGRAMŮ SADY
# ============================================================

def find_program(key, dev_script):
    """
    Vrátí příkaz ke spuštění programu sady.

    Nainstalovaná (zabalená) verze:
        spustitelný soubor <key>(.exe) vedle hlavního programu
        (pro jistotu i ve složce apps/)

    Vývojová verze:
        python <dev_script> ze složky s main.py
    """

    base = application_dir()

    if getattr(sys, "frozen", False):
        executable = key + (".exe" if os.name == "nt" else "")

        for folder in (base, os.path.join(base, "apps")):
            candidate = os.path.join(folder, executable)

            if os.path.isfile(candidate):
                return [candidate]

        raise FileNotFoundError(os.path.join(base, executable))

    script_path = os.path.join(base, dev_script)

    if not os.path.isfile(script_path):
        raise FileNotFoundError(script_path)

    return [sys.executable, script_path]


def start_program(key, dev_script):
    """Spustí program sady jako samostatný proces."""

    command = find_program(key, dev_script)
    frozen = getattr(sys, "frozen", False)

    environment = os.environ.copy()

    if frozen:
        # Bez tohoto by se nově spuštěný zabalený program (PyInstaller >= 6.9)
        # choval jako potomek rozcestníku a otevřel by místo sebe znovu rozcestník.
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

    options = {
        "cwd": os.path.dirname(command[0]) if frozen else application_dir(),
        "env": environment,
    }

    if os.name != "nt":
        # program přežije zavření rozcestníku
        options["start_new_session"] = True

    subprocess.Popen(command, **options)


# ============================================================
# IDENTIFIKÁTOR APLIKACE PRO WINDOWS
# ============================================================

if os.name == "nt":
    try:
        myappid = "vlcr.kancelar.dashboard.010"

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            myappid
        )

    except Exception:
        pass


# ============================================================
# AKTUALIZACE (celá sada najednou, viz updater.py)
# ============================================================

try:
    from updater import UpdateManager
except ImportError:
    UpdateManager = None


# ============================================================
# SEZNAM APLIKACÍ
# ============================================================

APPS = [
    ("pisar", "Písař", "pisar.png", "#00A300", "pisar.py"),
    ("prezentar", "Prezentář", "prezenter.png", "#2D89EF", "prezentar.py"),
    ("pocitar", "Počítář", "pocitar.png", "#DA532C", "pocitar.py"),
    ("poscilogramar", "Poscilogramář", "poscilogramar.png", "#00A85A", "poscilogramar.py"),
]


# ============================================================
# DLAŽDICE
# ============================================================

class MetroTile(QFrame):

    clicked = pyqtSignal()

    HOVER_MS = 180
    GLOW_RADIUS = 120
    GLOW_ALPHA = 85
    FOLLOW_SPEED = 0.28
    HOVER_BORDER_ALPHA = 170

    def __init__(
        self,
        title,
        icon_path=None,
        color="#0078D7",
        parent=None
    ):
        super().__init__(parent)

        self.base_color = QColor(color)
        self.is_pressed = False

        self._hover = 0.0
        self._pos = QPointF(75, 75)
        self._target = QPointF(75, 75)

        self._anim = QPropertyAnimation(self, b"hover", self)
        self._anim.setDuration(self.HOVER_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

        self.setMouseTracking(True)
        self.setFixedSize(150, 150)
        self.setCursor(Qt.ArrowCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.lbl_icon = QLabel()
        self.lbl_icon.setStyleSheet(
            "background: transparent; color: white;"
        )

        if icon_path and os.path.exists(icon_path):
            pm = QPixmap(icon_path).scaled(
                44,
                44,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            self.lbl_icon.setPixmap(pm)
        else:
            self.lbl_icon.setText("")

        self.lbl_icon.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.lbl_title = QLabel(title)
        self.lbl_title.setFont(QFont("Segoe UI", 10))
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.lbl_title.setStyleSheet(
            "color: white; background: transparent;"
        )

        for lbl in (self.lbl_icon, self.lbl_title):
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(self.lbl_icon)
        layout.addStretch()
        layout.addWidget(self.lbl_title)

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

    def enterEvent(self, event):
        p = QPointF(self.mapFromGlobal(QCursor.pos()))

        self._pos = QPointF(p)
        self._target = QPointF(p)

        self._animate_to(1.0)

        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_pressed = False
        self._animate_to(0.0)

        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self._target = QPointF(event.pos())

        if not self._timer.isActive():
            self._timer.start()

        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.is_pressed = True
            self.update()

        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and self.is_pressed
            and self.rect().contains(event.pos())
        ):
            self.clicked.emit()

        self.is_pressed = False
        self.update()

        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()

        painter.fillRect(rect, self.base_color)

        if self.is_pressed:
            painter.fillRect(rect, QColor(0, 0, 0, 45))

        elif self._hover > 0:
            gradient = QRadialGradient(
                self._pos,
                self.GLOW_RADIUS
            )

            gradient.setColorAt(
                0.0,
                QColor(
                    255,
                    255,
                    255,
                    int(self.GLOW_ALPHA * self._hover)
                )
            )

            gradient.setColorAt(
                1.0,
                QColor(255, 255, 255, 0)
            )

            painter.fillRect(rect, gradient)

        if self._hover > 0:
            painter.setPen(
                QPen(
                    QColor(
                        255,
                        255,
                        255,
                        int(self.HOVER_BORDER_ALPHA * self._hover)
                    ),
                    2
                )
            )

            painter.drawRect(
                1,
                1,
                self.width() - 2,
                self.height() - 2
            )


# ============================================================
# NAVIGAČNÍ TLAČÍTKO
# ============================================================

class NavButton(QPushButton):

    def __init__(self, text, accent="#FFFFFF", parent=None):
        super().__init__(text, parent)

        self.setCheckable(True)
        self.setCursor(Qt.ArrowCursor)
        self.setFixedHeight(46)
        self.setFont(QFont("Segoe UI", 10))

        self.setStyleSheet(f"""
            QPushButton {{
                color: #D0D0D0;
                background: transparent;
                border: none;
                border-left: 4px solid transparent;
                text-align: left;
                padding-left: 18px;
            }}

            QPushButton:hover {{
                background: #2E2E2E;
                color: white;
            }}

            QPushButton:checked {{
                background: #383838;
                color: white;
                border-left: 4px solid {accent};
            }}
        """)


# ============================================================
# HLAVNÍ OKNO
# ============================================================

class VlcrDashboard(QMainWindow):

    def __init__(self):
        super().__init__()

        self.base_dir = BASE_DIR

        self.setWindowTitle(APP_NAME)
        self.setFixedSize(940, 580)

        self.setStyleSheet(
            f"QMainWindow {{ background-color: {BG_COLOR}; }}"
        )

        self.setWindowOpacity(WINDOW_OPACITY)

        if os.path.isfile(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        root = QWidget()
        self.setCentralWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self.stack = QStackedWidget()

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        self.stack.setStyleSheet("background: transparent;")

        body_layout.addWidget(self._build_sidebar())
        body_layout.addWidget(self.stack, 1)

        root_layout.addLayout(body_layout, 1)
        root_layout.addWidget(self._build_footer())

        self.stack.addWidget(self._build_home_page())

        for key, title, icon, color, script in APPS:
            self.stack.addWidget(
                self._build_app_page(key, title, color)
            )

        self.go_to(0)

        # hlídání aktualizací (jen v nainstalované verzi; ve vývoji pomocí VLCR_CHECK_UPDATES=1)
        self.updater = None

        if UpdateManager is not None:
            self.updater = UpdateManager(
                self, APP_VERSION, GITHUB_REPO, application_dir()
            )

            if UPDATES_ENABLED and (
                getattr(sys, "frozen", False)
                or os.environ.get("VLCR_CHECK_UPDATES")
            ):
                QTimer.singleShot(4000, self.updater.check_silently)

    # --------------------------------------------------------
    # AKTUALIZACE
    # --------------------------------------------------------

    def check_updates_clicked(self):
        if self.updater is None:
            QMessageBox.information(
                self,
                "Aktualizace",
                "Modul aktualizací (updater.py) není k dispozici."
            )
            return

        self.updater.check_manually()

    # --------------------------------------------------------
    # CESTY K IKONÁM
    # --------------------------------------------------------

    def icon(self, filename):
        return resource_path(
            os.path.join("loga", filename)
        )

    # --------------------------------------------------------
    # SPOUŠTĚNÍ APLIKACÍ
    # --------------------------------------------------------

    def launch_app(self, key):
        """Spustí příslušnou aplikaci VLČR (Písař, Prezentář, Počítář)."""

        app_info = next(
            (app for app in APPS if app[0] == key),
            None
        )

        if app_info is None:
            QMessageBox.warning(
                self,
                "Neznámá aplikace",
                "Aplikace není v seznamu."
            )
            return

        try:
            start_program(key, app_info[4])

        except Exception as error:
            QMessageBox.critical(
                self,
                "Chyba při spouštění",
                f"Aplikaci {app_info[1]} se nepodařilo spustit:\n{error}"
            )

    def open_explorer(self):
        """Spustí Průzkumník souborů VLČR."""

        try:
            start_program("pruzkumnik", "pruzkumnik.py")

        except Exception as error:
            QMessageBox.critical(
                self,
                "Chyba při spouštění",
                f"Průzkumník souborů se nepodařilo spustit:\n{error}"
            )

    # --------------------------------------------------------
    # POSTRANNÍ PANEL
    # --------------------------------------------------------

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setFixedWidth(220)

        sidebar.setStyleSheet(
            f"QFrame {{ background-color: {SIDEBAR_BG}; }}"
        )

        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        logo_label = QLabel()

        logo_label.setStyleSheet(
            "background: transparent; padding: 22px 18px 18px 22px;"
        )

        logo_path = self.icon("kancelar_2008.png")

        if os.path.exists(logo_path):
            logo_label.setPixmap(
                QPixmap(logo_path).scaledToWidth(
                    170,
                    Qt.SmoothTransformation
                )
            )
        else:
            logo_label.setText(APP_NAME)
            logo_label.setFont(
                QFont("Segoe UI", 14, QFont.Light)
            )
            logo_label.setStyleSheet(
                "color: white; background: transparent; "
                "padding: 26px 18px 22px 22px;"
            )

        lay.addWidget(logo_label)

        self.nav_buttons = []

        entries = (
            [("Domů", "#FFFFFF")]
            + [(a[1], a[3]) for a in APPS]
        )

        for i, (text, accent) in enumerate(entries):
            btn = NavButton(text, accent)

            self.nav_group.addButton(btn, i)

            btn.clicked.connect(
                lambda checked=False, idx=i: self.go_to(idx)
            )

            lay.addWidget(btn)
            self.nav_buttons.append(btn)

        lay.addStretch()

        self.btn_update = QPushButton("Zkontrolovat aktualizace")
        self.btn_update.setCursor(Qt.PointingHandCursor)
        self.btn_update.setFont(QFont("Segoe UI", 8))
        self.btn_update.setStyleSheet(
            "QPushButton { color: #8FB8E8; background: transparent; border: none;"
            " text-align: left; padding: 6px 22px; }"
            "QPushButton:hover { color: white; }"
        )
        self.btn_update.clicked.connect(self.check_updates_clicked)

        lay.addWidget(self.btn_update)

        self.lbl_version = QLabel(
            f"Verze {APP_VERSION}"
        )

        self.lbl_version.setFont(
            QFont("Segoe UI", 8)
        )

        self.lbl_version.setStyleSheet(
            "color: #808080; background: transparent; padding: 14px 22px;"
        )

        lay.addWidget(self.lbl_version)

        return sidebar

    # --------------------------------------------------------
    # SPODNÍ PANEL
    # --------------------------------------------------------

    def _build_footer(self):
        footer = QFrame()

        footer.setFixedHeight(FOOTER_HEIGHT)

        footer.setStyleSheet(
            f"QFrame {{ background-color: {FOOTER_BG}; }}"
        )

        lay = QHBoxLayout(footer)
        lay.setContentsMargins(16, 0, 16, 0)

        lay.addStretch()

        lbl = QLabel(COPYRIGHT_TEXT)
        lbl.setFont(QFont("Segoe UI", 8))
        lbl.setStyleSheet(
            "color: #8A92B8; background: transparent;"
        )

        lay.addWidget(lbl)

        return footer

    # --------------------------------------------------------
    # NAVIGACE
    # --------------------------------------------------------

    def go_to(self, index):
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)

    def _page_index(self, key):
        return 1 + next(
            i for i, app in enumerate(APPS)
            if app[0] == key
        )

    # --------------------------------------------------------
    # NADPIS STRÁNKY
    # --------------------------------------------------------

    def _page_header(self, text, color="#161616"):
        lbl = QLabel(text)

        lbl.setFont(
            QFont("Segoe UI", 22, QFont.Light)
        )

        lbl.setStyleSheet(
            f"color: {color}; background: transparent;"
        )

        return lbl

    # --------------------------------------------------------
    # DOMOVSKÁ STRÁNKA
    # --------------------------------------------------------

    def _build_home_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        lay = QVBoxLayout(page)
        lay.setContentsMargins(36, 30, 30, 30)
        lay.setSpacing(22)

        lay.addWidget(
            self._page_header("Domů")
        )

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        # Dlaždice Průzkumníka souborů

        tile_open = MetroTile(
            "Otevřít soubor",
            self.icon("otevrit_soubor.png"),
            OPEN_COLOR
        )

        tile_open.clicked.connect(
            self.open_explorer
        )

        grid.addWidget(tile_open, 0, 0)

        # Dlaždice ostatních aplikací

        col_count = 4

        for i, (key, title, icon, color, script) in enumerate(
            APPS,
            start=1
        ):
            tile = MetroTile(
                title,
                self.icon(icon),
                color
            )

            tile.clicked.connect(
                lambda app_key=key: self.go_to(
                    self._page_index(app_key)
                )
            )

            grid.addWidget(
                tile,
                i // col_count,
                i % col_count
            )

        lay.addLayout(grid)
        lay.addStretch()

        return page

    # --------------------------------------------------------
    # STRÁNKY APLIKACÍ
    # --------------------------------------------------------

    def _build_app_page(self, key, title, color):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        strip = QFrame()
        strip.setFixedHeight(6)
        strip.setStyleSheet(
            f"background-color: {color};"
        )

        lay.addWidget(strip)

        body = QVBoxLayout()
        body.setContentsMargins(36, 30, 30, 30)
        body.setSpacing(18)

        body.addWidget(
            self._page_header(title, color)
        )

        hint = QLabel(
            "Aplikace je součástí prostředí VLČR Kancelář. "
            "Spusť ji tlačítkem níže."
        )

        hint.setWordWrap(True)
        hint.setFont(QFont("Segoe UI", 10))
        hint.setStyleSheet(
            "color: #777777; background: transparent;"
        )

        body.addWidget(hint)

        open_button = QPushButton("Otevřít aplikaci")

        open_button.setCursor(Qt.PointingHandCursor)
        open_button.setFixedSize(190, 44)
        open_button.setFont(
            QFont("Segoe UI", 10, QFont.DemiBold)
        )

        open_button.setStyleSheet(f"""
            QPushButton {{
                color: white;
                background-color: {color};
                border: none;
                padding: 8px 18px;
            }}

            QPushButton:hover {{
                border: 2px solid rgba(255, 255, 255, 170);
            }}

            QPushButton:pressed {{
                background-color: #555555;
            }}
        """)

        open_button.clicked.connect(
            lambda checked=False, app_key=key:
                self.launch_app(app_key)
        )

        body.addWidget(open_button)
        body.addStretch()

        lay.addLayout(body)

        return page


# ============================================================
# SPUŠTĚNÍ APLIKACE
# ============================================================

if __name__ == "__main__":

    app = QApplication(sys.argv)

    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("vlcr-kancelar")         # Linux: shoda s vlcr-kancelar.desktop (ikona na panelu)
    app.setFont(QFont("Segoe UI", 9))

    if os.path.isfile(APP_ICON_PATH):
        app_icon = QIcon(APP_ICON_PATH)
        app.setWindowIcon(app_icon)

    else:
        print(
            f"VAROVÁNÍ: Ikona nebyla nalezena: {APP_ICON_PATH}"
        )

    window = VlcrDashboard()

    if os.path.isfile(APP_ICON_PATH):
        window.setWindowIcon(QIcon(APP_ICON_PATH))

    window.show()

    sys.exit(app.exec_())