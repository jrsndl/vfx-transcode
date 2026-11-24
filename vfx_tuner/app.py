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
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QInputDialog, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsPixmapItem,
                               QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QStyle, QStyleOptionSlider,
                               QTableWidget, QTableWidgetItem,
                               QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from vfx_transcode import presets_store
from vfx_transcode import studio
from vfx_transcode import super_presets as sp
from vfx_transcode.burnin import BURN_PLACES, MARGIN_SIDES, PLACE_NAMES
from vfx_transcode.errors import TranscodeError
from vfx_transcode.movie import DEFAULT_FIRST_FRAME, MOVIE_EXTS
from vfx_transcode.presets import DEFAULT_PRESETS_FILE, load_presets
from vfx_transcode.tools import app_dir, resolve_path, split_args

from . import core

REFRESH_DELAY_MS = 500
STEP_DELAY_MS = 250                   # timeline keyboard / page steps: refresh after a pause
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


class SavePresetDialog(QDialog):
    """Name (an existing one replaces it), sub-preset (oiiotool transforms) and notes."""

    def __init__(self, parent, title, names, name="", notes="", subs_for=None, sub="",
                 notes_for=None, where=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.subs_for, self.notes_for = subs_for, notes_for
        form = QFormLayout(self)
        self.name = combo(names, editable=True)
        self.name.setEditText(name)
        self.name.lineEdit().setPlaceholderText("a new name, or pick one to replace it")
        form.addRow("Name", self.name)
        self.sub = None
        if subs_for is not None:
            self.sub = combo([], editable=True)
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


class PairTable(QWidget):
    """Two editable columns (label / value) with add, remove, up, down."""
    changed = Signal()

    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(lambda _item: self.changed.emit())
        buttons = QVBoxLayout()
        for text, slot in (("Add", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1))):
            button = QPushButton(text)
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
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        buttons = QVBoxLayout()
        for text, slot in (("Add", self._add), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1))):
            button = QPushButton(text)
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
        if input1:
            self._input_changed()
            QTimer.singleShot(200, self.refresh)

    # presets

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
        self.refresh_button.clicked.connect(self.refresh)
        self.auto_refresh = QCheckBox("Refresh after edit")
        self.auto_refresh.setChecked(True)
        fit = QPushButton("Fit")
        one = QPushButton("1:1")
        self.view_buttons = QButtonGroup(self)
        view_widgets = [QLabel("  View")]
        for mode, label in (("image", "Image"), ("slate", "Slate"), ("filmstrip", "Filmstrip")):
            button = QRadioButton(label)
            button.setProperty("mode", mode)
            button.setChecked(mode == "image")
            self.view_buttons.addButton(button)
            view_widgets.append(button)
        self.view_buttons.buttonToggled.connect(self._view_changed)
        self.mix = QSlider(Qt.Orientation.Horizontal)
        self.mix.setRange(0, 100)
        self.mix.setFixedWidth(160)
        self.mix.valueChanged.connect(lambda v: self.viewer.set_mix(v / 100.0))
        self.status = QLabel("")
        self.show_overlay = QCheckBox("Show overlay")
        self.show_overlay.setChecked(True)
        self.show_overlay.setToolTip("Input and output size, pixel aspect and frame rate over "
                                     "the top right of the picture")
        self.show_overlay.toggled.connect(lambda _on: self._update_overlay())
        for widget in ([self.refresh_button, self.auto_refresh, fit, one] + view_widgets
                       + [QLabel("  Compare mix"), self.mix, QLabel("  "), self.show_overlay,
                          QLabel("  "), self.status]):
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
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addStretch()
        clear_log = QPushButton("Clear")
        clear_log.clicked.connect(self.log.clear)
        log_bar.addWidget(clear_log)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log)
        self.bottom_tabs = QTabWidget()
        super_page = QWidget()
        self._build_super_presets(super_page)
        self.bottom_tabs.addTab(super_page, "Super-presets")
        self.bottom_tabs.addTab(log_box, "Log")
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(picture)
        left.addWidget(self.bottom_tabs)
        left.setStretchFactor(0, 3)
        left.setStretchFactor(1, 1)
        left.setSizes([620, 300])

        self.tabs = QTabWidget()
        for title, builder in (("Input", self._build_input), ("Color", self._build_colour),
                               ("Output", self._build_encode),
                               ("Transform Sequence", self._build_transform),
                               ("Transform", self._build_ff_transform),
                               ("Burn-in", self._build_burn), ("Slate", self._build_slate),
                               ("Filmstrip", self._build_filmstrip),
                               ("Presets", self._build_output),
                               ("Info", self._build_info)):
            page = QWidget()
            builder(page)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, title)

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
        top = QHBoxLayout()
        self.sp_list = QListWidget()
        self.sp_list.setMaximumWidth(240)
        self.sp_list.currentTextChanged.connect(self._super_selected)
        top.addWidget(self.sp_list)
        right = QVBoxLayout()
        form = QFormLayout()
        self.sp_name = QLineEdit()
        self.sp_name.setPlaceholderText("Name for Save")
        self.sp_notes = QLineEdit()
        form.addRow("Name", self.sp_name)
        form.addRow("Notes", self.sp_notes)
        right.addLayout(form)
        areas = QGroupBox("Areas to save / load")
        grid = QGridLayout(areas)
        self.sp_areas = {}
        for n, (area, title) in enumerate(sp.AREA_TITLES.items()):
            box = QCheckBox(title)
            box.setChecked(True)
            grid.addWidget(box, n // 4, n % 4)
            self.sp_areas[area] = box
        right.addWidget(areas)
        buttons = QHBoxLayout()
        for text, slot, tip in (
                ("Save", self._super_save, "Save the ticked areas of the current settings under Name"),
                ("Load", self._super_load, "Load the ticked areas of the selected super-preset"),
                ("Delete", self._super_delete, "Delete the selected super-preset")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        right.addLayout(buttons)
        self.sp_file = QLabel("")
        self.sp_file.setStyleSheet("color: #8a8a8a;")
        self.sp_file.setWordWrap(True)
        right.addWidget(self.sp_file)
        right.addStretch()
        top.addLayout(right, 1)
        layout.addLayout(top, 1)

        out_row = QFormLayout()
        self.sp_output = QLineEdit("./")
        self.sp_output.setToolTip("-o: a folder (ending in /) or a file name; ./ = next to the "
                                  "input. Keywords like {clean_name} work. A sequence gets "
                                  "{clean_name}.####.EXT in a folder, or .####.EXT after a name")
        self.sp_output.editingFinished.connect(self.update_outputs)
        self.sp_type_label = QLabel("")
        self.sp_type_label.setStyleSheet("color: #8a8a8a;")
        out_row.addRow("Output (-o)", browse_row(self.sp_output, "Output folder", folder=True))
        out_row.addRow("", self.sp_type_label)
        layout.addLayout(out_row)
        self.process_button = QPushButton("Process")
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

    def _super_selected(self, name):
        entry = self.super_presets.get(name)
        if not entry:
            return
        self.sp_name.setText(name)
        self.sp_notes.setText(entry.get("notes", ""))
        stored = set(entry.get("areas", {}))
        for area, box in self.sp_areas.items():
            box.setChecked(area in stored)

    def _ticked_areas(self):
        return [area for area, box in self.sp_areas.items() if box.isChecked()]

    def _super_save(self):
        name = self.sp_name.text().strip()
        areas = self._ticked_areas()
        if not areas:
            QMessageBox.information(self, "Super-presets", "Tick at least one area.")
            return
        if not name:
            name, ok = QInputDialog.getText(
                self, "Save super-preset", "Name for the super-preset ({}):".format(
                    ", ".join(sp.AREA_TITLES[a] for a in areas)))
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
        entry = core.super_preset_entry(self.state, areas, self.sp_notes.text().strip())
        result = self._super_write(lambda presets: presets.__setitem__(name, entry))
        if result is None:
            return
        self._load_super_presets()
        items = self.sp_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.sp_list.setCurrentItem(items[0])
        self.job_status.setText('Saved super-preset "{}": {} to {}{}'.format(
            name, ", ".join(sp.AREA_TITLES[a] for a in areas), result.path,
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
        loaded = core.apply_super_preset(self.state, entry, self._ticked_areas())
        if not loaded:
            self.job_status.setText("Nothing loaded: the ticked areas are not in this super-preset.")
            return
        self._fill_all()
        self._changed()
        self.job_status.setText('Loaded "{}": {}'.format(
            item.text(), ", ".join(sp.AREA_TITLES[a] for a in loaded)))

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
        output = self.sp_output.text().strip() or "./"
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
        self.in_input2 = QLineEdit()
        self.in_detected = QLabel("")
        self.in_detected.setWordWrap(True)
        self.in_fps = combo(FRAME_RATES, editable=True)
        self.in_fps.setToolTip("auto: the EXR header's or the movie's own rate, else 24")
        detect = QPushButton("Detect")
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
        form.addRow("", self.in_detected)
        form.addRow("Frame rate (-r)", fps_row)
        form.addRow("Pixel aspect (-par)", par_row)
        form.addRow("Movie: first frame number", self.in_first_frame)
        self.in_compare_source = QCheckBox("Compare to source (the unmodified input footage)")
        self.in_compare_source.setChecked(True)
        self.in_compare_source.setToolTip("Compare mix shows the input's own frame: no transform, "
                                          "resize, burn-ins or slate (EXR with the Color tab's "
                                          "colour conversion). Off: compare to the file below")
        self.in_input2.setPlaceholderText("an image, a movie or a sequence (any frame of it)")
        form.addRow("Compare", self.in_compare_source)
        form.addRow("Compare to", browse_row(self.in_input2, "Compare to"))
        form.addRow(self._build_timecode())
        self._connect(self.in_input1, self.in_fps,
                      self.in_first_frame, self.in_par)
        self.in_input1.editingFinished.connect(self._input_changed)
        self.in_first_frame.valueChanged.connect(lambda _v: self._input_changed())
        self.in_fps.currentTextChanged.connect(lambda _t: self.timeline.refresh_labels())
        self.in_input2.editingFinished.connect(self._compare_file_changed)
        self.in_compare_source.toggled.connect(lambda _on: self._compare_changed())

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
        self.in_ci.lineEdit().setPlaceholderText("ACEScg (the default)")
        self.in_co.lineEdit().setPlaceholderText("out_rec709 (vfx-transcode's default)")
        self.in_display.lineEdit().setPlaceholderText("(none: -ci to -co)")
        self.in_view.lineEdit().setPlaceholderText("(the display's default view)")
        self.in_looks.lineEdit().setPlaceholderText("(none; several: comma separated)")
        self.ocio_info = QLabel("")
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

    def _input_changed(self):
        """A new input (or movie first frame): find its sequence or probe the movie, and set
        the timeline to its frames."""
        path = self.in_input1.text().strip()
        name = Path(path).name if path else ""
        self.setWindowTitle("{} - vfx-transcode tuner".format(name) if name else "vfx-transcode tuner")
        info = core.describe_input(path, self.presets, self.in_first_frame.value())
        self.input_info = info
        self.in_detected.setText(info.error or info.text)
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
                self.compare_pixmap = None if image.isNull() else QPixmap.fromImage(image)
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
        self.tr_sub = compact_combo(QComboBox(), 8)
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
        self.tr_steps = StepList()
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
        self.bu_opacity = opacity_spin()
        self.bu_size = QLineEdit()
        self.bu_box = QCheckBox("box")
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
            widgets["size"].setFixedWidth(40)
            grid.addWidget(QLabel(PLACE_NAMES[place]), row, 0)
            for column, key in enumerate(("text", "own", "color", "opacity", "size", "box"), 1):
                grid.addWidget(widgets[key], row, column)
            self.bu_places[place] = widgets
            self._connect(*widgets.values())
        grid.setColumnStretch(1, 1)
        layout.addWidget(places)

        margins = QGroupBox("Margins (pixels or %; left/right % of width, top/bottom % of height)")
        row = QHBoxLayout(margins)
        self.bu_margins = {}
        for side in MARGIN_SIDES:
            edit = QLineEdit()
            edit.setFixedWidth(60)
            row.addWidget(QLabel(side))
            row.addWidget(edit)
            self.bu_margins[side] = edit
            self._connect(edit)
        layout.addWidget(margins)
        self.bu_font = QLineEdit()
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
        self.sl_preset = compact_combo(QComboBox(), 12)
        self.sl_preset.setToolTip("Pick a slate preset to load it (and switch the slate on)")
        self.sl_preset.activated.connect(lambda _i: self._load_slate())
        top.addWidget(self.sl_enabled)
        top.addWidget(self.sl_preset, 1)
        top.addWidget(self._save_button("slate"))
        layout.addLayout(top)
        form = QFormLayout()
        self.sl_title, self.sl_subtitle = QLineEdit(), QLineEdit()
        form.addRow("Title", self.sl_title)
        form.addRow("Subtitle", self.sl_subtitle)
        layout.addLayout(form)
        self.sl_fields = PairTable(["Label", "Value (keywords)"])
        layout.addWidget(self.sl_fields)
        form = QFormLayout()
        self.sl_thumbnail = combo(["middle", "first", "last", "none"], editable=True)
        self.sl_hide_empty = QCheckBox("hide rows whose value is empty")
        self.sl_rule = QCheckBox("line under the title")
        self.sl_burnin = QCheckBox("burn-ins on the slate too")
        self.sl_title_size, self.sl_subtitle_size, self.sl_size = QLineEdit(), QLineEdit(), QLineEdit()
        self.sl_scale = QDoubleSpinBox()
        self.sl_scale.setRange(0.05, 1.0)
        self.sl_scale.setSingleStep(0.05)
        self.sl_valign = combo(["center", "top"])
        form.addRow("Thumbnail", self.sl_thumbnail)
        form.addRow("", self.sl_hide_empty)
        form.addRow("", self.sl_rule)
        form.addRow("", self.sl_burnin)
        form.addRow("Title size", self.sl_title_size)
        form.addRow("Subtitle size", self.sl_subtitle_size)
        form.addRow("Row size", self.sl_size)
        form.addRow("Scale", self.sl_scale)
        form.addRow("Vertical", self.sl_valign)
        self.sl_colors = {}
        for key, label in (("background", "Background"), ("title_color", "Title"),
                           ("subtitle_color", "Subtitle"), ("value_color", "Values"),
                           ("label_color", "Labels"), ("line_color", "Line")):
            self.sl_colors[key] = ColorButton()
            form.addRow(label + " colour", self.sl_colors[key])
            self._connect(self.sl_colors[key])
        layout.addLayout(form)
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
        self.sl_logo.setPlaceholderText("none - a PNG with alpha (-slate_logo)")
        self.sl_logo_position = QComboBox()
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
        margins.addWidget(QLabel("y"))
        margins.addWidget(self.sl_logo_y)
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
                "transform_sequence": s.transform.preset, "burnin": s.burn.preset,
                "slate": s.slate.preset, "filmstrip": s.filmstrip.preset}[area]

    def _save_preset(self, area):
        """Ask for a name (and sub-preset, notes), then save this area's settings into the
        presets file in use, multi-user safe. The new preset is selected afterwards."""
        self._read_all()
        if area == "transform_sequence" and not self.state.transform.args:
            QMessageBox.information(self, "Save preset", "There are no oiiotool steps to save.")
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
        box = {"colour": self.co_preset, "transform": self.ff_preset,
               "transform_sequence": self.tr_preset, "burnin": self.bu_preset,
               "slate": self.sl_preset, "filmstrip": self.fs_preset}[area]
        set_combo(box, name)
        if area == "transform_sequence":
            set_combo(self.tr_sub, sub)
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
        self.fs_tiles = QSpinBox()
        self.fs_tiles.setRange(1, 1000)
        self.fs_width, self.fs_height = QSpinBox(), QSpinBox()
        for spin in (self.fs_width, self.fs_height):
            spin.setRange(16, 8192)
        size = QHBoxLayout()
        size.addWidget(self.fs_width)
        size.addWidget(QLabel("x"))
        size.addWidget(self.fs_height)
        self.fs_columns, self.fs_rows = QSpinBox(), QSpinBox()
        for spin in (self.fs_columns, self.fs_rows):
            spin.setRange(0, 1000)
            spin.setSpecialValueText("auto")
        grid = QHBoxLayout()
        grid.addWidget(self.fs_columns)
        grid.addWidget(QLabel("x"))
        grid.addWidget(self.fs_rows)
        self.fs_quality = QSpinBox()
        self.fs_quality.setRange(1, 100)
        self.fs_source = combo(list(core.FILMSTRIP_SOURCES))
        self.fs_slate = QCheckBox("the slate as the first tile (switch the slate on in the Slate tab)")
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
        self.md_timecode.setPlaceholderText("HH:MM:SS:FF (timecode source arg)")
        self.md_reel_source = combo([""] + list(core.REEL_SOURCES))
        self.md_reel_source.setToolTip("auto: the Reel name field, else the header attribute, else "
                                       "none. arg: the field only. metadata: the header attribute, "
                                       "else the field. off: no reel name")
        self.md_reel = QLineEdit()
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

    def _build_metadata(self):
        """The Metadata section of the Output tab (a movie, or an image sequence written by oiiotool)."""
        self.md_group = QGroupBox("Metadata")
        layout = QVBoxLayout(self.md_group)
        form = QFormLayout()
        self.md_preset = QComboBox()
        form.addRow("Metadata preset", self.md_preset)
        layout.addLayout(form)
        layout.addWidget(QLabel("Extra tags (KEY / VALUE, keywords ok; empty value removes a "
                                "preset's tag):"))
        self.md_tags = PairTable(["Key", "Value"])
        self.md_tags.setMinimumHeight(140)
        layout.addWidget(self.md_tags)
        self._connect(self.md_preset, self.md_tags)
        return self.md_group

    # output tab

    # output tab (the encode)

    def _build_encode(self, page):
        form = QFormLayout(page)
        self.sp_type = QComboBox()
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
        form.addRow("", self.alpha_note)
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
        self._connect(self.alpha_mode, self.out_write_tc, self.out_write_reel)
        form.addRow(self._build_metadata())
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
        self.out_bat.toggled.connect(self.update_outputs)
        layout.addWidget(self.out_bat)
        layout.addWidget(QLabel("<b>Full command</b> (the movie, next to the source):"))
        self.out_full = QPlainTextEdit()
        self.out_full.setReadOnly(True)
        self.out_full.setMaximumHeight(110)
        layout.addWidget(self.out_full)
        self.out_boxes = {}
        for key, title in (("transform_sequence", "Transform Sequence (oiiotool)"),
                           ("transform", "Transform (ffmpeg)"), ("burnin", "Burn-in"),
                           ("slate", "Slate"), ("filmstrip", "Filmstrip"), ("metadata", "Metadata")):
            box = QGroupBox(title)
            inner = QVBoxLayout(box)
            cli_box, json_box = QPlainTextEdit(), QPlainTextEdit()
            for widget, height in ((cli_box, 60), (json_box, 110)):
                widget.setReadOnly(True)
                widget.setMaximumHeight(height)
                inner.addWidget(widget)
            json_box.setPlaceholderText("presets.json snippet")
            self.out_boxes[key] = (cli_box, json_box)
            layout.addWidget(box)
        layout.addStretch()

    # info tab (metadata viewer)

    def _build_info(self, page):
        layout = QVBoxLayout(page)
        self.info_search = QLineEdit()
        self.info_search.setPlaceholderText("Search keys and values")
        self.info_search.setClearButtonEnabled(True)
        self.info_search.textChanged.connect(lambda _text: self._fill_info())
        layout.addWidget(self.info_search)
        self.info = QTreeWidget()
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
                           (self.md_preset, ["", "none"] + list(self.presets.metadata_presets))):
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            set_combo(box, current)
            fit_popup(box)
            box.blockSignals(False)
        self._fill_encode_presets()
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
        self.in_compare_source.setChecked(i.compare_source)
        self.in_convert.setChecked(i.convert_colour)
        index = self.sp_type.findData(i.output_type)
        self.sp_type.setCurrentIndex(index if index >= 0 else 0)
        self._fill_seq_presets()
        index = self.alpha_mode.findData(i.alpha)
        self.alpha_mode.setCurrentIndex(index if index >= 0 else 0)
        self.out_write_tc.setChecked(i.write_timecode)
        self.out_write_reel.setChecked(i.write_reelid)
        self.timeline.set_trim(i.trim_in, i.trim_out)
        self.in_first_frame.setValue(i.movie_first_frame)
        self.loading = False
        self._fill_transform()
        self._fill_burn()
        self._fill_slate()
        self._fill_filmstrip()
        self._fill_letterbox()
        self._fill_metadata()

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
        self.loading = False

    def _read_all(self):
        i = self.state.input
        i.input1 = self.in_input1.text().strip()
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
        self.state.input.write_timecode = self.out_write_tc.isChecked()
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

        self._read_letterbox_group(self.lb, self.state.letterbox)

        m = self.state.metadata
        m.preset = self.md_preset.currentText()
        m.timecode_source, m.timecode = self.md_tc_source.currentText(), self.md_timecode.text()
        m.reelid_source, m.reelid = self.md_reel_source.currentText(), self.md_reel.text()
        m.tags = self.md_tags.pairs()

    # outputs

    def update_outputs(self, *_args):
        if not self.presets:
            return
        full, sections = core.output_sections(self.state, self.presets, self._active_sub(),
                                              self.sp_output.text().strip() or "./")
        self._update_pipeline()
        fix = (lambda text: text.replace("%", "%%")) if self.out_bat.isChecked() else (lambda t: t)
        self.out_full.setPlainText(fix(full))
        for key, (cli_text, json_text) in sections.items():
            cli_box, json_box = self.out_boxes[key]
            cli_box.setPlainText(fix(cli_text))
            json_box.setPlainText(json_text)

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
        self._update_active_sub()
        self.update_outputs()
        self.status.setText("")
        self._update_overlay()
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
        p = core.pipeline(self.state.input.input1, self.state.input.output_type)
        wanted = {"Color": p.oiio, "Transform Sequence": p.oiio, "Transform": p.ffmpeg,
                  "Burn-in": p.ffmpeg, "Slate": p.movie}
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
                "EXR to EXR" if core.input_kind(self.state.input.input1) == "exr"
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
        return QPixmap.fromImage(QImage(path))       # QImage: no stale cache between runs

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
        self.viewer.set_images(main, compare, par)
        self.viewer.set_mix(self.mix.value() / 100.0 if self.mix.isEnabled() else 0.0)
        if main is not None and mode != self.shown_mode:
            self.viewer.fit()                        # a filmstrip and a frame differ in shape
            self.shown_mode = mode

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
