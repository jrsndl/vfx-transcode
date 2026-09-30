"""Letterbox / pillarbox: bars that cover part of the picture to show a target aspect ratio.

The bars are computed from the movie's display ratio (width x pixel aspect / height):
a target ratio wider than the picture gives bars top and bottom (letterbox), a narrower one
gives bars left and right (pillarbox). They are drawn after scaling and before the burn-ins,
and not on the slate.
"""

import logging
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import List, Tuple

from .errors import TranscodeError

log = logging.getLogger(__name__)

DEFAULT_RATIO = "2.39"
DEFAULT_FILL = (0, 0, 0, 255)
DEFAULT_LINE_COLOR = (255, 255, 255, 255)


@dataclass(frozen=True)
class LetterboxSpec:
    ratio: float
    fill: Tuple[int, int, int, int] = DEFAULT_FILL
    line: int = 0                                  # line thickness in pixels, 0 = no line
    line_color: Tuple[int, int, int, int] = DEFAULT_LINE_COLOR
    ratio_text: str = DEFAULT_RATIO


@dataclass(frozen=True)
class Box:
    x: str                 # drawbox expressions; iw/ih keep the far bars on the real edge
    y: str
    w: str
    h: str
    color: str             # ffmpeg colour, 0xRRGGBB@alpha


@dataclass(frozen=True)
class LetterboxPlan:
    kind: str              # "letterbox", "pillarbox" or "none"
    bar: int               # bar size in pixels (height for letterbox, width for pillarbox)
    boxes: List[Box]
    width: int = 0         # the movie size the plan was made for
    height: int = 0


_RATIO_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:[:/]\s*(\d+(?:\.\d+)?))?\s*$")


def parse_ratio(value, where="-letterbox_ratio"):
    """2.39, 2.39:1, 1.85, 16:9, 16/9, 239/100 -> float."""
    match = _RATIO_RE.match(str(value))
    if match:
        num = float(match.group(1))
        den = float(match.group(2)) if match.group(2) else 1.0
        if num > 0 and den > 0:
            return num / den
    raise TranscodeError('{}: use a ratio like 2.39, 2.39:1, 1.85 or 16:9, got "{}".'
                         .format(where, value))


def parse_rgba(value, where):
    """ "R,G,B,A" or "R,G,B" with 0-255 values (alpha 255 = solid), or #RRGGBB[AA]."""
    text = str(value).strip()
    parts = None
    if re.fullmatch(r"(?:#|0x)[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", text):
        digits = text[1:] if text.startswith("#") else text[2:]
        parts = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
    else:
        try:
            parts = [int(float(p)) for p in re.split(r"[,\s]+", text) if p]
        except ValueError:
            parts = None
    if parts and len(parts) in (3, 4) and all(0 <= p <= 255 for p in parts):
        return tuple(parts + [255] * (4 - len(parts)))
    raise TranscodeError('{}: use R,G,B,A with values 0-255 (e.g. 0,0,0,128) or #RRGGBBAA, '
                         'got "{}".'.format(where, value))


def ffmpeg_color(rgba):
    r, g, b, a = rgba
    return "0x{:02X}{:02X}{:02X}@{:.3g}".format(r, g, b, a / 255)


def plan(spec, width, height, par=Fraction(1)):
    """Bars for a movie of width x height stored pixels with pixel aspect par."""
    display = width * float(par) / height
    boxes = []
    if abs(spec.ratio - display) * height < 1:        # less than a pixel: nothing to cover
        return LetterboxPlan("none", 0, boxes)
    fill = ffmpeg_color(spec.fill)
    line_color = ffmpeg_color(spec.line_color)
    if spec.ratio > display:
        visible = width * float(par) / spec.ratio
        bar = int(round((height - visible) / 2))
        kind = "letterbox"
        boxes += [Box("0", "0", "iw", str(bar), fill),
                  Box("0", "ih-{}".format(bar), "iw", str(bar), fill)]
        line = min(spec.line, bar)
        if line > 0:     # on the inner edge of each bar, next to the picture
            boxes += [Box("0", str(bar - line), "iw", str(line), line_color),
                      Box("0", "ih-{}".format(bar), "iw", str(line), line_color)]
    else:
        visible = height * spec.ratio / float(par)
        bar = int(round((width - visible) / 2))
        kind = "pillarbox"
        boxes += [Box("0", "0", str(bar), "ih", fill),
                  Box("iw-{}".format(bar), "0", str(bar), "ih", fill)]
        line = min(spec.line, bar)
        if line > 0:
            boxes += [Box(str(bar - line), "0", str(line), "ih", line_color),
                      Box("iw-{}".format(bar), "0", str(line), "ih", line_color)]
    if bar <= 0:
        return LetterboxPlan("none", 0, [])
    return LetterboxPlan(kind, bar, boxes, width, height)


def filters(letterbox_plan, skip_frames=0, alpha=False):
    """drawbox filters for the plan; skip_frames leading frames (the slate) are left alone.
    alpha: the movie keeps an alpha channel. drawbox leaves alpha as it is, so bars over
    transparent parts would stay invisible; the bars are then composited with overlay."""
    from .ffmpeg import make_filter        # ffmpeg.py imports this module
    if alpha and letterbox_plan.boxes:
        return [_overlay_graph(letterbox_plan, skip_frames, make_filter)]
    out = []
    for box in letterbox_plan.boxes:
        options = [("x", box.x), ("y", box.y), ("w", box.w), ("h", box.h),
                   ("color", box.color), ("t", "fill")]
        if skip_frames:
            options.append(("enable", "gte(n,{})".format(skip_frames)))
        out.append(make_filter("drawbox", options))
    return out


ALPHA_WORK_FORMAT = "yuva444p10le"


def _overlay_graph(letterbox_plan, skip_frames, make_filter):
    """One filtergraph piece: the main chain ends in [lbm0], each bar is a colour source
    overlaid on it; the last overlay has no output label, so the chain continues after it.
    Bar sources are a little larger than needed: the far bars are placed from the real edge
    (main_w / main_h), and whatever sticks out is clipped."""
    def size(value, full):
        return full + 16 if value in ("iw", "ih") else int(value)

    def position(value):
        return value.replace("iw", "main_w").replace("ih", "main_h")

    parts = [make_filter("format", [("pix_fmts", ALPHA_WORK_FORMAT)]) + "[lbm0]"]
    count = len(letterbox_plan.boxes)
    for i, box in enumerate(letterbox_plan.boxes):
        w = size(box.w, letterbox_plan.width)
        h = size(box.h, letterbox_plan.height)
        parts.append(",".join([
            make_filter("color", [("c", box.color), ("s", "{}x{}".format(w, h))]),
            make_filter("format", [("pix_fmts", "rgba")]),
            make_filter("scale", [("out_color_matrix", "bt709"), ("out_range", "tv")]),
            make_filter("format", [("pix_fmts", ALPHA_WORK_FORMAT)])]) + "[lbs{}]".format(i))
        options = [("x", position(box.x)), ("y", position(box.y)), ("shortest", "1")]
        if skip_frames:
            options.append(("enable", "gte(n,{})".format(skip_frames)))
        out = "" if i == count - 1 else "[lbm{}]".format(i + 1)
        parts.append("[lbm{}][lbs{}]".format(i, i) + make_filter("overlay", options) + out)
    return ";".join(parts)
