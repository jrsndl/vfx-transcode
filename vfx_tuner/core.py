"""Tuner logic without Qt: control state, loading presets into it, and turning it into
vfx-transcode command lines and presets.json snippets. The Qt window (app.py) only shows
and edits these objects."""

import json
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from vfx_transcode import metadata as md
from vfx_transcode.burnin import (BURN_PLACES, DEFAULT_MARGINS, DEFAULT_STYLE, MARGIN_SIDES,
                                  WATERMARK, WATERMARK_STYLE)
from vfx_transcode.slate import SlateSpec
from vfx_transcode.tools import split_args
from vfx_transcode.transforms import select as select_sub

REPO = Path(__file__).resolve().parent.parent


def quote(args):
    """A command line to show and copy. Windows quoting (the tool's main platform)."""
    return subprocess.list2cmdline([str(a) for a in args])


def vfx_transcode_command():
    """How to start vfx-transcode: the .exe next to a frozen tuner, else this Python + script."""
    if getattr(sys, "frozen", False):
        # a frozen tuner has no Python to run the script with; if the .exe is missing the run
        # fails with this path in the error, which says what to put back
        return [str(Path(sys.executable).with_name("vfx-transcode.exe"))]
    return [sys.executable, str(REPO / "vfx-transcode.py")]


# --- state ----------------------------------------------------------------------------------

@dataclass
class InputState:
    input1: str = ""
    input2: str = ""                  # compare image, optional
    frame: str = "middle"             # middle | first | last | a frame number
    presets_file: str = ""            # "" = the default ./presets.json
    encode_preset: str = ""           # "" = the file's default
    colorin: str = ""
    colorout: str = ""
    display: str = ""
    view: str = ""
    looks: str = ""
    clf: str = ""
    ocio: str = ""
    extra: str = ""                   # any other vfx-transcode options

    def args(self):
        out = ["-i", self.input1]
        pairs = (("-presets_file", self.presets_file), ("-preset", self.encode_preset),
                 ("-ci", self.colorin), ("-co", self.colorout), ("-display", self.display),
                 ("-view", self.view), ("-looks", self.looks), ("-clf", self.clf),
                 ("-ocio", self.ocio))
        for option, value in pairs:
            if value.strip():
                out += [option, value.strip()]
        return out + split_args(self.extra)


@dataclass
class TransformState:
    preset: str = ""                  # the loaded preset (for the output), "" = none
    sub: str = ""                     # the loaded sub-preset
    args: List[str] = field(default_factory=list)   # one oiiotool step per entry
    loaded_args: List[str] = field(default_factory=list)
    width: int = 1920                 # final ffmpeg resize (-width); 0 = keep
    use: bool = False                 # False: no transform, the usual -width resize

    def oiio_args(self):
        out = []
        for entry in self.args:
            out += split_args(entry)
        return out

    def preview_args(self):
        """What the preview runs: the edited steps as they are, even for a sub-preset that
        would not be chosen for this input."""
        out = ["-width", str(self.width)]
        if self.use:
            out = ["-oiio_transform_args", " ".join(quote([a]) for a in self.oiio_args())] + out
        return out

    def cli_args(self, active_sub=None):
        """For the output box: the preset name when the loaded sub-preset is unchanged and is
        the one vfx-transcode will pick for this input; else the steps written out."""
        out = ["-width", str(self.width)]
        if not self.use:
            return out
        if (self.preset and self.sub and self.sub == active_sub
                and list(self.args) == list(self.loaded_args)):
            return ["-oiio_transform", self.preset] + out
        return ["-oiio_transform_args", " ".join(quote([a]) for a in self.oiio_args())] + out


def load_transform(presets, name, sub_name, width=1920):
    preset = presets.transforms[name]
    sub = next(s for s in preset.subs if s.name == sub_name)
    return TransformState(preset=name, sub=sub_name, args=list(sub.transforms),
                          loaded_args=list(sub.transforms), width=width, use=True)


def active_sub_name(presets, name, keywords, header):
    """Which sub-preset vfx-transcode picks for the previewed input, or None."""
    if not name or name not in presets.transforms:
        return None
    context = dict(keywords or {})
    context["meta"] = header or {}
    sub, _details = select_sub(presets.transforms[name], context, md.expand)
    return sub.name if sub else None


def transform_json(presets, state):
    """The edited sub-preset as it would go into presets.json."""
    conditions = []
    if state.preset in presets.transforms:
        sub = next((s for s in presets.transforms[state.preset].subs if s.name == state.sub), None)
        if sub:
            conditions = [{"left": c.left, "op": c.op, "right": c.right} for c in sub.conditions]
    return OrderedDict([("name", state.sub or "tuned"), ("conditions", conditions),
                        ("transforms", list(state.args))])


# burn-ins

STYLE_KEYS = ("color", "opacity", "size", "box")


@dataclass
class PlaceState:
    text: str = ""
    own_style: bool = False           # use the values below instead of the preset-wide ones
    color: str = "white"
    opacity: float = 1.0
    size: str = "1.33%"
    box: bool = False


def _default_places():
    places = OrderedDict((p, PlaceState()) for p in BURN_PLACES)
    places[WATERMARK] = PlaceState(own_style=True, size=WATERMARK_STYLE.size,
                                   opacity=WATERMARK_STYLE.opacity)
    return places


@dataclass
class BurnState:
    preset: str = ""                  # loaded preset, "" = none
    places: Dict[str, PlaceState] = field(default_factory=lambda: _default_places())
    color: str = "white"
    opacity: float = 1.0
    size: str = "1.33%"
    box: bool = False
    margins: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MARGINS))
    font: str = ""

    def any_text(self):
        return any(p.text for p in self.places.values())


def burn_from_preset(preset):
    state = BurnState(preset=preset.name if preset else "")
    if preset is None:
        return state
    style = DEFAULT_STYLE.over(preset.style)
    state.color, state.opacity, state.size, state.box = (style.color, style.opacity, style.size,
                                                          style.box)
    for place in BURN_PLACES:
        own = preset.place_styles.get(place)
        base = style.over(WATERMARK_STYLE) if place == WATERMARK else style
        full = base.over(own)
        state.places[place] = PlaceState(text=preset.corners.get(place, ""),
                                         own_style=bool(own and not own.is_empty()) or place == WATERMARK,
                                         color=full.color, opacity=full.opacity, size=full.size,
                                         box=full.box)
    state.margins = dict(DEFAULT_MARGINS, **preset.margins)
    state.font = preset.font or ""
    return state


def _num(value):
    return "{:g}".format(float(value))


def burn_args(state, presets=None):
    """-burn_* options giving exactly this state (the preset name alone when unchanged)."""
    if presets is not None and state.preset and state.preset in presets.burnins:
        if burn_from_preset(presets.burnins[state.preset]) == state:
            return ["-burn_preset", state.preset]
    out = ["-burn_preset", "none"]
    for place, p in state.places.items():
        if p.text:
            out += ["-burn_" + place, p.text]
    if not state.any_text():
        return out
    out += ["-burn_color", state.color, "-burn_opacity", _num(state.opacity),
            "-burn_size", state.size, "-burn_box", "1" if state.box else "0"]
    for place, p in state.places.items():
        if p.text and p.own_style:
            out += ["-burn_style", "{}:color={},opacity={},size={},box={}".format(
                place, p.color, _num(p.opacity), p.size, 1 if p.box else 0)]
    for side in MARGIN_SIDES:
        if state.margins.get(side) != DEFAULT_MARGINS[side]:
            out += ["-burn_margin_" + side, state.margins[side]]
    if state.font:
        out += ["-burn_font", state.font]
    return out


def burn_json(state):
    data = OrderedDict([("notes", "tuned")])
    data.update([("color", state.color), ("opacity", state.opacity), ("size", state.size),
                 ("box", state.box)])
    for place, p in state.places.items():
        if not p.text:
            continue
        if p.own_style:
            data[place] = OrderedDict([("text", p.text), ("color", p.color), ("opacity", p.opacity),
                                       ("size", p.size), ("box", p.box)])
        else:
            data[place] = p.text
    for side in MARGIN_SIDES:
        if state.margins.get(side) != DEFAULT_MARGINS[side]:
            data["margin_" + side] = state.margins[side]
    if state.font:
        data["font"] = state.font
    return data


# slate

@dataclass
class SlateState:
    enabled: bool = False
    preset: str = ""
    title: str = "{clean_name}"
    subtitle: str = ""
    fields: List[Tuple[str, str]] = field(default_factory=list)
    thumbnail: str = "middle"
    hide_empty: bool = True
    background: str = "#1b1b1b"
    title_color: str = "white"
    subtitle_color: str = "#b4b4b4"
    value_color: str = "white"
    label_color: str = "#7a7a7a"
    line_color: str = "#5a5a5a"
    title_size: str = "5%"
    subtitle_size: str = "3.2%"
    size: str = "2.5%"
    scale: float = 1.0
    valign: str = "center"
    rule: bool = False
    burnin: bool = False


SLATE_SPEC_KEYS = ("title", "subtitle", "thumbnail", "hide_empty", "background", "title_color",
                   "subtitle_color", "value_color", "label_color", "line_color", "title_size",
                   "subtitle_size", "size", "scale", "valign", "rule")


def slate_from_spec(spec, name=""):
    spec = spec or SlateSpec()
    values = {k: getattr(spec, k) for k in SLATE_SPEC_KEYS}
    return SlateState(enabled=True, preset=name, fields=list(spec.fields.items()),
                      burnin=spec.burnin, **values)


def slate_args(state, presets=None):
    if not state.enabled:
        return ["-slate", "none"]
    if presets is not None and state.preset in presets.slates:
        if slate_from_spec(presets.slates[state.preset], state.preset) == state:
            return ["-slate", state.preset]
    out = ["-slate", "plain", "-slate_title", state.title]
    if state.subtitle:
        out += ["-slate_subtitle", state.subtitle]
    for label, value in state.fields:
        if label.strip() and value != "":
            out += ["-slate_field", "{}={}".format(label.strip(), value)]
    out += ["-slate_thumbnail", state.thumbnail,
            "-slate_title_size", state.title_size, "-slate_subtitle_size", state.subtitle_size,
            "-slate_size", state.size, "-slate_value_color", state.value_color,
            "-slate_label_color", state.label_color,
            "-slate_style", ",".join([
                "background=" + state.background, "title_color=" + state.title_color,
                "subtitle_color=" + state.subtitle_color, "line_color=" + state.line_color,
                "scale=" + _num(state.scale), "valign=" + state.valign,
                "rule=" + ("1" if state.rule else "0"),
                "hide_empty=" + ("1" if state.hide_empty else "0")])]
    if state.burnin:
        out += ["-slate_burnin", "1"]
    return out


def slate_json(state):
    data = OrderedDict([("notes", "tuned"), ("title", state.title), ("subtitle", state.subtitle),
                        ("fields", OrderedDict((l, v) for l, v in state.fields if l.strip()))])
    for key in SLATE_SPEC_KEYS[2:]:
        data[key] = getattr(state, key)
    return data


# letterbox

@dataclass
class LetterboxState:
    enabled: bool = False
    ratio: str = "2.39"
    color: str = "#000000"
    opacity: float = 1.0
    line: int = 0
    line_color: str = "#ffffff"
    line_opacity: float = 1.0


def _rgba(color, opacity):
    color = color.lstrip("#")
    return "{},{},{},{}".format(int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16),
                                int(round(float(opacity) * 255)))


def letterbox_args(state):
    if not state.enabled:
        return []
    out = ["-letterbox", "1", "-letterbox_ratio", state.ratio,
           "-letterbox_color", _rgba(state.color, state.opacity)]
    if state.line > 0:
        out += ["-letterbox_line", str(state.line),
                "-letterbox_line_color", _rgba(state.line_color, state.line_opacity)]
    return out


# metadata

@dataclass
class MetadataState:
    preset: str = ""                  # "" = the file's default
    tags: List[Tuple[str, str]] = field(default_factory=list)
    timecode_source: str = ""         # "" = preset / default
    timecode: str = ""
    reelid_source: str = ""
    reelid: str = ""


def metadata_args(state):
    out = []
    if state.preset:
        out += ["-metadata_preset", state.preset]
    for key, value in state.tags:
        if key.strip():
            out += ["-metadata", "{}={}".format(key.strip(), value)]
    for option, value in (("-timecode_source", state.timecode_source), ("-timecode", state.timecode),
                          ("-reelid_source", state.reelid_source), ("-reelid", state.reelid)):
        if value.strip():
            out += [option, value.strip()]
    return out


# --- commands -----------------------------------------------------------------------------

@dataclass
class TunerState:
    input: InputState = field(default_factory=InputState)
    transform: TransformState = field(default_factory=TransformState)
    burn: BurnState = field(default_factory=BurnState)
    slate: SlateState = field(default_factory=SlateState)
    letterbox: LetterboxState = field(default_factory=LetterboxState)
    metadata: MetadataState = field(default_factory=MetadataState)


def preview_command(state, preview_path, tool=None):
    """The command the tuner runs for the preview image."""
    s = state
    return ((tool or vfx_transcode_command()) + s.input.args()
            + (["-thumbnail_frame", s.input.frame] if s.input.frame else [])
            + s.transform.preview_args() + burn_args(s.burn) + slate_args(s.slate)
            + letterbox_args(s.letterbox) + metadata_args(s.metadata)
            + ["-preview", str(preview_path)])


def output_sections(state, presets, active_sub=None):
    """{section: (command-line text, presets.json text or "")} for the copy boxes, plus the
    full command for the movie."""
    s = state
    sections = OrderedDict()
    sections["transform"] = (quote(s.transform.cli_args(active_sub)),
                             json.dumps(transform_json(presets, s.transform), indent=2)
                             if s.transform.use else "")
    sections["burnin"] = (quote(burn_args(s.burn, presets)),
                          json.dumps(burn_json(s.burn), indent=2) if s.burn.any_text() else "")
    sections["slate"] = (quote(slate_args(s.slate, presets)),
                         json.dumps(slate_json(s.slate), indent=2) if s.slate.enabled else "")
    sections["letterbox"] = (quote(letterbox_args(s.letterbox)), "")
    sections["metadata"] = (quote(metadata_args(s.metadata)), "")
    full = (["vfx-transcode"] + s.input.args() + s.transform.cli_args(active_sub)
            + burn_args(s.burn, presets) + slate_args(s.slate, presets)
            + letterbox_args(s.letterbox) + metadata_args(s.metadata) + ["-o", "./"])
    return quote(full), sections


# --- metadata viewer ----------------------------------------------------------------------

def metadata_rows(report):
    """[(section, key, value)] from the -preview JSON."""
    rows = []
    if not report:
        return rows
    inp = report.get("input", {})
    rows.append(("Input", "first file", inp.get("first_file", "")))
    rows.append(("Input", "frames", inp.get("frames", "")))
    rows.append(("Input", "previewed frame", "{} ({})".format(report.get("frame"),
                                                              report.get("frame_file", ""))))
    rows.append(("Input", "pixel aspect", inp.get("par", "")))
    for key, value in sorted((inp.get("header") or {}).items(), key=lambda kv: kv[0].lower()):
        rows.append(("Input header", key, _text(value)))
    out = report.get("output", {})
    for key in ("width", "height", "par", "pix_fmt", "fps", "timecode", "movie_timecode",
                "reel_name", "colour", "transform", "encode_preset"):
        if out.get(key) not in (None, ""):
            rows.append(("Output", key, _text(out[key])))
    if out.get("transform_args"):
        rows.append(("Output", "transform steps", " ".join(out["transform_args"])))
    if out.get("letterbox"):
        rows.append(("Output", "letterbox", "{kind}, bars {bar} px".format(**out["letterbox"])))
    for key, value in (out.get("metadata") or {}).items():
        rows.append(("Output metadata", key, _text(value)))
    for key, value in sorted((report.get("keywords") or {}).items()):
        rows.append(("Keywords", "{" + key + "}", _text(value)))
    return rows


def filter_rows(rows, text):
    """Rows whose key or value contains text (case-insensitive); a section whose name matches
    keeps all its rows. Empty text: all rows."""
    needle = (text or "").strip().lower()
    if not needle:
        return list(rows)
    return [r for r in rows if needle in r[0].lower() or needle in r[1].lower()
            or needle in r[2].lower()]


def _text(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)
