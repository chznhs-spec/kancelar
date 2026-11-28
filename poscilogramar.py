"""Poscilogramář – ruční editor průběhů digitálního logického analyzátoru.

Vyžaduje PyQt5. Pracovní soubory ukládá do kompatibilního kontejneru .vlcr.
"""
from __future__ import annotations

import os
import sys
import math
import importlib.util
import traceback
from bisect import bisect_left
from copy import deepcopy

from PyQt5.QtCore import Qt, QPoint, QPointF, QSize, QRectF
from PyQt5.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QImage,
    QTextDocument, QTextOption, QTextCursor, QTextCharFormat, QBrush,
)
from PyQt5.QtWidgets import (
    QApplication, QAbstractScrollArea, QAction, QButtonGroup, QColorDialog,
    QFrame, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QShortcut, QVBoxLayout, QWidget, QInputDialog, QMenu, QScrollArea,
    QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QTextEdit, QToolButton,
)
from PyQt5.QtPrintSupport import QPrinter
from PyQt5.QtSvg import QSvgGenerator

from vlcr_format import (
    DEFAULT_TIME_UNIT_INDEX, MAX_CHANNELS, MAX_TIMELINE_NS, MIN_TIMELINE_DIVISIONS,
    TIME_UNITS_NS, default_workspace, load_workspace, save_workspace,
)

APP_NAME = "Poscilogramář"
APP_VERSION = "1.2.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "loga", "poscilogramar.png")

# Jedna svislá dílka mřížky odpovídá právě zvolené časové jednotce.
TIME_UNITS = []
for exponent in range(-9, 0):
    for mantissa in (1, 2, 5):
        TIME_UNITS.append(mantissa * (10.0 ** exponent))
TIME_UNIT_LABELS = []
for value in TIME_UNITS:
    if value < 1e-6:
        TIME_UNIT_LABELS.append(f"{value * 1e9:g} ns")
    elif value < 1e-3:
        TIME_UNIT_LABELS.append(f"{value * 1e6:g} µs")
    elif value < 1:
        TIME_UNIT_LABELS.append(f"{value * 1e3:g} ms")
    else:
        TIME_UNIT_LABELS.append(f"{value:g} s")

WIN_BG = "#C0C0C0"
WIN_TEXT = "#111111"
WIN_DARK = "#404040"
WIN_SHADOW = "#808080"
WIN_LIGHT = "#FFFFFF"

_EXPLORER_DIALOG_CLASS = None


def _load_file_explorer_dialog_class():
    """Načte dialog VLČR Průzkumníku ze zdrojové i PyInstaller verze.

    PyInstaller 6 ukládá přibalené datové soubory do _internal. Soubor proto
    hledáme také přes sys._MEIPASS a vedle spustitelného souboru. Pokud soubor
    existuje, ale jeho import selže (např. kvůli chybějící závislosti), vrátíme
    skutečnou příčinu místo zavádějící zprávy, že soubor nebyl nalezen.
    """
    global _EXPLORER_DIALOG_CLASS
    if _EXPLORER_DIALOG_CLASS is not None:
        return _EXPLORER_DIALOG_CLASS

    module_name = "pruzkumnik"
    already_loaded = sys.modules.get(module_name)
    if already_loaded is not None:
        dialog_class = getattr(already_loaded, "FileExplorerDialog", None)
        if dialog_class is not None:
            _EXPLORER_DIALOG_CLASS = dialog_class
            return dialog_class

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    meipass = getattr(sys, "_MEIPASS", None)

    candidates = []
    if meipass:
        candidates.append(os.path.join(meipass, "pruzkumnik.py"))
    candidates.append(os.path.join(BASE_DIR, "pruzkumnik.py"))
    candidates.extend((
        os.path.join(exe_dir, "_internal", "pruzkumnik.py"),
        os.path.join(exe_dir, "pruzkumnik.py"),
        os.path.join(os.getcwd(), "pruzkumnik.py"),
    ))

    unique_candidates = []
    seen = set()
    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if candidate not in seen:
            seen.add(candidate)
            unique_candidates.append(candidate)

    existing_files = [path for path in unique_candidates if os.path.isfile(path)]
    errors = []

    for path in existing_files:
        module_dir = os.path.dirname(path)
        # Modul Průzkumníka může importovat další lokální moduly ze stejné
        # složky. Přidáme ji do sys.path pouze jednou.
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)

        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            errors.append(f"{path}: Python pro tento soubor nevytvořil importní spec.")
            continue

        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(module_name)
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)
            dialog_class = getattr(module, "FileExplorerDialog", None)
            if dialog_class is None:
                raise AttributeError(
                    "Modul se načetl, ale neobsahuje třídu FileExplorerDialog."
                )

            _EXPLORER_DIALOG_CLASS = dialog_class
            return dialog_class

        except Exception as exc:
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            errors.append(f"{path}: {detail}")
            if previous_module is not None:
                sys.modules[module_name] = previous_module
            else:
                sys.modules.pop(module_name, None)

    if existing_files:
        details = "\n".join(f"• {item}" for item in errors)
        raise RuntimeError(
            "Soubor pruzkumnik.py byl nalezen, ale jeho načtení selhalo. "
            "Podrobnosti:\n" + details + "\n\n"
            "Zkontrolujte také, zda jsou v PyInstalleru zahrnuté importy Průzkumníka."
        )

    searched = "\n".join(f"• {path}" for path in unique_candidates)
    raise RuntimeError(
        "Soubor pruzkumnik.py nebyl nalezen. Prohledaná umístění:\n" + searched
    )


def retro_button_style() -> str:
    return """
        QPushButton {
            color: #111; background: #C0C0C0;
            border-top: 2px solid #FFFFFF; border-left: 2px solid #FFFFFF;
            border-right: 2px solid #404040; border-bottom: 2px solid #404040;
            padding: 5px 7px; text-align: left; min-height: 23px;
        }
        QPushButton:hover { background: #D6D6D6; }
        QPushButton:pressed, QPushButton:checked {
            border-top: 2px solid #404040; border-left: 2px solid #404040;
            border-right: 2px solid #FFFFFF; border-bottom: 2px solid #FFFFFF;
            background: #B5B5B5;
        }
        QPushButton:disabled { color: #777; }
    """


class WaveformCanvas(QAbstractScrollArea):
    """Vykreslovací plocha s pevnými popisky a posuvným časovým průběhem."""

    from PyQt5.QtCore import pyqtSignal as _pyqtSignal

    channelSelected = _pyqtSignal(int)
    waveformChanged = _pyqtSignal()
    renameRequested = _pyqtSignal(int)
    zoomRequested = _pyqtSignal(int)
    textRequested = _pyqtSignal(int, int, bool)
    arrowCreated = _pyqtSignal(int, int, int, int)
    annotationsChanged = _pyqtSignal()
    editTextRequested = _pyqtSignal(int)
    gridChanged = _pyqtSignal(int)

    GUTTER = 138
    RULER_H = 28
    LANE_H = 76
    PX_PER_DIV = 76

    def __init__(self, parent=None):
        super().__init__(parent)
        self.channels = []
        self.selected_index = 0
        self.time_unit_index = DEFAULT_TIME_UNIT_INDEX
        self.tool = "paint"
        self.annotation_grid_px = 19

        self.annotations = []
        self.selected_annotation_index = None
        self.pending_arrow = None

        self._pan_origin = None
        self._pan_scroll_origin = 0

        self._drag_annotation_index = None
        self._drag_part = None
        self._drag_start_pos = None
        self._drag_original = None
        self._drag_changed = False
        self._pending_annotation_action = None

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)

        self.setStyleSheet("QScrollBar { background: #C0C0C0; }")
        self.set_channels([])

    def set_channels(self, channels):
        self.channels = channels
        self._update_scrollbars()
        self.viewport().update()

    def set_annotations(self, annotations):
        self.annotations = annotations if isinstance(annotations, list) else []
        self.selected_annotation_index = None
        self._update_scrollbars()
        self.viewport().update()

    def set_annotation_grid_px(self, value):
        self.annotation_grid_px = max(2, min(152, int(value)))
        self.viewport().update()

    def adjust_annotation_grid(self, finer):
        if finer:
            value = max(2, self.annotation_grid_px // 2)
        else:
            value = min(152, self.annotation_grid_px * 2)
        if value != self.annotation_grid_px:
            self.set_annotation_grid_px(value)
            self.gridChanged.emit(value)

    def set_tool(self, tool):
        self.tool = tool
        self.pending_arrow = None
        self._drag_annotation_index = None
        self._drag_part = None
        self._pending_annotation_action = None

        if tool == "pan":
            self.viewport().setCursor(Qt.OpenHandCursor)
        elif tool in ("text", "long_text", "arrow", "paint"):
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().setCursor(Qt.ArrowCursor)

        self.viewport().update()

    def set_selected_index(self, index):
        self.selected_index = max(0, min(index, max(0, len(self.channels) - 1)))
        self.viewport().update()

    def _unit_ns(self):
        return TIME_UNITS_NS[self.time_unit_index]

    def _total_divisions(self):
        """Délka osy se přizpůsobí nejpozdější hraně v absolutním čase."""
        latest_ns = 0

        for channel in self.channels:
            transitions = channel.get("transitions", [])
            if transitions:
                latest_ns = max(latest_ns, max(int(value) for value in transitions))

        for annotation in self.annotations:
            if annotation.get("kind") == "text":
                latest_ns = max(latest_ns, int(annotation.get("x_ns", 0)))
            elif annotation.get("kind") == "arrow":
                latest_ns = max(
                    latest_ns,
                    int(annotation.get("x1_ns", 0)),
                    int(annotation.get("x2_ns", 0))
                )

        needed = int(math.ceil(latest_ns / self._unit_ns())) + 20
        return max(MIN_TIMELINE_DIVISIONS, min(10_000_000, needed))

    def set_time_unit_index(self, index, anchor_x=None):
        """Změní měřítko, ale zachová absolutní časy všech hran.

        Při přiblížení/oddálení se pokud možno zachová čas pod kurzorem
        (u klávesnice pod středem kreslicí plochy).
        """
        new_index = max(0, min(index, len(TIME_UNITS) - 1))
        if new_index == self.time_unit_index:
            return False

        viewport_width = max(self.GUTTER + 1, self.viewport().width())

        if anchor_x is None:
            anchor_x = self.GUTTER + max(1, viewport_width - self.GUTTER) // 2

        anchor_x = max(self.GUTTER, min(int(anchor_x), viewport_width))

        old_scroll = self.horizontalScrollBar().value()
        old_time_ns = (
            (old_scroll + anchor_x - self.GUTTER) / self.PX_PER_DIV
        ) * self._unit_ns()

        self.time_unit_index = new_index
        self._update_scrollbars()

        new_scroll = (
            (old_time_ns / self._unit_ns()) * self.PX_PER_DIV
            - (anchor_x - self.GUTTER)
        )

        self.horizontalScrollBar().setValue(max(0, int(round(new_scroll))))
        self.viewport().update()
        return True

    def _update_scrollbars(self):
        plot_width = self._total_divisions() * self.PX_PER_DIV
        available_width = max(1, self.viewport().width() - self.GUTTER)

        self.horizontalScrollBar().setPageStep(available_width)
        self.horizontalScrollBar().setRange(
            0, max(0, plot_width - available_width)
        )

        content_height = self.RULER_H + len(self.channels) * self.LANE_H + 12

        for annotation in self.annotations:
            if annotation.get("kind") == "text":
                annotation_height = self._text_annotation_layout(
                    annotation, 0, 0
                )[0].height()
                content_height = max(
                    content_height,
                    int(annotation.get("y", self.RULER_H) + annotation_height + 12)
                )
            elif annotation.get("kind") == "arrow":
                content_height = max(
                    content_height,
                    int(annotation.get("y1", self.RULER_H)) + 20,
                    int(annotation.get("y2", self.RULER_H)) + 20
                )

        self.verticalScrollBar().setPageStep(max(1, self.viewport().height()))
        self.verticalScrollBar().setRange(
            0, max(0, content_height - self.viewport().height())
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scrollbars()

    def zoom(self, direction, anchor_x=None):
        # direction -1 = detailnější časová stupnice, +1 = širší časový rozsah.
        new_index = max(
            0,
            min(len(TIME_UNITS) - 1, self.time_unit_index + direction)
        )
        return self.set_time_unit_index(new_index, anchor_x=anchor_x)

    def _channel_at_y(self, y):
        absolute_y = y + self.verticalScrollBar().value()

        if absolute_y < self.RULER_H:
            return None

        index = (absolute_y - self.RULER_H) // self.LANE_H

        if 0 <= index < len(self.channels):
            return int(index)

        return None

    def _tick_at_x(self, x):
        """Převod pozice kurzoru na absolutní čas (ns), zarovnaný na dílek."""
        absolute_x = x - self.GUTTER + self.horizontalScrollBar().value()

        tick = max(
            0,
            min(
                self._total_divisions(),
                int(round(absolute_x / self.PX_PER_DIV))
            )
        )

        return min(
            MAX_TIMELINE_NS,
            int(round(tick * self._unit_ns()))
        )

    def _time_at_x(self, x):
        absolute_x = x - self.GUTTER + self.horizontalScrollBar().value()

        return max(
            0,
            min(
                MAX_TIMELINE_NS,
                int(round(
                    (absolute_x / self.PX_PER_DIV) * self._unit_ns()
                ))
            )
        )

    def _snap_annotation_point(self, x_ns, y):
        """Zarovná bod anotace na neviditelnou mřížku v aktuálním zoomu."""
        grid_px = max(2, int(self.annotation_grid_px))
        step_ns = max(
            1,
            int(round(self._unit_ns() * grid_px / self.PX_PER_DIV))
        )
        snapped_x = int(round(float(x_ns) / step_ns) * step_ns)
        snapped_x = max(0, min(MAX_TIMELINE_NS, snapped_x))

        relative_y = max(0, int(y) - self.RULER_H)
        snapped_y = self.RULER_H + int(round(relative_y / grid_px) * grid_px)
        return snapped_x, snapped_y

    def _content_y_at(self, y):
        return max(
            self.RULER_H,
            int(y + self.verticalScrollBar().value())
        )

    def _annotation_screen_point(self, x_ns, y):
        x = (
            self.GUTTER
            + (float(x_ns) / self._unit_ns()) * self.PX_PER_DIV
            - self.horizontalScrollBar().value()
        )

        screen_y = float(y) - self.verticalScrollBar().value()

        return QPointF(x, screen_y)

    @staticmethod
    def _point_segment_distance(point, start, end):
        vx = end.x() - start.x()
        vy = end.y() - start.y()

        length_sq = vx * vx + vy * vy

        if length_sq <= 1e-9:
            return math.hypot(
                point.x() - start.x(),
                point.y() - start.y()
            )

        t = (
            (point.x() - start.x()) * vx
            + (point.y() - start.y()) * vy
        ) / length_sq

        t = max(0.0, min(1.0, t))

        projection = QPointF(
            start.x() + t * vx,
            start.y() + t * vy
        )

        return math.hypot(
            point.x() - projection.x(),
            point.y() - projection.y()
        )

    @staticmethod
    def _is_long_text_annotation(annotation):
        """Rozpozná dlouhý komentář; zachová i soubory z předchozí verze."""
        return (
            annotation.get("comment_type") == "long"
            or annotation.get("mode") == "long"
            or bool(annotation.get("expandable", False))
        )

    @staticmethod
    def _wrapped_text_document(text, font, width, color=None):
        """Připraví dokument se zalamováním i dlouhých slov a volitelnou barvou."""
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(font)
        option = document.defaultTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(option)
        document.setPlainText(str(text))
        if color is not None:
            cursor = QTextCursor(document)
            cursor.select(QTextCursor.Document)
            char_format = QTextCharFormat()
            char_format.setForeground(QBrush(QColor(color)))
            cursor.mergeCharFormat(char_format)
        document.setTextWidth(max(1.0, float(width)))
        return document

    def _text_annotation_layout(
        self,
        annotation,
        horizontal_offset=None,
        vertical_offset=None
    ):
        if horizontal_offset is None:
            horizontal_offset = self.horizontalScrollBar().value()
        if vertical_offset is None:
            vertical_offset = self.verticalScrollBar().value()

        x = (
            self.GUTTER
            + (float(annotation.get("x_ns", 0)) / self._unit_ns())
            * self.PX_PER_DIV
            - horizontal_offset
        )
        y = float(annotation.get("y", self.RULER_H)) - vertical_offset

        from PyQt5.QtGui import QFontMetrics

        font_size = max(6, min(96, int(annotation.get("font_size", 10))))
        text = str(annotation.get("text", ""))
        long_comment = self._is_long_text_annotation(annotation)

        if not long_comment:
            # Klasická poznámka je skutečně jen text bez rámečku a šipky.
            font = QFont("Tahoma", font_size)
            metrics = QFontMetrics(font)
            bounds = metrics.boundingRect(
                0, 0, 480, 10000, Qt.TextWordWrap, text
            )
            width = max(24, min(480, bounds.width() + 4))
            height = max(metrics.height(), bounds.height()) + 4
            return QRectF(x, y, width, height), height

        # Dlouhý komentář má samostatný nadpis a rozbalitelný popis.
        # Výška se měří QTextDocumentem se stejnou šířkou, jakou dostane
        # vykreslení. Tím se zabrání oříznutí vícerádkového popisu.
        title_font = QFont("Tahoma", font_size, QFont.Bold)
        title_metrics = QFontMetrics(title_font)
        longest_title_line = max(
            (title_metrics.width(line) for line in text.splitlines()),
            default=0
        )
        width = max(170, min(440, longest_title_line + 58))
        title_width = max(90, width - 42)
        title_document = self._wrapped_text_document(
            text, title_font, title_width
        )
        title_height = math.ceil(title_document.size().height())
        header_height = max(
            34,
            title_height + 12
        )
        height = header_height

        description = str(annotation.get("description", ""))
        if annotation.get("expanded", False) and description:
            desc_font = QFont("Tahoma", max(8, font_size - 1))
            desc_width = max(80, width - 18)
            desc_document = self._wrapped_text_document(
                description, desc_font, desc_width
            )
            description_height = max(
                QFontMetrics(desc_font).height(),
                math.ceil(desc_document.size().height())
            ) + 20
            height += description_height

        return QRectF(x, y, width, height), header_height

    def _text_annotation_rect(
        self,
        annotation,
        horizontal_offset=None,
        vertical_offset=None
    ):
        return self._text_annotation_layout(
            annotation, horizontal_offset, vertical_offset
        )[0]

    def _hit_test_annotation(self, position):
        point = QPointF(position)

        for index in range(len(self.annotations) - 1, -1, -1):
            annotation = self.annotations[index]
            kind = annotation.get("kind")

            if kind == "text":
                rect, _header_height = self._text_annotation_layout(annotation)
                if rect.adjusted(-4, -4, 4, 4).contains(point):
                    if self._is_long_text_annotation(annotation):
                        # Kliknutí kamkoli do dlouhého komentáře ho rozbalí/sbalí.
                        # Při tažení se stejná oblast používá k přesunu.
                        return index, "toggle_expanded"
                    return index, "move"

            elif kind == "arrow":
                start = self._annotation_screen_point(
                    annotation.get("x1_ns", 0),
                    annotation.get("y1", self.RULER_H)
                )
                end = self._annotation_screen_point(
                    annotation.get("x2_ns", 0),
                    annotation.get("y2", self.RULER_H)
                )

                start_distance = math.hypot(
                    point.x() - start.x(), point.y() - start.y()
                )
                end_distance = math.hypot(
                    point.x() - end.x(), point.y() - end.y()
                )

                if start_distance <= 10:
                    return index, "start"
                if end_distance <= 10:
                    return index, "end"
                if self._point_segment_distance(point, start, end) <= 7:
                    return index, "move"

        return None, None

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            menu = QMenu(self)
            grid_menu = menu.addMenu("Upravit mřížku")
            finer = grid_menu.addAction("Jemnější mřížka")
            coarser = grid_menu.addAction("Hrubší mřížka")
            grid_menu.addSeparator()
            current = grid_menu.addAction(
                f"Aktuální rozteč: {self.annotation_grid_px} px"
            )
            current.setEnabled(False)
            chosen = menu.exec_(self.viewport().mapToGlobal(event.pos()))
            if chosen is finer:
                self.adjust_annotation_grid(True)
            elif chosen is coarser:
                self.adjust_annotation_grid(False)
            event.accept()
            return

        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        if self.tool == "pan":
            self._pan_origin = event.pos()
            self._pan_scroll_origin = self.horizontalScrollBar().value()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return

        if self.tool in ("text", "long_text"):
            if (
                event.pos().x() >= self.GUTTER
                and event.pos().y() >= self.RULER_H
            ):
                x_ns, y = self._snap_annotation_point(
                    self._time_at_x(event.pos().x()),
                    self._content_y_at(event.pos().y())
                )
                self.textRequested.emit(
                    x_ns, y, self.tool == "long_text"
                )
            return

        if self.tool == "arrow":
            if (
                event.pos().x() >= self.GUTTER
                and event.pos().y() >= self.RULER_H
            ):
                point = self._snap_annotation_point(
                    self._time_at_x(event.pos().x()),
                    self._content_y_at(event.pos().y())
                )

                if self.pending_arrow is None:
                    self.pending_arrow = point
                else:
                    x1, y1 = self.pending_arrow
                    x2, y2 = point
                    self.pending_arrow = None

                    if (x1, y1) != (x2, y2):
                        self.arrowCreated.emit(x1, y1, x2, y2)

                self.viewport().update()

            return

        if self.tool == "select":
            index, part = self._hit_test_annotation(event.pos())

            self.selected_annotation_index = index
            self._drag_annotation_index = index
            self._pending_annotation_action = (
                "toggle_expanded" if part == "toggle_expanded" else None
            )
            # Kliknutí na dlouhý komentář přepne popis; tažení ho přesune.
            self._drag_part = "move" if part == "toggle_expanded" else part

            self._drag_start_pos = (
                QPoint(event.pos()) if index is not None else None
            )

            self._drag_original = (
                deepcopy(self.annotations[index])
                if index is not None else None
            )

            self._drag_changed = False
            self.viewport().update()

            if index is None:
                channel_index = self._channel_at_y(event.pos().y())

                if channel_index is not None:
                    self.selected_index = channel_index
                    self.channelSelected.emit(channel_index)

            return

        index = self._channel_at_y(event.pos().y())

        if index is None:
            return

        self.selected_index = index
        self.viewport().update()
        self.channelSelected.emit(index)

        if self.tool == "paint" and event.pos().x() >= self.GUTTER:
            self._toggle_transition(
                index,
                self._tick_at_x(event.pos().x())
            )
            self.waveformChanged.emit()

    def mouseMoveEvent(self, event):
        if (
            self.tool == "pan"
            and self._pan_origin is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            delta = event.pos().x() - self._pan_origin.x()
            bar = self.horizontalScrollBar()
            bar.setValue(self._pan_scroll_origin - delta)
            return

        if (
            self.tool == "select"
            and self._drag_annotation_index is not None
            and self._drag_start_pos is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            dx = event.pos().x() - self._drag_start_pos.x()
            dy = event.pos().y() - self._drag_start_pos.y()

            if abs(dx) + abs(dy) >= 2:
                annotation = self.annotations[self._drag_annotation_index]
                original = self._drag_original

                delta_ns = int(round(
                    (dx / self.PX_PER_DIV) * self._unit_ns()
                ))

                if original.get("kind") == "text":
                    new_x, new_y = self._snap_annotation_point(
                        int(original.get("x_ns", 0)) + delta_ns,
                        int(original.get("y", self.RULER_H)) + dy
                    )
                    annotation["x_ns"] = new_x
                    annotation["y"] = new_y

                elif original.get("kind") == "arrow":
                    if self._drag_part == "start":
                        new_x, new_y = self._snap_annotation_point(
                            int(original.get("x1_ns", 0)) + delta_ns,
                            int(original.get("y1", self.RULER_H)) + dy
                        )
                        annotation["x1_ns"] = new_x
                        annotation["y1"] = new_y

                    elif self._drag_part == "end":
                        new_x, new_y = self._snap_annotation_point(
                            int(original.get("x2_ns", 0)) + delta_ns,
                            int(original.get("y2", self.RULER_H)) + dy
                        )
                        annotation["x2_ns"] = new_x
                        annotation["y2"] = new_y

                    else:
                        new_x1, new_y1 = self._snap_annotation_point(
                            int(original.get("x1_ns", 0)) + delta_ns,
                            int(original.get("y1", self.RULER_H)) + dy
                        )
                        new_x2, new_y2 = self._snap_annotation_point(
                            int(original.get("x2_ns", 0)) + delta_ns,
                            int(original.get("y2", self.RULER_H)) + dy
                        )
                        annotation["x1_ns"] = new_x1
                        annotation["y1"] = new_y1
                        annotation["x2_ns"] = new_x2
                        annotation["y2"] = new_y2

                self._drag_changed = annotation != original
                self.viewport().update()

            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pan_origin is not None:
            self._pan_origin = None
            self.viewport().setCursor(Qt.OpenHandCursor)
            return

        if (
            event.button() == Qt.LeftButton
            and self._drag_annotation_index is not None
        ):
            if self._drag_changed:
                self._update_scrollbars()
                self.annotationsChanged.emit()
            elif self._pending_annotation_action == "toggle_expanded":
                annotation = self.annotations[self._drag_annotation_index]
                annotation["expanded"] = not bool(
                    annotation.get("expanded", False)
                )
                self._update_scrollbars()
                self.annotationsChanged.emit()
                self.viewport().update()

            self._drag_annotation_index = None
            self._drag_part = None
            self._drag_start_pos = None
            self._drag_original = None
            self._drag_changed = False
            self._pending_annotation_action = None
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        index, _part = self._hit_test_annotation(event.pos())

        if index is not None:
            self.selected_annotation_index = index

            if self.annotations[index].get("kind") == "text":
                self.editTextRequested.emit(index)

            self.viewport().update()
            return

        index = self._channel_at_y(event.pos().y())

        if index is not None and event.pos().x() < self.GUTTER:
            self.selected_index = index
            self.channelSelected.emit(index)
            self.renameRequested.emit(index)

    def wheelEvent(self, event):
        modifiers = event.modifiers()
        delta = event.angleDelta()

        # Ctrl + kolečko mění časovou jednotku. Čas pod kurzorem zůstává
        # pokud možno na stejné vodorovné pozici.
        if modifiers & Qt.ControlModifier:
            amount = delta.y() or delta.x()

            if amount:
                changed = self.zoom(
                    -1 if amount > 0 else 1,
                    anchor_x=event.pos().x()
                )

                if changed:
                    self.zoomRequested.emit(self.time_unit_index)

                event.accept()
                return

        # Shift + kolečko posouvá vodorovně časovou osu (jako trackpad).
        if modifiers & Qt.ShiftModifier:
            amount = delta.x() or delta.y()
            pixel_amount = (
                event.pixelDelta().x() or event.pixelDelta().y()
            )

            if pixel_amount:
                scroll_delta = -pixel_amount
            elif amount:
                scroll_delta = -int(
                    (amount / 120.0) * self.PX_PER_DIV * 3
                )
            else:
                scroll_delta = 0

            if scroll_delta:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() + scroll_delta)

            event.accept()
            return

        super().wheelEvent(event)

    def _toggle_transition(self, channel_index, time_ns):
        transitions = self.channels[channel_index].setdefault(
            "transitions", []
        )

        time_ns = max(0, min(MAX_TIMELINE_NS, int(time_ns)))
        position = bisect_left(transitions, time_ns)

        if (
            position < len(transitions)
            and transitions[position] == time_ns
        ):
            transitions.pop(position)
        else:
            transitions.insert(position, time_ns)

        self._update_scrollbars()
        self.viewport().update()

    def _draw_arrow(
        self,
        painter,
        start,
        end,
        color,
        width=2.2,
        selected=False
    ):
        pen_color = QColor(color or "#FFFFFF")

        pen = QPen(
            pen_color,
            float(width),
            Qt.SolidLine,
            Qt.SquareCap,
            Qt.MiterJoin
        )

        painter.setPen(pen)
        painter.drawLine(start, end)

        angle = math.atan2(
            end.y() - start.y(),
            end.x() - start.x()
        )

        head_length = 12.0
        spread = math.radians(27)

        left = QPointF(
            end.x() - head_length * math.cos(angle - spread),
            end.y() - head_length * math.sin(angle - spread)
        )

        right = QPointF(
            end.x() - head_length * math.cos(angle + spread),
            end.y() - head_length * math.sin(angle + spread)
        )

        painter.drawLine(end, left)
        painter.drawLine(end, right)

        if selected:
            painter.setBrush(QColor("#FFFFFF"))
            painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
            painter.drawEllipse(start, 4.5, 4.5)
            painter.drawEllipse(end, 4.5, 4.5)

    def _draw_annotations(
        self,
        painter,
        width,
        height,
        horizontal_offset,
        vertical_offset
    ):
        for index, annotation in enumerate(self.annotations):
            kind = annotation.get("kind")
            selected = index == self.selected_annotation_index

            if kind == "text":
                rect, header_height = self._text_annotation_layout(
                    annotation,
                    horizontal_offset,
                    vertical_offset
                )

                if (
                    rect.right() < 0
                    or rect.left() > width
                    or rect.bottom() < 0
                    or rect.top() > height
                ):
                    continue

                text_color = QColor(annotation.get("color", "#FFFFFF"))
                if not text_color.isValid():
                    text_color = QColor("#FFFFFF")
                font_size = max(6, min(96, int(annotation.get("font_size", 10))))
                text = str(annotation.get("text", ""))

                if not self._is_long_text_annotation(annotation):
                    # Výchozí klasický komentář: pouze text, žádné pozadí,
                    # rámeček ani rozbalovací symbol.
                    painter.setFont(QFont("Tahoma", font_size))
                    painter.setPen(text_color)
                    painter.drawText(
                        rect.adjusted(1, 1, -1, -1),
                        Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                        text
                    )
                    continue

                # Dlouhý komentář: karta s nadpisem a rozbalovacím popisem.
                fill = QColor("#3C3C3C")
                if not fill.isValid():
                    fill = QColor("#252525")
                fill.setAlpha(238)
                border_color = QColor("#818181" if selected else "#1B1B1B")
                painter.setPen(QPen(border_color, 2 if selected else 1))
                painter.setBrush(fill)
                painter.drawRect(rect)

                title_font = QFont("Tahoma", font_size, QFont.Bold)
                painter.setFont(title_font)
                painter.setPen(text_color)

                title_rect = QRectF(
                    rect.left() + 8,
                    rect.top() + 4,
                    max(50, rect.width() - 42),
                    header_height - 8
                )
                title_document = self._wrapped_text_document(
                    text, title_font, title_rect.width(), text_color
                )
                painter.save()
                painter.translate(title_rect.topLeft())
                title_document.drawContents(
                    painter,
                    QRectF(0, 0, title_rect.width(), title_rect.height())
                )
                painter.restore()

                toggle_rect = QRectF(
                    rect.right() - 30,
                    rect.top(),
                    30,
                    header_height
                )
                painter.setPen(QPen(border_color, 1))
                painter.drawLine(
                    QPointF(toggle_rect.left(), toggle_rect.top()),
                    QPointF(toggle_rect.left(), toggle_rect.bottom())
                )
                glyph = "▾" if annotation.get("expanded", False) else "▸"
                painter.setPen(text_color)
                painter.drawText(toggle_rect, Qt.AlignCenter, glyph)

                description = str(annotation.get("description", ""))
                if annotation.get("expanded", False) and description:
                    desc_font = QFont("Tahoma", max(8, font_size - 1))
                    desc_rect = QRectF(
                        rect.left() + 9,
                        rect.top() + header_height + 5,
                        max(1, rect.width() - 18),
                        max(1, rect.height() - header_height - 10)
                    )
                    desc_document = self._wrapped_text_document(
                        description, desc_font, desc_rect.width(), text_color
                    )
                    painter.setFont(desc_font)
                    painter.setPen(text_color)
                    painter.save()
                    painter.translate(desc_rect.topLeft())
                    desc_document.drawContents(
                        painter,
                        QRectF(
                            0, 0,
                            desc_rect.width(),
                            desc_rect.height()
                        )
                    )
                    painter.restore()

            elif kind == "arrow":
                start = self._annotation_screen_point(
                    annotation.get("x1_ns", 0),
                    annotation.get("y1", self.RULER_H)
                )

                end = self._annotation_screen_point(
                    annotation.get("x2_ns", 0),
                    annotation.get("y2", self.RULER_H)
                )

                self._draw_arrow(
                    painter,
                    start,
                    end,
                    annotation.get("color", "#FFFFFF"),
                    annotation.get("width", 2.2),
                    selected
                )

        if self.pending_arrow is not None:
            start = self._annotation_screen_point(*self.pending_arrow)

            painter.setPen(QPen(
                QColor("#FFFFFF"),
                2,
                Qt.DashLine
            ))

            painter.drawEllipse(start, 4, 4)
            painter.drawText(
                start + QPointF(8, -8),
                "Klikněte na konec šipky"
            )

    def draw_scene(
        self,
        painter,
        width,
        height,
        horizontal_offset=0,
        vertical_offset=0
    ):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(
            0, 0, width, height, QColor("#000000")
        )

        # Pevný levý sloupec s názvy kanálů.
        painter.fillRect(
            0, 0, self.GUTTER, height, QColor("#080808")
        )

        painter.fillRect(
            0, 0, self.GUTTER, self.RULER_H, QColor("#1B1B1B")
        )

        painter.setPen(QPen(QColor("#555555"), 1))
        painter.drawLine(
            self.GUTTER - 1, 0,
            self.GUTTER - 1, height
        )

        painter.drawLine(
            0, self.RULER_H - 1,
            width, self.RULER_H - 1
        )

        font = QFont("Tahoma", 8)
        painter.setFont(font)
        painter.setPen(QColor("#C8C8C8"))
        painter.drawText(10, 18, "KANÁL")

        painter.setPen(QColor("#A0A0A0"))
        painter.drawText(
            self.GUTTER + 8,
            18,
            f"Čas / dílek: {TIME_UNIT_LABELS[self.time_unit_index]}"
        )

        # Svislá mřížka představuje aktuální časovou jednotku.
        total_divisions = self._total_divisions()

        first_tick = max(
            0,
            int(horizontal_offset // self.PX_PER_DIV) - 1
        )

        last_tick = min(
            total_divisions,
            int(
                (horizontal_offset + max(0, width - self.GUTTER))
                // self.PX_PER_DIV
            ) + 2
        )

        unit_ns = self._unit_ns()

        # Při exportu extrémně dlouhé osy se čáry mřížky vhodně zředí.
        grid_stride = max(
            1,
            int(math.ceil(
                max(1, last_tick - first_tick + 1) / 50_000
            ))
        )

        for tick in range(first_tick, last_tick + 1, grid_stride):
            x = (
                self.GUTTER
                + tick * self.PX_PER_DIV
                - horizontal_offset
            )

            major = (tick % 10 == 0)

            painter.setPen(QPen(
                QColor("#3A3A3A" if major else "#262626"),
                1
            ))

            painter.drawLine(
                int(x),
                self.RULER_H,
                int(x),
                height
            )

            if major and tick > 0:
                absolute_seconds = (
                    tick * unit_ns
                ) / 1_000_000_000

                unit_label = self._format_time(absolute_seconds)

                painter.setPen(QColor("#9A9A9A"))
                painter.drawText(
                    int(x + 4),
                    18,
                    unit_label
                )

        # Signály a popisky kanálů.
        for index, channel in enumerate(self.channels):
            row_top = (
                self.RULER_H
                + index * self.LANE_H
                - vertical_offset
            )

            if row_top + self.LANE_H < self.RULER_H or row_top > height:
                continue

            if index == self.selected_index:
                painter.fillRect(
                    0,
                    int(row_top),
                    self.GUTTER - 1,
                    self.LANE_H,
                    QColor("#202020")
                )

            color = QColor(channel.get("color", "#00FF66"))

            painter.fillRect(
                10,
                int(row_top + 29),
                9,
                9,
                color
            )

            painter.setPen(QColor(
                "#E0E0E0" if index == self.selected_index else "#B0B0B0"
            ))

            painter.drawText(
                26,
                int(row_top + 37),
                str(channel.get("name", f"CH {index + 1}"))[:17]
            )

            painter.setPen(QPen(
                color,
                2.4,
                Qt.SolidLine,
                Qt.FlatCap,
                Qt.MiterJoin
            ))

            high_y = row_top + 19
            low_y = row_top + 52
            current_y = low_y
            previous_x = self.GUTTER - horizontal_offset

            for transition_ns in channel.get("transitions", []):
                x = (
                    self.GUTTER
                    + (int(transition_ns) / unit_ns) * self.PX_PER_DIV
                    - horizontal_offset
                )

                painter.drawLine(
                    QPointF(previous_x, current_y),
                    QPointF(x, current_y)
                )

                next_y = high_y if current_y == low_y else low_y

                painter.drawLine(
                    QPointF(x, current_y),
                    QPointF(x, next_y)
                )

                current_y = next_y
                previous_x = x

            end_x = (
                self.GUTTER
                + total_divisions * self.PX_PER_DIV
                - horizontal_offset
            )

            painter.drawLine(
                QPointF(previous_x, current_y),
                QPointF(end_x, current_y)
            )

        # Komentáře a šipky se vykreslují nad průběhy a jsou součástí exportu.
        self._draw_annotations(
            painter,
            width,
            height,
            horizontal_offset,
            vertical_offset
        )

        painter.restore()

    @staticmethod
    def _format_time(seconds):
        if seconds == 0:
            return "0"
        if seconds < 1e-6:
            return f"{seconds * 1e9:g} ns"
        if seconds < 1e-3:
            return f"{seconds * 1e6:g} µs"
        if seconds < 1:
            return f"{seconds * 1e3:g} ms"
        return f"{seconds:g} s"

    def paintEvent(self, event):
        painter = QPainter(self.viewport())

        self.draw_scene(
            painter,
            self.viewport().width(),
            self.viewport().height(),
            self.horizontalScrollBar().value(),
            self.verticalScrollBar().value()
        )

        painter.end()

    def export_size(self):
        # Celý časový rozsah se exportuje, ale extrémně široká časová osa
        # se přiměřeně zmenší, aby nevznikaly mnohagigabajtové bitmapy.
        natural_width = (
            self.GUTTER
            + self._total_divisions() * self.PX_PER_DIV
        )

        natural_height = (
            self.RULER_H
            + len(self.channels) * self.LANE_H
            + 12
        )

        # Zahrne i anotace umístěné pod posledním kanálem.
        for annotation in self.annotations:
            if annotation.get("kind") == "text":
                annotation_height = self._text_annotation_layout(
                    annotation, 0, 0
                )[0].height()
                natural_height = max(
                    natural_height,
                    int(annotation.get("y", self.RULER_H) + annotation_height + 12)
                )
            elif annotation.get("kind") == "arrow":
                natural_height = max(
                    natural_height,
                    int(annotation.get("y1", self.RULER_H)) + 20,
                    int(annotation.get("y2", self.RULER_H)) + 20
                )

        scale = min(
            1.0,
            30_000 / max(1, natural_width),
            12_000 / max(1, natural_height)
        )

        return (
            max(1, int(natural_width * scale)),
            max(1, int(natural_height * scale))
        )

    def export_to_painter(self, painter):
        out_width, out_height = self.export_size()

        natural_width = (
            self.GUTTER
            + self._total_divisions() * self.PX_PER_DIV
        )

        natural_height = (
            self.RULER_H
            + len(self.channels) * self.LANE_H
            + 12
        )

        for annotation in self.annotations:
            if annotation.get("kind") == "text":
                annotation_height = self._text_annotation_layout(
                    annotation, 0, 0
                )[0].height()
                natural_height = max(
                    natural_height,
                    int(annotation.get("y", self.RULER_H) + annotation_height + 12)
                )
            elif annotation.get("kind") == "arrow":
                natural_height = max(
                    natural_height,
                    int(annotation.get("y1", self.RULER_H)) + 20,
                    int(annotation.get("y2", self.RULER_H)) + 20
                )

        painter.save()

        painter.scale(
            out_width / natural_width,
            out_height / natural_height
        )

        self.draw_scene(
            painter,
            natural_width,
            natural_height,
            0,
            0
        )

        painter.restore()


class PoscilogramarWindow(QMainWindow):
    def __init__(self, open_path=None):
        super().__init__()

        self.workspace = default_workspace()
        self.current_path = None
        self.dirty = False
        self._loading = False
        self.selected_index = 0

        self.setWindowTitle(APP_NAME)
        self.resize(1240, 790)
        self.setMinimumSize(900, 560)

        if os.path.isfile(LOGO_PATH):
            self.setWindowIcon(QIcon(LOGO_PATH))

        self._build_ui()
        self._build_menus()
        self._build_shortcuts()
        self._refresh_canvas()
        self._update_status()

        if open_path:
            self.open_path(open_path)

    def _build_ui(self):
        root = QWidget()
        root.setStyleSheet(
            f"background: {WIN_BG}; color: {WIN_TEXT};"
        )

        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        outer.addLayout(body, 1)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(205)

        self.sidebar.setStyleSheet(
            "QFrame#sidebar { background: #C0C0C0; border-right: 2px solid #808080; }"
        )

        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(9, 9, 9, 9)
        side.setSpacing(6)

        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFrameShape(QFrame.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar_scroll.setWidget(self.sidebar)
        sidebar_scroll.setFixedWidth(222)

        body.addWidget(sidebar_scroll)

        side.addWidget(self._sunken_separator())

        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons = {}

        self._section(side, "NÁSTROJE")

        self._tool_button(
            side,
            "✎  Malovat průběh",
            "paint",
            "Kliknutím na svislou mřížku přepnete signál."
        )

        self._tool_button(
            side,
            "✋  Posouvat",
            "pan",
            "Tažením myší posunete časovou osu."
        )

        self._tool_button(
            side,
            "▣  Označit / upravit",
            "select",
            "Komentáře a šipky lze přesouvat. Dlouhý komentář rozbalíte nebo sbalíte kliknutím na jeho plochu."
        )

        self._text_tool_control(
            side,
            "T  Textový komentář",
            "Klikněte do oscilogramu a napište prostý text bez rámečku."
        )

        self._tool_button(
            side,
            "↗  Kreslit šipku",
            "arrow",
            "Klikněte na začátek a potom na konec šipky."
        )

        self._add_button(
            side,
            "Smazat vybranou anotaci",
            self.delete_selected_annotation
        )

        side.addWidget(self._sunken_separator())

        self._section(side, "KANÁLY")
        self._add_button(side, "+  Přidat kanál", self.add_channel)
        self._add_button(side, "Přejmenovat kanál", self.rename_channel)
        self._add_button(side, "Změnit barvu kanálu", self.change_channel_color)
        self._add_button(side, "Odebrat kanál", self.remove_channel)

        side.addWidget(self._sunken_separator())

        self._section(side, "PRACOVNÍ SOUBOR")
        self._add_button(side, "Otevřít .vlcr…", self.open_dialog)
        self._add_button(side, "Uložit", self.save)
        self._add_button(side, "Uložit jako…", self.save_as)

        side.addWidget(self._sunken_separator())

        self._section(side, "EXPORTOVAT")
        self._add_button(
            side,
            "Exportovat PNG…",
            lambda: self.export_dialog("png")
        )
        self._add_button(
            side,
            "Exportovat PDF…",
            lambda: self.export_dialog("pdf")
        )
        self._add_button(
            side,
            "Exportovat SVG…",
            lambda: self.export_dialog("svg")
        )

        side.addStretch(1)

        zoom_frame = QFrame()
        zoom_layout = QHBoxLayout(zoom_frame)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(5)

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_in = QPushButton("+")

        for button in (self.btn_zoom_out, self.btn_zoom_in):
            button.setStyleSheet(retro_button_style())
            button.setFixedWidth(38)

        self.btn_zoom_out.clicked.connect(
            lambda: self.zoom_time(1)
        )

        self.btn_zoom_in.clicked.connect(
            lambda: self.zoom_time(-1)
        )

        zoom_layout.addWidget(self.btn_zoom_out)
        zoom_layout.addWidget(self.btn_zoom_in)

        self.time_unit_label = QLabel("1 µs / dílek")
        self.time_unit_label.setFont(QFont("Tahoma", 8))

        zoom_layout.addWidget(self.time_unit_label, 1)
        side.addWidget(zoom_frame)

        right = QVBoxLayout()
        right.setContentsMargins(5, 5, 5, 5)
        right.setSpacing(0)

        body.addLayout(right, 1)

        canvas_border = QFrame()
        canvas_border.setFrameShape(QFrame.Panel)
        canvas_border.setFrameShadow(QFrame.Sunken)
        canvas_border.setLineWidth(2)

        canvas_layout = QVBoxLayout(canvas_border)
        canvas_layout.setContentsMargins(1, 1, 1, 1)
        canvas_layout.setSpacing(0)

        self.canvas = WaveformCanvas()
        canvas_layout.addWidget(self.canvas)
        right.addWidget(canvas_border, 1)

        self.canvas.channelSelected.connect(
            self.on_channel_selected
        )

        self.canvas.waveformChanged.connect(
            self.mark_dirty
        )

        self.canvas.renameRequested.connect(
            lambda _index: self.rename_channel()
        )

        self.canvas.zoomRequested.connect(
            self._on_zoom_changed
        )

        self.canvas.textRequested.connect(
            self.add_text_annotation
        )

        self.canvas.arrowCreated.connect(
            self.add_arrow_annotation
        )

        self.canvas.annotationsChanged.connect(
            self.mark_dirty
        )

        self.canvas.editTextRequested.connect(
            self.edit_text_annotation
        )

        self.canvas.gridChanged.connect(
            self._on_annotation_grid_changed
        )

        self.status = QLabel("Připraveno")
        self.status.setFont(QFont("Tahoma", 8))
        self.status.setFixedHeight(23)
        self.status.setMargin(5)

        self.status.setStyleSheet(
            "background: #C0C0C0; border-top: 1px solid #FFFFFF; color: #222;"
        )

        outer.addWidget(self.status)

        self.setStyleSheet(
            self.styleSheet()
            + " QMenuBar { background: #C0C0C0; }"
            + " QMenuBar::item { padding: 4px 8px; }"
            + " QMenu { background: #C0C0C0; border: 1px solid #404040; }"
        )

    def _sunken_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _section(self, layout, text):
        label = QLabel(text)
        label.setFont(QFont("Tahoma", 8, QFont.Bold))
        label.setStyleSheet("color: #444; padding-top: 2px;")
        layout.addWidget(label)

    def _add_button(self, layout, text, callback):
        button = QPushButton(text)
        button.setFont(QFont("Tahoma", 8))
        button.setStyleSheet(retro_button_style())
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _text_tool_control(self, layout, text, tooltip):
        """Hlavní tlačítko tvoří prostý text; šipka nabídne dlouhý komentář."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        button = QPushButton(text)
        button.setCheckable(True)
        button.setToolTip(tooltip)
        button.setFont(QFont("Tahoma", 8))
        button.setStyleSheet(retro_button_style())
        self.tool_group.addButton(button)
        self.tool_buttons["text"] = button
        button.clicked.connect(lambda _checked=False: self.set_tool("text"))
        row.addWidget(button, 1)

        dropdown = QToolButton()
        dropdown.setText("▾")
        dropdown.setToolTip("Další druh textového komentáře")
        dropdown.setFixedWidth(27)
        dropdown.setStyleSheet("""
            QToolButton {
                color: #111; background: #C0C0C0;
                border-top: 2px solid #FFFFFF; border-left: 2px solid #FFFFFF;
                border-right: 2px solid #404040; border-bottom: 2px solid #404040;
                padding: 3px;
            }
            QToolButton:hover { background: #D6D6D6; }
            QToolButton:pressed { background: #B5B5B5; }
            QMenu { background: #C0C0C0; border: 1px solid #404040; }
        """)
        menu = QMenu(dropdown)
        long_action = menu.addAction("Dlouhý komentář…")
        long_action.triggered.connect(lambda: self.set_tool("long_text"))
        dropdown.setMenu(menu)
        dropdown.setPopupMode(QToolButton.InstantPopup)
        row.addWidget(dropdown)

        layout.addLayout(row)
        return button

    def _tool_button(self, layout, text, tool, tooltip):
        button = QPushButton(text)
        button.setCheckable(True)
        button.setToolTip(tooltip)
        button.setFont(QFont("Tahoma", 8))
        button.setStyleSheet(retro_button_style())

        self.tool_group.addButton(button)
        self.tool_buttons[tool] = button

        button.clicked.connect(
            lambda _checked=False, t=tool: self.set_tool(t)
        )

        layout.addWidget(button)

        if tool == "paint":
            button.setChecked(True)

        return button

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("Soubor")

        self._menu_action(
            file_menu, "Nový", self.new_workspace, "Ctrl+N"
        )

        self._menu_action(
            file_menu, "Otevřít…", self.open_dialog, "Ctrl+O"
        )

        file_menu.addSeparator()

        self._menu_action(
            file_menu, "Uložit", self.save, "Ctrl+S"
        )

        self._menu_action(
            file_menu, "Uložit jako…", self.save_as, "Ctrl+Shift+S"
        )

        export_menu = file_menu.addMenu("Exportovat")

        self._menu_action(
            export_menu, "PNG…", lambda: self.export_dialog("png")
        )

        self._menu_action(
            export_menu, "PDF…", lambda: self.export_dialog("pdf")
        )

        self._menu_action(
            export_menu, "SVG…", lambda: self.export_dialog("svg")
        )

        file_menu.addSeparator()

        self._menu_action(
            file_menu, "Ukončit", self.close, "Alt+F4"
        )

        view_menu = self.menuBar().addMenu("Zobrazení")

        self._menu_action(
            view_menu,
            "Přiblížit časovou osu",
            lambda: self.zoom_time(-1)
        )

        self._menu_action(
            view_menu,
            "Oddálit časovou osu",
            lambda: self.zoom_time(1)
        )

        channel_menu = self.menuBar().addMenu("Kanál")

        self._menu_action(
            channel_menu, "Přidat kanál", self.add_channel
        )

        self._menu_action(
            channel_menu, "Přejmenovat kanál", self.rename_channel
        )

        self._menu_action(
            channel_menu, "Změnit barvu…", self.change_channel_color
        )

        self._menu_action(
            channel_menu, "Odebrat kanál", self.remove_channel
        )

        help_menu = self.menuBar().addMenu("Nápověda")

        self._menu_action(
            help_menu, "Klávesové zkratky", self.show_shortcuts
        )
        help_menu.addSeparator()
        self._menu_action(
            help_menu, "O aplikaci", self.about
        )

    def _menu_action(self, menu, text, callback, shortcut=None):
        action = QAction(text, self)

        if shortcut:
            action.setShortcut(QKeySequence(shortcut))

        action.triggered.connect(callback)
        menu.addAction(action)

        return action

    def _build_shortcuts(self):
        for sequence, direction in (
            ("Ctrl++", -1),
            ("Ctrl+=", -1),
            ("Ctrl+-", 1)
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(
                lambda d=direction: self.zoom_time(d)
            )

        for sequence, tool in (
            ("Ctrl+D", "paint"),
            ("Ctrl+T", "text"),
            ("Ctrl+E", "select"),
            ("Ctrl+M", "pan"),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(
                lambda selected_tool=tool: self.set_tool(selected_tool)
            )

        delete_shortcut = QShortcut(
            QKeySequence(Qt.Key_Delete),
            self
        )

        delete_shortcut.setContext(Qt.WindowShortcut)
        delete_shortcut.activated.connect(
            self.delete_selected_annotation
        )

    def _refresh_canvas(self):
        self.workspace.setdefault("annotations", [])

        self.canvas.set_channels(
            self.workspace["channels"]
        )

        self.canvas.set_annotations(
            self.workspace["annotations"]
        )
        self.canvas.set_annotation_grid_px(
            self.workspace.get("annotation_grid_px", 19)
        )

        self.canvas.set_selected_index(
            self.selected_index
        )

        self.canvas.time_unit_index = max(
            0,
            min(
                len(TIME_UNITS) - 1,
                self.workspace.get(
                    "time_unit_index",
                    DEFAULT_TIME_UNIT_INDEX
                )
            )
        )

        self.canvas._update_scrollbars()
        self.canvas.viewport().update()

        self.time_unit_label.setText(
            TIME_UNIT_LABELS[
                self.workspace.get(
                    "time_unit_index",
                    DEFAULT_TIME_UNIT_INDEX
                )
            ] + " / dílek"
        )

        self._update_status()

    def _update_status(self):
        unit = TIME_UNIT_LABELS[
            self.workspace.get(
                "time_unit_index",
                DEFAULT_TIME_UNIT_INDEX
            )
        ]

        count = len(self.workspace.get("channels", []))

        file_name = (
            os.path.basename(self.current_path)
            if self.current_path
            else "Nový pracovní soubor"
        )

        dirty_mark = (
            " • změny nejsou uloženy"
            if self.dirty else ""
        )

        self.status.setText(
            f"{file_name}    |    "
            f"{count}/{MAX_CHANNELS} kanálů    |    "
            f"{unit} / dílek{dirty_mark}"
        )

        self.setWindowTitle(
            f"{APP_NAME} — {file_name}{' *' if self.dirty else ''}"
        )

    def set_tool(self, tool):
        self.canvas.set_tool(tool)

        button_key = "text" if tool == "long_text" else tool
        if button_key in self.tool_buttons:
            self.tool_buttons[button_key].setChecked(True)

        tips = {
            "paint": "Malování průběhu: kliknutím na mřížku přepnete signál.",
            "pan": "Posouvání: táhněte myší nebo použijte Shift + kolečko.",
            "select": "Označení a úpravy: komentář nebo šipku přetáhněte. Kliknutím na dlouhý komentář zobrazíte či skryjete popis.",
            "text": "Klasický text: klikněte do oscilogramu a napište prostý text.",
            "long_text": "Dlouhý komentář: klikněte do oscilogramu a zadejte nadpis i popis.",
            "arrow": "Šipka: klikněte na začátek a potom na konec."
        }

        self.status.setToolTip(tips.get(tool, ""))

    def on_channel_selected(self, index):
        self.selected_index = index
        self.canvas.set_selected_index(index)
        self._update_status()

    def mark_dirty(self):
        if self._loading:
            return

        self.dirty = True
        self._update_status()

    def zoom_time(self, direction):
        current = self.workspace.get(
            "time_unit_index",
            DEFAULT_TIME_UNIT_INDEX
        )

        new_index = max(
            0,
            min(len(TIME_UNITS) - 1, current + direction)
        )

        if (
            new_index != current
            and self.canvas.set_time_unit_index(new_index)
        ):
            self.workspace["time_unit_index"] = new_index

            self.time_unit_label.setText(
                TIME_UNIT_LABELS[new_index] + " / dílek"
            )

            self.mark_dirty()

    def _on_zoom_changed(self, index):
        self.workspace["time_unit_index"] = index

        self.time_unit_label.setText(
            TIME_UNIT_LABELS[index] + " / dílek"
        )

        self.mark_dirty()

    def _on_annotation_grid_changed(self, value):
        self.workspace["annotation_grid_px"] = int(value)
        self.mark_dirty()

    def show_shortcuts(self):
        QMessageBox.information(
            self,
            "Klávesové zkratky",
            "NÁSTROJE\n"
            "Ctrl+D  Malovat průběh\n"
            "Ctrl+T  Textový komentář\n"
            "Ctrl+E  Označit / upravit komentář a šipku\n"
            "Ctrl+M  Posouvat časovou osu\n"
            "Ctrl+Enter  Potvrdit textový dialog\n\n"
            "SOUBOR\n"
            "Ctrl+N  Nový pracovní soubor\n"
            "Ctrl+O  Otevřít pracovní soubor\n"
            "Ctrl+S  Uložit\n"
            "Ctrl+Shift+S  Uložit jako\n"
            "Delete  Smazat vybranou anotaci\n"
            "Alt+F4  Ukončit\n\n"
            "ZOBRAZENÍ\n"
            "Ctrl++ / Ctrl+=  Přiblížit časovou osu\n"
            "Ctrl+-  Oddálit časovou osu\n"
            "Ctrl + kolečko myši  Změnit časovou jednotku\n"
            "Shift + kolečko myši  Posunout vodorovně\n\n"
            "Pravé tlačítko myši na oscilogramu: Upravit mřížku anotací."
        )

    def _text_annotation_dialog(
        self,
        dialog_title,
        is_long=False,
        initial_title="",
        initial_description="",
        initial_color="#FFFFFF"
    ):
        """Dialog prosté poznámky nebo dlouhého komentáře včetně barvy písma.

        Ctrl+Enter (včetně Enter na numerické klávesnici) potvrzuje dialog.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(dialog_title)
        dialog.resize(520, 390 if is_long else 300)

        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        if is_long:
            title_edit = QLineEdit(str(initial_title))
            title_edit.setMaxLength(500)
            title_edit.setPlaceholderText("Krátký nadpis komentáře")
            form.addRow("Nadpis:", title_edit)

            description_edit = QTextEdit()
            description_edit.setPlainText(str(initial_description))
            description_edit.setPlaceholderText("Dlouhý popis komentáře…")
            description_edit.setMinimumHeight(190)
            form.addRow("Popis:", description_edit)
        else:
            title_edit = QTextEdit()
            title_edit.setPlainText(str(initial_title))
            title_edit.setPlaceholderText("Text komentáře…")
            title_edit.setMinimumHeight(145)
            form.addRow("Text:", title_edit)
            description_edit = None

        initial_qcolor = QColor(initial_color)
        if not initial_qcolor.isValid():
            initial_qcolor = QColor("#FFFFFF")
        selected_color = {"value": initial_qcolor.name().upper()}

        color_row = QWidget(dialog)
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(7)

        color_preview = QFrame(color_row)
        color_preview.setFixedSize(28, 22)
        color_label = QLabel(selected_color["value"], color_row)
        color_button = QPushButton("Změnit barvu…", color_row)
        color_button.setFont(QFont("Tahoma", 8))
        color_button.setStyleSheet(retro_button_style())

        def update_color_preview():
            color_preview.setStyleSheet(
                "QFrame { background: %s; border: 1px solid #404040; }"
                % selected_color["value"]
            )
            color_label.setText(selected_color["value"])

        def choose_text_color():
            chosen = QColorDialog.getColor(
                QColor(selected_color["value"]),
                dialog,
                "Barva písma poznámky"
            )
            if chosen.isValid():
                selected_color["value"] = chosen.name().upper()
                update_color_preview()

        color_button.clicked.connect(choose_text_color)
        update_color_preview()

        color_layout.addWidget(color_preview)
        color_layout.addWidget(color_label)
        color_layout.addWidget(color_button)
        color_layout.addStretch(1)
        form.addRow("Barva písma:", color_row)
        layout.addLayout(form)

        hint = QLabel("Ctrl+Enter = potvrdit")
        hint.setStyleSheet("color: #555; font-size: 8pt;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        # Qt rozlišuje Return a Enter (numerická klávesnice).
        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(sequence), dialog)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(dialog.accept)

        title_edit.setFocus()

        if dialog.exec_() != QDialog.Accepted:
            return None

        if is_long:
            title = title_edit.text().strip()[:500]
            description = description_edit.toPlainText().strip()[:5000]
            if not title:
                QMessageBox.information(
                    self,
                    "Dlouhý komentář",
                    "Dlouhý komentář musí mít krátký nadpis."
                )
                return None
            if not description:
                QMessageBox.information(
                    self,
                    "Dlouhý komentář",
                    "Zadejte také popisný text."
                )
                return None
            return title, description, selected_color["value"]

        plain_text = title_edit.toPlainText().strip()[:500]
        if not plain_text:
            return None
        return plain_text, "", selected_color["value"]

    def add_text_annotation(self, x_ns, y, is_long=False):
        fields = self._text_annotation_dialog(
            "Nový dlouhý komentář" if is_long else "Nový textový komentář",
            is_long=is_long
        )
        if fields is None:
            return

        title, description, text_color = fields
        comment_type = "long" if is_long else "plain"

        self.workspace.setdefault("annotations", []).append({
            "kind": "text",
            "comment_type": comment_type,
            "x_ns": int(x_ns),
            "y": int(y),
            "text": title,
            "description": description,
            "expandable": bool(is_long),
            "expanded": False,
            "color": text_color,
            "background": "#252525" if is_long else "#FFFFFF",
            "font_size": 10
        })

        self.canvas.set_annotations(self.workspace["annotations"])
        self.canvas.selected_annotation_index = (
            len(self.workspace["annotations"]) - 1
        )
        self.canvas._update_scrollbars()
        self.canvas.viewport().update()
        self.mark_dirty()

    def add_arrow_annotation(
        self,
        x1_ns,
        y1,
        x2_ns,
        y2
    ):
        self.workspace.setdefault(
            "annotations", []
        ).append({
            "kind": "arrow",
            "x1_ns": int(x1_ns),
            "y1": int(y1),
            "x2_ns": int(x2_ns),
            "y2": int(y2),
            "color": "#FFFFFF",
            "width": 2.2
        })

        self.canvas.set_annotations(
            self.workspace["annotations"]
        )

        self.canvas.selected_annotation_index = (
            len(self.workspace["annotations"]) - 1
        )

        self.canvas.viewport().update()
        self.mark_dirty()

    def edit_text_annotation(self, index):
        annotations = self.workspace.setdefault("annotations", [])
        if not (0 <= index < len(annotations)):
            return

        annotation = annotations[index]
        if annotation.get("kind") != "text":
            return

        is_long = self.canvas._is_long_text_annotation(annotation)
        fields = self._text_annotation_dialog(
            "Upravit dlouhý komentář" if is_long else "Upravit textový komentář",
            is_long=is_long,
            initial_title=str(annotation.get("text", "")),
            initial_description=str(annotation.get("description", "")),
            initial_color=str(annotation.get("color", "#FFFFFF"))
        )
        if fields is None:
            return

        (
            annotation["text"],
            annotation["description"],
            annotation["color"]
        ) = fields
        annotation["comment_type"] = "long" if is_long else "plain"
        annotation["expandable"] = bool(is_long)
        if not is_long:
            annotation["expanded"] = False

        self.canvas._update_scrollbars()
        self.canvas.viewport().update()
        self.mark_dirty()

    def delete_selected_annotation(self):
        index = (
            self.canvas.selected_annotation_index
            if hasattr(self, "canvas")
            else None
        )

        annotations = self.workspace.setdefault(
            "annotations", []
        )

        if index is None or not (0 <= index < len(annotations)):
            return

        annotations.pop(index)

        self.canvas.selected_annotation_index = None
        self.canvas.set_annotations(annotations)
        self.canvas.viewport().update()

        self.mark_dirty()

    def add_channel(self):
        channels = self.workspace["channels"]

        if len(channels) >= MAX_CHANNELS:
            QMessageBox.information(
                self,
                "Kanály",
                f"Lze vytvořit nejvýše {MAX_CHANNELS} kanálů."
            )
            return

        palette = [
            "#00FF66", "#00D9FF", "#FFCC00", "#FF4D6D",
            "#B388FF", "#FF8C42", "#7CFC00", "#F78CFF",
            "#F0F0F0", "#4DA6FF"
        ]

        number = len(channels) + 1

        channels.append({
            "name": f"CH {number}",
            "color": palette[(number - 1) % len(palette)],
            "transitions": []
        })

        self.selected_index = len(channels) - 1

        self.canvas.set_channels(channels)
        self.canvas.set_selected_index(self.selected_index)

        self.mark_dirty()

    def rename_channel(self):
        channel = self._selected_channel()

        if channel is None:
            return

        name, ok = QInputDialog.getText(
            self,
            "Přejmenovat kanál",
            "Název kanálu:",
            text=channel["name"]
        )

        if ok and name.strip():
            channel["name"] = name.strip()[:80]
            self.canvas.viewport().update()
            self.mark_dirty()

    def change_channel_color(self):
        channel = self._selected_channel()

        if channel is None:
            return

        color = QColorDialog.getColor(
            QColor(channel["color"]),
            self,
            "Barva kanálu"
        )

        if color.isValid():
            channel["color"] = color.name().upper()
            self.canvas.viewport().update()
            self.mark_dirty()

    def remove_channel(self):
        channels = self.workspace["channels"]

        if len(channels) <= 1:
            QMessageBox.information(
                self,
                "Kanály",
                "Pracovní soubor musí obsahovat alespoň jeden kanál."
            )
            return

        name = channels[self.selected_index]["name"]

        answer = QMessageBox.question(
            self,
            "Odebrat kanál",
            f"Opravdu odebrat kanál {name}?"
        )

        if answer == QMessageBox.Yes:
            channels.pop(self.selected_index)

            self.selected_index = min(
                self.selected_index,
                len(channels) - 1
            )

            self.canvas.set_channels(channels)
            self.canvas.set_selected_index(self.selected_index)

            self.mark_dirty()

    def _selected_channel(self):
        channels = self.workspace.get("channels", [])

        if not channels:
            return None

        self.selected_index = max(
            0,
            min(self.selected_index, len(channels) - 1)
        )

        return channels[self.selected_index]

    def new_workspace(self):
        if not self._confirm_discard_changes():
            return

        self.workspace = default_workspace()
        self.current_path = None
        self.selected_index = 0
        self.dirty = False

        self._refresh_canvas()

    def open_dialog(self):
        try:
            ExplorerDialog = _load_file_explorer_dialog_class()
            start_path = (
                os.path.dirname(self.current_path)
                if self.current_path else os.path.expanduser("~")
            )
            dialog = ExplorerDialog(
                mode="open",
                parent=self,
                start_path=start_path
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "VLČR Průzkumník není dostupný",
                str(exc)
            )
            return

        if dialog.exec_() == QDialog.Accepted and dialog.selected_path:
            self.open_path(dialog.selected_path)

    def open_path(self, path):
        if not self._confirm_discard_changes():
            return False

        try:
            metadata, payload = load_workspace(path)

            # Hlídáme i aplikaci; Průzkumník sice předává jen příslušný .vlcr,
            # ale uživatel může soubor otevřít také přímo z dialogu.
            if metadata.get("app") not in (
                "poscilogramar",
                "poscilogramář"
            ):
                raise ValueError(
                    "Soubor nepatří aplikaci Poscilogramář."
                )

            self._loading = True

            self.workspace = {
                "title": (
                    metadata.get("title")
                    or os.path.splitext(os.path.basename(path))[0]
                ),
                "created": metadata.get("created"),
                "time_unit_index": max(
                    0,
                    min(
                        len(TIME_UNITS) - 1,
                        payload.get(
                            "time_unit_index",
                            DEFAULT_TIME_UNIT_INDEX
                        )
                    )
                ),
                "transition_unit": "ns",
                "channels": payload["channels"],
                "annotations": payload.get("annotations", []),
                "annotation_grid_px": payload.get("annotation_grid_px", 19)
            }

            self.current_path = os.path.abspath(path)
            self.selected_index = 0
            self.dirty = False

            self._refresh_canvas()
            self._loading = False

            return True

        except Exception as exc:
            self._loading = False

            QMessageBox.warning(
                self,
                "Soubor nelze otevřít",
                str(exc)
            )

            return False

    def save(self):
        if not self.current_path:
            return self.save_as()

        return self._save_to(self.current_path)

    def save_as(self):
        suggested = (
            os.path.basename(self.current_path)
            if self.current_path else "Oscilogram.vlcr"
        )
        try:
            ExplorerDialog = _load_file_explorer_dialog_class()
            start_path = (
                os.path.dirname(self.current_path)
                if self.current_path else os.path.expanduser("~")
            )
            dialog = ExplorerDialog(
                mode="save",
                parent=self,
                start_path=start_path,
                initial_name=suggested
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "VLČR Průzkumník není dostupný",
                str(exc)
            )
            return False

        if dialog.exec_() != QDialog.Accepted or not dialog.selected_path:
            return False
        return self._save_to(dialog.selected_path)

    def _save_to(self, path):
        try:
            self.workspace["title"] = os.path.splitext(
                os.path.basename(path)
            )[0]

            save_workspace(
                path,
                self.workspace,
                self.workspace["title"]
            )

            self.current_path = os.path.abspath(path)
            self.dirty = False

            self._update_status()
            return True

        except Exception as exc:
            QMessageBox.warning(
                self,
                "Uložení se nezdařilo",
                str(exc)
            )
            return False

    def _confirm_discard_changes(self):
        if not self.dirty:
            return True

        answer = QMessageBox.question(
            self,
            "Neuložené změny",
            "Pracovní soubor obsahuje neuložené změny. Chcete je uložit?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save
        )

        if answer == QMessageBox.Save:
            return self.save()

        return answer == QMessageBox.Discard

    def export_dialog(self, file_type):
        filters = {
            "png": "PNG obrázek (*.png)",
            "pdf": "PDF dokument (*.pdf)",
            "svg": "SVG vektorový obrázek (*.svg)"
        }

        default_name = (
            os.path.splitext(os.path.basename(self.current_path))[0]
            if self.current_path
            else "Oscilogram"
        )

        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Exportovat {file_type.upper()}",
            default_name + "." + file_type,
            filters[file_type]
        )

        if not path:
            return

        if not path.lower().endswith("." + file_type):
            path += "." + file_type

        try:
            self._export(path, file_type)
            self.status.setText(f"Exportováno: {path}")

        except Exception as exc:
            QMessageBox.warning(
                self,
                "Export se nezdařil",
                str(exc)
            )

    def _export(self, path, file_type):
        width, height = self.canvas.export_size()

        if file_type == "png":
            image = QImage(
                width,
                height,
                QImage.Format_ARGB32
            )

            image.fill(QColor("#000000"))

            painter = QPainter(image)
            self.canvas.export_to_painter(painter)
            painter.end()

            if not image.save(path, "PNG"):
                raise IOError("PNG soubor se nepodařilo uložit.")

            return

        if file_type == "svg":
            generator = QSvgGenerator()
            generator.setFileName(path)
            generator.setSize(QSize(width, height))
            generator.setViewBox(QRectF(0, 0, width, height))

            generator.setTitle(
                "Poscilogramář – logické průběhy"
            )

            painter = QPainter(generator)
            self.canvas.export_to_painter(painter)
            painter.end()

            return

        if file_type == "pdf":
            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(path)
            printer.setOrientation(QPrinter.Landscape)
            printer.setFullPage(False)

            painter = QPainter(printer)
            page = printer.pageRect(QPrinter.DevicePixel)

            scale = min(
                page.width() / width,
                page.height() / height
            )

            painter.translate(page.x(), page.y())
            painter.scale(scale, scale)

            self.canvas.export_to_painter(painter)
            painter.end()

            return

        raise ValueError("Nepodporovaný formát exportu.")

    def about(self):
        QMessageBox.about(
            self,
            "O aplikaci",
            f"<b>{APP_NAME}</b><br>"
            f"Verze {APP_VERSION}<br><br>"
            "Ruční editor průběhů až 10 kanálů.<br>"
            "Pracovní soubory: kompatibilní kontejner .vlcr."
        )

    def closeEvent(self, event):
        if self._confirm_discard_changes():
            event.accept()
        else:
            event.ignore()


def main():
    # CI a balíčkovací skripty mohou zjistit verzi bez otevření GUI.
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(APP_VERSION)
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("VLCR")

    app.setDesktopFileName("vlcr-poscilogramar")
    app.setFont(QFont("Tahoma", 9))

    if os.path.isfile(LOGO_PATH):
        app.setWindowIcon(QIcon(LOGO_PATH))

    open_path = None

    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".vlcr"):
        open_path = sys.argv[1]

    window = PoscilogramarWindow(open_path)
    window.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
