"""Thumbnails: one still image next to (or instead of) the movie.

EXR input: a separate oiiotool run on one source frame with the same processing as the movie
(channels, alpha, OIIO transform or -width resize, colour / CLF, -oiio_command), then the
thumbnail preset's resize and file format. No burn-ins, letterbox or slate. It does not use
the movie's intermediate files, so -ot without -o can make only a thumbnail.
Other input: ffmpeg scales one source frame.
"""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import TranscodeError

log = logging.getLogger(__name__)

NO_THUMBNAIL = "none"
FRAME_CHOICES = ("middle", "first", "last", "largest")
FORMATS = {"jpg": "jpg", "jpeg": "jpg", "png": "png"}
THUMBNAIL_EXTS = ("jpg", "jpeg", "png")
PRESET_FIELDS = ("notes", "frame", "width", "height", "format", "quality", "compression", "bits")


@dataclass(frozen=True)
class ThumbnailPreset:
    name: str
    frame: str = "middle"      # middle | first | last | largest | a frame number
    width: int = 0             # 0 = follow the other side (both 0: the movie's size)
    height: int = 0
    format: str = "jpg"        # jpg | png
    quality: int = 90          # jpg, 1-100
    compression: int = 6       # png, 0 (none, fastest) - 9 (smallest)
    bits: int = 8              # png: 8 or 16; jpg is always 8
    notes: str = ""

    @property
    def ext(self):
        return self.format

    def describe(self):
        size = ("{}x{}".format(self.width or "auto", self.height or "auto")
                if (self.width or self.height) else "movie size")
        detail = ("quality {}".format(self.quality) if self.format == "jpg"
                  else "compression {}, {}-bit".format(self.compression, self.bits))
        return "{} frame, {}, {} {}".format(self.frame, size, self.format, detail)


def parse_frame(value, where="-thumbnail_frame"):
    text = str(value).strip().lower()
    if text in FRAME_CHOICES or re.fullmatch(r"-?\d+", text):
        return text
    raise TranscodeError('{}: use middle, first, last, largest or a frame number, got "{}".'
                         .format(where, value))


def _int(data, key, low, high, where, default):
    value = data.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = None
    if isinstance(value, bool) or number is None or not low <= number <= high or number != float(value):
        raise TranscodeError("{}: {} must be a whole number from {} to {}, got {!r}."
                             .format(where, key, low, high, value))
    return number


def preset_from_json(name, data, path):
    where = '{}: thumbnail preset "{}"'.format(path, name)
    if name == NO_THUMBNAIL:
        raise TranscodeError('{}: "{}" is reserved.'.format(path, name))
    if not isinstance(data, dict):
        raise TranscodeError("{} must be an object.".format(where))
    unknown = sorted(set(data) - set(PRESET_FIELDS))
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
            where, ", ".join(unknown), ", ".join(PRESET_FIELDS)))
    fmt = FORMATS.get(str(data.get("format", "jpg")).strip().lower().lstrip("."))
    if not fmt:
        raise TranscodeError('{}: format must be jpg or png, got "{}".'.format(where, data.get("format")))
    bits = _int(data, "bits", 8, 16, where, 8)
    if bits not in (8, 16):
        raise TranscodeError("{}: bits must be 8 or 16.".format(where))
    if fmt == "jpg" and bits != 8:
        raise TranscodeError("{}: jpg is 8-bit only.".format(where))
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = " ".join(str(n) for n in notes)
    return ThumbnailPreset(
        name=name, frame=parse_frame(data.get("frame", "middle"), where + ": frame"),
        width=_int(data, "width", 0, 65536, where, 0), height=_int(data, "height", 0, 65536, where, 0),
        format=fmt, quality=_int(data, "quality", 1, 100, where, 90),
        compression=_int(data, "compression", 0, 9, where, 6), bits=bits, notes=str(notes))


def presets_from_json(data, path):
    if not isinstance(data, dict):
        raise TranscodeError('{}: "thumbnail_presets" must be an object.'.format(path))
    return OrderedDict((name, preset_from_json(name, value, path)) for name, value in data.items())


def to_json(preset):
    """The preset as its presets-file entry: only the fields the format uses."""
    data = OrderedDict()
    if preset.notes:
        data["notes"] = preset.notes
    data["frame"] = preset.frame
    data["width"], data["height"] = preset.width, preset.height
    data["format"] = preset.format
    if preset.format == "jpg":
        data["quality"] = preset.quality
    else:
        data["compression"], data["bits"] = preset.compression, preset.bits
    return data


def parse_size(value, where="-thumbnail_size"):
    """(width, height) from WxH; 0 or empty = follow the other side: 1920x0, 1920x, x540."""
    match = re.fullmatch(r"\s*(\d*)\s*[xX*]\s*(\d*)\s*", str(value))
    if not match:
        raise TranscodeError('{}: use WIDTHxHEIGHT, 0 or empty for auto (e.g. 1920x0, x540), '
                             'got "{}".'.format(where, value))
    width, height = (int(v) if v else 0 for v in match.groups())
    if width > 65536 or height > 65536:
        raise TranscodeError("{}: at most 65536 pixels per side.".format(where))
    return width, height


def apply_options(preset, size=None, fmt=None, quality=None, compression=None, bits=None):
    """The command line over the preset, checked like a preset from the presets file.
    -thumbnail_format png without -thumbnail_bits keeps the preset's bits; jpg sets 8."""
    data = to_json(preset)
    data.update(quality=preset.quality, compression=preset.compression, bits=preset.bits)
    if size is not None:
        data["width"], data["height"] = parse_size(size)
    if fmt is not None:
        data["format"] = fmt
        if FORMATS.get(str(fmt).strip().lower().lstrip(".")) == "jpg" and bits is None:
            data["bits"] = 8
    for key, value in (("quality", quality), ("compression", compression), ("bits", bits)):
        if value is not None:
            try:
                data[key] = int(value)
            except (TypeError, ValueError):
                raise TranscodeError("-thumbnail_{}: a whole number, got {!r}.".format(key, value))
    return preset_from_json(preset.name, data, "command line")


# --- which frame ---

def choose_frame(choice, frames, sizes=None):
    """(frame, how it was chosen). frames: the frames on disk. sizes: {frame: bytes} of the
    converted intermediates, only when the movie was made; needed for "largest"."""
    frames = sorted(frames)
    if choice == "first":
        return frames[0], "first"
    if choice == "last":
        return frames[-1], "last"
    middle = frames[0] + (frames[-1] - frames[0]) // 2
    middle = min(frames, key=lambda f: (abs(f - middle), f))
    if choice == "middle":
        return middle, "middle"
    if choice == "largest":
        if not sizes:
            return middle, "middle; largest needs the movie's converted frames"
        if len(set(sizes.values())) == 1:
            return middle, "middle; all converted frames are the same size"
        frame = max(sorted(sizes), key=lambda f: sizes[f])
        return frame, "largest, {} bytes".format(sizes[frame])
    wanted = int(choice)
    frame = min(frames, key=lambda f: (abs(f - wanted), f))
    return frame, "frame {}".format(wanted) + ("" if frame == wanted else ", nearest to it")


# --- commands ---

def _without_depth(args):
    """The movie's oiiotool arguments minus the intermediate's -d bit depth."""
    out = list(args)
    for i in range(len(out) - 1):
        if out[i] == "-d":
            del out[i:i + 2]
            break
    return out


def oiiotool_command(oiiotool, src, dst, movie_args, preset, par=Fraction(1), alpha=False):
    args = _without_depth(movie_args)
    if alpha and preset.format == "jpg":
        args += ["--ch", "0,1,2"]                       # jpg has no alpha
    if par != 1:
        # square pixels for viewing: tell oiiotool the pixel aspect, then let it re-scale
        args += ["--attrib:type=float", "PixelAspectRatio", "{:g}".format(float(par)),
                 "--pixelaspect", "1"]
    if preset.width or preset.height:
        args += ["--resize", "{}x{}".format(preset.width, preset.height)]
    args += ["-d", "uint16" if preset.format == "png" and preset.bits == 16 else "uint8"]
    if preset.format == "jpg":
        args += ["--compression", "jpeg:{}".format(preset.quality)]
    else:
        args += ["--attrib:type=int", "png:compressionLevel", str(preset.compression)]
    return [oiiotool, "-i", Path(src).as_posix()] + args + ["-o", Path(dst).as_posix()]


def jpeg_qscale(quality):
    """1-100 quality -> ffmpeg mjpeg -q:v 31 (worst) .. 2 (best)."""
    return max(2, min(31, int(round(2 + (100 - quality) * 29 / 99))))


def ffmpeg_command(ffmpeg, src, dst, preset, par=Fraction(1), keep_alpha=False):
    from .ffmpeg import make_filter
    filters = []
    if par != 1:
        filters.append(make_filter("scale", [("w", "trunc(iw*{}/{}/2)*2".format(par.numerator,
                                                                               par.denominator)),
                                             ("h", "ih")]))
    if preset.width or preset.height:
        filters.append(make_filter("scale", [("w", str(preset.width or -1)),
                                             ("h", str(preset.height or -1))]))
    filters.append(make_filter("setsar", [("sar", "1")]))
    if preset.format == "png":
        pix = ("rgba64be" if keep_alpha else "rgb48be") if preset.bits == 16 else \
              ("rgba" if keep_alpha else "rgb24")
        filters.append(make_filter("format", [("pix_fmts", pix)]))
        codec = ["-compression_level", str(preset.compression)]
    else:
        codec = ["-q:v", str(jpeg_qscale(preset.quality))]
    return ([ffmpeg, "-hide_banner", "-y", "-i", Path(src).as_posix(), "-vf", ",".join(filters),
             "-frames:v", "1", "-update", "1"] + codec + [Path(dst).as_posix()])
