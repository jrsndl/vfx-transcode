"""VFX slate: one information frame placed before the first picture frame.

The slate is drawn by ffmpeg (a colour canvas, a thumbnail of the shot and drawtext lines),
so nothing beyond ffmpeg is needed. It is written next to the intermediate frames with the
same size and pixel format as they have, then listed first in the concat list, so the
movie's scaling, pixel aspect reformat and colour tags treat it exactly like a picture frame.

Frame numbering and timecode: the slate is frame start-1 and timecode start-1. A start
timecode of 00:00:00:00 cannot go lower, so the slate gets 00:00:00:00 as well and, since a
movie's timecode runs continuously, the pictures then start at 00:00:00:01.

Anamorphic sources: the slate is laid out at display proportions (width x pixel aspect) and
squeezed to the stored width, so text and thumbnail look right when the player un-squeezes.

Layout ideas follow ynput/LabLib's HTML slate (title, info rows, shot thumbnail, fields whose
value is empty are hidden); LabLib renders HTML with a headless browser, here ffmpeg draws it.
"""

import logging
import re
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional

from .burnin import parse_size, size_pixels
from .errors import TranscodeError
from .timecode import frame_to_tc, offset_timecode, tc_to_frames
from .tools import run_tool

log = logging.getLogger(__name__)

NO_SLATE = "none"
THUMBNAIL_CHOICES = ("middle", "first", "last", "none")
VALIGN_CHOICES = ("center", "top")
SLATE_COLOR_FIELDS = ("background", "title_color", "subtitle_color", "value_color", "label_color",
                      "line_color")
SLATE_SIZE_FIELDS = ("title_size", "subtitle_size", "size")
SLATE_STYLE_FIELDS = SLATE_COLOR_FIELDS + SLATE_SIZE_FIELDS + ("scale", "valign", "rule", "font",
                                                               "color")
SLATE_PRESET_FIELDS = ("notes", "title", "subtitle", "fields", "thumbnail", "hide_empty") \
    + SLATE_STYLE_FIELDS
SLATE_KEYWORDS = ("clean_name", "frame_start", "frame_end", "frames", "timecode", "timecode_end",
                  "duration", "fps", "width", "height", "par", "colour", "reel", "preset",
                  "date", "user", "input", "meta")

MONO_ADVANCE = 0.6      # character width / font size of DroidSansMono; wrapping estimate


@dataclass(frozen=True)
class SlateSpec:
    name: str = ""
    title: str = "{clean_name}"
    subtitle: str = ""
    fields: Dict[str, str] = field(default_factory=OrderedDict)   # label -> template
    thumbnail: str = "middle"          # middle | first | last | none | a frame number
    hide_empty: bool = True            # drop rows whose value comes out empty
    background: str = "#1b1b1b"
    title_color: str = "white"
    subtitle_color: str = "#b4b4b4"
    value_color: str = "white"         # the row values
    label_color: str = "#7a7a7a"       # the row labels (field names)
    line_color: str = "#5a5a5a"        # the rule under the title
    rule: bool = False                 # draw a line under the title
    title_size: str = "5%"             # sizes: % of the picture height, or pixels
    subtitle_size: str = "3.2%"
    size: str = "2.5%"                 # row text size
    scale: float = 1.0                 # whole layout: 0.5 = half size, centred
    valign: str = "center"             # center | top
    font: Optional[str] = None
    burnin: bool = False               # draw the burn-ins on the slate as well
    notes: str = ""


# --- options ---

def parse_scale(value, where="-slate_scale"):
    """ "50%", 50 or 0.5 -> 0.5. Up to 100%: the layout already fills the picture."""
    text = str(value).strip()
    try:
        number = float(text[:-1]) / 100 if text.endswith("%") else float(text)
    except ValueError:
        number = -1
    if not text.endswith("%") and number > 1:
        number /= 100                       # "50" means 50%
    if not 0.05 <= number <= 1:
        raise TranscodeError('{}: use a scale from 5% to 100% (e.g. 50% or 0.5), got "{}".'
                             .format(where, value))
    return number


def style_values(values, where):
    """Slate style settings (JSON preset or command line) -> SlateSpec field values.
    "color" is a shortcut for title_color and value_color."""
    from .burnin import style_from_values        # colour check shared with the burn-ins
    out = {}
    if values.get("color") is not None:
        colour = style_from_values({"color": values["color"]}, where + ": color").color
        out["title_color"] = out["value_color"] = colour
    for key in SLATE_COLOR_FIELDS:
        if values.get(key) is not None:
            out[key] = style_from_values({"color": values[key]}, "{}: {}".format(where, key)).color
    for key in SLATE_SIZE_FIELDS:
        if values.get(key) is not None:
            out[key] = parse_size(values[key], "{}: {}".format(where, key))
    if values.get("scale") is not None:
        out["scale"] = parse_scale(values["scale"], where + ": scale")
    if values.get("valign") is not None:
        out["valign"] = parse_valign(values["valign"], where + ": valign")
    if values.get("rule") is not None:
        out["rule"] = parse_bool(values["rule"], where + ": rule")
    return out


def parse_bool(value, where):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise TranscodeError("{}: use 1/0 or true/false, got {!r}.".format(where, value))


def parse_valign(value, where="-slate_valign"):
    text = str(value).strip().lower()
    text = {"centre": "center", "middle": "center"}.get(text, text)
    if text not in VALIGN_CHOICES:
        raise TranscodeError('{}: use center or top, got "{}".'.format(where, value))
    return text

def parse_thumbnail(value, where="-slate_thumbnail"):
    text = str(value).strip().lower()
    if text in THUMBNAIL_CHOICES or re.fullmatch(r"-?\d+", text):
        return text
    raise TranscodeError('{}: use middle, first, last, none or a frame number, got "{}".'
                         .format(where, value))


def parse_fields(value, where):
    """JSON {"Label": "template"} or [["Label", "template"], ...] -> OrderedDict."""
    pairs = value.items() if isinstance(value, dict) else value
    out = OrderedDict()
    try:
        for label, template in pairs:
            out[str(label)] = "" if template is None else str(template)
    except (TypeError, ValueError):
        raise TranscodeError('{}: "fields" must be an object like {{"Date": "{{date}}"}}.'
                             .format(where))
    return out


def apply_field_options(fields, options):
    """-slate_field LABEL=TEXT: replace or append a row; LABEL= removes it."""
    fields = OrderedDict(fields)
    for item in options:
        label, sep, text = item.partition("=")
        label = label.strip()
        if not sep or not label:
            raise TranscodeError('-slate_field expects LABEL=TEXT, got "{}".'.format(item))
        if text == "":
            fields.pop(label, None)
        else:
            fields[label] = text
    return fields


# --- timecode ---

def slate_timecode(start_tc, fps_int):
    """Timecode of the slate: one frame before the pictures, but never below 00:00:00:00."""
    if tc_to_frames(start_tc, fps_int) <= 0:
        return start_tc
    return offset_timecode(start_tc, -1, fps_int)


# --- probing the frames the slate has to match ---

@dataclass(frozen=True)
class FrameFormat:
    width: int
    height: int
    pix_fmt: str


_STREAM_RE = re.compile(r"Video: .*?, (\w+)(?:\([^)]*\))?, (\d+)x(\d+)")


def parse_probe(text):
    for line in text.splitlines():
        match = _STREAM_RE.search(line)
        if match:
            return FrameFormat(int(match.group(2)), int(match.group(3)), match.group(1))
    return None


def probe_frame(ffmpeg, path, verbose=0):
    """Size and pixel format of an image file, read from `ffmpeg -i` (no ffprobe needed)."""
    result = run_tool([ffmpeg, "-hide_banner", "-i", Path(path).as_posix()], "ffmpeg probe",
                      verbose, check=False)
    fmt = parse_probe((result.stderr or "") + (result.stdout or ""))
    if fmt is None:
        raise TranscodeError("Slate: could not read the size of {}".format(path))
    return fmt


def pick_thumbnail(choice, frames, available):
    """Frame number for the thumbnail, from the frames that exist; None for no thumbnail."""
    have = sorted(f for f in frames if f in available)
    if choice == "none" or not have:
        return None
    if choice == "first":
        return have[0]
    if choice == "last":
        return have[-1]
    if choice == "middle":
        wanted = frames[0] + (frames[-1] - frames[0]) // 2
    else:
        wanted = int(choice)
    nearest = min(have, key=lambda f: (abs(f - wanted), f))
    if choice not in ("middle",) and nearest != wanted:
        log.warning("Slate thumbnail: frame %d not available, using %d", wanted, nearest)
    return nearest


# --- text ---

def expand_all(spec, context, expand):
    """Title, subtitle and rows with keywords filled. Rows that come out empty are dropped
    when spec.hide_empty (e.g. "{meta:Artist|}" when the header has no Artist)."""
    title = expand(spec.title, context).replace("\\n", "\n").strip()
    subtitle = expand(spec.subtitle, context).replace("\\n", "\n").strip()
    rows = []
    for label, template in spec.fields.items():
        value = expand(template, context).replace("\\n", "\n").strip()
        if not value and spec.hide_empty:
            continue
        rows.append((label, value))
    return title, subtitle, rows


def wrap(text, width):
    width = max(8, int(width))
    lines = []
    for paragraph in text.split("\n"):
        lines += textwrap.wrap(paragraph, width, break_long_words=True,
                               break_on_hyphens=False) or [""]
    return lines


# --- layout ---

@dataclass
class TextItem:
    text: str
    x: int
    y: int
    size: int
    color: str
    align_right: bool = False     # x is the right edge (labels)


@dataclass
class SlateLayout:
    width: int                    # canvas, display proportions
    height: int
    texts: List[TextItem]
    rule: tuple                   # x, y, w, h of the line under the title
    thumb: Optional[tuple]        # x, y, w, h


def _size_px(size, height, scale):
    """Text size in pixels: percentages follow the (scaled) height, pixels are scaled."""
    if size.endswith("%") or size.startswith("h/"):
        return size_pixels(size, height)
    return float(size) * scale


def layout(spec, canvas_w, canvas_h, title, subtitle, rows, with_thumbnail):
    """Positions in the display-proportioned canvas.

    The layout is made for a box of scale x the canvas, centred horizontally; vertically the
    content is centred (valign center) or the box sits at the top. Text shrinks if the rows
    do not fit."""
    scale = spec.scale
    vw, vh = canvas_w * scale, canvas_h * scale
    sizes = tuple(_size_px(getattr(spec, f), vh, scale) for f in SLATE_SIZE_FIELDS)
    shrink, result = 1.0, None
    for _attempt in range(15):
        result = _layout(spec, vw, vh, title, subtitle, rows, with_thumbnail,
                         [s * shrink for s in sizes])
        if result is not None or sizes[2] * shrink <= 6:
            break
        shrink *= 0.9
    if result is None:
        log.warning("Slate: too many rows for the picture height; some are cut off.")
        result = _layout(spec, vw, vh, title, subtitle, rows, with_thumbnail,
                         [s * shrink for s in sizes], force=True)
    texts, rule, thumb, top, bottom = result
    ox = (canvas_w - vw) / 2
    if spec.valign == "center":
        oy = (canvas_h - (bottom - top)) / 2 - top
    else:
        oy = 0
    for t in texts:
        t.x, t.y = int(round(t.x + ox)), int(round(t.y + oy))
    move = lambda box: (int(round(box[0] + ox)), int(round(box[1] + oy)), box[2], box[3])
    return SlateLayout(canvas_w, canvas_h, texts, move(rule), move(thumb) if thumb else None)


def _layout(spec, cw, ch, title, subtitle, rows, with_thumbnail, sizes, force=False):
    """Layout in a cw x ch box; returns (texts, rule, thumb, content top, content bottom) or
    None if the rows do not fit. Coordinates are floats until layout() places the box."""
    title_size, sub_size, s = (max(6.0, v) for v in sizes)
    left, right = 0.05 * cw, 0.95 * cw
    top, bottom = 0.08 * ch, 0.93 * ch
    texts = []
    y = top
    for line in wrap(title, (right - left) / (MONO_ADVANCE * title_size)) if title else []:
        texts.append(TextItem(line, left, y, int(round(title_size)), spec.title_color))
        y += title_size * 1.25
    if subtitle:
        y += 0.15 * sub_size
        for line in wrap(subtitle, (right - left) / (MONO_ADVANCE * sub_size)):
            texts.append(TextItem(line, left, y, int(round(sub_size)), spec.subtitle_color))
            y += sub_size * 1.3
    y += 0.5 * s
    rule = (left, y, int(right - left), max(1, int(round(ch / 540))))   # drawn if spec.rule
    y += 1.4 * s
    top_rows = y

    thumb = None
    text_right = right
    if with_thumbnail:
        tw = 0.40 * cw
        th = tw * ch / cw
        room = bottom - top_rows
        if th > room:
            th, tw = room, room * cw / ch
        thumb = (right - tw, top_rows, int(tw) // 2 * 2, int(th) // 2 * 2)
        text_right = right - tw - 1.5 * s

    label_chars = max([len(label) for label, _ in rows] + [4])
    label_right = left + label_chars * MONO_ADVANCE * s
    value_x = label_right + 1.0 * s
    value_chars = (text_right - value_x) / (MONO_ADVANCE * s)
    line_step, row_gap = 1.3 * s, 0.35 * s
    size = int(round(s))
    for label, value in rows:
        lines = wrap(value, value_chars) if value else [""]
        if y + len(lines) * line_step > bottom and not force:
            return None
        texts.append(TextItem(label, label_right, y, size, spec.label_color, True))
        for line in lines:
            texts.append(TextItem(line, value_x, y, size, spec.value_color))
            y += line_step
        y += row_gap
    content_bottom = y - row_gap - (line_step - s) if rows else rule[1] + rule[3]
    if thumb:
        content_bottom = max(content_bottom, thumb[1] + thumb[3])
    return texts, rule, thumb, top, content_bottom


# --- ffmpeg ---

def canvas_size(fmt, par):
    """Display-proportioned canvas for a stored frame size and pixel aspect."""
    width = int(round(fmt.width * float(par)))
    return max(2, width), fmt.height


def build_slate_command(ffmpeg, spec, fmt, par, lay, thumb_path, out_path, font=""):
    from .ffmpeg import escape_text_expansion, make_filter   # ffmpeg.py imports this module
    cw, ch = lay.width, lay.height
    cmd = [ffmpeg, "-hide_banner", "-y", "-f", "lavfi",
           # rgb24 inside the source: left to negotiate, the colour source may pick yuv420p
           # and round an odd height down, and the slate would no longer match the frames
           "-i", "color=c={}:s={}x{}:r=1:d=1,format=rgb24".format(spec.background, cw, ch)]
    graph = []
    current = "[0:v]"
    if lay.thumb and thumb_path:
        cmd += ["-i", Path(thumb_path).as_posix()]
        x, y, w, h = lay.thumb
        graph.append("[1:v]" + ",".join([
            make_filter("scale", [("w", str(w)), ("h", str(h)), ("flags", "bicubic")]),
            make_filter("setsar", [("sar", "1")])]) + "[thumb]")
        graph.append("[0:v][thumb]" + make_filter("overlay", [("x", str(x)), ("y", str(y))])
                     + "[base]")
        current = "[base]"
    chain = []
    if spec.rule:
        rx, ry, rw, rh = lay.rule
        chain.append(make_filter("drawbox", [("x", str(rx)), ("y", str(ry)), ("w", str(rw)),
                                             ("h", str(rh)), ("color", spec.line_color),
                                             ("t", "fill")]))
    for item in lay.texts:
        if not item.text:
            continue
        options = [("fontfile", Path(font).as_posix())] if font else []
        options += [("text", escape_text_expansion(item.text)), ("fontsize", str(item.size)),
                    ("fontcolor", item.color),
                    ("x", "{}-tw".format(item.x) if item.align_right else str(item.x)),
                    ("y", str(item.y))]
        chain.append(make_filter("drawtext", options))
    if (cw, ch) != (fmt.width, fmt.height):
        chain.append(make_filter("scale", [("w", str(fmt.width)), ("h", str(fmt.height)),
                                           ("flags", "bicubic")]))
    chain.append(make_filter("setsar", [("sar", "1")]))
    chain.append(make_filter("format", [("pix_fmts", fmt.pix_fmt)]))
    graph.append(current + ",".join(chain))
    cmd += ["-filter_complex", ";".join(graph), "-frames:v", "1", "-update", "1",
            Path(out_path).as_posix()]
    return cmd


def probe_frames(ffmpeg, frame_files, verbose=0):
    """Format of the frames the slate must match (first frame; warns if the last differs)."""
    first, last = min(frame_files), max(frame_files)
    fmt = probe_frame(ffmpeg, frame_files[first], verbose)
    if last != first:
        other = probe_frame(ffmpeg, frame_files[last], verbose)
        if other != fmt:
            log.warning("Slate: frame %d is %dx%d %s but frame %d is %dx%d %s; the slate "
                        "matches the first.", first, fmt.width, fmt.height, fmt.pix_fmt,
                        last, other.width, other.height, other.pix_fmt)
    return fmt


def movie_size(fmt, par_in, par_out, scale_width):
    """Size of the encoded movie, following build_video_filter: pixel aspect reformat of the
    height, scale to scale_width (0 = keep), both sides padded to even."""
    width, height = fmt.width, fmt.height
    factor = par_out / par_in
    if factor != 1:
        height = int(height * factor / 2) * 2
    if scale_width > 0:
        height = int(round(scale_width * height / width / 2.0)) * 2
        width = scale_width
    return width + width % 2, height + height % 2


def render_slate(cfg, spec, context, expand, fmt, thumb_path, out_path):
    """Draw the slate image in the frames' format `fmt`. Returns out_path."""
    cw, ch = canvas_size(fmt, cfg.par_in)
    title, subtitle, rows = expand_all(spec, context, expand)
    lay = layout(spec, cw, ch, title, subtitle, rows, thumb_path is not None)
    font = spec.font or cfg.burn_font
    if font and not Path(font).is_file():
        log.warning('Slate font "%s" not found, using ffmpeg default font.', font)
        font = ""
    cmd = build_slate_command(cfg.ffmpeg, spec, fmt, cfg.par_in, lay, thumb_path, out_path, font)
    run_tool(cmd, "ffmpeg (slate)", cfg.verbose)
    made = probe_frame(cfg.ffmpeg, out_path, cfg.verbose)
    if (made.width, made.height) != (fmt.width, fmt.height):
        raise TranscodeError("Slate: came out {}x{} instead of {}x{}.".format(
            made.width, made.height, fmt.width, fmt.height))
    if made.pix_fmt != fmt.pix_fmt:
        log.warning("Slate: pixel format %s, the frames are %s.", made.pix_fmt, fmt.pix_fmt)
    return Path(out_path)
