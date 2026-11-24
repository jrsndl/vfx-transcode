"""Image sequences as output: -o naming an image file (".exr", ".png", ".jpg" ...).

Which tool makes the frames:
  sequence in (EXR or not) -> sequence out   oiiotool only, no ffmpeg
  movie in -> EXR sequence                   ffmpeg unpacks, oiiotool writes the EXRs
  movie in -> other image sequence           ffmpeg only (burn-ins and the ffmpeg transform work)

The frame number pattern in the name sets the padding: "####" (4), "%05d" (5), "@@@@" (4).
Without one, ".####" is added before the extension with the input's padding. Frame numbers are
the input's (trimmed with -in / -out).
"""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PureWindowsPath

from .errors import TranscodeError
from .tools import run_tool

log = logging.getLogger(__name__)

SEQ_EXTS = (".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".dpx", ".bmp")
SEQ_EXT_RE = re.compile(r"^\.(?:exr|png|jpg|jpeg|tif|tiff|dpx|bmp)$", re.I)
_PATTERN_RE = re.compile(r"(#+|@+|%0?(\d*)d)$")

# oiiotool output arguments per format (-seq_args replaces them)
OIIO_OUTPUT_ARGS = {
    ".exr": ["-d", "half", "--compression", "zip"],
    ".png": ["-d", "uint8"],
    ".jpg": ["-d", "uint8", "--compression", "jpeg:95"],
    ".jpeg": ["-d", "uint8", "--compression", "jpeg:95"],
    ".tif": ["-d", "uint8"],
    ".tiff": ["-d", "uint8"],
    ".dpx": ["-d", "uint10"],
    ".bmp": ["-d", "uint8"],
}
SIXTEEN_BIT = (".png", ".tif", ".tiff")


def norm_ext(ext):
    """"jpeg" / ".JPG" -> "jpg", "tiff" -> "tif"."""
    ext = str(ext or "").strip().lower().lstrip(".")
    return {"jpeg": "jpg", "tiff": "tif"}.get(ext, ext)


SEQUENCE_PRESET_FIELDS = ("notes", "ext", "args")


@dataclass(frozen=True)
class SequencePreset:
    """oiiotool output settings for an image sequence -o (presets.json "sequence_presets")."""
    name: str
    ext: str                   # "exr", "jpg", "png" ... (normalised)
    args: tuple                # oiiotool output arguments: -d, --compression, --attrib ...
    notes: str = ""

    def describe(self):
        return "{}: {}".format(self.ext, " ".join(self.args))


def sequence_presets_from_json(data, defaults, path):
    """({name: SequencePreset}, {ext: default preset name})."""
    from .tools import split_args
    presets = OrderedDict()
    if not isinstance(data or {}, dict):
        raise TranscodeError('{}: "sequence_presets" must be an object.'.format(path))
    for name, raw in (data or {}).items():
        where = '{}: sequence preset "{}"'.format(path, name)
        if not isinstance(raw, dict):
            raise TranscodeError("{} must be an object.".format(where))
        unknown = sorted(set(raw) - set(SEQUENCE_PRESET_FIELDS))
        if unknown:
            raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
                where, ", ".join(unknown), ", ".join(SEQUENCE_PRESET_FIELDS)))
        ext = norm_ext(raw.get("ext"))
        if "." + ext not in SEQ_EXTS:
            raise TranscodeError("{}: ext must be one of {}, got {!r}.".format(
                where, ", ".join(e.lstrip(".") for e in SEQ_EXTS), raw.get("ext")))
        args = raw.get("args", [])
        args = split_args(args) if isinstance(args, str) else [str(a) for a in args]
        notes = raw.get("notes", "")
        presets[name] = SequencePreset(name, ext, tuple(args),
                                       " ".join(notes) if isinstance(notes, list) else str(notes))
    if not isinstance(defaults or {}, dict):
        raise TranscodeError('{}: "default_sequence_presets" must be an object like '
                             '{{"exr": "NAME"}}.'.format(path))
    default_map = OrderedDict()
    for ext, name in (defaults or {}).items():
        if name not in presets:
            raise TranscodeError('{}: default sequence preset "{}" for {} is not defined.'.format(
                path, name, ext))
        if presets[name].ext != norm_ext(ext):
            raise TranscodeError('{}: default sequence preset for {} is "{}", a {} preset.'.format(
                path, ext, name, presets[name].ext))
        default_map[norm_ext(ext)] = name
    return presets, default_map


def sequence_ext(template):
    """The image extension of an -o template (".exr"), or "" for a movie / folder."""
    text = (template or "").strip()
    if not text or text.endswith(("/", "\\")):
        return ""
    suffix = PureWindowsPath(text).suffix
    return suffix.lower() if SEQ_EXT_RE.match(suffix) else ""


@dataclass(frozen=True)
class OutputSeq:
    folder: Path
    base: str                  # before the frame number, separator included
    ext: str
    zfill: int

    def name(self, frame, ext=None):
        return "{}{:0{}d}{}".format(self.base, frame, self.zfill, self.ext if ext is None else ext)

    def path(self, frame):
        return self.folder / self.name(frame)

    @property
    def is_exr(self):
        return self.ext.lower() == ".exr"

    def describe(self):
        return str(self.folder / "{}{}{}".format(self.base, "#" * self.zfill, self.ext))


def from_path(path, default_zfill=4):
    """OutputSeq from a resolved output path: "D:/out/sh010.####.exr", "sh010.%04d.png",
    "sh010.exr" (padding added)."""
    path = Path(path)
    stem, ext = path.stem, path.suffix
    match = _PATTERN_RE.search(stem)
    if match:
        pattern = match.group(1)
        if pattern.startswith("%"):
            zfill = int(match.group(2) or 1)
        else:
            zfill = len(pattern)
        base = stem[:match.start()]
    else:
        zfill = default_zfill
        base = stem if stem.endswith(("_", ".", "-")) else stem + "."
    if not base:
        raise TranscodeError('-o "{}": the file name needs a name before the frame number.'.format(path))
    return OutputSeq(path.parent, base, ext, max(1, zfill))


def oiio_output_args(ext, user_args=None, sixteen_bit=False):
    if user_args:
        return list(user_args)
    args = list(OIIO_OUTPUT_ARGS.get(ext.lower(), ["-d", "uint8"]))
    if sixteen_bit and ext.lower() in SIXTEEN_BIT:
        args[args.index("uint8")] = "uint16"
    return args


def ffmpeg_output_args(ext, sixteen_bit=False, alpha=False):
    """ffmpeg codec arguments for an image sequence (movie in -> image sequence out); alpha
    only for png and tif."""
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        return ["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj444p"]
    if ext == ".png":
        fmt = ("rgba64be" if sixteen_bit else "rgba") if alpha else ("rgb48be" if sixteen_bit else "rgb24")
        return ["-c:v", "png", "-pix_fmt", fmt]
    if ext in (".tif", ".tiff"):
        fmt = ("rgba64le" if sixteen_bit else "rgba") if alpha else ("rgb48le" if sixteen_bit else "rgb24")
        return ["-c:v", "tiff", "-pix_fmt", fmt]
    if ext == ".dpx":
        return ["-c:v", "dpx", "-pix_fmt", "gbrp10le"]
    if ext == ".bmp":
        return ["-c:v", "bmp", "-pix_fmt", "bgr24"]
    raise TranscodeError("ffmpeg does not write {} here; use oiiotool (EXR input).".format(ext))


# --- letterbox with oiiotool -----------------------------------------------------------------

def measure(oiiotool, src, mid, verbose=0):
    """(width, height, channels) of what the oiiotool steps make of one frame."""
    cmd = [oiiotool, "-i", Path(src).as_posix()] + list(mid) + [
        "--echo", "VT_SIZE {TOP.width} {TOP.height} {TOP.nchannels}"]
    result = run_tool(cmd, "oiiotool (measure)", verbose)
    match = re.search(r"VT_SIZE (\d+) (\d+) (\d+)", result.stdout or "")
    if not match:
        raise TranscodeError("oiiotool did not report the picture size for the letterbox.")
    return tuple(int(v) for v in match.groups())


def _color(rgba, channels):
    """oiiotool --box color: one value per channel (0-1), then the box's own opacity."""
    r, g, b, a = (v / 255.0 for v in rgba)
    values = [r, g, b][:channels] if channels <= 3 else [r, g, b, 1.0] + [0.0] * (channels - 4)
    if channels == 1:
        values = [0.2126 * r + 0.7152 * g + 0.0722 * b]
    return ",".join("{:.4g}".format(v) for v in values + [a])


def letterbox_args(spec, width, height, channels, par=Fraction(1)):
    """oiiotool --box arguments for the bars (and the lines on their inner edges); (args, kind,
    bar size)."""
    display = width * float(par) / height
    if abs(spec.ratio - display) * height < 1:
        return [], "none", 0
    rects = []
    if spec.ratio > display:
        bar = int(round((height - width * float(par) / spec.ratio) / 2))
        kind = "letterbox"
        if bar > 0:
            rects += [((0, 0, width - 1, bar - 1), spec.fill),
                      ((0, height - bar, width - 1, height - 1), spec.fill)]
            line = min(spec.line, bar)
            if line > 0:
                rects += [((0, bar - line, width - 1, bar - 1), spec.line_color),
                          ((0, height - bar, width - 1, height - bar + line - 1), spec.line_color)]
    else:
        bar = int(round((width - height * spec.ratio / float(par)) / 2))
        kind = "pillarbox"
        if bar > 0:
            rects += [((0, 0, bar - 1, height - 1), spec.fill),
                      ((width - bar, 0, width - 1, height - 1), spec.fill)]
            line = min(spec.line, bar)
            if line > 0:
                rects += [((bar - line, 0, bar - 1, height - 1), spec.line_color),
                          ((width - bar, 0, width - bar + line - 1, height - 1), spec.line_color)]
    if bar <= 0:
        return [], "none", 0
    args = []
    for (x1, y1, x2, y2), rgba in rects:
        args += ["--box:color={}:fill=1".format(_color(rgba, channels)),
                 "{},{},{},{}".format(x1, y1, x2, y2)]
    return args, kind, bar
