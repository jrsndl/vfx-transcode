"""vfx-transcode-process: convert files or whole folders with a super-preset, without the
tuner's knobs.

Process tab: Source (a file, or a folder searched with the patterns beside it), Preset (the
super-presets, each checked against the source), Destination (path and name, with presets).
Process puts the jobs in the queue and starts it; Add to Queue only adds them; optionally the
output is played or shown when done. Queue tab: the jobs with their progress; pause, cancel, retry, reorder, and
Send to Deadline. The queue is kept between sessions.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import (QItemSelectionModel, QModelIndex, QObject, QProcess, QProcessEnvironment, QSettings, QSize, QStandardPaths, Qt,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame,
                               QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QProgressDialog, QPushButton, QRadioButton, QSizePolicy, QSlider, QSpinBox, QSplitter, QStyle, QToolButton,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from vfx_transcode import movie as mv
from vfx_transcode import studio
from vfx_transcode import super_presets as sp
from vfx_transcode.errors import TranscodeError
from vfx_transcode.presets import load_presets
from vfx_transcode.tools import app_dir, resolve_path
from vfx_tuner import tooltips
from vfx_tuner.app import apply_dark_theme

from . import core

ORG, APP = "vfx-transcode", "process"
POLL_MS = 1000
MARKS = {core.OK: "✔", core.WARN: "⚠", core.ERROR: "✖"}
CLEARED_ON_START = (core.DONE, core.FARM_DONE)
GO_STYLE = ("QPushButton { background: #2f6f3e; color: white; }"     # as the tuner's Process
            "QPushButton:hover { background: #378048; }"
            "QPushButton:disabled { background: #2b3a2f; color: #7a8a7e; }")
STATUS_COLOURS = {core.DONE: "#7ec27e", core.FARM_DONE: "#7ec2b0", core.FAILED: "#e07070",
                  core.CANCELLED: "#a0a0a0",
                  core.SUBMITTED: "#7fb0e8", core.RUNNING: "#e8d27f", core.WAITING: "#dcdcdc"}
INPUT_FILTER = ("Images and movies (*.exr *.png *.jpg *.jpeg *.tif *.tiff *.dpx *.tga *.bmp *.hdr {});;"
                "All files (*)".format(" ".join("*" + e for e in mv.MOVIE_EXTS)))


def settings():
    return QSettings(ORG, APP)


def _json_setting(key, default):
    try:
        value = settings().value(key, "")
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _set_json_setting(key, value):
    settings().setValue(key, json.dumps(value))


def saved_deadline_settings():
    """The Deadline settings as the Send dialog starts with them: vfx-transcode.ini, then
    what this user last used (the password only from the ini / environment)."""
    s = core.DeadlineSettings.from_ini()
    for key, value in _json_setting("deadline", {}).items():
        key = core.DEADLINE_KEY_ALIASES.get(key, key)
        if key in ("executable", "department") and not str(value).strip():
            continue
        if hasattr(s, key) and key not in core.DeadlineSettings.PRIVATE:
            setattr(s, key, value)
    return s


def queue_file():
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(folder or tempfile.gettempdir()) / "queue.json"


# --- folder search ----------------------------------------------------------------------------

class Scanner(QThread):
    """Lists a folder and all its sub-folders once (core.walk_media); the app keeps the listing,
    so a new filter is applied without scanning again."""
    progressed = Signal(str, int, int)        # folder, files scanned, media found
    found_more = Signal(str, list)            # folder, the listing so far (while scanning)
    listed = Signal(str, list)                # folder, listing

    def __init__(self, folder, patterns, parent=None):
        super().__init__(parent)
        self.folder, self.patterns = folder, patterns
        self.stop = False
        self.error = ""
        self._last = 0.0
        self._last_partial = 0.0
        self.partial_every = 0.8              # seconds; the app stretches it when a refill is slow

    def _partial(self, listing):
        now = time.time()
        if now - self._last_partial > self.partial_every:   # the Files tab fills meanwhile
            self._last_partial = now
            self.found_more.emit(self.folder, core.sort_listing(listing))

    def _progress(self, scanned, found):
        now = time.time()
        if now - self._last > 0.1:            # the status bar needn't redraw for every folder
            self._last = now
            self.progressed.emit(self.folder, scanned, found)

    def run(self):
        try:
            listing = core.walk_media(self.folder, self.patterns, lambda: self.stop, self._progress,
                                      self._partial)
        except Exception as exc:              # an unreadable share etc.: say so
            self.error = str(exc)
            listing = []
        self.listed.emit(self.folder, listing)


# --- Deadline ---------------------------------------------------------------------------------

class DeadlineDialog(QDialog):
    """Deadline submission settings: the studio's from vfx-transcode.ini [deadline] and
    [deadline_web_service], then what this user last used (never the password)."""

    def __init__(self, count_selected, count_waiting, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Send to Deadline")
        s = saved_deadline_settings()
        s.batch_name = "vfx-transcode {}".format(time.strftime("%Y-%m-%d %H:%M"))
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.fields = {}

        def line(key, label, tip=""):
            edit = QLineEdit(str(getattr(s, key)))
            edit.setToolTip(tip)
            self.fields[key] = edit
            form.addRow(label, edit)
            return edit

        def spin(key, label, top, tip=""):
            box = QSpinBox()
            box.setToolTip(tip)
            box.setRange(0, top)
            box.setValue(int(getattr(s, key)))
            self.fields[key] = box
            form.addRow(label, box)

        line("batch_name", "Batch name:", "Deadline batch the jobs are grouped under")
        line("comment", "Comment:", "Comment shown with each Deadline job")
        def choice(key, label, tip):
            # a text field that becomes a list once Deadline has told its pools / groups
            box = QComboBox()
            box.setEditable(True)
            box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            box.setEditText(str(getattr(s, key)))
            box.setToolTip(tip)
            self.fields[key] = box
            form.addRow(label, box)

        choice("pool", "Pool:", "Deadline pool (vfx-transcode.ini [deadline] pool); the list comes "
                                "from Deadline, or type one")
        choice("secondary_pool", "Secondary pool:", "Deadline secondary pool, used when the pool "
                                                    "is busy; the list comes from Deadline, or type one")
        choice("group", "Group:", "Deadline group: which machines may run the jobs (vfx-transcode.ini "
                                  "[deadline] group); the list comes from Deadline, or type one")
        self.lists_note = QLabel("Asking Deadline for its pools and groups...")
        self.lists_note.setWordWrap(True)
        self.lists_note.setStyleSheet("color: #8a8a8a;")
        form.addRow("", self.lists_note)
        spin("priority", "Priority:", 100, "Deadline priority, 0-100 (vfx-transcode.ini [deadline] priority)")
        spin("machine_limit", "Machine limit:", 1000, "How many machines may work on a job at once; 0 = no limit")
        spin("concurrent_tasks", "Concurrent tasks:", 16, "How many of the jobs' tasks one machine runs at once")
        line("department", "Department:", "Deadline department, for sorting and filtering in the Monitor")
        line("executable", "vfx-transcode on the farm:",
             "The vfx-transcode the render nodes run, on a path every node has "
             "(vfx-transcode.ini [deadline] executable; default " + core.FARM_EXECUTABLE + ")")
        self.suspended = QCheckBox("Submit suspended")
        self.suspended.setToolTip('Submit the jobs suspended: they start only when resumed in the Deadline Monitor')
        self.suspended.setChecked(bool(s.suspended))
        form.addRow("", self.suspended)
        layout.addLayout(form)

        # how it gets there: the web service when it has a URL, else deadlinecommand
        box = QGroupBox("Submit through")
        conn = QFormLayout(box)
        web = QLineEdit(s.web_url)
        web.setPlaceholderText("empty: deadlinecommand below")
        web.setToolTip("Deadline Web Service, e.g. http://192.168.100.20:8081 "
                       "(vfx-transcode.ini [deadline_web_service] url)")
        self.fields["web_url"] = web
        conn.addRow("Web service URL:", web)
        self.web_auth = QCheckBox("Requires authentication")
        self.web_auth.setToolTip('The Web Service asks for a user and password (vfx-transcode.ini require_authentication)')
        self.web_auth.setChecked(bool(s.web_auth))
        conn.addRow("", self.web_auth)
        user = QLineEdit(s.web_user)
        user.setToolTip('Web Service user name (vfx-transcode.ini [deadline_web_service] user)')
        self.fields["web_user"] = user
        conn.addRow("User:", user)
        self.web_password = QLineEdit(s.web_password)          # from the ini / environment
        self.web_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.web_password.setToolTip("Not saved: vfx-transcode.ini can take it from the environment, "
                                     "password = ${DEADLINE_WEB_PASSWORD}")
        conn.addRow("Password:", self.web_password)
        self.web_verify = QCheckBox("Verify the SSL certificate (https)")
        self.web_verify.setToolTip("Check the server's certificate on https; off for self-signed certificates (vfx-transcode.ini verify_ssl)")
        self.web_verify.setChecked(bool(s.web_verify_ssl))
        conn.addRow("", self.web_verify)
        dl = QLineEdit(s.command)
        dl.setPlaceholderText(core.deadline_command(""))
        dl.setToolTip("Used when there is no web service URL. Empty: found from DEADLINE_PATH or "
                      "the usual install folder (vfx-transcode.ini [deadline] command)")
        self.fields["command"] = dl
        conn.addRow("deadlinecommand:", dl)

        def show_route():
            on = bool(web.text().strip())
            for widget in (self.web_auth, user, self.web_password, self.web_verify):
                widget.setEnabled(on)
            user.setEnabled(on and self.web_auth.isChecked())
            self.web_password.setEnabled(on and self.web_auth.isChecked())
            dl.setEnabled(not on)
        web.textChanged.connect(lambda _t: show_route())
        self.web_auth.toggled.connect(lambda _on: show_route())
        show_route()
        layout.addWidget(box)

        self.only_selected = QRadioButton("The {} selected waiting job(s)".format(count_selected))
        self.only_selected.setToolTip('Send only the waiting jobs selected in the queue')
        every = QRadioButton("All {} waiting job(s)".format(count_waiting))
        every.setToolTip('Send every waiting job in the queue')
        (self.only_selected if count_selected else every).setChecked(True)
        self.only_selected.setEnabled(bool(count_selected))
        layout.addWidget(self.only_selected)
        layout.addWidget(every)
        note = QLabel("Each job becomes a Deadline job (CommandLine plugin) with the super-preset "
                      "written out in full, so the render nodes need only vfx-transcode and the "
                      "inputs and outputs on paths they can reach.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Submit")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # pools and groups from Deadline, without holding up the dialog
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._lists = self._pool.submit(core.deadline_lists, s)
        self._lists_timer = QTimer(self)
        self._lists_timer.timeout.connect(self._lists_arrived)
        self._lists_timer.start(150)

    def _lists_arrived(self):
        if not self._lists.done():
            return
        self._lists_timer.stop()
        self._pool.shutdown(wait=False)
        try:
            pools, groups = self._lists.result()
        except Exception as exc:                  # Deadline not reachable: plain text fields
            self.lists_note.setText("Could not get the pools and groups from Deadline ({}); "
                                    "type them.".format(exc))
            return
        for key, names in (("pool", pools), ("secondary_pool", [""] + pools), ("group", groups)):
            box = self.fields[key]
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(names)
            box.setEditText(current)              # the ini's / last used choice stays
            box.blockSignals(False)
        self.lists_note.setText("{} pools and {} groups from Deadline; pick one or type it."
                                .format(len(pools), len(groups)))

    def done(self, result):
        self._lists_timer.stop()
        self._pool.shutdown(wait=False)
        super().done(result)

    def result_settings(self):
        s = core.DeadlineSettings()
        for key, widget in self.fields.items():
            if isinstance(widget, QSpinBox):
                setattr(s, key, widget.value())
            elif isinstance(widget, QComboBox):
                setattr(s, key, widget.currentText().strip())
            else:
                setattr(s, key, widget.text().strip())
        s.suspended = self.suspended.isChecked()
        s.web_url = s.web_url.rstrip("/")
        s.web_auth, s.web_verify_ssl = self.web_auth.isChecked(), self.web_verify.isChecked()
        _set_json_setting("deadline", {k: v for k, v in asdict(s).items()
                                       if k not in core.DeadlineSettings.PRIVATE})
        s.web_password = self.web_password.text()
        return s


# --- a queue entry ----------------------------------------------------------------------------

class ElidedLabel(QLabel):
    """One line of text. When it doesn't fit, the start is cut ("...sh010/comp/v003") and it is
    aligned right, so the end of a path stays visible; the tooltip has it all."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(30)
        self.set_full_text(text)

    def set_full_text(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        metrics = self.fontMetrics()
        width = max(10, self.contentsRect().width())
        if metrics.horizontalAdvance(self._full) <= width:
            self.setText(self._full)
            self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        else:
            self.setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideLeft, width))
            self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def _split(path):
    """(folder, name) of a path, the folder with its trailing separator."""
    folder, name = os.path.split(os.path.normpath(path)) if path else ("", "")
    return (folder + os.sep if folder else ""), name


# --- thumbnails -------------------------------------------------------------------------------

THUMB_WIDTH = 256                     # made at this width, shown smaller
# Files tab columns
FILE_COLUMNS = ("Name", "Thumbnail", "Size", "PAR", "FPS", "Frames", "Output", "Folder", "Open", "#")
(COL_NAME, COL_THUMB, COL_SIZE, COL_PAR, COL_FPS, COL_FRAMES, COL_OUTPUT, COL_FOLDER,
 COL_OPEN, COL_ROW) = range(len(FILE_COLUMNS))
FILTER_GRACE = 600                    # ms after the last key in a filter field before it filters
THUMB_GRACE = 250                     # ms after a scroll before the rows on screen get pictures
THUMB_GRACE_NEW = 1500                # ... after the filter changed what is listed
THUMB_SIZES = (32, 384, 64)            # the Files tab's thumbnail width: min, max, default
PROBED_COLUMNS = ("Thumbnail", "Size", "PAR", "FPS")      # filled from the thumbnail run


def probe_info(report):
    """What the files list shows from a preview report: size, pixel aspect and frame rate of
    the input as vfx-transcode reads it."""
    words = report.get("keywords") or {}
    out = report.get("output") or {}
    width, height = words.get("input_width", ""), words.get("input_height", "")
    fps = words.get("source_fps_frac") or out.get("source_fps") or ""
    fps_text = words.get("source_fps_float", "")
    if fps_text.endswith(".000"):
        fps_text = fps_text[:-4]
    first, last = (report.get("input") or {}).get("first_frame"), (report.get("input") or {}).get("last_frame")
    frames = last - first + 1 if isinstance(first, int) and isinstance(last, int) else 0
    return {"size": "{}x{}".format(width, height) if width and height else "",
            "frames": frames,
            "par": str(words.get("input_par") or (report.get("input") or {}).get("par") or ""),
            "fps": fps_text or str(fps), "fps_frac": str(fps)}


ICON_COLOUR = "#b4b4b4"                # the line icons: grey, like the interface's text
_LINE_ICONS = {}


def _folder_path():
    path = QPainterPath()                     # the back with its tab, on a 16 x 16 grid
    path.moveTo(1.5, 12.5)
    path.lineTo(1.5, 4.0)
    path.quadTo(1.5, 3.0, 2.5, 3.0)
    path.lineTo(6.0, 3.0)
    path.lineTo(7.5, 4.5)
    path.lineTo(13.5, 4.5)
    path.quadTo(14.5, 4.5, 14.5, 5.5)
    path.lineTo(14.5, 12.5)
    path.quadTo(14.5, 13.5, 13.5, 13.5)
    path.lineTo(2.5, 13.5)
    path.quadTo(1.5, 13.5, 1.5, 12.5)
    path.closeSubpath()
    path.moveTo(2.0, 7.0)                     # the top of the front flap
    path.lineTo(14.0, 7.0)
    return path


def _play_path():
    path = QPainterPath()                     # a triangle pointing right, its corners rounded
    path.moveTo(4.5, 3.0)                     # by the pen's round joins
    path.lineTo(13.0, 8.0)
    path.lineTo(4.5, 13.0)
    path.closeSubpath()
    return path


def line_icon(kind):
    """A plain one-colour outline icon, "folder" (Explore) or "play" (Play): grey strokes, no
    fill (the style's own icons are coloured pictures that sit badly in the rows). Drawn once,
    at 1x and 2x."""
    if kind in _LINE_ICONS:
        return _LINE_ICONS[kind]
    icon = QIcon()
    for scale in (1, 2):
        size = 16 * scale
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(scale, scale)
        pen = QPen(QColor(ICON_COLOUR))
        pen.setWidthF(1.3)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(_folder_path() if kind == "folder" else _play_path())
        painter.end()
        icon.addPixmap(pixmap)
    _LINE_ICONS[kind] = icon
    return icon


def folder_icon():
    return line_icon("folder")


def row_icon(style, pixmap):
    """The icon of a row button: the outline Play / Explore, else the style's own."""
    if pixmap == QStyle.StandardPixmap.SP_DirOpenIcon:
        return line_icon("folder")
    if pixmap == QStyle.StandardPixmap.SP_MediaPlay:
        return line_icon("play")
    return style.standardIcon(pixmap)


def files_frames(items, thumbs, colour=()):
    """(frames of the inputs, how many movies have no frame count yet): sequences by their
    files on disk, movies by what their thumbnail run read."""
    frames, unknown = 0, 0
    for media in items:
        if media.kind != "movie":
            frames += media.count or 1
            continue
        info = thumbs.info(media.path, colour) or {}
        if info.get("frames"):
            frames += int(info["frames"])
        else:
            unknown += 1
    return frames, unknown


THUMB_SHOW = QSize(96, 54)


class Thumbnails(QObject):
    """Small pictures of the inputs, made lazily by vfx-transcode -preview (no burn-ins, no
    slate, 256 pixels wide) with the colour options of the super-preset (-ci, -co, -display,
    -view ...; none = the presets file's default), and kept on disk, keyed by the input's path,
    modification time and those options. Only what is asked for is made, the newest request
    first, two at a time; ready(path) when one is done."""

    ready = Signal(str)

    def __init__(self, parent=None, parallel=2):
        super().__init__(parent)
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        self.folder = Path(folder or tempfile.gettempdir()) / "thumbnails"
        self.parallel = parallel
        self.presets_file = ""
        self.pixmaps = {}             # (source path, colour) -> QPixmap
        self.waiting = []             # (source, colour options), the newest last
        self.running = {}             # (source, colour) -> QProcess
        self.failed = set()
        self.stamps = {}              # source -> modification time, read once (a share is slow)
        self.infos = {}               # (source, colour) -> probe_info read from disk

    def forget_stamps(self):
        """Read the files' times again (a new scan: they may have changed)."""
        self.stamps = {}
        self.infos = {}

    @staticmethod
    def key(source, colour=()):
        return source, " ".join(colour or ())

    def file_for(self, source, colour=()):
        import hashlib
        stamp = self.stamps.get(source)
        if stamp is None:
            try:
                stamp = os.stat(source).st_mtime_ns
            except OSError:
                stamp = 0
            self.stamps[source] = stamp
        key = hashlib.sha1("{}|{}|{}|{}".format(os.path.normcase(source), stamp, THUMB_WIDTH,
                                                "\x1f".join(colour or ())).encode("utf-8")).hexdigest()[:20]
        return self.folder / (key + ".png")

    def get(self, source, colour=()):
        """The thumbnail with these colour options if it is made (memory or disk), else None."""
        key = self.key(source, colour)
        if key in self.pixmaps:
            return self.pixmaps[key]
        path = self.file_for(source, colour)
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                pixmap = pixmap.scaled(THUMB_SHOW, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                self.pixmaps[key] = pixmap
                return pixmap
        return None

    def original(self, source, colour=()):
        """The thumbnail as made (256 pixels wide), for showing at any size; None if not made."""
        if self.get(source, colour) is None:
            return None
        key = ("full",) + self.key(source, colour)
        if key not in self.pixmaps:
            self.pixmaps[key] = QPixmap(str(self.file_for(source, colour)))
        return self.pixmaps[key]

    def info(self, source, colour=()):
        """{"size", "par", "fps"} read when its thumbnail was made, or None."""
        key = self.key(source, colour)
        if key in self.infos:
            return self.infos[key]
        path = self.file_for(source, colour).with_suffix(".info.json")
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        self.infos[key] = info
        return info

    def request(self, source, colour=()):
        """Make it, if it isn't made, asked for, being made or failed before."""
        colour = tuple(colour or ())
        key = self.key(source, colour)
        if not source or key in self.running or key in self.failed:
            return
        info = self.info(source, colour) if self.get(source, colour) is not None else None
        if info is not None and ("frames" in info or core.kind_of(source) != "movie"):
            return                                # made, with its size / pixel aspect / fps
            # (a movie's frame count too: older thumbnails of movies are made again)
        self.waiting = [w for w in self.waiting if self.key(*w) != key]
        self.waiting.append((source, colour))     # the newest first: what is on screen now
        self._next()

    def _next(self):
        while self.waiting and len(self.running) < self.parallel:
            source, colour = self.waiting.pop()
            key = self.key(source, colour)
            out = self.file_for(source, colour)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.failed.add(key)
                continue
            cmd = core.vfx_transcode_command() + ["-i", source]
            if self.presets_file:
                cmd += ["-presets_file", self.presets_file]
            cmd += list(colour) + ["-preview", str(out), "-width", str(THUMB_WIDTH),
                                   "-burn_preset", "none", "-slate", "none", "-oiio_threads", "2"]
            process = QProcess(self)
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            process.finished.connect(lambda _code, _status, s=source, c=colour: self._finished(s, c))
            process.errorOccurred.connect(lambda _err, s=source, c=colour: self._finished(s, c))
            self.running[key] = process
            process.start(cmd[0], cmd[1:])

    def _finished(self, source, colour):
        key = self.key(source, colour)
        process = self.running.pop(key, None)
        if process is None:
            return
        process.deleteLater()
        if self.get(source, colour) is None:
            self.failed.add(key)                  # not again this session
        else:
            # what vfx-transcode read of the input (its middle frame): kept small for the list
            out = self.file_for(source, colour)
            try:
                report = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
                info = probe_info(report)
                self.infos[key] = info
                out.with_suffix(".info.json").write_text(json.dumps(info), encoding="utf-8")
            except (OSError, ValueError):
                pass
            self.ready.emit(source)
            # the preview's side files are not needed
            for extra in (out.with_suffix(".json"), out.with_name(out.stem + "_slate.png")):
                try:
                    extra.unlink()
                except OSError:
                    pass
        self._next()

    def forget_waiting(self):
        """Drop what was asked for but not started (the list changed)."""
        self.waiting = []

    def stop(self):
        self.waiting = []
        for process in list(self.running.values()):
            process.kill()


def visible_items(view, items):
    """The tree / list items (in order) whose rows are on screen."""
    area = view.viewport().rect()
    out = []
    for item in items:
        if item is None or item.isHidden():
            continue
        rect = view.visualItemRect(item)
        if rect.isValid() and rect.intersects(area):
            out.append(item)
    return out


class FilesView(QTreeWidget):
    """The Files tab's list: several rows can be selected (Ctrl / Shift), and a click on the
    only selected row selects nothing again."""

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        plain = event.modifiers() == Qt.KeyboardModifier.NoModifier
        if (plain and event.button() == Qt.MouseButton.LeftButton and item is not None
                and item.isSelected() and len(self.selectedItems()) == 1):
            self.clearSelection()
            self.setCurrentItem(None)
            event.accept()
            return
        super().mousePressEvent(event)


class JobWidget(QFrame):
    """A job in the queue:
        preset                                    added date and time
        source folder   | source file name        [play] [explore]
        output folder   | output file name        [play] [explore]   (when it is done)
        status          [ progress / time it took ...................... ]"""

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.setObjectName("job")
        self.setStyleSheet("QFrame#job { border-bottom: 1px solid #4a4a4e; background: transparent; }"
                           "QLabel { background: transparent; }")
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(3)
        self.preset = QLabel()
        font = QFont(self.preset.font())
        font.setBold(True)
        self.preset.setFont(font)
        self.added = QLabel()
        self.added.setStyleSheet("color: #9a9a9a;")
        self.added.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.preset, 0, 0, 1, 2)
        grid.addWidget(self.added, 0, 2, 1, 3)

        style = self.style()
        self.src_path, self.src_name = ElidedLabel(), ElidedLabel()
        self.out_path, self.out_name = ElidedLabel(), ElidedLabel()
        for label in (self.src_path, self.out_path):
            label.setStyleSheet("color: #a8a8a8;")
        self.src_play, self.src_explore = self._button(style, QStyle.StandardPixmap.SP_MediaPlay,
                                                       "Open the source"), \
            self._button(style, QStyle.StandardPixmap.SP_DirOpenIcon, "Show the source in the file browser")
        self.out_play, self.out_explore = self._button(style, QStyle.StandardPixmap.SP_MediaPlay,
                                                       "Open the output"), \
            self._button(style, QStyle.StandardPixmap.SP_DirOpenIcon, "Show the output in the file browser")
        self.src_play.clicked.connect(lambda: _open(self.job.input))
        self.src_explore.clicked.connect(lambda: core.reveal(self.job.input))
        self.out_play.clicked.connect(lambda: _open(core.result_path(self.job, self.job.result)))
        self.out_explore.clicked.connect(lambda: core.reveal(_existing(core.result_path(self.job, self.job.result))))
        for row, widgets in ((1, (QLabel("In"), self.src_path, self.src_name, self.src_play, self.src_explore)),
                             (2, (QLabel("Out"), self.out_path, self.out_name, self.out_play, self.out_explore))):
            widgets[0].setStyleSheet("color: #7a7a7a;")
            for col, widget in enumerate(widgets):
                grid.addWidget(widget, row, col)
        grid.setColumnStretch(1, 2)            # the path
        grid.setColumnStretch(2, 3)            # the file name gets more room
        self.thumb = QLabel()
        self.thumb.setFixedSize(THUMB_SHOW)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet("background: #1e1e20; border: 1px solid #3a3a3e;")
        self.thumb.setToolTip("The source (made in the background by vfx-transcode)")
        grid.addWidget(self.thumb, 0, 5, 4, 1)

        self.status = QLabel()
        self.status.setMinimumWidth(90)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setMaximumHeight(16)
        self.log_button = QPushButton("Log")
        self.log_button.setToolTip("Show what vfx-transcode printed for this job")
        self.log_button.setMaximumHeight(20)
        self.log_button.clicked.connect(lambda: LogDialog.show_for(self.job, self))
        row = QHBoxLayout()
        row.addWidget(self.status)
        row.addWidget(self.bar, 1)
        row.addWidget(self.log_button)
        grid.addLayout(row, 3, 0, 1, 5)
        self.refresh()

    def _button(self, style, pixmap, tip):
        button = QToolButton()
        button.setIcon(row_icon(style, pixmap))
        button.setToolTip(tip)
        button.setAutoRaise(True)
        return button

    def refresh(self, busy=False, stage=""):
        job = self.job
        self.preset.setText(job.super_preset)
        self.added.setText(time.strftime("%Y-%m-%d  %H:%M", time.localtime(job.added)))
        folder, _name = _split(job.input)
        self.src_path.set_full_text(folder)
        self.src_name.set_full_text(job.pattern)
        finished = job.status in core.FINISHED_OK
        out = core.result_path(job, job.result) if finished else job.predicted
        if finished and job.output_type != "movie" and job.predicted:
            out = os.path.join(os.path.dirname(out) if os.path.isfile(out) else out,
                               os.path.basename(job.predicted))
        folder, name = _split(out or job.output)
        self.out_path.set_full_text(folder)
        self.out_name.set_full_text(name)
        done = job.status in core.FINISHED_OK
        self.out_play.setEnabled(done)
        self.out_explore.setEnabled(done)
        self.log_button.setVisible(job.status in (core.DONE, core.FAILED, core.CANCELLED) and bool(job.log))

        title = core.STATUS_TITLES[job.status]
        self.status.setText(title)
        self.status.setStyleSheet("color: {};".format(STATUS_COLOURS[job.status]))
        self.status.setToolTip(job.message)
        took = core.format_seconds(job.elapsed()) if job.started else ""
        bar = self.bar
        if job.status == core.RUNNING:
            bar.setEnabled(True)
            if busy:
                bar.setRange(0, 0)
            else:
                bar.setRange(0, 1000)
                bar.setValue(int(1000 * job.progress))
            bar.setFormat("{}%   {}   {}".format(int(100 * job.progress), stage or job.message, took))
        else:
            bar.setRange(0, 1000)
            bar.setValue(1000 if done else 0)
            bar.setEnabled(False)
            bar.setFormat({core.DONE: "done in " + took,
                           core.FAILED: "failed{}: {}".format(" after " + took if took else "", job.message),
                           core.CANCELLED: "cancelled" + (" after " + took if took else ""),
                           core.SUBMITTED: "on farm: Deadline job {}{}".format(
                               job.deadline_id, " - " + job.message if job.message.startswith(("on Deadline", "Deadline job")) else ""),
                           core.FARM_DONE: "done on farm: Deadline job {}".format(job.deadline_id),
                           core.WAITING: job.message or "waiting"}.get(job.status, ""))
        bar.setToolTip(bar.format())


class LogDialog(QDialog):
    """What vfx-transcode printed for one job, in a window of its own (one per job; asking
    again brings it to the front)."""
    open_dialogs = {}

    @classmethod
    def show_for(cls, job, parent=None):
        dialog = cls.open_dialogs.get(job.id)
        if dialog is None:
            dialog = cls.open_dialogs[job.id] = cls(job, parent.window() if parent else None)
        dialog.fill()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.destroyed.connect(lambda *_a, i=job.id: LogDialog.open_dialogs.pop(i, None))
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.title)
        self.text = QPlainTextEdit()
        self.text.setToolTip('What vfx-transcode printed for this job')
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Consolas", 9))
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.wrap = QCheckBox("Word wrap")
        self.wrap.setToolTip('Wrap long lines to the window width')
        self.wrap.setChecked(True)
        self.wrap.toggled.connect(self._set_wrap)
        self._set_wrap(True)
        row.addWidget(self.wrap)
        copy = QPushButton("Copy")
        copy.setToolTip('Copy the whole log to the clipboard')
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        close = QPushButton("Close")
        close.setToolTip('Close this window')
        close.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(copy)
        row.addWidget(close)
        layout.addLayout(row)

    def _set_wrap(self, on):
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if on
                                  else QPlainTextEdit.LineWrapMode.NoWrap)

    def fill(self):
        job = self.job
        self.setWindowTitle("Log: {} [{}]".format(job.pattern, job.super_preset))
        colour = STATUS_COLOURS.get(job.status, "#dcdcdc")
        self.title.setText("<b>{}</b> &rarr; {}<br><span style='color:{}'>{}</span>{}".format(
            job.pattern, (job.result or job.predicted or job.output).replace("<", "&lt;"), colour,
            core.STATUS_TITLES[job.status], ": " + job.message.replace("<", "&lt;") if job.message else ""))
        self.text.setPlainText("\n".join(job.log))
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


def _existing(path):
    return path if os.path.exists(path) else os.path.dirname(path)


def _open(path):
    """Open with the system's program for it (a movie player, an image viewer); a folder when
    the file isn't there."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(_existing(path)))


# --- the window -------------------------------------------------------------------------------

class ProcessWindow(QMainWindow):
    def __init__(self, inputs=(), presets_file=""):
        super().__init__()
        self.setWindowTitle("vfx-transcode process")
        self.resize(1150, 780)
        self.setAcceptDrops(True)
        self.presets_file = presets_file
        self.presets = None
        self.super_presets, self.super_origin = {}, {}
        self.items = []                       # the source's inputs (core.MediaItem)
        self.files = []                       # ... picked as files
        self.folders = []                     # ... the folders picked
        self.listings = {}                    # folder -> its listing (core.walk_media), kept
        self.scanner = None
        self.dest_source = ""                 # the super-preset whose destination the fields show
        self.dest_edited = False              # ... changed by the user since
        self.to_scan = []                     # folders waiting for the scanner
        # the last session's queue, without the finished jobs (done, or sent to Deadline);
        # failed and cancelled ones stay, to be looked at or retried
        self.jobs = [j for j in core.load_queue(queue_file()) if j.status not in CLEARED_ON_START]
        self.running = {}                     # job id -> (QProcess, core.Progress, line buffer)
        self.cancelling = set()
        self.paused = True
        self.queue_dirty = False
        self.rows = {}                        # job id -> JobWidget
        self.thumbs = Thumbnails(self)        # pictures of the inputs, made on demand
        self.thumbs.presets_file = presets_file
        self.thumbs.ready.connect(self._thumbnail_ready)
        self.thumb_timer = QTimer(self)       # ask for the rows on screen, once things settle
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.setInterval(THUMB_GRACE)
        self.rows_timer = QTimer(self)        # the Play / Explore buttons of the rows on screen
        self.rows_timer.setSingleShot(True)
        self.rows_timer.setInterval(30)
        self.rows_timer.timeout.connect(self._fill_open_buttons)
        self.thumb_timer.timeout.connect(self._request_thumbnails)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        process_page, queue_page = QWidget(), QWidget()
        self.tabs.addTab(process_page, "Process")
        self.tabs.addTab(queue_page, "Queue")
        self.tabs.currentChanged.connect(lambda _i: self.thumb_timer.start())
        self._build_process(process_page)
        self._build_queue(queue_page)
        self._load_presets()
        self._restore_settings()
        self._fill_queue()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(POLL_MS)
        tooltips.fill_missing(self)           # any control still without a hint gets one
        if inputs:
            QTimer.singleShot(0, lambda: self.set_source(inputs))

    # presets

    def _presets_path(self):
        if self.presets_file:
            return resolve_path(self.presets_file, app_dir())
        return self.presets_choice.path

    def _load_presets(self):
        self.presets_choice = None if self.presets_file else studio.choose_presets()
        try:
            if self.presets_choice is not None and self.presets_choice.presets is not None:
                self.presets = self.presets_choice.presets
            else:
                self.presets = load_presets(self._presets_path())
        except Exception as exc:
            self.presets = None
            QMessageBox.warning(self, "Presets", "The presets file can't be read:\n{}".format(exc))
        try:
            self.super_presets, self.super_origin = sp.load_merged(sp.search_paths(self._presets_path()))
        except TranscodeError as exc:
            self.super_presets, self.super_origin = {}, {}
            QMessageBox.warning(self, "Super-presets", str(exc))
        self._fill_menus()
        self.presets_label.setText("presets: {}".format(self._presets_path()))
        self.presets_label.setToolTip(self.presets_choice.source if self.presets_choice else "-presets_file")
        self._fill_super_presets()

    # Process tab: Source, Preset, Destination, Process / Add to Queue

    def _build_process(self, page):
        layout = QVBoxLayout(page)

        box = QGroupBox("Source")
        row = QHBoxLayout(box)
        self.source = QLineEdit()
        self.source.setToolTip('A file (any frame of a sequence, or a movie) or a folder to search; drop files or folders here')
        self.source.setPlaceholderText("a file (any frame of a sequence) or a folder - or drop it here")
        self.source.editingFinished.connect(self._source_typed)
        row.addWidget(self.source, 1)
        add_file = QPushButton("Add File")
        add_file.setToolTip('Pick one or more files: any frame of a sequence, stills or movies')
        add_file.clicked.connect(self.pick_files)
        add_folder = QPushButton("Add Folder")
        add_folder.setToolTip('Pick a folder: it is searched (with its subfolders) for media matching the filter')
        add_folder.clicked.connect(self.pick_folder)
        for widget in (add_file, add_folder):
            row.addWidget(widget)
        layout.addWidget(box)

        # Presets and Files, as two tabs
        self.middle_tabs = QTabWidget()
        presets_page = QWidget()
        row = QHBoxLayout(presets_page)
        row.setContentsMargins(4, 4, 4, 4)
        self.preset_list = QListWidget()
        self.preset_list.setToolTip('The super-presets made in the tuner; the panel beside it says what the selected one makes and whether it fits the source')
        # picked by the user (click or keys); refills block the signal and keep the fields
        self.preset_list.currentRowChanged.connect(self._preset_picked)
        row.addWidget(self.preset_list, 1)
        self.preset_info = QLabel()
        self.preset_info.setWordWrap(True)
        self.preset_info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.preset_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preset_info.setMargin(8)
        self.preset_info.setStyleSheet("background: #2a2a2d; border: 1px solid #4a4a4e;")
        row.addWidget(self.preset_info, 1)
        self.middle_tabs.addTab(presets_page, "Presets")
        self.middle_tabs.addTab(self._build_files(), "Files")
        self.middle_tabs.currentChanged.connect(lambda _i: self.thumb_timer.start())
        layout.addWidget(self.middle_tabs, 1)

        self._build_interpretation(layout)

        box = QGroupBox("Destination")
        grid = QGridLayout(box)
        self.path_preset = QComboBox()          # filled from the presets file (_fill_menus)
        self.path_preset.setToolTip('Where the output goes: a path preset, One folder for all..., or Custom (type it in Path)')
        self.path_preset.activated.connect(self._path_preset_picked)
        self.name_preset = QComboBox()
        self.name_preset.setToolTip('The output file name: a name preset, or Custom (type it in Name)')
        self.name_preset.activated.connect(self._name_preset_picked)
        self.out_path = QLineEdit("./")
        self.out_path.setToolTip("./ is the source's folder, ../ the folder above it. Keywords work, "
                                 "e.g. ./../review/{date}/")
        browse = QPushButton("...")
        browse.setToolTip('Pick the output folder')
        browse.setFixedWidth(30)
        browse.clicked.connect(self._pick_out_folder)
        self.out_name = QLineEdit()
        self.out_name.setPlaceholderText("same as the source")
        self.out_name.setToolTip("Keywords: {clean_name} {namesplit1} {version_string} {date} ... "
                                 "The extension comes from the preset.")
        grid.addWidget(self.path_preset, 0, 0, 1, 3)
        grid.addWidget(self.name_preset, 0, 3, 1, 2)
        self.dest_reset = QPushButton("Use preset's destination")
        self.dest_reset.setToolTip("Put back the path and name the selected super-preset saves")
        self.dest_reset.clicked.connect(lambda: self._apply_preset_destination(force=True))
        grid.addWidget(self.dest_reset, 0, 5)
        grid.addWidget(QLabel("Path:"), 1, 0)
        grid.addWidget(self.out_path, 1, 1)
        grid.addWidget(browse, 1, 2)
        grid.addWidget(QLabel("Name:"), 1, 3)
        grid.addWidget(self.out_name, 1, 4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(4, 1)
        # shown when the Path / Name use {custom_destination_path} / {custom_destination_name}
        self.custom_path = QLineEdit()
        self.custom_path.setPlaceholderText("{custom_destination_path}: type it or pick a folder")
        self.custom_path.setToolTip("Fills {custom_destination_path} in the Path or Name (the preset's "
                                    "destination uses it).")
        self.custom_browse = QPushButton("...")
        self.custom_browse.setToolTip('Pick the folder for {custom_destination_path}')
        self.custom_browse.setFixedWidth(30)
        self.custom_browse.clicked.connect(self._pick_custom_folder)
        self.custom_name = QLineEdit()
        self.custom_name.setPlaceholderText("{custom_destination_name}")
        self.custom_name.setToolTip("Fills {custom_destination_name} in the Path or Name (the preset's "
                                    "destination uses it).")
        self.custom_path_label, self.custom_name_label = QLabel("Custom path:"), QLabel("Custom name:")
        grid.addWidget(self.custom_path_label, 2, 0)
        grid.addWidget(self.custom_path, 2, 1)
        grid.addWidget(self.custom_browse, 2, 2)
        grid.addWidget(self.custom_name_label, 2, 3)
        grid.addWidget(self.custom_name, 2, 4)
        for edit in (self.custom_path, self.custom_name):
            edit.textChanged.connect(self._output_changed)
        self.out_preview = QLabel()
        self.out_preview.setStyleSheet("color: #9a9a9a;")
        self.out_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.out_preview, 3, 0, 1, 5)
        self.out_path.textEdited.connect(lambda _t: self._mark_custom(self.path_preset))
        self.out_name.textEdited.connect(lambda _t: self._mark_custom(self.name_preset))
        for edit in (self.out_path, self.out_name):
            edit.textEdited.connect(lambda _t: self._dest_edited())
        self.out_path.textChanged.connect(self._output_changed)
        self.out_name.textChanged.connect(self._output_changed)
        layout.addWidget(box)

        row = QHBoxLayout()
        self.go_button = QPushButton("Process")
        self.go_button.setToolTip('Add the jobs to the queue and start it now')
        self.go_button.setMinimumHeight(34)
        self.go_button.setStyleSheet(GO_STYLE)
        self.go_button.setDefault(True)
        self.go_button.clicked.connect(lambda: self.submit(start=True))
        self.queue_button = QPushButton("Add to Queue")
        self.queue_button.setToolTip('Add the jobs to the queue without starting it (Queue tab: Start, or Send to Deadline)')
        self.queue_button.setMinimumHeight(34)
        self.queue_button.setStyleSheet(GO_STYLE)
        self.queue_button.setMinimumWidth(160)
        self.queue_button.clicked.connect(lambda: self.submit(start=False))
        row.addWidget(self.go_button, 1)
        row.addSpacing(20)
        row.addWidget(self.queue_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.play_after = QCheckBox("Play after conversion")
        self.play_after.setToolTip('Open each output in its default player when its job is done')
        self.explore_after = QCheckBox("Explore after conversion")
        self.explore_after.setToolTip('Show each output in the file browser when its job is done')
        row.addWidget(self.play_after)
        row.addSpacing(20)
        row.addWidget(self.explore_after)
        row.addStretch(1)
        self.presets_label = QLabel()
        self.presets_label.setStyleSheet("color: #7a7a7a;")
        row.addWidget(self.presets_label)
        layout.addLayout(row)

    # tweaks: how the source is read (collapsed by default)

    def _build_interpretation(self, layout):
        self.interp_toggle = QToolButton()
        self.interp_toggle.setCheckable(True)
        self.interp_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.interp_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.interp_toggle.setAutoRaise(True)
        self.interp_toggle.setToolTip("Override how the source is read: frame rate, pixel aspect, "
                                      "timecode, reel name, colour, alpha. Empty / auto: as detected, "
                                      "or as the preset says.")
        self.interp_toggle.toggled.connect(self._toggle_interpretation)
        layout.addWidget(self.interp_toggle)
        self.interp_box = QGroupBox()
        grid = QGridLayout(self.interp_box)
        auto = "auto"

        def edit_combo(items, tip):
            box = QComboBox()
            box.setEditable(True)
            box.addItem(auto)
            box.addItems(list(items))
            box.setToolTip(tip)
            box.lineEdit().setPlaceholderText(auto)
            box.currentTextChanged.connect(lambda _t: self._interpretation_changed())
            return box

        def line(placeholder, tip):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setToolTip(tip)
            edit.textChanged.connect(lambda _t: self._interpretation_changed())
            return edit

        def choice(items, tip):
            box = QComboBox()
            for data, text in items:
                box.addItem(text, data)
            box.setToolTip(tip)
            box.currentIndexChanged.connect(lambda _i: self._interpretation_changed())
            return box

        self.in_fps = edit_combo(core.FRAME_RATES, "Frame rate (-r): auto = the EXR header or the movie's, else 24")
        self.in_par = edit_combo(core.PIXEL_ASPECTS, "Pixel aspect (-par): auto = the EXR header's or the movie's, else 1")
        self.in_first = line("auto (1001)", "Movies: the number of the first frame (-movie_first_frame), "
                                            "for burn-ins and the frame counter")
        self.in_timecode = line("auto (from the file, or the preset)", "Start timecode HH:MM:SS:FF (-timecode)")
        self.in_reel = line("auto (from the file, or the preset)", "Reel name (-reelid); keywords work, e.g. {namesplit1}")
        self.in_ci = edit_combo([], "Input colourspace (-ci), e.g. ACEScg. auto: the preset's colour, else ACEScg "
                                    "for EXR. The list comes from the OCIO config.")
        self.in_ci.setMinimumContentsLength(18)
        as_preset = ("", "as the preset")
        self.in_alpha = choice([as_preset] + list(core.ALPHA_MODES.items()), "Alpha (-alpha)")
        self.in_write_tc = choice([as_preset, ("1", "yes"), ("0", "no")], "Write the timecode into the output (-output_timecode)")
        self.in_write_reel = choice([as_preset, ("1", "yes"), ("0", "no")], "Write the reel name into the output (-output_reelid)")
        rows = ((("Frame rate", self.in_fps), ("Pixel aspect", self.in_par), ("Movie first frame", self.in_first)),
                (("Timecode", self.in_timecode), ("Reel name", self.in_reel), ("Colour in", self.in_ci)),
                (("Alpha", self.in_alpha), ("Write timecode", self.in_write_tc), ("Write reel name", self.in_write_reel)))
        for r, row in enumerate(rows):
            for c, (label, widget) in enumerate(row):
                grid.addWidget(QLabel(label), r, 2 * c)
                grid.addWidget(widget, r, 2 * c + 1)
                grid.setColumnStretch(2 * c + 1, 1)
        reset = QPushButton("Reset all to auto")
        reset.setToolTip('Clear every tweak: the source is read as it is, and the super-preset decides the rest')
        reset.clicked.connect(lambda: self._set_interpretation(core.Interpretation()))
        self.interp_note = QLabel("")
        self.interp_note.setStyleSheet("color: #e8d27f;")
        self.interp_note.setWordWrap(True)
        grid.addWidget(self.interp_note, 3, 0, 1, 5)
        grid.addWidget(reset, 3, 5)
        layout.addWidget(self.interp_box)
        self.interp_box.setVisible(False)
        self.ocio_listed = False
        self._interpretation_changed()

    def _toggle_interpretation(self, on):
        self.interp_box.setVisible(on)
        self.interp_toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        if on and not self.ocio_listed:            # oiiotool lists the colourspaces: only once needed
            self.ocio_listed = True
            QTimer.singleShot(0, self._list_colourspaces)

    def _list_colourspaces(self):
        names = list(core.COMMON_COLOURSPACES)
        try:
            info = core.tuner.scan_ocio(core.tuner.DEFAULT_OCIO, self.presets)
            if info.colorspaces:
                names = list(info.colorspaces)
        except Exception:                          # no oiiotool / config: the common names
            pass
        current = self.in_ci.currentText()
        self.in_ci.blockSignals(True)
        self.in_ci.clear()
        self.in_ci.addItem("auto")
        self.in_ci.addItems(names)
        self.in_ci.setCurrentText(current)
        self.in_ci.blockSignals(False)

    def interpretation(self):
        text = lambda box: "" if box.currentText().strip().lower() == "auto" else box.currentText().strip()
        return core.Interpretation(
            fps=text(self.in_fps), par=text(self.in_par), first_frame=self.in_first.text().strip(),
            timecode=self.in_timecode.text().strip(), reel=self.in_reel.text().strip(),
            colorin=text(self.in_ci), alpha=self.in_alpha.currentData() or "",
            write_timecode=self.in_write_tc.currentData() or "",
            write_reel=self.in_write_reel.currentData() or "")

    def _set_interpretation(self, i):
        for box, value in ((self.in_fps, i.fps), (self.in_par, i.par), (self.in_ci, i.colorin)):
            box.setCurrentText(value or "auto")
        self.in_first.setText(i.first_frame)
        self.in_timecode.setText(i.timecode)
        self.in_reel.setText(i.reel)
        for box, value in ((self.in_alpha, i.alpha), (self.in_write_tc, i.write_timecode),
                           (self.in_write_reel, i.write_reel)):
            box.setCurrentIndex(max(0, box.findData(value)))
        self._interpretation_changed()

    def _interpretation_changed(self):
        if not hasattr(self, "in_write_reel"):
            return                                # still building
        i = self.interpretation()
        n = i.changed()
        self.interp_toggle.setText("Tweaks" + ("   ({} changed)".format(n) if n else "   (auto)"))
        problems = i.problems()
        self.interp_note.setText("\n".join(problems))
        self.interp_note.setVisible(bool(problems))

    # destination

    def _fill_menus(self):
        """The Path and Name menus: the presets file's destination paths and names (the
        built-in ones if it has none), then "One folder for all..." and "Custom"."""
        paths, names = core.tuner.path_menu(self.presets), core.tuner.name_menu(self.presets)
        for combo, menu, value, folder in ((self.path_preset, paths, self.out_path.text().strip() or "./", True),
                                           (self.name_preset, names, self.out_name.text().strip(), False)):
            combo.blockSignals(True)
            combo.clear()
            for label, template in menu.items():
                combo.addItem(label, template)
                if template is not None:
                    combo.setItemData(combo.count() - 1, template or "(empty)", Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(core.tuner.preset_index(menu, value, folder))
            combo.blockSignals(False)

    def _dest_edited(self):
        self.dest_edited = True
        self._show_dest_state()

    def _show_dest_state(self):
        entry = self.super_presets.get(self._preset_name())
        has = core.destination_of(entry) is not None
        self.dest_reset.setEnabled(has and (self.dest_edited or self.dest_source != self._preset_name()))

    def _apply_preset_destination(self, force=False):
        """Fill Path and Name with the selected super-preset's destination. Without force a
        destination the user changed for this same preset is kept (a reload or a refill of the
        list); another preset, or force, always fills them."""
        name = self._preset_name()
        dest = core.destination_of(self.super_presets.get(name))
        if dest is not None and (force or name != self.dest_source or not self.dest_edited):
            self.out_path.setText(dest.path or "./")
            self.out_name.setText(dest.name)
            self._show_preset(self.path_preset, dest.path or "./", True)
            self._show_preset(self.name_preset, dest.name, False)
            self.dest_source, self.dest_edited = name, False
        elif dest is None and name != self.dest_source:
            self.dest_source, self.dest_edited = name, True    # nothing saved: the fields stay
        self._show_dest_state()
        self._output_changed()

    def _path_preset_picked(self, index):
        self._dest_edited()
        if self.path_preset.itemText(index) == core.tuner.ONE_FOLDER:
            self._pick_out_folder()               # one folder for all: ask which
            return
        value = self.path_preset.itemData(index)
        if value is not None:
            self.out_path.setText(value)

    def _name_preset_picked(self, index):
        self._dest_edited()
        value = self.name_preset.itemData(index)
        if value is not None:
            self.out_name.setText(value)

    def _show_preset(self, combo, value, folder):
        """The menu shows the preset with this value, else Custom."""
        menu = {combo.itemText(i): combo.itemData(i) for i in range(combo.count())}
        combo.setCurrentIndex(core.tuner.preset_index(menu, value, folder))

    def _mark_custom(self, combo):
        combo.setCurrentIndex(combo.count() - 1)

    def _pick_out_folder(self):
        start = self.out_path.text() if os.path.isdir(self.out_path.text()) else settings().value("last_folder", "")
        path = QFileDialog.getExistingDirectory(self, "Output folder", start)
        if path:
            self.out_path.setText(path)
            self.path_preset.setCurrentIndex(self.path_preset.findText(core.tuner.ONE_FOLDER))
            self._dest_edited()

    def _destination(self):
        """core.DestinationState: the Path and Name as typed here, the thumbnail and filmstrip
        ones from the super-preset."""
        entry = self.super_presets.get(self._preset_name())
        dest = core.destination_of(entry) or core.tuner.DestinationState()
        dest.path, dest.name = self.out_path.text().strip() or "./", self.out_name.text().strip()
        return dest

    def output_spec(self):
        return core.destination_spec(self.out_path.text(), self.out_name.text())

    def _dest_texts(self):
        """The output, thumbnail and filmstrip templates the jobs would use."""
        if not hasattr(self, "preset_list"):
            return [self.out_path.text(), self.out_name.text()]
        d = self._destination()
        return [d.output(), d.thumbnail(), d.filmstrip()]

    def _custom(self):
        """(custom path, custom name) for the ones the Destination uses; "" for the others."""
        uses_path, uses_name = core.tuner.uses_custom(*self._dest_texts())
        return (self.custom_path.text().strip() if uses_path else "",
                self.custom_name.text().strip() if uses_name else "")

    def _show_custom_fields(self):
        uses_path, uses_name = core.tuner.uses_custom(*self._dest_texts())
        for widget in (self.custom_path_label, self.custom_path, self.custom_browse):
            widget.setVisible(uses_path)
        for widget in (self.custom_name_label, self.custom_name):
            widget.setVisible(uses_name)

    def _pick_custom_folder(self):
        start = self.custom_path.text() if os.path.isdir(self.custom_path.text()) else \
            settings().value("last_folder", "")
        path = QFileDialog.getExistingDirectory(self, "Custom destination path", start)
        if path:
            self.custom_path.setText(path)

    def _dest_origin(self):
        name = self._preset_name()
        if core.destination_of(self.super_presets.get(name)) is None:
            return ""
        if self.dest_source == name and not self.dest_edited:
            return "   (the super-preset's destination)"
        return "   (changed from the super-preset's destination)"

    def _output_changed(self, *_args):
        if not hasattr(self, "preset_list"):
            return                            # still building the window
        self._show_custom_fields()
        self._fill_file_outputs()
        spec = self.output_spec()
        entry = self.super_presets.get(self._preset_name())
        if not self.items or entry is None:
            self.out_preview.setText("-o " + core.output_arg(spec, "movie"))
            return
        output_type, ext, _issues = core.output_of(entry, self.presets)
        first = core.predict_output(self.items[0], spec, output_type, ext, self._custom())
        try:                                      # as the job will have it: all keywords known now
            jobs, _p = core.make_jobs(self.items[:1], self._preset_name(), entry, spec, self.presets,
                                      presets_file=self.presets_file, custom=self._custom(),
                                      dest=self._destination(), interpret=self.interpretation())
            first = jobs[0].predicted if jobs else first
        except Exception:
            pass
        missing = core.missing_custom(self._destination(), self._custom())
        if missing:
            self.out_preview.setText("<span style='color:#e8d27f'>Type the {}.</span>".format(
                " and the ".join(missing)))
            return
        more = "   (+ {} more)".format(len(self.items) - 1) if len(self.items) > 1 else ""
        self.out_preview.setText("Output: {}{}{}".format(first, more, self._dest_origin()))

    # source

    def pick_files(self):
        start = settings().value("last_folder", "")
        paths, _ = QFileDialog.getOpenFileNames(self, "Source (any frame of a sequence)", start, INPUT_FILTER)
        if paths:
            settings().setValue("last_folder", os.path.dirname(paths[0]))
            self.set_source(paths)

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Source folder", settings().value("last_folder", ""))
        if path:
            settings().setValue("last_folder", path)
            self.set_source([path])

    def _source_typed(self):
        text = self.source.text().strip().strip('"')
        if text and os.path.exists(text) and text != getattr(self, "_source_text", ""):
            self.set_source([text])

    def set_source(self, paths):
        """Files: their sequences or movies. Folders: scanned (in the background, once; Add
        Folder again scans again) and filtered with the pattern field."""
        self._stop_scan()
        self.thumbs.forget_stamps()                   # the files may have changed since
        self.files_selected = set()                   # nothing selected: all are processed
        self.files, self.folders, problems = [], [], []
        for path in paths:
            if os.path.isdir(path):
                folder = os.path.normpath(path)
                self.folders.append(folder)
                self.listings.pop(folder, None)        # picked again: scan again
                continue
            try:
                item = core.describe_file(path)
            except (TranscodeError, OSError) as exc:
                problems.append(str(exc))
                continue
            if all(os.path.normcase(i.path) != os.path.normcase(item.path) for i in self.files):
                self.files.append(item)
        self._source_text = paths[0] if len(paths) == 1 else "; ".join(paths)
        self.source.setText(self._source_text)
        if problems:
            QMessageBox.warning(self, "Source", "\n".join(problems[:20]))
        self.to_scan = [f for f in self.folders if f not in self.listings]
        self._scan_next()
        self._refilter()

    # Files tab: what the folder scan found, filtered

    def _build_files(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        row.addWidget(QLabel("Names:"))
        self.patterns = QLineEdit(core.DEFAULT_PATTERNS)
        self.patterns.setToolTip("Which files of the scanned folders to process: words separated by "
                                 "spaces, matched against the file name (any case), ALL of them: "
                                 "*.exr beauty v003 = EXR files with beauty and v003 in the name. "
                                 "File types (*.exr *.mov *.mp4) are alternatives: any of them. "
                                 "With wildcards a word matches the whole name (*beauty*.exr); a "
                                 "plain word matches anywhere in it. ! leaves names out: *.exr !tech\n"
                                 "Picking a preset sets it to the preset's input type. A folder is "
                                 "scanned once: changing this filters again without scanning.")
        row.addWidget(self.patterns, 2)
        row.addWidget(QLabel("Leave out:"))
        self.leave_out = QLineEdit()
        self.leave_out.setPlaceholderText("words matched against the whole path")
        self.leave_out.setToolTip("Leaves out files whose whole path matches one of these words "
                                  "(separated by spaces, any case): a plain word anywhere in the "
                                  "path (old, _tmp), or wildcards over the whole path "
                                  "(*/old/*, */wip_*)")
        row.addWidget(self.leave_out, 2)
        row.addWidget(QLabel("Thumbnails:"))
        self.thumb_size = QSlider(Qt.Orientation.Horizontal)
        self.thumb_size.setRange(THUMB_SIZES[0], THUMB_SIZES[1])
        self.thumb_size.setValue(THUMB_SIZES[2])
        self.thumb_size.setFixedWidth(110)
        self.thumb_size.setToolTip("How big the thumbnails are shown (they are made 256 pixels wide; "
                                   "bigger is scaled up)")
        self.thumb_size.valueChanged.connect(self._thumb_size_changed)
        row.addWidget(self.thumb_size)
        self.files_tree = QCheckBox("Tree")
        self.files_tree.setChecked(True)
        self.files_tree.setToolTip("On: the files in their folders (below the scanned folder). Off: "
                                   "one flat list, with each file's folder")
        self.files_tree.toggled.connect(lambda _on: self._fill_files())
        row.addWidget(self.files_tree)
        self.files_expand = QPushButton("Unfold")
        self.files_expand.setToolTip("Open every folder of the tree")
        self.files_expand.clicked.connect(self._unfold_all)
        self.files_collapse = QPushButton("Fold")
        self.files_collapse.setToolTip("Close every folder of the tree")
        self.files_collapse.clicked.connect(self._fold_all)
        row.addWidget(self.files_expand)
        row.addWidget(self.files_collapse)
        col.addLayout(row)
        self.files_view = FilesView()
        self.files_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files_selected = set()                # normcase paths of the selected inputs
        self._files_filling = False
        self.files_view.itemSelectionChanged.connect(self._files_selection_changed)
        self.files_view.setColumnCount(len(self.FILE_COLUMNS))
        self.files_view.setHeaderLabels(list(self.FILE_COLUMNS))
        # the thumbnail shown first (left), the tree's folders and arrows stay in Name; the
        # Play / Explore buttons right after the name
        header = self.files_view.header()
        header.moveSection(header.visualIndex(COL_THUMB), 0)
        header.moveSection(header.visualIndex(COL_OPEN), header.visualIndex(COL_NAME) + 1)
        header.moveSection(header.visualIndex(COL_ROW), 0)          # the row number before all
        self.files_view.setTreePosition(COL_NAME)
        self.files_view.setToolTip("The media the jobs are made for: picked files, and what the scan of "
                              "the folders found with these filters (image sequences as one item)")
        self.files_view.setUniformRowHeights(True)
        self.files_view.setIconSize(QSize(THUMB_SIZES[2], THUMB_SIZES[2] * 9 // 16))
        self.files_view.verticalScrollBar().valueChanged.connect(self._files_scrolled)
        self.files_folded = set()                  # folder rows the user folded (kept on refill)
        self.files_view.itemExpanded.connect(self._folder_unfolded)
        self.files_view.itemCollapsed.connect(self._folder_folded)
        self.files_hidden = set()                  # columns the user hid (right-click the header)
        header = self.files_view.header()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._files_columns_menu)
        header.setToolTip("Right-click to show or hide columns")
        self.files_view.setAlternatingRowColors(True)
        col.addWidget(self.files_view, 1)
        self.files_count = QLabel("")
        self.files_count.setStyleSheet("color: #8a8a8a;")
        col.addWidget(self.files_count)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(FILTER_GRACE)   # once the typing stops
        self.filter_timer.timeout.connect(self._filter_typed)
        self.patterns.textChanged.connect(lambda _t: self.filter_timer.start())
        self.leave_out.textChanged.connect(lambda _t: self.filter_timer.start())
        return page

    def _fill_files(self, thumb_grace=THUMB_GRACE):
        """The Files tab from self.items: a tree below each scanned folder (picked files on
        their own), or a flat list. thumb_grace: ms before the rows on screen get their
        pictures (longer after a filter change: the filter may still be changing)."""
        if not hasattr(self, "files"):
            return
        tree = self.files_tree.isChecked()
        scroll = self.files_view.verticalScrollBar().value()   # kept while a scan refills
        self._files_filling = True                 # the selection is put back below
        self.files_view.clear()
        self.files_view.setRootIsDecorated(tree)
        self._apply_file_columns()
        self.file_leaves = []
        self.files_expand.setEnabled(tree)
        self.files_collapse.setEnabled(tree)
        nodes = {}

        def node(key, text, parent):
            if key not in nodes:
                item = QTreeWidgetItem([text] + [""] * (len(self.FILE_COLUMNS) - 1))
                item.setData(COL_NAME, Qt.ItemDataRole.UserRole, repr(key))
                item.setToolTip(0, key[-1] if isinstance(key, tuple) else text)
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
                if parent is None:
                    self.files_view.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                nodes[key] = item
            return nodes[key]

        colour = self._files_colour()
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for number, media in enumerate(self.items, 1):
            texts = [""] * len(self.FILE_COLUMNS)
            texts[COL_NAME], texts[COL_FRAMES], texts[COL_FOLDER] = media.pattern, media.describe(), media.folder
            texts[COL_ROW] = str(number)
            leaf = QTreeWidgetItem(texts)
            leaf.setTextAlignment(COL_ROW, right)
            leaf.setForeground(COL_ROW, _brush("#8a8a8a"))
            self._set_probed(leaf, self.thumbs.info(media.path, colour), media)
            leaf.setToolTip(0, media.path)
            leaf.setToolTip(COL_FOLDER, media.folder)
            self._set_thumb(leaf, media.path, colour)
            self.file_leaves.append(leaf)
            if not tree:
                self.files_view.addTopLevelItem(leaf)
                continue
            if media.base_folder:
                top = node(("base", media.base_folder), media.base_folder, None)
                rel = os.path.relpath(media.folder, media.base_folder)
                parent, path = top, media.base_folder
                for part in ([] if rel in (".", "") else rel.replace("\\", "/").split("/")):
                    path = os.path.join(path, part)
                    parent = node(("dir", path), part, parent)
            else:
                parent = node(("picked",), "Picked files", None)
            parent.addChild(leaf)
        self.open_buttons = set()                  # leaves with their buttons (made when shown)
        self._fill_file_outputs()
        self.thumbs.forget_waiting()
        self.thumb_timer.start(thumb_grace)
        self.rows_timer.start()
        if tree:
            self.files_view.blockSignals(True)
            self.files_view.expandAll()
            for key, item in nodes.items():       # folders the user folded stay folded
                if repr(key) in self.files_folded:
                    item.setExpanded(False)
            self.files_view.blockSignals(False)
        keep = self.files_selected                 # what the user selected, if still listed
        self.files_selected = set()
        for leaf, media in zip(self.file_leaves, self.items):
            if os.path.normcase(media.path) in keep:
                leaf.setSelected(True)
                self.files_selected.add(os.path.normcase(media.path))
        # no current row either: Qt would otherwise select it at the first key press or focus
        self.files_view.selectionModel().setCurrentIndex(
            QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate)
        self._files_filling = False
        self._show_selected_count()
        for column in range(len(self.FILE_COLUMNS)):
            self.files_view.resizeColumnToContents(column)
        self.files_view.header().resizeSection(COL_OPEN, 62)     # the two buttons
        QTimer.singleShot(0, lambda v=scroll: self.files_view.verticalScrollBar().setValue(v))
        self._show_files_count()

    def _leaves_below(self, item):
        out, todo = [], [item]
        while todo:
            node = todo.pop()
            if node.childCount() == 0:
                out.append(node)
            todo.extend(node.child(i) for i in range(node.childCount()))
        return out

    def selected_inputs(self):
        """The inputs of the selected rows (a selected folder row: all inputs below it), in
        list order; [] when nothing is selected."""
        if not hasattr(self, "files_view"):
            return []
        chosen = set()
        for item in self.files_view.selectedItems():
            chosen.update(id(leaf) for leaf in self._leaves_below(item))
        return [media for leaf, media in zip(getattr(self, "file_leaves", []), self.items)
                if id(leaf) in chosen]

    def inputs_to_submit(self):
        """What Process / Add to Queue send: the selected rows, or all when none are."""
        return self.selected_inputs() or list(self.items)

    def _files_selection_changed(self):
        if self._files_filling:
            return
        self.files_selected = {os.path.normcase(m.path) for m in self.selected_inputs()}
        self._show_selected_count()

    def _show_selected_count(self):
        """ "Add to Queue [3]" while 3 inputs are selected; the plain names when none are."""
        n = len(self.files_selected)
        for button, text, what in ((getattr(self, "go_button", None), "Process", "process"),
                                   (getattr(self, "queue_button", None), "Add to Queue", "queue")):
            if button is None:
                continue
            button.setText("{} [{}]".format(text, n) if n else text)
            button.setToolTip(
                ("Only the {} input{} selected on the Files tab ({} all: select nothing - click the "
                 "selected row again, or Ctrl+click)").format(n, "" if n == 1 else "s", what)
                if n else ("Add the jobs to the queue and start it now" if what == "process" else
                           "Add the jobs to the queue without starting it (Queue tab: Start, or "
                           "Send to Deadline)") + ": every input listed, or only the rows "
                           "selected on the Files tab")

    def _show_files_count(self):
        """ "28 inputs - 1,234 frames  (from 1,000 media files ...)"; movies not probed yet
        add a + (their frame count comes with their thumbnail)."""
        n = len(self.items)
        frames, unknown = files_frames(self.items, self.thumbs, self._files_colour())
        scanned = sum(len(names) for f in self.folders for _d, names in self.listings.get(f, []))
        text = "{:,} input{}".format(n, "" if n == 1 else "s")
        if n:
            text += " - {:,}{} frame{}".format(frames, "+" if unknown else "", "" if frames == 1 else "s")
        if self.folders:
            text += "  (from {:,} media files in the scanned folders)".format(scanned)
        self.files_count.setText(text)
        self.files_count.setToolTip(
            "{} movie{} not read yet: their frames are counted once their thumbnails are made".format(
                unknown, "" if unknown == 1 else "s") if unknown else "")

    FILE_COLUMNS = FILE_COLUMNS

    def _apply_file_columns(self):
        """Hide the columns the user hid; Folder also in the tree (the tree shows it)."""
        tree = self.files_tree.isChecked()
        for column, title in enumerate(self.FILE_COLUMNS):
            hidden = title in self.files_hidden or (title == "Folder" and tree)
            self.files_view.setColumnHidden(column, hidden and column > 0)

    def _files_columns_menu(self, pos):
        menu = QMenu(self)
        for title in self.FILE_COLUMNS[1:]:          # Name always stays
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(title not in self.files_hidden)
            if title == "Folder" and self.files_tree.isChecked():
                action.setEnabled(False)
                action.setText("Folder (in the flat list)")
            action.toggled.connect(lambda on, t=title: self._toggle_file_column(t, on))
        menu.exec(self.files_view.header().mapToGlobal(pos))

    def _toggle_file_column(self, title, shown):
        if shown:
            self.files_hidden.discard(title)
        else:
            self.files_hidden.add(title)
        self._apply_file_columns()
        for column in range(len(self.FILE_COLUMNS)):
            self.files_view.resizeColumnToContents(column)
        self.files_view.header().resizeSection(COL_OPEN, 62)
        self.thumb_timer.start()

    def _fold_all(self):
        """Fold every folder (and keep them folded while a scan adds files)."""
        stack = [self.files_view.topLevelItem(i) for i in range(self.files_view.topLevelItemCount())]
        while stack:
            item = stack.pop()
            key = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
            if key:
                self.files_folded.add(key)
            stack += [item.child(i) for i in range(item.childCount())]
        self.files_view.collapseAll()

    def _unfold_all(self):
        self.files_folded.clear()
        self.files_view.expandAll()

    def _folder_unfolded(self, item):
        self.files_folded.discard(item.data(COL_NAME, Qt.ItemDataRole.UserRole))
        self.thumb_timer.start()

    def _folder_folded(self, item):
        key = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        if key:
            self.files_folded.add(key)

    def _fill_file_outputs(self):
        """The Output column: the file each input makes with the selected super-preset and
        the destination (keywords known before the run filled in). Red: two inputs make the
        same file, or the output would write over its input."""
        leaves = getattr(self, "file_leaves", [])
        if not leaves or len(leaves) != len(self.items):
            return
        entry = self.super_presets.get(self._preset_name()) if hasattr(self, "preset_list") else None
        if entry is None:
            for leaf in leaves:
                leaf.setText(COL_OUTPUT, "")
                leaf.setToolTip(COL_OUTPUT, "Pick a super-preset on the Presets tab")
            return
        spec = self.output_spec()
        output_type, ext, _issues = core.output_of(entry, self.presets)
        custom = self._custom()
        outputs = []
        for media in self.items:
            try:
                outputs.append(core.predict_output(media, spec, output_type, ext, custom))
            except Exception as exc:              # a keyword that can't be filled yet
                outputs.append("? " + str(exc))
        seen = {}
        for out in outputs:
            key = os.path.normcase(out)
            seen[key] = seen.get(key, 0) + 1
        clash = QBrush(QColor("#e07070"))
        for leaf, media, out in zip(leaves, self.items, outputs):
            leaf.setText(COL_OUTPUT, os.path.basename(out) or out)
            tip = out
            if seen[os.path.normcase(out)] > 1:
                tip += "\nThe same output as another input: one would overwrite the other."
            elif core.overwrites_input(media, out):
                tip += "\nThis would write over the input."
            else:
                leaf.setData(COL_OUTPUT, Qt.ItemDataRole.ForegroundRole, None)
                leaf.setToolTip(COL_OUTPUT, tip)
                continue
            leaf.setForeground(COL_OUTPUT, clash)
            leaf.setToolTip(COL_OUTPUT, tip)
        self.files_view.resizeColumnToContents(COL_OUTPUT)

    # thumbnails, made for what is on screen

    def _files_scrolled(self, _value=0):
        self.rows_timer.start()
        self.thumb_timer.start()

    def _fill_open_buttons(self):
        """The Play / Explore buttons, made only for the rows on screen (thousands of rows
        would take long to make all at once)."""
        leaves = getattr(self, "file_leaves", [])
        if not leaves or len(leaves) != len(self.items):
            return
        index = {id(leaf): media for leaf, media in zip(leaves, self.items)}
        for leaf in visible_items(self.files_view, leaves):
            if id(leaf) not in self.open_buttons:
                self.open_buttons.add(id(leaf))
                self.files_view.setItemWidget(leaf, COL_OPEN, self._open_buttons(index[id(leaf)].path))

    def _filter_typed(self):
        """A filter field changed and the typing stopped: filter again; the new rows get their
        pictures a little later (the filter may change again)."""
        self._refilter(thumb_grace=THUMB_GRACE_NEW)

    def _request_thumbnails(self):
        self.thumb_timer.setInterval(THUMB_GRACE)
        if hasattr(self, "open_buttons"):
            self._fill_open_buttons()                 # rows shown by unfolding, resizing ...
        page = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        if page is None:
            return
        if self.tabs.currentIndex() == 0 and self.middle_tabs.currentIndex() == 1:
            if all(title in self.files_hidden for title in PROBED_COLUMNS):
                return                            # nothing shown needs the thumbnail run
            leaves = visible_items(self.files_view, getattr(self, "file_leaves", []))
            index = {id(leaf): media for leaf, media in zip(self.file_leaves, self.items)}
            colour = self._files_colour()
            for leaf in reversed(leaves):         # the top of the view first (newest first)
                media = index.get(id(leaf))
                if media is not None:
                    self.thumbs.request(media.path, colour)
        elif self.tabs.currentIndex() == 1:
            items = [self.queue_list.item(i) for i in range(self.queue_list.count())]
            for item in reversed(visible_items(self.queue_list, items)):
                job = self._job(item.data(Qt.ItemDataRole.UserRole))
                if job is not None:
                    self.thumbs.request(job.input, core.colour_args(job.super_args))

    def _set_thumb(self, leaf, source, colour):
        """The input's thumbnail at the slider's size (scaled from the 256-pixel one)."""
        pixmap = self.thumbs.original(source, colour)
        if pixmap is None or pixmap.isNull():
            leaf.setIcon(COL_THUMB, QIcon())
            return
        size = self.files_view.iconSize()
        leaf.setIcon(COL_THUMB, QIcon(pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation)))

    def _thumb_size_changed(self, width):
        self.files_view.setIconSize(QSize(width, width * 9 // 16))
        colour = self._files_colour()
        for leaf, media in zip(getattr(self, "file_leaves", []), self.items):
            self._set_thumb(leaf, media.path, colour)
        self.files_view.resizeColumnToContents(COL_THUMB)
        self.thumb_timer.start()                  # more rows may fit on screen, or fewer

    def _open_buttons(self, path):
        """Play and Explore for a row of the Files tab, as in the queue."""
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(2)
        style = self.style()
        for icon, tip, slot in ((QStyle.StandardPixmap.SP_MediaPlay, "Open the source", lambda: _open(path)),
                                (QStyle.StandardPixmap.SP_DirOpenIcon, "Show the source in the file browser",
                                 lambda: core.reveal(path))):
            button = QToolButton()
            button.setIcon(row_icon(style, icon))
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.clicked.connect(slot)
            lay.addWidget(button)
        lay.addStretch(1)
        return box

    @staticmethod
    def _set_probed(leaf, info, media=None):
        """Size, pixel aspect and frame rate of an input, from its thumbnail run (and a
        movie's frame count)."""
        info = info or {}
        if media is not None and media.kind == "movie" and info.get("frames"):
            leaf.setText(COL_FRAMES, "movie, {:,} frames".format(info["frames"]))
        leaf.setText(COL_SIZE, info.get("size", ""))
        leaf.setText(COL_PAR, info.get("par", ""))
        leaf.setText(COL_FPS, info.get("fps", ""))
        if info.get("fps_frac"):
            leaf.setToolTip(COL_FPS, info["fps_frac"])

    def _files_colour(self):
        """The selected super-preset's colour options, for the Files tab's pictures."""
        entry = self.super_presets.get(self._preset_name()) if hasattr(self, "preset_list") else None
        return tuple(core.preset_colour_args(entry))

    def _refresh_file_thumbs(self):
        """Another super-preset: the Files tab's pictures in its colour (made if needed)."""
        colour = self._files_colour()
        for leaf, media in zip(getattr(self, "file_leaves", []), self.items):
            pixmap = self.thumbs.get(media.path, colour)
            self._set_thumb(leaf, media.path, colour)
            self._set_probed(leaf, self.thumbs.info(media.path, colour), media)
        self.thumbs.forget_waiting()
        self.thumb_timer.start()

    def _thumbnail_ready(self, source):
        colour = self._files_colour()
        pixmap = self.thumbs.get(source, colour)
        if pixmap is not None:
            for leaf, media in zip(getattr(self, "file_leaves", []), self.items):
                if media.path == source:
                    self._set_thumb(leaf, source, colour)
                    self._set_probed(leaf, self.thumbs.info(source, colour), media)
                    if media.kind == "movie":
                        self._show_files_count()      # its frames are known now
            for column in (COL_SIZE, COL_PAR, COL_FPS):
                self.files_view.resizeColumnToContents(column)
        for job in self.jobs:
            if job.input == source and job.id in self.rows:
                pixmap = self.thumbs.get(source, core.colour_args(job.super_args))
                if pixmap is not None:
                    self.rows[job.id].thumb.setPixmap(pixmap)

    def _stop_scan(self):
        self.to_scan = []
        if self.scanner is not None:
            self.scanner.stop = True
            self.scanner.wait()
            self.scanner = None

    def _scan_next(self):
        if self.scanner is not None or not self.to_scan:
            return
        folder = self.to_scan.pop(0)
        self.scanner = Scanner(folder, self.patterns.text(), self)
        self.scanner.progressed.connect(self._scan_progress)
        self.scanner.found_more.connect(self._scan_partial)
        self.scanner.listed.connect(self._scan_done)
        self.statusBar().showMessage("Scanning {} ...".format(folder))
        self.scanner.start()

    def _scan_progress(self, folder, scanned, found):
        self.statusBar().showMessage("Scanning {}:  {:,} files scanned, {:,} media found".format(
            folder, scanned, found))

    def _scan_partial(self, folder, listing):
        """What the running scan found so far: shown (and filtered) at once."""
        if self.scanner is None or self.scanner.stop:
            return
        self.listings[folder] = listing
        start = time.time()
        self._refilter()
        # a big listing takes a while to show: refill less often, so the scan isn't held up
        self.scanner.partial_every = max(0.8, 5 * (time.time() - start))

    def _scan_done(self, folder, listing):
        scanner = self.scanner
        scanner.wait()
        self.scanner = None
        if scanner.stop:
            return
        self.listings[folder] = listing
        if scanner.error:
            QMessageBox.warning(self, "Source", "{}:\n{}".format(folder, scanner.error))
        self._refilter()
        self._scan_next()

    def _refilter(self, thumb_grace=THUMB_GRACE):
        """The inputs: the picked files, and what the pattern field finds in the folders'
        listings (no new scan)."""
        items = list(self.files)
        known = {os.path.normcase(i.path) for i in items}
        for folder in self.folders:
            for item in core.filter_listing(self.listings.get(folder, []), self.patterns.text(), folder,
                                            self.leave_out.text()):
                if os.path.normcase(item.path) not in known:
                    known.add(os.path.normcase(item.path))
                    items.append(item)
        self.items = items
        self._fill_files(thumb_grace)
        self._inputs_changed()

    def _inputs_changed(self):
        n = len(self.items)
        if n == 1:
            text = "{}: {}".format(self.items[0].pattern, self.items[0].describe())
        elif n:
            kinds = {}
            for item in self.items:
                kinds[core.KIND_TITLES[item.kind]] = kinds.get(core.KIND_TITLES[item.kind], 0) + 1
            text = "{} inputs: {}".format(n, ", ".join("{} {}".format(c, k.lower()) for k, c in kinds.items()))
        else:
            text = "No media found." if self.source.text().strip() else ""
        if self.scanner is not None:
            text = "Scanning... " + text
        elif self.folders:
            files = sum(len(names) for f in self.folders for _d, names in self.listings.get(f, []))
            text += "   ({:,} media files in the folder{}; the filter picks from them)".format(
                files, "s" if len(self.folders) > 1 else "")
        self.statusBar().showMessage(text)
        self.source.setToolTip("\n".join(i.path for i in self.items[:40])
                               + ("\n..." if len(self.items) > 40 else ""))
        self._fill_super_presets()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.tabs.setCurrentIndex(0)
            event.acceptProposedAction()
            QTimer.singleShot(0, lambda: self.set_source(paths))

    # presets list

    def _preset_name(self):
        item = self.preset_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def _fill_super_presets(self):
        """The super-presets, each marked with how it fits the source."""
        current = self._preset_name() or settings().value("super_preset", "")
        self.preset_list.blockSignals(True)
        self.preset_list.clear()
        for name, entry in self.super_presets.items():
            output_type, ext, issues = core.output_of(entry, self.presets)
            level = core.ERROR if issues else (
                core.check_summary(entry, self.items, self.presets)[0] if self.items else core.OK)
            mark = MARKS[level] if (self.items or issues) else "  "
            item = QListWidgetItem("{}  {}".format(mark, name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            if level != core.OK:
                item.setForeground(_brush({core.WARN: "#e8d27f", core.ERROR: "#e07070"}[level]))
            item.setToolTip(entry.get("notes") or name)
            self.preset_list.addItem(item)
            if name == current:
                self.preset_list.setCurrentItem(item)
        if self.preset_list.currentRow() < 0 and self.preset_list.count():
            self.preset_list.setCurrentRow(0)
        self.preset_list.blockSignals(False)
        if hasattr(self, "dest_reset"):
            self._apply_preset_destination()          # a reload picks up a changed destination
        self._super_changed()

    def _preset_picked(self, *_args):
        """A preset picked by the user: the folder filter follows its input type, and the
        Destination its path and name (both can be changed after)."""
        entry = self.super_presets.get(self._preset_name())
        if entry is not None:
            patterns = core.patterns_for_preset(entry, self.patterns.text())
            if patterns is not None and patterns != self.patterns.text():
                self.patterns.setText(patterns)          # filters again, no new scan
        self._apply_preset_destination(force=True)
        self._super_changed()
        self._refresh_file_thumbs()            # the pictures in this preset's colour

    def _super_changed(self, *_args):
        name = self._preset_name()
        entry = self.super_presets.get(name)
        if entry is None:
            self.preset_info.setText("No super-presets yet: save one in vfx-transcode-tuner."
                                     if not self.super_presets else "")
            self._output_changed()
            return
        output_type, ext, issues = core.output_of(entry, self.presets)
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        lines = ["<b>{}</b>".format(esc(name)),
                 "Makes: {}".format("a .{} movie".format(ext) if output_type == "movie"
                                    else "{} frames".format(output_type.upper()))]
        if entry.get("input_type") or entry.get("inputs"):
            lines.append("For: " + (core.tuner.INPUT_TYPES.get(entry.get("input_type", ""))
                                    or ", ".join(core.KIND_TITLES[k] for k in entry["inputs"])))
        dest = core.destination_of(entry)
        if dest is not None:
            lines.append("Destination: {}{}".format(esc(dest.path or "./"), esc(dest.name or "")))
        if entry.get("notes"):
            lines.append("<span style='color:#b0b0b0'>{}</span>".format(esc(entry["notes"])))
        if issues:
            level, text = core.ERROR, "\n".join("Error: " + i.text for i in issues)
        elif self.items:
            level, text = core.check_summary(entry, self.items, self.presets)
        else:
            level, text = core.OK, ""
        if self.items or issues:
            colour = {core.OK: "#7ec27e", core.WARN: "#e8d27f", core.ERROR: "#e07070"}[level]
            body = esc(text) or "Fits the source ({} input{}).".format(
                len(self.items), "" if len(self.items) == 1 else "s")
            lines.append("<br><span style='color:{}'>{} {}</span>".format(colour, MARKS[level], body))
        self.preset_info.setText("<br>".join(lines))
        self._output_changed()

    # Process / Add to Queue

    def submit(self, start):
        if self.scanner is not None:
            QMessageBox.information(self, "Process", "Still searching the folder - a moment.")
            return
        if not self.items:
            QMessageBox.information(self, "Process", "Add a file or a folder first.")
            return
        inputs = self.inputs_to_submit()          # the selected rows, or all of them
        name = self._preset_name()
        entry = self.super_presets.get(name)
        if entry is None:
            QMessageBox.information(self, "Process", "Pick a preset.")
            return
        missing = core.missing_custom(self._destination(), self._custom())
        if missing:
            QMessageBox.information(self, "Process", "The destination needs a value: type the {}.".format(
                " and the ".join(missing)))
            return
        interpret = self.interpretation()
        bad = interpret.problems()
        if bad:
            self.interp_toggle.setChecked(True)
            QMessageBox.information(self, "Process", "Tweaks:\n" + "\n".join(bad))
            return
        jobs, problems = core.make_jobs(inputs, name, entry, self.output_spec(), self.presets,
                                        custom=self._custom(), dest=self._destination(),
                                        interpret=interpret,
                                        presets_file=self.presets_file,
                                        play=self.play_after.isChecked(),
                                        explore=self.explore_after.isChecked())
        if problems:
            lines = ["{}  {}:\n    {}".format(MARKS[c.level], i.pattern, c.text().replace("\n", "\n    "))
                     for i, c in problems[:30]]
            if len(problems) > 30:
                lines.append("... and {} more".format(len(problems) - 30))
            skipped = sum(1 for _i, c in problems if c.level == core.ERROR)
            head = ("{} input(s) can't be converted with {} and are left out.\n\n".format(skipped, name)
                    if skipped else "")
            if not jobs:
                QMessageBox.warning(self, "Process", head + "\n".join(lines))
                return
            box = QMessageBox(QMessageBox.Icon.Warning, "Process",
                              head + "{} {} job(s)?".format("Process" if start else "Queue", len(jobs)),
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
            box.setDetailedText("\n".join(lines))
            box.exec()
            if box.clickedButton() is not box.button(QMessageBox.StandardButton.Yes):
                return
        self.jobs += jobs
        self._fill_queue()
        self._save_queue()
        self._save_settings()
        if start:
            self.set_paused(False)
            self.statusBar().showMessage("Processing {} job(s): see the Queue tab.".format(len(jobs)), 6000)
        else:
            self.statusBar().showMessage("{} job(s) added to the queue{}.".format(
                len(jobs), ": press Start on the Queue tab" if self.paused else ""), 6000)

    # Queue tab

    def _build_queue(self, page):
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.start_button = QPushButton("Start")       # at the bottom of the tab, below
        self.start_button.setToolTip('Start the queue, or pause it (the running job finishes)')
        self.start_button.setMinimumHeight(34)
        self.start_button.setStyleSheet(GO_STYLE)
        self.start_button.clicked.connect(lambda: self.set_paused(not self.paused))
        row.addWidget(QLabel("Jobs at once:"))
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setToolTip("oiiotool already uses all cores: more than 1 helps mostly for movies "
                                 "and small sequences.")
        row.addWidget(self.parallel)
        row.addSpacing(16)
        for text, slot, tip in (
                ("Cancel", self.cancel_selected, "Stop the selected running jobs, and don't run the selected waiting ones."),
                ("Retry", self.retry_selected, "Run the selected finished, failed or cancelled jobs again."),
                ("Remove", self.remove_selected, "Take the selected jobs out of the queue (running ones are stopped)."),
                ("Clear finished", self.clear_finished, "Take done, done on farm and cancelled jobs out of the queue "
                                                        "(jobs still on the farm stay, to check)."),
                ("Up", lambda: self.move_selected(-1), "Run the selected jobs earlier."),
                ("Down", lambda: self.move_selected(1), "Run the selected jobs later.")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        self.deadline_button = QPushButton("Send to Deadline...")
        self.deadline_button.setToolTip("Submit the waiting jobs to Deadline instead of running them here.")
        self.deadline_button.clicked.connect(self.send_to_deadline)
        self.farm_button = QPushButton("Check farm status")
        self.farm_button.setToolTip("For the jobs on the farm: ask Deadline how each is doing and look "
                                    "for its output. Completed with the output there: Done on farm; "
                                    "failed, or completed without the output: Failed.")
        self.farm_button.clicked.connect(self.check_farm)
        row.addWidget(self.farm_button)
        row.addWidget(self.deadline_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.hide_done = QCheckBox("Hide done")
        self.hide_done.setToolTip("Leave the jobs that are done out of the list")
        self.hide_done.toggled.connect(lambda _on: self._apply_hide_done())
        row.addWidget(self.hide_done)
        row.addStretch(1)
        for text, slot, tip in (
                ("Save Queue...", self.save_queue_as, "Save all the jobs, with their status and logs, to a "
                                                      "file, e.g. for another user to process"),
                ("Load Queue...", self.load_queue_from, "Load jobs from a saved queue file")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)

        split = QSplitter(Qt.Orientation.Vertical)
        self.queue_list = QListWidget()
        self.queue_list.setToolTip('The jobs, in the order they run; select several with Ctrl / Shift for the buttons above')
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.queue_list.setSpacing(1)
        self.queue_list.verticalScrollBar().valueChanged.connect(lambda _v: self.thumb_timer.start())
        self.queue_list.itemSelectionChanged.connect(self._show_job_log)
        split.addWidget(self.queue_list)

        bottom = QWidget()
        col = QVBoxLayout(bottom)
        col.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.job_title = QLabel("Select a job to see its log.")
        row.addWidget(self.job_title, 1)
        button = QPushButton("Copy command")
        button.clicked.connect(self.copy_command)
        row.addWidget(button)
        col.addLayout(row)
        self.job_log = QPlainTextEdit()
        self.job_log.setToolTip('What vfx-transcode prints for the selected job')
        self.job_log.setReadOnly(True)
        self.job_log.setFont(QFont("Consolas", 9))
        self.job_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        col.addWidget(self.job_log)
        split.addWidget(bottom)
        split.setSizes([450, 250])
        layout.addWidget(split, 1)
        layout.addWidget(self.start_button)
        self.queue_summary = QLabel()
        layout.addWidget(self.queue_summary)

    def _fill_queue(self):
        selected = set(self.selected_ids())
        self.queue_list.clear()
        self.rows = {}
        for job in self.jobs:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            widget = JobWidget(job)
            item.setSizeHint(widget.sizeHint())
            self.queue_list.addItem(item)
            self.queue_list.setItemWidget(item, widget)
            item.setSelected(job.id in selected)
            self.rows[job.id] = widget
            self._update_row(job)
            pixmap = self.thumbs.get(job.input, core.colour_args(job.super_args))
            if pixmap is not None:
                widget.thumb.setPixmap(pixmap)
        self._apply_hide_done()
        self.thumb_timer.start()
        self._update_summary()

    def _apply_hide_done(self):
        hide = self.hide_done.isChecked() if hasattr(self, "hide_done") else False
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            job = self._job(item.data(Qt.ItemDataRole.UserRole))
            done = job is not None and job.status in core.FINISHED_OK
            item.setHidden(hide and done)
            if hide and done:
                item.setSelected(False)

    # save / load a queue (one user sets it up, another processes it)

    def save_queue_as(self):
        if not self.jobs:
            QMessageBox.information(self, "Save Queue", "The queue is empty.")
            return
        start = settings().value("queue_folder", "") or settings().value("last_folder", "")
        name = os.path.join(start, "queue_{}{}".format(time.strftime("%Y%m%d_%H%M"), core.QUEUE_EXT))
        path, _ = QFileDialog.getSaveFileName(self, "Save Queue", name,
                                              "vfx-transcode queue (*{});;All files (*)".format(core.QUEUE_EXT))
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += core.QUEUE_EXT
        try:
            core.save_queue(path, self.jobs)
        except OSError as exc:
            QMessageBox.warning(self, "Save Queue", "Could not save {}:\n{}".format(path, exc))
            return
        settings().setValue("queue_folder", os.path.dirname(path))
        counts = self._status_counts(self.jobs)
        self.statusBar().showMessage("Saved {} job(s) ({}) to {}".format(len(self.jobs), counts, path), 8000)

    def load_queue_from(self):
        start = settings().value("queue_folder", "") or settings().value("last_folder", "")
        path, _ = QFileDialog.getOpenFileName(self, "Load Queue", start,
                                              "vfx-transcode queue (*{} *.json);;All files (*)".format(core.QUEUE_EXT))
        if not path:
            return
        try:
            loaded, header = core.read_queue_file(path)
        except TranscodeError as exc:
            QMessageBox.warning(self, "Load Queue", str(exc))
            return
        settings().setValue("queue_folder", os.path.dirname(path))
        if not loaded:
            QMessageBox.information(self, "Load Queue", "{} has no jobs.".format(path))
            return
        origin = "saved {} by {} on {}".format(header.get("saved", "?"), header.get("saved_by") or "?",
                                               header.get("computer") or "?")
        replace = False
        if self.jobs:
            box = QMessageBox(QMessageBox.Icon.Question, "Load Queue",
                              "{} job(s) ({}), {}.\n\nThe queue has {} job(s) already.".format(
                                  len(loaded), self._status_counts(loaded), origin, len(self.jobs)),
                              QMessageBox.StandardButton.Cancel, self)
            append = box.addButton("Append", QMessageBox.ButtonRole.AcceptRole)
            replace_button = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
            box.setDefaultButton(append)
            box.exec()
            if box.clickedButton() not in (append, replace_button):
                return
            replace = box.clickedButton() is replace_button
            if replace and self.running:
                QMessageBox.information(self, "Load Queue", "Jobs are running: cancel them first, or "
                                                            "append instead.")
                return
        self.jobs = core.merge_jobs(self.jobs, loaded, replace)
        self._fill_queue()
        self.queue_dirty = True
        self.tabs.setCurrentIndex(1)
        missing = core.unreachable_inputs(loaded)
        message = "Loaded {} job(s) ({}), {}.".format(len(loaded), self._status_counts(loaded), origin)
        if missing:
            QMessageBox.warning(self, "Load Queue", message + "\n\n{} waiting job(s) have an input this "
                                "computer can't find, e.g.\n{}\n\nThey will fail unless the path is "
                                "reachable from here.".format(len(missing), "\n".join(j.input for j in missing[:5])))
        else:
            self.statusBar().showMessage(message + (" Press Start to run the waiting ones." if self.paused else ""), 8000)

    @staticmethod
    def _status_counts(jobs):
        counts = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        return ", ".join("{} {}".format(n, core.STATUS_TITLES[s].lower()) for s, n in counts.items())

    def _job(self, job_id):
        return next((j for j in self.jobs if j.id == job_id), None)

    def _update_row(self, job):
        widget = self.rows.get(job.id)
        if widget is None:
            return
        progress = self.running.get(job.id, (None, None, None))[1]
        busy = progress is not None and progress.value <= 0 and not progress.frames
        widget.job = job
        widget.refresh(busy, progress.stage if progress is not None else "")

    def _update_summary(self):
        counts = {}
        for job in self.jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        parts = ["{} {}".format(n, core.STATUS_TITLES[s].lower()) for s, n in counts.items()]
        state = "Paused" if self.paused else "Running"
        if self.paused and self.running:
            state = "Pausing: the running jobs finish, no new ones start"
        self.queue_summary.setText("{}.   {}".format(state, ", ".join(parts) or "The queue is empty."))
        self.start_button.setText("Start" if self.paused else "Pause")
        self.tabs.setTabText(1, "Queue ({})".format(len([j for j in self.jobs if j.status in
                                                         (core.WAITING, core.RUNNING)])))

    def selected_ids(self):
        return [i.data(Qt.ItemDataRole.UserRole) for i in self.queue_list.selectedItems()]

    def set_paused(self, paused):
        self.paused = paused
        self._update_summary()
        if not paused:
            self._start_next()

    # running

    def _start_next(self):
        if self.paused:
            return
        for job in self.jobs:
            if len(self.running) >= self.parallel.value():
                break
            if job.status == core.WAITING:
                self._start(job)
        if not self.running and not any(j.status == core.WAITING for j in self.jobs):
            self.paused = True                # all done: the next Queue doesn't start by itself
        self._update_summary()

    def _start(self, job):
        cmd = job.command()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")   # lines as they come, when run as a script
        process.setProcessEnvironment(env)
        job.status, job.progress, job.message = core.RUNNING, 0.0, "starting"
        job.started, job.finished = time.time(), 0.0
        job.log = ["> " + core.quote(cmd)]
        progress = core.Progress(job.frames, job.kind, job.output_type, job.started)
        self.running[job.id] = (process, progress, [""])
        process.readyReadStandardOutput.connect(lambda j=job: self._read(j))
        process.finished.connect(lambda code, status, j=job: self._finished(j, code, status))
        process.errorOccurred.connect(lambda error, j=job: self._error(j, error))
        process.start(cmd[0], cmd[1:])
        self._update_row(job)
        self._show_job_log()
        self.queue_dirty = True

    def _read(self, job):
        entry = self.running.get(job.id)
        if entry is None:
            return
        process, progress, buffer = entry
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").replace("\r\n", "\n")
        lines = (buffer[0] + text).split("\n")
        buffer[0] = lines.pop()
        for line in lines:
            line = line.rstrip()
            if not line:
                continue
            job.log.append(line)
            progress.feed(line)
        if lines:
            job.progress = progress.value
            if job.id not in self.cancelling:
                job.message = progress.stage
            if self._current_id() == job.id:
                self.job_log.appendPlainText("\n".join(l for l in lines if l.strip()))
            self._update_row(job)

    def _error(self, job, error):
        if error == QProcess.ProcessError.FailedToStart:
            job.log.append("Could not start: " + core.quote(job.command()))
            self._end(job, core.FAILED, "vfx-transcode could not be started")

    def _finished(self, job, code, status):
        entry = self.running.get(job.id)
        if entry is None:
            return                            # already ended (it failed to start)
        if entry is not None and entry[2][0].strip():
            job.log.append(entry[2][0].rstrip())
            entry[1].feed(entry[2][0])
        if job.id in self.cancelling:
            self._end(job, core.CANCELLED, "cancelled")
        elif status == QProcess.ExitStatus.NormalExit and code == 0:
            output = entry[1].output if entry is not None else ""
            self._end(job, core.DONE, output or "done")
        else:
            errors = [l[len("Error: "):] for l in job.log if l.startswith("Error: ")]
            self._end(job, core.FAILED, (errors[-1] if errors else "exit code {}".format(code))[:300])

    def _end(self, job, status, message):
        entry = self.running.pop(job.id, None)
        self.cancelling.discard(job.id)
        job.status, job.message, job.finished = status, message, time.time()
        if status == core.DONE:
            job.progress = 1.0
            job.result = entry[1].output if entry is not None else ""
            self._after(job, job.result)
        job.log.append("=== {} ({}): {}".format(core.STATUS_TITLES[status], core.format_seconds(job.elapsed()), message))
        if entry is not None:
            entry[0].deleteLater()
        self._update_row(job)
        self._apply_hide_done()
        if self._current_id() == job.id:
            self.job_log.appendPlainText(job.log[-1])
        self.queue_dirty = True
        QTimer.singleShot(0, self._start_next)

    def _after(self, job, done_line):
        """Play after conversion / Explore after conversion."""
        if not (job.play or job.explore):
            return
        path = core.result_path(job, done_line)
        try:
            if job.play and os.path.exists(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            if job.explore:
                core.reveal(path if os.path.exists(path) else os.path.dirname(path))
        except OSError as exc:
            job.log.append("Could not open {}: {}".format(path, exc))

    def _tick(self):
        for job_id, (process, progress, _buffer) in list(self.running.items()):
            job = self._job(job_id)
            if job is None:
                continue
            progress.poll()
            job.progress = progress.value
            if job.id not in self.cancelling:
                job.message = progress.stage
            self._update_row(job)
        if self.running:
            self._update_summary()
        if self.queue_dirty:
            self._save_queue()
        if not self.paused and len(self.running) < self.parallel.value():
            self._start_next()

    # queue actions

    def cancel_selected(self):
        for job_id in self.selected_ids():
            job = self._job(job_id)
            if job is None:
                continue
            if job.id in self.running:
                self.cancelling.add(job.id)
                job.message = "cancelling"
                process = self.running[job.id][0]
                pid = process.processId()
                if pid:
                    core.kill_tree(pid)
                process.kill()
            elif job.status == core.WAITING:
                job.status, job.message = core.CANCELLED, "cancelled before it ran"
            self._update_row(job)
        self.queue_dirty = True
        self._update_summary()

    def retry_selected(self):
        for job_id in self.selected_ids():
            job = self._job(job_id)
            if job is not None and job.status in (core.DONE, core.FARM_DONE, core.FAILED, core.CANCELLED,
                                                  core.SUBMITTED):
                job.status, job.progress, job.message, job.deadline_id = core.WAITING, 0.0, "", ""
                job.result = ""
                job.started = job.finished = 0.0
                self._update_row(job)
        self.queue_dirty = True
        self._update_summary()
        self._start_next()

    def remove_selected(self):
        ids = set(self.selected_ids())
        if any(i in self.running for i in ids):
            if QMessageBox.question(self, "Remove", "Stop the running jobs and remove them?") != \
                    QMessageBox.StandardButton.Yes:
                return
            self.cancel_selected()
        self.jobs = [j for j in self.jobs if j.id not in ids or j.id in self.running]
        self._fill_queue()
        self.queue_dirty = True

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.status not in (core.DONE, core.FARM_DONE, core.CANCELLED)]
        self._fill_queue()
        self.queue_dirty = True

    def move_selected(self, step):
        ids = set(self.selected_ids())
        indexes = [i for i, j in enumerate(self.jobs) if j.id in ids]
        for i in (indexes if step < 0 else reversed(indexes)):
            k = i + step
            if 0 <= k < len(self.jobs) and self.jobs[k].id not in ids:
                self.jobs[i], self.jobs[k] = self.jobs[k], self.jobs[i]
        self._fill_queue()
        self.queue_dirty = True

    def _current_id(self):
        items = self.queue_list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if len(items) == 1 else None

    def _show_job_log(self):
        job = self._job(self._current_id()) if self._current_id() else None
        if job is None:
            self.job_title.setText("Select a job to see its log.")
            self.job_log.clear()
            return
        self.job_title.setText("{}  ->  {}".format(job.pattern, job.predicted or job.output))
        self.job_log.setPlainText("\n".join(job.log) if job.log else core.quote(["vfx-transcode"] + job.args()))
        self.job_log.verticalScrollBar().setValue(self.job_log.verticalScrollBar().maximum())

    def copy_command(self):
        jobs = [self._job(i) for i in self.selected_ids()]
        lines = [core.quote(j.command()) for j in jobs if j is not None]
        if lines:
            QGuiApplication.clipboard().setText("\n".join(lines))
            self.statusBar().showMessage("Copied {} command(s).".format(len(lines)), 4000)

    # Deadline

    def check_farm(self):
        """Ask Deadline about every job on the farm (in the background) and look for its
        output; the rows change as the answers come."""
        jobs = [j for j in self.jobs if j.status == core.SUBMITTED and j.deadline_id]
        if not jobs:
            QMessageBox.information(self, "Check farm status", "No jobs on the farm.")
            return
        if getattr(self, "_farm_futures", None):
            return                                # a check is still running
        settings = saved_deadline_settings()
        self.farm_button.setEnabled(False)
        self.statusBar().showMessage("Checking {} job(s) on the farm through {} ...".format(
            len(jobs), settings.via))
        self._farm_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._farm_futures = {self._farm_pool.submit(core.farm_check, job, settings): job for job in jobs}
        self._farm_counts = {}
        self._farm_timer = QTimer(self)
        self._farm_timer.timeout.connect(self._farm_answers)
        self._farm_timer.start(200)

    def _farm_answers(self):
        for future in [f for f in self._farm_futures if f.done()]:
            job = self._farm_futures.pop(future)
            try:
                status, message = future.result()
            except Exception as exc:                  # Deadline not reachable: stop asking
                for other in list(self._farm_futures):
                    other.cancel()
                self._farm_futures.clear()
                self._farm_finished("Could not check the farm: {}".format(exc))
                return
            if status != job.status or message != job.message:
                job.log.append("Farm check: " + message)
            job.status, job.message = status, message
            if status in (core.FARM_DONE, core.FAILED) and not job.finished:
                job.finished = time.time()
            if status == core.FARM_DONE:
                job.progress = 1.0
            self._farm_counts[status] = self._farm_counts.get(status, 0) + 1
            self._update_row(job)
            self.queue_dirty = True
        if not self._farm_futures:
            counts = ", ".join("{} {}".format(n, core.STATUS_TITLES[s].lower())
                               for s, n in self._farm_counts.items())
            self._farm_finished("Farm checked: " + (counts or "nothing changed"))

    def _farm_finished(self, message):
        self._farm_timer.stop()
        self._farm_pool.shutdown(wait=False)
        self._farm_futures = {}
        self.farm_button.setEnabled(True)
        self._update_summary()
        self._apply_hide_done()
        self.statusBar().showMessage(message, 10000)

    def send_to_deadline(self):
        waiting = [j for j in self.jobs if j.status == core.WAITING]
        selected = [j for j in waiting if j.id in set(self.selected_ids())]
        if not waiting:
            QMessageBox.information(self, "Deadline", "No waiting jobs: queue some on the Process tab.")
            return
        dialog = DeadlineDialog(len(selected), len(waiting), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        s = dialog.result_settings()
        jobs = selected if dialog.only_selected.isChecked() and selected else waiting
        local = core.local_paths([j.input for j in jobs] + [j.predicted for j in jobs if j.predicted]
                                 + [s.executable or core.vfx_transcode_command()[0]])
        if local:
            text = ("These paths are on a local drive of this computer, the render nodes may not "
                    "see them:\n\n" + "\n".join(local[:12]) + ("\n..." if len(local) > 12 else "")
                    + "\n\nSubmit anyway?")
            if QMessageBox.question(self, "Deadline", text) != QMessageBox.StandardButton.Yes:
                return
        work = Path(tempfile.gettempdir()) / "vfx-transcode-deadline"
        dialog = QProgressDialog("Submitting to Deadline...", "Stop", 0, len(jobs), self)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        failed = []
        for n, job in enumerate(jobs):
            if dialog.wasCanceled():
                break
            dialog.setValue(n)
            dialog.setLabelText("Submitting {} ({}/{}) through {}".format(job.pattern, n + 1, len(jobs), s.via))
            QApplication.processEvents()
            try:
                job.deadline_id = core.submit_to_deadline(job, s, work)
            except TranscodeError as exc:
                job.message = str(exc).splitlines()[0][:200]
                job.log.append("Deadline: " + str(exc))
                failed.append((job, str(exc)))
                self._update_row(job)
                if len(failed) == 1 and any(t in str(exc) for t in (
                        "not found", "can't be reached", "HTTP 401", "HTTP 403", "needs a user")):
                    break                     # no connection: the others fail the same way
                continue
            job.status, job.message = core.SUBMITTED, "batch " + s.batch_name
            job.log.append("Submitted to Deadline: job {} (batch {})".format(job.deadline_id, s.batch_name))
            self._update_row(job)
        dialog.setValue(len(jobs))
        self.queue_dirty = True
        self._update_summary()
        done = sum(1 for j in jobs if j.status == core.SUBMITTED)
        if failed:
            QMessageBox.warning(self, "Deadline", "{} submitted, {} failed:\n\n{}".format(
                done, len(failed), failed[0][1][:1500]))
        else:
            self.statusBar().showMessage("{} job(s) submitted to Deadline, batch {}.".format(done, s.batch_name), 8000)

    # settings

    def _restore_settings(self):
        state = _json_setting("process", {})
        self.patterns.setText(state.get("patterns", core.DEFAULT_PATTERNS))
        self.leave_out.setText(state.get("leave_out", ""))
        self.files_tree.setChecked(bool(state.get("files_tree", True)))
        self.files_hidden = set(state.get("files_hidden", []))
        self.thumb_size.setValue(int(state.get("thumb_size", THUMB_SIZES[2])))
        self._apply_file_columns()
        # the selected super-preset's destination is in the fields already; last session's
        # path and name come back only when they were the user's own (for this same preset)
        preset_dest = core.destination_of(self.super_presets.get(self._preset_name()))
        if preset_dest is None or (state.get("dest_edited") and state.get("dest_source") == self._preset_name()):
            self.out_path.setText(state.get("path", "./"))
            self.out_name.setText(state.get("name", ""))
            for combo, key in ((self.path_preset, "path_preset"), (self.name_preset, "name_preset")):
                index = combo.findText(state.get(key, ""))
                if index >= 0:
                    combo.setCurrentIndex(index)
            self.dest_source, self.dest_edited = self._preset_name(), True
        self._show_dest_state()
        self.play_after.setChecked(bool(state.get("play", False)))
        known = core.Interpretation.__dataclass_fields__
        self._set_interpretation(core.Interpretation(**{k: str(v) for k, v in (state.get("interpret") or {}).items()
                                                        if k in known}))
        self.interp_toggle.setChecked(bool(state.get("interpret_open", False)))
        self.custom_path.setText(state.get("custom_path", ""))
        self.custom_name.setText(state.get("custom_name", ""))
        self._show_custom_fields()
        self.explore_after.setChecked(bool(state.get("explore", False)))
        try:
            self.parallel.setValue(int(settings().value("parallel", 1)))
        except (TypeError, ValueError):
            pass
        geometry = settings().value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_settings(self):
        _set_json_setting("process", {
            "patterns": self.patterns.text(), "leave_out": self.leave_out.text(),
            "files_tree": self.files_tree.isChecked(), "files_hidden": sorted(self.files_hidden),
            "thumb_size": self.thumb_size.value(),
            "path": self.out_path.text(), "name": self.out_name.text(),
            "path_preset": self.path_preset.currentText(), "name_preset": self.name_preset.currentText(),
            "play": self.play_after.isChecked(), "explore": self.explore_after.isChecked(),
            "custom_path": self.custom_path.text(), "custom_name": self.custom_name.text(),
            "dest_source": self.dest_source, "dest_edited": self.dest_edited,
            "interpret": asdict(self.interpretation()), "interpret_open": self.interp_toggle.isChecked()})
        settings().setValue("super_preset", self._preset_name())
        settings().setValue("parallel", self.parallel.value())
        settings().setValue("geometry", self.saveGeometry())

    def _save_queue(self):
        self.queue_dirty = False
        try:
            core.save_queue(queue_file(), self.jobs)
        except OSError as exc:
            self.statusBar().showMessage("The queue can't be saved: {}".format(exc), 6000)

    def closeEvent(self, event):
        self._stop_scan()
        self.thumbs.stop()
        if self.running:
            answer = QMessageBox.question(
                self, "Quit", "{} job(s) are running. Stop them and quit? They run again next "
                "time.".format(len(self.running)))
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for job_id, (process, _p, _b) in list(self.running.items()):
                process.blockSignals(True)
                pid = process.processId()
                if pid:
                    core.kill_tree(pid)
                process.kill()
                process.waitForFinished(3000)
        self._save_settings()
        self._save_queue()
        event.accept()


def _brush(colour):
    return QBrush(QColor(colour))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vfx-transcode-process",
                                     description="Convert files or folders with a super-preset, in a queue.")
    parser.add_argument("inputs", nargs="*", help="Files (any frame of a sequence, movies) or folders to search")
    parser.add_argument("-presets_file", default="", help="presets.json to use (default: vfx-transcode.ini, "
                                                          "else the one next to the application)")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setOrganizationName(ORG)
    app.setApplicationName(APP)
    apply_dark_theme(app)
    window = ProcessWindow(args.inputs, args.presets_file)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
