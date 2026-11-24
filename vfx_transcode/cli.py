"""Command line entry point.

Option names are unchanged from the original single-file script, so existing
.bat files keep working. 0/1 switches accept 0/1, true/false, or no value (= on).

Exit codes: 0 ok, 1 error, 2 movie written but some frames failed to convert and
were replaced by a neighbouring frame, 130 interrupted.
"""

import argparse
import datetime
import getpass
import json
import logging
import re
import shutil
import string
import tempfile
from collections import OrderedDict
from fractions import Fraction
import os
import sys
from pathlib import Path, PureWindowsPath

from . import __version__
from .channels import pick_channels
from .aspect import output_par, par_display, parse_par, resolve_par
from dataclasses import replace
from .burnin import (BURN_PLACES, DEFAULT_MARGINS, PLACE_NAMES, BurnStyle, margins_from_values,
                     parse_size, parse_style_option, resolve_margins, resolve_styles,
                     style_from_values)
from .config import Config
from .deep import parse_zrange, plan_depth_view
from .errors import TranscodeError
from .ffmpeg import (BurnContext, build_command, build_image_command, build_video_filter, encode,
                     encode_args, write_concat_list)
from .oiio import convert_sequence
from .oiio_info import get_image_info, write_info_json
from . import metadata as md
from .presets import DEFAULT_PRESETS_FILE, load_presets
from .letterbox import (DEFAULT_FILL as LB_FILL, DEFAULT_LINE_COLOR as LB_LINE_COLOR,
                        DEFAULT_RATIO as LB_DEFAULT_RATIO, LetterboxSpec, parse_ratio, parse_rgba,
                        plan as letterbox_plan)
from .smart_tokens import evaluate_preset as evaluate_smart_tokens
from .names import (NAME_KEYWORDS, DEFAULT_VERSION_REGEX, compile_version_regex, custom_tokens,
                    merge_rules, name_tokens)
from .transforms import INPUT_KEYWORDS, input_tokens, select as select_transform, transform_args
from . import filmstrip as fs
from .thumbnails import (choose_frame as choose_thumbnail_frame, ffmpeg_command as thumbnail_ffmpeg,
                         oiiotool_command as thumbnail_oiiotool, parse_frame as parse_thumbnail_frame)
from .oiio import plain_args, process_args
from .thumbnails import _without_depth
from .sequence import fill_holes, find_sequence, format_frames
from . import movie as mv
from .movie import DEFAULT_FIRST_FRAME
from . import outseq
from . import super_presets as sp
from . import studio
from .channels import find_alpha
from .slate import (PLAIN_SLATE, SLATE_STYLE_FIELDS, SlateSpec, apply_field_options, has_alpha, movie_size,
                    parse_thumbnail, pick_thumbnail, probe_frame, probe_frames, render_slate,
                    slate_timecode, style_values)
from .timecode import (fps_display, fps_from_attribs, fps_to_str, frame_to_tc, nominal_fps,
                       offset_timecode, parse_fps, timecode_from_attribs, validate_timecode)
from .tools import TOOLS, app_dir, expand_path, ffmpeg_filters, resolve_path, resolve_tool, run_tool, split_args

log = logging.getLogger("vfx_transcode")

EXIT_OK, EXIT_ERROR, EXIT_FRAMES_FAILED, EXIT_INTERRUPTED = 0, 1, 2, 130
DEFAULT_BURN_FONT = "./fonts/DroidSansMono.ttf"
DEFAULT_FPS = Fraction(24)
DEFAULT_WIDTH = 1920


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
    p.add_argument("-o", default="", metavar="PATH",
                   help="Output movie: a file or a folder (ending in / or \\). ./ and ../ are "
                        "relative to the input sequence's folder, e.g. \"./../review/\" or "
                        "\"./../review/{clean_name}_{date}.mov\". Keywords as in -metadata plus "
                        "{ext} and {input_width} {input_height}. The extension is added if missing. "
                        "Default: <input folder>/<name>.<ext>")
    p.add_argument("-ot", default=None, nargs="?", const="", metavar="PATH",
                   help="Write a thumbnail (jpg/png, no burn-ins). PATH works like -o (file or "
                        "folder, ./ = input folder, keywords, plus {frame}); -ot alone puts "
                        "<name>.<ext> next to the movie, or in the input folder. With -ot and no "
                        "-o only the thumbnail is made; add -o (e.g. -o ./) for the movie too. "
                        "See -thumbnail_preset")
    p.add_argument("-thumbnail_preset", default=None, metavar="NAME",
                   help="Thumbnail preset from the presets file (frame, size, jpg/png, quality). "
                        "Default: the file's default_thumbnail")
    p.add_argument("-thumbnail_frame", default=None, metavar="middle|first|last|largest|FRAME",
                   help="Which frame: overrides the preset. largest = the frame whose converted "
                        "image is the biggest file (needs the movie; else middle)")
    p.add_argument("-of", default=None, nargs="?", const="", metavar="PATH",
                   help="Write a filmstrip: one jpg of evenly spaced frames in a grid of equal "
                        "tiles, same look as the thumbnail (no burn-ins). PATH works like -ot; -of "
                        "alone puts <name>_filmstrip.jpg next to the movie, or in the input folder. "
                        "Without -o only the stills are made. See -filmstrip_preset")
    p.add_argument("-filmstrip_preset", default=None, metavar="NAME",
                   help="Filmstrip preset from the presets file (tiles, tile size, grid, quality). "
                        "Default: the file's default_filmstrip")
    p.add_argument("-filmstrip_tiles", default=None, metavar="N",
                   help="Number of tiles (frames), evenly spaced from the first to the last frame: "
                        "overrides the preset. Default 24")
    p.add_argument("-filmstrip_tile", default=None, metavar="WxH",
                   help="Size of one tile; each frame is fitted in and padded with black. "
                        "Overrides the preset. Default 480x270")
    p.add_argument("-filmstrip_grid", default=None, metavar="COLSxROWS",
                   help="Layout, e.g. 24x1 (one row), 6x4, or 6x (rows as needed). Without "
                        "-filmstrip_tiles the tile count becomes COLS x ROWS. Default: one row")
    p.add_argument("-filmstrip_quality", default=None, metavar="1-100",
                   help="jpg quality of the filmstrip: overrides the preset. Default 90")
    p.add_argument("-filmstrip_source", default=None, choices=fs.SOURCES,
                   help="Where the tiles come from. source (default): the source frames with the "
                        "movie's look, also without a movie. intermediates: the frames oiiotool "
                        "converted for the movie (EXR, with the movie or -skipffmpeg). movie: "
                        "frames of the encoded movie via ffmpeg, burn-ins and letterbox included")
    flag(p, "-filmstrip_slate", None,
         "1: the slate (when the movie has one) is the first tile. Default 0: no slate")
    p.add_argument("-preview", default="", metavar="FILE.png",
                   help="Instead of the movie: one frame exactly as the movie shows it (burn-ins, "
                        "letterbox, pixel format), as FILE.png; the slate as FILE_slate.png; and "
                        "FILE.json with the input and output metadata. The frame: "
                        "-thumbnail_frame (default middle). Used by vfx-transcode-tuner")
    p.add_argument("-r", "--fps", default=None, metavar="RATE",
                   help="Frame rate: 24, 25, 23.976, 24000/1001. See -fps_source for how it "
                        "combines with the EXR header (or a movie's own rate); 24 if neither "
                        "gives one")
    p.add_argument("-in", dest="trim_in", type=int, default=None, metavar="FRAME",
                   help="Trim: the first frame to use (frame number, inclusive)")
    p.add_argument("-out", dest="trim_out", type=int, default=None, metavar="FRAME",
                   help="Trim: the last frame to use (frame number, inclusive)")
    p.add_argument("-seq_preset", default=None, metavar="NAME",
                   help="Sequence preset from the presets file (oiiotool output settings for an "
                        "image sequence -o: bit depth, compression, quality). Default: the file's "
                        "default_sequence_presets entry for the output's format")
    p.add_argument("-seq_args", default="", metavar="ARGS",
                   help="oiiotool output arguments for an image sequence -o, replacing the "
                        "sequence preset, e.g. \"-d half --compression dwab:45\". Without either: "
                        "exr -d half --compression zip; jpg -d uint8 --compression jpeg:95; png/tif "
                        "-d uint8 (uint16 with -mezzanine_16bit 1); dpx -d uint10")
    p.add_argument("-movie_first_frame", type=int, default=None, metavar="N",
                   help="Movie input (.mov .mp4 .mxf ...): the frame number of its first frame, "
                        "for burn-ins, -thumbnail_frame and the frame counter. Default 1001")
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
    p.add_argument("-ci", "--colorin", default=None, metavar="",
                   help='EXR (input) colourspace, e.g. ACEScg, "Utility - Linear - sRGB". Default '
                        "ACEScg. Other input through oiiotool (a sequence or EXR output): only "
                        "converted when -ci / -co is given")
    p.add_argument("-co", "--colorout", default=None, metavar="",
                   help="Output colourspace, e.g. out_rec709, out_srgb. Default out_rec709")
    p.add_argument("-display", default=None, metavar="NAME",
                   help='OCIO display, e.g. "sRGB - Display" (ACES 1.3+ configs). With -display '
                        "and/or -view, oiiotool --ociodisplay is used instead of -colorout; "
                        '"" means the config\'s default')
    p.add_argument("-view", default=None, metavar="NAME",
                   help='OCIO view, e.g. "ACES 1.0 - SDR Video". Empty or missing: the display\'s default view')
    p.add_argument("-looks", default="", metavar="LOOKS",
                   help="OCIO looks applied with the display/view transform, e.g. \"ACES 1.3 Reference Gamut Compression\"")
    p.add_argument("-clf", default="", metavar="PATH",
                   help="Apply this CLF (Common LUT Format) file with oiiotool --ociofiletransform "
                        "instead of -ci/-co or -display/-view. The path may use keywords, e.g. "
                        '"D:/luts/{namesplit1}/{namesplit2}.clf" or {meta:ATTR}; ./ is relative '
                        "to the application. -co then only names the output for the movie's "
                        "colour tags (out_srgb for sRGB). EXR only")
    p.add_argument("-colour_preset", "-color_preset", default=None, metavar="NAME",
                   help="Colour preset from the presets file (\"colour_presets\"): named "
                        "-ci / -co / -display / -view / -looks / -clf / -ocio values, e.g. "
                        "rec709_to_acescg. Options given on the command line win")
    p.add_argument("-ocio_info", action="store_true",
                   help="Print the colourspaces, displays and views of the OCIO config (oiiotool "
                        "--colorconfiginfo) and exit. -i is not needed")
    p.add_argument("-v", "--verbose", type=int, nargs="?", const=1, default=0, metavar="N",
                   help="1: show commands, 2: also show tool output")
    p.add_argument("--version", action="version", version=__version__)

    g = p.add_argument_group("tools")
    g.add_argument("-ffmpeg", default=None, metavar="PATH",
                   help="ffmpeg executable. Default: the presets file's tools.ffmpeg, else "
                        "./ffmpeg/ffmpeg.exe. ./ paths are relative to the application, "
                        "%%VAR%% / $VAR are expanded; falls back to PATH")
    g.add_argument("-ffprobe", default=None, metavar="PATH",
                   help="ffprobe executable. Default: the presets file's tools.ffprobe, else "
                        "./ffmpeg/ffprobe.exe")
    g.add_argument("-oiio", default=None, metavar="PATH",
                   help="oiiotool executable. Default: the presets file's tools.oiiotool, else "
                        "./oiio/oiiotool.exe")
    g.add_argument("-ocio", default=None,
                   help="OCIO config file, or an ocio:// built-in config URI. Default: the "
                        "colour preset's, else " + DEFAULT_OCIO)

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
    g.add_argument("-oiio_transform_args", default=None, metavar="ARGS",
                   help='oiiotool transform arguments used as they are, instead of a preset, e.g. '
                        '"--rangecompress --resize 0x1080 --rangeexpand". Keywords work. Replaces '
                        "-oiio_transform; -width then applies in ffmpeg. EXR only")
    g.add_argument("-oiio_transform", default=None, metavar="NAME",
                   help="Transform preset from the presets file (oiio_transforms): resize / crop "
                        "chosen by conditions on the input, e.g. its size. Replaces the oiiotool "
                        '-width resize; ffmpeg then scales its output to -width. "none" = off. '
                        "Default: the file's default_oiio_transform. EXR only")
    flag(g, "-oiio_per_file", False,
         "Run one oiiotool per file instead of --parallel-frames batches")
    g.add_argument("-mezzanine", default="png", help="Intermediate file format. Default png")
    flag(g, "-mezzanine_16bit", False,
         "Write 16-bit intermediates (slower; useful for 10-bit presets)")
    g.add_argument("-width", type=int, default=DEFAULT_WIDTH,
                   help="Width of the movie; the height follows the aspect. 0 = no resize. For EXRs "
                        "oiiotool does it, or with -oiio_transform ffmpeg scales the transform's "
                        "output to this width. Default %(default)s")
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
    g.add_argument("-timecode_source", choices=("counter", "arg", "exr"), type=metadata_source,
                   default=None, metavar="{counter,arg,metadata}",
                   help="counter: from the first frame number and fps (1001 at 24 fps = "
                        "00:00:41:17), the default. arg: -timecode. metadata: the timecode in the "
                        "EXR header or the movie (falls back to counter if there is none); "
                        "exr is the old name of metadata")
    g.add_argument("-timecode", default=None,
                   help="Start timecode HH:MM:SS:FF. Giving it implies -timecode_source arg")
    flag(g, "-timecode_from_counter", None,
         "Old switch: 1 = -timecode_source counter, 0 = -timecode_source arg")
    flag(g, "-output_timecode", True,
         "Write the timecode to the output: a timecode track in mov/mp4/mxf, a tag in other "
         "containers; in an EXR or DPX sequence each frame's own timecode (smpte:TimeCode)")
    flag(g, "-seq_metadata", True,
         "Image sequence written by oiiotool: write the -metadata tags and the metadata preset's "
         "tags into each image as string attributes (EXR keeps them all; other formats what "
         "they can store). 0: none")
    flag(g, "-output_reelid", True,
         "Write the reel name to the output: the movie, or each image of an EXR sequence "
         "(reelName). 0: it is still found, for the slate and the {reel} keyword, but not written")
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
    g.add_argument("-name_token", action="append", default=[], metavar="NAME=REGEX",
                   help="Custom keyword {NAME}: group 1 (else the whole match) of the first match "
                        "of REGEX in the sequence name, empty if none; repeatable. Adds to / "
                        "overrides the presets file's \"name_tokens\" (which can also read other "
                        "sources, e.g. a header value); NAME= removes one. Example: "
                        "-name_token \"shot=^([A-Z0-9]+_\\d+)\"")
    g.add_argument("-smart_tokens", default=None, metavar="NAME",
                   help='Smart tokens preset from the presets file: per-project keyword rules '
                        '(conditions, regex, replacement, case), or "none". Default: the file\'s '
                        "default_smart_tokens")
    g.add_argument("-metadata_preset", default=None, metavar="NAME",
                   help='Metadata preset from the presets file (tags, copy rules, reel name and '
                        'timecode settings), or "none". The options below override its values')
    g.add_argument("-reelid_source", choices=("auto", "arg", "exr", "off"), type=metadata_source,
                   default=None, metavar="{auto,arg,metadata,off}",
                   help="Reel name for conforming. auto (default): -reelid, else the header "
                        "attribute, else none. arg: -reelid only. metadata: the header attribute, "
                        "else -reelid (exr is the old name). off: no reel name")
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
    g.add_argument("-burn_margin", default=None, metavar="SIZE",
                   help="Empty space between the picture edge and the burn-ins, all four sides: "
                        "pixels (20) or percent (2%%: of the width for left/right, of the height "
                        "for top/bottom). Default: the preset's, else 1.5%% top/bottom, 1%% "
                        "left/right")
    for side in ("top", "bottom", "left", "right"):
        g.add_argument("-burn_margin_" + side, default=None, metavar="SIZE",
                       help="{} margin only; wins over -burn_margin".format(side.capitalize()))
    g.add_argument("-burn_font", default=None,
                   help="Burn-in font file. Default: the burn-in preset's font, else "
                        + DEFAULT_BURN_FONT)
    for place in BURN_PLACES:
        g.add_argument("-burn_" + place, default=None, metavar="TEXT",
                       help='Burn-in {} text{} ("" clears the preset\'s). Keywords: {{clean_name}} '
                            "{{current_frame}} {{frame_start}} {{frame_end}} {{tc}} {{timecode}} "
                            "{{fps}} {{date}} {{preset}} {{meta:ATTR}} {{version_number}} {{namesplit1}} ...".format(
                                PLACE_NAMES[place], ", centred" if place in ("tc", "bc") else
                                " (big, see-through, centred in the picture; margins do not apply)"
                                if place == "wm" else ""))

    g = p.add_argument_group("slate")
    g.add_argument("-slate", default=None, metavar="NAME",
                   help='Slate preset from the presets file, "plain" (only the -slate_* options) or "none" (default: the file\'s '
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
    g.add_argument("-slate_logo", default=None, metavar="PATH",
                   help="Vendor logo drawn on the slate: an image, best a PNG with alpha. "
                        "./ = the application folder. \"\" removes a preset's logo")
    g.add_argument("-slate_logo_position", default=None, metavar="tl|tc|tr|ml|mc|mr|bl|bc|br",
                   help="Where the logo sits: top / middle / bottom + left / centre / right "
                        "(top-right, bottom-left ... work too). Default tr")
    g.add_argument("-slate_logo_size", default=None, metavar="SIZE",
                   help="Logo height: percent of the picture height (10%% default) or pixels; "
                        "the width follows the logo's proportions")
    g.add_argument("-slate_logo_x", default=None, metavar="SIZE",
                   help="Logo distance from the left / right edge: percent of the width (4%% "
                        "default) or pixels. Not used for a centred position")
    g.add_argument("-slate_logo_y", default=None, metavar="SIZE",
                   help="Logo distance from the top / bottom edge: percent of the height (5%% "
                        "default) or pixels. Not used for a middle position")
    flag(g, "-slate_rule", None, "Line under the slate title. Default: the preset's, else 0")
    flag(g, "-slate_burnin", False, "Draw the burn-ins on the slate frame too")

    g = p.add_argument_group("alpha")
    g.add_argument("-alpha", choices=ALPHA_MODES, default="drop",
                   help="drop (default): the output has no alpha. keep: carry the input alpha into "
                        "the output; a movie needs an encode preset that can store it (marked "
                        "[alpha] below, e.g. prores4444) or -ff_encode with an alpha pixel format, "
                        "a sequence must be exr, png or tif (else an error). auto: as keep when "
                        "the input has alpha and the output can store it, else as drop (no error)")
    flag(g, "-alpha_premult", False,
         "With -alpha keep: 1 = colour premultiplied by alpha, 0 (default) = straight "
         "(unpremultiplied) colour, what most players and editors expect in a mov")

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
    flag(g, "-oiio_letterbox", False,
         "Letterbox drawn by oiiotool on the converted frames (EXR input, or any oiiotool-only "
         "sequence output), so no ffmpeg is needed for it. Size from the frames after the OIIO "
         "transform; on the slate too, as the slate is made from them")
    g.add_argument("-oiio_letterbox_ratio", default=None, metavar="RATIO",
                   help="As -letterbox_ratio, for the oiiotool letterbox")
    g.add_argument("-oiio_letterbox_color", default=None, metavar="R,G,B,A",
                   help="As -letterbox_color (0-255; alpha below 255 = see-through)")
    g.add_argument("-oiio_letterbox_line", type=int, default=None, metavar="PIXELS",
                   help="As -letterbox_line")
    g.add_argument("-oiio_letterbox_line_color", default=None, metavar="R,G,B,A",
                   help="As -letterbox_line_color")

    g = p.add_argument_group("encoding (ffmpeg)")
    flag(g, "-skipffmpeg", False, "Only convert the EXRs; implies -temp_keep 1")
    g.add_argument("-super_preset", default=None, metavar="NAME[,NAME]",
                   help="Super-preset from super_presets.json (written by vfx-transcode-tuner): "
                        "the options of its areas (encode, transform, burn-in, slate, filmstrip, "
                        "metadata, letterbox) go in front of the command line's, which win")
    g.add_argument("-super_presets_file", default=None, metavar="PATH",
                   help="super_presets.json to read. Default: next to the presets file")
    g.add_argument("-preset", default=None,
                   help="Encode preset from the presets file, see the list below. "
                        "Default: the file's \"default\"")
    g.add_argument("-presets_file", default=None,
                   help="JSON file with the encode, burn-in, metadata and slate presets and channel "
                        "preferences. Default: the studio preferences named in vfx-transcode.ini "
                        "next to the application, else ./presets.json (relative to the application)")
    g.add_argument("-ff_transform_preset", default=None, metavar="NAME",
                   help="ffmpeg transform preset from the presets file (\"ff_transforms\"); "
                        "-ff_transform wins")
    g.add_argument("-ff_transform", default="", metavar="FILTERS",
                   help="ffmpeg filters that replace the -width scale, e.g. "
                        "\"scale=w=2048:h=-2:flags=lanczos\" or a zscale chain. Filters are "
                        "separated by commas or new lines; a line starting with # is skipped")
    g.add_argument("-ff_filter", default="",
                   help="Extra ffmpeg video filters, inserted before the burn-ins")
    g.add_argument("-ff_encode", default="",
                   help="Replaces the preset's codec arguments (no colour tags are added). "
                        "Use {videofilter} where the -vf filter goes")
    g.add_argument("-ff_out_ext", default=None, help="Output extension. Default: from the preset")
    return p


def metadata_source(value):
    """-timecode_source / -reelid_source value: "metadata" is the new name of "exr"."""
    value = value.strip().lower()
    return "exr" if value == "metadata" else value


def timecode_source_from_args(ns, metadata_preset=None):
    if ns.timecode_source:
        return ns.timecode_source
    if ns.timecode is not None or ns.timecode_from_counter is False:
        return "arg"
    if metadata_preset is not None and metadata_preset.timecode_source:
        return metadata_preset.timecode_source
    return "counter"


def timecode_source_given(ns, metadata_preset=None):
    """False when the timecode source is only the default (the frame counter)."""
    return bool(ns.timecode_source or ns.timecode is not None or ns.timecode_from_counter is not None
                or (metadata_preset is not None and metadata_preset.timecode_source))


def _pick(cli_value, preset, attr, default):
    """Command line (if given) > metadata preset (if it sets the value) > default."""
    if cli_value is not None:
        return cli_value
    if preset is not None and getattr(preset, attr) is not None:
        return getattr(preset, attr)
    return default


SLATE_OPTIONS = ("slate_title", "slate_subtitle", "slate_thumbnail", "slate_size", "slate_scale",
                 "slate_valign", "slate_title_size", "slate_subtitle_size", "slate_value_color",
                 "slate_label_color", "slate_logo", "slate_logo_position", "slate_logo_size",
                 "slate_logo_x", "slate_logo_y")


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
    if ns.slate == PLAIN_SLATE:
        spec = SlateSpec()                  # -slate plain: title {clean_name}, only given rows
    else:
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
               "valign": ns.slate_valign, "rule": ns.slate_rule,
               "logo_position": ns.slate_logo_position, "logo_size": ns.slate_logo_size,
               "logo_x": ns.slate_logo_x, "logo_y": ns.slate_logo_y}
    if ns.slate_logo is not None:
        changes["logo"] = resolve_path(expand_path(ns.slate_logo.strip()), app_dir())
    changes.update(style_values(options, "command line"))
    changes.update(style_values(parse_slate_style(ns.slate_style), "-slate_style"))
    if spec.font:
        changes["font"] = resolve_path(spec.font, app_dir())
    return replace(spec, **changes)


ALPHA_MODES = ("drop", "keep", "auto")
ALPHA_MEZZANINES = ("png", "tif", "tiff", "exr")
ALPHA_SEQUENCES = (".exr", ".png", ".tif", ".tiff")


def alpha_blocker(ns, preset):
    """Why the output cannot store alpha, or None when it can."""
    seq_ext = outseq.sequence_ext(ns.o)
    if seq_ext and seq_ext not in ALPHA_SEQUENCES:
        return "{} files cannot store alpha; use exr, png or tif.".format(seq_ext.lstrip("."))
    if not seq_ext and not ns.ff_encode and not preset.alpha_pix_fmt:
        return ('the encode preset "{}" cannot store alpha. Use a preset with "alpha_pix_fmt" '
                '(e.g. prores4444), or -ff_encode with an alpha pixel format such as '
                'yuva444p10le.'.format(preset.name))
    if seq_ext != ".exr" and ns.mezzanine.strip().lower().lstrip(".") not in ALPHA_MEZZANINES:
        return "-mezzanine {} cannot store alpha; use png or tif.".format(ns.mezzanine)
    return None


def check_alpha_args(ns, preset):
    """-alpha as run: keep or drop. auto becomes keep when the output can store alpha (whether
    the input has one is only known later), else drop; keep with such an output is an error."""
    mode = ns.alpha
    if mode != "drop":
        blocker = alpha_blocker(ns, preset)
        if blocker and mode == "keep":
            raise TranscodeError("-alpha keep: " + blocker)
        if blocker:
            log.info("Alpha: not kept (-alpha auto): %s", blocker)
            mode = "drop"
        else:
            mode = "keep"
    if mode == "drop" and ns.alpha_premult:
        log.warning("-alpha_premult ignored: no alpha in the output.")
    return mode


def alpha_missing(cfg, text, *args):
    """No alpha in the input: a warning for -alpha keep, information for -alpha auto."""
    (log.info if cfg.alpha_auto else log.warning)(text, *args)


DEFAULT_COLORIN, DEFAULT_COLOROUT = "ACEScg", "out_rec709"
OIIO_LETTERBOX_OPTIONS = ("oiio_letterbox_ratio", "oiio_letterbox_color", "oiio_letterbox_line",
                          "oiio_letterbox_line_color")


def sequence_args_from(ns, presets):
    """(oiiotool output args, where from) for an image sequence -o: -seq_args, else
    -seq_preset, else the presets file's default for the format, else ([], "") = built-in."""
    ext = outseq.norm_ext(outseq.sequence_ext(ns.o))
    if ns.seq_args.strip():
        return split_args(ns.seq_args), "-seq_args"
    sequences = presets.sequences if presets is not None else {}
    if ns.seq_preset:
        if ns.seq_preset not in sequences:
            raise TranscodeError('Unknown sequence preset "{}". Available: {}'.format(
                ns.seq_preset, ", ".join(sequences) or "none"))
        preset = sequences[ns.seq_preset]
        if ext and preset.ext != ext:
            log.warning('-seq_preset %s is a %s preset, the output is %s: its settings may not fit.',
                        preset.name, preset.ext, ext)
        return list(preset.args), preset.name
    defaults = presets.default_sequences if presets is not None else {}
    if ext and ext in defaults:
        preset = sequences[defaults[ext]]
        return list(preset.args), preset.name
    return [], ""


def ff_transform_from_args(text):
    """-ff_transform: one filter chain from lines and commas; # lines skipped."""
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n")]
    parts = [l.rstrip(",") for l in lines if l and not l.startswith("#")]
    return ",".join(p for p in parts if p)


def oiio_letterbox_from_args(ns):
    given = [o for o in OIIO_LETTERBOX_OPTIONS if getattr(ns, o) is not None]
    if not ns.oiio_letterbox:
        if given:
            log.warning("-%s ignored: the oiiotool letterbox is off (-oiio_letterbox 1 turns it on).",
                        given[0])
        return None
    ratio_text = ns.oiio_letterbox_ratio or LB_DEFAULT_RATIO
    line = ns.oiio_letterbox_line or 0
    if line < 0:
        raise TranscodeError("-oiio_letterbox_line must be 0 or more pixels.")
    return LetterboxSpec(
        ratio=parse_ratio(ratio_text, "-oiio_letterbox_ratio"), ratio_text=ratio_text.strip(),
        fill=(parse_rgba(ns.oiio_letterbox_color, "-oiio_letterbox_color")
              if ns.oiio_letterbox_color else LB_FILL),
        line=line,
        line_color=(parse_rgba(ns.oiio_letterbox_line_color, "-oiio_letterbox_line_color")
                    if ns.oiio_letterbox_line_color else LB_LINE_COLOR))


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


DEFAULT_OCIO = "./ocio/aces_1.2cg/config.ocio"


def apply_named_presets(ns, presets):
    """-colour_preset and -ff_transform_preset fill the options not given on the command
    line. Safe to call twice."""
    if getattr(ns, "_named_presets_applied", False):
        return
    ns._named_presets_applied = True
    if ns.colour_preset:
        if ns.colour_preset not in presets.colours:
            raise TranscodeError('Unknown colour preset "{}". Available: {}'.format(
                ns.colour_preset, ", ".join(presets.colours) or "none (add \"colour_presets\")"))
        c = presets.colours[ns.colour_preset]
        for attr, value, unset in (("colorin", c.ci, None), ("colorout", c.co, None),
                                   ("display", c.display, None), ("view", c.view, None),
                                   ("looks", c.looks, ""), ("clf", c.clf, ""), ("ocio", c.ocio, None)):
            if value and getattr(ns, attr) in (unset, ""):
                setattr(ns, attr, value)
        log.info("Colour preset %s: %s", c.name, c.describe())
    if ns.ocio is None:
        ns.ocio = DEFAULT_OCIO
    if ns.ff_transform_preset:
        if ns.ff_transform_preset not in presets.ff_transforms:
            raise TranscodeError('Unknown ffmpeg transform preset "{}". Available: {}'.format(
                ns.ff_transform_preset, ", ".join(presets.ff_transforms) or "none (add \"ff_transforms\")"))
        if not ns.ff_transform.strip():
            ns.ff_transform = presets.ff_transforms[ns.ff_transform_preset].chain()


def config_from_args(ns, presets):
    apply_named_presets(ns, presets)
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
    cli_margins = margins_from_values(
        {"margin": ns.burn_margin, "margin_top": ns.burn_margin_top,
         "margin_bottom": ns.burn_margin_bottom, "margin_left": ns.burn_margin_left,
         "margin_right": ns.burn_margin_right}, "command line")
    burn_margins = resolve_margins(burn_preset.margins if burn_preset else None, cli_margins)
    burn_font = ns.burn_font or (burn_preset.font if burn_preset else None) or DEFAULT_BURN_FONT
    if ns.deep_zrange:
        parse_zrange(ns.deep_zrange)   # validate early
    slate = slate_from_args(ns, presets)
    letterbox = letterbox_from_args(ns)
    alpha_mode = check_alpha_args(ns, preset)
    name_rules = merge_rules(presets.name_rules, ns.name_token)
    smart_tokens = presets.get_smart_tokens(
        ns.smart_tokens if ns.smart_tokens is not None else presets.default_smart_tokens)
    token_names = tuple(name_rules) + tuple(smart_tokens.names() if smart_tokens else ())
    if ns.o.strip():
        check_output_template(ns.o, extra=token_names)
    thumbnail = None
    if ns.ot is not None:
        thumbnail = presets.get_thumbnail(ns.thumbnail_preset or presets.default_thumbnail)
        if ns.thumbnail_frame is not None:
            thumbnail = replace(thumbnail, frame=parse_thumbnail_frame(ns.thumbnail_frame))
        if ns.ot.strip():
            check_output_template(ns.ot, "-ot", tuple(THUMBNAIL_KEYWORDS) + token_names)
    elif (ns.thumbnail_preset is not None or ns.thumbnail_frame is not None) and not ns.preview:
        log.warning("-thumbnail_preset / -thumbnail_frame ignored: no -ot.")
    filmstrip = filmstrip_from_args(ns, presets, token_names)
    seq_args, seq_preset = sequence_args_from(ns, presets)
    # what to make: -o, -ot and/or -of; none of them = the movie at its default place
    movie = bool(ns.o.strip()) or (ns.ot is None and ns.of is None)
    preview_frame = "middle"
    if ns.preview:
        if ns.ot is not None or ns.of is not None:
            log.warning("-preview: -ot / -of are ignored, only the preview is written.")
        if ns.o.strip() and not outseq.sequence_ext(ns.o):
            log.debug("-preview: -o is only used to tell an image sequence output from a movie.")
        movie, thumbnail, filmstrip = True, None, None
        preview_frame = parse_thumbnail_frame(ns.thumbnail_frame or "middle")
    if ns.clf.strip() and (ns.display is not None or ns.view is not None or ns.looks):
        log.warning("-clf is used for the colour; -display / -view / -looks are ignored.")
    oiio_transform = presets.get_transform(
        ns.oiio_transform if ns.oiio_transform is not None else presets.default_transform)
    base = app_dir()
    skip_ffmpeg = ns.skipffmpeg
    tools = tool_paths(ns, presets)
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
        oiiotool=tools["oiiotool"][0],
        ffmpeg=tools["ffmpeg"][0],
        ffprobe=tools["ffprobe"][0],
        tool_sources={name: where for name, (_path, where) in tools.items()},
        ocio=ns.ocio if ns.ocio.startswith("ocio://") else resolve_path(ns.ocio, base),
        colorin=ns.colorin or DEFAULT_COLORIN,
        colorout=ns.colorout or DEFAULT_COLOROUT,
        colour_given=bool(ns.colorin or ns.colorout or ns.display is not None
                          or ns.view is not None or ns.clf.strip()),
        ocio_display=ns.display,
        ocio_view=ns.view,
        ocio_looks=ns.looks,
        clf=ns.clf.strip(),
        oiio_threads=max(0, ns.oiio_threads),
        oiio_channel=[c for c in ns.oiio_channel.split(",") if c.strip()],
        oiio_channel_use_first=ns.oiio_channel_use_first,
        channel_preferences=presets.channel_preferences,
        oiio_command=split_args(ns.oiio_command),
        oiio_transform=oiio_transform,
        oiio_transform_explicit=(split_args(ns.oiio_transform_args)
                                 if ns.oiio_transform_args is not None else None),
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
        timecode_source_given=timecode_source_given(ns, metadata_preset),
        movie_first_frame=(ns.movie_first_frame if ns.movie_first_frame is not None
                           else DEFAULT_FIRST_FRAME),
        output_timecode=ns.output_timecode,
        output_reelid=ns.output_reelid,
        seq_metadata=ns.seq_metadata,
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
        burn_margins=burn_margins,
        slate=slate,
        letterbox=letterbox,
        oiio_letterbox=oiio_letterbox_from_args(ns),
        trim_in=ns.trim_in,
        trim_out=ns.trim_out,
        ff_transform=ff_transform_from_args(ns.ff_transform),
        seq_args=seq_args,
        seq_preset=seq_preset,
        seq_out_ext=outseq.sequence_ext(ns.o),
        movie=movie,
        thumbnail=thumbnail,
        preview=os.path.abspath(ns.preview) if ns.preview else "",
        preview_frame=preview_frame,
        thumbnail_output=ns.ot,
        filmstrip=filmstrip,
        filmstrip_output=ns.of,
        alpha=alpha_mode,
        alpha_auto=ns.alpha == "auto",
        alpha_premult=bool(ns.alpha_premult) and alpha_mode == "keep",
        version_regex=ns.version_regex,
        name_rules=name_rules,
        smart_tokens=smart_tokens,
        skip_ffmpeg=skip_ffmpeg,
        ff_filter=ns.ff_filter,
        preset=preset,
        ff_encode=ns.ff_encode,
        # the container: -ff_out_ext, else the -o file's extension, else the preset's
        ff_out_ext=(ns.ff_out_ext or container_from_output(ns.o) or preset.ext).lstrip("."),
    )


TOOL_OPTIONS = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "oiiotool": "oiio"}


def tool_paths(ns, presets=None):
    """{tool: (path, where it came from)}: the command line, else the presets file's "tools",
    else the built-in default. resolve_tool falls back to the PATH when the file is missing."""
    result = OrderedDict()
    for name, default in TOOLS.items():
        value = getattr(ns, TOOL_OPTIONS[name], None)
        if value:
            result[name] = (value, "-" + TOOL_OPTIONS[name])
        elif presets is not None and presets.tools.get(name):
            result[name] = (presets.tools[name], '{} "tools"'.format(Path(presets.path).name))
        else:
            result[name] = (default, "default")
    return result


FILMSTRIP_OPTIONS = ("filmstrip_preset", "filmstrip_tiles", "filmstrip_tile", "filmstrip_grid",
                     "filmstrip_quality", "filmstrip_source", "filmstrip_slate")


def filmstrip_from_args(ns, presets, token_names=()):
    """The filmstrip preset with the -filmstrip_* options over it; None without -of."""
    given = [o for o in FILMSTRIP_OPTIONS if getattr(ns, o) is not None]
    if ns.of is None:
        if given and not ns.preview:
            log.warning("%s ignored: no -of.", " / ".join("-" + o for o in given))
        return None
    preset = presets.get_filmstrip(ns.filmstrip_preset or presets.default_filmstrip)
    preset = fs.apply_options(preset, ns.filmstrip_tiles, ns.filmstrip_tile, ns.filmstrip_grid,
                              ns.filmstrip_quality, ns.filmstrip_source, ns.filmstrip_slate)
    if ns.of.strip():
        check_output_template(ns.of, "-of", token_names)
    return preset


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
    if cfg.clf:
        return 'CLF "{}"'.format(cfg.clf_path or cfg.clf)
    if cfg.display_mode:
        text = '{} -> display "{}", view "{}"'.format(
            cfg.colorin, cfg.ocio_display or "(default)", cfg.ocio_view or "(default)")
        return text + (', looks "{}"'.format(cfg.ocio_looks) if cfg.ocio_looks else "")
    return "{} -> {}".format(cfg.colorin, cfg.colorout)


def show_ocio_info(ns, presets=None):
    """-ocio_info: print what the OCIO config offers."""
    if presets is not None:
        apply_named_presets(ns, presets)
    ns.ocio = ns.ocio or DEFAULT_OCIO
    base = app_dir()
    ocio = ns.ocio if ns.ocio.startswith("ocio://") else resolve_path(ns.ocio, base)
    path, where = tool_paths(ns, presets)["oiiotool"]
    cfg = Config(input="", ocio=ocio, oiiotool=resolve_tool(path, base, "oiiotool", where))
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
    elif cfg.clf:
        log.debug('OCIO config "%s" not found; not needed for a CLF file.', cfg.ocio)
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
            chosen = (header_fps, cfg.header_label if cfg.header_label != "EXR header"
                      else "EXR header " + header_key)
            break
    if chosen is None:
        chosen = (DEFAULT_FPS, "default")
    if header_fps is not None and cfg.fps_arg is not None and header_fps != cfg.fps_arg:
        unused = ("{} {}".format(cfg.header_label, fps_display(header_fps)) if chosen[1] == "-r"
                  else "-r {}".format(fps_display(cfg.fps_arg)))
        log.info("Frame rate: %s ignored (-fps_source %s)", unused, cfg.fps_source)
    if cfg.fps_source == "exr" and header_fps is None:
        log.info("Frame rate: %s", "no rate in the EXR header" if info else
                 "input is not EXR, so there is no header rate")
    return chosen


def today():
    return datetime.date.today().isoformat()


def resolve_timecode(cfg, seq, info, header_frame=None):
    """Start timecode of the movie and where it came from (also kept in cfg.timecode_found,
    for the preview report)."""
    timecode, source = _resolve_timecode(cfg, seq, info, header_frame)
    cfg.timecode_found = source
    return timecode, source


def _resolve_timecode(cfg, seq, info, header_frame=None):
    fps_int = nominal_fps(cfg.fps)
    if cfg.timecode_source == "arg":
        return cfg.timecode, "-timecode"
    if cfg.timecode_source == "exr":
        tc = None
        if info is None:
            log.warning("-timecode_source metadata needs EXR or movie input; using the frame counter.")
        else:
            tc = next((t for t in (timecode_from_attribs(sub.get("attribs", {})) for sub in info)
                       if t), None)
            if tc is None:
                log.warning("No timecode in the %s; using the frame counter.", cfg.header_label)
        if tc:
            frame = header_frame if header_frame is not None else seq.frame_of(cfg.input)
            if frame is not None and frame != seq.start:
                # the header we read belongs to a later frame, not the first one
                tc = offset_timecode(tc, seq.start - frame, fps_int)
            return tc, cfg.header_label
    return frame_to_tc(seq.start, fps_int), "frame counter"


def colour_label(cfg, seq):
    """Short colour description for keywords: 'CLF name.clf' or the OCIO conversion."""
    if not seq.is_exr:
        return ""
    return ("CLF " + Path(cfg.clf_path).name) if cfg.clf_path else describe_colour(cfg)


def metadata_context(cfg, seq, timecode):
    """Keywords for -metadata, metadata preset tags and -reelid. Besides the name keywords:
    {frames} frame count, {slate_length} 1 or 0, {burnins} true/false (any burn-in or
    watermark drawn), {colour} the colour conversion - the delivery tags some specs ask for."""
    context = dict(cfg.name_tokens)
    context.update({"clean_name": seq.clean_name, "frame_start": seq.start, "frame_end": seq.end,
                    "frames": seq.end - seq.start + 1,
                    "timecode": timecode, "fps": fps_display(cfg.fps), "preset": cfg.preset.name,
                    "date": today(), "meta": cfg.header_attribs,
                    "slate_length": 1 if cfg.slate else 0,
                    "burnins": "true" if any(cfg.burn.values()) else "false",
                    "colour": colour_label(cfg, seq)})
    return context


def needed_filters(cfg):
    """{ffmpeg filter: what needs it} for the movie as configured. Only filters a build may
    lack are listed; scale, format, pad and setsar are in every ffmpeg."""
    need = OrderedDict()

    def add(name, why):
        need[name] = need[name] + ", " + why if name in need else why

    if any(cfg.burn.values()):
        add("drawtext", "burn-ins")
    if cfg.slate:
        add("drawtext", "the slate")
        add("color", "the slate")
        if cfg.slate.thumbnail != "none":
            add("overlay", "the slate thumbnail")
        if cfg.slate.rule:
            add("drawbox", "the slate rule")
    if cfg.letterbox:
        add("drawbox", "the letterbox")
        if cfg.alpha == "keep":
            add("color", "the letterbox with alpha")
            add("overlay", "the letterbox with alpha")
    if cfg.alpha == "keep" and cfg.alpha_premult:
        add("premultiply", "-alpha_premult")
    if cfg.preset.colour_tags and not cfg.ff_encode.strip():
        add("setparams", "the colour tags")
    return need


def check_ffmpeg_filters(cfg):
    """Stop before any frame is converted if this ffmpeg lacks a filter the movie needs
    (typically drawtext: builds without libfreetype, e.g. some static ones, have none)."""
    need = needed_filters(cfg)
    if not need:
        return
    available = ffmpeg_filters(cfg.ffmpeg)
    if available is None:
        log.warning("Could not list the filters of %s; not checked.", cfg.ffmpeg)
        return
    missing = [name for name in need if name not in available]
    if not missing:
        return
    lines = ["  {} - needed for {}".format(name, need[name]) for name in missing]
    hint = (" drawtext comes with libfreetype: use a \"full\" build (e.g. gyan.dev or BtbN), or "
            "turn off the burn-ins and the slate." if "drawtext" in missing else "")
    raise TranscodeError('ffmpeg "{}" has no {} filter:\n{}\n{}'.format(
        cfg.ffmpeg, " / ".join(missing), "\n".join(lines), hint.strip()).rstrip())


def add_custom_tokens(cfg, seq, info=None):
    """The presets file's name_tokens and -name_token, then the smart tokens preset, added to
    the name keywords. Run once the header is read, so sources can use {meta:ATTR}, and
    before the oiio transform is chosen, so its conditions can use the tokens."""
    context = dict(cfg.name_tokens)
    context.update({"clean_name": seq.clean_name, "frame_start": seq.start, "frame_end": seq.end,
                    "meta": cfg.header_attribs})
    if cfg.name_rules:
        cfg.name_tokens.update(custom_tokens(cfg.name_rules, context, md.expand))
    if cfg.smart_tokens is not None:
        context = smart_token_context(cfg, seq, info)
        cfg.name_tokens.update(evaluate_smart_tokens(cfg.smart_tokens, context, md.expand))


def smart_token_context(cfg, seq, info):
    """What smart token sources, repl and conditions can use: everything known before the
    frames are converted. Timecode, fps, reel and output size come later, so they are not here."""
    context = transform_context(cfg, seq, info)
    context.update({"frames": seq.end - seq.start + 1, "date": today(), "user": current_user(),
                    "preset": cfg.preset.name, "input": Path(cfg.input).name,
                    "input_path": Path(cfg.input).resolve().as_posix(),
                    "input_folder": Path(cfg.input).resolve().parent.name})
    return context


def resolve_reel_name(cfg, seq, info, timecode):
    """Reel name and where it came from, following -reelid_source; ("", reason) for none.
    The source is also kept in cfg.reel_found, for the preview report."""
    reel, source = _resolve_reel_name(cfg, seq, info, timecode)
    cfg.reel_found = source
    return reel, source


def _resolve_reel_name(cfg, seq, info, timecode):
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


SEQ_METADATA_FULL = (".exr",)
SEQ_TIMECODE_EXTS = (".exr", ".dpx")     # oiiotool writes smpte:TimeCode into these
SEQ_REEL_EXTS = (".exr",)                # reelName (OpenEXR standard attribute)
EXR_REEL_ATTR = "reelName"


def sequence_timecode(cfg, seq, timecode):
    """(frame -> oiiotool arguments, or None; fixed arguments) for the timecode of an image
    sequence written by oiiotool: each frame its own smpte:TimeCode. EXR input with its own
    timecode keeps it (oiiotool carries the header), so the fast batch stays."""
    ext = cfg.seq_out_ext
    label = ext.lstrip(".")
    if ext not in SEQ_TIMECODE_EXTS:
        if cfg.output_timecode:
            log.info("Timecode: not written, %s files have no timecode field.", label)
        return None, []
    if not cfg.output_timecode:
        log.info("Timecode: not written (-output_timecode 0); any in the input header removed.")
        return None, ["--eraseattrib", "smpte:TimeCode"]
    if seq.is_exr and cfg.timecode_source == "exr" and cfg.timecode_found == cfg.header_label:
        log.info("Timecode: each %s keeps the input frame's own timecode (EXR header).", label)
        return None, []
    fps_int = nominal_fps(cfg.fps)
    start = seq.start

    def per_frame(frame):
        return ["--attrib:type=timecode", "smpte:TimeCode",
                offset_timecode(timecode, frame - start, fps_int)]

    log.info("Timecode: written into each %s (smpte:TimeCode), %s at frame %d", label, timecode, start)
    return per_frame, []


def sequence_reel(cfg, reel_name):
    """oiiotool arguments for the reel name of an image sequence (EXR: reelName)."""
    ext = cfg.seq_out_ext
    if ext not in SEQ_REEL_EXTS:
        if cfg.output_reelid and reel_name:
            log.info("Reel name: not written, %s files have no reel name field.", ext.lstrip("."))
        return []
    if not cfg.output_reelid:
        if reel_name:
            log.info("Reel name: not written (-output_reelid 0)")
        return ["--eraseattrib", EXR_REEL_ATTR]
    if not reel_name:
        return []
    log.info("Reel name: written into each exr (%s)", EXR_REEL_ATTR)
    return md.oiio_args({EXR_REEL_ATTR: reel_name})


def sequence_metadata(cfg, seq, timecode):
    """Tags for an image sequence written by oiiotool: the metadata preset's tags and
    -metadata (not the encode preset's, those are codec settings). The input header is kept
    by oiiotool already, so nothing is copied."""
    if not cfg.seq_metadata:
        return OrderedDict()
    tags = md.build({}, cfg.metadata, metadata_context(cfg, seq, timecode), [cfg.preset_tags])
    if tags:
        log.info("Metadata: %s written into each %s%s", ", ".join(tags), cfg.seq_out_ext.lstrip("."),
                 "" if cfg.seq_out_ext in SEQ_METADATA_FULL else
                 " (as far as the format can store them; EXR keeps all)")
    return tags


def open_movie(cfg, base):
    """Movie input: probe it, and the sequence its frames will be unpacked as."""
    if not os.path.isfile(cfg.input):
        raise TranscodeError('Input file "{}" does not exist.'.format(cfg.input))
    cfg.ffprobe = resolve_tool(cfg.ffprobe, base, "ffprobe", cfg.tool_sources.get("ffprobe", ""))
    info = mv.probe_movie(cfg.ffprobe, cfg.input, cfg.verbose)
    seq = mv.movie_sequence(cfg.input, info, cfg.movie_first_frame, cfg.mezzanine_ext)
    cfg.header_label = "movie"
    if not cfg.timecode_source_given and info.timecode:
        cfg.timecode_source = "exr"            # the movie's own timecode, unless told otherwise
    log.info("Input: movie %s  %s, frames numbered %s", cfg.input, info.describe(),
             format_frames(seq.frames))
    if cfg.clf or cfg.display_mode:
        log.info("Colour: a movie is used as it is (display-referred); no OCIO conversion.")
    return info, seq


def run(cfg):
    base = app_dir()
    movie = None
    if mv.is_movie(cfg.input):
        movie, seq = open_movie(cfg, base)
    else:
        seq = find_sequence(cfg.input)
    full_seq = seq                         # the preview may show a frame outside the trim
    seq = trim_sequence(cfg, seq)
    cfg.header_frame = cfg.movie_first_frame if movie is not None else seq.start
    cfg.name_tokens = name_tokens(seq.clean_name, cfg.version_regex)
    if cfg.name_tokens["version_string"]:
        log.info("Version: %s (from %s)", cfg.name_tokens["version_string"], seq.clean_name)
    else:
        log.debug("No version in %s (-version_regex %s)", seq.clean_name, cfg.version_regex)
    if movie is None:
        log.info("Input: %s  frames %s (%d)", seq.folder / seq.name(seq.start),
                 format_frames(seq.frames), len(seq.frames))
    missing = seq.missing
    if missing:
        message = "Missing frames: {}".format(format_frames(missing))
        if cfg.missing_frames == "error":
            raise TranscodeError(message)
        log.warning("%s (holding the previous frame)", message)

    stills = cfg.thumbnail is not None or cfg.filmstrip is not None
    # which tools: image sequence out is oiiotool only, except a movie to a non-EXR sequence
    # (ffmpeg only); a movie out is ffmpeg, after oiiotool for EXR input
    seq_out = cfg.seq_out_ext
    oiio_only = bool(seq_out) and cfg.movie and not (movie is not None and seq_out != ".exr")
    cfg.image_output = bool(seq_out) and cfg.movie and not oiio_only
    cfg.skip_colour = seq_out == ".exr" and not cfg.colour_given
    if seq_out and cfg.movie:
        log.info("Output: %s sequence, made by %s", seq_out.lstrip("."),
                 "oiiotool (no ffmpeg)" if oiio_only else "ffmpeg (no oiiotool)")
        warn_unused_for_sequences(cfg, oiio_only)
    encode_ffmpeg = cfg.movie and not cfg.skip_ffmpeg and not oiio_only
    if encode_ffmpeg or (stills and not seq.is_exr) or movie is not None:
        cfg.ffmpeg = resolve_tool(cfg.ffmpeg, base, "ffmpeg", cfg.tool_sources.get("ffmpeg", ""))
        if encode_ffmpeg:
            check_ffmpeg_filters(cfg)
    work_dir, created_dir = prepare_work_dir(cfg, seq)
    log.info("Intermediate files: %s", work_dir)
    created_files = []
    exit_code = EXIT_OK
    info = pick = depth = None
    preview_frame = choose_preview_frame(cfg, full_seq) if cfg.preview else None
    try:
        if movie is not None:
            seq, info = unpack_movie(cfg, movie, seq, work_dir, preview_frame, created_files)
        if seq.is_exr:
            cfg.oiiotool = resolve_tool(cfg.oiiotool, base, "oiiotool",
                                        cfg.tool_sources.get("oiiotool", ""))
            setup_ocio(cfg)
            # header of the first frame: channels, fps, timecode, reel and {meta:...} all
            # come from there
            cfg.header_file = str(seq.path(seq.start))
            if not cfg.clf:                   # a CLF path is known once the header is read
                log.info("Colour: %s", describe_colour(cfg))
            if cfg.display_mode and HDR_OR_WIDE.search(cfg.ocio_display or ""):
                log.warning('Display "%s": the movie is tagged Rec.709 (bt709 primaries); set the '
                            "right colour tags with -ff_encode for HDR / P3 deliveries.", cfg.ocio_display)
            info = get_image_info(cfg.oiiotool, cfg.header_file, cfg.verbose)
            cfg.header_attribs = md.header_attribs(info)
            add_custom_tokens(cfg, seq, info)
            if cfg.write_exr_info:
                write_info_json(info, cfg.header_file + ".json")
            pick = pick_channels(info, cfg.oiio_channel, cfg.oiio_channel_use_first,
                                 cfg.channel_preferences or None)
            depth = plan_depth_view(cfg, info, pick)
            if cfg.alpha == "keep":
                if depth is not None:
                    alpha_missing(cfg, "Alpha: a depth picture has no alpha; the output gets none.")
                else:
                    pick.alpha = find_alpha(pick, info)
                    if not pick.alpha:
                        alpha_missing(cfg, "Alpha: no alpha channel next to %s; the output gets none.",
                                      ",".join(pick.without_alpha))
                cfg.alpha_active = bool(pick.alpha) and depth is None
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

            if cfg.clf:
                cfg.clf_path = resolve_clf(cfg, seq, info)
                log.info("Colour: %s", describe_colour(cfg))
            if cfg.oiio_transform_explicit is not None:
                context = transform_context(cfg, seq, info)
                cfg.oiio_transform_args = [md.expand(a, context) for a in cfg.oiio_transform_explicit]
                cfg.transform_choice = "-oiio_transform_args"
                log.info("OIIO transform: %s", " ".join(cfg.oiio_transform_args) or "(none)")
            elif cfg.oiio_transform is not None:
                cfg.oiio_transform_args = choose_transform(cfg, seq, info)
            if not cfg.movie:
                return make_stills_only(cfg, seq, info, pick, depth, exit_code, work_dir)
            if oiio_only:
                return write_sequence_oiio(cfg, seq, info, pick, depth, work_dir, preview_frame,
                                           exit_code)
            source = seq.path(preview_frame) if cfg.preview else seq.path(seq.start)
            mid = process_args(cfg, pick, depth) + oiio_letterbox_steps(cfg, source,
                                                                         process_args(cfg, pick, depth))
            if cfg.preview:
                result = convert_preview_frame(cfg, seq, preview_frame, pick, work_dir, depth, mid)
            else:
                result = convert_sequence(cfg, seq, pick, work_dir, depth, mid=mid)
            # every target, so partial files of failed frames are removed as well
            created_files += [work_dir / seq.name(f, cfg.mezzanine_ext) for f in seq.frames]
            if result.failed:
                message = "Frames failed to convert: {}".format(format_frames(result.failed))
                if cfg.missing_frames == "error" or not result.outputs:
                    raise TranscodeError(message)
                log.warning("%s (replaced by neighbouring frames)", message)
                exit_code = EXIT_FRAMES_FAILED
            available = result.outputs
            # oiiotool resized to -width, unless a transform preset set the size: then ffmpeg
            # scales the transform's output (whatever size it is) to -width
            scale_width = cfg.width if cfg.oiio_transform_args is not None else 0
        else:
            if oiio_only and cfg.movie:
                info = setup_oiio_for_other_input(cfg, seq, info, base, preview_frame)
                plan_plain_alpha(cfg, seq, info, movie)
                return write_sequence_oiio(cfg, seq, info, None, None, work_dir, preview_frame,
                                           exit_code)
            if cfg.oiio_letterbox is not None and cfg.movie:
                log.warning("-oiio_letterbox: %s input to a movie runs no oiiotool; use "
                            "-letterbox (ffmpeg).", "movie" if movie is not None else seq.ext.lstrip("."))
            add_custom_tokens(cfg, seq, info)
            available = {f: seq.path(f) for f in seq.frames}
            if cfg.preview:
                available = {preview_frame: seq.path(preview_frame)}
            scale_width = cfg.width
            if cfg.clf:
                log.warning("-clf only applies to EXR input (oiiotool); %s frames are used as "
                            "they are.", seq.ext.lstrip("."))
            if not cfg.movie:
                return make_stills_only(cfg, seq, info, pick, depth, exit_code, work_dir)
            if cfg.alpha == "keep" and not cfg.skip_ffmpeg:
                source_fmt = probe_frame(cfg.ffmpeg, available[min(available)], cfg.verbose)
                cfg.alpha_active = has_alpha(source_fmt.pix_fmt)
                if not cfg.alpha_active:
                    alpha_missing(cfg, "Alpha: the %s input has no alpha (%s); the output gets none.",
                                  "movie" if movie is not None else seq.ext.lstrip("."),
                                  source_fmt.pix_fmt)

        if cfg.skip_ffmpeg:
            log.info("Skipping ffmpeg; intermediate files are in %s%s", work_dir,
                     " (no slate: ffmpeg draws it)" if cfg.slate else "")
            if stills:
                make_stills_only(cfg, seq, info, pick, depth, exit_code, work_dir,
                                 sizes=frame_sizes(available) if seq.is_exr else None,
                                 available=available)
            return exit_code

        if cfg.alpha_active:
            log.info("Alpha: kept%s, written %s, %s", " ({})".format(pick.alpha) if seq.is_exr else "",
                     "premultiplied" if cfg.alpha_premult else "straight (unpremultiplied)",
                     cfg.seq_out_ext.lstrip(".") + " sequence" if cfg.image_output
                     else cfg.preset.alpha_pix_fmt if not cfg.ff_encode else "-ff_encode")
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
        timecode, tc_source = resolve_timecode(cfg, seq, info, cfg.header_frame)
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
        if reel_name and not cfg.output_reelid:
            log.info("Reel name: %s (%s), not written (-output_reelid 0)", reel_name, reel_source)
        elif reel_name:
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
        frame_files = ([available[preview_frame]] if cfg.preview
                       else fill_holes(seq.full_range, available))
        slate_file = None
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
            if cfg.burn_margins and cfg.burn_margins != DEFAULT_MARGINS:
                log.info("  margins: %s", ", ".join("{} {}".format(k, v)
                                                    for k, v in cfg.burn_margins.items()))
        lead = 1 if cfg.slate else 0
        counter_base = None
        if cfg.preview:
            # the one picture frame shows its own frame number and running timecode
            counter_base = preview_frame - lead
            movie_tc = offset_timecode(timecode, preview_frame - seq.start - lead,
                                       nominal_fps(cfg.fps))
        ctx = BurnContext(seq.clean_name, seq.start, seq.end, timecode, cfg.fps,
                          cfg.preset.name, today(), cfg.header_attribs,
                          lead_frames=lead, movie_timecode=movie_tc,
                          skip_lead=not (cfg.slate and cfg.slate.burnin), tokens=cfg.name_tokens,
                          counter_base=counter_base)
        video_filter = build_video_filter(cfg, ctx, scale_width)
        if cfg.preview:
            write_preview(cfg, seq, info, list_file, video_filter, work_dir, preview_frame,
                          timecode, movie_tc, reel_name, metadata)
            return exit_code
        if cfg.image_output:
            if metadata:
                log.info("Metadata: not written: ffmpeg writes this %s sequence (a movie input), and "
                         "its image files carry no tags; EXR output (oiiotool) can.",
                         cfg.seq_out_ext.lstrip("."))
            out = resolve_output_seq(cfg, seq, output_context(cfg, seq, info, timecode, reel_name))
            out.folder.mkdir(parents=True, exist_ok=True)
            encode(cfg, build_image_command(cfg, list_file, out, video_filter, seq.start))
            log.info("Done: %s (%s)", out.describe(), format_frames(seq.full_range))
            cfg.stills_folder = out.folder
            context = output_context(cfg, seq, info, timecode, reel_name)
            if cfg.thumbnail is not None:
                make_thumbnail(cfg, seq, info, pick, depth, None, context)
            if cfg.filmstrip is not None:
                make_filmstrip(cfg, seq, pick, depth, work_dir, context, available=available)
            return exit_code
        output = resolve_output(cfg, seq, name, output_context(cfg, seq, info, timecode, reel_name))
        output.parent.mkdir(parents=True, exist_ok=True)
        encode(cfg, build_command(cfg, list_file, output, video_filter, movie_tc, metadata,
                                  reel_name))
        log.info("Done: %s", output)
        if cfg.thumbnail is not None:
            make_thumbnail(cfg, seq, info, pick, depth,
                           frame_sizes(available) if seq.is_exr else None,
                           output_context(cfg, seq, info, timecode, reel_name), movie_output=output)
        if cfg.filmstrip is not None:
            make_filmstrip(cfg, seq, pick, depth, work_dir,
                           output_context(cfg, seq, info, timecode, reel_name), movie_output=output,
                           available=available, slate_file=slate_file)
        return exit_code
    finally:
        if cfg.temp_keep:
            log.info("Intermediate files kept in %s", work_dir)
        else:
            cleanup(created_files, work_dir, created_dir,
                    whole_folder=cfg.temp_location == "user" and created_dir)


def trim_sequence(cfg, seq):
    """seq limited to -in / -out (inclusive frame numbers)."""
    if cfg.trim_in is None and cfg.trim_out is None:
        return seq
    lo = cfg.trim_in if cfg.trim_in is not None else seq.start
    hi = cfg.trim_out if cfg.trim_out is not None else seq.end
    if lo > hi:
        raise TranscodeError("-in {} is after -out {}.".format(lo, hi))
    frames = tuple(f for f in seq.frames if lo <= f <= hi)
    if not frames:
        raise TranscodeError("-in / -out {}-{}: no frames of {}-{} in that range.".format(
            lo, hi, seq.start, seq.end))
    log.info("Trim: frames %d-%d (%d of %d)", frames[0], frames[-1], len(frames), len(seq.frames))
    return replace(seq, frames=frames)


def warn_unused_for_sequences(cfg, oiio_only):
    """An image sequence output has no slate or container metadata; without ffmpeg, no
    burn-ins, ffmpeg letterbox or ffmpeg transform either."""
    if cfg.slate:
        log.warning("Slate: not made for an image sequence output.")
        cfg.slate = None
    if oiio_only:
        unused = [name for name, on in (("-letterbox (use -oiio_letterbox)", cfg.letterbox),
                                        ("-ff_transform", cfg.ff_transform),
                                        ("-ff_filter", cfg.ff_filter.strip())) if on]
        if unused:
            log.warning("No ffmpeg runs for this output, so these are not applied: %s.",
                        ", ".join(unused))
        if any(cfg.burn.values()):
            log.info("Burn-ins: not drawn, no ffmpeg runs for this output.")
        cfg.burn = {}
        cfg.letterbox = None


def oiio_letterbox_steps(cfg, source, mid):
    """oiiotool --box steps for -oiio_letterbox, from the size the steps give one frame
    (measured once)."""
    if cfg.oiio_letterbox is None:
        return []
    if cfg.oiio_letterbox_args is None:
        width, height, channels = outseq.measure(cfg.oiiotool, source, mid, cfg.verbose)
        par = resolve_par(cfg, quiet=True)[0]
        args, kind, bar = outseq.letterbox_args(cfg.oiio_letterbox, width, height, channels, par)
        cfg.oiio_letterbox_args = args
        cfg.oiio_letterbox_info = (kind, bar, width, height)
        if kind == "none":
            log.info("oiiotool letterbox %s: the %dx%d frames already have that ratio, no bars.",
                     cfg.oiio_letterbox.ratio_text, width, height)
        else:
            log.info("oiiotool letterbox %s: %s, bars of %d px on the %dx%d frames",
                     cfg.oiio_letterbox.ratio_text, kind, bar, width, height)
    return list(cfg.oiio_letterbox_args)


def setup_oiio_for_other_input(cfg, seq, info, base, preview_frame):
    """oiiotool for PNG / JPG / a movie's frames: the tool, OCIO when a colour was given, the
    header (for sizes and keywords) and the OIIO transform."""
    cfg.oiiotool = resolve_tool(cfg.oiiotool, base, "oiiotool", cfg.tool_sources.get("oiiotool", ""))
    if cfg.colour_given and not cfg.skip_colour:
        if cfg.clf:
            log.warning("-clf only applies to EXR input; not used for %s frames.", seq.ext.lstrip("."))
        else:
            setup_ocio(cfg)
            log.info("Colour: %s", describe_colour(cfg))
    first = seq.path(preview_frame if preview_frame is not None else seq.start)
    if info is None:
        info = get_image_info(cfg.oiiotool, str(first), cfg.verbose)
        cfg.header_file = str(first)
        cfg.header_attribs = md.header_attribs(info)
    add_custom_tokens(cfg, seq, info)
    if cfg.oiio_transform_explicit is not None:
        context = transform_context(cfg, seq, info)
        cfg.oiio_transform_args = [md.expand(a, context) for a in cfg.oiio_transform_explicit]
        cfg.transform_choice = "-oiio_transform_args"
        log.info("OIIO transform: %s", " ".join(cfg.oiio_transform_args) or "(none)")
    elif cfg.oiio_transform is not None:
        cfg.oiio_transform_args = choose_transform(cfg, seq, info)
    return info


ALPHA_NAMES = ("A", "ALPHA")


def plan_plain_alpha(cfg, seq, info, movie):
    """Alpha for oiiotool on frames that are not EXR: kept with -alpha keep when the frames
    have one, else removed (--ch with the other channels)."""
    if movie is not None:       # the frames were unpacked with alpha only for -alpha keep
        names, alpha = [], cfg.alpha == "keep" and bool(movie.alpha)
    else:
        names = list((info[0] if info else {}).get("channelnames") or [])
        alpha = [n for n in names if n.upper() in ALPHA_NAMES]
    if cfg.alpha == "keep":
        cfg.alpha_active = bool(alpha)
        if alpha:
            log.info("Alpha: kept (%s input)", "movie" if movie is not None else seq.ext.lstrip("."))
        else:
            alpha_missing(cfg, "Alpha: the %s input has no alpha; the output gets none.",
                          "movie" if movie is not None else seq.ext.lstrip("."))
    elif alpha and names:
        cfg.alpha_strip = ",".join(n for n in names if n not in alpha)


def resolve_output_seq(cfg, seq, context):
    """-o as an image sequence (outseq.OutputSeq); never onto the input files."""
    ext = cfg.seq_out_ext.lstrip(".")
    context = dict(context, ext=ext)
    zfill = seq.zfill or 4
    default = "{}.{}.{}".format(seq.clean_name or "frames", "#" * zfill, ext)
    path = resolve_path_template(cfg.output, seq, default, ext, outseq.SEQ_EXT_RE, "-o", context)
    out = outseq.from_path(path, zfill)
    if not seq.frames_folder and any(out.path(f).resolve() == seq.path(f).resolve()
                                     for f in seq.frames[:1] + seq.frames[-1:]):
        raise TranscodeError("-o {}: the output would overwrite the input frames.".format(out.describe()))
    return out


def sequence_mid(cfg, seq, pick, depth, source, tags=None, stamp=()):
    """oiiotool steps for an image sequence output: the movie's processing (EXR) or the plain
    steps (other input), the oiiotool letterbox, the metadata tags, the output format."""
    if seq.is_exr:
        mid = _without_depth(process_args(cfg, pick, depth))
        if pick is not None and pick.alpha and not cfg.skip_colour and cfg.seq_out_ext == ".exr":
            mid += ["--premult"]                    # EXR keeps premultiplied colour
    else:
        mid = plain_args(cfg)
    mid += oiio_letterbox_steps(cfg, source, mid)
    mid += md.oiio_args(tags) + list(stamp)
    return mid + outseq.oiio_output_args(cfg.seq_out_ext, cfg.seq_args, cfg.mezzanine_16bit)


def write_sequence_oiio(cfg, seq, info, pick, depth, work_dir, preview_frame, exit_code):
    """Sequence in -> image sequence out (or a movie's frames -> EXR) with oiiotool alone."""
    cfg.fps, fps_source = resolve_fps(cfg, info)
    cfg.par_in = resolve_par(cfg, quiet=True)[0]
    cfg.par_out_value = cfg.par_in
    timecode, tc_source = resolve_timecode(cfg, seq, info, cfg.header_frame)
    log.info("Frame rate: %s (%s), timecode %s (%s)", fps_display(cfg.fps), fps_source, timecode,
             tc_source)
    source = seq.path(preview_frame if preview_frame is not None else seq.start)
    tags = sequence_metadata(cfg, seq, timecode)
    reel_name, reel_source = resolve_reel_name(cfg, seq, info, timecode)
    if reel_name:
        log.info("Reel name: %s (%s)", reel_name, reel_source)
    per_frame, tc_args = sequence_timecode(cfg, seq, timecode)
    mid = sequence_mid(cfg, seq, pick, depth, source, tags, tc_args + sequence_reel(cfg, reel_name))
    context = output_context(cfg, seq, info, timecode, reel_name)
    if cfg.preview:
        return write_preview_oiio(cfg, seq, info, mid, work_dir, preview_frame, timecode, exit_code,
                                  tags, reel_name)
    out = resolve_output_seq(cfg, seq, context)
    out.folder.mkdir(parents=True, exist_ok=True)
    log.info("Writing %s (%s: %s)", out.describe(), cfg.seq_preset or "built-in settings",
             " ".join(outseq.oiio_output_args(cfg.seq_out_ext, cfg.seq_args, cfg.mezzanine_16bit)))
    result = convert_sequence(cfg, seq, pick, work_dir, depth, target=out, mid=mid, per_frame=per_frame)
    if result.failed:
        message = "Frames failed to convert: {}".format(format_frames(result.failed))
        if cfg.missing_frames == "error" or not result.outputs:
            raise TranscodeError(message)
        log.warning(message)
        exit_code = EXIT_FRAMES_FAILED
    log.info("Done: %s (%s)", out.describe(), format_frames(sorted(result.outputs)))
    cfg.stills_folder = out.folder
    if cfg.thumbnail is not None:
        make_thumbnail(cfg, seq, info, pick, depth, None, context)
    if cfg.filmstrip is not None:
        make_filmstrip(cfg, seq, pick, depth, work_dir, context)
    return exit_code


def write_preview_oiio(cfg, seq, info, mid, work_dir, frame, timecode, exit_code, tags=None,
                       reel_name=""):
    """-preview of an oiiotool-only output: the frame through the same steps into the output
    format, then as 8-bit PNG (an EXR's values clipped, no view transform) and the JSON."""
    image = Path(cfg.preview)
    image.parent.mkdir(parents=True, exist_ok=True)
    written = Path(work_dir) / ("preview_out" + cfg.seq_out_ext)
    run_tool([cfg.oiiotool, "-i", seq.path(frame).as_posix()] + mid + ["-o", written.as_posix()],
             "oiiotool (preview)", cfg.verbose)
    run_tool([cfg.oiiotool, "-i", written.as_posix(), "-d", "uint8", "-o", image.as_posix()],
             "oiiotool (preview png)", cfg.verbose)
    width, height, channels = outseq.measure(cfg.oiiotool, image, [], cfg.verbose)
    context = transform_context(cfg, seq, info)
    is_movie = cfg.header_label == "movie"
    lb = cfg.oiio_letterbox_info
    report = {
        "image": str(image), "slate": None, "frame": frame,
        "frame_file": cfg.header_file if is_movie else str(seq.path(frame)),
        "input": {"first_file": cfg.header_file if is_movie else str(seq.path(seq.start)),
                  "frames": format_frames(seq.frames), "first_frame": seq.start,
                  "last_frame": seq.end, "movie": is_movie, "is_exr": seq.is_exr,
                  "header": cfg.header_attribs, "par": par_display(cfg.par_in)},
        "keywords": {k: v for k, v in context.items() if k != "meta"},
        "output": {"width": width, "height": height, "par": float(cfg.par_in),
                   "pix_fmt": "{} {}".format(cfg.seq_out_ext.lstrip("."), " ".join(
                       outseq.oiio_output_args(cfg.seq_out_ext, cfg.seq_args, cfg.mezzanine_16bit))),
                   "fps": fps_display(cfg.fps), "timecode": timecode,
                   "movie_timecode": offset_timecode(timecode, frame - seq.start, nominal_fps(cfg.fps)),
                   "reel_name": reel_name, "metadata": dict(tags or {}),
                   "timecode_source": cfg.timecode_found, "reel_source": cfg.reel_found,
                   "colour": describe_colour(cfg) if (seq.is_exr and not cfg.skip_colour) or (
                       cfg.colour_given and not cfg.skip_colour) else "none",
                   "transform": cfg.transform_choice, "transform_args": cfg.oiio_transform_args,
                   "letterbox": None if not lb or lb[0] == "none" else {"kind": lb[0], "bar": lb[1]},
                   "encode_preset": "", "pipeline": "oiiotool",
                   "sequence": cfg.seq_out_ext},
    }
    with open(str(image.with_suffix(".json")), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    log.info("Preview: %s (oiiotool, %s output), %s", image, cfg.seq_out_ext.lstrip("."),
             image.with_suffix(".json").name)
    return exit_code


SEEK_UNPACK_RATIO = 8      # stills only: seek frame by frame when the movie is this many times longer


def stills_frames(cfg, seq):
    """Stills without the movie: the frames the thumbnail and the filmstrip will use, when that
    is few enough to seek to one by one; None means unpack the whole movie."""
    if cfg.thumbnail is not None and cfg.thumbnail.frame == "largest":
        return None                           # needs every frame's size
    wanted = set()
    if cfg.thumbnail is not None:
        wanted.add(choose_thumbnail_frame(cfg.thumbnail.frame, seq.frames, None)[0])
    if cfg.filmstrip is not None:
        wanted.update(fs.choose_frames(seq.frames, cfg.filmstrip.layout()[0]))
    if not wanted or len(wanted) * SEEK_UNPACK_RATIO > len(seq.frames):
        return None
    return wanted


def unpack_movie(cfg, movie, seq, work_dir, preview_frame, created_files):
    """Unpack the movie's frames (a preview: only its frame) into work_dir. Returns the
    sequence of the files written and the movie's header."""
    if Path(work_dir).resolve() == Path(seq.folder).resolve():
        clash = [seq.name(f) for f in seq.frames if (Path(work_dir) / seq.name(f)).exists()]
        if clash:
            raise TranscodeError("-temp is empty and the movie's frames would overwrite existing "
                                 "files, e.g. {}".format(clash[0]))
    seq = mv.with_frames(seq, work_dir)
    alpha = cfg.alpha == "keep" and movie.alpha
    some = stills_frames(cfg, seq) if not cfg.movie and preview_frame is None else None
    if preview_frame is not None:
        targets = [preview_frame]
    elif some is not None:
        targets = sorted(some)
    else:
        targets = list(seq.frames)
    created_files += [seq.path(f) for f in targets]
    log.info("Movie: unpacking %s as %s%s", "frame {}".format(preview_frame) if preview_frame
             is not None else "{} frames".format(len(targets)), seq.ext.lstrip("."),
             " with alpha" if alpha else "")
    if some is not None:
        found = mv.unpack_some(cfg.ffmpeg, movie, seq, targets, cfg.mezzanine_16bit, alpha,
                               cfg.verbose, cfg.workers, cfg.movie_first_frame)
    else:
        found = mv.unpack(cfg.ffmpeg, movie, seq, preview_frame, cfg.mezzanine_16bit, alpha,
                          cfg.verbose, cfg.movie_first_frame)
    if preview_frame is None and some is None:
        seq = mv.with_frames(seq, work_dir, found)
    # (a preview or stills: the full range stays, so the frames chosen from it are the ones
    # unpacked)
    info = movie.header()
    cfg.header_file = str(movie.path)
    cfg.header_attribs = md.header_attribs(info)
    return seq, info


def transform_context(cfg, seq, info):
    """Keywords for oiio transform conditions: input size and pixel aspect, the name keywords,
    {clean_name} {frame_start} {frame_end} and {meta:ATTR}."""
    context = dict(cfg.name_tokens)
    context.update({"clean_name": seq.clean_name, "frame_start": seq.start, "frame_end": seq.end,
                    "meta": cfg.header_attribs})
    context.update(input_tokens(info, resolve_par(cfg, quiet=True)[0]))
    return context


CONTAINERS = ("mov", "mp4", "m4v", "mxf", "mkv", "webm", "avi", "mpg", "mpeg", "ts", "gif", "nut",
              "y4m")
_EXT_RE = re.compile(r"^\.(?:{})$".format("|".join(CONTAINERS)), re.I)
_SOURCE_RELATIVE = re.compile(r"^\.{1,2}(?:[\\/]|$)")


OUTPUT_KEYWORDS = {"clean_name", "frame_start", "frame_end", "timecode", "fps", "preset", "date",
                   "meta", "ext", "reel"} | set(NAME_KEYWORDS) | set(INPUT_KEYWORDS)


THUMBNAIL_KEYWORDS = ("frame",)


def check_output_template(template, label="-o", extra=()):
    """Fail before converting anything if -o / -ot uses an unknown keyword."""
    try:
        fields = [f for _l, f, _s, _c in string.Formatter().parse(template) if f is not None]
    except ValueError as exc:
        raise TranscodeError('-o "{}": {}.'.format(template, exc))
    allowed = OUTPUT_KEYWORDS | set(extra)
    unknown = sorted(set(fields) - allowed)
    if unknown:
        known = sorted(allowed - set(NAME_KEYWORDS) - {"meta"})
        raise TranscodeError('{} "{}": unknown keyword(s) {}. Known: {}, {{meta:ATTR}}, '
                             "{{version_number}}, {{namesplit1}} ...".format(
                                 label, template, ", ".join("{" + f + "}" for f in unknown),
                                 " ".join("{" + k + "}" for k in known)))


def container_from_output(template):
    """The -o extension, if it is a plain one (".mov"); none for "{name}.v{version_number}"."""
    suffix = PureWindowsPath(template.strip()).suffix if template else ""
    return suffix if _EXT_RE.match(suffix) else ""


def output_context(cfg, seq, info, timecode, reel_name):
    context = metadata_context(cfg, seq, timecode)
    context.update(ext=cfg.ff_out_ext, reel=reel_name)
    if info:
        context.update(input_tokens(info, cfg.par_in))
    return context


def resolve_output(cfg, seq, name, context):
    """-o with keywords expanded; see resolve_path_template."""
    return resolve_path_template(cfg.output, seq, "{}.{}".format(name, cfg.ff_out_ext),
                                 cfg.ff_out_ext, _EXT_RE, "-o", context)


def resolve_path_template(template, seq, default_name, ext, ext_re, label, context,
                          default_folder=None):
    """Keywords expanded. ./ and ../ are relative to the input sequence's folder and .. is
    resolved; a folder (trailing slash, "." / "..", or an existing folder) gets the default
    file name; the extension is added when the name lacks a known one."""
    template = template.strip()
    if not template:
        return Path(default_folder or seq.folder) / default_name
    expanded = md.expand(template, context).strip()
    unknown = re.findall(r"\{[^{}]*\}", expanded)
    if unknown:
        raise TranscodeError('{} "{}": unknown keyword(s) {}.'.format(label, template,
                                                                     ", ".join(unknown)))
    is_folder = expanded.endswith(("/", "\\")) or expanded in (".", "..")
    if _SOURCE_RELATIVE.match(expanded):
        path = os.path.normpath(os.path.join(str(seq.folder), expanded))
    else:
        path = os.path.normpath(expanded)
    if is_folder or os.path.isdir(path):
        path = os.path.join(path, default_name)
    else:
        suffix = PureWindowsPath(path).suffix
        if not ext_re.match(suffix):
            path += "." + ext
        elif suffix.lower().lstrip(".") not in (ext.lower(), "jpeg" if ext == "jpg" else ext):
            log.warning("%s ends in %s but the format is .%s; the file name decides, so the "
                        "settings for .%s may not fit.", label, suffix, ext, ext)
    if template != path:
        log.info('%s: "%s" -> %s', {"-o": "Output", "-of": "Filmstrip path"}.get(label, "Thumbnail path"),
                 template, path)
    return Path(path)


PREVIEW_CACHE_FILES = 60


def choose_preview_frame(cfg, seq):
    frame, how = choose_thumbnail_frame(cfg.preview_frame, seq.frames, None)
    log.info("Preview: frame %d (%s)", frame, how)
    return frame


def convert_preview_frame(cfg, seq, frame, pick, work_dir, depth, mid=None):
    """oiiotool for the one preview frame, cached by its exact oiiotool arguments and source
    file, so burn-in / slate / letterbox changes don't convert it again."""
    import hashlib
    from .oiio import ConversionResult
    if mid is None:
        mid = process_args(cfg, pick, depth)
    src = seq.path(frame)
    stat = os.stat(str(src))
    parts = [str(src), str(stat.st_mtime_ns), str(stat.st_size), cfg.mezzanine_ext,
             os.environ.get("OCIO", "")] + list(mid)
    key = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    cache_dir = Path(tempfile.gettempdir()) / TEMP_ROOT_NAME / "preview-cache"
    cached = cache_dir / (key + cfg.mezzanine_ext)
    target = Path(work_dir) / seq.name(frame, cfg.mezzanine_ext)
    if cached.is_file():
        shutil.copyfile(str(cached), str(target))
        log.info("Preview: converted frame taken from the cache")
        return ConversionResult(outputs={frame: target})
    result = convert_sequence(cfg, replace(seq, frames=(frame,)), pick, work_dir, depth, mid=mid)
    if frame in result.outputs:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(result.outputs[frame]), str(cached))
            old = sorted(cache_dir.iterdir(), key=lambda p: p.stat().st_mtime)[:-PREVIEW_CACHE_FILES]
            for path in old:
                path.unlink()
        except OSError as exc:
            log.debug("Preview cache: %s", exc)
    return result


def preview_pix_fmt(cfg):
    if cfg.image_output:
        return "rgb24"
    args = encode_args(cfg, "{videofilter}")
    if "-pix_fmt" in args[:-1]:
        return args[args.index("-pix_fmt") + 1]
    return "yuv420p"


def write_preview(cfg, seq, info, list_file, video_filter, work_dir, frame, timecode, movie_tc,
                  reel_name, metadata):
    """-preview: the movie's filter chain on the slate (if any) and the one frame, written as
    PNG through the codec's pixel format (so chroma subsampling and range look like the movie),
    plus a JSON file with what the tuner shows."""
    image = Path(cfg.preview)
    image.parent.mkdir(parents=True, exist_ok=True)
    slate = image.with_name(image.stem + "_slate" + image.suffix) if cfg.slate else None
    pix_fmt = preview_pix_fmt(cfg)
    rgb = "rgba" if cfg.alpha_active else "rgb24"
    chain = ",".join([video_filter, "format=" + pix_fmt,
                      "scale=in_range=tv:in_color_matrix=bt709:out_range=pc", "format=" + rgb])
    pattern = Path(work_dir) / "preview_%d.png"
    cmd = [cfg.ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
           "-r", fps_to_str(cfg.fps), "-i", Path(list_file).as_posix(), "-vf", chain,
           "-f", "image2", "-start_number", "0", Path(pattern).as_posix()]
    run_tool(cmd, "ffmpeg (preview)", cfg.verbose)
    frames = [Path(str(pattern).replace("%d", str(i))) for i in range(2 if slate else 1)]
    targets = [slate, image] if slate else [image]
    for src, dst in zip(frames, targets):
        shutil.move(str(src), str(dst))
    fmt = probe_frame(cfg.ffmpeg, image, cfg.verbose)
    context = transform_context(cfg, seq, info)
    is_movie = cfg.header_label == "movie"
    report = {
        "image": str(image), "slate": str(slate) if slate else None,
        "frame": frame,
        "frame_file": cfg.header_file if is_movie else str(seq.path(frame)),
        "input": {"first_file": cfg.header_file if is_movie else str(seq.path(seq.start)),
                  "frames": format_frames(seq.frames), "first_frame": seq.start,
                  "last_frame": seq.end, "movie": is_movie,
                  "is_exr": seq.is_exr, "header": cfg.header_attribs,
                  "par": par_display(cfg.par_in)},
        "keywords": {k: v for k, v in context.items() if k != "meta"},
        "output": {"width": fmt.width, "height": fmt.height, "par": float(cfg.par_out_value),
                   "pix_fmt": pix_fmt, "fps": fps_display(cfg.fps), "timecode": timecode,
                   "movie_timecode": movie_tc, "reel_name": reel_name, "metadata": metadata,
                   "timecode_source": cfg.timecode_found, "reel_source": cfg.reel_found,
                   "colour": describe_colour(cfg) if seq.is_exr else "",
                   "transform": cfg.transform_choice,
                   "transform_args": cfg.oiio_transform_args,
                   "letterbox": (None if cfg.letterbox_plan is None else
                                 {"kind": cfg.letterbox_plan.kind, "bar": cfg.letterbox_plan.bar}),
                   "encode_preset": cfg.preset.name},
    }
    json_path = image.with_suffix(".json")
    with open(str(json_path), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    log.info("Preview: %s%s, %s", image, " + " + str(slate) if slate else "", json_path.name)


def frame_sizes(available):
    """{frame: bytes} of the converted intermediates, for the "largest" thumbnail."""
    sizes = {}
    for frame, path in available.items():
        try:
            sizes[frame] = os.path.getsize(str(path))
        except OSError:
            pass
    return sizes


def make_stills_only(cfg, seq, info, pick, depth, exit_code, work_dir, sizes=None, available=None):
    """Thumbnail and/or filmstrip without a movie (-ot / -of without -o, or -skipffmpeg): the
    keywords of the movie's context are resolved here, as the movie step did not run."""
    cfg.par_in = resolve_par(cfg, quiet=True)[0]
    cfg.fps = resolve_fps(cfg, info)[0]
    timecode = resolve_timecode(cfg, seq, info, seq.start)[0]
    reel_name = resolve_reel_name(cfg, seq, info, timecode)[0] if cfg.reelid_source != "off" else ""
    if not cfg.movie:
        what = " and ".join(name for name, item in (("thumbnail", cfg.thumbnail),
                                                    ("filmstrip", cfg.filmstrip)) if item is not None)
        log.info("No -o: only the %s %s made (add -o ./ for the movie as well).", what,
                 "are" if " and " in what else "is")
    context = output_context(cfg, seq, info, timecode, reel_name)
    if cfg.thumbnail is not None:
        make_thumbnail(cfg, seq, info, pick, depth, sizes, context)
    if cfg.filmstrip is not None:
        make_filmstrip(cfg, seq, pick, depth, work_dir, context, available=available)
    return exit_code


_THUMB_EXT_RE = re.compile(r"^\.(?:jpg|jpeg|png)$", re.I)


def make_thumbnail(cfg, seq, info, pick, depth, sizes, context, movie_output=None):
    """Write the thumbnail; returns its path. sizes: {frame: bytes} of the movie's converted
    frames (for "largest"), or None."""
    preset = cfg.thumbnail
    frame, how = choose_thumbnail_frame(preset.frame, seq.frames, sizes)
    name = seq.clean_name or seq.folder.name or "thumbnail"
    context = dict(context, frame=frame, ext=preset.ext)
    default_folder = movie_output.parent if movie_output is not None else (cfg.stills_folder or seq.folder)
    path = resolve_path_template(cfg.thumbnail_output or "", seq, "{}.{}".format(name, preset.ext),
                                 preset.ext, _THUMB_EXT_RE, "-ot", context, default_folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    par = cfg.par_in
    src = seq.path(frame)
    if seq.is_exr:
        # the thumbnail's own size replaces the movie's -width resize (one resize, not two);
        # an OIIO transform stays, it is part of the look (crop, reformat)
        look = replace(cfg, width=0) if (preset.width or preset.height) else cfg
        look = replace(look, skip_colour=False)       # stills are display-referred
        cmd = thumbnail_oiiotool(cfg.oiiotool, src, path, process_args(look, pick, depth), preset,
                                 par, alpha=bool(pick is not None and pick.alpha and depth is None))
        run_tool(cmd, "oiiotool (thumbnail)", cfg.verbose)
    else:
        cmd = thumbnail_ffmpeg(cfg.ffmpeg, src, path, preset, par,
                               keep_alpha=cfg.alpha == "keep" and preset.format == "png")
        run_tool(cmd, "ffmpeg (thumbnail)", cfg.verbose)
    log.info("Thumbnail: frame %d (%s), %s -> %s", frame, how, preset.describe().split(", ", 1)[1],
             path)
    return path


_FILMSTRIP_EXT_RE = re.compile(r"^\.(?:jpg|jpeg)$", re.I)


def filmstrip_plan(preset, seq, movie_output, available, slate_file, movie_has_slate):
    """(source, slate wanted and possible, frames) after checking what this run has."""
    source = preset.source
    if source == "movie" and movie_output is None:
        log.warning("Filmstrip: -filmstrip_source movie needs the movie (-o, without -skipffmpeg); "
                    "using the source frames.")
        source = "source"
    if source == "intermediates":
        if not seq.is_exr:
            log.info("Filmstrip: %s input has no intermediates; the source frames are used.",
                     seq.ext.lstrip("."))
            source = "source"
        elif not available:
            log.warning("Filmstrip: -filmstrip_source intermediates needs the movie's converted "
                        "frames (-o or -skipffmpeg); using the source frames.")
            source = "source"
    slate = False
    if preset.slate:
        slate = movie_has_slate if source == "movie" else slate_file is not None
        if not slate:
            log.warning("Filmstrip: no slate in this run%s; all tiles are frames.",
                        " (-skipffmpeg makes none)" if available and movie_output is None else "")
    count = preset.layout()[0]
    pool = sorted(available) if source == "intermediates" else seq.frames
    frames = fs.choose_frames(pool, count - 1 if slate else count)
    return source, slate, frames


def make_filmstrip(cfg, seq, pick, depth, work_dir, context, movie_output=None, available=None,
                   slate_file=None):
    """Write the filmstrip jpg; returns its path. available: {frame: file} the movie was made
    from (EXR: the intermediates); slate_file: the movie's slate image, if any."""
    preset = cfg.filmstrip
    count = preset.layout()[0]
    source, slate, frames = filmstrip_plan(preset, seq, movie_output, available, slate_file,
                                           bool(cfg.slate))
    name = seq.clean_name or seq.folder.name or "filmstrip"
    context = dict(context, ext=preset.ext)
    default_folder = movie_output.parent if movie_output is not None else (cfg.stills_folder or seq.folder)
    path = resolve_path_template(cfg.filmstrip_output or "", seq, "{}_filmstrip.{}".format(
        name, preset.ext), preset.ext, _FILMSTRIP_EXT_RE, "-of", context, default_folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    used = len(frames) + (1 if slate else 0)
    if used < count:
        log.info("Filmstrip: %d frames for %d tiles; the rest stay black.", len(frames),
                 count - (1 if slate else 0))
    failed = []
    if source == "movie":
        lead = 1 if cfg.slate else 0
        indices = ([0] if slate else []) + [lead + f - seq.start for f in frames]
        run_tool(fs.ffmpeg_movie_command(cfg.ffmpeg, movie_output, indices, path, preset,
                                         cfg.par_out_value), "ffmpeg (filmstrip)", cfg.verbose)
    elif seq.is_exr:
        chans = fs.channel_args(alpha=bool(pick is not None and pick.alpha and depth is None),
                                mono=bool(depth is None and pick is not None and not pick.alpha
                                          and len(pick.without_alpha) == 1))
        jobs = [("the slate", slate_file, chans)] if slate else []
        if source == "intermediates":
            jobs += [("frame {}".format(f), available[f], chans) for f in frames]
        else:
            # the tile size replaces the movie's -width resize (one resize, not two)
            mid = fs.source_tile_args(process_args(replace(cfg, width=0, skip_colour=False), pick, depth)) + chans
            jobs += [("frame {}".format(f), seq.path(f), mid) for f in frames]
        failed = fs.render_oiiotool(cfg.oiiotool, jobs, path, preset, work_dir, cfg.par_in,
                                    workers=cfg.workers, verbose=cfg.verbose, keep=cfg.temp_keep,
                                    name=name)
    else:
        files = ([slate_file] if slate else []) + [seq.path(f) for f in frames]
        list_file = Path(work_dir) / "{}.filmstrip.ffconcat.txt".format(name)
        write_concat_list(files, Fraction(25), list_file)
        try:
            run_tool(fs.ffmpeg_files_command(cfg.ffmpeg, list_file, path, preset, cfg.par_in),
                     "ffmpeg (filmstrip)", cfg.verbose)
        finally:
            if not cfg.temp_keep:
                try:
                    list_file.unlink()
                except OSError:
                    pass
    log.info("Filmstrip: %s%d frames (%s) from the %s%s, %s -> %s", "slate + " if slate else "",
             len(frames), format_frames(frames) if len(frames) <= 8 else "{}-{} evenly spaced".format(
                 frames[0], frames[-1]) if frames else "none", source,
             "; failed: {}".format(", ".join(failed)) if failed else "",
             preset.describe().rsplit(", from the", 1)[0], path)
    return path


def resolve_clf(cfg, seq, info):
    """-clf with keywords expanded, ./ resolved; the file must exist."""
    context = transform_context(cfg, seq, info)
    expanded = md.expand(cfg.clf, context).strip()
    unknown = re.findall(r"\{[^{}]*\}", expanded)
    path = resolve_path(expanded, app_dir())
    if not os.path.isfile(path):
        raise TranscodeError('CLF file not found: "{}"{}{}'.format(
            path, ' (from "{}")'.format(cfg.clf) if expanded != cfg.clf else "",
            "; unknown keyword(s) {}".format(", ".join(unknown)) if unknown else ""))
    if expanded != cfg.clf:
        log.info('CLF "%s" -> %s', cfg.clf, path)
    return path


def choose_transform(cfg, seq, info):
    """The matching sub-preset's oiiotool arguments, or None (then -width applies)."""
    preset = cfg.oiio_transform
    context = transform_context(cfg, seq, info)
    sub, details = select_transform(preset, context, md.expand)
    size = "{}x{}".format(context["input_width"], context["input_height"])
    if sub is None:
        log.warning('OIIO transform "%s": no sub-preset matches the %s input (pixel aspect %s); '
                    "using -width %s.", preset.name, size, context["input_par"], cfg.width)
        return None
    args = transform_args(sub, context, md.expand)
    cfg.transform_choice = "{} / {}".format(preset.name, sub.name)
    log.info('OIIO transform "%s": %s input -> sub "%s"%s: %s', preset.name, size, sub.name,
             " ({})".format("; ".join(details)) if details else "", " ".join(args) or "(no arguments)")
    if cfg.width > 0:
        log.info("  then ffmpeg scales the result to width %s (-width 0 keeps its size)", cfg.width)
    return args


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
                   colour=colour_label(cfg, seq), reel=reel_name,
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


def presets_choice_for(argv):
    """The presets file to use: -presets_file, else the studio preferences from
    vfx-transcode.ini, else ./presets.json. "-preset" must not be taken for "-presets_file"."""
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("-presets_file", default=None)
    pre.add_argument("-preset")   # exact match, else some Pythons read it as -presets_file
    known, _ = pre.parse_known_args(argv)
    if known.presets_file:
        return studio.PresetsChoice(resolve_path(known.presets_file, app_dir()), "-presets_file")
    return studio.choose_presets()


def presets_path_for(argv):
    return presets_choice_for(argv).path


def load_presets_for(argv, choice=None):
    """Load the presets file before the full parse, so --help can list the presets."""
    choice = choice or presets_choice_for(argv)
    try:
        return (choice.presets or load_presets(choice.path)), None
    except TranscodeError as exc:
        return None, exc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    choice = presets_choice_for(argv)
    try:
        argv = sp.expand_argv(argv, choice.path)
    except TranscodeError as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    presets, presets_error = load_presets_for(argv, choice)
    ns = build_parser(presets, presets_error).parse_args(argv)
    setup_logging(ns.verbose)
    for problem in choice.problems:
        log.warning("Studio preferences not used: %s; using %s", problem, choice.path)
    if choice.presets is not None:
        log.info("Presets: %s (%s)", choice.path, choice.source)
    try:
        if ns.ocio_info:
            return show_ocio_info(ns, presets)
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
