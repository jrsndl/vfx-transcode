"""vfx-transcode-tuner: tune presets visually.

The window edits a core.TunerState, runs `vfx-transcode ... -preview FILE.png` for every refresh
and shows FILE.png (or FILE_slate.png) with pan / zoom and an optional compare image.
"""

import argparse
import itertools
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPixmap, QTransform
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsPixmapItem,
                               QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from vfx_transcode.burnin import BURN_PLACES, MARGIN_SIDES, PLACE_NAMES
from vfx_transcode.presets import DEFAULT_PRESETS_FILE, load_presets
from vfx_transcode.tools import app_dir, resolve_path, split_args

from . import core

REFRESH_DELAY_MS = 500


# --- small widgets --------------------------------------------------------------------------

class ColorButton(QPushButton):
    """Shows a colour; click to pick. value: "#rrggbb" or a colour name."""
    changed = Signal()

    def __init__(self, value="white", parent=None):
        super().__init__(parent)
        self.setFixedWidth(70)
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
        self.setText(self._value)

    def _pick(self):
        colour = QColorDialog.getColor(QColor(self._value), self, "Colour")
        if colour.isValid():
            self.set_value(colour.name())
            self.changed.emit()


def opacity_spin(value=1.0):
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 1.0)
    spin.setSingleStep(0.05)
    spin.setDecimals(2)
    spin.setValue(value)
    return spin


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
        start = os.path.dirname(edit.text()) if edit.text() else ""
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
        self.state.input.presets_file = presets_file
        self.presets = None
        self.report = None
        self.loading = False                  # True while widgets are filled from the state
        self.work_dir = Path(tempfile.mkdtemp(prefix="vfx-transcode-tuner-"))
        self.run_counter = itertools.count(1)
        self.process = None
        self.pending = False
        self.images = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        self._load_presets()
        self._build()
        self._fill_all()
        self.update_outputs()
        if input1:
            QTimer.singleShot(200, self.refresh)

    # presets

    def _presets_path(self):
        return resolve_path(self.state.input.presets_file or DEFAULT_PRESETS_FILE, app_dir())

    def _load_presets(self):
        try:
            self.presets = load_presets(self._presets_path())
        except Exception as exc:                  # show it, keep the old presets
            QMessageBox.warning(self, "Presets", str(exc))

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
        self.show_slate = QCheckBox("Slate")
        self.show_slate.toggled.connect(self.show_images)
        self.mix = QSlider(Qt.Orientation.Horizontal)
        self.mix.setRange(0, 100)
        self.mix.setFixedWidth(160)
        self.mix.valueChanged.connect(lambda v: self.viewer.set_mix(v / 100.0))
        self.status = QLabel("")
        for widget in (self.refresh_button, self.auto_refresh, fit, one, self.show_slate,
                       QLabel("  Compare mix"), self.mix, QLabel("  "), self.status):
            toolbar.addWidget(widget)

        self.viewer = Viewer()
        fit.clicked.connect(self.viewer.fit)
        one.clicked.connect(self.viewer.one_to_one)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel("Log"))
        log_bar.addStretch()
        clear_log = QPushButton("Clear")
        clear_log.clicked.connect(self.log.clear)
        log_bar.addWidget(clear_log)
        log_layout.addLayout(log_bar)
        log_layout.addWidget(self.log)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.viewer)
        left.addWidget(log_box)
        left.setSizes([700, 150])

        self.tabs = QTabWidget()
        for title, builder in (("Input", self._build_input), ("Transform", self._build_transform),
                               ("Burn-in", self._build_burn), ("Slate", self._build_slate),
                               ("Letterbox", self._build_letterbox),
                               ("Metadata", self._build_metadata), ("Output", self._build_output),
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

    # input tab

    def _build_input(self, page):
        form = QFormLayout(page)
        self.in_input1 = QLineEdit()
        self.in_input2 = QLineEdit()
        self.in_frame = combo(["middle", "first", "last"], editable=True)
        self.in_presets = QLineEdit()
        self.in_encode = QComboBox()
        self.in_ci, self.in_co, self.in_display, self.in_view, self.in_looks = (
            QLineEdit() for _ in range(5))
        self.in_clf, self.in_ocio, self.in_extra, self.in_tool = (QLineEdit() for _ in range(4))
        self.in_tool.setText(core.quote(core.vfx_transcode_command()))
        form.addRow("Test footage (any frame)", browse_row(self.in_input1, "Test footage"))
        form.addRow("Compare image", browse_row(self.in_input2, "Compare image",
                                                filters="Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)"))
        form.addRow("Preview frame", self.in_frame)
        form.addRow("Presets file", browse_row(self.in_presets, "presets.json",
                                               filters="JSON (*.json)"))
        form.addRow("Encode preset", self.in_encode)
        form.addRow(QLabel("<b>Colour</b> (empty = the tool's default)"))
        form.addRow("-ci colour in", self.in_ci)
        form.addRow("-co colour out", self.in_co)
        form.addRow("-display", self.in_display)
        form.addRow("-view", self.in_view)
        form.addRow("-looks", self.in_looks)
        form.addRow("-clf (keywords ok)", browse_row(self.in_clf, "CLF", filters="CLF (*.clf)"))
        form.addRow("-ocio config", browse_row(self.in_ocio, "OCIO config", filters="OCIO (*.ocio)"))
        form.addRow("Other options", self.in_extra)
        form.addRow("vfx-transcode command", self.in_tool)
        self._connect(self.in_input1, self.in_frame, self.in_encode, self.in_ci,
                      self.in_co, self.in_display, self.in_view, self.in_looks, self.in_clf,
                      self.in_ocio, self.in_extra)
        self.in_input2.editingFinished.connect(self._compare_changed)
        self.in_presets.editingFinished.connect(self._presets_changed)

    def _compare_changed(self):
        self.state.input.input2 = self.in_input2.text().strip()
        self.show_images()

    def _presets_changed(self):
        self.state.input.presets_file = self.in_presets.text().strip()
        self._load_presets()
        self._fill_preset_lists()
        self._changed()

    # transform tab

    def _build_transform(self, page):
        layout = QVBoxLayout(page)
        box = QGroupBox("Load from presets.json")
        form = QFormLayout(box)
        self.tr_preset = QComboBox()
        self.tr_sub = QComboBox()
        self.tr_active = QLabel("")
        self.tr_conditions = QLabel("")
        self.tr_conditions.setWordWrap(True)
        load = QPushButton("Load sub-preset")
        load.clicked.connect(self._load_transform)
        self.tr_preset.currentTextChanged.connect(self._transform_preset_selected)
        self.tr_sub.currentTextChanged.connect(self._transform_sub_selected)
        form.addRow("Transform preset", self.tr_preset)
        form.addRow("Sub-preset", self.tr_sub)
        form.addRow("Conditions", self.tr_conditions)
        form.addRow("Active for this input", self.tr_active)
        form.addRow("", load)
        layout.addWidget(box)
        self.tr_use = QCheckBox("Use these oiiotool steps (else the -width resize in oiiotool)")
        self.tr_steps = StepList()
        self.tr_width = QSpinBox()
        self.tr_width.setRange(0, 16384)
        self.tr_width.setSpecialValueText("0 (keep size)")
        layout.addWidget(self.tr_use)
        layout.addWidget(QLabel("oiiotool steps, one per line (double-click to edit; keywords "
                                "like {input_width} and oiiotool {TOP.width} work):"))
        layout.addWidget(self.tr_steps)
        row = QFormLayout()
        row.addRow("Final ffmpeg resize: -width", self.tr_width)
        layout.addLayout(row)
        layout.addStretch()
        self._connect(self.tr_use, self.tr_steps, self.tr_width)

    def _transform_preset_selected(self, name):
        self.tr_sub.blockSignals(True)
        self.tr_sub.clear()
        if self.presets and name in self.presets.transforms:
            self.tr_sub.addItems([s.name for s in self.presets.transforms[name].subs])
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
        self.state.transform = core.load_transform(self.presets, name, sub, self.tr_width.value())
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
        self.bu_preset = QComboBox()
        load = QPushButton("Load preset")
        load.clicked.connect(self._load_burn)
        top.addWidget(QLabel("Burn-in preset"))
        top.addWidget(self.bu_preset, 1)
        top.addWidget(load)
        layout.addLayout(top)

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
        for column, head in enumerate(("", "text", "own", "colour", "opacity", "size", "box")):
            grid.addWidget(QLabel("<i>{}</i>".format(head)), 0, column)
        self.bu_places = {}
        for row, place in enumerate(BURN_PLACES, 1):
            widgets = {"text": QLineEdit(), "own": QCheckBox(), "color": ColorButton(),
                       "opacity": opacity_spin(), "size": QLineEdit(), "box": QCheckBox()}
            widgets["size"].setFixedWidth(60)
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
        layout.addWidget(QLabel("Keywords: {clean_name} {current_frame} {frame_start} {frame_end} "
                                "{tc} {timecode} {fps} {date} {preset} {meta:ATTR} "
                                "{version_number} {namesplit1} ...   \\n = new line"))
        layout.addStretch()
        self._connect(self.bu_color, self.bu_opacity, self.bu_size, self.bu_box, self.bu_font)

    def _load_burn(self):
        name = self.bu_preset.currentText()
        preset = self.presets.burnins.get(name) if self.presets else None
        self.state.burn = core.burn_from_preset(preset)
        self._fill_burn()
        self._changed()

    # slate tab

    def _build_slate(self, page):
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        self.sl_enabled = QCheckBox("Slate")
        self.sl_preset = QComboBox()
        load = QPushButton("Load preset")
        load.clicked.connect(self._load_slate)
        top.addWidget(self.sl_enabled)
        top.addWidget(self.sl_preset, 1)
        top.addWidget(load)
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
        layout.addStretch()
        self._connect(self.sl_enabled, self.sl_title, self.sl_subtitle, self.sl_fields,
                      self.sl_thumbnail, self.sl_hide_empty, self.sl_rule, self.sl_burnin,
                      self.sl_title_size, self.sl_subtitle_size, self.sl_size, self.sl_scale,
                      self.sl_valign)

    def _load_slate(self):
        name = self.sl_preset.currentText()
        spec = self.presets.slates.get(name) if self.presets else None
        self.state.slate = core.slate_from_spec(spec, name if spec else "")
        self._fill_slate()
        self._changed()

    # letterbox tab

    def _build_letterbox(self, page):
        form = QFormLayout(page)
        self.lb_enabled = QCheckBox("Letterbox / pillarbox")
        self.lb_ratio = combo(["2.39", "2.35", "2.0", "1.85", "16:9", "4:3", "1:1"], editable=True)
        self.lb_color = ColorButton()
        self.lb_opacity = opacity_spin()
        self.lb_line = QSpinBox()
        self.lb_line.setRange(0, 200)
        self.lb_line_color = ColorButton()
        self.lb_line_opacity = opacity_spin()
        form.addRow("", self.lb_enabled)
        form.addRow("Ratio", self.lb_ratio)
        form.addRow("Bar colour", self.lb_color)
        form.addRow("Bar opacity", self.lb_opacity)
        form.addRow("Line thickness (px)", self.lb_line)
        form.addRow("Line colour", self.lb_line_color)
        form.addRow("Line opacity", self.lb_line_opacity)
        self._connect(self.lb_enabled, self.lb_ratio, self.lb_color, self.lb_opacity, self.lb_line,
                      self.lb_line_color, self.lb_line_opacity)

    # metadata tab

    def _build_metadata(self, page):
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.md_preset = QComboBox()
        self.md_tc_source = combo(["", "counter", "arg", "exr"])
        self.md_timecode = QLineEdit()
        self.md_reel_source = combo(["", "auto", "arg", "exr", "off"])
        self.md_reel = QLineEdit()
        form.addRow("Metadata preset", self.md_preset)
        form.addRow("Timecode source", self.md_tc_source)
        form.addRow("Timecode", self.md_timecode)
        form.addRow("Reel name source", self.md_reel_source)
        form.addRow("Reel name", self.md_reel)
        layout.addLayout(form)
        layout.addWidget(QLabel("Extra tags (KEY / VALUE, keywords ok; empty value removes a "
                                "preset's tag):"))
        self.md_tags = PairTable(["Key", "Value"])
        layout.addWidget(self.md_tags)
        layout.addStretch()
        self._connect(self.md_preset, self.md_tc_source, self.md_timecode, self.md_reel_source,
                      self.md_reel, self.md_tags)

    # output tab

    def _build_output(self, page):
        layout = QVBoxLayout(page)
        self.out_bat = QCheckBox("For a .bat file (% written as %%)")
        self.out_bat.toggled.connect(self.update_outputs)
        layout.addWidget(self.out_bat)
        layout.addWidget(QLabel("<b>Full command</b> (the movie, next to the source):"))
        self.out_full = QPlainTextEdit()
        self.out_full.setReadOnly(True)
        self.out_full.setMaximumHeight(110)
        layout.addWidget(self.out_full)
        self.out_boxes = {}
        for key, title in (("transform", "Transform"), ("burnin", "Burn-in"), ("slate", "Slate"),
                           ("letterbox", "Letterbox"), ("metadata", "Metadata")):
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
        for box, names in ((self.tr_preset, list(self.presets.transforms)),
                           (self.bu_preset, list(self.presets.burnins)),
                           (self.sl_preset, list(self.presets.slates)),
                           (self.md_preset, ["", "none"] + list(self.presets.metadata_presets)),
                           (self.in_encode, [""] + list(self.presets.presets))):
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            set_combo(box, current)
            box.blockSignals(False)
        self.loading = False
        self._transform_preset_selected(self.tr_preset.currentText())

    def _fill_all(self):
        self._fill_preset_lists()
        self.loading = True
        i = self.state.input
        for edit, value in ((self.in_input1, i.input1), (self.in_input2, i.input2),
                            (self.in_presets, i.presets_file), (self.in_ci, i.colorin),
                            (self.in_co, i.colorout), (self.in_display, i.display),
                            (self.in_view, i.view), (self.in_looks, i.looks), (self.in_clf, i.clf),
                            (self.in_ocio, i.ocio), (self.in_extra, i.extra)):
            edit.setText(value)
        set_combo(self.in_frame, i.frame)
        set_combo(self.in_encode, i.encode_preset)
        self.loading = False
        self._fill_transform()
        self._fill_burn()
        self._fill_slate()
        self._fill_letterbox()
        self._fill_metadata()

    def _fill_transform(self):
        self.loading = True
        t = self.state.transform
        self.tr_use.setChecked(t.use)
        self.tr_steps.set_steps(t.args)
        self.tr_width.setValue(t.width)
        if t.preset:
            set_combo(self.tr_preset, t.preset)
            set_combo(self.tr_sub, t.sub)
        self.loading = False

    def _fill_burn(self):
        self.loading = True
        b = self.state.burn
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
        self.loading = False

    def _fill_letterbox(self):
        self.loading = True
        lb = self.state.letterbox
        self.lb_enabled.setChecked(lb.enabled)
        set_combo(self.lb_ratio, lb.ratio)
        self.lb_color.set_value(lb.color)
        self.lb_opacity.setValue(lb.opacity)
        self.lb_line.setValue(lb.line)
        self.lb_line_color.set_value(lb.line_color)
        self.lb_line_opacity.setValue(lb.line_opacity)
        self.loading = False

    def _fill_metadata(self):
        self.loading = True
        m = self.state.metadata
        set_combo(self.md_preset, m.preset)
        set_combo(self.md_tc_source, m.timecode_source)
        self.md_timecode.setText(m.timecode)
        set_combo(self.md_reel_source, m.reelid_source)
        self.md_reel.setText(m.reelid)
        self.md_tags.set_pairs(m.tags)
        self.loading = False

    def _read_all(self):
        i = self.state.input
        i.input1 = self.in_input1.text().strip()
        i.frame = self.in_frame.currentText().strip() or "middle"
        i.encode_preset = self.in_encode.currentText()
        i.colorin, i.colorout = self.in_ci.text(), self.in_co.text()
        i.display, i.view, i.looks = self.in_display.text(), self.in_view.text(), self.in_looks.text()
        i.clf, i.ocio, i.extra = self.in_clf.text(), self.in_ocio.text(), self.in_extra.text()

        t = self.state.transform
        t.use = self.tr_use.isChecked()
        t.args = self.tr_steps.steps()
        t.width = self.tr_width.value()

        b = self.state.burn
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

        lb = self.state.letterbox
        lb.enabled, lb.ratio = self.lb_enabled.isChecked(), self.lb_ratio.currentText().strip()
        lb.color, lb.opacity = QColor(self.lb_color.value()).name(), self.lb_opacity.value()
        lb.line = self.lb_line.value()
        lb.line_color = QColor(self.lb_line_color.value()).name()
        lb.line_opacity = self.lb_line_opacity.value()

        m = self.state.metadata
        m.preset = self.md_preset.currentText()
        m.timecode_source, m.timecode = self.md_tc_source.currentText(), self.md_timecode.text()
        m.reelid_source, m.reelid = self.md_reel_source.currentText(), self.md_reel.text()
        m.tags = self.md_tags.pairs()

    # outputs

    def update_outputs(self, *_args):
        if not self.presets:
            return
        full, sections = core.output_sections(self.state, self.presets, self._active_sub())
        fix = (lambda text: text.replace("%", "%%")) if self.out_bat.isChecked() else (lambda t: t)
        self.out_full.setPlainText(fix(full))
        for key, (cli_text, json_text) in sections.items():
            cli_box, json_box = self.out_boxes[key]
            cli_box.setPlainText(fix(cli_text))
            json_box.setPlainText(json_text)

    # running vfx-transcode

    def refresh(self):
        self._read_all()
        if not self.state.input.input1:
            self.status.setText("Choose the test footage on the Input tab.")
            return
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            self.pending = True                 # finish, then run again with the new settings
            self.process.kill()
            return
        run = next(self.run_counter)
        image = self.work_dir / "preview_{}.png".format(run)
        tool = split_args(self.in_tool.text()) or core.vfx_transcode_command()
        cmd = core.preview_command(self.state, image, tool)
        self.log.appendPlainText("\n> " + core.quote(cmd))
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(lambda code, _status, image=image: self._finished(code, image))
        self.status.setText("Processing...")
        self.refresh_button.setEnabled(False)
        self.process.start(cmd[0], cmd[1:])

    def _read_output(self):
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.log.appendPlainText(data.rstrip())

    def _finished(self, code, image):
        self.refresh_button.setEnabled(True)
        if self.pending:
            self.pending = False
            self.status.setText("")
            QTimer.singleShot(0, self.refresh)
            return
        if code != 0:
            self.status.setText("vfx-transcode failed (exit {}), see the log".format(code))
            return
        try:
            with open(str(image.with_suffix(".json")), encoding="utf-8") as fh:
                self.report = json.load(fh)
        except (OSError, ValueError) as exc:
            self.status.setText("No preview written: {}".format(exc))
            return
        self.images = {"image": self._load_pixmap(self.report.get("image")),
                       "slate": self._load_pixmap(self.report.get("slate"))}
        self.show_images()
        self._fill_info()
        self._update_active_sub()
        self.update_outputs()
        out = self.report.get("output", {})
        self.status.setText("Frame {}  {}x{}  {}".format(self.report.get("frame"), out.get("width"),
                                                          out.get("height"), out.get("transform") or ""))
        self._clean_old_previews(image)

    def _load_pixmap(self, path):
        if not path or not os.path.isfile(path):
            return None
        return QPixmap.fromImage(QImage(path))       # QImage: no stale cache between runs

    def show_images(self):
        want_slate = self.show_slate.isChecked()
        main = self.images.get("slate" if want_slate else "image")
        if want_slate and main is None:
            self.status.setText("No slate: switch it on in the Slate tab.")
        compare = None
        path = self.state.input.input2 or self.in_input2.text().strip()
        if path and os.path.isfile(path):
            compare = QPixmap.fromImage(QImage(path))
        self.mix.setEnabled(compare is not None and not compare.isNull())
        par = float((self.report or {}).get("output", {}).get("par", 1.0) or 1.0)
        self.viewer.set_images(main, compare, par)
        self.viewer.set_mix(self.mix.value() / 100.0 if self.mix.isEnabled() else 0.0)

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
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(2000)
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
    parser.add_argument("input1", nargs="?", default="", help="Test footage: any frame of a sequence")
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
