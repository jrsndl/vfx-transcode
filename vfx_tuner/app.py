"""vfx-transcode-tuner: tune presets visually, and convert.

The window edits a core.TunerState and shows one of three views with pan / zoom and an optional
compare image: Image and Slate run `vfx-transcode ... -preview FILE.png` and show FILE.png or
FILE_slate.png; Filmstrip runs `vfx-transcode ... -of FILE.jpg` (with the movie when the tiles come
from it) and shows the filmstrip. A refresh runs the command of the view on screen.

The input is any frame of a sequence or a movie file. The timeline under the image picks the
previewed frame. Under it, the Super-presets tab saves and loads the settings of several areas
at once and has the Process button, which runs the whole conversion; the Log tab shows what
vfx-transcode prints.
"""

import argparse
import html
from collections import OrderedDict
import itertools
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QImageReader, QKeySequence, QPainter, QPalette, QPen, QPixmap,
                           QShortcut, QTransform)
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QApplication, QFrame, QSizePolicy, QStackedWidget, QToolButton, QInputDialog, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsPixmapItem,
                               QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QStyle, QStyleOptionSlider,
                               QTableWidget, QTableWidgetItem,
                               QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from vfx_transcode import presets_store
from vfx_transcode import studio
from vfx_transcode import super_presets as sp
from vfx_transcode.burnin import BURN_PLACES, MARGIN_SIDES, PLACE_NAMES
from vfx_transcode.channels import parts as exr_parts
from vfx_transcode.errors import TranscodeError
from vfx_transcode.movie import DEFAULT_FIRST_FRAME, MOVIE_EXTS
from vfx_transcode.presets import DEFAULT_PRESETS_FILE, load_presets
from vfx_transcode.tools import app_dir, resolve_path, split_args

from . import core
from . import tooltips

REFRESH_DELAY_MS = 500
STEP_DELAY_MS = 250                   # timeline keyboard / page steps: refresh after a pause
SAME_FPS = "same as input"
PIXEL_ASPECTS = ("auto", "1", "2", "1.8", "1.5", "1.33", "1.3", "1.25", "0.9")
FRAME_RATES = ("auto", "23.976", "24", "25", "29.97", "30", "47.952", "48", "50", "59.94", "60")
INPUT_FILTER = ("Images and movies (*.exr *.png *.jpg *.jpeg *.tif *.tiff *.dpx *.bmp {});;"
                "All files (*)".format(" ".join("*" + e for e in MOVIE_EXTS)))


# --- small widgets --------------------------------------------------------------------------

class ColorButton(QPushButton):
    """Shows a colour; click to pick. value: "#rrggbb" or a colour name."""
    changed = Signal()

    def __init__(self, value="white", parent=None, compact=False):
        super().__init__(parent)
        self.compact = compact                    # a small swatch; the value in the tooltip
        self.setFixedWidth(28 if compact else 70)
        self._value = ""
        self.set_value(value)
        self.clicked.connect(self._pick)

    def value(self):
        return self._value

    def set_value(self, value):
        self._value = value or "white"
        colour = QColor(self._value)
        text = "black" if colour.lightness() > 128 else "white"
        self.setStyleSheet("background:{}; color:{};".format(colour.name(), text))
        self.setText("" if self.compact else self._value)
        self.setToolTip(self._value)

    def _pick(self):
        colour = QColorDialog.getColor(QColor(self._value), self, "Colour")
        if colour.isValid():
            self.set_value(colour.name())
            self.changed.emit()


def opacity_spin(value=1.0, compact=False):
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 1.0)
    spin.setSingleStep(0.05)
    spin.setDecimals(2)
    spin.setValue(value)
    if compact:                                   # no arrows (wheel / keys still step)
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setFixedWidth(40)
        spin.setToolTip("opacity 0-1")
    return spin


def compact_combo(box, chars=10):
    """A dropdown as narrow as `chars` characters, however long its entries (the list that
    opens still shows them in full)."""
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(chars)
    box.view().setTextElideMode(Qt.TextElideMode.ElideNone)
    return box


# compact panels: a control whose value is always short gets a fixed width of that many
# characters; neighbours in a column get the same width, so their right edges line up
COMPACT_WIDTHS = OrderedDict([
    # numbers, rates, sizes, short choices
    (10, ("in_fps", "in_par", "in_first_frame", "th_frame", "th_width",
          "th_height", "th_format", "th_quality", "th_compression", "th_bits", "fs_tiles",
          "fs_width", "fs_height", "fs_columns", "fs_rows", "fs_quality", "im_threads",
          "sl_title_size", "sl_subtitle_size", "sl_size", "sl_scale", "sl_logo_size",
          "sl_logo_x", "sl_logo_y", "bu_size", "sq_ext", "en_ext")),
    # short words: sources, alignment, pixel formats, rates ("same as input")
    (16, ("sl_valign", "sl_thumbnail", "fs_source", "en_alpha", "par_out", "out_fps")),
    # the timecode and reel name block (timecode 11 characters, reel names up to ~20)
    (22, ("md_tc_source", "md_timecode", "md_reel_source", "md_reel", "ch_deep_zrange")),
    # choices with a description; the open list shows them in full
    (34, ("in_input_type", "in_missing", "sp_type", "in_encode", "seq_preset", "alpha_mode",
          "im_format", "im_location", "sl_logo_position")),
])
COMPACT_PADDING = ((QComboBox, 34), (QAbstractSpinBox, 30), (QAbstractButton, 20), (QLineEdit, 14))


def set_chars(widget, chars):
    """Fix the widget's width to about `chars` characters of its font (plus its arrows and
    frame). A combo box's open list stays as wide as its longest entry."""
    pad = next((p for cls, p in COMPACT_PADDING if isinstance(widget, cls)), 14)
    try:
        width = int(widget.fontMetrics().averageCharWidth() * chars + pad)
    except TypeError:                     # no real font (tests)
        return
    widget.setFixedWidth(width)
    if isinstance(widget, QComboBox):
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        widget.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        fit_popup(widget)


def fit_popup(box):
    """Let a narrow dropdown's open list be as wide as its longest entry."""
    if box.count():
        box.view().setMinimumWidth(box.view().sizeHintForColumn(0) + 24)


def combo(items, editable=False, current=""):
    box = QComboBox()
    box.setEditable(editable)
    box.addItems(list(items))
    if current:
        set_combo(box, current)
    return box


def set_combo(box, text):
    index = box.findText(text)
    if index >= 0:
        box.setCurrentIndex(index)
    elif box.isEditable():
        box.setEditText(text)


def browse_row(edit, title, folder=False, filters="All files (*)"):
    row = QHBoxLayout()
    row.addWidget(edit)
    button = QPushButton("...")
    button.setToolTip('Browse for it')
    button.setFixedWidth(30)

    def pick():
        text = edit.text().strip()
        start = text if text and os.path.isdir(text) else (os.path.dirname(text) if text else "")
        if folder:
            path = QFileDialog.getExistingDirectory(None, title, start)
        else:
            path, _ = QFileDialog.getOpenFileName(None, title, start, filters)
        if path:
            edit.setText(path)
            edit.editingFinished.emit()

    button.clicked.connect(pick)
    row.addWidget(button)
    return row


PICK_PRESET = "(pick a preset to load)"
ROW_BUTTON_TIPS = {"Add": "Add a row (after the selected one)", "Duplicate": "Copy the selected row",
                   "Remove": "Remove the selected row", "Up": "Move the selected row up",
                   "Down": "Move the selected row down"}
# Presets tab: the full command coloured by the tab each option comes from (readable on dark)
TAB_COLOURS = OrderedDict([
    ("Input", "#7fb2ff"), ("Color", "#ff9d5c"), ("Tokens", "#c79bff"), ("Intermediate", "#9aa7b8"),
    ("Multichannel", "#5fd7c4"), ("Output", "#ffd75f"), ("Destination", "#87d75f"),
    ("Metadata", "#d7afd7"), ("Transform Sequence", "#5fafff"), ("Transform", "#00d7ff"),
    ("Burn-in", "#ff6f91"), ("Slate", "#e0c080"), ("Filmstrip", "#afd700"),
    ("Thumbnail", "#ffaf87")])


class SavePresetDialog(QDialog):
    """Name (an existing one replaces it), sub-preset (oiiotool transforms) and notes."""

    def __init__(self, parent, title, names, name="", notes="", subs_for=None, sub="",
                 notes_for=None, where=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.subs_for, self.notes_for = subs_for, notes_for
        form = QFormLayout(self)
        self.name = combo(names, editable=True)
        self.name.setToolTip('A new name, or pick an existing preset to replace it')
        self.name.setEditText(name)
        self.name.lineEdit().setPlaceholderText("a new name, or pick one to replace it")
        form.addRow("Name", self.name)
        self.sub = None
        if subs_for is not None:
            self.sub = combo([], editable=True)
            self.sub.setToolTip('The sub-preset to save into the transform preset: a new one, or pick one to replace it')
            form.addRow("Sub-preset", self.sub)
            self._fill_subs(name)
            self.sub.setEditText(sub or "default")
            hint = QLabel("A sub-preset of the same name is replaced and keeps its conditions; "
                          "a new one gets the conditions of the loaded sub-preset.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #8a8a8a;")
            form.addRow("", hint)
            self.name.currentTextChanged.connect(self._fill_subs)
        self.notes = QLineEdit(notes)
        self.notes.setToolTip('Notes saved with the preset: shown in --help, the tuner and the process app')
        self.notes.setPlaceholderText("shown in --help and the tuner")
        form.addRow("Notes", self.notes)
        if where:
            label = QLabel("Saved into " + where)
            label.setWordWrap(True)
            label.setStyleSheet("color: #8a8a8a;")
            form.addRow("", label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.name.currentTextChanged.connect(self._name_changed)
        self.setMinimumWidth(480)

    def _fill_subs(self, name):
        if self.sub is None:
            return
        text = self.sub.currentText()
        self.sub.clear()
        self.sub.addItems(self.subs_for(name.strip()))
        self.sub.setEditText(text)

    def _name_changed(self, name):
        if self.notes_for is not None:
            notes = self.notes_for(name.strip(), self.values()[1])
            if notes:
                self.notes.setText(notes)

    def values(self):
        return (self.name.currentText().strip(),
                self.sub.currentText().strip() if self.sub is not None else None,
                self.notes.text().strip())


class TokenTable(QWidget):
    """Smart token definitions: one row each (name, conditions, source, regex, repl, mode,
    case, notes), processed top to bottom; Add, Duplicate, Remove, Up, Down."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, len(core.TOKEN_COLUMNS))
        self.table.setToolTip('Smart token definitions, processed top to bottom; double-click a cell to edit it (hover the column names for what each holds)')
        self.table.setHorizontalHeaderLabels(list(core.TOKEN_COLUMNS))
        header = self.table.horizontalHeader()
        for column, width in enumerate((90, 150, 110, 150, 80, 80, 70)):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(lambda _item: self.changed.emit())
        tips = {"name": "the keyword it sets: shot -> {shot}",
                "conditions": "all must hold, e.g. {namesplit-2} == plate; {version_string} != "
                              "(empty right side = empty text); separate with ;",
                "source": "text to work on, keywords allowed; empty = {clean_name}",
                "regex": "optional; without one the whole source is the value",
                "repl": "extract: what the match becomes (empty = group 1); replace: the "
                        "replacement for every match",
                "mode": "extract: the first match; replace: replace all matches",
                "case": "as_is, lower, upper, title", "notes": "free text"}
        for column, key in enumerate(core.TOKEN_COLUMNS):
            self.table.horizontalHeaderItem(column).setToolTip(tips[key])
        buttons = QVBoxLayout()
        for text, slot in (("Add", self._add), ("Duplicate", self._duplicate),
                           ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1))):
            button = QPushButton(text)
            button.setToolTip(ROW_BUTTON_TIPS.get(text, ""))
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)

    # rows <-> table

    def rows(self):
        """The definitions as dicts (default fields left out, like presets.json). Raises
        ValueError for a conditions cell that can't be read."""
        out = []
        for row in range(self.table.rowCount()):
            cell = {key: self._text(row, column) for column, key in enumerate(core.TOKEN_COLUMNS)}
            item = OrderedDict([("name", cell["name"].strip())])
            conditions = core.parse_conditions(cell["conditions"])
            if conditions:
                item["conditions"] = conditions
            if cell["source"].strip() and cell["source"].strip() != "{clean_name}":
                item["source"] = cell["source"]
            for key, default in (("regex", ""), ("repl", ""), ("mode", "extract"),
                                 ("case", "as_is"), ("notes", "")):
                if cell[key] and cell[key] != default:
                    item[key] = cell[key]
            out.append(item)
        return out

    def set_rows(self, rows):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row in rows:
            self._append(row)
        self.table.blockSignals(False)

    def _text(self, row, column):
        widget = self.table.cellWidget(row, column)
        if isinstance(widget, QComboBox):
            return widget.currentText()
        item = self.table.item(row, column)
        return item.text() if item else ""

    def _append(self, data, at=None):
        row = self.table.rowCount() if at is None else at
        self.table.insertRow(row)
        for column, key in enumerate(core.TOKEN_COLUMNS):
            if key in ("mode", "case"):
                box = combo(core.TOKEN_MODES if key == "mode" else core.TOKEN_CASES)
                box.setToolTip('extract: the first match (or group 1); replace: replace every match' if key == "mode" else 'Change the case of the result: as_is, lower, upper or title')
                set_combo(box, data.get(key) or ("extract" if key == "mode" else "as_is"))
                box.currentTextChanged.connect(lambda _t: self.changed.emit())
                self.table.setCellWidget(row, column, box)
                continue
            if key == "conditions":
                text = core.conditions_text(data.get("conditions"))
            elif key == "source":
                text = data.get("source", "")
            else:
                text = str(data.get(key, ""))
            self.table.setItem(row, column, QTableWidgetItem(text))

    def _safe_rows(self):
        try:
            return self.rows()
        except ValueError:
            return None

    def _add(self):
        row = self.table.currentRow()
        self.table.blockSignals(True)
        self._append({"name": "token"}, row + 1 if row >= 0 else None)
        self.table.blockSignals(False)
        self.changed.emit()

    def _duplicate(self):
        rows, row = self._safe_rows(), self.table.currentRow()
        if rows is None or row < 0:
            return
        rows.insert(row + 1, OrderedDict(rows[row]))
        self.set_rows(rows)
        self.table.selectRow(row + 1)
        self.changed.emit()

    def _remove(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.changed.emit()

    def _move(self, step):
        rows, row = self._safe_rows(), self.table.currentRow()
        if rows is None or row < 0 or not 0 <= row + step < len(rows):
            return
        rows[row], rows[row + step] = rows[row + step], rows[row]
        self.set_rows(rows)
        self.table.selectRow(row + step)
        self.changed.emit()


class PairTable(QWidget):
    """Two editable columns (label / value) with add, remove, up, down."""
    changed = Signal()

    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, 2)
        self.table.setToolTip('Double-click a cell to edit it; Add / Remove / Up / Down on the right')
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(lambda _item: self.changed.emit())
        buttons = QVBoxLayout()
        for text, slot in (("Add", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1))):
            button = QPushButton(text)
            button.setToolTip(ROW_BUTTON_TIPS.get(text, ""))
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def pairs(self):
        out = []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, c) for c in (0, 1)]
            out.append(tuple(c.text() if c else "" for c in cells))
        return out

    def set_pairs(self, pairs):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for label, value in pairs:
            self._append(label, value)
        self.table.blockSignals(False)

    def _append(self, label, value):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(label))
        self.table.setItem(row, 1, QTableWidgetItem(value))

    def _add(self):
        self._append("Label", "")
        self.changed.emit()

    def _remove(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.changed.emit()

    def _move(self, step):
        row = self.table.currentRow()
        pairs = self.pairs()
        target = row + step
        if row < 0 or not 0 <= target < len(pairs):
            return
        pairs[row], pairs[target] = pairs[target], pairs[row]
        self.set_pairs(pairs)
        self.table.setCurrentCell(target, 0)
        self.changed.emit()


class StepList(QWidget):
    """Editable list of oiiotool steps."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setToolTip('One entry per line; double-click to edit, Up / Down to reorder')
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        buttons = QVBoxLayout()
        for text, slot in (("Add", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1))):
            button = QPushButton(text)
            button.setToolTip(ROW_BUTTON_TIPS.get(text, ""))
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list)
        layout.addLayout(buttons)

    def steps(self):
        return [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).text().strip()]

    def set_steps(self, steps):
        self.list.blockSignals(True)
        self.list.clear()
        for step in steps:
            self._append(step)
        self.list.blockSignals(False)

    def _append(self, text):
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.list.addItem(item)
        return item

    def _add(self):
        item = self._append("--resize 1920x0")
        self.list.setCurrentItem(item)
        self.list.editItem(item)
        self.changed.emit()

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self.changed.emit()

    def _move(self, step):
        row = self.list.currentRow()
        steps = [self.list.item(i).text() for i in range(self.list.count())]
        target = row + step
        if row < 0 or not 0 <= target < len(steps):
            return
        steps[row], steps[target] = steps[target], steps[row]
        self.set_steps(steps)
        self.list.setCurrentRow(target)
        self.changed.emit()


# --- timeline -------------------------------------------------------------------------------

class TrimSlider(QSlider):
    """A slider that also draws the trim: the kept range shaded, In and Out as marks."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.trim_in = None
        self.trim_out = None
        self.setMinimumHeight(26)

    def _x(self, value):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                             QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        return groove.x() + handle.width() / 2 + QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), value, span)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.trim_in is None and self.trim_out is None:
            return
        lo = self.trim_in if self.trim_in is not None else self.minimum()
        hi = self.trim_out if self.trim_out is not None else self.maximum()
        painter = QPainter(self)
        x1, x2 = self._x(lo), self._x(hi)
        h = self.height()
        painter.fillRect(int(x1), h // 2 - 3, max(2, int(x2 - x1)), 6, QColor(70, 150, 230, 150))
        for value, colour in ((self.trim_in, QColor(90, 200, 110)), (self.trim_out, QColor(230, 90, 80))):
            if value is not None:
                painter.setPen(QPen(colour, 2))
                x = int(self._x(value))
                painter.drawLine(x, 2, x, h - 3)
        painter.end()


class TimelineBar(QWidget):
    """A slider over the input's frames, with the frame number and its timecode. frameChosen
    is sent when a frame is picked: the slider let go, a typed frame number, or a pause after
    keyboard / page steps (not on every pixel of a drag). In and Out mark the trim (the I and
    O keys, or the buttons); trimChanged is sent when they change."""
    frameChosen = Signal(int)
    trimChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timecode_for = None              # callable frame -> timecode text, or None
        self.slider = TrimSlider()
        self.slider.setTracking(True)
        self.slider.setPageStep(10)
        self.first_label, self.last_label = QLabel(""), QLabel("")
        self.spin = QSpinBox()
        self.spin.setToolTip('The frame to preview: type a number, or drag the timeline')
        self.spin.setKeyboardTracking(False)
        self.spin.setFixedWidth(90)
        self.tc_label = QLabel("")
        self.tc_label.setMinimumWidth(90)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._emit)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addWidget(self.first_label)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.last_label)
        layout.addWidget(QLabel("  Frame"))
        layout.addWidget(self.spin)
        layout.addWidget(self.tc_label)
        for text, tip, slot in (("In", "Set In at this frame (I)", self.set_in),
                                ("Out", "Set Out at this frame (O)", self.set_out),
                                ("Clear", "Clear the trim", self.clear_trim)):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setFixedWidth(46)
            button.clicked.connect(slot)
            layout.addWidget(button)
        self.trim_label = QLabel("")
        self.trim_label.setMinimumWidth(150)
        layout.addWidget(self.trim_label)
        self.slider.valueChanged.connect(self._moved)
        self.slider.sliderReleased.connect(self._released)
        self.spin.editingFinished.connect(self._spin_done)
        self.set_range(0, 0)

    def set_range(self, first, last):
        for widget in (self.slider, self.spin):
            widget.blockSignals(True)
            widget.setRange(first, max(first, last))
            widget.blockSignals(False)
        self.first_label.setText(str(first) if last > first else "")
        self.last_label.setText(str(last) if last > first else "")
        self.setEnabled(last > first)
        self._labels(self.slider.value())

    def set_frame(self, frame):
        for widget in (self.slider, self.spin):
            widget.blockSignals(True)
            widget.setValue(int(frame))
            widget.blockSignals(False)
        self._labels(int(frame))

    def frame(self):
        return self.slider.value()

    # trim

    def trim(self):
        return self.slider.trim_in, self.slider.trim_out

    def set_trim(self, trim_in, trim_out, notify=False):
        lo, hi = self.slider.minimum(), self.slider.maximum()
        self.slider.trim_in = trim_in if trim_in is not None and lo <= trim_in <= hi else None
        self.slider.trim_out = trim_out if trim_out is not None and lo <= trim_out <= hi else None
        self.slider.update()
        a, b = self.slider.trim_in, self.slider.trim_out
        if a is None and b is None:
            self.trim_label.setText("")
        else:
            first = a if a is not None else lo
            last = b if b is not None else hi
            self.trim_label.setText("In {}  Out {}  ({} frames)".format(
                a if a is not None else "-", b if b is not None else "-", last - first + 1))
        if notify:
            self.trimChanged.emit()

    def set_in(self):
        if not self.isEnabled():
            return
        frame = self.slider.value()
        out = self.slider.trim_out
        self.set_trim(frame, out if out is None or out >= frame else None, notify=True)

    def set_out(self):
        if not self.isEnabled():
            return
        frame = self.slider.value()
        start = self.slider.trim_in
        self.set_trim(start if start is None or start <= frame else None, frame, notify=True)

    def clear_trim(self):
        self.set_trim(None, None, notify=True)

    def refresh_labels(self):
        self._labels(self.slider.value())

    def _labels(self, frame):
        text = self.timecode_for(frame) if self.timecode_for else ""
        self.tc_label.setText(text or "")

    def _moved(self, frame):
        self.spin.blockSignals(True)
        self.spin.setValue(frame)
        self.spin.blockSignals(False)
        self._labels(frame)
        if not self.slider.isSliderDown():
            self.timer.start(STEP_DELAY_MS)       # a key or a page step: wait for more

    def _released(self):
        self.timer.stop()
        self._emit()

    def _spin_done(self):
        if self.spin.value() != self.slider.value():
            self.slider.blockSignals(True)
            self.slider.setValue(self.spin.value())
            self.slider.blockSignals(False)
            self._labels(self.spin.value())
            self._emit()

    def _emit(self):
        self.frameChosen.emit(self.slider.value())


# --- viewer ---------------------------------------------------------------------------------

CHECKER = (QColor(102, 102, 102), QColor(153, 153, 153))


def checkerboard(width, height):
    """An opaque grey checkerboard, squares about 1/48 of the longer side (8 px at least)."""
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(CHECKER[0])
    size = max(8, max(width, height) // 48)
    painter = QPainter(image)
    for y in range(0, height, size):
        for x in range((y // size) % 2 * size, width, size * 2):
            painter.fillRect(x, y, size, size, CHECKER[1])
    painter.end()
    return image


def _opaque(straight):
    """The colour of a straight ARGB32 picture with alpha ignored: the same bytes read as
    RGB32 (Qt's own conversions would blend or unpremultiply)."""
    return QImage(bytes(straight.constBits()), straight.width(), straight.height(),
                  straight.bytesPerLine(), QImage.Format.Format_RGB32).copy()


def alpha_image(image, mode, premultiply):
    """One way of showing a picture with alpha (display only). image: the picture as read
    from the file (straight colour, not through a QPixmap, which premultiplies and so loses
    the colour where alpha is 0). premultiply: the colour is multiplied by alpha first; else
    it is used as stored. rgb: the colour. alpha: alpha as grey. checker: the colour (times
    alpha with premultiply) + checker x (1 - alpha)."""
    straight = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = straight.width(), straight.height()
    if mode == "alpha":
        alpha = straight.convertToFormat(QImage.Format.Format_Alpha8)
        grey = QImage(bytes(alpha.constBits()), width, height,
                      alpha.bytesPerLine(), QImage.Format.Format_Grayscale8)
        return grey.copy()
    if mode == "rgb" and not premultiply:
        return _opaque(straight)
    if mode == "rgb":
        result = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(QColor(0, 0, 0))
    else:
        result = checkerboard(width, height)
    painter = QPainter(result)
    if premultiply:
        painter.drawImage(0, 0, straight)            # colour x alpha + background x (1 - alpha)
    else:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        painter.drawImage(0, 0, straight)            # background x (1 - alpha)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.drawImage(0, 0, _opaque(straight))   # + the colour as stored
    painter.end()
    return result.convertToFormat(QImage.Format.Format_RGB32)


class Viewer(QGraphicsView):
    """Pan (drag), zoom (wheel), fit, 1:1. The compare image lies over the result, scaled to
    its size; the mix slider sets its opacity."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setBackgroundBrush(QColor(40, 40, 40))
        self.main = QGraphicsPixmapItem()
        self.compare = QGraphicsPixmapItem()
        self.main.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.compare.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene().addItem(self.main)
        self.scene().addItem(self.compare)
        self.compare.setOpacity(0.0)
        self._fitted = False
        # small info text over the top right of the picture (not part of the image)
        self.overlay = QLabel(self)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.overlay.setStyleSheet("QLabel { background: rgba(0, 0, 0, 150); color: #e8e8e8; "
                                   "padding: 4px 7px; border-radius: 3px; font-size: 11px; }")
        self.overlay.hide()

    def set_overlay(self, lines, visible=True):
        """lines: [(label, text)], shown as a small two-column table at the top right."""
        if not lines or not visible:
            self.overlay.hide()
            return
        rows = "".join("<tr><td>{}:&nbsp;&nbsp;</td><td>{}</td></tr>".format(label, text)
                       for label, text in lines)
        self.overlay.setText("<table cellspacing=0 cellpadding=0>{}</table>".format(rows))
        self.overlay.adjustSize()
        self._place_overlay()
        self.overlay.show()
        self.overlay.raise_()

    def _place_overlay(self):
        margin = 8
        right = self.viewport().geometry().right()
        top = self.viewport().geometry().top()
        self.overlay.move(right - self.overlay.width() - margin, top + margin)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_overlay()

    def set_images(self, main, compare=None, par=1.0):
        self.main.setPixmap(main or QPixmap())
        self.main.setTransform(QTransform.fromScale(par, 1.0))
        width = (main.width() * par) if main else 0
        height = main.height() if main else 0
        if compare is not None and not compare.isNull() and main:
            self.compare.setPixmap(compare)
            self.compare.setTransform(QTransform.fromScale(width / compare.width(),
                                                           height / compare.height()))
            self.compare.setVisible(True)
        else:
            self.compare.setVisible(False)
        self.scene().setSceneRect(0, 0, width, height)
        if not self._fitted and main:
            self.fit()
            self._fitted = True

    def set_mix(self, mix):
        self.compare.setOpacity(mix)

    def fit(self):
        if not self.scene().sceneRect().isEmpty():
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def one_to_one(self):
        self.resetTransform()

    def wheelEvent(self, event):
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)


# --- main window ----------------------------------------------------------------------------

class RowTabs(QWidget):
    """Tabs whose names wrap into several rows (QTabWidget only scrolls them): a grid of
    tab-like buttons over a stack of pages. Offers the QTabWidget calls the window uses."""
    currentChanged = Signal(int)

    def __init__(self, rows=2, parent=None):
        super().__init__(parent)
        self.rows = rows
        self.buttons, self.titles = [], []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bar = QWidget()
        self.bar.setObjectName("rowtabs")
        self.grid = QGridLayout(self.bar)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(1)
        layout.addWidget(self.bar)
        self.stack = QStackedWidget()
        self.stack.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self.stack, 1)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self.setCurrentIndex)
        self.bar.setStyleSheet(
            "QToolButton { background: #3a3a3d; color: #c8c8c8; border: 1px solid #4a4a4e;"
            " border-bottom: none; padding: 4px 8px; }"
            "QToolButton:hover { background: #46464a; }"
            "QToolButton:checked { background: #2d2d30; color: white; font-weight: bold;"
            " border-top: 2px solid #3a6eb4; }"
            "QToolButton:disabled { color: #707070; }"
            "QToolButton[unused=\"true\"] { font-style: italic; }")

    def addTab(self, widget, title):
        index = len(self.buttons)
        button = QToolButton()
        button.setText(title)
        button.setCheckable(True)
        # may get narrower than its text, so the rows never force the panel wider
        button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        button.setMinimumWidth(40)
        button.setToolTip(title)
        self.group.addButton(button, index)
        self.buttons.append(button)
        self.titles.append(title)
        self.stack.addWidget(widget)
        self._relayout()
        if index == 0:
            self.setCurrentIndex(0)
        return index

    def _relayout(self):
        """Spread the buttons over the rows, filling each row's width."""
        for button in self.buttons:
            self.grid.removeWidget(button)
        per_row = max(1, -(-len(self.buttons) // self.rows))       # ceil
        for index, button in enumerate(self.buttons):
            self.grid.addWidget(button, index // per_row, index % per_row)

    def count(self):
        return len(self.buttons)

    def tabText(self, index):
        return self.titles[index]

    def currentIndex(self):
        return self.stack.currentIndex()

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self.buttons):
            return
        self.buttons[index].setChecked(True)
        if self.stack.currentIndex() != index:
            self.stack.setCurrentIndex(index)
            self.currentChanged.emit(index)

    def setTabEnabled(self, index, enabled):
        self.buttons[index].setEnabled(enabled)
        if not enabled and index == self.currentIndex():
            # like QTabWidget: move off a page that is switched off
            for other in list(range(index + 1, self.count())) + list(range(index)):
                if self.buttons[other].isEnabled():
                    self.setCurrentIndex(other)
                    break

    def setTabToolTip(self, index, text):
        self.buttons[index].setToolTip(text or self.titles[index])

    def setTabMarks(self, index, edited, unused):
        """"Title *" when edited since its preset was loaded / saved; italics when not used.
        tabText() stays the plain title."""
        button = self.buttons[index]
        text = self.titles[index] + (" *" if edited else "")
        if button.text() != text:
            button.setText(text)
        if bool(button.property("unused")) != bool(unused):
            button.setProperty("unused", bool(unused))
            button.style().unpolish(button)
            button.style().polish(button)


class TunerWindow(QMainWindow):
    def __init__(self, input1="", input2="", presets_file=""):
        super().__init__()
        self.setWindowTitle("vfx-transcode tuner")
        self.state = core.TunerState()
        self.state.input.input1 = input1
        self.state.input.input2 = input2
        self.state.input.compare_source = not input2     # a compare file given: compare to it
        self.state.input.presets_file = presets_file
        self.presets = None
        self.report = None
        self.loading = False                  # True while widgets are filled from the state
        self.work_dir = Path(tempfile.mkdtemp(prefix="vfx-transcode-tuner-"))
        self.run_counter = itertools.count(1)
        self.process = None
        self.compare_process, self.compare_key, self.compare_pixmap = None, None, None
        self.compare_kinds = {}
        self.pending = False
        self.images = {}
        self.raw_images = {}             # pixmap cache key -> the image as read (alpha view)
        self.stale = {"preview": True, "filmstrip": True}    # settings changed since the last run
        self.shown_mode = "image"
        self.job = None                       # the Process run (the conversion)
        self.job_output = None                # the movie it wrote, from its "Done:" line
        self.input_info = core.InputInfo()    # what the input is: sequence, still or movie
        self.tc_base = None                   # (first frame, its timecode, fps) for the timeline
        self.super_presets = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        self._load_presets()
        self._build()
        # drops anywhere in the window (the viewer, a text field ...) set the input
        QApplication.instance().installEventFilter(self)
        self.setAcceptDrops(True)             # so drops over labels and empty space arrive too
        self._fill_all()
        self._show_presets_choice()
        self._scan_ocio()
        self._load_super_presets()
        self.update_outputs()
        tooltips.fill_missing(self)           # any control still without a hint gets one
        if input1:
            self._input_changed()
            QTimer.singleShot(200, self.refresh)

    # presets

    def _compact_controls(self):
        """Short values get narrow fields of equal width (COMPACT_WIDTHS)."""
        for chars, names in COMPACT_WIDTHS.items():
            for name in names:
                widget = getattr(self, name, None)
                if widget is not None:
                    set_chars(widget, chars)
        for button in self.detect_buttons:          # Detect next to frame rate and pixel aspect
            set_chars(button, 8)

    def _presets_path(self):
        if self.state.input.presets_file:
            return resolve_path(self.state.input.presets_file, app_dir())
        if getattr(self, "presets_choice", None) is None:
            self.presets_choice = studio.choose_presets()
        return self.presets_choice.path

    def _load_presets(self):
        """The Presets file field, else the studio preferences from vfx-transcode.ini, else
        ./presets.json; the field's placeholder says which default is used."""
        self.presets_choice = None if self.state.input.presets_file else studio.choose_presets()
        choice = self.presets_choice
        try:
            if choice is not None and choice.presets is not None:
                self.presets = choice.presets
            else:
                self.presets = load_presets(self._presets_path())
        except Exception as exc:                  # show it, keep the old presets
            QMessageBox.warning(self, "Presets", str(exc))
        self._show_presets_choice()

    def _show_presets_choice(self):
        """The presets file in use, on the Presets tab (the tuner's -presets_file, else the
        studio preferences from vfx-transcode.ini, else ./presets.json)."""
        if not hasattr(self, "presets_label"):
            return                                # called again once the widgets exist
        choice = self.presets_choice
        if choice is None:
            self.presets_label.setText("Presets file: {} (-presets_file)".format(self._presets_path()))
            self.presets_label.setToolTip("")
            return
        self.presets_label.setText("Presets file: {} ({})".format(choice.path, choice.source))
        self.presets_label.setToolTip(studio.describe(choice))
        if choice.problems and hasattr(self, "status"):
            self.status.setText("Studio preferences not used: " + "; ".join(choice.problems))

    # layout

    def _build(self):
        toolbar = QToolBar("View")
        self.addToolBar(toolbar)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip('Run vfx-transcode again for the view on screen (preview, slate or filmstrip)')
        self.refresh_button.clicked.connect(self.refresh)
        self.auto_refresh = QCheckBox("Refresh after edit")
        self.auto_refresh.setToolTip('Refresh the picture shortly after each change; off: press Refresh yourself')
        self.auto_refresh.setChecked(True)
        fit = QPushButton("Fit")
        fit.setToolTip('Fit the whole picture into the view')
        one = QPushButton("1:1")
        one.setToolTip('Show the picture at its real size: one image pixel per screen pixel')
        self.view_buttons = QButtonGroup(self)
        view_widgets = [QLabel("  View")]
        for mode, label in (("image", "Image"), ("slate", "Slate"), ("filmstrip", "Filmstrip")):
            button = QRadioButton(label)
            button.setToolTip({"image": 'The previewed frame as the movie shows it', "slate": 'The slate frame (switch the slate on in the Slate tab)', "filmstrip": "The filmstrip (made with the Filmstrip tab's settings)"}[mode])
            button.setProperty("mode", mode)
            button.setChecked(mode == "image")
            self.view_buttons.addButton(button)
            view_widgets.append(button)
        self.view_buttons.buttonToggled.connect(self._view_changed)
        self.mix = QSlider(Qt.Orientation.Horizontal)
        self.mix.setToolTip('Dissolve to the compare picture: the source frame or the Compare to file (Input tab)')
        self.mix.setRange(0, 100)
        self.mix.setFixedWidth(160)
        self.mix.valueChanged.connect(lambda v: self.viewer.set_mix(v / 100.0))
        self.status = QLabel("")
        self.show_overlay = QCheckBox("Show overlay")
        self.show_overlay.setChecked(True)
        self.show_overlay.setToolTip("Input and output size, pixel aspect and frame rate over "
                                     "the top right of the picture")
        self.show_overlay.toggled.connect(lambda _on: self._update_overlay())
        # alpha: how a picture with an alpha channel is shown (the view only, not the output)
        self.alpha_box = QWidget()
        alpha_layout = QHBoxLayout(self.alpha_box)
        alpha_layout.setContentsMargins(0, 0, 0, 0)
        self.alpha_view = QComboBox()
        for key, label in core.ALPHA_VIEWS.items():
            self.alpha_view.addItem(label, key)
        self.alpha_view.setToolTip("RGB: the colour without alpha. Alpha: the alpha channel as "
                                   "grey (white = opaque). Checkerboard: the picture over a "
                                   "checkerboard. Only the view: the output is not changed.")
        self.alpha_premult = QCheckBox("Premultiply")
        self.alpha_premult.setChecked(False)
        self.alpha_premult.setToolTip(
            "On: the colour is multiplied by alpha for the display (RGB and Checkerboard). "
            "Off: the colour is shown as stored. Only the view: the output is not changed.")
        self.alpha_view.currentIndexChanged.connect(lambda _i: self.show_images())
        self.alpha_premult.toggled.connect(lambda _on: self.show_images())
        alpha_layout.addWidget(QLabel("  Alpha"))
        alpha_layout.addWidget(self.alpha_view)
        alpha_layout.addWidget(self.alpha_premult)
        self.alpha_hint = QLabel("")
        self.alpha_hint.setStyleSheet("color: #8a8a8a;")
        for widget in ([self.refresh_button, self.auto_refresh, fit, one] + view_widgets
                       + [QLabel("  Compare mix"), self.mix, QLabel("  "), self.show_overlay]):
            toolbar.addWidget(widget)
        self.alpha_action = toolbar.addWidget(self.alpha_box)
        self.alpha_action.setVisible(False)
        self.alpha_hint_action = toolbar.addWidget(self.alpha_hint)
        self.alpha_hint_action.setVisible(False)
        for widget in (QLabel("  "), self.status):
            toolbar.addWidget(widget)

        self.viewer = Viewer()
        fit.clicked.connect(self.viewer.fit)
        one.clicked.connect(self.viewer.one_to_one)
        self.timeline = TimelineBar()
        self.timeline.timecode_for = self._timecode_for
        self.timeline.frameChosen.connect(self._timeline_frame)
        self.timeline.trimChanged.connect(self._trim_changed)
        for key, slot in (("I", self.timeline.set_in), ("O", self.timeline.set_out)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)      # text fields keep their typed I and O
        picture = QWidget()
        picture_layout = QVBoxLayout(picture)
        picture_layout.setContentsMargins(0, 0, 0, 0)
        picture_layout.setSpacing(2)
        picture_layout.addWidget(self.viewer, 1)
        picture_layout.addWidget(self.timeline)

        self.log = QPlainTextEdit()
        self.log.setToolTip('What vfx-transcode printed in the last runs')
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addStretch()
        clear_log = QPushButton("Clear")
        clear_log.setToolTip('Clear the log')
        clear_log.clicked.connect(self.log.clear)
        log_bar.addWidget(clear_log)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log)
        self.bottom_tabs = QTabWidget()
        super_page = QWidget()
        self._build_super_presets(super_page)
        self.bottom_tabs.addTab(super_page, "Super-presets")
        info_page = QWidget()
        self._build_info(info_page)
        self.bottom_tabs.addTab(info_page, "Info")
        self.bottom_tabs.addTab(log_box, "Log")
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(picture)
        left.addWidget(self.bottom_tabs)
        left.setStretchFactor(0, 3)
        left.setStretchFactor(1, 1)
        left.setSizes([620, 300])

        self.tabs = RowTabs(rows=3)           # the tab names in three rows: all visible at once
        for title, builder in (("Input", self._build_input), ("Color", self._build_colour),
                               ("Tokens", self._build_tokens),
                               ("Intermediate", self._build_intermediate),
                               ("Multichannel", self._build_channels),
                               ("Output", self._build_encode),
                               ("Encode", self._build_encode_tab),
                               ("Destination", self._build_destination),
                               ("Metadata", self._build_metadata),
                               ("Transform Sequence", self._build_transform),
                               ("Transform", self._build_ff_transform),
                               ("Burn-in", self._build_burn), ("Slate", self._build_slate),
                               ("Filmstrip", self._build_filmstrip),
                               ("Thumbnail", self._build_thumb),
                               ("Presets", self._build_output)):
            page = QWidget()
            builder(page)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, title)

        self._compact_controls()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setSizes([900, 560])
        self.setCentralWidget(splitter)
        self.resize(1500, 950)

    def _changed(self, *_args):
        """Any control edited: copy the widgets into the state, update the outputs, and refresh
        after a short pause if "Refresh after edit" is on."""
        if self.loading:
            return
        self._read_all()
        self.stale = {"preview": True, "filmstrip": True}
        self.update_outputs()
        if self.auto_refresh.isChecked():
            self.timer.start(REFRESH_DELAY_MS)

    def _connect(self, *widgets):
        for w in widgets:
            if isinstance(w, QLineEdit):
                w.editingFinished.connect(self._changed)
            elif isinstance(w, QComboBox):
                w.currentTextChanged.connect(self._changed)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(self._changed)
            elif isinstance(w, QCheckBox):
                w.toggled.connect(self._changed)
            elif isinstance(w, (ColorButton, PairTable, StepList)):
                w.changed.connect(self._changed)

    # super-presets tab (under the picture)

    def _build_super_presets(self, page):
        layout = QVBoxLayout(page)
        # left: the saved super-presets, the selected one's notes under them, then Load /
        # Duplicate / Delete; right: Name and Notes for Save. The divider moves sideways.
        top = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        list_split = QSplitter(Qt.Orientation.Vertical)
        self.sp_list = QListWidget()
        self.sp_list.setToolTip('The saved super-presets (italic: from the local file next to the application); select one to see its notes, then Load')
        self.sp_list.currentTextChanged.connect(self._super_selected)
        self.sp_info = QPlainTextEdit()
        self.sp_info.setToolTip('The selected super-preset: notes, the input it was made for, its areas')
        self.sp_info.setReadOnly(True)
        self.sp_info.setPlaceholderText("Notes of the selected super-preset")
        list_split.addWidget(self.sp_list)
        list_split.addWidget(self.sp_info)
        list_split.setSizes([200, 120])
        left_layout.addWidget(list_split, 1)
        buttons = QHBoxLayout()
        for text, slot, tip in (
                ("Load", self._super_load, "Load the selected super-preset into all the tabs"),
                ("Duplicate", self._super_duplicate, "Save a copy of the selected super-preset under a new name"),
                ("Delete", self._super_delete, "Delete the selected super-preset")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        left_layout.addLayout(buttons)
        top.addWidget(left)

        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Name"))
        self.sp_name = QLineEdit()
        self.sp_name.setToolTip('The name to save the current settings under (an existing name replaces that super-preset)')
        self.sp_name.setPlaceholderText("Name for Save")
        row.addWidget(self.sp_name, 1)
        right.addLayout(row)
        right.addWidget(QLabel("Notes"))
        self.sp_notes = QPlainTextEdit()           # grows with the panel
        self.sp_notes.setToolTip('Notes saved with the super-preset: what it is for, who uses it; the process app shows them')
        self.sp_notes.setPlaceholderText("What it is for, who uses it ... (saved with it)")
        self.sp_notes.setMinimumHeight(40)
        right.addWidget(self.sp_notes, 1)
        saves = QLabel("A super-preset holds every area: " + ", ".join(sp.AREA_TITLES.values())
                       + ", and the Destination.")
        saves.setWordWrap(True)
        saves.setStyleSheet("color: #8a8a8a;")
        right.addWidget(saves)
        row = QHBoxLayout()
        save = QPushButton("Save")
        save.setToolTip("Save all the current settings under Name")
        save.clicked.connect(self._super_save)
        row.addWidget(save)
        row.addStretch()
        right.addLayout(row)
        self.sp_file = QLabel("")
        self.sp_file.setStyleSheet("color: #8a8a8a;")
        self.sp_file.setWordWrap(True)
        right.addWidget(self.sp_file)
        top.addWidget(right_widget)
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setSizes([280, 600])
        top.setChildrenCollapsible(False)
        layout.addWidget(top, 1)

        self.process_button = QPushButton("Process")
        self.process_button.setToolTip('Make the output now with these settings (vfx-transcode in the background; see Log)')
        font = QFont(self.process_button.font())
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        self.process_button.setFont(font)
        self.process_button.setMinimumHeight(54)
        self.process_button.setStyleSheet("QPushButton { background: #2f6f3e; color: white; }"
                                          "QPushButton:hover { background: #378048; }")
        self.process_button.clicked.connect(self._process)
        layout.addWidget(self.process_button)
        status = QHBoxLayout()
        self.job_progress = QProgressBar()
        self.job_progress.setRange(0, 1)
        self.job_progress.setValue(0)
        self.job_progress.setTextVisible(False)
        self.job_progress.setFixedWidth(160)
        self.job_status = QLabel("")
        self.job_status.setWordWrap(True)
        status.addWidget(self.job_progress)
        status.addWidget(self.job_status, 1)
        layout.addLayout(status)

    def _super_path(self):
        return sp.default_path(self._presets_path())

    def _load_super_presets(self):
        """The super-presets next to the presets file in use (the studio file, when
        vfx-transcode.ini names one) plus any local copies next to the application."""
        path = self._super_path()
        paths = sp.search_paths(self._presets_path())
        try:
            self.super_presets, self.super_origin = sp.load_merged(paths)
        except TranscodeError as exc:
            self.super_presets, self.super_origin = {}, {}
            QMessageBox.warning(self, "Super-presets", str(exc))
        local = [n for n, p in self.super_origin.items() if p != path]
        self.sp_file.setText("File: {}{}{}".format(
            path, "" if path.is_file() else " (made on the first Save)",
            "\n+ {} local (italic): {}".format(len(local), paths[-1]) if local else ""))
        current = self.sp_list.currentItem().text() if self.sp_list.currentItem() else ""
        self.sp_list.blockSignals(True)
        self.sp_list.clear()
        for name in self.super_presets:
            item = QListWidgetItem(name)
            origin = self.super_origin.get(name)
            item.setToolTip(str(origin) if origin else "")
            if origin is not None and origin != path:
                font = item.font()
                font.setItalic(True)
                item.setFont(font)
            self.sp_list.addItem(item)
        self.sp_list.blockSignals(False)
        items = self.sp_list.findItems(current, Qt.MatchFlag.MatchExactly) if current else []
        if items:
            self.sp_list.setCurrentItem(items[0])
            self._super_selected(current)         # the notes panel follows a changed file
        else:
            self.sp_info.setPlainText("")

    def _super_selected(self, name):
        entry = self.super_presets.get(name)
        if not entry:
            self.sp_info.setPlainText("")
            return
        self.sp_name.setText(name)
        self.sp_notes.setPlainText(entry.get("notes", ""))
        self.sp_info.setPlainText(self._super_info(name, entry))

    def _super_info(self, name, entry):
        """The notes, then what the super-preset is: input, areas, destination, file."""
        lines = [entry.get("notes", "").strip() or "(no notes)", ""]
        if entry.get("input_type"):
            lines.append("For: " + core.INPUT_TYPES.get(entry["input_type"], entry["input_type"]))
        lines.append("Areas: " + (", ".join(sp.AREA_TITLES.get(a, a) for a in entry.get("areas", {})) or "none"))
        dest = core.destination_of(entry)
        if dest is not None:
            lines.append("Destination: " + dest.output())
        origin = self.super_origin.get(name)
        if origin:
            lines.append("File: {}".format(origin))
        return "\n".join(lines)

    def _super_save(self):
        name = self.sp_name.text().strip()
        areas = list(sp.AREAS)                  # a super-preset always holds everything
        titles = [sp.AREA_TITLES[a] for a in areas] + ["Destination"]
        if not name:
            name, ok = QInputDialog.getText(self, "Save super-preset", "Name for the super-preset:")
            name = name.strip()
            if not ok or not name:
                return
            self.sp_name.setText(name)
        if name in self.super_presets:
            answer = QMessageBox.question(self, "Super-presets",
                                          'Replace the super-preset "{}"?'.format(name))
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._read_all()
        entry = core.super_preset_entry(self.state, areas, self.sp_notes.toPlainText().strip(),
                                        self.super_presets.get(name), self.state.dest)
        result = self._super_write(lambda presets: presets.__setitem__(name, entry))
        if result is None:
            return
        self._load_super_presets()
        items = self.sp_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.sp_list.setCurrentItem(items[0])
        self.job_status.setText('Saved super-preset "{}" (all areas and the destination) to {}{}'.format(
            name, result.path,
            " (previous version: {})".format(result.backup.name) if result.backup else ""))

    def _super_write(self, change, path=None):
        """Save a change to the super-presets file (multi-user safe, see safe_save). The studio
        file first; if it can't be written, offer the local file next to the application.
        Returns the SaveResult, or None if nothing was saved."""
        path = path or self._super_path()
        local = sp.local_path()
        try:
            return sp.update(path, change)
        except sp.safe_save.SaveConflict as exc:
            QMessageBox.warning(self, "Super-presets", "{}\nNothing was saved; try again.".format(exc))
            return None
        except (OSError, TranscodeError) as exc:
            if Path(path) == local:
                QMessageBox.warning(self, "Super-presets", "Could not save: {}".format(exc))
                return None
            answer = QMessageBox.question(
                self, "Super-presets",
                "Could not save to the studio super-presets:\n{}\n\n{}\n\nSave to the local file "
                "next to the application instead?\n{}\n\nOn this computer it is listed with the "
                "studio ones (in italic) and wins over a studio one of the same name.".format(
                    path, exc, local))
            if answer != QMessageBox.StandardButton.Yes:
                return None
            return self._super_write(change, local)

    def _super_load(self):
        item = self.sp_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Super-presets", "Select a super-preset in the list.")
            return
        entry = self.super_presets.get(item.text(), {})
        self._read_all()
        loaded = core.apply_super_preset(self.state, entry)
        titles = [sp.AREA_TITLES[a] for a in loaded]
        dest = core.destination_of(entry)
        if not loaded and dest is None:
            self.job_status.setText("Nothing loaded: the super-preset is empty.")
            return
        if dest is not None:
            self._apply_destination(dest)
            titles.append("Destination")
        if loaded:
            self._fill_all()
            self._changed()
        made_for = entry.get("input_type", "")
        mismatch = ""
        if made_for and self.state.input.input1 and made_for != core.effective_input_type(self.state.input):
            mismatch = " (made for {} input; the current input is {})".format(
                core.INPUT_TYPES.get(made_for, made_for), core.INPUT_TYPES.get(
                    core.effective_input_type(self.state.input), "another type"))
        self.job_status.setText('Loaded "{}": {}{}'.format(item.text(), ", ".join(titles), mismatch))

    def _super_duplicate(self):
        item = self.sp_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Super-presets", "Select a super-preset in the list.")
            return
        source = item.text()
        suggestion, n = source + " copy", 2
        while suggestion in self.super_presets:
            suggestion, n = "{} copy {}".format(source, n), n + 1
        name, ok = QInputDialog.getText(self, "Duplicate super-preset",
                                        'Name for the copy of "{}":'.format(source), text=suggestion)
        name = name.strip()
        if not ok or not name:
            return
        if name in self.super_presets:
            answer = QMessageBox.question(self, "Super-presets", 'Replace the super-preset "{}"?'.format(name))
            if answer != QMessageBox.StandardButton.Yes:
                return
        entry = json.loads(json.dumps(self.super_presets[source]))    # a copy, not shared
        result = self._super_write(lambda presets: presets.__setitem__(name, entry))
        if result is None:
            return
        self._load_super_presets()
        items = self.sp_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.sp_list.setCurrentItem(items[0])
        self.job_status.setText('Duplicated "{}" as "{}" in {}'.format(source, name, result.path))

    def _super_delete(self):
        item = self.sp_list.currentItem()
        if item is None:
            return
        name = item.text()
        answer = QMessageBox.question(self, "Super-presets", 'Delete the super-preset "{}" from\n{}?'.format(
            name, self.super_origin.get(name, self._super_path())))
        if answer != QMessageBox.StandardButton.Yes:
            return
        origin = self.super_origin.get(name, self._super_path())
        try:
            sp.update(origin, lambda presets: presets.pop(name, None))
        except (OSError, TranscodeError) as exc:
            QMessageBox.warning(self, "Super-presets", "Could not delete from {}: {}".format(origin, exc))
            return
        self._load_super_presets()

    # processing (the conversion)

    def _job_running(self):
        return self.job is not None and self.job.state() != QProcess.ProcessState.NotRunning

    def _process(self):
        if self._job_running():
            self.job.kill()
            self.job_status.setText("Cancelling...")
            return
        self._read_all()
        if not self.state.input.input1 or os.path.isdir(self.state.input.input1):
            QMessageBox.information(self, "Process", "Choose the input file on the Input tab.")
            return
        tool = split_args(self.in_tool.text()) or core.vfx_transcode_command()
        output = self._output()
        d = self.state.dest
        uses = core.uses_custom(d.output(), d.thumbnail() if self.state.thumb.enabled else "",
                                d.filmstrip() if self.state.filmstrip.enabled else "")
        empty = [n for n, used, value in (("Custom path", uses[0], d.custom_path),
                                          ("Custom name", uses[1], d.custom_name))
                 if used and not value]
        if empty:
            QMessageBox.information(self, "Process", "The Destination uses {}: type a value for "
                                    "it.".format(" and ".join(empty)))
            return
        cmd = core.process_command(self.state, self.presets, output, self._active_sub(), tool)
        self.log.appendPlainText("\n=== Process ===\n> " + core.quote(cmd))
        self.job_output = None
        self.job = QProcess(self)
        self.job.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.job.readyReadStandardOutput.connect(self._job_output)
        self.job.finished.connect(self._job_finished)
        self.job.errorOccurred.connect(self._job_error)
        self.process_button.setText("Cancel")
        self.job_progress.setRange(0, 0)                  # busy
        self.job_status.setText("Processing...")
        self.job.start(cmd[0], cmd[1:])

    def _job_output(self):
        data = bytes(self.job.readAllStandardOutput()).decode("utf-8", errors="replace").rstrip()
        if not data:
            return
        self.log.appendPlainText(data)
        lines = [l.strip() for l in data.splitlines() if l.strip()]
        for line in lines:
            if line.startswith("Done: "):
                self.job_output = line[len("Done: "):]
        if lines:
            self.job_status.setText(lines[-1][:200])

    def _job_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._job_done("Could not start vfx-transcode: check the vfx-transcode command on "
                           "the Presets tab.")

    def _job_finished(self, code, status):
        if status == QProcess.ExitStatus.CrashExit:
            self._job_done("Cancelled." if self.job_status.text() == "Cancelling..." else
                           "vfx-transcode stopped (crashed or cancelled).")
        elif code == 0:
            self._job_done("Done: {}".format(self.job_output) if self.job_output else "Done.")
        else:
            self._job_done("Failed (exit {}): see the Log tab.".format(code))

    def _job_done(self, text):
        self.process_button.setText("Process")
        self.job_progress.setRange(0, 1)
        self.job_progress.setValue(1 if text.startswith("Done") else 0)
        self.job_status.setText(text)
        self.log.appendPlainText("=== " + text)

    # input tab

    def _build_input(self, page):
        form = QFormLayout(page)
        self.in_input1 = QLineEdit()
        self.in_input1.setToolTip('Any frame of a sequence, or a movie: what the tuner previews (drop a file here too)')
        self.in_input2 = QLineEdit()
        self.in_input2.setToolTip('An image, movie or sequence to compare with (Compare mix), when Compare to source is off')
        self.in_detected = QLabel("")
        self.in_detected.setWordWrap(True)
        self.in_fps = combo(FRAME_RATES, editable=True)
        self.in_fps.setToolTip("auto: the EXR header's or the movie's own rate, else 24")
        detect = QPushButton("Detect")
        detect.setToolTip('Read the frame rate from the movie or the EXR header and put it in the field')
        detect.clicked.connect(self._detect_fps)
        self.in_fps_info = QLabel("")
        fps_row = QHBoxLayout()
        fps_row.addWidget(self.in_fps)
        fps_row.addWidget(detect)
        fps_row.addWidget(self.in_fps_info, 1)
        self.in_par = combo(PIXEL_ASPECTS, editable=True)
        self.in_par.setToolTip("Input pixel aspect (-par): auto = the EXR header's PixelAspectRatio "
                               "or the movie's sample aspect ratio, else 1. Any value: 2, 1.33, 4/3")
        detect_par = QPushButton("Detect")
        self.detect_buttons = (detect, detect_par)
        detect_par.setToolTip('Read the pixel aspect from the EXR header or the movie and put it in the field')
        detect_par.clicked.connect(self._detect_par)
        self.in_par_info = QLabel("")
        par_row = QHBoxLayout()
        par_row.addWidget(self.in_par)
        par_row.addWidget(detect_par)
        par_row.addWidget(self.in_par_info, 1)
        self.in_first_frame = QSpinBox()
        self.in_first_frame.setRange(0, 9999999)
        self.in_first_frame.setValue(DEFAULT_FIRST_FRAME)
        self.in_first_frame.setToolTip("Movie input: the frame number of its first frame")
        form.addRow("Input (any frame, or a movie)",
                    browse_row(self.in_input1, "Input", filters=INPUT_FILTER))
        self.in_input_type = QComboBox()
        for key, label in core.INPUT_TYPES.items():
            self.in_input_type.addItem(label, key)
        self.in_input_type.setToolTip("Set from the input file. With no input, choose it to set "
                                      "up the tabs (and super-presets) for that kind of input")
        form.addRow("Input type", self.in_input_type)
        form.addRow("", self.in_detected)
        form.addRow("Frame rate (-r)", fps_row)
        form.addRow("Pixel aspect (-par)", par_row)
        form.addRow("Movie: first frame number", self.in_first_frame)
        self.in_missing = QComboBox()
        for key, label in core.MISSING_FRAMES.items():
            self.in_missing.addItem(label, key)
        self.in_missing.setToolTip("Holes in the sequence or frames that fail to convert: hold "
                                   "the previous frame, or stop with an error (-missing_frames)")
        form.addRow("Missing frames", self.in_missing)
        self.in_compare_source = QCheckBox("Compare to source (the unmodified input footage)")
        self.in_compare_source.setChecked(True)
        self.in_compare_source.setToolTip("Compare mix shows the input's own frame: no transform, "
                                          "resize, burn-ins or slate (EXR with the Color tab's "
                                          "colour conversion). Off: compare to the file below")
        self.in_input2.setPlaceholderText("an image, a movie or a sequence (any frame of it)")
        form.addRow("Compare", self.in_compare_source)
        form.addRow("Compare to", browse_row(self.in_input2, "Compare to"))
        form.addRow(self._build_timecode())
        self._connect(self.in_input1, self.in_input_type, self.in_fps,
                      self.in_first_frame, self.in_par, self.in_missing)
        self.in_input1.editingFinished.connect(self._input_changed)
        self.in_first_frame.valueChanged.connect(lambda _v: self._input_changed())
        self.in_fps.currentTextChanged.connect(lambda _t: self.timeline.refresh_labels())
        self.in_input2.editingFinished.connect(self._compare_file_changed)
        self.in_compare_source.toggled.connect(lambda _on: self._compare_changed())

    # tokens tab

    def _build_tokens(self, page):
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.tk_preset = compact_combo(QComboBox(), 14)
        self.tk_preset.setToolTip("Pick a smart tokens preset to load its definitions")
        self.tk_preset.activated.connect(lambda _i: self._load_tokens())
        top.addWidget(QLabel("Smart tokens"))
        top.addWidget(self.tk_preset, 1)
        top.addWidget(self._save_button("smart_tokens"))
        layout.addLayout(top)
        self.tk_notes = QLabel("")
        self.tk_notes.setWordWrap(True)
        self.tk_notes.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(self.tk_notes)
        help_text = QLabel("Per-project keywords made from the name (or any other keyword): each "
                           "row sets {name}, top to bottom; a later row that applies wins, and "
                           "rows can use the tokens above them. A row is skipped when a condition "
                           "is false, or its regex does not match (extract). Use them anywhere "
                           "keywords work: output path, burn-ins, slate, metadata.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.tk_table = TokenTable()
        self.tk_table.setToolTip('Smart token definitions, processed top to bottom; a later row that applies wins')
        self.tk_table.changed.connect(self._changed)
        # smart tokens over name tokens, the border between them dragged to resize
        self.tk_split = QSplitter(Qt.Orientation.Vertical)
        self.tk_split.setChildrenCollapsible(False)
        self.tk_split.addWidget(self.tk_table)
        layout.addWidget(self.tk_split, 1)
        name_box = QGroupBox("Name tokens")
        name_layout = QVBoxLayout(name_box)
        name_form = QFormLayout()
        self.tk_version = QLineEdit()
        self.tk_version.setPlaceholderText(core.DEFAULT_VERSION_REGEX + " (default)")
        self.tk_version.setToolTip("Finds the version in the sequence name for {version_string} "
                                   "(group 1, e.g. 007) and {version_number} (7) (-version_regex)")
        name_form.addRow("Version regex", self.tk_version)
        name_layout.addLayout(name_form)
        name_help = QLabel("Custom keywords {NAME}: group 1 (else the whole match) of the first "
                           "match of REGEX in the sequence name, empty if none (-name_token). "
                           "They add to / override the presets file's name tokens; an empty "
                           "regex removes one.")
        name_help.setWordWrap(True)
        name_layout.addWidget(name_help)
        self.tk_name_tokens = PairTable(["Name", "Regex"])
        self.tk_name_tokens.setToolTip("Custom keywords {NAME} = a regex on the sequence name (-name_token NAME=REGEX); an empty regex removes the presets file's token")
        self.tk_name_tokens.setMinimumHeight(110)
        name_layout.addWidget(self.tk_name_tokens)
        self.tk_file_names = QLabel("")
        self.tk_file_names.setWordWrap(True)
        self.tk_file_names.setStyleSheet("color: #8a8a8a;")
        self.tk_file_names.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        name_layout.addWidget(self.tk_file_names)
        self.tk_split.addWidget(name_box)
        self.tk_split.setStretchFactor(0, 3)
        self.tk_split.setStretchFactor(1, 2)
        self._connect(self.tk_version, self.tk_name_tokens)
        self.tk_values = QLabel("")
        self.tk_values.setWordWrap(True)
        self.tk_values.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        values_box = QGroupBox("Values in the last preview")
        QVBoxLayout(values_box).addWidget(self.tk_values)
        layout.addWidget(values_box)

    def _load_tokens(self):
        name = self.tk_preset.currentData()
        if name is None:
            return
        old = self.state.tokens                  # the name tokens are not part of the preset
        self.state.tokens = core.tokens_from_preset(self.presets, name)
        self.state.tokens.version_regex, self.state.tokens.name_tokens = old.version_regex, old.name_tokens
        self._fill_tokens()
        self.status.setText("Smart tokens: {}".format(self.tk_preset.currentText()))
        self._changed()

    def _fill_token_presets(self):
        box = self.tk_preset
        box.blockSignals(True)
        box.clear()
        default = (self.presets.default_smart_tokens if self.presets else "") or "none"
        box.addItem("(file default: {})".format(default), "")
        box.addItem("none", "none")
        for name in (self.presets.smart_tokens if self.presets else {}):
            box.addItem(name, name)
        index = box.findData(self.state.tokens.preset)
        box.setCurrentIndex(index if index >= 0 else 0)
        fit_popup(box)
        box.blockSignals(False)

    def _fill_tokens(self):
        t = self.state.tokens
        self.loading = True
        index = self.tk_preset.findData(t.preset)
        self.tk_preset.setCurrentIndex(index if index >= 0 else 0)
        self.tk_table.set_rows(t.rows)
        name = t.preset or (self.presets.default_smart_tokens if self.presets else "")
        preset = self.presets.smart_tokens.get(name) if self.presets and name else None
        self.tk_notes.setText(preset.notes if preset else "")
        self.tk_version.setText(t.version_regex)
        self.tk_name_tokens.set_pairs(t.name_tokens)
        rules = self.presets.name_rules if self.presets else {}
        self.tk_file_names.setText(
            "In the presets file: " + ", ".join("{{{}}} = {}".format(r.name, r.regex) for r in rules.values())
            if rules else "The presets file has no name tokens.")
        self.loading = False
        self._show_token_values()

    def _read_tokens(self):
        try:
            self.state.tokens.rows = self.tk_table.rows()
        except ValueError as exc:
            self.status.setText("Tokens: {}; that row's conditions are not used yet".format(exc))
        if hasattr(self, "tk_version"):
            self.state.tokens.version_regex = self.tk_version.text().strip()
            self.state.tokens.name_tokens = self.tk_name_tokens.pairs()

    def _show_token_values(self):
        values = core.token_values(self.report, self.state.tokens)
        if not self.report:
            self.tk_values.setText("(after the first preview)")
            return
        self.tk_values.setText("   ".join(
            "{{{}}} = {}".format(name, repr(value) if value is not None else "(not set)")
            for name, value in values) or "(no smart tokens)")

    # color tab

    def _build_colour(self, page):
        form = QFormLayout(page)
        self.colour_state = QLabel("")
        self.colour_state.setWordWrap(True)
        self.in_convert = QCheckBox("Convert colour (-ci to -co)")
        self.in_convert.setToolTip("For EXR to EXR, and PNG / JPG / movie frames going through "
                                   "oiiotool: convert with the settings below. EXR to a movie or "
                                   "a display format is always converted.")
        # editable dropdowns: the names the OCIO config offers, or any name typed in
        self.in_ci, self.in_co, self.in_display, self.in_view, self.in_looks = (
            combo([], editable=True) for _ in range(5))
        for box in (self.in_ci, self.in_co, self.in_display, self.in_view, self.in_looks):
            box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            box.setMaxVisibleItems(25)
        self.in_clf, self.in_ocio = QLineEdit(), QLineEdit()
        self.in_clf.setToolTip('A CLF / LUT file applied instead of the OCIO conversion (-clf)')
        self.in_ci.lineEdit().setPlaceholderText("ACEScg (the default)")
        self.in_co.lineEdit().setPlaceholderText("out_rec709 (vfx-transcode's default)")
        self.in_display.lineEdit().setPlaceholderText("(none: -ci to -co)")
        self.in_view.lineEdit().setPlaceholderText("(the display's default view)")
        self.in_looks.lineEdit().setPlaceholderText("(none; several: comma separated)")
        self.ocio_info = QLabel("")
        self.in_ocio.setToolTip('The OCIO config (-ocio): a .ocio file or an ocio:// built-in; the dropdowns are filled from it')
        self.ocio_info.setWordWrap(True)
        self.ocio_info.setStyleSheet("color: #8a8a8a;")
        scan = QPushButton("Scan")
        scan.setToolTip("Read the OCIO config: its colour spaces, displays, views and looks "
                        "fill the dropdowns")
        scan.clicked.connect(lambda: self._scan_ocio(force=True))
        ocio_row = QHBoxLayout()
        ocio_row.addLayout(browse_row(self.in_ocio, "OCIO config", filters="OCIO (*.ocio)"), 1)
        ocio_row.addWidget(scan)
        form.addRow(self.colour_state)
        form.addRow("Colour preset", self._preset_row("colour"))
        form.addRow("", self.in_convert)
        form.addRow("-ci colour in", self.in_ci)
        form.addRow("-co colour out (display-referred)", self.in_co)
        form.addRow("-display", self.in_display)
        form.addRow("-view", self.in_view)
        form.addRow("-looks", self.in_looks)
        form.addRow("-clf (keywords ok)", browse_row(self.in_clf, "CLF", filters="CLF (*.clf)"))
        form.addRow("-ocio config", ocio_row)
        form.addRow("", self.ocio_info)
        note = QLabel("The dropdowns list what the -ocio config offers (read when it changes, "
                      "or with Scan); any other name can be typed. Starting values: -co Output - Rec.709 and the ACES 1.2 config in the "
                      "application's ocio folder. -display / -view (with an OCIO 2 config) or "
                      "-clf replace -co.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8a8a;")
        form.addRow(note)
        self._connect(self.in_convert, self.in_ci, self.in_co, self.in_display, self.in_view,
                      self.in_looks, self.in_clf, self.in_ocio)
        self.in_ocio.editingFinished.connect(self._scan_ocio)
        self.in_display.currentTextChanged.connect(lambda _t: self._fill_views())

    def _scan_ocio(self, force=False):
        """Fill the colour dropdowns from the -ocio config (cached; Scan reads it again)."""
        if force:
            core._OCIO_CACHE.clear()
        self.ocio_info.setText("Reading the OCIO config...")
        QApplication.processEvents()
        info = core.scan_ocio(self.in_ocio.text(), self.presets)
        self.ocio = info
        if info.error:
            self.ocio_info.setText("Not read: " + info.error)
            return
        how = "via oiiotool" if info.source == "oiiotool" else "read from the file"
        default = ""
        if info.default_display:
            default = "; default display {}, view {}".format(
                info.default_display, info.default_views.get(info.default_display, "?"))
        self.ocio_info.setText("{} ({}){}".format(info.describe(), how, default))
        spaces = info.colorspaces + [r for r in info.roles if r not in info.colorspaces]
        loading, self.loading = self.loading, True
        for box in (self.in_ci, self.in_co):
            self._refill(box, spaces)
        self._refill(self.in_display, list(info.displays))
        self._refill(self.in_looks, info.looks)
        self.loading = loading
        self._fill_views()

    @staticmethod
    def _refill(box, items):
        """New dropdown items, keeping what is typed or chosen; the first item is empty."""
        text = box.currentText()
        box.blockSignals(True)
        box.clear()
        box.addItem("")
        box.addItems(items)
        set_combo(box, text)
        box.blockSignals(False)

    def _fill_views(self):
        """The view dropdown lists the chosen display's views (all views without one)."""
        info = getattr(self, "ocio", None)
        if info is None or info.error:
            return
        loading, self.loading = self.loading, True
        self._refill(self.in_view, info.views(self.in_display.currentText().strip()))
        self.loading = loading

    # drag and drop

    @staticmethod
    def _dropped_path(event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        local = [u.toLocalFile() for u in urls if u.isLocalFile()]
        return local[0] if local else None

    def eventFilter(self, watched, event):
        """A file or folder dragged onto any part of this window is the new input."""
        kind = event.type()
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            widget = watched if isinstance(watched, QWidget) else None
            if widget is not None and widget.window() is self and self._dropped_path(event):
                if kind == QEvent.Type.Drop:
                    event.acceptProposedAction()
                    QTimer.singleShot(0, lambda path=self._dropped_path(event): self._drop(path))
                else:
                    event.acceptProposedAction()
                return True
        return super().eventFilter(watched, event)

    def _drop(self, path):
        """A dropped file: the input, if it is an image or a movie. A dropped folder: its one
        movie / image sequence / image if it holds just one; else the folder goes into the
        input field, so ... opens there."""
        self.tabs.setCurrentIndex(0)
        if os.path.isdir(path):
            found, what = core.media_in_folder(path)
            if found is None:
                self.in_input1.setText(path)
                self._read_all()
                self._input_changed()
                self.in_detected.setText("Dropped folder: {}. Press ... to pick a file in it.".format(what))
                self.status.setText("Dropped folder: " + what)
                return
            self.status.setText("Dropped folder: using " + what)
            path = str(found)
        elif not core.is_media(path):
            self.status.setText("Not an image or movie: {}".format(os.path.basename(path)))
            return
        self.in_input1.setText(path)
        self._input_changed()
        self._changed()
        if not self.auto_refresh.isChecked():
            self.refresh()

    def _show_input_type(self):
        """The Input type follows the input file and is greyed out; with no input it can be
        chosen."""
        i = self.state.input
        path = self.in_input1.text().strip()
        kind = core.detect_input_type(path) if path else i.input_type
        if path and kind:
            i.input_type = kind
        loading, self.loading = self.loading, True
        index = self.in_input_type.findData(kind or i.input_type)
        if index >= 0:
            self.in_input_type.setCurrentIndex(index)
        self.loading = loading
        self.in_input_type.setEnabled(not path)

    def _input_changed(self):
        """A new input (or movie first frame): find its sequence or probe the movie, and set
        the timeline to its frames."""
        path = self.in_input1.text().strip()
        name = Path(path).name if path else ""
        self.setWindowTitle("{} - vfx-transcode tuner".format(name) if name else "vfx-transcode tuner")
        info = core.describe_input(path, self.presets, self.in_first_frame.value())
        self.input_info = info
        if hasattr(self, "ch_tree"):
            QTimer.singleShot(0, self._read_channels)    # the Multichannel tab's parts and channels
        self.in_detected.setText(info.error or info.text)
        self._show_input_type()
        self.in_first_frame.setEnabled(info.kind == "movie")
        self.tc_base = (info.first, info.timecode, info.fps) if info.kind == "movie" else None
        self.timeline.set_range(info.first, info.last)
        if path != getattr(self, "frame_input", None):   # a new input: preview its middle frame
            self.frame_input = path
            self.state.input.frame = "middle"
        self.timeline.set_frame(self._frame_for_choice(self.state.input.frame, info))
        i = self.state.input
        self.timeline.set_trim(i.trim_in, i.trim_out)
        i.trim_in, i.trim_out = self.timeline.trim()      # a trim outside the new input goes
        self._update_pipeline()
        self.in_fps_info.setText("movie: {}".format(core.fps_text(info.fps)) if info.fps else "")
        self.in_par_info.setText("movie: {}".format(core.par_text(info.par)) if info.par else "")
        self._auto_timecode(info)

    def _frame_for_choice(self, choice, info):
        """The frame vfx-transcode will preview for middle / first / last / a number."""
        frames = sorted(info.frames) or [0]
        choice = (choice or "middle").strip()
        if choice == "first":
            return frames[0]
        if choice == "last":
            return frames[-1]
        if choice.lstrip("-").isdigit():
            wanted = int(choice)
            return min(frames, key=lambda f: (abs(f - wanted), f))
        middle = frames[0] + (frames[-1] - frames[0]) // 2
        return min(frames, key=lambda f: (abs(f - middle), f))

    def _detect_fps(self):
        fps, where = core.detect_fps(self.in_input1.text(), self.presets)
        if fps is None:
            self.in_fps_info.setText("not detected: " + where)
            return
        self.in_fps_info.setText("detected from the " + where)
        set_combo(self.in_fps, core.fps_text(fps))
        self._changed()

    def _detect_par(self):
        par, where = core.detect_par(self.in_input1.text(), self.presets)
        if par is None:
            self.in_par_info.setText("not detected: " + where)
            return
        self.in_par_info.setText("detected from the " + where)
        set_combo(self.in_par, core.par_text(par))
        self._changed()

    def _timecode_for(self, frame):
        """Timecode of a timeline frame: from the last preview (or the movie's probe)."""
        if not self.tc_base:
            return ""
        first, start_tc, fps = self.tc_base
        rate = self.state.input.fps or fps
        return core.frame_timecode(frame, first, start_tc, rate)

    def _trim_changed(self):
        """In / Out changed: they go to -in / -out (the preview's frame range and slate
        follow)."""
        self.state.input.trim_in, self.state.input.trim_out = self.timeline.trim()
        self.stale = {"preview": True, "filmstrip": True}
        self.update_outputs()
        if self.auto_refresh.isChecked():
            self.timer.start(REFRESH_DELAY_MS)

    def _timeline_frame(self, frame):
        """A frame picked on the timeline: preview it."""
        self._read_all()
        self.state.input.frame = str(frame)
        self.stale["preview"] = True
        self.update_outputs()
        if self.view_mode() != "filmstrip":
            self.refresh()

    def _compare_file_changed(self):
        """A compare file typed or picked: compare to it (unticks "compare to source")."""
        if self.in_input2.text().strip() and self.in_compare_source.isChecked():
            self.in_compare_source.setChecked(False)     # runs _compare_changed
        else:
            self._compare_changed()

    def _compare_changed(self):
        if self.loading:
            return
        self.state.input.input2 = self.in_input2.text().strip()
        self.state.input.compare_source = self.in_compare_source.isChecked()
        self._update_compare()

    def _compare_kind(self, path):
        """still / sequence / movie of a compare file (cached: finding a sequence lists its
        folder)."""
        key = (path, os.path.getmtime(path) if os.path.exists(path) else 0)
        if key not in self.compare_kinds:
            self.compare_kinds[key] = core.describe_input(path, self.presets).kind
        return self.compare_kinds[key]

    def _update_compare(self):
        """The compare frame for the previewed frame: a plain image as it is; else
        vfx-transcode makes it from the compare input (a movie, a sequence, an EXR) in the
        background, only when the frame or the compare settings changed."""
        path = core.compare_input(self.state)
        frame = (self.report or {}).get("frame")
        if not path or not os.path.isfile(path):
            self.compare_pixmap, self.compare_key = None, None
            self.show_images()
            return
        if core.compare_is_plain_image(path, self._compare_kind(path)):
            key = ("image", path, os.path.getmtime(path))
            if key != self.compare_key:
                image = QImage(path)
                self.compare_pixmap = None if image.isNull() else self._remember(image)
                self.compare_key = key
            self.show_images()
            return
        if frame is None:
            return                                # after the first preview
        tool = split_args(self.in_tool.text()) or core.vfx_transcode_command()
        key = tuple(core.compare_command(self.state, frame, "", tool))
        if key == self.compare_key:
            self.show_images()
            return
        if self.compare_process is not None and \
                self.compare_process.state() != QProcess.ProcessState.NotRunning:
            self.compare_process.kill()           # its result is no longer wanted
            self.compare_process.waitForFinished(2000)
        out = self.work_dir / "compare_{}.png".format(next(self.run_counter))
        cmd = core.compare_command(self.state, frame, out, tool)
        self.log.appendPlainText("\n> " + core.quote(cmd))
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(
            lambda p=process: self.log.appendPlainText(
                bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace").rstrip()))
        process.finished.connect(
            lambda code, _status, p=process, out=out, key=key: self._compare_finished(p, code, out, key))
        self.compare_process = process
        process.start(cmd[0], cmd[1:])

    def _compare_finished(self, process, code, out, key):
        if process is not self.compare_process:
            return                                # an older, killed run
        if code != 0 or not out.is_file():
            self.status.setText("The compare frame could not be made, see the log")
            return
        pixmap = self._load_pixmap(str(out))
        self.compare_pixmap, self.compare_key = pixmap, key
        self.show_images()

        self._scan_ocio()                     # the presets file may name another oiiotool
        self._changed()

    # transform tab

    def _build_transform(self, page):
        layout = QVBoxLayout(page)
        box = QGroupBox("Load from presets.json")
        form = QFormLayout(box)
        self.tr_preset = compact_combo(QComboBox(), 12)
        self.tr_preset.setToolTip('Pick a transform preset (oiiotool) to load its first sub-preset')
        self.tr_sub = compact_combo(QComboBox(), 8)
        self.tr_sub.setToolTip('Pick a sub-preset to load its steps; vfx-transcode picks the sub-preset for each input by its conditions')
        self.tr_active = QLabel("")
        self.tr_active.setWordWrap(True)
        self.tr_conditions = QLabel("")
        self.tr_conditions.setWordWrap(True)
        self.tr_preset.currentTextChanged.connect(self._transform_preset_selected)
        self.tr_sub.currentTextChanged.connect(self._transform_sub_selected)
        # picking from a dropdown loads it (activated: only the user's picks, not refills)
        self.tr_preset.activated.connect(lambda _i: self._load_transform())
        self.tr_sub.activated.connect(lambda _i: self._load_transform())
        form.addRow("Transform preset", self.tr_preset)
        form.addRow("Sub-preset", self.tr_sub)
        form.addRow("Conditions", self.tr_conditions)
        form.addRow("Active for this input", self.tr_active)
        save = QPushButton("Save as preset...")
        save.setToolTip("Save these oiiotool steps as a sub-preset (new or replacing one) "
                        "into the presets file")
        save.clicked.connect(lambda: self._save_preset("transform_sequence"))
        buttons = QHBoxLayout()
        buttons.addWidget(save)
        buttons.addStretch()
        form.addRow("", buttons)
        layout.addWidget(box)
        self.tr_use = QCheckBox("Use these oiiotool steps (off: the frames keep their size)")
        self.tr_use.setToolTip('On: the steps below run on every frame (-oiio_transform_args); off: no oiiotool steps')
        self.tr_steps = StepList()
        self.tr_steps.setToolTip('oiiotool steps, one per line, run in order; double-click to edit, Up / Down to reorder')
        layout.addWidget(self.tr_use)
        steps_label = QLabel("oiiotool steps, one per line (double-click to edit; keywords "
                             "like {input_width} and oiiotool {TOP.width} work):")
        steps_label.setWordWrap(True)
        layout.addWidget(steps_label)
        layout.addWidget(self.tr_steps)
        box, self.olb = self._letterbox_group(
            "Letterbox (oiiotool, drawn on the converted frames: no ffmpeg needed)")
        layout.addWidget(box)
        layout.addStretch()
        self._connect(self.tr_use, self.tr_steps)

    def _transform_preset_selected(self, name):
        self.tr_sub.blockSignals(True)
        self.tr_sub.clear()
        if self.presets and name in self.presets.transforms:
            self.tr_sub.addItems([s.name for s in self.presets.transforms[name].subs])
        fit_popup(self.tr_sub)
        self.tr_sub.blockSignals(False)
        self._transform_sub_selected(self.tr_sub.currentText())
        self._update_active_sub()

    def _transform_sub_selected(self, sub_name):
        text = ""
        name = self.tr_preset.currentText()
        if self.presets and name in self.presets.transforms:
            sub = next((s for s in self.presets.transforms[name].subs if s.name == sub_name), None)
            if sub:
                text = " and ".join(c.describe() for c in sub.conditions) or "always"
        self.tr_conditions.setText(text)

    def _load_transform(self):
        name, sub = self.tr_preset.currentText(), self.tr_sub.currentText()
        if not (self.presets and name in self.presets.transforms and sub):
            return
        self.state.transform = core.load_transform(self.presets, name, sub)
        self.status.setText("Loaded oiiotool transform {} / {}".format(name, sub))
        self._fill_transform()
        self._changed()

    def _active_sub(self):
        if not self.report:
            return None
        return core.active_sub_name(self.presets, self.state.transform.preset or
                                    self.tr_preset.currentText(), self.report.get("keywords"),
                                    self.report.get("input", {}).get("header"))

    def _update_active_sub(self):
        name = self.tr_preset.currentText()
        if not self.report:
            self.tr_active.setText("(refresh to find out)")
            return
        active = core.active_sub_name(self.presets, name, self.report.get("keywords"),
                                      self.report.get("input", {}).get("header"))
        self.tr_active.setText("<b>{}</b>".format(active) if active else "none matches")

    # burn-in tab

    def _build_burn(self, page):
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.bu_preset = compact_combo(QComboBox(), 12)
        self.bu_preset.setToolTip("Pick a burn-in preset to load it")
        self.bu_preset.activated.connect(lambda _i: self._load_burn())
        self.bu_enabled = QCheckBox("Burn-ins on")
        self.bu_enabled.setToolTip("Off: no burn-ins at all (-burn_preset none)")
        top.addWidget(self.bu_enabled)
        top.addWidget(QLabel("   Burn-in preset"))
        top.addWidget(self.bu_preset, 1)
        top.addWidget(self._save_button("burnin"))
        layout.addLayout(top)
        self._connect(self.bu_enabled)

        style = QGroupBox("Style for all places")
        grid = QHBoxLayout(style)
        self.bu_color = ColorButton()
        self.bu_color.setToolTip('Text colour of all places (-burn_color)')
        self.bu_opacity = opacity_spin()
        self.bu_opacity.setToolTip('Opacity of the text, 0-1 (-burn_opacity)')
        self.bu_size = QLineEdit()
        self.bu_size.setToolTip('Text size: % of the picture height (1.33%) or pixels (-burn_size)')
        self.bu_box = QCheckBox("box")
        self.bu_box.setToolTip('A dark box behind the text (-burn_box)')
        for label, widget in (("colour", self.bu_color), ("opacity", self.bu_opacity),
                              ("size", self.bu_size), ("", self.bu_box)):
            if label:
                grid.addWidget(QLabel(label))
            grid.addWidget(widget)
        layout.addWidget(style)

        places = QGroupBox("Places (tick 'own' to style one place differently)")
        grid = QGridLayout(places)
        for column, head in enumerate(("", "text", "own", "col", "opac", "size", "box")):
            grid.addWidget(QLabel("<i>{}</i>".format(head)), 0, column)
        self.bu_places = {}
        for row, place in enumerate(BURN_PLACES, 1):
            widgets = {"text": QLineEdit(), "own": QCheckBox(), "color": ColorButton(compact=True),
                       "opacity": opacity_spin(compact=True), "size": QLineEdit(), "box": QCheckBox()}
            widgets["text"].setMinimumWidth(40)
            widgets["text"].setToolTip('The text of this place; keywords work, e.g. {clean_name} {current_frame} {timecode}')
            widgets["size"].setFixedWidth(40)
            widgets["size"].setToolTip("This place's text size (with own ticked)")
            grid.addWidget(QLabel(PLACE_NAMES[place]), row, 0)
            widgets["own"].setToolTip('Give this place its own style (colour, opacity, size, box)')
            for column, key in enumerate(("text", "own", "color", "opacity", "size", "box"), 1):
                grid.addWidget(widgets[key], row, column)
            self.bu_places[place] = widgets
            widgets["color"].setToolTip("This place's text colour (with own ticked)")
            self._connect(*widgets.values())
            widgets["opacity"].setToolTip("This place's opacity, 0-1 (with own ticked)")
        grid.setColumnStretch(1, 1)
        widgets["box"].setToolTip("A dark box behind this place's text (with own ticked)")
        layout.addWidget(places)

        margins = QGroupBox("Margins (pixels or %; left/right % of width, top/bottom % of height)")
        row = QHBoxLayout(margins)
        self.bu_margins = {}
        for side in MARGIN_SIDES:
            edit = QLineEdit()
            edit.setToolTip('Space between the picture edge and the burn-ins on this side: pixels (20) or percent (2%)')
            edit.setFixedWidth(60)
            row.addWidget(QLabel(side))
            row.addWidget(edit)
            self.bu_margins[side] = edit
            self._connect(edit)
        layout.addWidget(margins)
        self.bu_font = QLineEdit()
        self.bu_font.setToolTip('A .ttf / .otf font file for the burn-ins (-burn_font); empty: the bundled font')
        font_row = QFormLayout()
        font_row.addRow("Font file", browse_row(self.bu_font, "Font", filters="Fonts (*.ttf *.otf)"))
        layout.addLayout(font_row)
        keywords = QLabel("Keywords: {clean_name} {current_frame} {frame_start} {frame_end} "
                          "{tc} {timecode} {fps} {date} {preset} {meta:ATTR} "
                          "{version_number} {namesplit1} ...   \\n = new line")
        keywords.setWordWrap(True)
        layout.addWidget(keywords)
        layout.addStretch()
        self._connect(self.bu_color, self.bu_opacity, self.bu_size, self.bu_box, self.bu_font)

    def _load_burn(self):
        name = self.bu_preset.currentText()
        preset = self.presets.burnins.get(name) if self.presets else None
        if preset is None:                        # the "(pick a preset ...)" entry
            return
        enabled = self.state.burn.enabled
        self.state.burn = core.burn_from_preset(preset)
        self.state.burn.enabled = enabled
        self._fill_burn()
        self._loaded_status("burn-in", preset)
        self._changed()

    # slate tab

    def _build_slate(self, page):
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.sl_enabled = QCheckBox("Slate")
        self.sl_enabled.setToolTip('Put a slate frame before the movie')
        self.sl_preset = compact_combo(QComboBox(), 12)
        self.sl_preset.setToolTip("Pick a slate preset to load it (and switch the slate on)")
        self.sl_preset.activated.connect(lambda _i: self._load_slate())
        top.addWidget(self.sl_enabled)
        top.addWidget(self.sl_preset, 1)
        top.addWidget(self._save_button("slate"))
        layout.addLayout(top)
        form = QFormLayout()
        self.sl_title, self.sl_subtitle = QLineEdit(), QLineEdit()
        self.sl_title.setToolTip("The slate's title; keywords work, e.g. {clean_name}")
        form.addRow("Title", self.sl_title)
        self.sl_subtitle.setToolTip('The line under the title; keywords work')
        form.addRow("Subtitle", self.sl_subtitle)
        layout.addLayout(form)
        self.sl_fields = PairTable(["Label", "Value (keywords)"])
        self.sl_fields.setToolTip("The slate's rows: a label and its value (keywords work, e.g. {timecode}, {version_string})")
        layout.addWidget(self.sl_fields)
        self.sl_thumbnail = combo(["middle", "first", "last", "none"], editable=True)
        self.sl_thumbnail.setToolTip('The picture on the slate: a frame of the movie (middle, first, last or a number) or none')
        self.sl_hide_empty = QCheckBox("hide rows whose value is empty")
        self.sl_hide_empty.setToolTip('Leave out rows whose value comes out empty for this input')
        self.sl_rule = QCheckBox("line under the title")
        self.sl_rule.setToolTip('Draw a line under the title')
        self.sl_burnin = QCheckBox("burn-ins on the slate too")
        self.sl_burnin.setToolTip('Draw the burn-ins on the slate frame as well (an option, not part of the slate preset)')
        self.sl_title_size, self.sl_subtitle_size, self.sl_size = QLineEdit(), QLineEdit(), QLineEdit()
        self.sl_title_size.setToolTip('Title text size: % of the picture height or pixels')
        self.sl_scale = QDoubleSpinBox()
        self.sl_subtitle_size.setToolTip('Subtitle text size: % of the picture height or pixels')
        self.sl_scale.setRange(0.05, 1.0)
        self.sl_scale.setSingleStep(0.05)
        self.sl_scale.setToolTip('Size of the whole slate layout: 1 fills the picture, 0.5 is half as big')
        self.sl_valign = combo(["center", "top"])
        self.sl_size.setToolTip('Row text size: % of the picture height or pixels')
        self.sl_valign.setToolTip('Vertical placement of the slate content')
        # three blocks of two rows on one grid: label / field pairs in three columns, so the
        # blocks line up with each other
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        def pair(row, column, label, widget):
            if label:
                grid.addWidget(QLabel(label), row, column * 2)
            grid.addWidget(widget, row, column * 2 + 1)

        pair(0, 0, "Thumbnail", self.sl_thumbnail)
        grid.addWidget(self.sl_hide_empty, 0, 2, 1, 4)
        grid.addWidget(self.sl_rule, 1, 0, 1, 2)
        grid.addWidget(self.sl_burnin, 1, 2, 1, 4)
        grid.setRowMinimumHeight(2, 6)
        pair(3, 0, "Title size", self.sl_title_size)
        pair(3, 1, "Subtitle size", self.sl_subtitle_size)
        pair(3, 2, "Row size", self.sl_size)
        pair(4, 0, "Scale", self.sl_scale)
        pair(4, 1, "Vertical", self.sl_valign)
        grid.setRowMinimumHeight(5, 6)
        self.sl_colors = {}
        for index, (key, label) in enumerate((("background", "Background"), ("title_color", "Title"),
                                              ("subtitle_color", "Subtitle"), ("value_color", "Values"),
                                              ("label_color", "Labels"), ("line_color", "Line"))):
            self.sl_colors[key] = ColorButton()
            self.sl_colors[key].setToolTip("{} colour: click to pick it".format(label))
            pair(6 + index // 3, index % 3, label + " colour", self.sl_colors[key])
            self._connect(self.sl_colors[key])
        grid.setColumnStretch(6, 1)
        layout.addLayout(grid)
        layout.addWidget(self._build_slate_logo())
        layout.addStretch()
        self._connect(self.sl_enabled, self.sl_title, self.sl_subtitle, self.sl_fields,
                      self.sl_thumbnail, self.sl_hide_empty, self.sl_rule, self.sl_burnin,
                      self.sl_title_size, self.sl_subtitle_size, self.sl_size, self.sl_scale,
                      self.sl_valign)

    def _build_slate_logo(self):
        """Vendor logo: an image (PNG with alpha) placed on the slate, with its position,
        height and distance from the edges."""
        box = QGroupBox("Logo")
        form = QFormLayout(box)
        self.sl_logo = QLineEdit()
        self.sl_logo.setToolTip('A vendor logo image on the slate, best a PNG with alpha (-slate_logo)')
        self.sl_logo.setPlaceholderText("none - a PNG with alpha (-slate_logo)")
        self.sl_logo_position = QComboBox()
        self.sl_logo_position.setToolTip('Where the logo goes on the slate (-slate_logo_position)')
        for key, label in core.LOGO_POSITION_NAMES.items():
            self.sl_logo_position.addItem(label, key)
        self.sl_logo_size = QLineEdit()
        self.sl_logo_size.setToolTip("Logo height: % of the picture height (10%) or pixels; the "
                                     "width follows the logo's proportions")
        self.sl_logo_x, self.sl_logo_y = QLineEdit(), QLineEdit()
        self.sl_logo_x.setToolTip("Distance from the left / right edge: % of the width or pixels")
        self.sl_logo_y.setToolTip("Distance from the top / bottom edge: % of the height or pixels")
        margins = QHBoxLayout()
        margins.addWidget(QLabel("x"))
        margins.addWidget(self.sl_logo_x)
        margins.addSpacing(12)
        margins.addWidget(QLabel("y"))
        margins.addWidget(self.sl_logo_y)
        margins.addStretch(1)
        form.addRow("Image", browse_row(self.sl_logo, "Logo",
                                        filters="Images (*.png *.tif *.tiff *.jpg *.jpeg)"))
        form.addRow("Position", self.sl_logo_position)
        form.addRow("Height", self.sl_logo_size)
        form.addRow("From the edges", margins)
        self.sl_logo.editingFinished.connect(self._logo_state)
        self._connect(self.sl_logo, self.sl_logo_position, self.sl_logo_size, self.sl_logo_x,
                      self.sl_logo_y)
        return box

    def _logo_state(self):
        on = bool(self.sl_logo.text().strip())
        for w in (self.sl_logo_position, self.sl_logo_size, self.sl_logo_x, self.sl_logo_y):
            w.setEnabled(on)

    # named presets: load (colour, ffmpeg transform) and save (all areas)

    def _preset_row(self, area):
        """Preset dropdown + Load + Save as... for the Color and Transform tabs."""
        box = compact_combo(QComboBox(), 12)
        box.setToolTip("Pick a preset from the presets file to load it")
        if area == "colour":
            self.co_preset = box
        else:
            self.ff_preset = box
        box.activated.connect(lambda _i: self._load_named(area))
        row = QHBoxLayout()
        row.addWidget(box, 1)
        row.addWidget(self._save_button(area))
        return row

    def _save_button(self, area):
        button = QPushButton("Save as preset...")
        button.setToolTip("Save these settings as a named {} preset into the presets file "
                          "(the studio preferences, when vfx-transcode.ini names them)"
                          .format(core.PRESET_AREAS[area].lower()))
        button.clicked.connect(lambda: self._save_preset(area))
        return button

    def _load_named(self, area):
        if area == "colour":
            preset = self.presets.colours.get(self.co_preset.currentText()) if self.presets else None
            if preset is None:
                return
            core.colour_from_preset(self.state.input, preset)
            self._fill_colour()
        else:
            preset = self.presets.ff_transforms.get(self.ff_preset.currentText()) if self.presets else None
            if preset is None:
                return
            self.state.ff_transform = core.ff_transform_from_preset(preset)
            self._fill_letterbox()
        self.status.setText("Loaded {} preset {}{}".format(
            core.PRESET_AREAS[area].lower(), preset.name, ": " + preset.notes if preset.notes else ""))
        self._changed()

    def _fill_colour(self):
        self.loading = True
        i = self.state.input
        for box, value in ((self.in_ci, i.colorin), (self.in_co, i.colorout),
                           (self.in_display, i.display), (self.in_view, i.view),
                           (self.in_looks, i.looks)):
            index = box.findText(value)
            if index >= 0:
                box.setCurrentIndex(index)
            else:
                box.setEditText(value)
        self.in_clf.setText(i.clf)
        self.in_ocio.setText(i.ocio)
        self.loading = False

    def _current_preset_name(self, area):
        s = self.state
        return {"colour": s.input.colour_preset, "transform": s.ff_transform.preset,
                "smart_tokens": s.tokens.preset if s.tokens.preset not in ("", "none") else "",
                "transform_sequence": s.transform.preset, "burnin": s.burn.preset,
                "slate": s.slate.preset, "filmstrip": s.filmstrip.preset,
                "thumbnail": s.thumb.preset, "encode_preset": s.encode_edit.preset,
                "metadata": s.metadata.preset if s.metadata.preset not in ("", "none") else "",
                "sequence_preset": s.sequence_edit.preset,
                "destination_path": s.dest.path_preset, "destination_name": s.dest.name_preset,
                "channel_order": s.channels.order_preset}[area]

    def _save_preset(self, area):
        """Ask for a name (and sub-preset, notes), then save this area's settings into the
        presets file in use, multi-user safe. The new preset is selected afterwards."""
        self._read_all()
        if area == "channel_order":
            self.state.channels.order = self._order_items()   # what the list shows is saved
            if not self.state.channels.order:
                QMessageBox.information(self, "Save preset", "The list is empty: add channel sets first.")
                return
        if area == "transform_sequence" and not self.state.transform.args:
            QMessageBox.information(self, "Save preset", "There are no oiiotool steps to save.")
            return
        if area in ("encode_preset", "sequence_preset"):
            problem = (core.encode_problem(self.state.encode_edit) if area == "encode_preset"
                       else core.sequence_problem(self.state.sequence_edit))
            if problem:
                QMessageBox.information(self, "Save preset", problem)
                return
        if area == "smart_tokens" and not self.state.tokens.rows:
            QMessageBox.information(self, "Save preset", "There are no smart token definitions to save.")
            return
        path = self._presets_path()
        presets = self.presets
        name = self._current_preset_name(area)
        sub = self.state.transform.sub if area == "transform_sequence" else None
        subs_for = None
        if area == "transform_sequence":
            subs_for = lambda n: [s.name for s in presets.transforms[n].subs] if n in presets.transforms else []
        dialog = SavePresetDialog(
            self, "Save {} preset".format(core.PRESET_AREAS[area].lower()),
            core.preset_names(presets, area), name, core.preset_notes(presets, area, name, sub),
            subs_for, sub, lambda n, s: core.preset_notes(presets, area, n, s), str(path))
        while True:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            name, sub, notes = dialog.values()
            if not name or (area == "transform_sequence" and not sub):
                QMessageBox.information(self, "Save preset", "Type a name.")
                continue
            if presets_store.exists(presets, area, name, sub):
                what = '"{}"{}'.format(name, ' sub-preset "{}"'.format(sub) if sub else "")
                answer = QMessageBox.question(self, "Save preset", "Replace the preset {}?".format(what))
                if answer != QMessageBox.StandardButton.Yes:
                    continue
            break
        entry = core.preset_entry(self.state, area, presets, notes, sub)
        if area == "transform_sequence" and presets and name in presets.transforms:
            old = next((s for s in presets.transforms[name].subs if s.name == sub), None)
            if old is not None:                   # replacing a sub keeps its conditions
                entry["conditions"] = [{"left": c.left, "op": c.op, "right": c.right}
                                       for c in old.conditions]
        try:
            result = presets_store.save_preset(path, area, name, entry, sub)
        except (OSError, TranscodeError) as exc:
            QMessageBox.warning(self, "Save preset", "Could not save into\n{}\n\n{}".format(path, exc))
            return
        self._load_presets()
        self._fill_preset_lists()
        core.mark_saved(self.state, area, name, sub)
        if area == "burnin" and name in self.presets.burnins:
            enabled = self.state.burn.enabled           # as saved, so -burn_preset NAME is used
            self.state.burn = core.burn_from_preset(self.presets.burnins[name])
            self.state.burn.enabled = enabled
            self._fill_burn()
        box = {"colour": self.co_preset, "transform": self.ff_preset, "smart_tokens": self.tk_preset,
               "transform_sequence": self.tr_preset, "burnin": self.bu_preset,
               "slate": self.sl_preset, "filmstrip": self.fs_preset,
               "thumbnail": self.th_preset, "encode_preset": self.en_preset,
               "metadata": self.md_preset,
               "sequence_preset": self.sq_preset,
               "destination_path": self.de_path_preset, "destination_name": self.de_name_preset,
               "channel_order": self.ch_order_preset}[area]
        set_combo(box, name)
        if area == "transform_sequence":
            set_combo(self.tr_sub, sub)
        if area in ("encode_preset", "sequence_preset"):
            self._use_saved_encode(area, name)
        self.status.setText('Saved {} preset "{}"{} into {}{}'.format(
            core.PRESET_AREAS[area].lower(), name, ' / "{}"'.format(sub) if sub else "", result.path,
            " (previous version: {})".format(result.backup.name) if result.backup else ""))
        self._changed()

    def _load_slate(self):
        name = self.sl_preset.currentText()
        spec = self.presets.slates.get(name) if self.presets else None
        if spec is None:
            return
        burnin = self.state.slate.burnin          # an option, not part of the preset
        self.state.slate = core.slate_from_spec(spec, name)
        self.state.slate.burnin = burnin
        self._fill_slate()
        self._loaded_status("slate", spec)
        self._changed()

    # filmstrip tab

    def _build_filmstrip(self, page):
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.fs_preset = compact_combo(QComboBox(), 12)
        self.fs_preset.setToolTip("Pick a filmstrip preset to load it")
        self.fs_preset.activated.connect(lambda _i: self._load_filmstrip())
        top.addWidget(QLabel("Preset"))
        top.addWidget(self.fs_preset, 1)
        top.addWidget(self._save_button("filmstrip"))
        layout.addLayout(top)
        form = QFormLayout()
        self.fs_enabled = QCheckBox("add the filmstrip to the full command (-of)")
        self.fs_enabled.setToolTip('Make a filmstrip with the output: -of in the full command, Process and super-presets')
        self.fs_tiles = QSpinBox()
        self.fs_tiles.setToolTip('How many frames, evenly spaced from the first to the last (-filmstrip_tiles)')
        self.fs_tiles.setRange(1, 1000)
        self.fs_width, self.fs_height = QSpinBox(), QSpinBox()
        self.fs_width.setToolTip('Tile width in pixels; each frame is fitted in and padded (-filmstrip_tile)')
        for spin in (self.fs_width, self.fs_height):
            spin.setRange(16, 8192)
        size = QHBoxLayout()
        self.fs_height.setToolTip('Tile height in pixels (-filmstrip_tile)')
        size.addWidget(self.fs_width)
        size.addWidget(QLabel("x"))
        size.addWidget(self.fs_height)
        self.fs_columns, self.fs_rows = QSpinBox(), QSpinBox()
        self.fs_columns.setToolTip('Columns of the grid; auto follows the tiles (-filmstrip_grid)')
        for spin in (self.fs_columns, self.fs_rows):
            spin.setRange(0, 1000)
            spin.setSpecialValueText("auto")
        grid = QHBoxLayout()
        self.fs_rows.setToolTip('Rows of the grid; auto = as many as needed (-filmstrip_grid)')
        grid.addWidget(self.fs_columns)
        grid.addWidget(QLabel("x"))
        grid.addWidget(self.fs_rows)
        self.fs_quality = QSpinBox()
        self.fs_quality.setToolTip('jpg quality of the filmstrip, 1-100 (-filmstrip_quality)')
        self.fs_quality.setRange(1, 100)
        self.fs_source = combo(list(core.FILMSTRIP_SOURCES))
        self.fs_source.setToolTip("source: the source frames with the movie's look (fast); intermediates: the converted frames; movie: frames of the encoded movie (burn-ins, letterbox)")
        self.fs_slate = QCheckBox("the slate as the first tile (switch the slate on in the Slate tab)")
        self.fs_slate.setToolTip('Put the slate in the first tile (needs the slate on in the Slate tab)')
        form.addRow("", self.fs_enabled)
        form.addRow("Tiles (frames)", self.fs_tiles)
        form.addRow("Tile size", size)
        form.addRow("Grid columns x rows", grid)
        form.addRow("jpg quality", self.fs_quality)
        form.addRow("Tiles from", self.fs_source)
        form.addRow("", self.fs_slate)
        layout.addLayout(form)
        note = QLabel("Grid: both auto = one row; one auto = as many as needed. "
                      "<i>source</i>: only the filmstrip is made (fast). <i>intermediates</i>, "
                      "<i>movie</i> or the slate: the Filmstrip view makes the whole movie too, "
                      "with burn-ins, slate and letterbox.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self._connect(self.fs_enabled, self.fs_tiles, self.fs_width, self.fs_height,
                      self.fs_columns, self.fs_rows, self.fs_quality, self.fs_source, self.fs_slate)

    def _load_filmstrip(self):
        name = self.fs_preset.currentText()
        preset = self.presets.filmstrips.get(name) if self.presets else None
        if preset is None:
            return
        self.state.filmstrip = core.filmstrip_from_preset(preset, name, self.state.filmstrip.enabled)
        self._fill_filmstrip()
        self._loaded_status("filmstrip", preset)
        self._changed()

    def _loaded_status(self, what, preset):
        notes = getattr(preset, "notes", "") or getattr(preset, "description", "")
        self.status.setText("Loaded {} preset {}{}".format(what, preset.name,
                                                           ": " + notes if notes else ""))

    def _fill_filmstrip(self):
        self.loading = True
        f = self.state.filmstrip
        self.fs_enabled.setChecked(f.enabled)
        self.fs_tiles.setValue(f.tiles)
        self.fs_width.setValue(f.tile_width)
        self.fs_height.setValue(f.tile_height)
        self.fs_columns.setValue(f.columns)
        self.fs_rows.setValue(f.rows)
        self.fs_quality.setValue(f.quality)
        set_combo(self.fs_source, f.source)
        self.fs_slate.setChecked(f.slate)
        self.loading = False

    # destination tab: where the output, the thumbnail and the filmstrip go

    def _build_destination(self, page):
        layout = QVBoxLayout(page)
        box = QGroupBox("Output (movie or frames)")
        grid = QGridLayout(box)
        self.de_path_preset = QComboBox()
        self.de_path_preset.setToolTip("Path presets (\"destination_paths\" in the presets file). "
                                       "Picking one fills the Path.")
        self.de_path_preset.activated.connect(self._dest_path_picked)
        self.de_name_preset = QComboBox()
        self.de_name_preset.setToolTip("Name presets (\"destination_names\" in the presets file). "
                                       "Picking one fills the Name.")
        self.de_name_preset.activated.connect(self._dest_name_picked)
        self.de_path = QLineEdit("./")
        self.de_path.setToolTip("Output folder, keywords allowed: ./ = the input's folder, ../ the "
                                "folder above it, {sourcepath}, {sourcefolder-1}, {base_folder} ...")
        self.de_name = QLineEdit()
        self.de_name.setPlaceholderText("same as the input")
        self.de_name.setToolTip("Output name without extension, keywords allowed: "
                                "{clean_name}_transcoded ... The extension comes from the output "
                                "type; a sequence gets .####.EXT")
        browse = QPushButton("...")
        browse.setToolTip('Pick the output folder')
        browse.setFixedWidth(30)
        browse.clicked.connect(self._dest_browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.de_path_preset, 1)
        path_row.addWidget(self._save_button("destination_path"))
        path_row.addWidget(self._delete_button("destination_path"))
        name_row = QHBoxLayout()
        name_row.addWidget(self.de_name_preset, 1)
        name_row.addWidget(self._save_button("destination_name"))
        name_row.addWidget(self._delete_button("destination_name"))
        grid.addWidget(QLabel("Path preset"), 0, 0)
        grid.addLayout(path_row, 0, 1, 1, 2)
        grid.addWidget(QLabel("Path"), 1, 0)
        grid.addWidget(self.de_path, 1, 1)
        grid.addWidget(browse, 1, 2)
        grid.addWidget(QLabel("Name preset"), 2, 0)
        grid.addLayout(name_row, 2, 1, 1, 2)
        grid.addWidget(QLabel("Name"), 3, 0)
        grid.addWidget(self.de_name, 3, 1, 1, 2)
        # test values for {custom_destination_path} / _name (process app users type their own)
        self.de_custom_path, self.de_custom_name = QLineEdit(), QLineEdit()
        self.de_custom_path.setToolTip("A value to test {custom_destination_path} with; the process app's user types the real one")
        self.de_custom_path.setPlaceholderText("value to test {custom_destination_path} with")
        self.de_custom_name.setToolTip("A value to test {custom_destination_name} with; the process app's user types the real one")
        self.de_custom_name.setPlaceholderText("value to test {custom_destination_name} with")
        self.de_custom_path_label = QLabel("Custom path")
        self.de_custom_name_label = QLabel("Custom name")
        grid.addWidget(self.de_custom_path_label, 4, 0)
        grid.addWidget(self.de_custom_path, 4, 1, 1, 2)
        grid.addWidget(self.de_custom_name_label, 5, 0)
        grid.addWidget(self.de_custom_name, 5, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        self.de_preview = QLabel("")
        self.de_preview.setStyleSheet("color: #8a8a8a;")
        self.de_preview.setWordWrap(True)
        self.de_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.de_preview, 6, 0, 1, 3)
        self.sp_type_label = QLabel("")
        self.sp_type_label.setStyleSheet("color: #8a8a8a;")
        grid.addWidget(self.sp_type_label, 7, 0, 1, 3)
        layout.addWidget(box)

        # thumbnail and filmstrip: a path and a name from the same presets, or as the output
        self.de_stills = {}
        for key, title in (("thumb", "Thumbnail (Thumb tab)"), ("film", "Filmstrip (Filmstrip tab)")):
            box = QGroupBox(title)
            form = QFormLayout(box)
            path_box, name_box = QComboBox(), QComboBox()
            for combo in (path_box, name_box):
                combo.activated.connect(lambda _i: self._dest_changed())
            path_box.setToolTip("Where it goes: a path preset, or the output's path.")
            name_box.setToolTip("Its name: a name preset, or the output's name"
                                + (" + _filmstrip." if key == "film" else "."))
            form.addRow("Path", path_box)
            form.addRow("Name", name_box)
            self.de_stills[key] = (path_box, name_box)
            layout.addWidget(box)
        layout.addStretch()

        self.de_path.textEdited.connect(
            lambda _t: self.de_path_preset.setCurrentIndex(self.de_path_preset.count() - 1))
        self.de_name.textEdited.connect(
            lambda _t: self.de_name_preset.setCurrentIndex(self.de_name_preset.count() - 1))
        for edit in (self.de_path, self.de_name, self.de_custom_path, self.de_custom_name):
            edit.editingFinished.connect(self._dest_changed)
        for edit in (self.de_path, self.de_name):
            edit.textChanged.connect(lambda _t: self._show_custom_fields())
        self._fill_destination_menus()
        self._show_custom_fields()

    def _delete_button(self, area):
        button = QPushButton("Delete")
        button.setToolTip("Delete the selected {} preset from the presets file".format(
            core.PRESET_AREAS[area].lower()))
        button.clicked.connect(lambda: self._delete_dest_preset(area))
        return button

    def _fill_destination_menus(self):
        """The Path and Name menus from the presets file; the thumbnail and filmstrip menus
        start with "(as the output)"."""
        if not hasattr(self, "de_path_preset"):
            return
        d = self.state.dest
        paths, names = core.path_menu(self.presets), core.name_menu(self.presets)
        for combo, menu, value, folder in ((self.de_path_preset, paths, d.path, True),
                                           (self.de_name_preset, names, d.name, False)):
            combo.blockSignals(True)
            combo.clear()
            for label, template in menu.items():
                combo.addItem(label, template)
                if template is not None:
                    combo.setItemData(combo.count() - 1, template or "(empty)", Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(core.preset_index(menu, value, folder))
            combo.blockSignals(False)
        for key, (path_box, name_box) in self.de_stills.items():
            for combo, menu, value in ((path_box, paths, getattr(d, key + "_path")),
                                       (name_box, names, getattr(d, key + "_name"))):
                combo.blockSignals(True)
                combo.clear()
                combo.addItem(core.SAME_AS_OUTPUT, "")
                for label, template in menu.items():
                    if template:                  # no "one folder", "custom" or empty name
                        combo.addItem(label, template)
                        combo.setItemData(combo.count() - 1, template, Qt.ItemDataRole.ToolTipRole)
                index = combo.findData(value)
                if index < 0:                     # from a super-preset, not a preset here
                    combo.addItem("{} (not a preset)".format(value), value)
                    index = combo.count() - 1
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def _fill_destination(self):
        d = self.state.dest
        self.loading = True
        self.de_path.setText(d.path or "./")
        self.de_name.setText(d.name)
        self.de_custom_path.setText(d.custom_path)
        self.de_custom_name.setText(d.custom_name)
        self._fill_destination_menus()
        self.loading = False
        self._show_custom_fields()

    def _read_destination(self):
        d = self.state.dest
        d.path, d.name = self.de_path.text().strip() or "./", self.de_name.text().strip()
        d.custom_path, d.custom_name = self.de_custom_path.text().strip(), self.de_custom_name.text().strip()
        d.thumb_path = self.de_stills["thumb"][0].currentData() or ""
        d.thumb_name = self.de_stills["thumb"][1].currentData() or ""
        d.film_path = self.de_stills["film"][0].currentData() or ""
        d.film_name = self.de_stills["film"][1].currentData() or ""
        label = self.de_path_preset.currentText()
        d.path_preset = label if label not in (core.CUSTOM, core.ONE_FOLDER) else ""
        label = self.de_name_preset.currentText()
        d.name_preset = label if label != core.CUSTOM else ""

    def _show_custom_fields(self):
        """The Custom path / name fields, only when a destination uses their keyword."""
        if not hasattr(self, "de_stills"):
            return
        d = self.state.dest
        texts = [self.de_path.text(), self.de_name.text(), d.thumb_path, d.thumb_name, d.film_path, d.film_name]
        uses_path, uses_name = core.uses_custom(*texts)
        for widget in (self.de_custom_path_label, self.de_custom_path):
            widget.setVisible(uses_path)
        for widget in (self.de_custom_name_label, self.de_custom_name):
            widget.setVisible(uses_name)

    def _dest_changed(self):
        if self.loading:
            return
        self._read_destination()
        self._show_custom_fields()
        self._show_destination_preview()
        self.update_outputs()

    def _show_destination_preview(self):
        if not hasattr(self, "de_preview"):
            return
        d = self.state.dest
        lines = ["-o " + core.output_arg(d.output(), self.state.input.output_type)]
        if self.state.thumb.enabled:
            lines.append("-ot " + d.thumbnail())
        if self.state.filmstrip.enabled:
            lines.append("-of " + d.filmstrip())
        self.de_preview.setText("\n".join(lines))

    def _output(self):
        """-o from the Destination tab (its values are read into the state first)."""
        if hasattr(self, "de_path"):
            self._read_destination()
        return self.state.dest.output()

    def _apply_destination(self, dest):
        """A super-preset's destination (core.DestinationState) into the tab; the custom test
        values stay."""
        d = self.state.dest
        dest.custom_path, dest.custom_name = d.custom_path, d.custom_name
        self.state.dest = dest
        self._fill_destination()
        self._dest_changed()

    def _dest_path_picked(self, index):
        value = self.de_path_preset.itemData(index)
        if self.de_path_preset.itemText(index) == core.ONE_FOLDER:
            self._dest_browse()
        elif value is not None:
            self.de_path.setText(value)
            self._dest_changed()

    def _dest_name_picked(self, index):
        value = self.de_name_preset.itemData(index)
        if value is not None:
            self.de_name.setText(value)
            self._dest_changed()

    def _dest_browse(self):
        text = self.de_path.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Output folder", text if os.path.isdir(text) else "")
        if path:
            self.de_path.setText(path)
            self.de_path_preset.setCurrentIndex(self.de_path_preset.findText(core.ONE_FOLDER))
            self._dest_changed()

    def _delete_dest_preset(self, area):
        combo = self.de_path_preset if area == "destination_path" else self.de_name_preset
        name = combo.currentText()
        if combo.currentData() is None or name == core.ONE_FOLDER:
            QMessageBox.information(self, "Delete preset", "Pick a preset in the menu first.")
            return
        path = self._presets_path()
        answer = QMessageBox.question(self, "Delete preset", 'Delete the {} preset "{}" from\n{}?'.format(
            core.PRESET_AREAS[area].lower(), name, path))
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = presets_store.delete_preset(path, area, name)
        except (OSError, TranscodeError) as exc:
            QMessageBox.warning(self, "Delete preset", "Could not delete from\n{}\n\n{}".format(path, exc))
            return
        self._load_presets()
        self._fill_preset_lists()
        self.status.setText('Deleted {} preset "{}" from {}{}'.format(
            core.PRESET_AREAS[area].lower(), name, result.path,
            " (previous version: {})".format(result.backup.name) if result.backup else ""))

    # output frame rate (Output tab)

    def _out_fps_changed(self):
        if self.loading:
            return
        self._changed()
        self._show_out_fps()

    def _show_out_fps(self):
        """What the chosen rate does, against the input's rate when it is known."""
        i = self.state.input
        if not i.output_fps:
            self.out_fps_note.setText("")
            return
        try:
            out = core.parse_fps(i.output_fps)
        except (ValueError, ZeroDivisionError):
            self.out_fps_note.setText("Not a frame rate: use e.g. 25, 23.976 or 24000/1001.")
            return
        try:
            source = core.parse_fps(i.fps) if i.fps.strip() else getattr(self.input_info, "fps", None)
        except (ValueError, ZeroDivisionError):
            source = None
        if source is None:
            self.out_fps_note.setText("Every frame is kept and shown at {} fps.".format(core.fps_text(out)))
        elif source == out:
            self.out_fps_note.setText("The same as the input: nothing changes.")
        else:
            ratio = float(source) / float(out)
            self.out_fps_note.setText("{} -> {} fps: every frame is kept; it plays {:.1f}% {} (duration x{:.4f}).".format(
                core.fps_text(source), core.fps_text(out), abs(1 - ratio) * 100,
                "faster" if out > source else "slower", ratio))

    # multichannel tab: which EXR part and channels become the picture

    def _build_channels(self, page):
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.ch_status = QLabel("")
        self.ch_status.setWordWrap(True)
        self.ch_status.setStyleSheet("color: #a8a8a8;")
        row.addWidget(self.ch_status, 1)
        reread = QPushButton("Read the channels again")
        reread.setToolTip("Read the input's header again (oiiotool --info)")
        reread.clicked.connect(lambda: self._read_channels(force=True))
        row.addWidget(reread)
        layout.addLayout(row)
        self.ch_tree = QTreeWidget()
        self.ch_tree.setHeaderLabels(["Part / layer / channel", "Channels"])
        self.ch_tree.setColumnWidth(0, 230)
        self.ch_tree.setToolTip("Click a layer to use its R, G, B (and A); click one channel to "
                                "show it as grey; click a part to look only in it.")
        self.ch_tree.itemClicked.connect(self._channel_clicked)
        layout.addWidget(self.ch_tree, 1)

        box = QGroupBox("Use")
        grid = QGridLayout(box)
        self.ch_mode = QButtonGroup(self)
        self.ch_mode_buttons = {}
        for row_index, (key, text) in enumerate(core.CHANNEL_MODES.items()):
            button = QRadioButton(text)
            self.ch_mode.addButton(button)
            self.ch_mode_buttons[key] = button
            grid.addWidget(button, row_index, 0, 1, 6)
            button.toggled.connect(lambda on: on and self._channels_changed())
        self.ch_pick = {}
        labels = (("R", "red"), ("G", "green"), ("B", "blue"), ("A", "alpha"))
        for col, (key, title) in enumerate(labels):
            box_ = QComboBox()
            box_.setEditable(True)
            box_.setToolTip("The channel for {}{}".format(title, " (optional: kept with -alpha keep / auto)"
                                                         if key == "A" else ""))
            box_.currentTextChanged.connect(lambda _t: self._channels_changed())
            self.ch_pick[key] = box_
            grid.addWidget(QLabel(key), 3, 2 * col)
            grid.addWidget(box_, 3, 2 * col + 1)
            grid.setColumnStretch(2 * col + 1, 1)
        self.ch_mono = QCheckBox("One channel, shown as grey (R only)")
        self.ch_mono.setToolTip('Use only the R pick, shown as grey (e.g. depth.Z or a mask)')
        self.ch_mono.toggled.connect(lambda _on: self._channels_changed())
        grid.addWidget(self.ch_mono, 4, 0, 1, 6)
        self.ch_part = QComboBox()
        self.ch_part.setToolTip("-oiio_subimage: look for the channels only in this part")
        self.ch_part.currentIndexChanged.connect(lambda _i: self._channels_changed())
        grid.addWidget(QLabel("Part"), 5, 0)
        grid.addWidget(self.ch_part, 5, 1, 1, 5)
        layout.addWidget(box)
        self.ch_result = QLabel("")
        self.ch_result.setWordWrap(True)
        self.ch_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.ch_result)
        # the automatic search's order: a preset, edited here (shown for Automatic)
        self.ch_order_box = QGroupBox("Automatic: the channel sets tried, in order (then the first layer)")
        ob = QVBoxLayout(self.ch_order_box)
        row = QHBoxLayout()
        self.ch_order_preset = QComboBox()
        self.ch_order_preset.setToolTip("Channel preference presets (channel_preference_presets in the "
                                        "presets file); the first entry is its channel_preferences")
        self.ch_order_preset.activated.connect(lambda _i: self._order_preset_picked())
        row.addWidget(QLabel("Preset"))
        row.addWidget(self.ch_order_preset, 1)
        row.addWidget(self._save_button("channel_order"))
        delete = QPushButton("Delete")
        delete.setToolTip("Delete the selected channel preference preset from the presets file")
        delete.clicked.connect(self._delete_order_preset)
        row.addWidget(delete)
        ob.addLayout(row)
        row = QHBoxLayout()
        self.ch_order = QListWidget()
        self.ch_order.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.ch_order.setToolTip("Double-click to edit a set (channels separated by commas, e.g. "
                                 "beauty.R,beauty.G,beauty.B,beauty.A). Drag to reorder. "
                                 "Wildcards * ? [abc] work (case counts): Actor*.R,Actor*.G,Actor*.B "
                                 "takes the first layer starting with Actor that has R, G and B; the "
                                 "first set found ends the search.")
        self.ch_order.setMaximumHeight(170)
        self.ch_order.itemChanged.connect(lambda _i: self._order_edited())
        self.ch_order.model().rowsMoved.connect(lambda *_a: self._order_edited())
        row.addWidget(self.ch_order, 1)
        side = QVBoxLayout()
        for text, slot, tip in (
                ("Add", self._order_add, "Add an empty set to type"),
                ("Add the layer", self._order_add_layer, "Add the layer clicked in the tree above"),
                ("Remove", self._order_remove, "Remove the selected set"),
                ("Up", lambda: self._order_move(-1), "Try the selected set earlier"),
                ("Down", lambda: self._order_move(1), "Try the selected set later"),
                ("Reset", self._order_reset, "Back to the presets file's channel_preferences")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            side.addWidget(button)
        side.addStretch()
        row.addLayout(side)
        ob.addLayout(row)
        layout.addWidget(self.ch_order_box)
        deep_box = QGroupBox("Deep EXR")
        deep_form = QFormLayout(deep_box)
        self.ch_deep_zrange = QLineEdit()
        self.ch_deep_zrange.setPlaceholderText("measured on the first frame")
        self.ch_deep_zrange.setToolTip("Deep EXRs without colour are shown as depth (Z): "
                                       "\"near,far\" sets the depth range, e.g. 0.1,1000 "
                                       "(-deep_zrange). Empty: measured on the first frame")
        deep_form.addRow("Depth range (near,far)", self.ch_deep_zrange)
        layout.addWidget(deep_box)
        self.ch_deep_zrange.editingFinished.connect(self._channels_changed)
        self.ch_last_layer = []
        self.ch_info = []
        self.ch_info_path = None
        self._fill_channels()

    def _read_channels(self, force=False):
        """The input's parts, layers and channels into the tree (once per input)."""
        path = self.in_input1.text().strip()
        if not force and path == self.ch_info_path:
            return
        if force:
            core._CHANNEL_INFO_CACHE.clear()
        self.ch_info_path = path
        self.ch_info, why = core.read_channels(path, self.presets)
        self.ch_tree.clear()
        for index, name, layers in core.channel_tree(self.ch_info):
            channels = sum(len(c) for _l, c in layers)
            part = QTreeWidgetItem(["Part {}{}".format(index, ": " + name if name else ""),
                                    "{} channels".format(channels)])
            part.setData(0, Qt.ItemDataRole.UserRole, ("part", str(index) if not name else name, []))
            self.ch_tree.addTopLevelItem(part)
            for layer, names in layers:
                item = QTreeWidgetItem(part, [layer or "(no layer)", " ".join(n.rsplit(".", 1)[-1] for n in names)])
                item.setData(0, Qt.ItemDataRole.UserRole, ("layer", str(index) if not name else name, names))
                for channel in names:
                    leaf = QTreeWidgetItem(item, [channel, ""])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, ("channel", str(index) if not name else name, [channel]))
            part.setExpanded(True)
        self.ch_status.setText(why or "{} part(s) in {}".format(len(self.ch_info), Path(path).name))
        all_channels = [c for _i, _n, chans in exr_parts(self.ch_info) for c in chans]
        self.loading = True
        for key, combo in self.ch_pick.items():
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            if key == "A":
                combo.addItem("")
            combo.addItems(all_channels)
            combo.setCurrentText(current)
            combo.blockSignals(False)
        current = self.state.channels.subimage
        self.ch_part.blockSignals(True)
        self.ch_part.clear()
        self.ch_part.addItem("all parts, in order", "")
        for index, name, _layers in core.channel_tree(self.ch_info):
            self.ch_part.addItem("{} {}".format(index, name).strip(), name or str(index))
        if current and self.ch_part.findData(current) < 0:
            self.ch_part.addItem(current + " (not in this file)", current)
        self.ch_part.setCurrentIndex(max(0, self.ch_part.findData(current)))
        self.ch_part.blockSignals(False)
        self.loading = False
        self._show_channels()

    def _channel_clicked(self, item, _column):
        kind, part, names = item.data(0, Qt.ItemDataRole.UserRole) or ("", "", [])
        if kind in ("layer", "channel"):
            self.ch_last_layer = list(names)
            if self.state.channels.mode == "auto":
                return                            # Automatic: only remembered for "Add the layer"
        if kind == "part":
            self.ch_part.setCurrentIndex(max(0, self.ch_part.findData(part)))
            return
        colour = [n for n in names if n.rsplit(".", 1)[-1] not in ("A", "a")]
        alpha = [n for n in names if n.rsplit(".", 1)[-1] in ("A", "a")]
        self.loading = True
        self.ch_mode_buttons["pick"].setChecked(True)
        self.ch_mono.setChecked(len(colour) < 3)
        values = (colour + [""] * 3)[:3] if len(colour) >= 3 else [colour[0] if colour else names[0], "", ""]
        for key, value in zip(("R", "G", "B"), values):
            self.ch_pick[key].setCurrentText(value)
        self.ch_pick["A"].setCurrentText(alpha[0] if alpha else "")
        if len(self.ch_info) > 1:
            self.ch_part.setCurrentIndex(max(0, self.ch_part.findData(part)))
        self.loading = False
        self._channels_changed()

    def _fill_channels(self):
        c = self.state.channels
        self.loading = True
        self.ch_mode_buttons.get(c.mode, self.ch_mode_buttons["auto"]).setChecked(True)
        names = c.channel_list()
        colour = [n for n in names if n.rsplit(".", 1)[-1] not in ("A", "a")]
        alpha = [n for n in names if n.rsplit(".", 1)[-1] in ("A", "a")]
        self.ch_mono.setChecked(len(colour) == 1)
        for key, value in zip(("R", "G", "B"), (colour + ["", "", ""])[:3]):
            self.ch_pick[key].setCurrentText(value)
        self.ch_pick["A"].setCurrentText(alpha[0] if alpha else "")
        index = self.ch_part.findData(c.subimage)
        if index < 0 and c.subimage:
            self.ch_part.addItem(c.subimage, c.subimage)
            index = self.ch_part.count() - 1
        self.ch_part.setCurrentIndex(max(0, index))
        self.ch_deep_zrange.setText(c.deep_zrange)
        self._fill_order()
        self.loading = False
        self._show_channels()

    # the automatic search's order

    def _fill_order_presets(self):
        if not hasattr(self, "ch_order_preset"):
            return
        box = self.ch_order_preset
        box.blockSignals(True)
        box.clear()
        box.addItem("the presets file's channel_preferences", "")
        for name, preset in (self.presets.channel_orders if self.presets else {}).items():
            box.addItem(name, name)
            if preset.notes:
                box.setItemData(box.count() - 1, preset.notes, Qt.ItemDataRole.ToolTipRole)
        c = self.state.channels
        index = box.findData(c.order_preset)
        if index < 0 and c.order_preset:
            box.addItem(c.order_preset + " (not in this presets file)", c.order_preset)
            index = box.count() - 1
        box.setCurrentIndex(max(0, index))
        box.blockSignals(False)

    def _fill_order(self):
        """The list shows the order in use: the state's own, else the presets file's."""
        c = self.state.channels
        sets = c.order or [",".join(s) for s in c.order_sets(self.presets)]
        self.ch_order.blockSignals(True)
        self.ch_order.clear()
        for text in sets:
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.ch_order.addItem(item)
        self.ch_order.blockSignals(False)
        self._fill_order_presets()

    def _order_items(self):
        return [self.ch_order.item(i).text().strip() for i in range(self.ch_order.count())
                if self.ch_order.item(i).text().strip()]

    def _order_edited(self):
        """The list was changed: it is now this state's own order (unless it is the file's)."""
        if self.loading:
            return
        c = self.state.channels
        items = self._order_items()
        default = [",".join(s) for s in ((self.presets.channel_preferences if self.presets else None) or [])]
        c.order = [] if (not c.order_preset and items == default) else items
        self._show_channels()
        self._changed()

    def _order_preset_picked(self):
        c = self.state.channels
        name = self.ch_order_preset.currentData() or ""
        preset = self.presets.channel_orders.get(name) if (self.presets and name) else None
        c.order_preset = name if preset else ""
        c.order = [",".join(s) for s in preset.sets] if preset else []
        c.order_loaded = list(c.order)
        self.loading = True
        self._fill_order()
        self.loading = False
        if preset is not None and preset.notes:
            self.status.setText("Loaded channel order {}: {}".format(name, preset.notes))
        self._show_channels()
        self._changed()

    def _order_add(self):
        item = QListWidgetItem("")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.ch_order.addItem(item)
        self.ch_order.setCurrentItem(item)
        self.ch_order.editItem(item)

    def _order_add_layer(self):
        if not self.ch_last_layer:
            QMessageBox.information(self, "Channel order", "Click a layer in the tree above first.")
            return
        item = QListWidgetItem(",".join(self.ch_last_layer))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        row = max(0, self.ch_order.currentRow())
        self.ch_order.insertItem(row, item)            # before the selected one: tried earlier
        self.ch_order.setCurrentItem(item)
        self._order_edited()

    def _order_remove(self):
        row = self.ch_order.currentRow()
        if row >= 0:
            self.ch_order.takeItem(row)
            self._order_edited()

    def _order_move(self, step):
        row = self.ch_order.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.ch_order.count():
            return
        item = self.ch_order.takeItem(row)
        self.ch_order.insertItem(target, item)
        self.ch_order.setCurrentRow(target)
        self._order_edited()

    def _order_reset(self):
        c = self.state.channels
        c.order, c.order_preset, c.order_loaded = [], "", []
        self.loading = True
        self._fill_order()
        self.loading = False
        self._show_channels()
        self._changed()

    def _delete_order_preset(self):
        name = self.ch_order_preset.currentData() or ""
        if not name:
            QMessageBox.information(self, "Delete preset", "Pick a channel preference preset first "
                                    "(the presets file's own list can't be deleted here).")
            return
        path = self._presets_path()
        if QMessageBox.question(self, "Delete preset", 'Delete the channel preference preset "{}" '
                                'from\n{}?'.format(name, path)) != QMessageBox.StandardButton.Yes:
            return
        try:
            presets_store.delete_preset(path, "channel_order", name)
        except (OSError, TranscodeError) as exc:
            QMessageBox.warning(self, "Delete preset", "Could not delete from\n{}\n\n{}".format(path, exc))
            return
        if self.state.channels.order_preset == name:
            self.state.channels.order_preset = ""
        self._load_presets()
        self._fill_preset_lists()

    def _read_channels_state(self):
        c = self.state.channels
        c.mode = next((k for k, b in self.ch_mode_buttons.items() if b.isChecked()), "auto")
        colour = [self.ch_pick[k].currentText().strip() for k in (("R",) if self.ch_mono.isChecked() else ("R", "G", "B"))]
        alpha = self.ch_pick["A"].currentText().strip()
        c.channels = ",".join([n for n in colour if n] + ([alpha] if alpha else []))
        c.subimage = self.ch_part.currentData() or ""
        c.deep_zrange = self.ch_deep_zrange.text().strip()

    def _channels_changed(self):
        if self.loading:
            return
        self._read_channels_state()
        self._show_channels()
        self._changed()

    def _show_channels(self):
        c = self.state.channels
        picking = c.mode == "pick"
        for key, combo in self.ch_pick.items():
            combo.setEnabled(picking and (key in ("R", "A") or not self.ch_mono.isChecked()))
        self.ch_mono.setEnabled(picking)
        self.ch_order_box.setVisible(c.mode == "auto")
        if not self.ch_info:
            self.ch_result.setText("")
            return
        text = core.describe_pick(self.ch_info, c, self.presets)
        colour = "#e07070" if text.startswith("Not usable") else "#7ec27e"
        self.ch_result.setText("<span style='color:{}'>vfx-transcode will use: {}</span>".format(
            colour, text.replace("<", "&lt;")))

    # intermediate tab: the frames between oiiotool / the movie unpack and ffmpeg

    def _build_intermediate(self, page):
        layout = QVBoxLayout(page)
        intro = QLabel("EXRs are converted by oiiotool, and movies unpacked by ffmpeg, into frames "
                       "that ffmpeg then encodes. These settings decide their quality, size and speed, "
                       "and where they go.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #a8a8a8;")
        layout.addWidget(intro)
        box = QGroupBox("Quality")
        form = QFormLayout(box)
        self.im_format = QComboBox()
        for key, text in core.MEZZANINE_FORMATS.items():
            self.im_format.addItem(text, key)
        self.im_format.setToolTip("-mezzanine: the format of the intermediate frames")
        self.im_16bit = QCheckBox("16-bit (PNG / TIF): slower, larger; real 10-bit input for 10-bit presets")
        self.im_16bit.setToolTip("-mezzanine_16bit 1")
        form.addRow("Format", self.im_format)
        form.addRow("", self.im_16bit)
        layout.addWidget(box)

        box = QGroupBox("Where they go")
        form = QFormLayout(box)
        self.im_location = QComboBox()
        self.im_location.addItem("Your temp folder: a new folder for each run, usually a fast local disk", "user")
        self.im_location.addItem("A folder next to the source", "source")
        self.im_location.setToolTip("-temp_location")
        self.im_temp = QLineEdit()
        self.im_temp.setPlaceholderText("empty: the input folder itself")
        self.im_temp.setToolTip("-temp: the folder name inside the input folder")
        self.im_keep = QCheckBox("Keep the intermediate files after the run (-temp_keep)")
        self.im_keep.setToolTip('Leave the intermediate frames on disk after the run, e.g. to check them')
        form.addRow("Location", self.im_location)
        form.addRow("Folder name", self.im_temp)
        form.addRow("", self.im_keep)
        layout.addWidget(box)

        box = QGroupBox("oiiotool")
        form = QFormLayout(box)
        self.im_threads = combo(["25%", "50%", "75%", "100%"], editable=True)
        self.im_threads.setToolTip("Threads for oiiotool (-oiio_threads): a share of the threads of "
                                   "the machine that runs the conversion (worked out there, so a "
                                   "super-preset or a Deadline job fits each machine), or a number "
                                   "(0 = all). 50% by default: all threads at once have been seen "
                                   "to give random black frames")
        self.im_per_file = QCheckBox("One oiiotool per file instead of --parallel-frames batches (-oiio_per_file)")
        self.im_per_file.setToolTip("Slower; for when the batches fail (e.g. odd file names). Each "
                                    "frame is then also logged.")
        form.addRow("Threads", self.im_threads)
        form.addRow("", self.im_per_file)
        self.im_verify = QCheckBox("Convert suspected black frames again (-oiio_verify)")
        self.im_verify.setToolTip("oiiotool sometimes writes random black frames when it converts "
                                  "many at once. Frames far smaller than the others are converted "
                                  "again, one at a time; a frame that is really dark stays as it is. "
                                  "The log says which frames were fixed")
        form.addRow("", self.im_verify)
        layout.addWidget(box)
        self.im_note = QLabel("")
        self.im_note.setWordWrap(True)
        self.im_note.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(self.im_note)
        layout.addStretch()
        self._connect(self.im_format, self.im_16bit, self.im_location, self.im_temp, self.im_keep,
                      self.im_threads, self.im_per_file, self.im_verify)
        self._fill_intermediate()

    def _fill_intermediate(self):
        self.loading = True
        m = self.state.intermediate
        self.im_format.setCurrentIndex(max(0, self.im_format.findData(m.mezzanine)))
        self.im_16bit.setChecked(m.sixteen_bit)
        self.im_location.setCurrentIndex(max(0, self.im_location.findData(m.location)))
        self.im_temp.setText(m.temp)
        self.im_keep.setChecked(m.keep)
        set_combo(self.im_threads, str(m.oiio_threads))
        self.im_per_file.setChecked(m.per_file)
        self.im_verify.setChecked(m.verify)
        self.loading = False
        self._show_intermediate()

    def _read_intermediate(self):
        m = self.state.intermediate
        m.mezzanine = self.im_format.currentData() or "png"
        m.sixteen_bit = self.im_16bit.isChecked()
        m.location = self.im_location.currentData() or "user"
        m.temp = self.im_temp.text().strip()
        m.keep = self.im_keep.isChecked()
        m.oiio_threads = self.im_threads.currentText().strip() or "50%"
        m.per_file = self.im_per_file.isChecked()
        m.verify = self.im_verify.isChecked()
        self._show_intermediate()

    def _show_intermediate(self):
        m = self.state.intermediate
        self.im_16bit.setEnabled(m.mezzanine in core.SIXTEEN_BIT_FORMATS)
        self.im_temp.setEnabled(m.location == "source")
        self.im_note.setText(m.note())

    # thumb tab

    def _build_thumb(self, page):
        layout = QVBoxLayout(page)
        self.th_enabled = QCheckBox("Write a thumbnail (-ot), next to the output")
        self.th_enabled.setToolTip("A jpg or png of one frame, with the movie's colour and transform, "
                                   "no burn-ins. It goes next to the movie (or the frames).")
        layout.addWidget(self.th_enabled)
        top = QHBoxLayout()
        self.th_preset = compact_combo(QComboBox(), 12)
        self.th_preset.setToolTip("Pick a thumbnail preset from the presets file to load its "
                                  "settings (-thumbnail_preset)")
        self.th_preset.activated.connect(lambda _i: self._load_thumb())
        top.addWidget(QLabel("Preset"))
        top.addWidget(self.th_preset, 1)
        top.addWidget(self._save_button("thumbnail"))
        layout.addLayout(top)
        form = QFormLayout()
        self.th_frame = combo(core.THUMB_FRAMES, editable=True)
        self.th_frame.setToolTip("Which frame: middle, first, last, largest (the biggest converted "
                                 "frame; needs the movie, else middle) or a frame number "
                                 "(-thumbnail_frame)")
        self.th_width, self.th_height = QSpinBox(), QSpinBox()
        for spin in (self.th_width, self.th_height):
            spin.setRange(0, 65536)
            spin.setSpecialValueText("auto")
            spin.setToolTip("auto = follow the other side; both auto = the movie's size "
                            "(-thumbnail_size)")
        size = QHBoxLayout()
        size.addWidget(self.th_width)
        size.addWidget(QLabel("x"))
        size.addWidget(self.th_height)
        self.th_format = combo(["jpg", "png"])
        self.th_format.setToolTip("-thumbnail_format")
        self.th_quality = QSpinBox()
        self.th_quality.setRange(1, 100)
        self.th_quality.setToolTip("jpg quality (-thumbnail_quality)")
        self.th_compression = QSpinBox()
        self.th_compression.setRange(0, 9)
        self.th_compression.setToolTip("png compression: 0 none (fastest) - 9 smallest "
                                       "(-thumbnail_compression)")
        self.th_bits = combo(["8", "16"])
        self.th_bits.setToolTip("png bit depth (-thumbnail_bits)")
        form.addRow("Frame", self.th_frame)
        form.addRow("Size", size)
        form.addRow("Format", self.th_format)
        form.addRow("jpg quality", self.th_quality)
        form.addRow("png compression", self.th_compression)
        form.addRow("png bits", self.th_bits)
        self.th_form = QWidget()
        self.th_form.setLayout(form)
        layout.addWidget(self.th_form)
        self.th_notes = QLabel("")
        self.th_notes.setWordWrap(True)
        self.th_notes.setStyleSheet("color: #a8a8a8;")
        layout.addWidget(self.th_notes)
        layout.addStretch()
        self.th_enabled.toggled.connect(lambda _on: self._thumb_changed())
        for spin in (self.th_width, self.th_height, self.th_quality, self.th_compression):
            spin.valueChanged.connect(lambda _v: self._thumb_changed())
        for box in (self.th_format, self.th_bits):
            box.currentIndexChanged.connect(lambda _i: self._thumb_changed())
        self.th_frame.editTextChanged.connect(lambda _t: self._thumb_changed())

    def _fill_thumb_presets(self):
        """The thumbnail presets; the first entry is the presets file's default ("")."""
        box = self.th_preset
        box.blockSignals(True)
        box.clear()
        default = self.presets.default_thumbnail if self.presets else ""
        box.addItem("default ({})".format(default or "built-in"), "")
        for name in (self.presets.thumbnails if self.presets else {}):
            box.addItem(name, name)
        self._select_thumb_preset()
        box.blockSignals(False)

    def _select_thumb_preset(self):
        preset = self.state.thumb.preset
        index = self.th_preset.findData(preset)
        if index < 0 and preset:              # not in this presets file: keep it, say so
            self.th_preset.addItem("{} (not in the presets file)".format(preset), preset)
            index = self.th_preset.count() - 1
        self.th_preset.setCurrentIndex(max(0, index))

    def _load_thumb(self):
        """A preset picked: its settings fill the tab."""
        name = self.th_preset.currentData() or ""
        preset = core.thumb_preset(self.presets, name)
        self.state.thumb = core.thumb_from_preset(preset, name, self.th_enabled.isChecked())
        self._fill_thumb()
        if preset.name:
            self._loaded_status("thumbnail", preset)
        self._changed()

    def _fill_thumb(self):
        self.loading = True
        t = self.state.thumb
        if not t.custom:                         # (a super-preset may hold only the name)
            loaded = core.thumb_from_preset(core.thumb_preset(self.presets, t.preset), t.preset, t.enabled)
            t.frame, t.width, t.height, t.format = loaded.frame, loaded.width, loaded.height, loaded.format
            t.quality, t.compression, t.bits = loaded.quality, loaded.compression, loaded.bits
        self.th_enabled.setChecked(t.enabled)
        self.th_preset.blockSignals(True)
        self._select_thumb_preset()
        self.th_preset.blockSignals(False)
        set_combo(self.th_frame, str(t.frame))
        self.th_width.setValue(t.width)
        self.th_height.setValue(t.height)
        set_combo(self.th_format, t.format)
        self.th_quality.setValue(t.quality)
        self.th_compression.setValue(t.compression)
        set_combo(self.th_bits, str(t.bits))
        self.loading = False
        self._show_thumb_notes()

    def _read_thumb(self):
        t = self.state.thumb
        t.enabled = self.th_enabled.isChecked()
        t.preset = self.th_preset.currentData() or ""
        t.frame = self.th_frame.currentText().strip().lower() or "middle"
        t.width, t.height = self.th_width.value(), self.th_height.value()
        t.format = self.th_format.currentText() or "jpg"
        t.quality, t.compression = self.th_quality.value(), self.th_compression.value()
        t.bits = int(self.th_bits.currentText() or 8) if t.format == "png" else 8
        t.custom = core.thumb_custom(t, self.presets)

    def _show_thumb_notes(self):
        t = self.state.thumb
        name = t.preset or (self.presets.default_thumbnail if self.presets else "")
        preset = self.presets.thumbnails.get(name) if self.presets and name else None
        if name and preset is None:
            text = "Unknown thumbnail preset: vfx-transcode will stop with an error."
        else:
            text = (preset.notes if preset is not None and preset.notes else "")
            if t.custom:
                text = ("Changed from the preset: the settings are written as -thumbnail_* "
                        "options." + ("\n" + text if text else ""))
        frame = t.frame
        if frame not in core.THUMB_FRAMES and not frame.lstrip("-").isdigit():
            text = 'Frame "{}": use middle, first, last, largest or a frame number.'.format(frame)
        self.th_notes.setText(text)
        on = self.th_enabled.isChecked()
        png = self.th_format.currentText() == "png"
        self.th_quality.setEnabled(on and not png)
        self.th_compression.setEnabled(on and png)
        self.th_bits.setEnabled(on and png)
        for widget in (self.th_preset, self.th_notes, self.th_frame, self.th_width,
                       self.th_height, self.th_format):
            widget.setEnabled(on)

    def _thumb_changed(self):
        if self.loading:
            return
        self._read_thumb()
        self._show_thumb_notes()
        self._changed()

    # letterbox tab

    def _letterbox_group(self, title):
        """A letterbox section: {"enabled", "ratio", "color", "opacity", "line", "line_color",
        "line_opacity"} widgets in a group box."""
        box = QGroupBox(title)
        grid = QGridLayout(box)
        w = {"enabled": QCheckBox("Letterbox / pillarbox"),
             "ratio": compact_combo(combo(["2.39", "2.35", "2.0", "1.85", "16:9", "4:3", "1:1"],
                                          editable=True), 5),
             "color": ColorButton(compact=True), "opacity": opacity_spin(compact=True),
             "line": QSpinBox(), "line_color": ColorButton(compact=True),
             "line_opacity": opacity_spin(compact=True)}
        w["line"].setRange(0, 200)
        w["line"].setToolTip("line thickness in pixels, 0 = no line")
        w["line"].setFixedWidth(56)
        # Ratio [2.39]   Bars [colour][opacity]   Line [px][colour][opacity]
        grid.addWidget(w["enabled"], 0, 0, 1, 4)
        grid.addWidget(QLabel("Ratio"), 1, 0)
        grid.addWidget(w["ratio"], 1, 1, 1, 3)
        grid.addWidget(QLabel("Bars"), 2, 0)
        grid.addWidget(w["color"], 2, 1)
        grid.addWidget(w["opacity"], 2, 2)
        grid.addWidget(QLabel("Line px"), 3, 0)
        grid.addWidget(w["line"], 3, 1)
        grid.addWidget(w["line_color"], 3, 2)
        grid.addWidget(w["line_opacity"], 3, 3)
        grid.setColumnStretch(4, 1)
        box.setToolTip("Bars: colour, opacity. Line: thickness (px), colour, opacity")
        self._connect(*w.values())
        return box, w

    @staticmethod
    def _fill_letterbox_group(w, lb):
        w["enabled"].setChecked(lb.enabled)
        set_combo(w["ratio"], lb.ratio)
        w["color"].set_value(lb.color)
        w["opacity"].setValue(lb.opacity)
        w["line"].setValue(lb.line)
        w["line_color"].set_value(lb.line_color)
        w["line_opacity"].setValue(lb.line_opacity)

    @staticmethod
    def _read_letterbox_group(w, lb):
        lb.enabled, lb.ratio = w["enabled"].isChecked(), w["ratio"].currentText().strip()
        lb.color, lb.opacity = QColor(w["color"].value()).name(), w["opacity"].value()
        lb.line = w["line"].value()
        lb.line_color = QColor(w["line_color"].value()).name()
        lb.line_opacity = w["line_opacity"].value()

    # transform (ffmpeg) tab

    def _build_ff_transform(self, page):
        layout = QVBoxLayout(page)
        header = QLabel("<b>ffmpeg transform</b>: scale / zscale filters, one per line "
                        "(# starts a comment). Empty = no scaling. They replace the "
                        "-width scale, before the letterbox and the burn-ins.")
        header.setWordWrap(True)
        layout.addWidget(header)
        self.ff_text = QPlainTextEdit()
        self.ff_text.setToolTip('ffmpeg scale / zscale filters, one per line (# = comment), sent as -ff_transform; empty: no scaling')
        self.ff_text.setPlaceholderText("scale=w=1920:h=-2:flags=lanczos\n"
                                        "# or zscale, e.g.\n"
                                        "# zscale=w=2048:h=-2:filter=spline36")
        self.ff_text.setMinimumHeight(110)
        self.ff_text.setMaximumHeight(180)
        self.ff_text.textChanged.connect(self._changed)
        layout.addWidget(self.ff_text)
        row = QHBoxLayout()
        row.addWidget(QLabel("Preset"))
        row.addLayout(self._preset_row("transform"), 1)
        layout.addLayout(row)
        box, self.lb = self._letterbox_group("Letterbox (ffmpeg, drawn after the transform)")
        layout.addWidget(box)
        layout.addStretch()

    # metadata tab

    def _build_timecode(self):
        """The Timecode and reel name section of the Input tab. The sources are set for each
        new input (see core.auto_timecode); change them after that if needed."""
        self.tc_group = QGroupBox("Timecode and reel name")
        form = QFormLayout(self.tc_group)
        self.md_tc_source = combo([""] + list(core.TIMECODE_SOURCES))
        self.md_tc_source.setToolTip("counter: from the frame number and frame rate. arg: the "
                                     "Timecode field. metadata: the EXR header's or the movie's "
                                     "timecode. Set for each new input; empty = the metadata preset's")
        self.md_timecode = QLineEdit()
        self.md_timecode.setToolTip('The start timecode HH:MM:SS:FF, used with the timecode source arg (-timecode)')
        self.md_timecode.setPlaceholderText("HH:MM:SS:FF (timecode source arg)")
        self.md_reel_source = combo([""] + list(core.REEL_SOURCES))
        self.md_reel_source.setToolTip("auto: the Reel name field, else the header attribute, else "
                                       "none. arg: the field only. metadata: the header attribute, "
                                       "else the field. off: no reel name")
        self.md_reel = QLineEdit()
        self.md_reel.setToolTip('The reel name, used with the reel source arg (-reelid); keywords work')
        self.tc_auto_note = QLabel("")
        self.tc_auto_note.setWordWrap(True)
        self.tc_auto_note.setStyleSheet("color: #8a8a8a;")
        form.addRow("Timecode source", self.md_tc_source)
        form.addRow("Timecode", self.md_timecode)
        form.addRow("", self.tc_auto_note)
        form.addRow("Reel name source", self.md_reel_source)
        form.addRow("Reel name", self.md_reel)
        self.tc_found = QLabel("")
        self.tc_found.setWordWrap(True)
        self.tc_found.setToolTip("What the last preview found with these settings")
        form.addRow("Last preview", self.tc_found)
        self.md_tc_source.currentTextChanged.connect(lambda _t: self._tc_field_state())
        self._connect(self.md_tc_source, self.md_timecode, self.md_reel_source, self.md_reel)
        self.tc_auto_path = None              # the input the sources were last set for
        return self.tc_group

    def _tc_field_state(self):
        self.md_timecode.setEnabled(self.md_tc_source.currentText() in ("", "arg"))

    def _auto_timecode(self, info):
        """A new input: timecode source from what the input carries, reel name source auto."""
        path = self.in_input1.text().strip()
        if not path or info.error or path == self.tc_auto_path:
            return
        self.tc_auto_path = path
        is_exr = info.kind != "movie" and path.lower().endswith(".exr")
        tc = info.timecode if info.kind == "movie" else (
            core.detect_timecode(path, self.presets) if is_exr else None)
        source, text = core.auto_timecode(info.kind, is_exr, tc)
        loading, self.loading = self.loading, True
        set_combo(self.md_tc_source, source)
        if text is not None:
            self.md_timecode.setText(text)
        set_combo(self.md_reel_source, "auto")
        self.loading = loading
        self._tc_field_state()
        what = ("the movie" if info.kind == "movie" else "the EXR header" if is_exr
                else "{} files".format(Path(path).suffix.lstrip(".").upper() or "these"))
        if source == "metadata":
            note = "Set for this input: timecode from {} ({}).".format(what, tc)
        elif source == "arg":
            note = "Set for this input: {} has no timecode, so it starts at {}.".format(what, text)
        elif is_exr:
            note = "Set for this input: no timecode in the EXR header, so the frame counter."
        else:
            note = "Set for this input: {} carry no timecode, so the frame counter.".format(what)
        self.tc_auto_note.setText(note)
        if is_exr and tc and self.tc_base is None:
            self.tc_base = (info.first, tc, None)
        if not self.loading:
            self._changed()

    def _build_metadata(self, page):
        """Metadata tab: the tags written into the movie (or each image of a sequence written
        by oiiotool), EXR header attributes copied, and where the reel name is read."""
        page_layout = QVBoxLayout(page)
        self.md_group = QGroupBox("Metadata")
        layout = QVBoxLayout(self.md_group)
        form = QFormLayout()
        self.md_preset = compact_combo(QComboBox(), 12)
        self.md_preset.setToolTip("Pick a metadata preset to load its tags, attributes copied and "
                                  "reel name settings (its timecode / reel sources go to the Input "
                                  "tab). Empty = the presets file's default, none = no preset")
        self.md_preset.activated.connect(lambda _i: self._load_metadata())
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.md_preset, 1)
        preset_row.addWidget(self._save_button("metadata"))
        form.addRow("Metadata preset", preset_row)
        self.md_notes = QLabel("")
        self.md_notes.setWordWrap(True)
        self.md_notes.setStyleSheet("color: #8a8a8a;")
        form.addRow("", self.md_notes)
        self.md_copy = QLineEdit()
        self.md_copy.setPlaceholderText("the preset's (default: none)")
        self.md_copy.textChanged.connect(lambda _t: self._show_copy_note())
        self.md_copy.setToolTip("Copy EXR header attributes to the output (-metadata_copy): "
                                "\"all\", or key patterns with * and - to exclude, e.g. "
                                "\"all,-husk:*\" or \"Artist,capDate\"")
        form.addRow("Copy EXR attributes", self.md_copy)
        self.md_copy_note = QLabel("")
        self.md_copy_note.setWordWrap(True)
        self.md_copy_note.setStyleSheet("color: #8a8a8a;")
        form.addRow("", self.md_copy_note)
        layout.addLayout(form)
        layout.addWidget(QLabel("Tags (KEY / VALUE, keywords ok):"))
        self.md_tags = PairTable(["Key", "Value"])
        self.md_tags.setToolTip('Tags written into the movie (or each image): KEY and VALUE, keywords work, e.g. title = {clean_name}')
        self.md_tags.setMinimumHeight(140)
        layout.addWidget(self.md_tags)
        page_layout.addWidget(self.md_group)
        reel_box = QGroupBox("Reel name from the EXR header")
        reel_form = QFormLayout(reel_box)
        self.md_reel_attr = QLineEdit()
        self.md_reel_attr.setPlaceholderText("the preset's (default: owner)")
        self.md_reel_attr.setToolTip("EXR header attribute holding the reel name (-reelid_attr)")
        self.md_reel_pattern = QLineEdit()
        self.md_reel_pattern.setPlaceholderText("the preset's (default: the value minus a [first-last] range)")
        self.md_reel_pattern.setToolTip("Regular expression applied to that value; its first group "
                                        "(or the whole match) is the reel name (-reelid_pattern)")
        reel_form.addRow("Attribute", self.md_reel_attr)
        reel_form.addRow("Pattern", self.md_reel_pattern)
        note = QLabel("Used when the reel source (Input tab) is auto or metadata.")
        note.setStyleSheet("color: #8a8a8a;")
        reel_form.addRow("", note)
        page_layout.addWidget(reel_box)
        page_layout.addStretch()
        self._connect(self.md_tags, self.md_copy, self.md_reel_attr, self.md_reel_pattern)

    def _load_metadata(self):
        """A metadata preset picked: its settings fill the tab (and the Input tab's sources)."""
        name = self.md_preset.currentText().strip()
        real = name or (self.presets.default_metadata if self.presets else "")
        preset = (self.presets.metadata_presets.get(real) if self.presets and real
                  and real != "none" else None)
        core.metadata_from_preset(self.state.metadata, preset, name)
        self._fill_metadata()
        if preset is not None:
            self._loaded_status("metadata", preset)
        self._changed()

    def _show_copy_note(self):
        """Under Copy EXR attributes: how many attributes of the last preview's header it
        copies, or why none (e.g. unreal:* when the keys are unreal/...)."""
        if hasattr(self, "md_copy_note"):
            note = core.copy_note(self.report, self.md_copy.text())
            self.md_copy_note.setText(note)
            self.md_copy_note.setStyleSheet("color: #e07070;" if "nothing" in note else "color: #8a8a8a;")

    def _show_metadata_notes(self):
        m = self.state.metadata
        real = m.preset or (self.presets.default_metadata if self.presets else "")
        preset = self.presets.metadata_presets.get(real) if self.presets and real else None
        text = preset.notes if preset is not None and preset.notes else ""
        if core.metadata_changed(m):
            text = ("Changed from the preset: written out as -metadata_preset none and the "
                    "settings below." + (" " + text if text else ""))
        self.md_notes.setText(text)

    # output tab

    # output tab (the encode)

    def _build_encode(self, page):
        form = QFormLayout(page)
        self.sp_type = QComboBox()
        self.sp_type.setToolTip('A movie, or an image sequence (EXR, PNG, JPG, TIF, DPX); decides which tools run and which tabs apply')
        for key, label in core.OUTPUT_TYPES.items():
            self.sp_type.addItem(label, key)
        self.sp_type.currentIndexChanged.connect(lambda _i: self._output_type_changed())
        self.sp_pipeline = QLabel("")
        self.sp_pipeline.setStyleSheet("color: #8a8a8a;")
        form.addRow("Output type", self.sp_type)
        form.addRow("", self.sp_pipeline)
        self.seq_preset = QComboBox()
        self.seq_preset.setToolTip("Sequence preset (-seq_preset): oiiotool output settings "
                                   "for an image sequence: bit depth, compression, quality")
        self.seq_preset.currentIndexChanged.connect(lambda _i: self._seq_preset_changed())
        self.seq_notes = QLabel("")
        self.seq_notes.setWordWrap(True)
        self.seq_notes.setStyleSheet("color: #8a8a8a;")
        self.in_encode = QComboBox()
        self.in_encode.setToolTip("Encode preset (-preset): codec, pixel format, container. "
                                  "(default: ...) = the presets file's default_encode, followed if it changes")
        self.encode_notes = QLabel("")
        self.encode_notes.setWordWrap(True)
        self.encode_notes.setStyleSheet("color: #8a8a8a;")
        self.encode_state = QLabel("")
        self.encode_state.setWordWrap(True)
        form.addRow("Encode preset", self.in_encode)
        form.addRow("", self.encode_notes)
        form.addRow("Sequence preset", self.seq_preset)
        form.addRow("", self.seq_notes)
        form.addRow("", self.encode_state)
        self.alpha_mode = QComboBox()
        for key, label in core.ALPHA_MODES.items():
            self.alpha_mode.addItem(label, key)
        self.alpha_mode.setToolTip("-alpha: auto keeps the input's alpha channel when the output "
                                   "can store it (EXR, PNG, TIF, or a movie preset marked [alpha] "
                                   "such as prores4444); drop never writes alpha")
        self.alpha_note = QLabel("")
        self.alpha_note.setWordWrap(True)
        self.alpha_note.setStyleSheet("color: #8a8a8a;")
        form.addRow("Alpha", self.alpha_mode)
        self.alpha_premult_out = QCheckBox("Premultiplied colour (-alpha_premult)")
        self.alpha_premult_out.setToolTip("With alpha kept: write the colour premultiplied by "
                                          "alpha. Off (default): straight colour, what most "
                                          "players and editors expect")
        form.addRow("", self.alpha_premult_out)
        form.addRow("", self.alpha_note)
        self.par_out = combo(core.PAR_OUT_CHOICES, editable=True)
        self.par_out.setToolTip("-par_out: same (default) keeps the pixels and tags the output "
                                "with the input pixel aspect. square: reformat for square pixels "
                                "(the height is scaled). A number: reformat for that pixel aspect, "
                                "e.g. 2 or 4/3")
        form.addRow("Output pixel aspect", self.par_out)
        self.out_write_tc = QCheckBox("Write timecode (-output_timecode)")
        self.out_write_tc.setToolTip("Movie: a timecode track in mov / mp4 / mxf, a tag in other "
                                     "containers. EXR / DPX sequence: each frame's own timecode "
                                     "(smpte:TimeCode). Off: the burn-ins and slate still show it")
        self.out_write_reel = QCheckBox("Write reel name (-output_reelid)")
        self.out_write_reel.setToolTip("The reel name from the Input tab, in the movie or in each "
                                       "image of an EXR sequence (reelName). Off: the slate and "
                                       "the {reel} keyword still use it")
        self.write_note = QLabel("")
        self.write_note.setWordWrap(True)
        self.write_note.setStyleSheet("color: #8a8a8a;")
        form.addRow("", self.out_write_tc)
        form.addRow("", self.out_write_reel)
        form.addRow("", self.write_note)
        self.out_fps = QComboBox()
        self.out_fps.setEditable(True)
        self.out_fps.addItem(SAME_FPS)
        self.out_fps.addItems([r for r in FRAME_RATES if r != "auto"])
        self.out_fps.setToolTip("-output_fps: play the output at another rate. Every frame is kept, "
                                "each is shown for 1/rate s: 24 -> 25 makes 100 frames last 4 s "
                                "instead of 4.17 s. The timecode, {fps} and the slate follow it.")
        self.out_fps.currentTextChanged.connect(lambda _t: self._out_fps_changed())
        self.out_fps_note = QLabel("")
        self.out_fps_note.setStyleSheet("color: #8a8a8a;")
        self.out_fps_note.setWordWrap(True)
        form.addRow("Frame rate", self.out_fps)
        form.addRow("", self.out_fps_note)
        self._connect(self.alpha_mode, self.out_write_tc, self.out_write_reel,
                      self.alpha_premult_out, self.par_out)
        self._connect(self.in_encode)
        self.in_encode.currentTextChanged.connect(lambda _t: self._show_encode_notes())

    def _output_type_changed(self):
        if self.loading:
            return
        self.state.input.output_type = self.sp_type.currentData() or "movie"
        self.state.input.seq_preset = ""          # a preset belongs to one format
        self._fill_seq_presets()
        self._changed()

    def _seq_preset_changed(self):
        self._show_seq_notes()
        if not self.loading:
            self._changed()

    def _fill_seq_presets(self):
        """The sequence presets of the chosen format; the first entry is its default."""
        names, default = core.sequence_presets_for(self.presets, self.state.input.output_type)
        loading, self.loading = self.loading, True
        self.seq_preset.clear()
        if self.state.input.output_type == "movie":
            self.seq_preset.addItem("(a movie: no sequence preset)", "")
        else:
            self.seq_preset.addItem("(default{})".format(": " + default if default else
                                                         ": built-in settings"), "")
            for name in names:
                self.seq_preset.addItem(name, name)
        index = self.seq_preset.findData(self.state.input.seq_preset)
        self.seq_preset.setCurrentIndex(index if index >= 0 else 0)
        self.loading = loading
        self._show_seq_notes()

    def _show_seq_notes(self):
        name = self.seq_preset.currentData() or core.sequence_presets_for(
            self.presets, self.state.input.output_type)[1]
        preset = self.presets.sequences.get(name) if self.presets and name else None
        if self.state.input.output_type == "movie":
            text = ""
        elif preset is not None:
            text = "{}: {}  ({})".format(name, preset.notes or "", " ".join(preset.args))
        else:
            text = "built-in: " + " ".join(core.builtin_sequence_args(self.state.input.output_type))
        self.seq_notes.setText(text)

    def _encode_choice(self):
        """The chosen encode preset; "" = the presets file's default_encode (no -preset)."""
        return self.in_encode.currentData() or ""

    def _set_encode_choice(self, name):
        index = self.in_encode.findData(name or "")
        self.in_encode.setCurrentIndex(index if index >= 0 else 0)

    def _fill_encode_presets(self):
        """First entry "(default: NAME)": no -preset, so a later change of the presets
        file's default_encode applies; then the presets by name."""
        current = self._encode_choice()
        self.in_encode.blockSignals(True)
        self.in_encode.clear()
        self.in_encode.addItem("(default: {})".format(self.presets.default), "")
        for name in self.presets.presets:
            self.in_encode.addItem(name, name)
        self._set_encode_choice(current)
        self.in_encode.blockSignals(False)

    def _show_encode_notes(self):
        name = self._encode_choice() or (self.presets.default if self.presets else "")
        preset = self.presets.presets.get(name) if self.presets else None
        text = ""
        if preset is not None:
            text = "{}{} (.{}): {}".format(name, ", the default" if not self._encode_choice()
                                           else "", preset.ext, preset.notes or "no notes")
        self.encode_notes.setText(text)

    # encode tab: writing encode presets (ffmpeg) and sequence presets (oiiotool)

    def _build_encode_tab(self, page):
        layout = QVBoxLayout(page)
        intro = QLabel("Write new presets here; saved ones appear on the Output tab (and the "
                       "saved one is picked there). Pick a preset to start from it.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #8a8a8a;")
        layout.addWidget(intro)

        movie = QGroupBox("Movie encode preset (ffmpeg, \"encode_presets\")")
        mv_layout = QVBoxLayout(movie)
        top = QHBoxLayout()
        self.en_preset = compact_combo(QComboBox(), 12)
        self.en_preset.setToolTip("Pick an encode preset to load it")
        self.en_preset.activated.connect(lambda _i: self._load_encode_edit())
        top.addWidget(QLabel("Preset"))
        top.addWidget(self.en_preset, 1)
        top.addWidget(self._save_button("encode_preset"))
        mv_layout.addLayout(top)
        self.en_notes = QLabel("")
        self.en_notes.setWordWrap(True)
        self.en_notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.en_notes.setStyleSheet("color: #a8a8a8;")
        mv_layout.addWidget(self.en_notes)
        form = QFormLayout()
        self.en_ext = combo(core.ENCODE_EXTS, editable=True)
        self.en_ext.setToolTip("The movie's container / file extension")
        self.en_args = QPlainTextEdit()
        self.en_args.setFixedHeight(72)
        self.en_args.setToolTip("ffmpeg output arguments: codec, pixel format, quality ... "
                                "{videofilter} (a word of its own) is where the -vf filters go, "
                                "e.g. -pix_fmt yuv422p10le {videofilter} -c:v prores_ks -profile:v 3")
        self.en_alpha = combo(core.ALPHA_PIX_FMTS, editable=True)
        self.en_alpha.setToolTip("Pixel format used instead of -pix_fmt when alpha is kept "
                                 "(alpha_pix_fmt). Empty: the codec cannot store alpha")
        self.en_check = QLabel("")
        self.en_check.setWordWrap(True)
        form.addRow("Container", self.en_ext)
        form.addRow("ffmpeg arguments", self.en_args)
        form.addRow("Alpha pixel format", self.en_alpha)
        form.addRow("", self.en_check)
        mv_layout.addLayout(form)
        layout.addWidget(movie)

        seq = QGroupBox("Image sequence preset (oiiotool, \"sequence_presets\")")
        sq_layout = QVBoxLayout(seq)
        top = QHBoxLayout()
        self.sq_preset = compact_combo(QComboBox(), 12)
        self.sq_preset.setToolTip("Pick a sequence preset to load it")
        self.sq_preset.activated.connect(lambda _i: self._load_sequence_edit())
        top.addWidget(QLabel("Preset"))
        top.addWidget(self.sq_preset, 1)
        top.addWidget(self._save_button("sequence_preset"))
        sq_layout.addLayout(top)
        form = QFormLayout()
        self.sq_ext = combo(core.SEQUENCE_EXTS)
        self.sq_ext.setToolTip("The image format the preset is for")
        self.sq_args = QLineEdit()
        self.sq_args.setToolTip("oiiotool output arguments: bit depth, compression, attributes, "
                                "e.g. -d half --compression dwaa:45 or -d uint8 "
                                "--compression jpeg:95 --attrib jpeg:subsampling 4:4:4")
        self.sq_check = QLabel("")
        self.sq_check.setWordWrap(True)
        form.addRow("Format", self.sq_ext)
        form.addRow("oiiotool arguments", self.sq_args)
        form.addRow("", self.sq_check)
        sq_layout.addLayout(form)
        layout.addWidget(seq)
        layout.addStretch()
        self.en_args.textChanged.connect(self._encode_edit_changed)
        self.en_ext.currentTextChanged.connect(lambda _t: self._encode_edit_changed())
        self.en_alpha.currentTextChanged.connect(lambda _t: self._encode_edit_changed())
        self.sq_ext.currentTextChanged.connect(lambda _t: self._encode_edit_changed())
        self.sq_args.textChanged.connect(lambda _t: self._encode_edit_changed())
        self._fill_encode_edit()

    def _read_encode_edit(self):
        e = self.state.encode_edit
        e.ext = self.en_ext.currentText().strip().lstrip(".")
        e.encode = " ".join(self.en_args.toPlainText().split())
        e.alpha_pix_fmt = self.en_alpha.currentText().strip()
        q = self.state.sequence_edit
        q.ext = self.sq_ext.currentText().strip()
        q.args = self.sq_args.text().strip()

    def _fill_encode_edit(self):
        e, q = self.state.encode_edit, self.state.sequence_edit
        self.en_filling = True
        set_combo(self.en_ext, e.ext)
        self.en_args.setPlainText(e.encode)
        set_combo(self.en_alpha, e.alpha_pix_fmt)
        self.en_notes.setText(e.notes)
        self.en_notes.setVisible(bool(e.notes))
        set_combo(self.sq_ext, q.ext)
        self.sq_args.setText(q.args)
        self.en_filling = False
        self._encode_edit_changed()

    def _encode_edit_changed(self):
        if getattr(self, "en_filling", False):
            return
        self._read_encode_edit()
        for label, problem in ((self.en_check, core.encode_problem(self.state.encode_edit)),
                               (self.sq_check, core.sequence_problem(self.state.sequence_edit))):
            label.setText(problem or "OK: can be saved.")
            label.setStyleSheet("color: #e07070;" if problem else "color: #8a8a8a;")
        self._update_tab_marks()

    def _load_encode_edit(self):
        preset = self.presets.presets.get(self.en_preset.currentText()) if self.presets else None
        if preset is None:
            return
        self.state.encode_edit = core.encode_edit_from_preset(preset)
        self._fill_encode_edit()
        self._loaded_status("encode", preset)

    def _load_sequence_edit(self):
        preset = self.presets.sequences.get(self.sq_preset.currentText()) if self.presets else None
        if preset is None:
            return
        self.state.sequence_edit = core.sequence_edit_from_preset(preset)
        self._fill_encode_edit()
        self._loaded_status("sequence", preset)

    def _use_saved_encode(self, area, name):
        """After saving on the Encode tab: pick the new preset on the Output tab."""
        if area == "encode_preset":
            self._set_encode_choice(name)
            self.state.input.encode_preset = name
            saved = self.presets.presets.get(name) if self.presets else None
            self.state.encode_edit.notes = saved.notes if saved is not None else ""
            self.en_notes.setText(self.state.encode_edit.notes)
            self.en_notes.setVisible(bool(self.state.encode_edit.notes))
        elif area == "sequence_preset" and self.state.input.output_type == self.state.sequence_edit.ext:
            self.state.input.seq_preset = name
            self._fill_seq_presets()

    # presets tab (what to copy)

    def _build_output(self, page):
        layout = QVBoxLayout(page)
        tool_row = QFormLayout()
        self.in_tool = QLineEdit(core.quote(core.vfx_transcode_command()))
        self.in_tool.setToolTip("The vfx-transcode the tuner runs for previews and Process: by "
                                "default this Python with vfx-transcode.py from the same folder; "
                                "or a built vfx-transcode.exe")
        tool_row.addRow("vfx-transcode command", self.in_tool)
        layout.addLayout(tool_row)
        self.presets_label = QLabel("")
        self.presets_label.setWordWrap(True)
        self.presets_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.presets_label)
        self.out_bat = QCheckBox("For a .bat file (% written as %%)")
        self.out_bat.setToolTip('Write % as %% so the command works pasted into a Windows .bat file')
        self.out_bat.toggled.connect(self.update_outputs)
        layout.addWidget(self.out_bat)
        layout.addWidget(QLabel("<b>Full command</b> (the movie, next to the source):"))
        self.out_full = QTextEdit()
        self.out_full.setReadOnly(True)
        self.out_full.setAcceptRichText(False)
        self.out_full.setToolTip("Coloured by the tab each option comes from (see the key above it). "
                                 "Copying gives plain text")
        self.out_legend = QLabel("")
        self.out_legend.setWordWrap(True)
        layout.addWidget(self.out_legend)
        layout.addWidget(self.out_full, 1)

    # info tab (metadata viewer)

    def _build_info(self, page):
        layout = QVBoxLayout(page)
        self.info_search = QLineEdit()
        self.info_search.setToolTip('Show only keys and values containing this text (any case)')
        self.info_search.setPlaceholderText("Search keys and values")
        self.info_search.setClearButtonEnabled(True)
        self.info_search.textChanged.connect(lambda _text: self._fill_info())
        self.info_hide_empty = QCheckBox("hide empty")
        self.info_hide_empty.setToolTip("Leave out keys whose value is empty")
        self.info_hide_empty.toggled.connect(lambda _on: self._fill_info())
        row = QHBoxLayout()
        row.addWidget(self.info_search, 1)
        row.addWidget(self.info_hide_empty)
        layout.addLayout(row)
        self.info = QTreeWidget()
        self.info.setToolTip("The input's header, the keyword values and the output of the last preview")
        self.info.setHeaderLabels(["Key", "Value"])
        self.info.setColumnWidth(0, 220)
        layout.addWidget(self.info)

    # state <-> widgets

    def _fill_preset_lists(self):
        if not self.presets:
            return
        self.loading = True
        pick = [PICK_PRESET]              # first entry: nothing loaded yet
        for box, names in ((self.tr_preset, pick + list(self.presets.transforms)),
                           (self.bu_preset, pick + list(self.presets.burnins)),
                           (self.sl_preset, pick + list(self.presets.slates)),
                           (self.fs_preset, pick + list(self.presets.filmstrips)),
                           (self.co_preset, pick + list(self.presets.colours)),
                           (self.ff_preset, pick + list(self.presets.ff_transforms)),
                           (self.en_preset, pick + list(self.presets.presets)),
                           (self.sq_preset, pick + list(self.presets.sequences)),
                           (self.md_preset, ["", "none"] + list(self.presets.metadata_presets))):
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            set_combo(box, current)
            fit_popup(box)
            box.blockSignals(False)
        self._fill_encode_presets()
        self._fill_thumb_presets()
        self._fill_destination_menus()
        self._fill_order_presets()
        m = self.state.metadata
        if m.loaded is None and not m.tags and not m.preset:
            preset = self.presets.metadata_presets.get(self.presets.default_metadata or "")
            # the file's default, shown in the tab (filled by _fill_all); the Input tab keeps
            # the sources it chose for the input
            core.metadata_from_preset(m, preset, "", sources=False)
        if not self.state.tokens.rows and not self.state.tokens.preset:
            old = self.state.tokens
            self.state.tokens = core.tokens_from_preset(self.presets, "")   # the file's default
            self.state.tokens.version_regex, self.state.tokens.name_tokens = old.version_regex, old.name_tokens
        self._fill_token_presets()
        self.loading = False
        self._transform_preset_selected(self.tr_preset.currentText())
        self._show_encode_notes()
        self._fill_seq_presets()

    def _fill_all(self):
        self._fill_preset_lists()
        self.loading = True
        i = self.state.input
        for edit, value in ((self.in_input1, i.input1), (self.in_input2, i.input2),
                            (self.in_clf, i.clf),
                            (self.in_ocio, i.ocio)):
            edit.setText(value)
        for box, value in ((self.in_ci, i.colorin), (self.in_co, i.colorout),
                           (self.in_display, i.display), (self.in_view, i.view),
                           (self.in_looks, i.looks)):
            set_combo(box, value)
        self._set_encode_choice(i.encode_preset)
        set_combo(self.in_fps, i.fps or "auto")
        set_combo(self.in_par, i.par or "auto")
        index = self.in_input_type.findData(core.effective_input_type(i))
        if index >= 0:
            self.in_input_type.setCurrentIndex(index)
        self.in_input_type.setEnabled(not i.input1.strip())
        self.in_compare_source.setChecked(i.compare_source)
        self.in_convert.setChecked(i.convert_colour)
        index = self.sp_type.findData(i.output_type)
        self.sp_type.setCurrentIndex(index if index >= 0 else 0)
        self._fill_seq_presets()
        index = self.alpha_mode.findData(i.alpha)
        self.alpha_mode.setCurrentIndex(index if index >= 0 else 0)
        self.alpha_premult_out.setChecked(i.alpha_premult)
        self.alpha_premult_out.setEnabled(i.alpha != "drop")
        set_combo(self.par_out, i.par_out or "same")
        index = self.in_missing.findData(i.missing_frames)
        self.in_missing.setCurrentIndex(index if index >= 0 else 0)
        self.out_write_tc.setChecked(i.write_timecode)
        self.out_fps.setCurrentText(i.output_fps or SAME_FPS)
        self._show_out_fps()
        self.out_write_reel.setChecked(i.write_reelid)
        self.timeline.set_trim(i.trim_in, i.trim_out)
        self.in_first_frame.setValue(i.movie_first_frame)
        self.loading = False
        self._fill_transform()
        self._fill_burn()
        self._fill_slate()
        self._fill_filmstrip()
        self._fill_thumb()
        self._fill_destination()
        self._fill_intermediate()
        self._fill_channels()
        self._fill_letterbox()
        self._fill_metadata()
        self._fill_tokens()

    def _fill_transform(self):
        self.loading = True
        t = self.state.transform
        self.tr_use.setChecked(t.use)
        self.tr_steps.set_steps(t.args)
        if t.preset:
            set_combo(self.tr_preset, t.preset)
            set_combo(self.tr_sub, t.sub)
        self.loading = False

    def _fill_burn(self):
        self.loading = True
        b = self.state.burn
        self.bu_enabled.setChecked(b.enabled)
        if b.preset:
            set_combo(self.bu_preset, b.preset)
        self.bu_color.set_value(b.color)
        self.bu_opacity.setValue(b.opacity)
        self.bu_size.setText(b.size)
        self.bu_box.setChecked(b.box)
        for place, w in self.bu_places.items():
            p = b.places[place]
            w["text"].setText(p.text)
            w["own"].setChecked(p.own_style)
            w["color"].set_value(p.color)
            w["opacity"].setValue(p.opacity)
            w["size"].setText(p.size)
            w["box"].setChecked(p.box)
        for side, edit in self.bu_margins.items():
            edit.setText(b.margins[side])
        self.bu_font.setText(b.font)
        self.loading = False

    def _fill_slate(self):
        self.loading = True
        s = self.state.slate
        self.sl_enabled.setChecked(s.enabled)
        if s.preset:
            set_combo(self.sl_preset, s.preset)
        self.sl_title.setText(s.title)
        self.sl_subtitle.setText(s.subtitle)
        self.sl_fields.set_pairs(s.fields)
        set_combo(self.sl_thumbnail, s.thumbnail)
        self.sl_hide_empty.setChecked(s.hide_empty)
        self.sl_rule.setChecked(s.rule)
        self.sl_burnin.setChecked(s.burnin)
        self.sl_title_size.setText(s.title_size)
        self.sl_subtitle_size.setText(s.subtitle_size)
        self.sl_size.setText(s.size)
        self.sl_scale.setValue(s.scale)
        set_combo(self.sl_valign, s.valign)
        for key, button in self.sl_colors.items():
            button.set_value(getattr(s, key))
        self.sl_logo.setText(s.logo)
        index = self.sl_logo_position.findData(s.logo_position)
        self.sl_logo_position.setCurrentIndex(index if index >= 0 else 2)
        self.sl_logo_size.setText(s.logo_size)
        self.sl_logo_x.setText(s.logo_x)
        self.sl_logo_y.setText(s.logo_y)
        self._logo_state()
        self.loading = False

    def _fill_letterbox(self):
        """Both transform tabs: the ffmpeg transform and letterbox, the oiiotool letterbox."""
        self.loading = True
        self._fill_letterbox_group(self.lb, self.state.letterbox)
        self._fill_letterbox_group(self.olb, self.state.oiio_letterbox)
        if self.ff_text.toPlainText() != self.state.ff_transform.text:
            self.ff_text.setPlainText(self.state.ff_transform.text)
        self.loading = False

    def _fill_metadata(self):
        self.loading = True
        m = self.state.metadata
        set_combo(self.md_preset, m.preset)
        set_combo(self.md_tc_source, core._source(m.timecode_source))
        self.md_timecode.setText(m.timecode)
        set_combo(self.md_reel_source, core._source(m.reelid_source))
        self._tc_field_state()
        self.md_reel.setText(m.reelid)
        self.md_tags.set_pairs(m.tags)
        self.md_copy.setText(m.copy)
        self.md_reel_attr.setText(m.reelid_attr)
        self.md_reel_pattern.setText(m.reelid_pattern)
        self._show_metadata_notes()
        self.loading = False

    def _read_all(self):
        self._read_tokens()
        i = self.state.input
        i.input1 = self.in_input1.text().strip()
        if i.input1:
            i.input_type = core.detect_input_type(i.input1) or i.input_type
        else:
            i.input_type = self.in_input_type.currentData() or "exr"
        i.frame = i.frame or "middle"             # set by the timeline; "middle" for a new input
        i.encode_preset = self._encode_choice()
        i.colorin, i.colorout = self.in_ci.currentText().strip(), self.in_co.currentText().strip()
        i.convert_colour = self.in_convert.isChecked()
        i.display, i.view, i.looks = (self.in_display.currentText().strip(),
                                      self.in_view.currentText().strip(),
                                      self.in_looks.currentText().strip())
        i.clf, i.ocio = self.in_clf.text(), self.in_ocio.text()
        fps = self.in_fps.currentText().strip()
        i.fps = "" if fps.lower() in ("", "auto") else fps
        par = self.in_par.currentText().strip()
        i.par = "" if par.lower() in ("", "auto") else par
        i.movie_first_frame = self.in_first_frame.value()

        t = self.state.transform
        t.use = self.tr_use.isChecked()
        t.args = self.tr_steps.steps()
        self._read_letterbox_group(self.olb, self.state.oiio_letterbox)
        self.state.ff_transform.text = self.ff_text.toPlainText()
        self.state.input.output_type = self.sp_type.currentData() or "movie"
        if self.seq_preset.isEnabled() or self.state.input.output_type == "movie":
            self.state.input.seq_preset = self.seq_preset.currentData() or ""
        self.state.input.alpha = self.alpha_mode.currentData() or "drop"
        self.state.input.alpha_premult = self.alpha_premult_out.isChecked()
        self.alpha_premult_out.setEnabled(self.state.input.alpha != "drop")
        self.state.input.par_out = self.par_out.currentText().strip() or "same"
        self.state.input.missing_frames = self.in_missing.currentData() or "hold"
        self.state.input.write_timecode = self.out_write_tc.isChecked()
        fps = self.out_fps.currentText().strip()
        self.state.input.output_fps = "" if fps.lower() in ("", SAME_FPS) else fps
        self.state.input.write_reelid = self.out_write_reel.isChecked()

        b = self.state.burn
        b.enabled = self.bu_enabled.isChecked()
        b.color, b.opacity = self.bu_color.value(), self.bu_opacity.value()
        b.size, b.box = self.bu_size.text().strip() or "1.33%", self.bu_box.isChecked()
        for place, w in self.bu_places.items():
            p = b.places[place]
            p.text = w["text"].text()
            p.own_style = w["own"].isChecked()
            p.color, p.opacity = w["color"].value(), w["opacity"].value()
            p.size, p.box = w["size"].text().strip() or "1.33%", w["box"].isChecked()
        for side, edit in self.bu_margins.items():
            b.margins[side] = edit.text().strip() or "0"
        b.font = self.bu_font.text().strip()

        s = self.state.slate
        s.enabled = self.sl_enabled.isChecked()
        s.title, s.subtitle = self.sl_title.text(), self.sl_subtitle.text()
        s.fields = self.sl_fields.pairs()
        s.thumbnail = self.sl_thumbnail.currentText().strip() or "middle"
        s.hide_empty, s.rule = self.sl_hide_empty.isChecked(), self.sl_rule.isChecked()
        s.burnin = self.sl_burnin.isChecked()
        s.title_size = self.sl_title_size.text().strip() or "5%"
        s.subtitle_size = self.sl_subtitle_size.text().strip() or "3.2%"
        s.size = self.sl_size.text().strip() or "2.5%"
        s.scale, s.valign = round(self.sl_scale.value(), 2), self.sl_valign.currentText()
        for key, button in self.sl_colors.items():
            setattr(s, key, button.value())
        s.logo = self.sl_logo.text().strip()
        s.logo_position = self.sl_logo_position.currentData() or "tr"
        s.logo_size = self.sl_logo_size.text().strip() or "10%"
        s.logo_x = self.sl_logo_x.text().strip() or "4%"
        s.logo_y = self.sl_logo_y.text().strip() or "5%"

        f = self.state.filmstrip
        f.enabled, f.tiles = self.fs_enabled.isChecked(), self.fs_tiles.value()
        f.tile_width, f.tile_height = self.fs_width.value(), self.fs_height.value()
        f.columns, f.rows = self.fs_columns.value(), self.fs_rows.value()
        f.quality, f.slate = self.fs_quality.value(), self.fs_slate.isChecked()
        f.source = self.fs_source.currentText() or "source"
        self._read_thumb()
        if hasattr(self, "de_path"):
            self._read_destination()
        if hasattr(self, "im_format"):
            self._read_intermediate()
        if hasattr(self, "ch_part"):
            self._read_channels_state()
        if hasattr(self, "en_ext"):
            self._read_encode_edit()

        self._read_letterbox_group(self.lb, self.state.letterbox)

        m = self.state.metadata
        m.timecode_source, m.timecode = self.md_tc_source.currentText(), self.md_timecode.text()
        m.reelid_source, m.reelid = self.md_reel_source.currentText(), self.md_reel.text()
        m.tags = self.md_tags.pairs()
        m.copy = self.md_copy.text().strip()
        m.reelid_attr = self.md_reel_attr.text().strip()
        m.reelid_pattern = self.md_reel_pattern.text().strip()
        self._show_metadata_notes()

    # outputs

    def update_outputs(self, *_args):
        if not self.presets:
            return
        full, sections = core.output_sections(self.state, self.presets, self._active_sub(),
                                              self._output())
        self._show_destination_preview()
        self._update_pipeline()
        self._update_tab_marks()
        fix = (lambda text: text.replace("%", "%%")) if self.out_bat.isChecked() else (lambda t: t)
        args = ["vfx-transcode"] + core.full_args(self.state, self.presets, self._active_sub(),
                                                  self._output())
        parts = core.command_parts(args)
        spans = []
        for tab, words in parts:
            text = html.escape(fix(core.quote(words)))
            colour = TAB_COLOURS.get(tab)
            spans.append('<span style="color:{}">{}</span>'.format(colour, text) if colour else text)
        self.out_full.setHtml('<div style="font-family: Consolas, monospace;">{}</div>'.format(" ".join(spans)))
        used = [t for t in TAB_COLOURS if any(tab == t for tab, _w in parts)]
        self.out_legend.setText("&nbsp;&nbsp;".join(
            '<span style="color:{}">&#9632; {}</span>'.format(TAB_COLOURS[t], html.escape(t)) for t in used))

    def _update_tab_marks(self):
        """Tab names: "Burn-in *" = edited since its preset was loaded or saved (not saved as a
        preset), italics = switched off / nothing in it."""
        if not hasattr(self, "tabs"):
            return
        marks = core.tab_marks(self.state, self.presets)
        for index in range(self.tabs.count()):
            edited, unused = marks.get(self.tabs.tabText(index), (False, False))
            self.tabs.setTabMarks(index, edited, unused)

    # running vfx-transcode

    def view_mode(self):
        button = self.view_buttons.checkedButton()
        return button.property("mode") if button is not None else "image"

    def _view_changed(self, _button, checked):
        if not checked:
            return
        kind = "filmstrip" if self.view_mode() == "filmstrip" else "preview"
        if self.stale[kind] and self.state.input.input1 and self.auto_refresh.isChecked():
            self.refresh()
        else:
            self.show_images()

    def refresh(self):
        self._read_all()
        if not self.state.input.input1:
            self.status.setText("Choose the input on the Input tab, or drop a file or folder here.")
            return
        if os.path.isdir(self.state.input.input1):
            self.status.setText("The input is a folder: press ... on the Input tab to pick a file in it.")
            return
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            self.pending = True                 # finish, then run again with the new settings
            self.process.kill()
            return
        run = next(self.run_counter)
        tool = split_args(self.in_tool.text()) or core.vfx_transcode_command()
        kind = "filmstrip" if self.view_mode() == "filmstrip" else "preview"
        if kind == "filmstrip":
            image = self.work_dir / "preview_{}_filmstrip.jpg".format(run)
            movie = self.work_dir / "preview_{}_movie.{}".format(run, "mp4")
            cmd = core.filmstrip_command(self.state, image, movie, tool)
        else:
            image = self.work_dir / "preview_{}.png".format(run)
            cmd = core.preview_command(self.state, image, tool)
        self.log.appendPlainText("\n> " + core.quote(cmd))
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(
            lambda code, _status, image=image, kind=kind: self._finished(code, image, kind))
        self.status.setText("Making the filmstrip..." if kind == "filmstrip" else "Processing...")
        self.refresh_button.setEnabled(False)
        self.process.start(cmd[0], cmd[1:])

    def _read_output(self):
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.log.appendPlainText(data.rstrip())

    def _finished(self, code, image, kind="preview"):
        self.refresh_button.setEnabled(True)
        if self.pending:
            self.pending = False
            self.status.setText("")
            QTimer.singleShot(0, self.refresh)
            return
        if code != 0:
            self.status.setText("vfx-transcode failed (exit {}), see the log".format(code))
            return
        if kind == "filmstrip":
            pixmap = self._load_pixmap(str(image))
            if pixmap is None:
                self.status.setText("No filmstrip written, see the log")
                return
            self.images["filmstrip"] = pixmap
            self.stale["filmstrip"] = False
            self.show_images()
            self.status.setText("Filmstrip {}x{}".format(pixmap.width(), pixmap.height()))
            self._clean_old_previews(image)
            return
        try:
            with open(str(image.with_suffix(".json")), encoding="utf-8") as fh:
                self.report = json.load(fh)
        except (OSError, ValueError) as exc:
            self.status.setText("No preview written: {}".format(exc))
            return
        self.images["image"] = self._load_pixmap(self.report.get("image"))
        self.images["slate"] = self._load_pixmap(self.report.get("slate"))
        self.stale["preview"] = False
        self._sync_timeline()
        self.show_images()
        self._update_compare()
        self._fill_info()
        self._show_token_values()
        self._update_active_sub()
        self.update_outputs()
        self.status.setText("")
        self._update_overlay()
        self._show_copy_note()
        self._clean_old_previews(image)

    def _sync_timeline(self):
        """After a preview: the timeline on its frame, and the timecodes it now knows."""
        report = self.report or {}
        inp, out = report.get("input", {}), report.get("output", {})
        first = inp.get("first_frame")
        if first is not None and out.get("timecode"):
            self.tc_base = (first, out.get("timecode"), out.get("fps"))
        self.tc_found.setText(core.found_text(report, self.state))
        self._reel_write_follow(report)
        if report.get("frame") is not None and not self.timeline.slider.isSliderDown():
            self.timeline.set_frame(report["frame"])
        self.timeline.refresh_labels()

    def _reel_write_follow(self, report):
        """Reel name auto found nothing: untick Write reel name. Tick it again when a reel
        name turns up, but only if it was unticked here (not by the user)."""
        none = core.reel_found_none(report, self.state)
        box = self.out_write_reel
        auto_off = getattr(self, "reel_write_auto_off", False)
        want = None
        if none and box.isChecked():
            want, self.reel_write_auto_off = False, True
            self.status.setText("Reel name: auto found none, so Write reel name is unticked")
        elif none is False and not box.isChecked() and auto_off:
            want, self.reel_write_auto_off = True, False
        elif none is False:
            self.reel_write_auto_off = False
        if want is not None:
            # no _changed(): the preview does not depend on it, so no new refresh is needed
            loading, self.loading = self.loading, True
            box.setChecked(want)
            self.loading = loading
            self.state.input.write_reelid = want
            self.update_outputs()

    def _update_pipeline(self):
        """Grey out what this input and output don't use: Transform Sequence without oiiotool,
        Transform and Burn-in without ffmpeg, Slate and Metadata without a movie."""
        if not hasattr(self, "tabs"):
            return
        p = core.pipeline(self.state.input.input1, self.state.input.output_type,
                          self.state.input.input_type)
        wanted = {"Color": p.oiio, "Transform Sequence": p.oiio, "Transform": p.ffmpeg,
                  "Burn-in": p.ffmpeg, "Slate": p.movie,
                  "Multichannel": p.oiio and core.input_kind(self.state.input.input1,
                                                             self.state.input.input_type) in ("exr", "")}
        for index in range(self.tabs.count()):
            title = self.tabs.tabText(index)
            if title in wanted:
                self.tabs.setTabEnabled(index, wanted[title])
                self.tabs.setTabToolTip(index, "" if wanted[title] else
                                        "Not used for this input and output")
        self.sp_pipeline.setText("runs " + p.describe())
        self.md_group.setEnabled(p.movie or p.oiio)
        self.tc_group.setEnabled(p.movie or p.ffmpeg or p.oiio)
        self.md_group.setTitle("Metadata" if p.movie else
                               "Metadata (written into each image by oiiotool; EXR keeps all tags)"
                               if p.oiio else
                               "Metadata (not used: ffmpeg writes these images without tags)")
        used, always = core.colour_use(self.state)
        self.in_convert.setEnabled(p.oiio and not always)
        if not p.oiio:
            text = "No colour conversion: oiiotool does not run for this input and output."
        elif always:
            text = "<b>Used</b>: EXR input to a movie or a display format is always converted."
        elif used:
            text = "<b>Used</b>: Convert colour is on."
        else:
            text = ("<b>Not used</b>: {} stays as it is unless Convert colour is on.".format(
                "EXR to EXR" if core.input_kind(self.state.input.input1, self.state.input.input_type) == "exr"
                else "these frames"))
        self.colour_state.setText(text)
        movie = self.state.input.output_type == "movie"
        self.in_encode.setEnabled(movie)
        self.seq_preset.setEnabled(not movie and p.oiio)
        if movie:
            note = ""
        elif p.oiio:
            note = "The encode preset is not used: the output is an image sequence."
        else:
            note = ("ffmpeg writes this sequence (a movie input), so the sequence presets "
                    "(oiiotool settings) are not used: JPG is quality 2 (about 95), 4:4:4.")
        self.encode_state.setText(note)
        self.alpha_note.setText(core.alpha_note(self.state, self.presets))
        can_tc, can_reel, note = core.write_support(self.state)
        self.out_write_tc.setEnabled(can_tc)
        self.out_write_reel.setEnabled(can_reel)
        self.write_note.setText(note)
        self.sp_type_label.setText("Output type: {} (Output tab), runs {}".format(
            self.sp_type.currentText(), p.describe()))

    def _load_pixmap(self, path):
        if not path or not os.path.isfile(path):
            return None
        return self._remember(QImage(path))          # QImage: no stale cache between runs

    def _remember(self, image):
        """A pixmap of the image, keeping the image as read for the alpha view (a QPixmap
        stores colour premultiplied)."""
        pixmap = QPixmap.fromImage(image)
        if image.hasAlphaChannel():
            self.raw_images[pixmap.cacheKey()] = image
            while len(self.raw_images) > 8:
                self.raw_images.pop(next(iter(self.raw_images)))
        return pixmap

    def _raw(self, pixmap):
        return self.raw_images.get(pixmap.cacheKey()) or pixmap.toImage()

    def show_images(self):
        mode = self.view_mode()
        main = self.images.get(mode)
        if mode == "slate" and main is None and not self.stale["preview"]:
            self.status.setText("No slate: switch it on in the Slate tab.")
        elif mode == "filmstrip" and main is None:
            self.status.setText("No filmstrip yet: press Refresh.")
        compare = self.compare_pixmap if mode != "filmstrip" else None
        self.mix.setEnabled(compare is not None and not compare.isNull())
        # the filmstrip is square-pixel already; the movie frames are shown at their pixel aspect
        par = 1.0 if mode == "filmstrip" else float(
            (self.report or {}).get("output", {}).get("par", 1.0) or 1.0)
        main, compare = self._alpha_view(main, compare)
        self.viewer.set_images(main, compare, par)
        self.viewer.set_mix(self.mix.value() / 100.0 if self.mix.isEnabled() else 0.0)
        if main is not None and mode != self.shown_mode:
            self.viewer.fit()                        # a filmstrip and a frame differ in shape
            self.shown_mode = mode

    def _alpha_view(self, main, compare):
        """Show the alpha controls when the picture has alpha, and apply them: RGB (colour
        only), Alpha (grey) or Checkerboard, colour premultiplied or not."""
        has_alpha = main is not None and not main.isNull() and main.hasAlphaChannel()
        self.alpha_action.setVisible(has_alpha)
        hint = core.alpha_hint(self.state, self.report) if has_alpha else ""
        self.alpha_hint.setText(hint)
        self.alpha_hint_action.setVisible(bool(hint))
        if not has_alpha:
            return main, compare
        mode = self.alpha_view.currentData() or "rgb"
        premult = self.alpha_premult.isChecked()
        main = QPixmap.fromImage(alpha_image(self._raw(main), mode, premult))
        if compare is not None and not compare.isNull() and compare.hasAlphaChannel():
            compare = QPixmap.fromImage(alpha_image(self._raw(compare), mode, premult))
        return main, compare

    def _update_overlay(self):
        """Input / output size, pixel aspect and fps over the picture. The input size comes
        from the preview's report; for PNG / JPG / TIF frames that oiiotool did not read, from
        the previewed frame file itself."""
        size = None
        frame_file = (self.report or {}).get("frame_file")
        if frame_file and os.path.isfile(frame_file):
            found = QImageReader(frame_file).size()
            if found.isValid():
                size = (found.width(), found.height())
        self.viewer.set_overlay(core.overlay_lines(self.report, size), self.show_overlay.isChecked())

    def _fill_info(self):
        self.info.clear()
        sections = {}
        rows = core.filter_rows(core.metadata_rows(self.report), self.info_search.text())
        if self.info_hide_empty.isChecked():
            rows = core.non_empty_rows(rows)
        for section, key, value in rows:
            parent = sections.get(section)
            if parent is None:
                parent = QTreeWidgetItem([section, ""])
                self.info.addTopLevelItem(parent)
                sections[section] = parent
            QTreeWidgetItem(parent, [key, value])
        self.info.expandAll()

    def closeEvent(self, event):
        if self._job_running():
            answer = QMessageBox.question(self, "Process", "A conversion is running. Stop it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.job.kill()
            self.job.waitForFinished(3000)
        for process in (self.process, self.compare_process):
            if process is not None and process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(2000)
        QApplication.instance().removeEventFilter(self)
        shutil.rmtree(str(self.work_dir), ignore_errors=True)
        super().closeEvent(event)

    def _clean_old_previews(self, keep):
        for path in self.work_dir.glob("preview_*"):
            if not path.name.startswith(keep.stem):
                try:
                    path.unlink()
                except OSError:
                    pass


def apply_dark_theme(app):
    """Fusion style with a dark palette: the whole window, dialogs included."""
    app.setStyle("Fusion")
    palette = QPalette()
    role, group = QPalette.ColorRole, QPalette.ColorGroup
    colours = {
        role.Window: QColor(45, 45, 48), role.WindowText: QColor(220, 220, 220),
        role.Base: QColor(30, 30, 32), role.AlternateBase: QColor(40, 40, 43),
        role.ToolTipBase: QColor(55, 55, 58), role.ToolTipText: QColor(220, 220, 220),
        role.Text: QColor(220, 220, 220), role.Button: QColor(55, 55, 58),
        role.ButtonText: QColor(220, 220, 220), role.BrightText: QColor(255, 80, 80),
        role.Link: QColor(90, 160, 255), role.Highlight: QColor(58, 110, 180),
        role.HighlightedText: QColor(255, 255, 255), role.PlaceholderText: QColor(130, 130, 130),
    }
    for colour_role, colour in colours.items():
        palette.setColor(colour_role, colour)
    for colour_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(group.Disabled, colour_role, QColor(120, 120, 120))
    app.setPalette(palette)
    app.setStyleSheet("QToolTip { color: #dcdcdc; background: #37373a; border: 1px solid #555; }"
                      "QGroupBox { border: 1px solid #4a4a4e; margin-top: 8px; padding-top: 6px; }"
                      "QGroupBox::title { subcontrol-origin: margin; left: 8px; }")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vfx-transcode-tuner",
                                     description="Tune vfx-transcode presets on a test frame.")
    parser.add_argument("input1", nargs="?", default="", help="Input: any frame of a sequence, or a movie file")
    parser.add_argument("input2", nargs="?", default="", help="Image to compare to (optional)")
    parser.add_argument("-presets_file", default="", help="presets.json to load")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    apply_dark_theme(app)
    window = TunerWindow(args.input1, args.input2, args.presets_file)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
