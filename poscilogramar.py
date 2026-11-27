"""Poscilogramář – ruční editor průběhů digitálního logického analyzátoru.

Vyžaduje PyQt5. Pracovní soubory ukládá do kompatibilního kontejneru .vlcr.
"""
from __future__ import annotations

import os
import sys
import math
from bisect import bisect_left
from copy import deepcopy

from PyQt5.QtCore import Qt, QPoint, QPointF, QSize, QRectF
from PyQt5.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QImage,
)
from PyQt5.QtWidgets import (
    QApplication, QAbstractScrollArea, QAction, QButtonGroup, QColorDialog,
    QFrame, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QShortcut, QVBoxLayout, QWidget, QInputDialog, QMenu,
)
from PyQt5.QtPrintSupport import QPrinter
from PyQt5.QtSvg import QSvgGenerator

from vlcr_format import (
    DEFAULT_TIME_UNIT_INDEX, MAX_CHANNELS, MAX_TIMELINE_NS, MIN_TIMELINE_DIVISIONS,
    TIME_UNITS_NS, default_workspace, load_workspace, save_workspace,
)

APP_NAME = "Poscilogramář"
APP_VERSION = "1.1.0"
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
        self._pan_origin = None
        self._pan_scroll_origin = 0
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

    def set_tool(self, tool):
        self.tool = tool
        if tool == "pan":
            self.viewport().setCursor(Qt.OpenHandCursor)
        else:
            self.viewport().setCursor(Qt.CrossCursor if tool == "paint" else Qt.ArrowCursor)

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
        old_time_ns = ((old_scroll + anchor_x - self.GUTTER) / self.PX_PER_DIV) * self._unit_ns()

        self.time_unit_index = new_index
        self._update_scrollbars()

        new_scroll = (old_time_ns / self._unit_ns()) * self.PX_PER_DIV - (anchor_x - self.GUTTER)
        self.horizontalScrollBar().setValue(max(0, int(round(new_scroll))))
        self.viewport().update()
        return True

    def _update_scrollbars(self):
        plot_width = self._total_divisions() * self.PX_PER_DIV
        available_width = max(1, self.viewport().width() - self.GUTTER)
        self.horizontalScrollBar().setPageStep(available_width)
        self.horizontalScrollBar().setRange(0, max(0, plot_width - available_width))
        content_height = self.RULER_H + len(self.channels) * self.LANE_H + 12
        self.verticalScrollBar().setPageStep(max(1, self.viewport().height()))
        self.verticalScrollBar().setRange(0, max(0, content_height - self.viewport().height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scrollbars()

    def zoom(self, direction, anchor_x=None):
        # direction -1 = detailnější časová stupnice, +1 = širší časový rozsah.
        new_index = max(0, min(len(TIME_UNITS) - 1, self.time_unit_index + direction))
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
        tick = max(0, min(self._total_divisions(), int(round(absolute_x / self.PX_PER_DIV))))
        return min(MAX_TIMELINE_NS, int(round(tick * self._unit_ns())))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        if self.tool == "pan":
            self._pan_origin = event.pos()
            self._pan_scroll_origin = self.horizontalScrollBar().value()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        index = self._channel_at_y(event.pos().y())
        if index is None:
            return
        self.selected_index = index
        self.viewport().update()
        self.channelSelected.emit(index)
        if self.tool == "paint" and event.pos().x() >= self.GUTTER:
            self._toggle_transition(index, self._tick_at_x(event.pos().x()))
            self.waveformChanged.emit()

    def mouseMoveEvent(self, event):
        if self.tool == "pan" and self._pan_origin is not None and (event.buttons() & Qt.LeftButton):
            delta = event.pos().x() - self._pan_origin.x()
            bar = self.horizontalScrollBar()
            bar.setValue(self._pan_scroll_origin - delta)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pan_origin is not None:
            self._pan_origin = None
            self.viewport().setCursor(Qt.OpenHandCursor)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
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
                changed = self.zoom(-1 if amount > 0 else 1, anchor_x=event.pos().x())
                if changed:
                    self.zoomRequested.emit(self.time_unit_index)
                event.accept()
                return

        # Shift + kolečko posouvá vodorovně časovou osu (jako trackpad).
        if modifiers & Qt.ShiftModifier:
            amount = delta.x() or delta.y()
            pixel_amount = event.pixelDelta().x() or event.pixelDelta().y()
            if pixel_amount:
                scroll_delta = -pixel_amount
            elif amount:
                scroll_delta = -int((amount / 120.0) * self.PX_PER_DIV * 3)
            else:
                scroll_delta = 0
            if scroll_delta:
                bar = self.horizontalScrollBar()
                bar.setValue(bar.value() + scroll_delta)
            event.accept()
            return

        super().wheelEvent(event)

    def _toggle_transition(self, channel_index, time_ns):
        transitions = self.channels[channel_index].setdefault("transitions", [])
        time_ns = max(0, min(MAX_TIMELINE_NS, int(time_ns)))
        position = bisect_left(transitions, time_ns)
        if position < len(transitions) and transitions[position] == time_ns:
            transitions.pop(position)
        else:
            transitions.insert(position, time_ns)
        self._update_scrollbars()
        self.viewport().update()

    def draw_scene(self, painter, width, height, horizontal_offset=0, vertical_offset=0):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(0, 0, width, height, QColor("#000000"))

        # Pevný levý sloupec s názvy kanálů.
        painter.fillRect(0, 0, self.GUTTER, height, QColor("#080808"))
        painter.fillRect(0, 0, self.GUTTER, self.RULER_H, QColor("#1B1B1B"))
        painter.setPen(QPen(QColor("#555555"), 1))
        painter.drawLine(self.GUTTER - 1, 0, self.GUTTER - 1, height)
        painter.drawLine(0, self.RULER_H - 1, width, self.RULER_H - 1)

        font = QFont("Tahoma", 8)
        painter.setFont(font)
        painter.setPen(QColor("#C8C8C8"))
        painter.drawText(10, 18, "KANÁL")
        painter.setPen(QColor("#A0A0A0"))
        painter.drawText(self.GUTTER + 8, 18, f"Čas / dílek: {TIME_UNIT_LABELS[self.time_unit_index]}")

        # Svislá mřížka představuje aktuální časovou jednotku, nikoli délku
        # uloženého signálu. Hrany jsou vždy vykresleny z jejich absolutního času.
        total_divisions = self._total_divisions()
        first_tick = max(0, int(horizontal_offset // self.PX_PER_DIV) - 1)
        last_tick = min(total_divisions, int((horizontal_offset + max(0, width - self.GUTTER)) // self.PX_PER_DIV) + 2)
        unit_ns = self._unit_ns()
        # Při exportu extrémně dlouhé osy se čáry mřížky vhodně zředí;
        # kreslicí plocha na obrazovce zůstává vykreslována po jednotlivých dílcích.
        grid_stride = max(1, int(math.ceil(max(1, last_tick - first_tick + 1) / 50_000)))
        for tick in range(first_tick, last_tick + 1, grid_stride):
            x = self.GUTTER + tick * self.PX_PER_DIV - horizontal_offset
            major = (tick % 10 == 0)
            painter.setPen(QPen(QColor("#3A3A3A" if major else "#262626"), 1))
            painter.drawLine(int(x), self.RULER_H, int(x), height)
            if major and tick > 0:
                absolute_seconds = (tick * unit_ns) / 1_000_000_000
                unit_label = self._format_time(absolute_seconds)
                painter.setPen(QColor("#9A9A9A"))
                painter.drawText(int(x + 4), 18, unit_label)

        # Signály a popisky kanálů. Časy hran se nikdy nepřepočítávají
        # destruktivně při zoomu – mění se pouze jejich vykreslovaná poloha.
        for index, channel in enumerate(self.channels):
            row_top = self.RULER_H + index * self.LANE_H - vertical_offset
            if row_top + self.LANE_H < self.RULER_H or row_top > height:
                continue
            if index == self.selected_index:
                painter.fillRect(0, int(row_top), self.GUTTER - 1, self.LANE_H, QColor("#202020"))
            color = QColor(channel.get("color", "#00FF66"))
            painter.fillRect(10, int(row_top + 29), 9, 9, color)
            painter.setPen(QColor("#E0E0E0" if index == self.selected_index else "#B0B0B0"))
            painter.drawText(26, int(row_top + 37), str(channel.get("name", f"CH {index + 1}"))[:17])
            painter.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))

            high_y = row_top + 19
            low_y = row_top + 52
            current_y = low_y  # začátek každého kanálu je logická 0
            previous_x = self.GUTTER - horizontal_offset
            for transition_ns in channel.get("transitions", []):
                x = self.GUTTER + (int(transition_ns) / unit_ns) * self.PX_PER_DIV - horizontal_offset
                painter.drawLine(QPointF(previous_x, current_y), QPointF(x, current_y))
                next_y = high_y if current_y == low_y else low_y
                painter.drawLine(QPointF(x, current_y), QPointF(x, next_y))
                current_y = next_y
                previous_x = x
            end_x = self.GUTTER + total_divisions * self.PX_PER_DIV - horizontal_offset
            painter.drawLine(QPointF(previous_x, current_y), QPointF(end_x, current_y))

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
            painter, self.viewport().width(), self.viewport().height(),
            self.horizontalScrollBar().value(), self.verticalScrollBar().value(),
        )
        painter.end()

    def export_size(self):
        # Celý časový rozsah se exportuje, ale extrémně široká časová osa se
        # přiměřeně zmenší, aby nevznikaly mnohagigabajtové bitmapy.
        natural_width = self.GUTTER + self._total_divisions() * self.PX_PER_DIV
        natural_height = self.RULER_H + len(self.channels) * self.LANE_H + 12
        scale = min(1.0, 30_000 / max(1, natural_width), 12_000 / max(1, natural_height))
        return max(1, int(natural_width * scale)), max(1, int(natural_height * scale))

    def export_to_painter(self, painter):
        out_width, out_height = self.export_size()
        natural_width = self.GUTTER + self._total_divisions() * self.PX_PER_DIV
        natural_height = self.RULER_H + len(self.channels) * self.LANE_H + 12
        painter.save()
        painter.scale(out_width / natural_width, out_height / natural_height)
        self.draw_scene(painter, natural_width, natural_height, 0, 0)
        painter.restore()

    from PyQt5.QtCore import pyqtSignal as _pyqtSignal
    channelSelected = _pyqtSignal(int)
    waveformChanged = _pyqtSignal()
    renameRequested = _pyqtSignal(int)
    zoomRequested = _pyqtSignal(int)


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
        root.setStyleSheet(f"background: {WIN_BG}; color: {WIN_TEXT};")
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
        self.sidebar.setStyleSheet("QFrame#sidebar { background: #C0C0C0; border-right: 2px solid #808080; }")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(9, 9, 9, 9)
        side.setSpacing(6)
        body.addWidget(self.sidebar)


        side.addWidget(self._sunken_separator())

        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons = {}
        self._section(side, "Nástroje")
        self._tool_button(side, "✎  Malovat průběh", "paint", "Kliknutím na svislou mřížku přepnete signál.")
        self._tool_button(side, "✋  Posouvat", "pan", "Tažením myší posunete časovou osu.")
        self._tool_button(side, "▣  Označit kanál", "select", "Kliknutím na řádek vyberete kanál.")
        side.addWidget(self._sunken_separator())

        self._section(side, "Kanály")
        self._add_button(side, "+  Přidat kanál", self.add_channel)
        self._add_button(side, "Přejmenovat kanál", self.rename_channel)
        self._add_button(side, "Změnit barvu kanálu", self.change_channel_color)
        self._add_button(side, "Odebrat kanál", self.remove_channel)
        side.addWidget(self._sunken_separator())

        self._section(side, "Pracovní soubor")
        self._add_button(side, "Otevřít .vlcr…", self.open_dialog)
        self._add_button(side, "Uložit", self.save)
        self._add_button(side, "Uložit jako…", self.save_as)
        side.addWidget(self._sunken_separator())

        self._section(side, "Exportovat")
        self._add_button(side, "Exportovat PNG…", lambda: self.export_dialog("png"))
        self._add_button(side, "Exportovat PDF…", lambda: self.export_dialog("pdf"))
        self._add_button(side, "Exportovat SVG…", lambda: self.export_dialog("svg"))
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
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_time(1))
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_time(-1))
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

        self.canvas.channelSelected.connect(self.on_channel_selected)
        self.canvas.waveformChanged.connect(self.mark_dirty)
        self.canvas.renameRequested.connect(lambda _index: self.rename_channel())
        self.canvas.zoomRequested.connect(self._on_zoom_changed)

        self.status = QLabel("Připraveno")
        self.status.setFont(QFont("Tahoma", 8))
        self.status.setFixedHeight(23)
        self.status.setMargin(5)
        self.status.setStyleSheet("background: #C0C0C0; border-top: 1px solid #FFFFFF; color: #222;")
        outer.addWidget(self.status)

        self.setStyleSheet(self.styleSheet() + " QMenuBar { background: #C0C0C0; } QMenuBar::item { padding: 4px 8px; } QMenu { background: #C0C0C0; border: 1px solid #404040; }")

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

    def _tool_button(self, layout, text, tool, tooltip):
        button = QPushButton(text)
        button.setCheckable(True)
        button.setToolTip(tooltip)
        button.setFont(QFont("Tahoma", 8))
        button.setStyleSheet(retro_button_style())
        self.tool_group.addButton(button)
        self.tool_buttons[tool] = button
        button.clicked.connect(lambda _checked=False, t=tool: self.set_tool(t))
        layout.addWidget(button)
        if tool == "paint":
            button.setChecked(True)
        return button

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("Soubor")
        self._menu_action(file_menu, "Nový", self.new_workspace, "Ctrl+N")
        self._menu_action(file_menu, "Otevřít…", self.open_dialog, "Ctrl+O")
        file_menu.addSeparator()
        self._menu_action(file_menu, "Uložit", self.save, "Ctrl+S")
        self._menu_action(file_menu, "Uložit jako…", self.save_as, "Ctrl+Shift+S")
        export_menu = file_menu.addMenu("Exportovat")
        self._menu_action(export_menu, "PNG…", lambda: self.export_dialog("png"))
        self._menu_action(export_menu, "PDF…", lambda: self.export_dialog("pdf"))
        self._menu_action(export_menu, "SVG…", lambda: self.export_dialog("svg"))
        file_menu.addSeparator()
        self._menu_action(file_menu, "Ukončit", self.close, "Alt+F4")

        view_menu = self.menuBar().addMenu("Zobrazení")
        self._menu_action(view_menu, "Přiblížit časovou osu", lambda: self.zoom_time(-1))
        self._menu_action(view_menu, "Oddálit časovou osu", lambda: self.zoom_time(1))

        channel_menu = self.menuBar().addMenu("Kanál")
        self._menu_action(channel_menu, "Přidat kanál", self.add_channel)
        self._menu_action(channel_menu, "Přejmenovat kanál", self.rename_channel)
        self._menu_action(channel_menu, "Změnit barvu…", self.change_channel_color)
        self._menu_action(channel_menu, "Odebrat kanál", self.remove_channel)

        help_menu = self.menuBar().addMenu("Nápověda")
        self._menu_action(help_menu, "O aplikaci", self.about)

    def _menu_action(self, menu, text, callback, shortcut=None):
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(callback)
        menu.addAction(action)
        return action

    def _build_shortcuts(self):
        for sequence, direction in (("Ctrl++", -1), ("Ctrl+=", -1), ("Ctrl+-", 1)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(lambda d=direction: self.zoom_time(d))

    def _refresh_canvas(self):
        self.canvas.set_channels(self.workspace["channels"])
        self.canvas.set_selected_index(self.selected_index)
        self.canvas.time_unit_index = max(0, min(len(TIME_UNITS) - 1, self.workspace.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX)))
        self.canvas._update_scrollbars()
        self.canvas.viewport().update()
        self.time_unit_label.setText(TIME_UNIT_LABELS[self.workspace.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX)] + " / dílek")
        self._update_status()

    def _update_status(self):
        unit = TIME_UNIT_LABELS[self.workspace.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX)]
        count = len(self.workspace.get("channels", []))
        file_name = os.path.basename(self.current_path) if self.current_path else "Nový pracovní soubor"
        dirty_mark = " • změny nejsou uloženy" if self.dirty else ""
        self.status.setText(f"{file_name}    |    {count}/{MAX_CHANNELS} kanálů    |    {unit} / dílek{dirty_mark}")
        self.setWindowTitle(f"{APP_NAME} — {file_name}{' *' if self.dirty else ''}")

    def set_tool(self, tool):
        self.canvas.set_tool(tool)
        if tool in self.tool_buttons:
            self.tool_buttons[tool].setChecked(True)
        tips = {"paint": "Malování průběhu: kliknutím na mřížku přepnete úroveň od zvoleného času doprava.",
                "pan": "Posouvání: táhněte myší nebo použijte Shift + kolečko.",
                "select": "Výběr kanálu: klikněte na popisek nebo průběh kanálu."}
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
        current = self.workspace.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX)
        new_index = max(0, min(len(TIME_UNITS) - 1, current + direction))
        if new_index != current and self.canvas.set_time_unit_index(new_index):
            self.workspace["time_unit_index"] = new_index
            self.time_unit_label.setText(TIME_UNIT_LABELS[new_index] + " / dílek")
            self.mark_dirty()

    def _on_zoom_changed(self, index):
        self.workspace["time_unit_index"] = index
        self.time_unit_label.setText(TIME_UNIT_LABELS[index] + " / dílek")
        self.mark_dirty()

    def add_channel(self):
        channels = self.workspace["channels"]
        if len(channels) >= MAX_CHANNELS:
            QMessageBox.information(self, "Kanály", f"Lze vytvořit nejvýše {MAX_CHANNELS} kanálů.")
            return
        palette = ["#00FF66", "#00D9FF", "#FFCC00", "#FF4D6D", "#B388FF",
                   "#FF8C42", "#7CFC00", "#F78CFF", "#F0F0F0", "#4DA6FF"]
        number = len(channels) + 1
        channels.append({"name": f"CH {number}", "color": palette[(number - 1) % len(palette)], "transitions": []})
        self.selected_index = len(channels) - 1
        self.canvas.set_channels(channels)
        self.canvas.set_selected_index(self.selected_index)
        self.mark_dirty()

    def rename_channel(self):
        channel = self._selected_channel()
        if channel is None:
            return
        name, ok = QInputDialog.getText(self, "Přejmenovat kanál", "Název kanálu:", text=channel["name"])
        if ok and name.strip():
            channel["name"] = name.strip()[:80]
            self.canvas.viewport().update()
            self.mark_dirty()

    def change_channel_color(self):
        channel = self._selected_channel()
        if channel is None:
            return
        color = QColorDialog.getColor(QColor(channel["color"]), self, "Barva kanálu")
        if color.isValid():
            channel["color"] = color.name().upper()
            self.canvas.viewport().update()
            self.mark_dirty()

    def remove_channel(self):
        channels = self.workspace["channels"]
        if len(channels) <= 1:
            QMessageBox.information(self, "Kanály", "Pracovní soubor musí obsahovat alespoň jeden kanál.")
            return
        name = channels[self.selected_index]["name"]
        answer = QMessageBox.question(self, "Odebrat kanál", f"Opravdu odebrat kanál {name}?")
        if answer == QMessageBox.Yes:
            channels.pop(self.selected_index)
            self.selected_index = min(self.selected_index, len(channels) - 1)
            self.canvas.set_channels(channels)
            self.canvas.set_selected_index(self.selected_index)
            self.mark_dirty()

    def _selected_channel(self):
        channels = self.workspace.get("channels", [])
        if not channels:
            return None
        self.selected_index = max(0, min(self.selected_index, len(channels) - 1))
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
        path, _ = QFileDialog.getOpenFileName(self, "Otevřít pracovní soubor", "", "VLČR pracovní soubory (*.vlcr)")
        if path:
            self.open_path(path)

    def open_path(self, path):
        if not self._confirm_discard_changes():
            return False
        try:
            metadata, payload = load_workspace(path)
            # Hlídáme i aplikaci; Průzkumník sice předává jen příslušný .vlcr,
            # ale uživatel může soubor otevřít také přímo z dialogu.
            if metadata.get("app") not in ("poscilogramar", "poscilogramář"):
                raise ValueError("Soubor nepatří aplikaci Poscilogramář.")
            self._loading = True
            self.workspace = {
                "title": metadata.get("title") or os.path.splitext(os.path.basename(path))[0],
                "created": metadata.get("created"),
                "time_unit_index": max(0, min(len(TIME_UNITS) - 1, payload.get("time_unit_index", DEFAULT_TIME_UNIT_INDEX))),
                "transition_unit": "ns",
                "channels": payload["channels"],
            }
            self.current_path = os.path.abspath(path)
            self.selected_index = 0
            self.dirty = False
            self._refresh_canvas()
            self._loading = False
            return True
        except Exception as exc:
            self._loading = False
            QMessageBox.warning(self, "Soubor nelze otevřít", str(exc))
            return False

    def save(self):
        if not self.current_path:
            return self.save_as()
        return self._save_to(self.current_path)

    def save_as(self):
        suggested = "Oscilogram.vlcr"
        if self.current_path:
            suggested = os.path.basename(self.current_path)
        path, _ = QFileDialog.getSaveFileName(self, "Uložit pracovní soubor", suggested, "VLČR pracovní soubory (*.vlcr)")
        if not path:
            return False
        if not path.lower().endswith(".vlcr"):
            path += ".vlcr"
        return self._save_to(path)

    def _save_to(self, path):
        try:
            self.workspace["title"] = os.path.splitext(os.path.basename(path))[0]
            save_workspace(path, self.workspace, self.workspace["title"])
            self.current_path = os.path.abspath(path)
            self.dirty = False
            self._update_status()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Uložení se nezdařilo", str(exc))
            return False

    def _confirm_discard_changes(self):
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Neuložené změny", "Pracovní soubor obsahuje neuložené změny. Chcete je uložit?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save,
        )
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def export_dialog(self, file_type):
        filters = {"png": "PNG obrázek (*.png)", "pdf": "PDF dokument (*.pdf)", "svg": "SVG vektorový obrázek (*.svg)"}
        default_name = os.path.splitext(os.path.basename(self.current_path))[0] if self.current_path else "Oscilogram"
        path, _ = QFileDialog.getSaveFileName(self, f"Exportovat {file_type.upper()}", default_name + "." + file_type, filters[file_type])
        if not path:
            return
        if not path.lower().endswith("." + file_type):
            path += "." + file_type
        try:
            self._export(path, file_type)
            self.status.setText(f"Exportováno: {path}")
        except Exception as exc:
            QMessageBox.warning(self, "Export se nezdařil", str(exc))

    def _export(self, path, file_type):
        width, height = self.canvas.export_size()
        if file_type == "png":
            image = QImage(width, height, QImage.Format_ARGB32)
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
            generator.setTitle("Poscilogramář – logické průběhy")
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
            scale = min(page.width() / width, page.height() / height)
            painter.translate(page.x(), page.y())
            painter.scale(scale, scale)
            self.canvas.export_to_painter(painter)
            painter.end()
            return
        raise ValueError("Nepodporovaný formát exportu.")

    def about(self):
        QMessageBox.about(self, "O aplikaci", f"<b>{APP_NAME}</b><br>Verze {APP_VERSION}<br><br>Ruční editor průběhů až 10 kanálů.<br>Pracovní soubory: kompatibilní kontejner .vlcr.")

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
    # Název musí odpovídat souboru vlcr-poscilogramar.desktop.
    # Linux pak správně spáruje okno/panelovou ikonu s desktopovým záznamem.
    app.setDesktopFileName("vlcr-poscilogramar")
    app.setFont(QFont("Tahoma", 9))
    if os.path.isfile(LOGO_PATH):
        app_icon = QIcon(LOGO_PATH)
        app.setWindowIcon(app_icon)
    open_path = None
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".vlcr"):
        open_path = sys.argv[1]
    window = PoscilogramarWindow(open_path)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())