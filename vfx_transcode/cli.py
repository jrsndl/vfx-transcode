"""Command line entry point.

Option names are unchanged from the original single-file script, so existing
.bat files keep working. 0/1 switches accept 0/1, true/false, or no value (= on).

Exit codes: 0 ok, 1 error, 2 movie written but some frames failed to convert and
were replaced by a neighbouring frame, 130 interrupted.
"""

import argparse
import datetime
import getpass
import logging
import re
import shutil
import tempfile
from fractions import Fraction
import os
import sys
from pathlib import Path, PureWindowsPath

from . import __version__
from .channels import pick_channels
from .aspect import output_par, par_display, parse_par, resolve_par
from dataclasses import replace
from .burnin import (BURN_PLACES, PLACE_NAMES, BurnStyle, parse_size, parse_style_option,
                     resolve_styles, style_from_values)
from .config import Config
from .deep import parse_zrange, plan_depth_view
from .errors import TranscodeError
from .ffmpeg import BurnContext, build_command, build_video_filter, encode, write_concat_list
from .oiio import convert_sequence
from .oiio_info import get_image_info, write_info_json
from . import metadata as md
from .presets import DEFAULT_PRESETS_FILE, load_presets
from .letterbox import (DEFAULT_FILL as LB_FILL, DEFAULT_LINE_COLOR as LB_LINE_COLOR,
                        DEFAULT_RATIO as LB_DEFAULT_RATIO, LetterboxSpec, parse_ratio, parse_rgba,
                        plan as letterbox_plan)
from .names import DEFAULT_VERSION_REGEX, compile_version_regex, name_tokens
from .sequence import fill_holes, find_sequence, format_frames
from .slate import (SLATE_STYLE_FIELDS, SlateSpec, apply_field_options, movie_size,
                    parse_thumbnail, pick_thumbnail, probe_frames, render_slate, slate_timecode,
                    style_values)
from .timecode import (fps_display, fps_from_attribs, frame_to_tc, nominal_fps,
                       offset_timecode, parse_fps, timecode_from_attribs, validate_timecode)
from .tools import app_dir, resolve_path, resolve_tool, run_tool, split_args

log = logging.getLogger("vfx_transcode")

EXIT_OK, EXIT_ERROR, EXIT_FRAMES_FAILED, EXIT_INTERRUPTED = 0, 1, 2, 130
DEFAULT_BURN_FONT = "./fonts/DroidSansMono.ttf"
DEFAULT_FPS = Fraction(24)


def _flag(value):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise argparse.ArgumentTypeError("expected 0 or 1, got {!r}".format(value))


def build_parser(presets=None, presets_error=None):
    if presets is not None:
        epilog = "Presets from {}\n\n{}".format(presets.path, presets.describe())
    else:
        epilog = "Encode presets could not be loaded: {}".format(presets_error)
    p = argparse.ArgumentParser(
        prog="vfx-transcode",
        description="Converts image sequences (OpenEXR via oiiotool + OCIO) to review movies "
                    "with ffmpeg. Version {}".format(__version__),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    def flag(group, name, default, help_text):
        group.add_argument(name, type=_flag, nargs="?", const=True, default=default,
                       metavar="0|1", help=help_text)

    p.add_argument("-i", default=None, help="Any file of the input sequence. Required.")
    p.add_argument("-o", default="", help="Output movie path. Default: <input folder>/<name>.<ext>")
    p.add_argument("-r", "--fps", default=None, metavar="RATE",
                   help="Frame rate: 24, 25, 23.976, 24000/1001. See -fps_source for how it "
                        "combines with the EXR header; 24 if neither gives one")
    p.add_argument("-fps_source", choices=("arg", "exr", "arg_only"), default="arg",
                   help="arg (default): -r wins, else the EXR header, else 24. "
                        "exr: the EXR header wins, else -r, else 24. "
                        "arg_only: -r, else 24; the header is ignored")
    p.add_argument("-par", default=None, metavar="PAR",
                   help="Input pixel aspect ratio: 1, 2, 1.33 (= 4/3), 4/3 ... See -par_source; "
                        "1 if neither this nor the EXR header gives one")
    p.add_argument("-par_source", choices=("arg", "exr", "arg_only"), default="arg",
                   help="arg (default): -par wins, else the EXR header's PixelAspectRatio, else 1. "
                        "exr: the header wins, else -par. arg_only: -par, else 1")
    p.add_argument("-par_out", default="same", metavar="same|square|PAR",
                   help="same (default): keep the pixels and tag the movie with the input pixel "
                        "aspect. square: reformat for square pixels (height scaled, -width kept). "
                        "PAR: reformat for that pixel aspect, e.g. 2")
    p.add_argument("-ci", "--colorin", default="ACEScg", metavar="",
                   help='EXR (input) colourspace, e.g. ACEScg, "Utility - Linear - sRGB". Default ACEScg')
    p.add_argument("-co", "--colorout", default="out_rec709", metavar="",
                   help="Output colourspace, e.g. out_rec709, out_srgb. Default out_rec709")
    p.add_argument("-display", default=None, metavar="NAME",
                   help='OCIO display, e.g. "sRGB - Display" (ACES 1.3+ configs). With -display '
                        "and/or -view, oiiotool --ociodisplay is used instead of -colorout; "
                        '"" means the config\'s default')
    p.add_argument("-view", default=None, metavar="NAME",
                   help='OCIO view, e.g. "ACES 1.0 - SDR Video". Empty or missing: the display\'s default view')
    p.add_argument("-looks", default="", metavar="LOOKS",
                   help="OCIO looks applied with the display/view transform, e.g. \"ACES 1.3 Reference Gamut Compression\"")
    p.add_argument("-ocio_info", action="store_true",
                   help="Print the colourspaces, displays and views of the OCIO config (oiiotool "
                        "--colorconfiginfo) and exit. -i is not needed")
    p.add_argument("-v", "--verbose", type=int, nargs="?", const=1, default=0, metavar="N",
                   help="1: show commands, 2: also show tool output")
    p.add_argument("--version", action="version", version=__version__)

    g = p.add_argument_group("tools")
    g.add_argument("-ffmpeg", default="./ffmpeg/ffmpeg.exe",
                   help="ffmpeg executable. ./ paths are relative to the application; falls back to PATH")
    g.add_argument("-oiio", default="./oiio/oiiotool.exe",
                   help="oiiotool executable. ./ paths are relative to the application; falls back to PATH")
    g.add_argument("-ocio", default="./ocio/aces_1.2cg/config.ocio",
                   help="OCIO config file, or an ocio:// built-in config URI")

    g = p.add_argument_group("EXR conversion (oiiotool)")
    g.add_argument("-oiio_threads", type=int, default=0,
                   help="Threads for oiiotool. 0 = number of cores")
    g.add_argument("-oiio_channel", default="",
                   help="Channels to use, e.g. R,G,B or diffuse.R,diffuse.G,diffuse.B. Default: "
                        "automatic, from channel_preferences in the presets file")
    flag(g, "-oiio_channel_use_first", False,
         "Skip the automatic search (channel_preferences in the presets file) and use the "
         "first layer")
    g.add_argument("-oiio_command", default="",
                   help="Extra oiiotool arguments, added after colorconvert and resize")
    flag(g, "-oiio_per_file", False,
         "Run one oiiotool per file instead of --parallel-frames batches")
    g.add_argument("-mezzanine", default="png", help="Intermediate file format. Default png")
    flag(g, "-mezzanine_16bit", False,
         "Write 16-bit intermediates (slower; useful for 10-bit presets)")
    g.add_argument("-width", type=int, default=1920,
                   help="Resize to this width. 0 = no resize. Default 1920")
    g.add_argument("-temp_location", choices=("user", "source"), default="user",
                   help="Where intermediate files go. user (default): a new folder in your "
                        "user temp folder (TEMP/TMP), usually a fast local disk. source: the "
                        "-temp folder next to the source files")
    g.add_argument("-temp", default=".temp_png_sequence",
                   help="With -temp_location source: folder name inside the input folder "
                        '("" = the input folder itself)')
    flag(g, "-temp_keep", False, "Keep the intermediate files")
    flag(g, "-write_exr_info", False,
         "Write the EXR header (of the first frame) as JSON next to that frame")
    g.add_argument("-deep_zrange", default="",
                   help='Deep EXRs without colour are shown as depth (Z). "near,far" sets the '
                        "depth range; default: measured on the first frame")
    g.add_argument("-missing_frames", choices=("hold", "error"), default="hold",
                   help="Holes in the sequence or failed frames: hold the previous frame "
                        "(default) or stop with an error")

    g = p.add_argument_group("timecode, metadata and burn-in")
    g.add_argument("-timecode_source", choices=("counter", "arg", "exr"), default=None,
                   help="counter: from the first frame number and fps (1001 at 24 fps = "
                        "00:00:41:17), the default. arg: -timecode. exr: the EXR header "
                        "timecode (falls back to counter if there is none)")
    g.add_argument("-timecode", default=None,
                   help="Start timecode HH:MM:SS:FF. Giving it implies -timecode_source arg")
    flag(g, "-timecode_from_counter", None,
         "Old switch: 1 = -timecode_source counter, 0 = -timecode_source arg")
    flag(g, "-output_timecode", True,
         "Write the timecode to the output (timecode track in mov/mp4/mxf, a tag otherwise)")
    g.add_argument("-metadata", action="append", default=[], metavar="KEY=VALUE",
                   help="Write a metadata tag; repeatable; KEY= removes a preset's tag. Values may use {clean_name} "
                        "{frame_start} {frame_end} {timecode} {fps} {preset} {date} {meta:ATTR} and the name "
                        "keywords {version_number} {namesplit1} ... (see -version_regex)")
    g.add_argument("-version_regex", default=DEFAULT_VERSION_REGEX, metavar="REGEX",
                   help="Finds the version in the sequence name for {version_string} (group 1, "
                        "e.g. 007) and {version_number} (7). Default %(default)s. The name "
                        "keywords {namesplit1}..{namesplit9} and {namesplit-1}..{namesplit-9} are "
                        "the name's parts split at _, counted from the start or the end; all of "
                        "them work in burn-ins, slate, metadata and -reelid")
    g.add_argument("-metadata_preset", default=None, metavar="NAME",
                   help='Metadata preset from the presets file (tags, copy rules, reel name and '
                        'timecode settings), or "none". The options below override its values')
    g.add_argument("-reelid_source", choices=("auto", "arg", "exr", "off"), default=None,
                   help="Reel name for conforming. auto (default): -reelid, else the EXR "
                        "attribute, else none. arg: -reelid only. exr: the EXR attribute, else "
                        "-reelid. off: no reel name")
    g.add_argument("-reelid", default=None, metavar="TEXT",
                   help="Explicit reel name; may use {clean_name} and the other -metadata keywords")
    g.add_argument("-reelid_attr", default=None, metavar="NAME",
                   help="EXR header attribute holding the reel name. Default owner")
    g.add_argument("-reelid_pattern", default=None, metavar="REGEX",
                   help="Regular expression applied to the EXR value; its first group (or the "
                        "whole match) is the reel name. Default: the value minus a trailing "
                        "[first-last] frame range")
    g.add_argument("-metadata_copy", default=None, metavar="SPEC",
                   help='Copy EXR header attributes to the output: "all", or key patterns '
                        'with * and "-" to exclude, e.g. "all,-husk:*" or "Artist,capDate". '
                        "Default: none")
    g.add_argument("-burn_preset", default=None, metavar="NAME",
                   help='Burn-in preset from the presets file (see below), or "none". '
                        "The -burn_* options below override its values")
    g.add_argument("-burn_size", default=None, metavar="SIZE",
                   help="Text size for all places: pixels (40), percent of the picture height "
                        "(1.5%%) or h/N (h/75, the older form). Default: the preset's, else 1.33%%")
    g.add_argument("-burn_color", default=None, metavar="COLOR",
                   help="Text colour for all places: white, yellow, #ffcc00 ... Default: the preset's, else white")
    g.add_argument("-burn_opacity", default=None, metavar="0-1",
                   help="Opacity for all places (text, shadow and box): 1 solid, 0 invisible. "
                        "Default: the preset's, else 1")
    flag(g, "-burn_box", None, "Black box behind the text, all places. Default: the preset's, else 0")
    g.add_argument("-burn_style", action="append", default=[], metavar="PLACE:KEY=VALUE,...",
                   help="Style for one place (tl tc tr bl bc br, or all), e.g. "
                        '"tc:color=yellow,size=40,box=1". Repeatable; wins over everything else')
    g.add_argument("-burn_font", default=None,
                   help="Burn-in font file. Default: the burn-in preset's font, else "
                        + DEFAULT_BURN_FONT)
    for place in BURN_PLACES:
        g.add_argument("-burn_" + place, default=None, metavar="TEXT",
                       help='Burn-in {} text{} ("" clears the preset\'s). Keywords: {{clean_name}} '
                            "{{current_frame}} {{frame_start}} {{frame_end}} {{tc}} {{timecode}} "
                            "{{fps}} {{date}} {{preset}} {{meta:ATTR}} {{version_number}} {{namesplit1}} ...".format(
                                PLACE_NAMES[place], ", centred" if place in ("tc", "bc") else ""))

    g = p.add_argument_group("slate")
    g.add_argument("-slate", default=None, metavar="NAME",
                   help='Slate preset from the presets file, or "none" (default: the file\'s '
                        "default_slate). The slate is one frame before the first picture: frame "
                        "start-1, timecode start-1 (never below 00:00:00:00), same size and pixel "
                        "aspect as the pictures")
    g.add_argument("-slate_title", default=None, metavar="TEXT",
                   help="Slate title; keywords as in the fields. Default: the preset's, else {clean_name}")
    g.add_argument("-slate_subtitle", default=None, metavar="TEXT", help="Smaller line under the title")
    g.add_argument("-slate_field", action="append", default=[], metavar="LABEL=TEXT",
                   help='Add or replace a slate row, e.g. "Notes=first comp pass"; "LABEL=" '
                        "removes one. Repeatable. Keywords: {clean_name} {frame_start} {frame_end} "
                        "{frames} {timecode} {timecode_end} {duration} {fps} {width} {height} {par} "
                        "{colour} {reel} {preset} {date} {user} {input} {meta:ATTR} {version_string} "
                        "{version_number} {namesplit1}..{namesplit-9}")
    g.add_argument("-slate_thumbnail", default=None, metavar="middle|first|last|none|FRAME",
                   help="Shot thumbnail on the slate. Default: the preset's, else middle")
    g.add_argument("-slate_scale", default=None, metavar="PERCENT",
                   help="Size of the whole slate layout (text, spacing, thumbnail): 100%% "
                        "(default) fills the picture, 50%% is half as big, centred")
    g.add_argument("-slate_valign", default=None, metavar="center|top",
                   help="Vertical placement of the slate content. Default center")
    g.add_argument("-slate_title_size", default=None, metavar="SIZE",
                   help="Title text size: percent of the picture height (5%% default) or pixels")
    g.add_argument("-slate_subtitle_size", default=None, metavar="SIZE",
                   help="Subtitle text size (3.2%% default)")
    g.add_argument("-slate_size", default=None, metavar="SIZE",
                   help="Row text size, labels and values (2.5%% default)")
    g.add_argument("-slate_value_color", default=None, metavar="COLOR",
                   help="Colour of the row values (white default)")
    g.add_argument("-slate_label_color", default=None, metavar="COLOR",
                   help="Colour of the row labels, the field names (#7a7a7a default)")
    g.add_argument("-slate_style", action="append", default=[], metavar="KEY=VALUE,...",
                   help="Any slate style setting, e.g. \"title_color=yellow,background=#000000\". "
                        "Keys: " + ", ".join(k for k in SLATE_STYLE_FIELDS if k != "font") +
                        " (color = title and value colour). Repeatable; wins over the options above")
    flag(g, "-slate_rule", None, "Line under the slate title. Default: the preset's, else 0")
    flag(g, "-slate_burnin", False, "Draw the burn-ins on the slate frame too")

    g = p.add_argument_group("letterbox")
    flag(g, "-letterbox", False, "Cover part of the picture with bars to show -letterbox_ratio. "
                                 "Not drawn on the slate; burn-ins go on top")
    g.add_argument("-letterbox_ratio", default=None, metavar="RATIO",
                   help="Target ratio: 2.39, 2.39:1, 1.85, 16:9 ... Wider than the movie's display "
                        "ratio: bars top and bottom (letterbox); narrower: bars left and right "
                        "(pillarbox). Default " + LB_DEFAULT_RATIO)
    g.add_argument("-letterbox_color", default=None, metavar="R,G,B,A",
                   help="Bar colour, 0-255 per channel; alpha below 255 makes the bars see-through, "
                        "e.g. 0,0,0,160. Default 0,0,0,255")
    g.add_argument("-letterbox_line", type=int, default=None, metavar="PIXELS",
                   help="Thickness of a line on the inner edge of each bar, in movie pixels. "
                        "0 (default) = no line")
    g.add_argument("-letterbox_line_color", default=None, metavar="R,G,B,A",
                   help="Line colour, 0-255 per channel. Default 255,255,255,255")

    g = p.add_argument_group("encoding (ffmpeg)")
    flag(g, "-skipffmpeg", False, "Only convert the EXRs; implies -temp_keep 1")
    g.add_argument("-preset", default=None,
                   help="Encode preset from the presets file, see the list below. "
                        "Default: the file's \"default\"")
    g.add_argument("-presets_file", default=DEFAULT_PRESETS_FILE,
                   help="JSON file with the encode, burn-in, metadata and slate presets and channel "
                        "preferences. Default ./presets.json (relative to the application)")
    g.add_argument("-ff_filter", default="",
                   help="Extra ffmpeg video filters, inserted before the burn-ins")
    g.add_argument("-ff_encode", default="",
                   help="Replaces the preset's codec arguments (no colour tags are added). "
                        "Use {videofilter} where the -vf filter goes")
    g.add_argument("-ff_out_ext", default=None, help="Output extension. Default: from the preset")
    return p


def timecode_source_from_args(ns, metadata_preset=None):
    if ns.timecode_source:
        return ns.timecode_source
    if ns.timecode is not None or ns.timecode_from_counter is False:
        return "arg"
    if metadata_preset is not None and metadata_preset.timecode_source:
        return metadata_preset.timecode_source
    return "counter"


def _pick(cli_value, preset, attr, default):
    """Command line (if given) > metadata preset (if it sets the value) > default."""
    if cli_value is not None:
        return cli_value
    if preset is not None and getattr(preset, attr) is not None:
        return getattr(preset, attr)
    return default


SLATE_OPTIONS = ("slate_title", "slate_subtitle", "slate_thumbnail", "slate_size", "slate_scale",
                 "slate_valign", "slate_title_size", "slate_subtitle_size", "slate_value_color",
                 "slate_label_color")


def parse_slate_style(items):
    """-slate_style "key=value,key=value" (repeatable) -> {key: value}."""
    values = {}
    for item in items:
        for pair in filter(None, (p.strip() for p in item.split(","))):
            key, sep, value = pair.partition("=")
            key = key.strip().lower()
            if not sep or key not in SLATE_STYLE_FIELDS or key == "font":
                raise TranscodeError('-slate_style "{}": use KEY=VALUE with KEY one of {}.'.format(
                    item, ", ".join(k for k in SLATE_STYLE_FIELDS if k != "font")))
            values[key] = value.strip()
    return values


def slate_from_args(ns, presets):
    """The slate to draw (preset + command-line overrides), or None."""
    spec = presets.get_slate(ns.slate if ns.slate is not None else presets.default_slate)
    given = [o for o in SLATE_OPTIONS if getattr(ns, o) is not None] + \
        (["slate_field"] if ns.slate_field else []) + (["slate_burnin"] if ns.slate_burnin else []) + \
        (["slate_style"] if ns.slate_style else []) + (["slate_rule"] if ns.slate_rule is not None else [])
    if spec is None:
        if given and ns.slate is None:
            spec = SlateSpec()          # -slate_field etc. alone: a plain slate
        else:
            if given:
                log.warning("-%s ignored: -slate none.", given[0])
            return None
    changes = {"fields": apply_field_options(spec.fields, ns.slate_field),
               "burnin": bool(ns.slate_burnin)}
    if ns.slate_title is not None:
        changes["title"] = ns.slate_title
    if ns.slate_subtitle is not None:
        changes["subtitle"] = ns.slate_subtitle
    if ns.slate_thumbnail is not None:
        changes["thumbnail"] = parse_thumbnail(ns.slate_thumbnail)
    options = {"size": ns.slate_size, "title_size": ns.slate_title_size,
               "subtitle_size": ns.slate_subtitle_size, "value_color": ns.slate_value_color,
               "label_color": ns.slate_label_color, "scale": ns.slate_scale,
               "valign": ns.slate_valign, "rule": ns.slate_rule}
    changes.update(style_values(options, "command line"))
    changes.update(style_values(parse_slate_style(ns.slate_style), "-slate_style"))
    if spec.font:
        changes["font"] = resolve_path(spec.font, app_dir())
    return replace(spec, **changes)


LETTERBOX_OPTIONS = ("letterbox_ratio", "letterbox_color", "letterbox_line", "letterbox_line_color")


def letterbox_from_args(ns):
    given = [o for o in LETTERBOX_OPTIONS if getattr(ns, o) is not None]
    if not ns.letterbox:
        if given:
            log.warning("-%s ignored: the letterbox is off (-letterbox 1 turns it on).", given[0])
        return None
    ratio_text = ns.letterbox_ratio or LB_DEFAULT_RATIO
    line = ns.letterbox_line or 0
    if line < 0:
        raise TranscodeError("-letterbox_line must be 0 or more pixels.")
    return LetterboxSpec(
        ratio=parse_ratio(ratio_text), ratio_text=ratio_text.strip(),
        fill=parse_rgba(ns.letterbox_color, "-letterbox_color") if ns.letterbox_color else LB_FILL,
        line=line,
        line_color=(parse_rgba(ns.letterbox_line_color, "-letterbox_line_color")
                    if ns.letterbox_line_color else LB_LINE_COLOR))


def config_from_args(ns, presets):
    try:
        fps_arg = parse_fps(ns.fps) if ns.fps else None
        timecode = validate_timecode(ns.timecode or "01:00:00:00")
    except ValueError as exc:
        raise TranscodeError(str(exc))
    if ns.reelid_pattern:
        try:
            re.compile(ns.reelid_pattern)
        except re.error as exc:
            raise TranscodeError("-reelid_pattern is not a valid regular expression: {}".format(exc))
    compile_version_regex(ns.version_regex)
    if ns.par_out.strip().lower() not in ("same", "square"):
        parse_par(ns.par_out, "-par_out")
    preset = presets.get(ns.preset or presets.default)
    metadata_preset = presets.get_metadata_preset(
        ns.metadata_preset if ns.metadata_preset is not None else presets.default_metadata)
    burn_preset = presets.get_burnin(ns.burn_preset if ns.burn_preset is not None
                                     else presets.default_burnin)
    burn = {c: (burn_preset.corners.get(c, "") if burn_preset else "") for c in BURN_PLACES}
    for place in BURN_PLACES:
        value = getattr(ns, "burn_" + place)
        if value is not None:          # given on the command line, even "" (clears the place)
            burn[place] = value
    cli_style = style_from_values({"color": ns.burn_color, "opacity": ns.burn_opacity,
                                   "size": ns.burn_size, "box": ns.burn_box}, "command line")
    cli_places = {}
    for spec in ns.burn_style:
        places, style = parse_style_option(spec)
        for place in places:
            cli_places[place] = cli_places.get(place, BurnStyle()).over(style)
    burn_styles = resolve_styles(burn_preset.style if burn_preset else None,
                                 burn_preset.place_styles if burn_preset else None,
                                 cli_style, cli_places)
    burn_size = burn_styles["tl"].size
    burn_font = ns.burn_font or (burn_preset.font if burn_preset else None) or DEFAULT_BURN_FONT
    if ns.deep_zrange:
        parse_zrange(ns.deep_zrange)   # validate early
    slate = slate_from_args(ns, presets)
    letterbox = letterbox_from_args(ns)
    base = app_dir()
    skip_ffmpeg = ns.skipffmpeg
    return Config(
        input=ns.i,
        output=ns.o,
        fps=fps_arg or DEFAULT_FPS,
        fps_arg=fps_arg,
        fps_source=ns.fps_source,
        par_arg=parse_par(ns.par) if ns.par else None,
        par_source=ns.par_source,
        par_out=ns.par_out,
        verbose=ns.verbose or 0,
        oiiotool=ns.oiio,
        ffmpeg=ns.ffmpeg,
        ocio=ns.ocio if ns.ocio.startswith("ocio://") else resolve_path(ns.ocio, base),
        colorin=ns.colorin,
        colorout=ns.colorout,
        ocio_display=ns.display,
        ocio_view=ns.view,
        ocio_looks=ns.looks,
        oiio_threads=max(0, ns.oiio_threads),
        oiio_channel=[c for c in ns.oiio_channel.split(",") if c.strip()],
        oiio_channel_use_first=ns.oiio_channel_use_first,
        channel_preferences=presets.channel_preferences,
        oiio_command=split_args(ns.oiio_command),
        oiio_per_file=ns.oiio_per_file,
        mezzanine=ns.mezzanine,
        mezzanine_16bit=ns.mezzanine_16bit,
        width=max(0, ns.width),
        temp=ns.temp,
        temp_location=ns.temp_location,
        temp_keep=ns.temp_keep or skip_ffmpeg,
        write_exr_info=ns.write_exr_info,
        missing_frames=ns.missing_frames,
        deep_zrange=ns.deep_zrange,
        timecode=timecode,
        timecode_source=timecode_source_from_args(ns, metadata_preset),
        output_timecode=ns.output_timecode,
        metadata_copy=_pick(ns.metadata_copy, metadata_preset, "copy", ""),
        metadata=md.parse_pairs(ns.metadata),
        metadata_preset=metadata_preset.name if metadata_preset else "",
        preset_tags=dict(metadata_preset.tags) if metadata_preset else {},
        reelid=_pick(ns.reelid, metadata_preset, "reelid", ""),
        reelid_source=_pick(ns.reelid_source, metadata_preset, "reelid_source", "auto"),
        reelid_attr=(_pick(ns.reelid_attr, metadata_preset, "reelid_attr", "owner").strip() or "owner"),
        reelid_pattern=_pick(ns.reelid_pattern, metadata_preset, "reelid_pattern", ""),
        burn_preset=burn_preset.name if burn_preset else "",
        burn_size=burn_size,
        burn_font=resolve_path(burn_font, base),
        burn=burn,
        burn_styles=burn_styles,
        slate=slate,
        letterbox=letterbox,
        version_regex=ns.version_regex,
        skip_ffmpeg=skip_ffmpeg,
        ff_filter=ns.ff_filter,
        preset=preset,
        ff_encode=ns.ff_encode,
        # the container: -ff_out_ext, else the -o file's extension, else the preset's
        ff_out_ext=(ns.ff_out_ext or Path(ns.o).suffix or preset.ext).lstrip("."),
    )


TEMP_ROOT_NAME = "vfx-transcode"


def prepare_work_dir(cfg, seq):
    """Return (folder for intermediates, whether this run created it)."""
    if cfg.temp_location == "user":
        root = Path(tempfile.gettempdir()) / TEMP_ROOT_NAME
        root.mkdir(parents=True, exist_ok=True)
        prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", seq.clean_name or "sequence")[:60] + "_"
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root))), True

    temp = cfg.temp.strip()
    if not temp:
        return seq.folder, False
    rel = Path(temp)
    # Checked with Windows rules on every OS: "C:x", "/x" and "\\x" all leave the input
    # folder, although Windows does not call "/x" absolute.
    win = PureWindowsPath(temp)
    if rel.is_absolute() or rel.anchor or win.anchor or ".." in win.parts:
        raise TranscodeError('-temp must be a folder name inside the input folder, got "{}".'.format(temp))
    work_dir = seq.folder / rel
    created = not work_dir.exists()
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir, created


def cleanup(files, work_dir, created_dir, whole_folder=False):
    """Delete this run's files. whole_folder: the folder is private to this run (user temp),
    so remove it with everything in it, including partial files from failed frames."""
    if whole_folder:
        shutil.rmtree(str(work_dir), ignore_errors=True)
        return
    for path in files:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not delete %s: %s", path, exc)
    if created_dir:
        try:
            work_dir.rmdir()   # only if empty
        except OSError:
            pass


HDR_OR_WIDE = re.compile(r"(PQ|HLG|2100|2020|P3|XYZ|ST[- ]?2084|HDR)", re.I)


def describe_colour(cfg):
    if cfg.display_mode:
        text = '{} -> display "{}", view "{}"'.format(
            cfg.colorin, cfg.ocio_display or "(default)", cfg.ocio_view or "(default)")
        return text + (', looks "{}"'.format(cfg.ocio_looks) if cfg.ocio_looks else "")
    return "{} -> {}".format(cfg.colorin, cfg.colorout)


def show_ocio_info(ns):
    """-ocio_info: print what the OCIO config offers."""
    base = app_dir()
    ocio = ns.ocio if ns.ocio.startswith("ocio://") else resolve_path(ns.ocio, base)
    cfg = Config(input="", ocio=ocio, oiiotool=resolve_tool(ns.oiio, base, "oiiotool"))
    setup_ocio(cfg)
    result = run_tool([cfg.oiiotool, "--colorconfiginfo"], "oiiotool --colorconfiginfo", ns.verbose or 0)
    print("OCIO config: {}".format(os.environ.get("OCIO", ocio)))
    print((result.stdout + result.stderr).strip())
    return EXIT_OK


def setup_ocio(cfg):
    if cfg.ocio.startswith("ocio://") or os.path.isfile(cfg.ocio):
        os.environ["OCIO"] = cfg.ocio
    elif os.environ.get("OCIO"):
        log.warning('OCIO config "%s" not found, using $OCIO=%s', cfg.ocio, os.environ["OCIO"])
    else:
        raise TranscodeError('OCIO config "{}" not found.'.format(cfg.ocio))


FPS_ORDER = {"arg": ("arg", "exr"), "exr": ("exr", "arg"), "arg_only": ("arg",)}


def resolve_fps(cfg, info):
    """Frame rate and where it came from, following -fps_source; 24 as the last resort."""
    header_fps, header_key = None, None
    if cfg.fps_source != "arg_only" and info:
        for sub in info:
            header_fps, header_key = fps_from_attribs(sub.get("attribs", {}))
            if header_fps is not None:
                break
    chosen = None
    for source in FPS_ORDER[cfg.fps_source]:
        if source == "arg" and cfg.fps_arg is not None:
            chosen = (cfg.fps_arg, "-r")
            break
        if source == "exr" and header_fps is not None:
            chosen = (header_fps, "EXR header " + header_key)
            break
    if chosen is None:
        chosen = (DEFAULT_FPS, "default")
    if header_fps is not None and cfg.fps_arg is not None and header_fps != cfg.fps_arg:
        unused = ("EXR header {}".format(fps_display(header_fps)) if chosen[1] == "-r"
                  else "-r {}".format(fps_display(cfg.fps_arg)))
        log.info("Frame rate: %s ignored (-fps_source %s)", unused, cfg.fps_source)
    if cfg.fps_source == "exr" and header_fps is None:
        log.info("Frame rate: %s", "no rate in the EXR header" if info else
                 "input is not EXR, so there is no header rate")
    return chosen


def today():
    return datetime.date.today().isoformat()


def resolve_timecode(cfg, seq, info, header_frame=None):
    """Start timecode of the movie and where it came from."""
    fps_int = nominal_fps(cfg.fps)
    if cfg.timecode_source == "arg":
        return cfg.timecode, "-timecode"
    if cfg.timecode_source == "exr":
        tc = None
        if info is None:
            log.warning("-timecode_source exr needs EXR input; using the frame counter.")
        else:
            tc = next((t for t in (timecode_from_attribs(sub.get("attribs", {})) for sub in info)
                       if t), None)
            if tc is None:
                log.warning("No timecode in the EXR header; using the frame counter.")
        if tc:
            frame = header_frame if header_frame is not None else seq.frame_of(cfg.input)
            if frame is not None and frame != seq.start:
                # the header we read belongs to a later frame, not the first one
                tc = offset_timecode(tc, seq.start - frame, fps_int)
            return tc, "EXR header"
    return frame_to_tc(seq.start, fps_int), "frame counter"


def metadata_context(cfg, seq, timecode):
    context = dict(cfg.name_tokens)
    context.update({"clean_name": seq.clean_name, "frame_start": seq.start, "frame_end": seq.end,
                    "timecode": timecode, "fps": fps_display(cfg.fps), "preset": cfg.preset.name,
                    "date": today(), "meta": cfg.header_attribs})
    return context


def resolve_reel_name(cfg, seq, info, timecode):
    """Reel name and where it came from, following -reelid_source; ("", reason) for none."""
    if cfg.reelid_source == "off":
        return "", "off"
    explicit = md.expand(cfg.reelid, metadata_context(cfg, seq, timecode)).strip()
    header = ""
    if info:
        for sub in info:
            header = md.header_value(sub.get("attribs", {}), cfg.reelid_attr)
            if header:
                break
    for source in md.REEL_ORDER[cfg.reelid_source]:
        if source == "arg" and explicit:
            return explicit, "-reelid"
        if source == "exr" and header:
            reel = md.clean_reel_name(header, cfg.reelid_pattern)
            if reel != header:
                log.info("Reel name: EXR %s is %r", cfg.reelid_attr, header)
            if reel:
                return reel, "EXR header " + cfg.reelid_attr
    if cfg.reelid_source == "arg":
        log.warning("-reelid_source arg but no -reelid given; no reel name written.")
    elif cfg.reelid_source == "exr":
        log.warning('No "%s" attribute in the EXR header and no -reelid; no reel name written.',
                    cfg.reelid_attr)
    return "", "none found"


def output_metadata(cfg, seq, info, timecode):
    copied = {}
    if cfg.metadata_copy:
        if info is None:
            log.warning("-metadata_copy only works with EXR input; nothing copied.")
        else:
            attribs = {}
            for sub in reversed(info):          # first subimage wins
                attribs.update(sub.get("attribs", {}))
            copied = md.select(md.header_metadata(attribs), cfg.metadata_copy)
    result = md.build(copied, cfg.metadata, metadata_context(cfg, seq, timecode),
                      [cfg.preset.metadata, cfg.preset_tags])
    if result:
        log.info("Metadata: %s%s", ", ".join(result),
                 " (preset {})".format(cfg.metadata_preset) if cfg.metadata_preset else "")
    return result


def run(cfg):
    seq = find_sequence(cfg.input)
    cfg.name_tokens = name_tokens(seq.clean_name, cfg.version_regex)
    if cfg.name_tokens["version_string"]:
        log.info("Version: %s (from %s)", cfg.name_tokens["version_string"], seq.clean_name)
    else:
        log.debug("No version in %s (-version_regex %s)", seq.clean_name, cfg.version_regex)
    log.info("Input: %s  frames %s (%d)", seq.folder / seq.name(seq.start),
             format_frames(seq.frames), len(seq.frames))
    missing = seq.missing
    if missing:
        message = "Missing frames: {}".format(format_frames(missing))
        if cfg.missing_frames == "error":
            raise TranscodeError(message)
        log.warning("%s (holding the previous frame)", message)

    base = app_dir()
    if not cfg.skip_ffmpeg:
        cfg.ffmpeg = resolve_tool(cfg.ffmpeg, base, "ffmpeg")
    work_dir, created_dir = prepare_work_dir(cfg, seq)
    log.info("Intermediate files: %s", work_dir)
    created_files = []
    exit_code = EXIT_OK
    info = None
    try:
        if seq.is_exr:
            cfg.oiiotool = resolve_tool(cfg.oiiotool, base, "oiiotool")
            setup_ocio(cfg)
            # header of the first frame: channels, fps, timecode, reel and {meta:...} all
            # come from there
            cfg.header_file = str(seq.path(seq.start))
            log.info("Colour: %s", describe_colour(cfg))
            if cfg.display_mode and HDR_OR_WIDE.search(cfg.ocio_display or ""):
                log.warning('Display "%s": the movie is tagged Rec.709 (bt709 primaries); set the '
                            "right colour tags with -ff_encode for HDR / P3 deliveries.", cfg.ocio_display)
            info = get_image_info(cfg.oiiotool, cfg.header_file, cfg.verbose)
            cfg.header_attribs = md.header_attribs(info)
            if cfg.write_exr_info:
                write_info_json(info, cfg.header_file + ".json")
            pick = pick_channels(info, cfg.oiio_channel, cfg.oiio_channel_use_first,
                                 cfg.channel_preferences or None)
            depth = plan_depth_view(cfg, info, pick)
            if depth is None:
                log.info("Channels: %s (%s%s)", pick, pick.reason,
                         ", subimage {}".format(pick.subimage) if pick.subimage else "")
            if not cfg.mezzanine_16bit and cfg.preset.ten_bit and not cfg.ff_encode:
                log.info("Tip: -mezzanine_16bit 1 gives the 10-bit preset real 10-bit input.")

            if work_dir == seq.folder:
                clash = [seq.name(f, cfg.mezzanine_ext) for f in seq.frames
                         if (work_dir / seq.name(f, cfg.mezzanine_ext)).exists()]
                if clash:
                    raise TranscodeError("-temp is empty and intermediate files would overwrite "
                                         "existing files, e.g. {}".format(clash[0]))

            result = convert_sequence(cfg, seq, pick, work_dir, depth)
            # every target, so partial files of failed frames are removed as well
            created_files += [work_dir / seq.name(f, cfg.mezzanine_ext) for f in seq.frames]
            if result.failed:
                message = "Frames failed to convert: {}".format(format_frames(result.failed))
                if cfg.missing_frames == "error" or not result.outputs:
                    raise TranscodeError(message)
                log.warning("%s (replaced by neighbouring frames)", message)
                exit_code = EXIT_FRAMES_FAILED
            available = result.outputs
            scale_width = 0          # oiiotool already resized
        else:
            available = {f: seq.path(f) for f in seq.frames}
            scale_width = cfg.width

        if cfg.skip_ffmpeg:
            log.info("Skipping ffmpeg; intermediate files are in %s%s", work_dir,
                     " (no slate: ffmpeg draws it)" if cfg.slate else "")
            return exit_code

        cfg.fps, fps_source = resolve_fps(cfg, info)
        log.info("Frame rate: %s (%s)", fps_display(cfg.fps), fps_source)
        cfg.par_in, par_source = resolve_par(cfg)
        cfg.par_out_value = output_par(cfg.par_in, cfg.par_out)
        if par_source != "default" or cfg.par_out_value != 1:
            change = cfg.par_out_value / cfg.par_in
            log.info("Pixel aspect: %s (%s) -> output %s%s", par_display(cfg.par_in), par_source,
                     par_display(cfg.par_out_value),
                     "" if change == 1 else ", height x{:.4g}".format(float(change)))
            if cfg.par_out_value != 1 and any(cfg.burn.values()):
                log.warning("Burn-ins are drawn in stored pixels; with output pixel aspect %s they "
                            "look stretched on playback. -par_out square avoids that.",
                            par_display(cfg.par_out_value))
        timecode, tc_source = resolve_timecode(cfg, seq, info, seq.start)
        movie_tc = timecode
        if cfg.slate:
            fps_int = nominal_fps(cfg.fps)
            movie_tc = slate_timecode(timecode, fps_int)
            if movie_tc == timecode:
                timecode = offset_timecode(timecode, 1, fps_int)
                log.warning("Slate: the start timecode is %s, so the slate keeps it and the "
                            "pictures start at %s.", movie_tc, timecode)
        log.info("Timecode: %s (%s)%s", timecode, tc_source,
                 ", slate {}".format(movie_tc) if cfg.slate else "")
        metadata = output_metadata(cfg, seq, info, timecode)
        reel_name, reel_source = resolve_reel_name(cfg, seq, info, timecode)
        if reel_name:
            log.info("Reel name: %s (%s)", reel_name, reel_source)
            ext = cfg.ff_out_ext.lower()
            if ext == "mov" and not cfg.output_timecode:
                log.warning("Reel name: -output_timecode 0 means no timecode track, so the reel "
                            "name is only a metadata tag that conform tools may not read.")
            elif ext not in ("mov", "mxf"):
                log.warning("Reel name: .%s keeps it only as a reel_name metadata tag; use mov "
                            "or mxf if a conform tool has to read it.", ext)

        name = seq.clean_name or seq.folder.name or "output"
        list_file = work_dir / "{}.ffconcat.txt".format(name)
        created_files.append(list_file)
        frame_files = fill_holes(seq.full_range, available)
        fmt = probe_frames(cfg.ffmpeg, available, cfg.verbose) if (cfg.slate or cfg.letterbox) else None
        if cfg.letterbox:
            width, height = movie_size(fmt, cfg.par_in, cfg.par_out_value, scale_width)
            cfg.letterbox_plan = letterbox_plan(cfg.letterbox, width, height, cfg.par_out_value)
            describe_letterbox(cfg.letterbox, cfg.letterbox_plan, width, height)
        if cfg.slate:
            slate_file = work_dir / "{}.slate{}".format(name, Path(available[min(available)]).suffix)
            created_files.append(slate_file)
            make_slate(cfg, seq, available, slate_file, timecode, reel_name, scale_width, fmt)
            frame_files = [slate_file] + list(frame_files)
        write_concat_list(frame_files, cfg.fps, list_file)

        if any(cfg.burn.values()):
            log.info("Burn-ins%s:", " (preset {})".format(cfg.burn_preset) if cfg.burn_preset else "")
            for place in BURN_PLACES:
                if cfg.burn.get(place):
                    st = cfg.burn_styles.get(place)
                    log.info("  %s %r  [%s]", place, cfg.burn[place], st.describe() if st else "")
        ctx = BurnContext(seq.clean_name, seq.start, seq.end, timecode, cfg.fps,
                          cfg.preset.name, today(), cfg.header_attribs,
                          lead_frames=1 if cfg.slate else 0, movie_timecode=movie_tc,
                          skip_lead=not (cfg.slate and cfg.slate.burnin), tokens=cfg.name_tokens)
        video_filter = build_video_filter(cfg, ctx, scale_width)
        output = Path(cfg.output) if cfg.output else seq.folder / "{}.{}".format(name, cfg.ff_out_ext)
        output.parent.mkdir(parents=True, exist_ok=True)
        encode(cfg, build_command(cfg, list_file, output, video_filter, movie_tc, metadata,
                                  reel_name))
        log.info("Done: %s", output)
        return exit_code
    finally:
        if cfg.temp_keep:
            log.info("Intermediate files kept in %s", work_dir)
        else:
            cleanup(created_files, work_dir, created_dir,
                    whole_folder=cfg.temp_location == "user" and created_dir)


def current_user():
    try:
        return getpass.getuser()
    except Exception:          # no login name (some services); not worth failing for
        return ""


def slate_context(cfg, seq, timecode, reel_name, width, height):
    fps_int = nominal_fps(cfg.fps)
    count = seq.end - seq.start + 1
    context = metadata_context(cfg, seq, timecode)
    context.update(frames=count, timecode_end=offset_timecode(timecode, count - 1, fps_int),
                   duration=frame_to_tc(count, fps_int), width=width, height=height,
                   par=par_display(cfg.par_out_value),
                   colour=describe_colour(cfg) if seq.is_exr else "", reel=reel_name,
                   user=current_user(), input=Path(cfg.input).name)
    return context


def describe_letterbox(spec, lb_plan, width, height):
    if lb_plan.kind == "none":
        log.info("Letterbox %s: the %dx%d movie already has that ratio, no bars.",
                 spec.ratio_text, width, height)
        return
    where = "top and bottom" if lb_plan.kind == "letterbox" else "left and right"
    log.info("Letterbox %s: %s, bars of %d px %s%s", spec.ratio_text, lb_plan.kind, lb_plan.bar,
             where, ", line {} px".format(min(spec.line, lb_plan.bar)) if spec.line else "")


def make_slate(cfg, seq, available, out_path, timecode, reel_name, scale_width, fmt=None):
    """Draw the slate frame next to the intermediates, matching their format."""
    spec = cfg.slate
    fmt = fmt or probe_frames(cfg.ffmpeg, available, cfg.verbose)
    width, height = movie_size(fmt, cfg.par_in, cfg.par_out_value, scale_width)
    thumb = pick_thumbnail(spec.thumbnail, seq.frames, available)
    log.info("Slate%s: frame %d, %dx%d %s%s", " (preset {})".format(spec.name) if spec.name else "",
             seq.start - 1, fmt.width, fmt.height, fmt.pix_fmt,
             ", thumbnail of frame {}".format(thumb) if thumb is not None else "")
    context = slate_context(cfg, seq, timecode, reel_name, width, height)
    render_slate(cfg, spec, context, md.expand, fmt,
                 available[thumb] if thumb is not None else None, out_path)


class _Formatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        if record.levelno >= logging.ERROR:
            return "Error: " + text
        if record.levelno >= logging.WARNING:
            return "Warning: " + text
        return text


def setup_logging(verbose):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("%(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False


def load_presets_for(argv):
    """Load the presets file before the full parse, so --help can list the presets.
    "-preset" must not be taken for "-presets_file"."""
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("-presets_file", default=DEFAULT_PRESETS_FILE)
    pre.add_argument("-preset")   # exact match, else some Pythons read it as -presets_file
    known, _ = pre.parse_known_args(argv)
    try:
        return load_presets(resolve_path(known.presets_file, app_dir())), None
    except TranscodeError as exc:
        return None, exc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    presets, presets_error = load_presets_for(argv)
    ns = build_parser(presets, presets_error).parse_args(argv)
    setup_logging(ns.verbose)
    try:
        if ns.ocio_info:
            return show_ocio_info(ns)
        if not ns.i:
            raise TranscodeError("-i is required: any file of the input sequence.")
        if presets is None:
            raise presets_error
        cfg = config_from_args(ns, presets)
        log.debug("Configuration: %s", cfg)
        return run(cfg)
    except TranscodeError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return EXIT_INTERRUPTED
