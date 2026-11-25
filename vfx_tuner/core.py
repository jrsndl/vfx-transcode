"""Tuner logic without Qt: control state, loading presets into it, and turning it into
vfx-transcode command lines and presets.json snippets. The Qt window (app.py) only shows
and edits these objects."""

import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, fields, replace
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from vfx_transcode import metadata as md
from vfx_transcode import movie as mv
from vfx_transcode import super_presets as sp
from vfx_transcode.errors import TranscodeError
from vfx_transcode.burnin import (BURN_PLACES, DEFAULT_MARGINS, DEFAULT_STYLE, MARGIN_SIDES,
                                  WATERMARK, WATERMARK_STYLE)
from vfx_transcode.filmstrip import SOURCES as FILMSTRIP_SOURCES, FilmstripPreset
from vfx_transcode.slate import SlateSpec
from vfx_transcode.thumbnails import ThumbnailPreset
from vfx_transcode.sequence import find_sequence, format_frames
from vfx_transcode.timecode import (fps_display, fps_from_attribs, frame_to_tc, nominal_fps,
                                    offset_timecode, parse_fps)
from vfx_transcode.tools import TOOLS, app_dir, resolve_path, resolve_tool, split_args
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

# the Color tab's starting values: the ACES 1.2 config in the application's ocio folder
DEFAULT_COLOROUT = "Output - Rec.709"
DEFAULT_OCIO = "./ocio/aces_1.2cg/config.ocio"


@dataclass
class InputState:
    input1: str = ""
    input2: str = ""                  # compare to: an image, a movie or a sequence, optional
    compare_source: bool = True       # compare to the unmodified source (input1) instead
    frame: str = "middle"             # middle | first | last | a frame number
    presets_file: str = ""            # "" = the default ./presets.json
    encode_preset: str = ""           # "" = the file's default
    colorin: str = ""                 # "" = vfx-transcode's default (ACEScg)
    colorout: str = DEFAULT_COLOROUT  # display-referred output colourspace
    display: str = ""
    view: str = ""
    looks: str = ""
    clf: str = ""
    ocio: str = DEFAULT_OCIO
    convert_colour: bool = False      # EXR -> EXR, other input through oiiotool: convert too
    fps: str = ""                     # "" = auto: the EXR header / the movie's rate, else 24
    par: str = ""                     # "" = auto: the EXR header / the movie's SAR, else 1
    movie_first_frame: int = mv.DEFAULT_FIRST_FRAME
    trim_in: Optional[int] = None     # -in
    trim_out: Optional[int] = None    # -out
    output_type: str = "movie"        # movie | exr | png | jpg | tif | dpx (an image sequence)
    input_type: str = "exr"           # movie | exr | png | jpg | tif | dpx: set from input1 when
                                      # there is one, chosen by hand without (super-presets)
    seq_preset: str = ""              # sequence preset; "" = the file's default for the format
    colour_preset: str = ""           # the colour preset last loaded / saved (display only)
    alpha: str = "drop"               # drop | auto: keep the input's alpha when the output can
    write_timecode: bool = True       # -output_timecode (movie output)
    write_reelid: bool = True         # -output_reelid (movie output)
    output_fps: str = ""              # -output_fps: play the frames at this rate; "" = the input's
    missing_frames: str = "hold"      # -missing_frames: hold | error
    par_out: str = "same"             # -par_out: same | square | a pixel aspect
    alpha_premult: bool = False       # -alpha_premult (with alpha kept)
    extra: str = ""                   # any other vfx-transcode options
    def args(self):
        out = ["-i", self.input1]
        if self.trim_in is not None:
            out += ["-in", str(self.trim_in)]
        if self.trim_out is not None:
            out += ["-out", str(self.trim_out)]
        if self.fps.strip():
            out += ["-r", self.fps.strip()]
        if self.par.strip():
            out += ["-par", self.par.strip()]
        if self.movie_first_frame != mv.DEFAULT_FIRST_FRAME and mv.is_movie(self.input1):
            out += ["-movie_first_frame", str(self.movie_first_frame)]
        for option, value in (("-presets_file", self.presets_file), ("-preset", self.encode_preset),
                              ("-output_fps", self.output_fps)):
            if value.strip():
                out += [option, value.strip()]
        if self.missing_frames == "error":
            out += ["-missing_frames", "error"]
        if (self.par_out or "same").strip().lower() != "same":
            out += ["-par_out", self.par_out.strip()]
        return out + split_args(self.extra)

    def colour_args(self):
        """The Color tab's options (-ci, -co, -display, -view, -looks, -clf, -ocio)."""
        out = []
        for option, value in (("-ci", self.colorin), ("-co", self.colorout),
                              ("-display", self.display), ("-view", self.view),
                              ("-looks", self.looks), ("-clf", self.clf), ("-ocio", self.ocio)):
            if value.strip():
                out += [option, value.strip()]
        return out


@dataclass
class TransformState:
    preset: str = ""                  # the loaded preset (for the output), "" = none
    sub: str = ""                     # the loaded sub-preset
    args: List[str] = field(default_factory=list)   # one oiiotool step per entry
    loaded_args: List[str] = field(default_factory=list)
    use: bool = False                 # False: no oiiotool steps (the tuner sends -width 0)

    def oiio_args(self):
        out = []
        for entry in self.args:
            out += split_args(entry)
        return out

    def preview_args(self):
        """What the preview runs: the edited steps as they are, even for a sub-preset that
        would not be chosen for this input."""
        if not self.use:
            return []
        return ["-oiio_transform_args", " ".join(quote([a]) for a in self.oiio_args())]

    def cli_args(self, active_sub=None):
        """For the output box: the preset name when the loaded sub-preset is unchanged and is
        the one vfx-transcode will pick for this input; else the steps written out."""
        if not self.use:
            return []
        if (self.preset and self.sub and self.sub == active_sub
                and list(self.args) == list(self.loaded_args)):
            return ["-oiio_transform", self.preset]
        return self.preview_args()


def load_transform(presets, name, sub_name, width=None):
    preset = presets.transforms[name]
    sub = next(s for s in preset.subs if s.name == sub_name)
    return TransformState(preset=name, sub=sub_name, args=list(sub.transforms),
                          loaded_args=list(sub.transforms), use=True)


@dataclass
class FFTransformState:
    """The ffmpeg transform: scale / zscale filters, one per line (-ff_transform)."""
    text: str = ""
    preset: str = ""                  # the loaded / saved ff_transforms preset, "" = none

    def filters(self):
        lines = [l.strip() for l in self.text.replace("\r", "").split("\n")]
        return ",".join(l.rstrip(",") for l in lines if l and not l.startswith("#"))

    def args(self):
        chain = self.filters()
        return ["-ff_transform", chain] if chain else []


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
    enabled: bool = True              # off: -burn_preset none, nothing drawn
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
    if not state.enabled:
        return ["-burn_preset", "none"]
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
    logo: str = ""                    # vendor logo image (PNG with alpha), "" = none
    logo_position: str = "tr"
    logo_size: str = "10%"
    logo_x: str = "4%"
    logo_y: str = "5%"


SLATE_SPEC_KEYS = ("title", "subtitle", "thumbnail", "hide_empty", "background", "title_color",
                   "subtitle_color", "value_color", "label_color", "line_color", "title_size",
                   "subtitle_size", "size", "scale", "valign", "rule", "logo", "logo_position",
                   "logo_size", "logo_x", "logo_y")
LOGO_POSITION_NAMES = OrderedDict([("tl", "top left"), ("tc", "top centre"), ("tr", "top right"),
                                   ("ml", "middle left"), ("mc", "centre"), ("mr", "middle right"),
                                   ("bl", "bottom left"), ("bc", "bottom centre"),
                                   ("br", "bottom right")])


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
    if state.logo.strip():
        out += ["-slate_logo", state.logo.strip(), "-slate_logo_position", state.logo_position,
                "-slate_logo_size", state.logo_size, "-slate_logo_x", state.logo_x,
                "-slate_logo_y", state.logo_y]
    if state.burnin:
        out += ["-slate_burnin", "1"]
    return out


def slate_json(state):
    data = OrderedDict([("notes", "tuned"), ("title", state.title), ("subtitle", state.subtitle),
                        ("fields", OrderedDict((l, v) for l, v in state.fields if l.strip()))])
    for key in SLATE_SPEC_KEYS[2:]:
        data[key] = getattr(state, key)
    return data


# filmstrip

VIEW_MODES = ("image", "slate", "filmstrip")


@dataclass
class FilmstripState:
    enabled: bool = False             # add -of to the full command
    preset: str = ""                  # the loaded preset, "" = none
    tiles: int = 24
    tile_width: int = 480
    tile_height: int = 270
    columns: int = 0                  # 0 = follow the tiles (both 0: one row)
    rows: int = 0                     # 0 = as many as needed
    quality: int = 90
    source: str = "source"            # source | intermediates | movie
    slate: bool = False

    def preset_object(self):
        return FilmstripPreset(name=self.preset or "tuned", tiles=self.tiles,
                               tile_width=self.tile_width, tile_height=self.tile_height,
                               columns=self.columns, rows=self.rows, quality=self.quality,
                               source=self.source, slate=self.slate)

    def needs_movie(self):
        """The tiles come from the movie run: the movie or its intermediates, or the slate."""
        return self.source != "source" or self.slate


def filmstrip_from_preset(preset, name="", enabled=False):
    preset = preset or FilmstripPreset(name="")
    return FilmstripState(enabled=enabled, preset=name, tiles=preset.layout()[0],
                          tile_width=preset.tile_width, tile_height=preset.tile_height,
                          columns=preset.columns, rows=preset.rows, quality=preset.quality,
                          source=preset.source, slate=preset.slate)


def filmstrip_args(state, presets=None):
    """-filmstrip_* options giving exactly this state (the preset name alone when unchanged).
    Without -of in front; the caller adds it."""
    if presets is not None and state.preset in presets.filmstrips:
        loaded = filmstrip_from_preset(presets.filmstrips[state.preset], state.preset, state.enabled)
        if loaded == state:
            return ["-filmstrip_preset", state.preset]
    columns, rows = state.columns, state.rows
    if not columns and not rows:
        columns, rows = state.tiles, 1                # explicit, whatever the file's default is
    grid = "{}x{}".format(columns or "", rows or "")
    return ["-filmstrip_tiles", str(state.tiles), "-filmstrip_grid", grid,
            "-filmstrip_tile", "{}x{}".format(state.tile_width, state.tile_height),
            "-filmstrip_quality", str(state.quality), "-filmstrip_source", state.source,
            "-filmstrip_slate", "1" if state.slate else "0"]


# EXR channels (Multichannel tab): which part and channels become the picture

CHANNEL_MODES = OrderedDict([
    ("auto", "Automatic: the first set of the presets file's channel_preferences found"),
    ("first", "The first layer (-oiio_channel_use_first)"),
    ("pick", "These channels (-oiio_channel)"),
])


@dataclass
class ChannelsState:
    mode: str = "auto"                # auto | first | pick
    subimage: str = ""                # -oiio_subimage: a part's number or name; "" = all parts
    channels: str = ""                # -oiio_channel with mode pick: "diffuse.R,diffuse.G,diffuse.B"
    # the automatic search's order: sets like "rgba.R,rgba.G,rgba.B"; [] = the presets file's
    order: List[str] = field(default_factory=list)
    order_preset: str = ""            # the channel preference preset loaded / saved
    order_loaded: List[str] = field(default_factory=list)    # ... as it was loaded
    deep_zrange: str = ""             # -deep_zrange "near,far" for deep EXRs shown as depth

    def order_text(self):
        return "; ".join(s.strip() for s in self.order if s.strip())

    def order_sets(self, presets=None):
        """The sets the automatic search tries, in order."""
        if self.order:
            from vfx_transcode.presets import parse_channel_order
            return parse_channel_order(self.order_text())
        return (presets.channel_preferences if presets is not None else None) or []

    def channel_list(self):
        return [c.strip() for c in self.channels.split(",") if c.strip()]

    def args(self):
        out = []
        if self.mode == "first":
            out += ["-oiio_channel_use_first", "1"]
        elif self.mode == "pick" and self.channel_list():
            out += ["-oiio_channel", ",".join(self.channel_list())]
        if self.subimage.strip():
            out += ["-oiio_subimage", self.subimage.strip()]
        if self.mode == "auto" and self.order:
            if self.order_preset and list(self.order) == list(self.order_loaded):
                out += ["-channel_preferences", self.order_preset]     # the preset, unchanged
            else:
                out += ["-channel_order", self.order_text()]
        if self.deep_zrange.strip():
            out += ["-deep_zrange", self.deep_zrange.strip()]
        return out

    def full_args(self):
        """For a super-preset: written out, so it does not depend on what was set before, nor
        on the presets file (the order too; "" = the presets file's channel_preferences)."""
        return (["-oiio_channel", ",".join(self.channel_list()) if self.mode == "pick" else "",
                 "-oiio_channel_use_first", "1" if self.mode == "first" else "0",
                 "-oiio_subimage", self.subimage.strip(),
                 "-channel_preferences", "",
                 "-channel_order", self.order_text() if self.mode == "auto" else "",
                 "-deep_zrange", self.deep_zrange.strip()])


_CHANNEL_INFO_CACHE = {}


def read_channels(path, presets=None):
    """(the input's header per part, "") as vfx-transcode reads it, or ([], why). Cached by
    path and modification time."""
    from vfx_transcode.oiio_info import get_image_info
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return [], "no input"
    if Path(path).suffix.lower() != ".exr":
        return [], "only EXR files have channels to pick"
    key = (path, os.path.getmtime(path))
    if key in _CHANNEL_INFO_CACHE:
        return _CHANNEL_INFO_CACHE[key], ""
    oiiotool = tool_path(presets, "oiiotool")
    if oiiotool is None:
        return [], "oiiotool not found"
    try:
        info = get_image_info(oiiotool, path)
    except Exception as exc:                      # unreadable: say why
        return [], str(exc)
    _CHANNEL_INFO_CACHE[key] = info
    return info, ""


def channel_tree(info):
    """[(part index, part name, [(layer, [channels])])] for the channel browser."""
    from vfx_transcode.channels import group_layers, parts
    return [(i, name, list(group_layers(channels).items())) for i, name, channels in parts(info)]


def describe_pick(info, state, presets=None):
    """What vfx-transcode will use with these settings, e.g. "diffuse.R,diffuse.G,diffuse.B
    (requested), part 2 diffuse"; or what is wrong."""
    from vfx_transcode.channels import pick_channels, subimage_name
    if not info:
        return ""
    prefs = state.order_sets(presets) or None
    try:
        pick = pick_channels(info, state.channel_list() if state.mode == "pick" else [],
                             state.mode == "first", prefs, state.subimage)
    except ValueError as exc:
        return "Not usable: {}".format(exc)
    name = subimage_name(info[pick.subimage], pick.subimage) if pick.subimage < len(info) else ""
    mono = len(pick.without_alpha) == 1
    return "{}  ({}{}){}".format(",".join(pick.without_alpha), pick.reason,
                                 ", part {}{}".format(pick.subimage, " " + name if name else "")
                                 if len(info) > 1 else "",
                                 "  - one channel: shown as grey" if mono else "")


# intermediate files (Intermediate tab): the frames handed from oiiotool / the movie unpack
# to ffmpeg, and where they go

MEZZANINE_FORMATS = OrderedDict([
    ("png", "PNG: lossless, 8 or 16-bit (default)"),
    ("tif", "TIF: lossless, 8 or 16-bit, larger, quick to write"),
    ("jpg", "JPG: small and quick, lossy, 8-bit only"),
    ("bmp", "BMP: uncompressed 8-bit, quickest, largest"),
    ("exr", "EXR: float, keeps the full range (EXR input only; a movie unpacks to PNG)"),
])
SIXTEEN_BIT_FORMATS = ("png", "tif")


@dataclass
class IntermediateState:
    mezzanine: str = "png"            # -mezzanine
    sixteen_bit: bool = False         # -mezzanine_16bit: 16-bit PNG / TIF
    location: str = "user"            # -temp_location user | source
    temp: str = ".temp_png_sequence"  # -temp: the folder name with location "source"
    keep: bool = False                # -temp_keep
    oiio_threads: str = "50%"         # -oiio_threads: N, or a share of the running machine's threads
    per_file: bool = False            # -oiio_per_file: one oiiotool per frame
    verify: bool = True               # -oiio_verify: convert suspected black frames again

    def args(self, full=False):
        """The options; full: all of them (a super-preset), else only what is not the default."""
        default = IntermediateState()
        out = []
        for option, value, base in (
                ("-mezzanine", self.mezzanine, default.mezzanine),
                ("-mezzanine_16bit", "1" if self.sixteen_bit else "0", "0"),
                ("-temp_location", self.location, default.location),
                ("-temp_keep", "1" if self.keep else "0", "0"),
                ("-oiio_threads", str(self.oiio_threads).strip() or "50%", default.oiio_threads),
                ("-oiio_per_file", "1" if self.per_file else "0", "0"),
                ("-oiio_verify", "1" if self.verify else "0", "1")):
            if full or value != base:
                out += [option, value]
        if self.location == "source":
            out += ["-temp", self.temp]
        return out

    def note(self):
        """What the choice means, for the line under the controls."""
        lines = []
        if self.sixteen_bit and self.mezzanine not in SIXTEEN_BIT_FORMATS:
            lines.append("16-bit is used only with PNG or TIF; {} stays as it is.".format(self.mezzanine.upper()))
        if self.mezzanine == "jpg":
            lines.append("JPG loses a little quality on every frame: fine for quick reviews.")
        if not self.sixteen_bit and self.mezzanine in SIXTEEN_BIT_FORMATS:
            lines.append("8-bit: enough for H.264; a 10-bit preset (ProRes, DNxHR, H.265 10-bit) "
                         "gets real 10-bit input only with 16-bit.")
        if self.location == "source":
            lines.append('Next to the source in "{}" ({} the input folder itself).'.format(
                self.temp or ".", "empty =" if self.temp else "this is"))
        return " ".join(lines)


# thumbnail (Thumbnail tab): -ot next to the output, a thumbnail preset and its settings

THUMB_FRAMES = ("middle", "first", "last", "largest")
THUMB_FIELDS = ("frame", "width", "height", "format", "quality", "compression", "bits")


@dataclass
class ThumbState:
    enabled: bool = False             # write a thumbnail (-ot)
    preset: str = ""                  # thumbnail preset; "" = the presets file's default
    custom: bool = False              # the settings below differ from the preset: written out
    frame: str = "middle"
    width: int = 0                    # 0 = follow the other side (both 0: the movie's size)
    height: int = 0
    format: str = "jpg"               # jpg | png
    quality: int = 90                 # jpg
    compression: int = 6              # png 0-9
    bits: int = 8                     # png 8 | 16

    def preset_object(self, name=""):
        return ThumbnailPreset(name=name or self.preset or "tuned", frame=self.frame,
                               width=self.width, height=self.height, format=self.format,
                               quality=self.quality, compression=self.compression,
                               bits=self.bits if self.format == "png" else 8)


def thumb_preset(presets, name):
    """The preset a thumbnail state name stands for: "" = the file's default, else built-in."""
    name = name or (presets.default_thumbnail if presets else "")
    found = presets.thumbnails.get(name) if presets and name else None
    return found or ThumbnailPreset(name="")


def thumb_from_preset(preset, name="", enabled=False):
    """The Thumbnail tab loaded from a preset (name: what the tab shows; "" = the default)."""
    return ThumbState(enabled=enabled, preset=name, custom=False,
                      **{f: getattr(preset, f) for f in THUMB_FIELDS})


def thumb_custom(state, presets):
    """Whether the settings differ from the preset named in the state."""
    preset = thumb_preset(presets, state.preset)
    mine = state.preset_object()
    return any(getattr(mine, f) != getattr(preset, f) for f in THUMB_FIELDS
               if f != "bits" or mine.format == "png")


def thumb_args(state):
    """-ot (the thumbnail goes next to the output), -thumbnail_preset and, when the settings
    were changed from the preset, the -thumbnail_* options for all of them; nothing when off."""
    if not state.enabled:
        return []
    out = ["-ot"] + (["-thumbnail_preset", state.preset] if state.preset else [])
    if state.custom:
        out += ["-thumbnail_frame", str(state.frame),
                "-thumbnail_size", "{}x{}".format(state.width, state.height),
                "-thumbnail_format", state.format]
        if state.format == "jpg":
            out += ["-thumbnail_quality", str(state.quality)]
        else:
            out += ["-thumbnail_compression", str(state.compression),
                    "-thumbnail_bits", str(state.bits)]
    return out


def filmstrip_json(state):
    data = OrderedDict([("notes", "tuned"), ("tiles", state.tiles)])
    if state.columns:
        data["columns"] = state.columns
    if state.rows:
        data["rows"] = state.rows
    data.update([("tile_width", state.tile_width), ("tile_height", state.tile_height),
                 ("quality", state.quality), ("source", state.source), ("slate", state.slate)])
    return data


# --- saving named presets (presets_store) ---

PRESET_AREAS = OrderedDict([("colour", "Colour"), ("smart_tokens", "Smart tokens"),
                            ("transform", "ffmpeg transform"),
                            ("transform_sequence", "oiiotool transform"), ("burnin", "Burn-in"),
                            ("slate", "Slate"), ("filmstrip", "Filmstrip"),
                            ("thumbnail", "Thumbnail"),
                            ("encode_preset", "Encode"), ("sequence_preset", "Sequence"),
                            ("metadata", "Metadata"),
                            ("destination_path", "Destination path"),
                            ("channel_order", "Channel order"),
                            ("destination_name", "Destination name")])
COLOUR_STATE_FIELDS = OrderedDict([("ci", "colorin"), ("co", "colorout"), ("display", "display"),
                                   ("view", "view"), ("looks", "looks"), ("clf", "clf"),
                                   ("ocio", "ocio")])


def colour_json(i):
    """The Color tab as a colour preset (only the fields set; the OCIO config only when it is
    not the default one)."""
    data = OrderedDict([("notes", "")])
    for key, attr in COLOUR_STATE_FIELDS.items():
        value = getattr(i, attr).strip()
        if value and not (key == "ocio" and value == DEFAULT_OCIO):
            data[key] = value
    return data


def colour_from_preset(i, preset):
    """Load a colour preset into the Color tab's state. The OCIO config stays unless the
    preset names one."""
    for key, attr in COLOUR_STATE_FIELDS.items():
        value = getattr(preset, key)
        if key == "ocio" and not value:
            continue
        setattr(i, attr, value)
    i.colour_preset = preset.name


def ff_transform_json(state):
    lines = [l.strip() for l in state.text.replace("\r", "").split("\n") if l.strip()]
    return OrderedDict([("notes", ""), ("filters", lines)])


def ff_transform_from_preset(preset):
    return FFTransformState(text="\n".join(preset.filters), preset=preset.name)


def preset_names(presets, area):
    if presets is None:
        return []
    return list({"colour": presets.colours, "transform": presets.ff_transforms,
                 "transform_sequence": presets.transforms, "burnin": presets.burnins,
                 "slate": presets.slates, "filmstrip": presets.filmstrips,
                 "thumbnail": presets.thumbnails, "encode_preset": presets.presets,
                 "sequence_preset": presets.sequences, "metadata": presets.metadata_presets,
                 "smart_tokens": presets.smart_tokens, "destination_path": presets.dest_paths,
                 "destination_name": presets.dest_names, "channel_order": presets.channel_orders}[area])


def preset_notes(presets, area, name, sub=None):
    """The notes / description of an existing preset (to keep them when replacing it)."""
    if presets is None:
        return ""
    if area == "transform_sequence":
        t = presets.transforms.get(name)
        found = next((s for s in t.subs if s.name == sub), None) if t else None
        return found.description if found else (t.description if t else "")
    table = {"colour": presets.colours, "transform": presets.ff_transforms,
             "burnin": presets.burnins, "slate": presets.slates, "filmstrip": presets.filmstrips,
             "thumbnail": presets.thumbnails, "smart_tokens": presets.smart_tokens,
             "encode_preset": presets.presets, "sequence_preset": presets.sequences,
             "metadata": presets.metadata_presets, "destination_path": presets.dest_paths,
             "destination_name": presets.dest_names, "channel_order": presets.channel_orders}[area]
    found = table.get(name)
    return getattr(found, "notes", "") if found else ""


def preset_entry(state, area, presets=None, notes="", sub=None):
    """The JSON entry the area's current settings make, for presets_store.save_preset."""
    if area == "colour":
        data = colour_json(state.input)
    elif area == "transform":
        data = ff_transform_json(state.ff_transform)
    elif area == "transform_sequence":
        data = transform_json(presets, state.transform)
        data = OrderedDict([("name", sub or data["name"]), ("description", notes),
                            ("conditions", data["conditions"]), ("transforms", data["transforms"])])
        return data
    elif area == "burnin":
        data = burn_json(state.burn)
    elif area == "slate":
        data = slate_json(state.slate)
    elif area == "filmstrip":
        data = filmstrip_json(state.filmstrip)
    elif area == "thumbnail":
        from vfx_transcode import thumbnails
        data = thumbnails.to_json(state.thumb.preset_object())
    elif area == "encode_preset":
        data = encode_json(state.encode_edit)
    elif area == "sequence_preset":
        data = sequence_json(state.sequence_edit)
    elif area == "metadata":
        data = metadata_json(state.metadata)
    elif area == "smart_tokens":
        data = tokens_json(state.tokens)
    elif area == "channel_order":
        data = OrderedDict([("channels", [",".join(x) for x in state.channels.order_sets(presets)])])
    elif area in ("destination_path", "destination_name"):
        from vfx_transcode import destinations
        kind = "path" if area == "destination_path" else "name"
        data = destinations.to_json(state.dest.path if kind == "path" else state.dest.name, kind)
    else:
        raise KeyError(area)
    data["notes"] = notes
    if not notes:
        del data["notes"]
    return data


def mark_saved(state, area, name, sub=None):
    """After a save: the area counts as loaded from the preset of that name, so the output
    boxes can name it (-burn_preset NAME ...) while it stays unchanged."""
    if area == "colour":
        state.input.colour_preset = name
    elif area == "transform":
        state.ff_transform.preset = name
    elif area == "transform_sequence":
        t = state.transform
        t.preset, t.sub, t.loaded_args = name, sub, list(t.args)
    elif area == "burnin":
        state.burn.preset = name
    elif area == "slate":
        state.slate.preset = name
    elif area == "filmstrip":
        state.filmstrip.preset = name
    elif area == "thumbnail":
        state.thumb.preset, state.thumb.custom = name, False
    elif area == "encode_preset":
        state.encode_edit.preset = name
    elif area == "sequence_preset":
        state.sequence_edit.preset = name
    elif area == "metadata":
        state.metadata.preset, state.metadata.loaded = name, metadata_snapshot(state.metadata)
    elif area == "smart_tokens":
        t = state.tokens
        t.preset, t.loaded = name, [OrderedDict(r) for r in t.rows]
    elif area == "destination_path":
        state.dest.path_preset = name
    elif area == "destination_name":
        state.dest.name_preset = name
    elif area == "channel_order":
        c = state.channels
        c.order_preset, c.order_loaded = name, list(c.order)


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


def letterbox_args(state, prefix="-letterbox"):
    """-letterbox* options (ffmpeg); prefix "-oiio_letterbox" for the oiiotool one."""
    if not state.enabled:
        return []
    out = [prefix, "1", prefix + "_ratio", state.ratio,
           prefix + "_color", _rgba(state.color, state.opacity)]
    if state.line > 0:
        out += [prefix + "_line", str(state.line),
                prefix + "_line_color", _rgba(state.line_color, state.line_opacity)]
    return out


def oiio_letterbox_args(state):
    return letterbox_args(state, "-oiio_letterbox")


# metadata

@dataclass
class MetadataState:
    preset: str = ""                  # "" = the file's default
    tags: List[Tuple[str, str]] = field(default_factory=list)
    timecode_source: str = ""         # "" = preset / default; counter | arg | metadata
    timecode: str = ""
    reelid_source: str = ""           # "" = preset / default; auto | arg | metadata | off
    reelid: str = ""
    copy: str = ""                    # -metadata_copy: "all", "Artist,capDate", "all,-husk:*"; "" = preset
    reelid_attr: str = ""             # -reelid_attr: EXR attribute with the reel name; "" = preset / owner
    reelid_pattern: str = ""          # -reelid_pattern: regex on it; "" = preset / default
    # the tab as the preset loaded it (tags, copy, reelid_attr, reelid_pattern); None = no
    # preset loaded (older states): the preset is named and the tags add to it
    loaded: Optional[dict] = None


def metadata_snapshot(m):
    return {"tags": [[k, v] for k, v in m.tags], "copy": m.copy, "reelid_attr": m.reelid_attr,
            "reelid_pattern": m.reelid_pattern}


def metadata_from_preset(m, preset, name, sources=True):
    """Load a metadata preset into the Metadata tab's state (None: no tags, nothing copied).
    sources: its timecode / reel sources go to the Input tab's fields when it has them."""
    m.preset = name
    m.tags = list(preset.tags.items()) if preset else []
    m.copy = (preset.copy or "") if preset else ""
    m.reelid_attr = (preset.reelid_attr or "") if preset else ""
    m.reelid_pattern = (preset.reelid_pattern or "") if preset else ""
    if preset is not None and sources:
        if preset.timecode_source:
            m.timecode_source = _source(preset.timecode_source)
        if preset.reelid_source:
            m.reelid_source = _source(preset.reelid_source)
        if preset.reelid:
            m.reelid = preset.reelid
    m.loaded = metadata_snapshot(m)


def metadata_changed(m):
    return m.loaded is not None and metadata_snapshot(m) != m.loaded


def copy_note(report, spec):
    """What -metadata_copy SPEC copies from the last preview's header: "copies 154
    attributes", or why it copies nothing; "" with no spec or no header (movies)."""
    from vfx_transcode import metadata as md
    header = ((report or {}).get("input") or {}).get("header") or {}
    if not (spec or "").strip() or not header:
        return ""
    available = md.header_metadata(header)
    copied = md.select(available, spec)
    hint = md.copy_hint(available, spec)
    text = "copies {} attribute{} from the input header".format(len(copied), "" if len(copied) == 1 else "s")
    return text + (". " + hint[0].upper() + hint[1:] + "." if hint else "")


def metadata_json(m):
    """The Metadata tab (and the Input tab's timecode / reel sources) as a metadata preset."""
    data = OrderedDict([("tags", OrderedDict((k.strip(), v) for k, v in m.tags if k.strip() and v != ""))])
    for key, value in (("copy", m.copy), ("timecode_source", _source(m.timecode_source)),
                       ("reelid_source", _source(m.reelid_source)), ("reelid", m.reelid),
                       ("reelid_attr", m.reelid_attr), ("reelid_pattern", m.reelid_pattern)):
        if value.strip():
            data[key] = value.strip()
    return data


TIMECODE_SOURCES = ("counter", "arg", "metadata")
REEL_SOURCES = ("auto", "arg", "metadata", "off")
DEFAULT_ARG_TIMECODE = "01:00:00:00"


def _source(value):
    value = (value or "").strip()
    return "metadata" if value == "exr" else value      # the old name


def timecode_args(state):
    """-timecode_source / -timecode / -reelid_source / -reelid. -timecode only with arg."""
    out = []
    tc_source = _source(state.timecode_source)
    if tc_source:
        out += ["-timecode_source", tc_source]
    if state.timecode.strip() and tc_source in ("", "arg"):
        out += ["-timecode", state.timecode.strip()]
    reel_source = _source(state.reelid_source)
    if reel_source:
        out += ["-reelid_source", reel_source]
    if state.reelid.strip():
        out += ["-reelid", state.reelid.strip()]
    if state.reelid_attr.strip():
        out += ["-reelid_attr", state.reelid_attr.strip()]
    if state.reelid_pattern.strip():
        out += ["-reelid_pattern", state.reelid_pattern.strip()]
    return out


def metadata_args(state):
    """The preset by name while the tab is as it loaded it; once edited, no preset
    (-metadata_preset none) and the tab written out: the tags, -metadata_copy, the reel
    attribute and pattern. Older states (nothing loaded): the preset and the tags over it."""
    out = []
    if metadata_changed(state):
        out += ["-metadata_preset", "none"]
        for key, value in state.tags:
            if key.strip() and value != "":
                out += ["-metadata", "{}={}".format(key.strip(), value)]
        out += ["-metadata_copy", state.copy.strip()]
        return out + timecode_args(state)
    if state.preset:
        out += ["-metadata_preset", state.preset]
    if state.loaded is None:
        for key, value in state.tags:
            if key.strip():
                out += ["-metadata", "{}={}".format(key.strip(), value)]
        if state.copy.strip():
            out += ["-metadata_copy", state.copy.strip()]
    return out + timecode_args(state)


def _par_number(value):
    """1.0, 1.33 ... from a report's par: a number, "2", "1.3" or "4/3 (1.33)"."""
    text = str(value if value is not None else "1").strip()
    if "(" in text:
        text = text[text.index("(") + 1:].rstrip(")")
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            return float(num) / float(den)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 1.0


def _par_text(par):
    return "{:.3g}".format(par) if par != int(par) else "{:.1f}".format(par)


def overlay_lines(report, input_size=None):
    """[(label, text)] for the picture overlay, from the last preview:
    input  3840x2160 pa 1.3 @ 24fps / output 1920x1080 pa 1.0. The pixel aspect only when
    the input's or the output's is not 1; the output's frame rate only when -output_fps plays
    it at another rate than the input's: output 1920x1080 @ 25fps (4.2% faster). The
    timecode the output gets for this frame, when it writes one: timecode 01:00:00:12
    (starts 01:00:00:00)."""
    if not report:
        return []
    words, out = report.get("keywords") or {}, report.get("output") or {}
    in_par = _par_number((report.get("input") or {}).get("par") or words.get("input_par"))
    out_par = _par_number(out.get("par"))
    show_par = abs(in_par - 1.0) > 1e-3 or abs(out_par - 1.0) > 1e-3
    lines = []
    size = ((words.get("input_width"), words.get("input_height"))
            if words.get("input_width") and words.get("input_height") else input_size)
    if size:
        text = "{}x{}".format(*size)
        if show_par:
            text += " pa " + _par_text(in_par)
        source_fps = out.get("source_fps") or out.get("fps")
        if source_fps:
            text += " @ {}fps".format(source_fps)
        lines.append(("input", text))
    if out.get("width") and out.get("height"):
        text = "{}x{}".format(out["width"], out["height"])
        if show_par:
            text += " pa " + _par_text(out_par)
        text += retime_text(out.get("source_fps"), out.get("fps"))
        lines.append(("output", text))
    if out.get("timecode_written") and out.get("frame_timecode"):
        text = out["frame_timecode"]
        if out.get("timecode") and out["timecode"] != out["frame_timecode"]:
            text += " (starts {})".format(out["timecode"])
        lines.append(("timecode", text))
    return lines


def retime_text(source_fps, output_fps):
    """" @ 25fps (4.2% faster)" when the output plays at another rate; "" when it is the same."""
    if not source_fps or not output_fps or str(source_fps) == str(output_fps):
        return ""
    try:
        source, out = parse_fps(str(source_fps)), parse_fps(str(output_fps))
    except (ValueError, ZeroDivisionError):
        return " @ {}fps".format(output_fps)
    if source == out:
        return ""
    change = abs(float(out) / float(source) - 1) * 100
    return " @ {}fps ({:.1f}% {})".format(output_fps, change, "faster" if out > source else "slower")


def found_text(report, state):
    """What the last preview found for the timecode and reel name, for the Timecode and reel
    name section: e.g. "Reel name: auto → A001C003 (EXR header owner)". "" before a preview."""
    out = (report or {}).get("output") or {}
    if not out:
        return ""
    lines = []
    tc_source = _source(state.metadata.timecode_source) or "default"
    if out.get("timecode"):
        lines.append("Timecode: {} → {} ({})".format(tc_source, out["timecode"],
                                                     out.get("timecode_source") or "?"))
    reel_source = _source(state.metadata.reelid_source) or "default"
    found = out.get("reel_source", "")
    if reel_source == "off" or found == "off":
        lines.append("Reel name: off")
    elif not found:
        lines.append("Reel name: {} → not used for this output".format(reel_source))
    elif out.get("reel_name"):
        lines.append("Reel name: {} → {} ({})".format(reel_source, out["reel_name"], found))
    else:
        lines.append("Reel name: {} → none (no Reel name given and none in the header)".format(
            reel_source))
    return "\n".join(lines)


def reel_found_none(report, state):
    """True when the last preview's reel name source was auto and found nothing (then the
    movie has no reel name to write); False when it found one; None when unknown."""
    out = (report or {}).get("output") or {}
    found = out.get("reel_source", "")
    if (_source(state.metadata.reelid_source) or "auto") != "auto" or not found or found == "off":
        return None
    return not out.get("reel_name")


def auto_timecode(kind, is_exr, header_timecode):
    """(timecode source, timecode text or None to leave the field) for a new input:
    EXR with a header timecode -> metadata, else counter; other image sequences -> counter;
    a movie with timecode -> metadata, else arg starting at 01:00:00:00."""
    if kind == "movie":
        return ("metadata", None) if header_timecode else ("arg", DEFAULT_ARG_TIMECODE)
    if is_exr and header_timecode:
        return "metadata", None
    return "counter", None


def detect_timecode(path, presets=None):
    """The start timecode in an EXR header (oiiotool), or None."""
    path = (path or "").strip()
    if Path(path).suffix.lower() != ".exr" or not Path(path).is_file():
        return None
    oiiotool = tool_path(presets, "oiiotool")
    if oiiotool is None:
        return None
    from vfx_transcode.oiio_info import get_image_info
    from vfx_transcode.timecode import timecode_from_attribs
    try:
        info = get_image_info(oiiotool, path)
    except Exception:                             # oiiotool failing is just "unknown"
        return None
    return next((tc for tc in (timecode_from_attribs(sub.get("attribs", {})) for sub in info or [])
                 if tc), None)


# --- commands -----------------------------------------------------------------------------

@dataclass
class TunerState:
    input: InputState = field(default_factory=InputState)
    transform: TransformState = field(default_factory=TransformState)
    burn: BurnState = field(default_factory=BurnState)
    slate: SlateState = field(default_factory=SlateState)
    letterbox: LetterboxState = field(default_factory=LetterboxState)          # ffmpeg
    metadata: MetadataState = field(default_factory=MetadataState)
    filmstrip: FilmstripState = field(default_factory=FilmstripState)
    thumb: ThumbState = field(default_factory=ThumbState)
    intermediate: IntermediateState = field(default_factory=IntermediateState)
    channels: ChannelsState = field(default_factory=ChannelsState)
    dest: "DestinationState" = field(default_factory=lambda: DestinationState())
    ff_transform: FFTransformState = field(default_factory=FFTransformState)
    oiio_letterbox: LetterboxState = field(default_factory=LetterboxState)     # oiiotool
    tokens: "TokensState" = field(default_factory=lambda: TokensState())
    encode_edit: "EncodeEditState" = field(default_factory=lambda: EncodeEditState())
    sequence_edit: "SequenceEditState" = field(default_factory=lambda: SequenceEditState())


# --- Encode tab: writing encode presets (ffmpeg) and sequence presets (oiiotool) ---

ENCODE_EXTS = ("mov", "mp4", "mxf", "mkv", "avi", "webm")
ALPHA_PIX_FMTS = ("", "yuva444p10le", "yuva444p12le", "yuva420p", "rgba", "gbrap10le", "gbrap12le")
SEQUENCE_EXTS = ("exr", "png", "jpg", "tif", "dpx", "bmp")


@dataclass
class EncodeEditState:
    """An encode preset being written on the Encode tab ("encode_presets" in the presets file)."""
    preset: str = ""                  # the preset loaded / saved, "" = new
    ext: str = "mov"
    encode: str = "-pix_fmt yuv420p {videofilter} -c:v libx264 -crf 18"
    ten_bit: bool = False
    colour_tags: bool = True
    alpha_pix_fmt: str = ""
    metadata: List[Tuple[str, str]] = field(default_factory=list)   # codec-level tags
    notes: str = ""                   # the loaded preset's notes (shown on the tab)


@dataclass
class SequenceEditState:
    """A sequence preset being written on the Encode tab ("sequence_presets")."""
    preset: str = ""
    ext: str = "exr"
    args: str = "-d half --compression zip"


def encode_edit_from_preset(preset):
    return EncodeEditState(preset=preset.name, ext=preset.ext,
                           encode=" ".join(quote([a]) for a in preset.encode),
                           ten_bit=preset.ten_bit, colour_tags=preset.colour_tags,
                           alpha_pix_fmt=preset.alpha_pix_fmt,
                           metadata=list(preset.metadata.items()), notes=preset.notes)


def deep_pix_fmt(encode):
    """True / False when the arguments' -pix_fmt has more / not more than 8 bits per channel,
    None without -pix_fmt."""
    match = re.search(r"-pix_fmt\s+(\S+)", encode or "")
    if not match:
        return None
    return bool(re.search(r"(9|10|12|14|16)(le|be)?$", match.group(1)))


def sequence_edit_from_preset(preset):
    return SequenceEditState(preset=preset.name, ext=preset.ext,
                             args=" ".join(quote([a]) for a in preset.args))


def encode_json(e):
    deep = deep_pix_fmt(e.encode)        # ten_bit follows the pixel format when there is one
    data = OrderedDict([("ext", e.ext.strip().lstrip(".")), ("encode", e.encode.strip()),
                        ("ten_bit", bool(e.ten_bit) if deep is None else deep),
                        ("colour_tags", bool(e.colour_tags))])
    if e.alpha_pix_fmt.strip():
        data["alpha_pix_fmt"] = e.alpha_pix_fmt.strip()
    tags = OrderedDict((k.strip(), v) for k, v in e.metadata if k.strip())
    if tags:
        data["metadata"] = tags
    return data


def sequence_json(q):
    return OrderedDict([("ext", q.ext.strip().lstrip(".")), ("args", q.args.strip())])


def encode_problem(e):
    """Why the presets file would refuse this encode preset, or ""."""
    from vfx_transcode.presets import _preset_from_json
    if not e.ext.strip():
        return "Choose the container (ext)."
    try:
        _preset_from_json("check", encode_json(e), "Encode tab")
    except TranscodeError as exc:
        return str(exc).replace('Encode tab: preset "check": ', "")
    return ""


def sequence_problem(q):
    from vfx_transcode.outseq import sequence_presets_from_json
    try:
        sequence_presets_from_json({"check": sequence_json(q)}, {}, "Encode tab")
    except TranscodeError as exc:
        return str(exc).replace('Encode tab: sequence preset "check": ', "")
    return ""


# --- smart tokens (Tokens tab) ---

TOKEN_COLUMNS = ("name", "conditions", "source", "regex", "repl", "mode", "case", "notes")
TOKEN_MODES = ("extract", "replace")
TOKEN_CASES = ("as_is", "lower", "upper", "title")


@dataclass
class TokensState:
    """The Tokens tab: a smart tokens preset ("none" = off, "" = the file's default) and its
    definitions, as JSON-like dicts, editable. Unchanged from the loaded preset, the preset
    is named (-smart_tokens NAME); edited, the definitions go as -smart_tokens_def JSON."""
    preset: str = ""
    rows: List[dict] = field(default_factory=list)
    loaded: List[dict] = field(default_factory=list)
    version_regex: str = ""           # -version_regex; "" = vfx-transcode's default _v(\d\d\d)
    name_tokens: List[Tuple[str, str]] = field(default_factory=list)   # -name_token NAME=REGEX


def token_row(token):
    """A smart_tokens.SmartToken as the dict presets.json holds (default fields left out)."""
    row = OrderedDict([("name", token.name)])
    if token.conditions:
        row["conditions"] = [OrderedDict([("left", c.left), ("op", c.op), ("right", c.right)])
                             for c in token.conditions]
    if token.source != "{clean_name}":
        row["source"] = token.source
    for key, default in (("regex", ""), ("repl", ""), ("mode", "extract"), ("case", "as_is"),
                         ("notes", "")):
        value = getattr(token, key)
        if value != default:
            row[key] = value
    return row


def tokens_from_preset(presets, name):
    """TokensState for a preset name ("none", "" = the file's default)."""
    if presets is None:
        return TokensState(preset=name)
    real = name or presets.default_smart_tokens
    preset = presets.smart_tokens.get(real) if real and real != "none" else None
    rows = [token_row(t) for t in preset.tokens] if preset else []
    return TokensState(preset=name, rows=[OrderedDict(r) for r in rows],
                       loaded=[OrderedDict(r) for r in rows])


def conditions_text(conditions):
    """[{left, op, right}] -> "{namesplit-2} == plate; {x} != y" for one table cell."""
    return "; ".join("{} {} {}".format(c["left"], c["op"], c["right"]).strip() for c in conditions or [])


def parse_conditions(text):
    """The cell text back to [{left, op, right}]; "==" or "!=" in each part, ; between."""
    out = []
    for part in (text or "").split(";"):
        if not part.strip():
            continue
        found = [(part.find(op), op) for op in ("==", "!=") if op in part]
        if not found:
            raise ValueError('condition "{}" needs == or !='.format(part.strip()))
        index, op = min(found)
        out.append(OrderedDict([("left", part[:index].strip()), ("op", op),
                                ("right", part[index + len(op):].strip())]))
    return out


DEFAULT_VERSION_REGEX = r"_v(\d\d\d)"


def name_token_args(t):
    """-version_regex (when not the default) and one -name_token NAME=REGEX per row (an empty
    regex removes the presets file's token of that name)."""
    out = []
    regex = t.version_regex.strip()
    if regex and regex != DEFAULT_VERSION_REGEX:
        out += ["-version_regex", regex]
    for name, regex in t.name_tokens:
        if name.strip():
            out += ["-name_token", "{}={}".format(name.strip(), regex)]
    return out


def tokens_args(t):
    """-smart_tokens NAME while the definitions are the loaded preset's; else the edited
    definitions as -smart_tokens_def JSON ("none" when there are none). Then the name
    tokens (-version_regex, -name_token)."""
    if [dict(r) for r in t.rows] == [dict(r) for r in t.loaded]:
        out = ["-smart_tokens", t.preset] if t.preset else []
    elif not t.rows:
        out = ["-smart_tokens", "none"]
    else:
        out = ["-smart_tokens_def", json.dumps(t.rows, separators=(",", ":"), ensure_ascii=False)]
    return out + name_token_args(t)


def tokens_json(t, notes=""):
    return OrderedDict([("notes", notes), ("tokens", [OrderedDict(r) for r in t.rows])])


def token_values(report, t):
    """[(name, value or None)] for the token names of the table, from the last preview."""
    words = (report or {}).get("keywords") or {}
    names = list(OrderedDict.fromkeys(r.get("name", "") for r in t.rows if r.get("name")))
    return [(n, words.get(n)) for n in names]


# which tools run, by input and output (see vfx_transcode.outseq)

OUTPUT_TYPES = OrderedDict([("movie", "Movie (encode preset)"), ("exr", "EXR sequence"),
                            ("png", "PNG sequence"), ("jpg", "JPG sequence"),
                            ("tif", "TIF sequence"), ("dpx", "DPX sequence")])


@dataclass(frozen=True)
class Pipeline:
    oiio: bool                        # oiiotool runs: the Transform Sequence tab applies
    ffmpeg: bool                      # ffmpeg encodes: Transform, Burn-in apply
    movie: bool                       # a movie is written: Slate, Metadata apply

    def describe(self):
        tools = [n for n, on in (("oiiotool", self.oiio), ("ffmpeg", self.ffmpeg)) if on]
        return " + ".join(tools) or "nothing"


INPUT_TYPES = OrderedDict([("movie", "Movie (mov, mp4, mxf ...)"), ("exr", "EXR sequence"),
                           ("png", "PNG sequence"), ("jpg", "JPG sequence"),
                           ("tif", "TIF sequence"), ("dpx", "DPX sequence")])
_TYPE_ALIASES = {"jpeg": "jpg", "tiff": "tif"}


def detect_input_type(path):
    """The Input type an input file has: movie, exr, png, jpg, tif, dpx; "" for none or an
    image format not in the list."""
    path = (path or "").strip()
    if not path:
        return ""
    if mv.is_movie(path):
        return "movie"
    ext = Path(path).suffix.lower().lstrip(".")
    ext = _TYPE_ALIASES.get(ext, ext)
    return ext if ext in INPUT_TYPES else ""


def effective_input_type(i):
    """InputState -> its input type: detected from the input file, else the one chosen."""
    return detect_input_type(i.input1) if i.input1.strip() else (i.input_type or "exr")


def input_kind(path, input_type=""):
    """"exr", "movie", "image" (PNG, JPG ... sequences and stills), "" (none). Without a path,
    from the chosen input type."""
    path = (path or "").strip()
    if not path:
        if not input_type:
            return ""
        return input_type if input_type in ("exr", "movie") else "image"
    if mv.is_movie(path):
        return "movie"
    return "exr" if Path(path).suffix.lower() == ".exr" else "image"


def pipeline(input_path, output_type, input_type=""):
    kind = input_kind(input_path, input_type)
    if output_type == "movie":
        return Pipeline(oiio=kind in ("exr", ""), ffmpeg=True, movie=True)
    if kind == "movie" and output_type != "exr":
        return Pipeline(oiio=False, ffmpeg=True, movie=False)
    return Pipeline(oiio=True, ffmpeg=False, movie=False)


def output_arg(path, output_type):
    """-o for the output path field and type. A movie: the path as typed. A sequence: a
    folder (ending in / or ., or ./) gets {clean_name}.####.EXT; a name without an image
    extension gets .####.EXT."""
    path = (path or "").strip() or "./"
    if output_type == "movie":
        return path
    ext = "." + output_type
    if path.endswith(("/", "\\")) or path in (".", ".."):
        return path.rstrip("/\\") + "/{clean_name}.####" + ext
    if Path(path).suffix.lower() == ext:
        return path
    if "#" in path or "%0" in path:
        return path + ext
    return path + ".####" + ext


def colour_use(state):
    """(the Color tab is used, it always is): EXR input to a movie or a display format is
    always converted; EXR to EXR and PNG / JPG / movie frames through oiiotool only with
    Convert colour (vfx-transcode converts those only when -ci / -co is given); no OCIO where
    oiiotool does not run."""
    s = state.input
    p = pipeline(s.input1, s.output_type, s.input_type)
    if not p.oiio:
        return False, False
    if input_kind(s.input1, s.input_type) in ("exr", "") and s.output_type != "exr":
        return True, True
    return bool(s.convert_colour), False


def pipeline_args(state, presets=None, active_sub=None, cli=False):
    """The options of the areas that apply to this input and output: oiiotool steps and
    letterbox where oiiotool runs, the ffmpeg transform, letterbox and burn-ins where ffmpeg
    runs, slate and metadata for a movie. The tuner sends -width 0: all scaling comes from the
    two transform tabs."""
    s = state
    p = pipeline(s.input.input1, s.input.output_type, s.input.input_type)
    out = ["-width", "0"] + tokens_args(s.tokens)
    if colour_use(s)[0]:
        out += s.input.colour_args()
    if p.oiio:
        if input_kind(s.input.input1, s.input.input_type) in ("exr", ""):
            out += s.channels.args()             # which EXR channels (preview too)
        out += (s.transform.cli_args(active_sub) if cli else s.transform.preview_args())
        out += oiio_letterbox_args(s.oiio_letterbox)
    if p.ffmpeg:
        out += s.ff_transform.args() + burn_args(s.burn, presets if cli else None)
        out += letterbox_args(s.letterbox)
    if p.movie:
        out += slate_args(s.slate, presets if cli else None) + metadata_args(s.metadata)
    elif p.oiio:
        out += metadata_args(s.metadata)          # tags written into the images (EXR ...)
        if s.input.seq_preset:
            out += ["-seq_preset", s.input.seq_preset]
    elif p.ffmpeg:
        out += timecode_args(s.metadata)          # for the burn-ins' {timecode}
    if s.input.alpha != "drop":
        out += ["-alpha", s.input.alpha]
        if s.input.alpha_premult:
            out += ["-alpha_premult", "1"]
    if p.movie or p.oiio:
        out += movie_write_args(s.input, defaults=False)
    return out


SEQ_TIMECODE_TYPES = ("exr", "dpx")       # image sequences that store a timecode
SEQ_REEL_TYPES = ("exr",)                 # ... and a reel name


def write_support(state):
    """(timecode can be written, reel name can be written, note) for this input and output."""
    p = pipeline(state.input.input1, state.input.output_type, state.input.input_type)
    t = state.input.output_type
    if p.movie:
        return True, True, ""
    if not p.oiio:
        return False, False, "ffmpeg writes these {} images: no timecode or reel name.".format(t.upper())
    tc, reel = t in SEQ_TIMECODE_TYPES, t in SEQ_REEL_TYPES
    if tc and reel:
        note = "{}: each frame gets its timecode (smpte:TimeCode) and the reel name (reelName).".format(t.upper())
    elif tc:
        note = "{}: each frame gets its timecode; DPX has no reel name field.".format(t.upper())
    else:
        note = "{} files store no timecode or reel name.".format(t.upper())
    return tc, reel, note


def movie_write_args(i, defaults=True):
    """-output_timecode / -output_reelid; defaults=False leaves out the ones that are on."""
    out = []
    for option, on in (("-output_timecode", i.write_timecode), ("-output_reelid", i.write_reelid)):
        if defaults or not on:
            out += [option, "1" if on else "0"]
    return out


ALPHA_MODES = OrderedDict([("drop", "No alpha (-alpha drop)"),
                           ("auto", "Keep alpha if the input has one (-alpha auto)"),
                           ("keep", "Keep alpha; stop if the output cannot store it (-alpha keep)")])
MISSING_FRAMES = OrderedDict([("hold", "Hold the previous frame (-missing_frames hold)"),
                              ("error", "Stop with an error (-missing_frames error)")])
PAR_OUT_CHOICES = ("same", "square", "1", "2", "1.33", "1.5", "1.8", "0.9")
ALPHA_SEQUENCE_TYPES = ("exr", "png", "tif")


ALPHA_VIEWS = OrderedDict([("rgb", "RGB"), ("alpha", "Alpha"), ("checker", "Checkerboard")])


def alpha_hint(state, report):
    """A note next to the alpha view controls when the picture's alpha is for the view only:
    the preview keeps the input's alpha (-preview_alpha), the output does not."""
    out = (report or {}).get("output") or {}
    if not out.get("alpha") or out.get("alpha_output", True):
        return ""
    if state.input.alpha == "drop":
        return "view only: the output drops alpha (Output tab, Alpha: auto keeps it)"
    return "view only: this output cannot store alpha (see the Output tab)"


def alpha_note(state, presets=None):
    """What -alpha auto / keep does with this output: kept when the input has alpha, or why
    not (auto: no alpha, keep: vfx-transcode stops with an error)."""
    i = state.input
    if i.alpha == "drop":
        return ""
    fails = ("vfx-transcode will stop with an error (-alpha keep)." if i.alpha == "keep"
             else "the output gets none.")
    if i.output_type != "movie":
        if i.output_type in ALPHA_SEQUENCE_TYPES:
            return "Kept when the input has alpha ({} can store it).".format(i.output_type.upper())
        return "{} cannot store alpha: {}".format(i.output_type.upper(), fails)
    if presets is None:
        return ""
    name = i.encode_preset or presets.default
    preset = presets.presets.get(name)
    if preset is not None and preset.alpha_pix_fmt:
        return "Kept when the input has alpha (encode preset {}: {}).".format(name, preset.alpha_pix_fmt)
    return ("The encode preset {} cannot store alpha: {} Presets that can are marked [alpha] "
            "(e.g. prores4444).".format(name, fails))


def builtin_sequence_args(output_type):
    from vfx_transcode.outseq import oiio_output_args
    return oiio_output_args("." + output_type)


def sequence_presets_for(presets, output_type):
    """Names of the sequence presets for an output format, and the format's default ("")."""
    if presets is None or output_type == "movie":
        return [], ""
    names = [n for n, q in presets.sequences.items() if q.ext == output_type]
    return names, presets.default_sequences.get(output_type, "")


def _type_arg(state):
    """-o for a preview: only tells vfx-transcode which output it previews."""
    t = state.input.output_type
    return [] if t == "movie" else ["-o", "preview.####." + t]


def preview_command(state, preview_path, tool=None):
    """The command the tuner runs for the preview image."""
    s = state
    return ((tool or vfx_transcode_command()) + s.input.args()
            + (["-thumbnail_frame", s.input.frame] if s.input.frame else [])
            + pipeline_args(s) + _type_arg(s)
            + ["-preview_alpha", "1"]          # the input's alpha in the picture, for the view
            + ["-preview", str(preview_path)])


STILL_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def compare_input(state):
    """The file the picture is compared to: the source with "compare to source", else the
    compare file; "" for none."""
    i = state.input
    return i.input1 if i.compare_source else i.input2.strip()


def compare_is_plain_image(path, kind):
    """A single image Qt can show as it is (no frame to pick, no conversion)."""
    return kind == "still" and Path(path).suffix.lower() in STILL_EXTS


def compare_command(state, frame, out_path, tool=None):
    """vfx-transcode making the compare frame: the compare input at `frame` (or the nearest
    one it has), with no transform, resize, burn-ins or slate: the unmodified footage. EXR
    gets the Color tab's colour conversion, so it can be looked at."""
    i = state.input
    path = compare_input(state)
    cmd = (tool or vfx_transcode_command()) + ["-i", path]
    if i.compare_source:                          # the same frame numbering as the preview
        if i.fps.strip():
            cmd += ["-r", i.fps.strip()]
        if i.movie_first_frame != mv.DEFAULT_FIRST_FRAME and mv.is_movie(path):
            cmd += ["-movie_first_frame", str(i.movie_first_frame)]
    if i.presets_file.strip():
        cmd += ["-presets_file", i.presets_file.strip()]
    return (cmd + ["-thumbnail_frame", str(frame)] + i.colour_args()
            + ["-width", "0", "-oiio_transform", "none", "-burn_preset", "none",
               "-slate", "none", "-preview", str(out_path)])


def filmstrip_command(state, filmstrip_path, movie_path, tool=None):
    """The command for the filmstrip view. Tiles from the source frames: only the filmstrip.
    From the movie, its intermediates or with the slate (movie output): the movie is made too
    (movie_path), with burn-ins, slate and letterbox, so the filmstrip shows the delivery."""
    s = state
    cmd = ((tool or vfx_transcode_command()) + s.input.args() + pipeline_args(s)
           + filmstrip_args(s.filmstrip) + ["-of", str(filmstrip_path)])
    if s.filmstrip.needs_movie() and s.input.output_type == "movie":
        cmd += ["-o", str(movie_path)]
    return cmd


def full_args(state, presets, active_sub=None, output=None):
    """vfx-transcode options for the whole conversion with these settings (no program). The
    output, thumbnail and filmstrip paths come from the Destination (output: another -o)."""
    s = state
    d = s.dest
    output = d.output() if output is None else output
    film = ["-of", d.filmstrip()] + filmstrip_args(s.filmstrip, presets) if s.filmstrip.enabled else []
    thumb = thumb_args(s.thumb)
    if thumb:
        thumb = ["-ot", d.thumbnail()] + thumb[1:]
    return (s.input.args() + d.custom_args() + s.intermediate.args()
            + pipeline_args(s, presets, active_sub, cli=True)
            + ["-o", output_arg(output, s.input.output_type)] + film + thumb)


def process_command(state, presets, output=None, active_sub=None, tool=None):
    """What the Process button runs: the whole conversion."""
    return (tool or vfx_transcode_command()) + full_args(state, presets, active_sub, output)


# --- tab marks: "Name *" = edited since its preset was loaded or saved, italics = not used ---

def _colour_edited(i, presets):
    preset = presets.colours.get(i.colour_preset) if presets and i.colour_preset else None
    if preset is None:
        return False
    for key, attr in COLOUR_STATE_FIELDS.items():
        value = getattr(preset, key)
        if key == "ocio" and not value:
            continue
        if (value or "") != (getattr(i, attr) or ""):
            return True
    return False


def _encode_edited(state, presets):
    e, q = state.encode_edit, state.sequence_edit
    if presets is None:
        return False
    if e.preset in presets.presets:
        if encode_json(e) != encode_json(encode_edit_from_preset(presets.presets[e.preset])):
            return True
    elif encode_json(e) != encode_json(EncodeEditState()):
        return True
    if q.preset in presets.sequences:
        return sequence_json(q) != sequence_json(sequence_edit_from_preset(presets.sequences[q.preset]))
    return sequence_json(q) != sequence_json(SequenceEditState())


def _dest_edited(d, presets):
    if presets is None:
        return False
    path = presets.dest_paths.get(d.path_preset) if d.path_preset else None
    name = presets.dest_names.get(d.name_preset) if d.name_preset else None
    return bool((path is not None and path.template != d.path)
                or (name is not None and name.template != d.name))


def tab_marks(state, presets):
    """{tab title: (edited, unused)}. edited: the tab differs from the preset it loaded or
    saved last (only tabs with presets); unused: the tab is switched off or has nothing."""
    s = state
    marks = {}

    def edited_from(table, name, make, current):
        if presets is None or not name or name not in table:
            return False
        return make(table[name]) != current

    t = s.tokens
    marks["Color"] = (_colour_edited(s.input, presets), not colour_use(s)[0])
    marks["Tokens"] = ([dict(r) for r in t.rows] != [dict(r) for r in t.loaded],
                       not t.rows and not any(n.strip() for n, _r in t.name_tokens)
                       and t.version_regex.strip() in ("", DEFAULT_VERSION_REGEX))
    c = s.channels
    marks["Multichannel"] = (bool(c.order_preset) and list(c.order) != list(c.order_loaded), False)
    marks["Encode"] = (_encode_edited(s, presets), False)
    marks["Destination"] = (_dest_edited(s.dest, presets), False)
    m = s.metadata
    marks["Metadata"] = (metadata_changed(m), m.preset == "none" and not m.tags and not m.copy.strip())
    tr = s.transform
    marks["Transform Sequence"] = (bool(tr.preset) and list(tr.args) != list(tr.loaded_args),
                                   not tr.use and not s.oiio_letterbox.enabled)
    marks["Transform"] = (
        edited_from(presets.ff_transforms if presets else {}, s.ff_transform.preset,
                    lambda p: ff_transform_from_preset(p).filters(), s.ff_transform.filters()),
        not s.ff_transform.filters() and not s.letterbox.enabled)
    b = s.burn
    marks["Burn-in"] = (
        edited_from(presets.burnins if presets else {}, b.preset,
                    lambda p: replace(burn_from_preset(p), enabled=b.enabled), b),
        not b.enabled or not b.any_text())
    sl = s.slate
    marks["Slate"] = (
        edited_from(presets.slates if presets else {}, sl.preset,
                    lambda p: replace(slate_from_spec(p, sl.preset), enabled=sl.enabled,
                                      burnin=sl.burnin), sl),
        not sl.enabled)
    f = s.filmstrip
    marks["Filmstrip"] = (
        edited_from(presets.filmstrips if presets else {}, f.preset,
                    lambda p: filmstrip_from_preset(p, f.preset, f.enabled), f),
        not f.enabled)
    marks["Thumbnail"] = (s.thumb.custom, not s.thumb.enabled)
    return marks


# --- the full command by tab (Presets tab colours) ---

# option -> the tab it comes from; checked in order, an entry ending in * is a prefix
OPTION_TABS = (
    ("Input", ("-i", "-in", "-out", "-r", "-par", "-movie_first_frame", "-presets_file",
               "-missing_frames", "-timecode_source", "-timecode", "-reelid_source", "-reelid")),
    ("Color", ("-ci", "-co", "-display", "-view", "-looks", "-clf", "-ocio")),
    ("Tokens", ("-smart_tokens", "-smart_tokens_def", "-version_regex", "-name_token")),
    ("Intermediate", ("-mezzanine*", "-temp*", "-oiio_threads", "-oiio_per_file")),
    ("Multichannel", ("-oiio_channel*", "-oiio_subimage", "-channel_preferences",
                      "-channel_order", "-deep_zrange")),
    ("Output", ("-preset", "-seq_preset", "-alpha*", "-par_out", "-output_fps",
                "-output_timecode", "-output_reelid")),
    ("Destination", ("-o", "-custom_destination*", "-base_folder")),
    ("Metadata", ("-metadata*", "-reelid_attr", "-reelid_pattern")),
    ("Transform Sequence", ("-oiio_transform*", "-oiio_letterbox*")),
    ("Transform", ("-width", "-ff_transform*", "-letterbox*")),
    ("Burn-in", ("-burn*",)),
    ("Slate", ("-slate*",)),
    ("Filmstrip", ("-of", "-filmstrip*")),
    ("Thumbnail", ("-ot", "-thumbnail*")),
)
_OPTION_RE = re.compile(r"^-[A-Za-z][A-Za-z0-9_]*$")


def option_tab(option):
    """The tab an option belongs to, "" when none does."""
    for tab, names in OPTION_TABS:
        for name in names:
            if option == name or (name.endswith("*") and option.startswith(name[:-1])):
                return tab
    return ""


def command_parts(args):
    """[(tab, [args])]: the command cut into runs of the same tab. An option starts a run;
    its values (the words up to the next option) go with it."""
    parts, tab = [], ""
    for arg in args:
        if _OPTION_RE.match(str(arg)):
            tab = option_tab(arg)
        if parts and parts[-1][0] == tab:
            parts[-1][1].append(arg)
        else:
            parts.append((tab, [arg]))
    return parts


def output_sections(state, presets, active_sub=None, output=None):
    """{section: (command-line text, presets.json text or "")} for the copy boxes, plus the
    full command."""
    s = state
    sections = OrderedDict()
    sections["transform"] = (quote(s.ff_transform.args() + letterbox_args(s.letterbox)), "")
    sections["transform_sequence"] = (
        quote(s.transform.cli_args(active_sub) + oiio_letterbox_args(s.oiio_letterbox)),
        json.dumps(transform_json(presets, s.transform), indent=2) if s.transform.use else "")
    sections["burnin"] = (quote(burn_args(s.burn, presets)),
                          json.dumps(burn_json(s.burn), indent=2)
                          if s.burn.enabled and s.burn.any_text() else "")
    sections["slate"] = (quote(slate_args(s.slate, presets)),
                         json.dumps(slate_json(s.slate), indent=2) if s.slate.enabled else "")
    sections["metadata"] = (quote(metadata_args(s.metadata)), "")
    film = ["-of"] + filmstrip_args(s.filmstrip, presets)
    sections["filmstrip"] = (quote(film), json.dumps(filmstrip_json(s.filmstrip), indent=2))
    sections["thumbnail"] = (quote(thumb_args(s.thumb)), "")
    full = ["vfx-transcode"] + full_args(s, presets, active_sub, output)
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


def non_empty_rows(rows):
    """Rows whose value is not empty (blank text counts as empty)."""
    return [r for r in rows if str(r[2]).strip()]


def _text(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


# --- input: sequence or movie ---------------------------------------------------------------

@dataclass
class InputInfo:
    kind: str = ""                    # "sequence" | "still" | "movie" | "" (nothing / error)
    text: str = ""                    # one line for the Input tab
    first: int = 0
    last: int = 0
    frames: Tuple[int, ...] = ()
    fps: Optional[Fraction] = None    # the movie's rate (sequences: from the header, on Detect)
    timecode: Optional[str] = None    # the movie's start timecode
    par: Optional[Fraction] = None    # the movie's pixel aspect (SAR)
    error: str = ""


def tool_path(presets, name):
    """An executable as vfx-transcode would find it: the presets file's tools, else the
    built-in default, else the PATH. None if nowhere."""
    configured = (presets.tools.get(name) if presets is not None else None) or TOOLS[name]
    try:
        return resolve_tool(configured, app_dir(), name)
    except TranscodeError:
        return None


IMAGE_EXTS = (".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".dpx", ".bmp", ".tga", ".hdr")
_NUMBERED = re.compile(r"^(?P<base>.*?)(?P<num>\d+)$")


def is_media(path):
    """A file vfx-transcode can take: an image (EXR, PNG, JPG, TIF, DPX ...) or a movie."""
    path = Path(str(path))
    return path.is_file() and (path.suffix.lower() in IMAGE_EXTS or mv.is_movie(path))


def media_in_folder(folder):
    """(file, what was found) for a dropped folder: a file to use when the folder holds
    exactly one movie, one image sequence or one single image (sub-folders are not
    searched); else (None, what was found)."""
    folder = Path(folder)
    groups = {}
    try:
        entries = sorted(p for p in folder.iterdir() if p.is_file())
    except OSError as exc:
        return None, "cannot read the folder: {}".format(exc)
    for path in entries:
        ext = path.suffix.lower()
        if mv.is_movie(path):
            key = ("movie", path.name)
        elif ext in IMAGE_EXTS:
            match = _NUMBERED.match(path.stem)
            if match and match.group("base") and match.group("base")[-1] in "._-":
                key = ("sequence", match.group("base"), ext)       # name.####.ext
            elif match and not match.group("base"):
                key = ("sequence", "", ext)                        # ####.ext
            else:
                key = ("still", path.name)
        else:
            continue
        groups.setdefault(key, []).append(path)
    if not groups:
        return None, "no images or movies in the folder"
    if len(groups) > 1:
        kinds = sorted(set(k[0] for k in groups))
        return None, "{} media items in the folder ({}): pick one".format(len(groups), ", ".join(kinds))
    (key, files), = groups.items()
    what = {"movie": "the folder's movie", "sequence": "the folder's image sequence",
            "still": "the folder's image"}[key[0]]
    return files[0], what


def describe_input(path, presets=None, first_frame=mv.DEFAULT_FIRST_FRAME):
    """What the input is: the sequence a still belongs to, or a movie's probe."""
    path = (path or "").strip()
    if not path:
        return InputInfo()
    if Path(path).is_dir():
        return InputInfo(error="a folder: press ... to pick a file in it")
    if not Path(path).is_file():
        return InputInfo(error="not found: " + path)
    if mv.is_movie(path):
        ffprobe = tool_path(presets, "ffprobe")
        if ffprobe is None:
            return InputInfo(kind="movie", error="ffprobe not found (set tools.ffprobe in the presets file)")
        try:
            info = mv.probe_movie(ffprobe, path)
        except TranscodeError as exc:
            return InputInfo(kind="movie", error=str(exc))
        frames = tuple(range(first_frame, first_frame + info.frames))
        return InputInfo(kind="movie", text="Movie: " + info.describe(), first=frames[0],
                         last=frames[-1], frames=frames, fps=info.fps, timecode=info.timecode,
                         par=info.par)
    try:
        seq = find_sequence(path)
    except TranscodeError as exc:
        return InputInfo(error=str(exc))
    if not seq.numbered or len(seq.frames) == 1:
        return InputInfo(kind="still", text="Still image: {}".format(Path(path).name),
                         first=seq.start, last=seq.start, frames=seq.frames)
    pattern = "{}{}{}".format(seq.base, "#" * seq.zfill, seq.ext)
    missing = seq.missing
    text = "Sequence: {}  {}-{} ({} frames{})".format(
        pattern, seq.start, seq.end, len(seq.frames),
        ", missing {}".format(format_frames(missing)) if missing else "")
    return InputInfo(kind="sequence", text=text, first=seq.start, last=seq.end, frames=seq.frames)


def detect_fps(path, presets=None):
    """(fps or None, where from): the movie's rate, or the EXR header's."""
    path = (path or "").strip()
    if not path or not Path(path).is_file():
        return None, "no input"
    if mv.is_movie(path):
        info = describe_input(path, presets)
        if info.error:
            return None, info.error
        return info.fps, "movie" if info.fps else "the movie gives no rate"
    if Path(path).suffix.lower() != ".exr":
        return None, "{} files carry no frame rate".format(Path(path).suffix.lstrip(".") or "these")
    oiiotool = tool_path(presets, "oiiotool")
    if oiiotool is None:
        return None, "oiiotool not found"
    from vfx_transcode.oiio_info import get_image_info
    try:
        info = get_image_info(oiiotool, path)
    except Exception as exc:                      # oiiotool failing is just "unknown"
        return None, str(exc)
    for sub in info or []:
        fps, key = fps_from_attribs(sub.get("attribs", {}))
        if fps is not None:
            return fps, "EXR header " + key
    return None, "no rate in the EXR header"


def detect_par(path, presets=None):
    """(pixel aspect or None, where from): the movie's sample aspect ratio, or the EXR header's
    PixelAspectRatio. PNG / JPG / TIF carry none that vfx-transcode reads."""
    from vfx_transcode.aspect import header_par
    path = (path or "").strip()
    if not path or not Path(path).is_file():
        return None, "no input"
    if mv.is_movie(path):
        info = describe_input(path, presets)
        if info.error:
            return None, info.error
        return info.par, "movie (sample aspect ratio)"
    if Path(path).suffix.lower() != ".exr":
        return None, "{} files carry no pixel aspect; set it here".format(
            Path(path).suffix.lstrip(".") or "these")
    oiiotool = tool_path(presets, "oiiotool")
    if oiiotool is None:
        return None, "oiiotool not found"
    from vfx_transcode.oiio_info import get_image_info
    try:
        info = get_image_info(oiiotool, path)
    except Exception as exc:                      # oiiotool failing is just "unknown"
        return None, str(exc)
    par, name = header_par(md.header_attribs(info))
    if par is None:
        return None, "no pixel aspect in the EXR header; set it here"
    return par, "EXR header " + name


def par_text(par):
    """A pixel aspect as -par takes it: 2, 4/3, 1.8."""
    if par is None:
        return ""
    if par.denominator == 1:
        return str(par.numerator)
    if par == Fraction(4, 3):
        return "4/3"
    return "{:.6g}".format(float(par))


def fps_text(fps):
    return fps_display(fps) if fps is not None else ""


def frame_timecode(frame, first, start_tc, fps):
    """Timecode of a frame, from the timecode of the first frame; "" when unknown."""
    try:
        fps_value = parse_fps(fps) if isinstance(fps, str) else fps
        if fps_value is None:
            return ""
        fps_int = nominal_fps(fps_value)
        if not start_tc:
            return frame_to_tc(frame, fps_int)    # the frame counter, vfx-transcode's default
        return offset_timecode(start_tc, frame - first, fps_int)
    except (TranscodeError, ValueError, TypeError, ZeroDivisionError):
        return ""


# --- super-presets --------------------------------------------------------------------------

SUPER_AREAS = sp.AREAS


def _flat(cls, data, **convert):
    """A dataclass from a dict: known fields only, missing ones default, convert[name] applied."""
    data = data or {}
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            value = data[f.name]
            kwargs[f.name] = convert[f.name](value) if f.name in convert else value
    return cls(**kwargs)


def _pairs(value):
    return [tuple(p) for p in value or [] if isinstance(p, (list, tuple)) and len(p) == 2]


def _places(value):
    places = _default_places()
    for name, data in (value or {}).items():
        if name in places:
            places[name] = _flat(PlaceState, data)
    return places


def _plain(obj):
    return json.loads(json.dumps(asdict(obj)))


INPUT_AREA_FIELDS = ("fps", "par", "movie_first_frame")
COLOUR_AREA_FIELDS = ("colorin", "colorout", "display", "view", "looks", "clf", "ocio",
                      "convert_colour", "colour_preset")


def area_state(state, area):
    """The tuner's values of one area, as plain JSON data."""
    if area == "input":
        return {f: getattr(state.input, f) for f in INPUT_AREA_FIELDS}
    if area == "colour":
        return {f: getattr(state.input, f) for f in COLOUR_AREA_FIELDS}
    if area == "tokens":
        return _plain(state.tokens)
    if area == "encode":
        return {"output_type": state.input.output_type, "output_fps": state.input.output_fps,
                "encode_preset": state.input.encode_preset, "seq_preset": state.input.seq_preset,
                "alpha": state.input.alpha, "write_timecode": state.input.write_timecode,
                "write_reelid": state.input.write_reelid, "alpha_premult": state.input.alpha_premult,
                "par_out": state.input.par_out, "missing_frames": state.input.missing_frames}
    if area == "transform_sequence":
        return {"steps": _plain(state.transform), "letterbox": _plain(state.oiio_letterbox)}
    if area == "transform":
        return {"ffmpeg": _plain(state.ff_transform), "letterbox": _plain(state.letterbox)}
    obj = {"burnin": state.burn, "slate": state.slate, "filmstrip": state.filmstrip,
           "thumbnail": state.thumb, "intermediate": state.intermediate, "channels": state.channels,
           "metadata": state.metadata}[area]
    return _plain(obj)


def area_args(state, area):
    """vfx-transcode options for one area, written out in full (a super-preset must not depend
    on which presets file is loaded) except a transform preset left as loaded: its name, so
    vfx-transcode picks the sub-preset for each input by its conditions."""
    s = state
    if area == "input":
        i = s.input
        return ((["-r", i.fps.strip()] if i.fps.strip() else [])
                + (["-par", i.par.strip()] if i.par.strip() else [])
                + ["-movie_first_frame", str(i.movie_first_frame)])
    if area == "colour":
        return s.input.colour_args() if colour_use(s)[0] else []
    if area == "tokens":
        t = s.tokens
        out = (["-smart_tokens_def", json.dumps(t.rows, separators=(",", ":"), ensure_ascii=False)]
               if t.rows else ["-smart_tokens", "none"])
        return out + ["-version_regex", t.version_regex.strip() or DEFAULT_VERSION_REGEX] + [
            a for name, regex in t.name_tokens if name.strip()
            for a in ("-name_token", "{}={}".format(name.strip(), regex))]
    if area == "encode":
        return ((["-preset", s.input.encode_preset] if s.input.encode_preset else [])
                + (["-seq_preset", s.input.seq_preset] if s.input.seq_preset else [])
                + ["-alpha", s.input.alpha] + movie_write_args(s.input)
                + ["-alpha_premult", "1" if s.input.alpha_premult and s.input.alpha != "drop" else "0"]
                + ["-par_out", (s.input.par_out or "same").strip() or "same"]
                + ["-missing_frames", s.input.missing_frames or "hold"]
                + ["-output_fps", s.input.output_fps or "same"])
    if area == "transform_sequence":
        t = s.transform
        if t.use and t.preset and list(t.args) == list(t.loaded_args):
            steps = ["-oiio_transform", t.preset]
        else:
            steps = t.cli_args(None)
        return ["-width", "0"] + steps + (oiio_letterbox_args(s.oiio_letterbox) or ["-oiio_letterbox", "0"])
    if area == "transform":
        return ["-width", "0"] + s.ff_transform.args() + (letterbox_args(s.letterbox) or ["-letterbox", "0"])
    if area == "burnin":
        return burn_args(s.burn)
    if area == "slate":
        return slate_args(s.slate)
    if area == "filmstrip":
        return ["-of"] + filmstrip_args(s.filmstrip) if s.filmstrip.enabled else []
    if area == "thumbnail":
        return thumb_args(s.thumb)
    if area == "intermediate":
        return s.intermediate.args(full=True)
    if area == "channels":
        return s.channels.full_args()
    if area == "metadata":
        return metadata_args(s.metadata)
    raise KeyError(area)


def input_type_kind(input_type):
    """movie / exr / png ... -> the process app's input kind: movie, exr or image."""
    return input_type if input_type in ("movie", "exr") else "image"


# --- destination: where the output goes (-o), saved with a super-preset -------------------

DESTINATION = "destination"           # not an area: no vfx-transcode options, only the -o
ONE_FOLDER = "One folder for all..."  # menu entries that are no preset: pick a folder / type
CUSTOM = "Custom"
SAME_AS_OUTPUT = "(as the output)"    # thumbnail / filmstrip: the output's path or name


def path_menu(presets):
    """{menu text: path template} for the Path menu: the presets file's destination paths, then
    "One folder for all..." ("" - asks for the folder) and "Custom" (None - typed)."""
    from vfx_transcode import destinations
    table = presets.dest_paths if presets is not None else destinations.builtin("path")
    menu = OrderedDict((name, p.template) for name, p in table.items())
    menu[ONE_FOLDER] = ""
    menu[CUSTOM] = None
    return menu


def name_menu(presets):
    """{menu text: name template} for the Name menu, then "Custom" (None - typed)."""
    from vfx_transcode import destinations
    table = presets.dest_names if presets is not None else destinations.builtin("name")
    menu = OrderedDict((name, p.template) for name, p in table.items())
    menu[CUSTOM] = None
    return menu


@dataclass
class DestinationState:
    """Destination tab: where the output goes, and the thumbnail and filmstrip ("" = as the
    output). Paths and names are keyword templates; -o / -ot / -of are made of them."""
    path: str = "./"
    name: str = ""
    path_preset: str = ""             # the preset last picked (display only)
    name_preset: str = ""
    thumb_path: str = ""
    thumb_name: str = ""
    film_path: str = ""
    film_name: str = ""
    custom_path: str = ""             # test values for {custom_destination_path} / _name
    custom_name: str = ""

    def output(self):
        return destination_template(self.path, self.name)

    def thumbnail(self):
        return destination_template(self.thumb_path or self.path, self.thumb_name or self.name)

    def filmstrip(self):
        """The output's name + _filmstrip unless set, so it never takes the thumbnail's name."""
        name = self.film_name or (self.name.strip() + "_filmstrip" if self.name.strip() else "")
        return destination_template(self.film_path or self.path, name)

    def custom_args(self):
        uses_path, uses_name = uses_custom(self.output(), self.thumbnail(), self.filmstrip())
        return custom_args(self.custom_path if uses_path else "", self.custom_name if uses_name else "")

    def to_json(self):
        """The super-preset's "destination"."""
        out = OrderedDict([("path", self.path or "./"), ("name", self.name or "")])
        for key, path, name in (("thumbnail", self.thumb_path, self.thumb_name),
                                ("filmstrip", self.film_path, self.film_name)):
            if path or name:
                out[key] = OrderedDict([("path", path), ("name", name)])
        return out


def destination_of(entry):
    """DestinationState from a super-preset's "destination", or None."""
    dest = (entry or {}).get(DESTINATION)
    if not isinstance(dest, dict):
        return None
    text = lambda d, k: str((d or {}).get(k, "") or "")
    thumb, film = dest.get("thumbnail") or {}, dest.get("filmstrip") or {}
    return DestinationState(path=text(dest, "path") or "./", name=text(dest, "name"),
                            thumb_path=text(thumb, "path"), thumb_name=text(thumb, "name"),
                            film_path=text(film, "path"), film_name=text(film, "name"))


def preset_index(menu, value, folder_menu=False):
    """The menu entry showing this value, else Custom (the last). In the Path menu "" is "One
    folder for all...", which needs a folder: it never matches."""
    for index, (label, template) in enumerate(menu.items()):
        if template is not None and template == value and not (folder_menu and label == ONE_FOLDER):
            return index
    return len(menu) - 1


CUSTOM_PATH = "{custom_destination_path}"
CUSTOM_NAME = "{custom_destination_name}"


def uses_custom(*texts):
    """(uses {custom_destination_path}, uses {custom_destination_name}) in these texts."""
    joined = " ".join(t or "" for t in texts)
    return CUSTOM_PATH in joined, CUSTOM_NAME in joined


def custom_args(path, name):
    """-custom_destination_path / _name for the values given ("" leaves one out)."""
    out = []
    for option, value in (("-custom_destination_path", path), ("-custom_destination_name", name)):
        if (value or "").strip():
            out += [option, value.strip()]
    return out


def destination_template(path, name):
    """-o from the Destination's Path and Name: an empty path is the source's folder."""
    path = (path or "").strip()
    if path and not path.endswith(("/", "\\")):
        path += "/"
    return (path or "./") + (name or "").strip()


def super_preset_entry(state, areas, notes="", previous=None, destination=None):
    """A super-preset of these areas of the state. It records the input type it was made for:
    "input_type" (for the tuner) and "inputs" (the kind, for the process app, which offers it
    only for those inputs). inputs widened by hand in the file are kept while they include
    this kind. destination: a DestinationState to save with it, or None."""
    input_type = effective_input_type(state.input)
    kind = input_type_kind(input_type)
    inputs = list((previous or {}).get("inputs") or [])
    if kind not in inputs:
        inputs = [kind]
    out = OrderedDict([("notes", notes), ("input_type", input_type), ("inputs", inputs)])
    if destination is not None:
        out[DESTINATION] = destination.to_json()
    out["areas"] = OrderedDict()
    for area in SUPER_AREAS:
        if area in areas:
            out["areas"][area] = OrderedDict([("state", area_state(state, area)),
                                              ("args", area_args(state, area))])
    return out


def apply_super_preset(state, entry, areas=None):
    """Load the chosen areas (default: all the super-preset has) into the state. Returns the
    areas loaded. Older files: a "letterbox" area loads into Transform, a "transform" area with
    oiiotool steps into Transform Sequence."""
    loaded = []
    if entry.get("input_type") in INPUT_TYPES and not state.input.input1.strip():
        state.input.input_type = entry["input_type"]     # with an input, the file decides
    for area, value in (entry.get("areas") or {}).items():
        data = value.get("state") or {}
        if area == "letterbox":
            area, data = "transform", {"letterbox": data}
        elif area == "transform" and ("args" in data or "loaded_args" in data):
            area, data = "transform_sequence", {"steps": data}
        if areas is not None and area not in areas:
            continue
        if area == "input":
            for f in INPUT_AREA_FIELDS:
                if f in data:
                    setattr(state.input, f, int(data[f]) if f == "movie_first_frame" else str(data[f] or ""))
        elif area == "colour":
            for f in COLOUR_AREA_FIELDS:
                if f in data:
                    setattr(state.input, f, bool(data[f]) if f == "convert_colour" else str(data[f] or ""))
        elif area == "tokens":
            state.tokens = _flat(TokensState, data, name_tokens=_pairs)
        elif area == "encode":
            if data.get("output_type") in OUTPUT_TYPES:     # older super-presets have none
                state.input.output_type = data["output_type"]
            state.input.encode_preset = data.get("encode_preset", "")
            state.input.output_fps = str(data.get("output_fps", "") or "")
            state.input.seq_preset = data.get("seq_preset", "")
            state.input.alpha = data.get("alpha", "drop") if data.get("alpha") in ALPHA_MODES else "drop"
            state.input.write_timecode = bool(data.get("write_timecode", True))
            state.input.write_reelid = bool(data.get("write_reelid", True))
            state.input.alpha_premult = bool(data.get("alpha_premult", False))
            state.input.par_out = str(data.get("par_out") or "same")
            state.input.missing_frames = (data.get("missing_frames") if data.get("missing_frames")
                                          in MISSING_FRAMES else "hold")
        elif area == "transform_sequence":
            if "steps" in data:
                state.transform = _flat(TransformState, data["steps"])
            if "letterbox" in data:
                state.oiio_letterbox = _flat(LetterboxState, data["letterbox"])
        elif area == "transform":
            if "ffmpeg" in data:
                state.ff_transform = _flat(FFTransformState, data["ffmpeg"])
            if "letterbox" in data:
                state.letterbox = _flat(LetterboxState, data["letterbox"])
        elif area == "burnin":
            state.burn = _flat(BurnState, data, places=_places,
                               margins=lambda m: dict(DEFAULT_MARGINS, **(m or {})))
        elif area == "slate":
            state.slate = _flat(SlateState, data, fields=_pairs)
        elif area == "filmstrip":
            state.filmstrip = _flat(FilmstripState, data)
        elif area == "thumbnail":
            state.thumb = _flat(ThumbState, data)
        elif area == "intermediate":
            state.intermediate = _flat(IntermediateState, data)
        elif area == "channels":
            state.channels = _flat(ChannelsState, data)
        elif area == "metadata":
            state.metadata = _flat(MetadataState, data, tags=_pairs)
        else:
            continue
        if area not in loaded:
            loaded.append(area)
    return loaded


# --- OCIO config: what it offers, for the Color tab's dropdowns ----------------------------

@dataclass
class OcioInfo:
    colorspaces: List[str] = field(default_factory=list)
    roles: "OrderedDict[str, str]" = field(default_factory=OrderedDict)   # role -> colour space
    displays: "OrderedDict[str, List[str]]" = field(default_factory=OrderedDict)  # display -> views
    default_display: str = ""
    default_views: Dict[str, str] = field(default_factory=dict)          # display -> its default view
    looks: List[str] = field(default_factory=list)
    source: str = ""                  # "oiiotool" | "file"
    error: str = ""

    def views(self, display=""):
        """Views of a display; all views when no display is chosen."""
        if display in self.displays:
            return list(self.displays[display])
        seen = OrderedDict()
        for views in self.displays.values():
            for v in views:
                seen[v] = True
        return list(seen)

    def describe(self):
        if self.error:
            return self.error
        parts = ["{} colour spaces".format(len(self.colorspaces))]
        if self.displays:
            parts.append("{} display{}".format(len(self.displays), "s" if len(self.displays) > 1 else ""))
        if self.looks:
            parts.append("{} look{}".format(len(self.looks), "s" if len(self.looks) > 1 else ""))
        return ", ".join(parts)


_QUOTED_ITEM = re.compile(r'\s*(?:"([^"]*)"|([^,]+?))\s*(?:,|$)')


def _unquote(text):
    text = text.strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def _strip_marks(name):
    """'"ACES - ACEScg" (linear) (inactive)' -> 'ACES - ACEScg', flags."""
    flags = []
    while True:
        match = re.search(r"\s*\((\*|linear|inactive)\)\s*$", name)
        if not match:
            break
        flags.append(match.group(1))
        name = name[:match.start()]
    return _unquote(name), flags


def parse_colorconfiginfo(text):
    """OcioInfo from `oiiotool --colorconfiginfo` output."""
    info = OcioInfo(source="oiiotool")
    section, display, views_text = None, None, None

    def finish_views():
        if display is not None and views_text is not None:
            views = []
            for quoted, plain in _QUOTED_ITEM.findall(views_text):
                item = (quoted if quoted else plain).strip()
                if not item:
                    continue
                name, flags = _strip_marks(item if quoted == "" else '"{}"'.format(item))
                if quoted:
                    # the (*) mark follows the quotes: "name" (*)
                    name = quoted
                views.append(name)
                if "*" in flags:
                    info.default_views[display] = name
            # "(*)" after a quoted name is captured as its own item; drop it
            info.displays[display] = [v for v in views if v not in ("(*)", "*")]

    lines = text.splitlines()
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if line.startswith("Known color spaces"):
            section = "cs"
            continue
        if line.startswith("Known roles"):
            section = "roles"
            continue
        if line.startswith("Known looks"):
            section = "looks"
            continue
        if line.startswith("Known displays"):
            section = "displays"
            continue
        if line.startswith("Named transforms") or (line and not line.startswith(" ")):
            if section == "displays":
                finish_views()
                display, views_text = None, None
            section = None if not line.startswith("Known") else section
            continue
        if not stripped:
            continue
        if section == "cs" and stripped.startswith("- "):
            name, flags = _strip_marks(stripped[2:])
            if "inactive" not in flags:
                info.colorspaces.append(name)
        elif section == "roles" and stripped.startswith("- ") and " -> " in stripped:
            role, space = stripped[2:].split(" -> ", 1)
            info.roles[_unquote(role)] = _unquote(space)
        elif section == "looks" and stripped.startswith("- "):
            info.looks.append(_unquote(stripped[2:]))
        elif section == "displays":
            if stripped.startswith("- "):
                finish_views()
                display, flags = _strip_marks(stripped[2:])
                views_text = None
                info.displays[display] = []
                if "*" in flags:
                    info.default_display = display
            elif stripped.startswith("views:"):
                views_text = stripped[len("views:"):]
            elif views_text is not None:
                views_text += " " + stripped                 # word-wrapped view list
    if section == "displays":
        finish_views()
    return info


def parse_ocio_file(text):
    """OcioInfo read straight from a .ocio file (when oiiotool is not there): colour spaces,
    roles, looks, displays and their views. Covers the usual OCIO v1 / v2 layout, not every
    YAML form (inactive_colorspaces and active_displays / active_views are applied)."""
    info = OcioInfo(source="file")
    section, item = None, None
    inactive, active_displays, active_views = set(), [], []

    def inline_list(value):
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            return [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
        return [_unquote(v) for v in value.split(",") if v.strip()]

    display = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            key, _, value = line.partition(":")
            section, item, display = key.strip(), None, None
            if key == "inactive_colorspaces":
                inactive.update(inline_list(value))
            elif key == "active_displays":
                active_displays = inline_list(value)
            elif key == "active_views":
                active_views = inline_list(value)
            continue
        if section == "roles":
            key, _, value = line.partition(":")
            info.roles[_unquote(key)] = _unquote(value)
        elif section in ("colorspaces", "looks"):
            if line.startswith("- !<"):
                item = "cs" if section == "colorspaces" else "look"
                rest = line.split(">", 1)[1].strip()
                match = re.search(r"name:\s*([^,}]+)", rest)
                if match:
                    (info.colorspaces if item == "cs" else info.looks).append(_unquote(match.group(1)))
                    item = None
            elif item and line.startswith("name:"):
                (info.colorspaces if item == "cs" else info.looks).append(_unquote(line[5:]))
                item = None
        elif section == "displays":
            if line.endswith(":") and not line.startswith("-"):
                display = _unquote(line[:-1])
                info.displays[display] = []
            elif display and line.startswith("- !<View>"):
                match = re.search(r"name:\s*([^,}]+)", line)
                if match:
                    info.displays[display].append(_unquote(match.group(1)))
            elif display and line.startswith("- !<Views>"):
                info.displays[display] += inline_list(line.split(">", 1)[1])
    info.colorspaces = [c for c in info.colorspaces if c not in inactive]
    if active_displays:
        info.displays = OrderedDict((d, info.displays[d]) for d in active_displays if d in info.displays)
    if active_views:
        for d, views in info.displays.items():
            ordered = [v for v in active_views if v in views]
            info.displays[d] = ordered or views
    if info.displays:
        info.default_display = next(iter(info.displays))
        for d, views in info.displays.items():
            if views:
                info.default_views[d] = views[0]
    return info


_OCIO_CACHE = {}


def _no_window():
    """subprocess options so a console window does not flash up on Windows."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def scan_ocio(ocio, presets=None):
    """What an OCIO config offers. oiiotool --colorconfiginfo when oiiotool is there (what
    vfx-transcode itself will see, built-in ocio:// configs included); else a .ocio file read
    directly. Cached by path and modification time."""
    ocio = (ocio or "").strip() or DEFAULT_OCIO
    if ocio.startswith("ocio://"):
        path, stamp = ocio, ""
    else:
        path = resolve_path(ocio, app_dir())
        if not os.path.isfile(path):
            return OcioInfo(error="config not found: {}".format(path))
        stamp = os.path.getmtime(path)
    oiiotool = tool_path(presets, "oiiotool")
    key = (path, stamp, oiiotool)
    if key in _OCIO_CACHE:
        return _OCIO_CACHE[key]
    info = None
    if oiiotool:
        env = dict(os.environ, OCIO=path)
        try:
            result = subprocess.run([oiiotool, "--colorconfiginfo"], capture_output=True,
                                    text=True, env=env, timeout=60, **_no_window())
            info = parse_colorconfiginfo(result.stdout + "\n" + result.stderr)
            if not info.colorspaces:
                info = None
        except (OSError, subprocess.SubprocessError):
            info = None
    if info is None:
        if path.startswith("ocio://"):
            info = OcioInfo(error="a built-in config needs oiiotool to be listed")
        else:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    info = parse_ocio_file(fh.read())
            except OSError as exc:
                info = OcioInfo(error=str(exc))
    _OCIO_CACHE[key] = info
    return info
