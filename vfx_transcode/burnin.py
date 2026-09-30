"""Burn-in places and styles.

Six places: tl tc tr / bl bc br (top/bottom, left/centre/right; centre is centre-justified).
Each place has a text and a style (color, opacity, size, box). Styles are layered, later
wins field by field:

    defaults < preset style < preset place style < -burn_color/... < -burn_style PLACE:...
"""

import re
from dataclasses import dataclass, fields, replace
from typing import Optional

from .errors import TranscodeError

BURN_PLACES = ("tl", "tc", "tr", "bl", "bc", "br", "wm")
PLACE_NAMES = {"tl": "top left", "tc": "top centre", "tr": "top right",
               "bl": "bottom left", "bc": "bottom centre", "br": "bottom right",
               "wm": "watermark"}
WATERMARK = "wm"
STYLE_FIELDS = ("color", "opacity", "size", "box")

_COLOR_RE = re.compile(r"^(?:[A-Za-z]+|(?:#|0x)[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)$")


@dataclass(frozen=True)
class BurnStyle:
    color: Optional[str] = None      # ffmpeg colour: white, yellow, #ffcc00, 0xffcc00
    opacity: Optional[float] = None  # 1 = solid, 0 = invisible (text, shadow and box)
    size: Optional[str] = None       # "40" pixels, "1.5%" of picture height, or "h/75"
    box: Optional[bool] = None       # black box behind the text

    def over(self, other):
        """This style with the fields `other` sets."""
        if other is None:
            return self
        return replace(self, **{f.name: getattr(other, f.name) for f in fields(other)
                                if getattr(other, f.name) is not None})

    def is_empty(self):
        return all(getattr(self, f) is None for f in STYLE_FIELDS)

    def describe(self):
        return ", ".join("{}={}".format(f, getattr(self, f)) for f in STYLE_FIELDS
                         if getattr(self, f) is not None)


DEFAULT_STYLE = BurnStyle(color="white", opacity=1.0, size="1.33%", box=False)
# The watermark is big and see-through. Its size and opacity come only from these defaults and
# from settings made for "wm" itself: a preset's small text size must not shrink it.
WATERMARK_STYLE = BurnStyle(size="8%", opacity=0.25)
WATERMARK_OWN_FIELDS = ("size", "opacity")

_SIZE_RE = re.compile(r"^\s*(?:(?P<px>\d+(?:\.\d+)?)\s*(?:px)?|(?P<pct>\d+(?:\.\d+)?)\s*%"
                      r"|h\s*/\s*(?P<div>\d+(?:\.\d+)?))\s*$", re.I)


def _num(text):
    value = float(text)
    return str(int(value)) if value == int(value) else "{:g}".format(value)


def parse_size(value, where="size"):
    """Text size: 40 / "40" / "40px" pixels, "1.5%" of the picture height, "h/75" height divided
    by 75 (the older form). Returns the canonical text: "40", "1.5%", "h/75"."""
    if isinstance(value, bool):
        value = None
    text = _num(value) if isinstance(value, (int, float)) else str(value if value is not None else "")
    match = _SIZE_RE.match(text)
    if match:
        kind = next(k for k in ("px", "pct", "div") if match.group(k))
        number = float(match.group(kind))
        if number > 0 and not (kind == "pct" and number > 100):
            n = _num(match.group(kind))
            return {"px": n, "pct": n + "%", "div": "h/" + n}[kind]
    raise TranscodeError('{}: size "{}" must be pixels (40), a percentage of the picture height '
                         '(1.5%) or h/N (h/75).'.format(where, value))


def size_expression(size):
    """drawtext fontsize: "40" -> "40", "1.5%" -> "h*0.015", "h/75" -> "h/75"."""
    if size.endswith("%"):
        return "h*{:g}".format(float(size[:-1]) / 100)
    return size


def size_pixels(size, picture_height):
    """Estimated font size in pixels, for the box padding."""
    if size.endswith("%"):
        return picture_height * float(size[:-1]) / 100
    if size.startswith("h/"):
        return picture_height / float(size[2:])
    return float(size)


def _bool(value, where):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise TranscodeError("{}: box must be true/false or 1/0, got {!r}.".format(where, value))


def style_from_values(values, where):
    """{"color": .., "opacity": .., "size": .., "box": ..} (JSON or command line) -> BurnStyle."""
    out = {}
    if values.get("color") is not None:
        color = str(values["color"]).strip()
        if not _COLOR_RE.match(color):
            raise TranscodeError('{}: color "{}" is not a colour name, #RRGGBB or 0xRRGGBB.'
                                 .format(where, color))
        out["color"] = color
    if values.get("opacity") is not None:
        try:
            opacity = float(values["opacity"])
        except (TypeError, ValueError):
            opacity = -1
        if isinstance(values["opacity"], bool) or not 0 <= opacity <= 1:
            raise TranscodeError("{}: opacity must be a number from 0 to 1.".format(where))
        out["opacity"] = opacity
    if values.get("size") is not None:
        out["size"] = parse_size(values["size"], where)
    if values.get("box") is not None:
        out["box"] = _bool(values["box"], where)
    return BurnStyle(**out)


def parse_style_option(text):
    """-burn_style "tc:color=yellow,box=1" -> (places, BurnStyle). PLACE may be "all"."""
    place, sep, rest = text.partition(":")
    place = place.strip().lower()
    if not sep or not (place in BURN_PLACES or place == "all"):
        raise TranscodeError('-burn_style expects PLACE:key=value,... with PLACE one of {} or all, '
                             'got "{}".'.format(", ".join(BURN_PLACES), text))
    values = {}
    for item in filter(None, (i.strip() for i in rest.split(","))):
        key, sep, value = item.partition("=")
        key = key.strip().lower()
        if not sep or key not in STYLE_FIELDS:
            raise TranscodeError('-burn_style "{}": use {}=VALUE.'.format(text, "/".join(STYLE_FIELDS)))
        values[key] = value.strip()
    places = BURN_PLACES if place == "all" else (place,)
    return places, style_from_values(values, "-burn_style " + text)


# --- margins: empty space between the picture edge and the text ---

MARGIN_SIDES = ("top", "bottom", "left", "right")
MARGIN_FIELDS = ("margin",) + tuple("margin_" + side for side in MARGIN_SIDES)
DEFAULT_MARGINS = {"top": "1.5%", "bottom": "1.5%", "left": "1%", "right": "1%"}


def parse_margin(value, where="margin"):
    """Like a text size, but 0 is allowed: 20 (pixels), "1.5%", "h/75"."""
    if not isinstance(value, bool) and str(value).strip() in ("0", "0.0", "0%", "0px"):
        return "0"
    return parse_size(value, where)


def margin_expression(value, side):
    """drawtext expression: percent of the width for left/right, of the height for top/bottom."""
    if value.endswith("%"):
        return "{}*{:g}".format("w" if side in ("left", "right") else "h", float(value[:-1]) / 100)
    return value


def margins_from_values(values, where):
    """{"margin": .., "margin_top": ..} (JSON or command line) -> {side: canonical value},
    only the sides that are set; "margin" sets all four, a side value wins over it."""
    out = {}
    if values.get("margin") is not None:
        value = parse_margin(values["margin"], where + ": margin")
        out = {side: value for side in MARGIN_SIDES}
    for side in MARGIN_SIDES:
        key = "margin_" + side
        if values.get(key) is not None:
            out[side] = parse_margin(values[key], "{}: {}".format(where, key))
    return out


def resolve_margins(*layers):
    """Defaults, then each layer's sides (later wins)."""
    margins = dict(DEFAULT_MARGINS)
    for layer in layers:
        margins.update(layer or {})
    return margins


def resolve_styles(preset_style=None, preset_places=None, cli_style=None, cli_places=None):
    """Full style for each place, layered (later wins, field by field):
    defaults < preset style < preset place style < command-line style < command-line place style."""
    preset_places = preset_places or {}
    cli_places = cli_places or {}
    styles = {place: DEFAULT_STYLE.over(preset_style).over(preset_places.get(place))
                     .over(cli_style).over(cli_places.get(place))
              for place in BURN_PLACES}
    # watermark: colour and box follow the preset / all-place options, size and opacity don't
    wm = DEFAULT_STYLE.over(_without(preset_style)).over(WATERMARK_STYLE) \
        .over(preset_places.get(WATERMARK)).over(_without(cli_style)).over(cli_places.get(WATERMARK))
    styles[WATERMARK] = wm
    return styles


def _without(style, fields=WATERMARK_OWN_FIELDS):
    return replace(style, **{f: None for f in fields}) if style is not None else None
