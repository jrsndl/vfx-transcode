"""Movie files (QuickTime, MP4, MXF ...) as input.

ffprobe reads the movie's size, frame rate, frame count, pixel aspect and timecode; ffmpeg
unpacks its frames into the intermediate folder as a numbered image sequence (the -mezzanine
format), and from there the movie takes the same path as PNG / JPG input: slate, burn-ins,
letterbox, thumbnail, filmstrip and preview all work unchanged. A preview unpacks only its one
frame. The frames are display-referred already: no OCIO conversion. Audio is not carried over.

The movie's frames are numbered from -movie_first_frame (default 1001).
"""

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .errors import TranscodeError, ToolError
from .sequence import Sequence
from .timecode import fps_display, fps_from_value, validate_timecode
from .tools import run_tool

log = logging.getLogger(__name__)

MOVIE_EXTS = (".mov", ".mp4", ".m4v", ".mxf", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".mpg",
              ".mpeg", ".ts", ".wmv", ".flv", ".y4m", ".gif")
DEFAULT_FIRST_FRAME = 1001
# formats the frames are unpacked to; anything else (exr, dpx ...) falls back to png: the frames
# are display-referred, an EXR intermediate would send them through OCIO
UNPACK_EXTS = (".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp")
HEADER_PREFIX = "movie:"


def is_movie(path):
    return Path(str(path)).suffix.lower() in MOVIE_EXTS


@dataclass
class MovieInfo:
    path: Path
    width: int
    height: int
    fps: Optional[Fraction]          # None: the movie gives none
    frames: int
    codec: str = ""
    pix_fmt: str = ""
    par: Fraction = Fraction(1)
    timecode: Optional[str] = None
    color_space: str = ""
    color_range: str = ""
    duration: float = 0.0
    tags: "OrderedDict[str, str]" = field(default_factory=OrderedDict)

    @property
    def alpha(self):
        fmt = self.pix_fmt.lower()
        return fmt.startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya"))

    def header(self):
        """The movie as an image header (a one-subimage list like oiiotool's), so frame rate,
        timecode, pixel aspect and {meta:...} keywords take the same path as an EXR's."""
        attribs = OrderedDict()
        if self.fps is not None:
            attribs["FramesPerSecond"] = "{}/{}".format(self.fps.numerator, self.fps.denominator)
        if self.timecode:
            attribs["timecode"] = self.timecode
        if self.par != 1:
            attribs["PixelAspectRatio"] = float(self.par)
        attribs[HEADER_PREFIX + "file"] = str(self.path)
        attribs[HEADER_PREFIX + "codec"] = self.codec
        attribs[HEADER_PREFIX + "pix_fmt"] = self.pix_fmt
        attribs[HEADER_PREFIX + "frames"] = self.frames
        attribs[HEADER_PREFIX + "width"] = self.width
        attribs[HEADER_PREFIX + "height"] = self.height
        if self.color_space:
            attribs[HEADER_PREFIX + "color_space"] = self.color_space
        for key, value in self.tags.items():
            if key.lower() != "timecode":
                attribs[HEADER_PREFIX + key] = value
        # width / height like oiiotool's header: {input_width} for transform conditions
        return [{"attribs": attribs, "width": self.width, "height": self.height}]

    def describe(self):
        return "{}x{} {} {}, {} fps, {} frames{}{}".format(
            self.width, self.height, self.codec, self.pix_fmt,
            fps_display(self.fps) if self.fps else "?", self.frames,
            ", pixel aspect {:g}".format(float(self.par)) if self.par != 1 else "",
            ", timecode " + self.timecode if self.timecode else "")


def _fraction(text):
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return value if value > 0 else None


def parse_probe(data, path):
    """MovieInfo from `ffprobe -show_streams -show_format -of json` output."""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise TranscodeError('"{}" has no video stream.'.format(path))
    fps = None
    for key in ("r_frame_rate", "avg_frame_rate"):
        fps = fps_from_value(video.get(key)) if _fraction(video.get(key)) else None
        if fps is not None:
            break
    fmt = data.get("format") or {}
    duration = 0.0
    for value in (video.get("duration"), fmt.get("duration")):
        try:
            duration = float(value)
            break
        except (TypeError, ValueError):
            continue
    frames = 0
    try:
        frames = int(video.get("nb_frames") or 0)
    except ValueError:
        frames = 0
    if frames <= 0 and duration and fps:
        frames = int(round(duration * float(fps)))
    if frames <= 0:
        raise TranscodeError('"{}": the number of frames is unknown (no frame count and no '
                             'duration).'.format(path))
    par = _fraction(str(video.get("sample_aspect_ratio", "1:1")).replace(":", "/")) or Fraction(1)
    timecode = None
    tag_sources = [video.get("tags") or {}] + [s.get("tags") or {} for s in streams] + [fmt.get("tags") or {}]
    for tags in tag_sources:
        value = tags.get("timecode")
        if value:
            try:
                timecode = validate_timecode(value.replace(";", ":"))
                break
            except TranscodeError:
                continue
    tags = OrderedDict()
    for key, value in sorted((fmt.get("tags") or {}).items()):
        tags[key] = value
    for key, value in sorted((video.get("tags") or {}).items()):
        tags.setdefault(key, value)
    return MovieInfo(path=Path(path), width=int(video.get("width") or 0),
                     height=int(video.get("height") or 0), fps=fps, frames=frames,
                     codec=video.get("codec_name", ""), pix_fmt=video.get("pix_fmt", ""),
                     par=par, timecode=timecode, color_space=video.get("color_space", ""),
                     color_range=video.get("color_range", ""), duration=duration, tags=tags)


def probe_movie(ffprobe, path, verbose=0):
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        result = run_tool(cmd, "ffprobe", verbose)
    except ToolError as exc:
        raise TranscodeError('ffprobe could not read "{}": {}'.format(path, exc))
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        raise TranscodeError('ffprobe gave no readable answer for "{}".'.format(path))
    return parse_probe(data, path)


def unpack_ext(mezzanine_ext):
    ext = (mezzanine_ext or ".png").lower()
    if not ext.startswith("."):
        ext = "." + ext
    return ext if ext in UNPACK_EXTS else ".png"


def movie_sequence(path, info, first_frame=DEFAULT_FIRST_FRAME, ext=".png"):
    """The movie's frames as a numbered sequence named after the movie. Its folder is the
    movie's (outputs go next to it); the frames themselves go to frames_folder."""
    path = Path(path)
    last = first_frame + info.frames - 1
    zfill = max(4, len(str(last)))
    return Sequence(path.parent, path.stem + ".", unpack_ext(ext), zfill,
                    tuple(range(first_frame, last + 1)))


def _pixel_format(ext, sixteen_bit, alpha):
    if ext in (".jpg", ".jpeg"):
        return "yuvj444p"
    if ext == ".bmp":
        return "bgra" if alpha else "bgr24"
    if sixteen_bit:
        return "rgba64be" if alpha else "rgb48be"
    return "rgba" if alpha else "rgb24"


def unpack_command(ffmpeg, info, seq, frame=None, sixteen_bit=False, alpha=False, base_frame=None):
    """ffmpeg command writing the movie's frames (seq's frames, or the one frame) as seq's
    files. base_frame: the number of the movie's first frame (default seq.start; a trimmed seq
    starts later). YUV is read as the movie is tagged; untagged HD and larger as Rec.709
    (SD: Rec.601)."""
    from .ffmpeg import make_filter
    cmd = [ffmpeg, "-hide_banner", "-y"]
    first = seq.start
    base = first if base_frame is None else base_frame
    seek_frame = frame if frame is not None else first
    if seek_frame != base:
        index = seek_frame - base
        fps = info.fps or Fraction(24)
        # a quarter frame early: the first frame at or after it is the one asked for
        seek = max(0.0, (index - 0.25) / float(fps))
        if seek > 0:
            cmd += ["-ss", "{:.6f}".format(seek)]
    cmd += ["-i", str(info.path), "-map", "0:v:0", "-an", "-sn", "-dn"]
    matrix = info.color_space if info.color_space and info.color_space not in ("unknown", "reserved") \
        else ("bt709" if info.height >= 720 else "bt601")
    matrix = {"bt470bg": "bt601", "smpte170m": "bt601", "bt2020nc": "bt2020", "bt2020c": "bt2020"}.get(
        matrix, matrix)
    pix_fmt = _pixel_format(seq.ext.lower(), sixteen_bit, alpha)
    steps = []
    if matrix in ("bt709", "bt601", "fcc", "smpte240m", "bt2020"):
        steps.append(make_filter("scale", [("in_color_matrix", matrix), ("out_range", "pc")]))
    steps.append(make_filter("format", [("pix_fmts", pix_fmt)]))
    cmd += ["-vf", ",".join(steps), "-fps_mode", "passthrough"]
    if frame is not None:
        cmd += ["-frames:v", "1", "-update", "1", str(seq.path(frame))]
    else:
        pattern = seq.path(first).with_name("{}%0{}d{}".format(seq.base, seq.zfill, seq.ext))
        cmd += ["-frames:v", str(seq.end - first + 1), "-start_number", str(first), str(pattern)]
    if seq.ext.lower() in (".jpg", ".jpeg"):
        cmd[-1:-1] = ["-q:v", "2"]
    elif seq.ext.lower() == ".png":
        cmd[-1:-1] = ["-compression_level", "1"]      # unpacking speed over file size
    return cmd


def unpack(ffmpeg, info, seq, frame=None, sixteen_bit=False, alpha=False, verbose=0,
           base_frame=None):
    """Unpack the frames; returns {frame: path} of the files written. Fewer frames than the
    probe said (a short last GOP, a wrong frame count) shorten the sequence."""
    run_tool(unpack_command(ffmpeg, info, seq, frame, sixteen_bit, alpha, base_frame),
             "ffmpeg (unpack movie)", verbose)
    wanted = [frame] if frame is not None else list(seq.frames)
    found = {f: seq.path(f) for f in wanted if seq.path(f).is_file()}
    if not found:
        raise TranscodeError('ffmpeg unpacked no frames from "{}".'.format(info.path))
    if frame is None and len(found) != len(wanted):
        log.warning("Movie: %d frames unpacked, the probe said %d; using %d.", len(found),
                    len(wanted), len(found))
    return found


def unpack_some(ffmpeg, info, seq, frames, sixteen_bit=False, alpha=False, verbose=0, workers=1,
                base_frame=None):
    """Unpack only these frames, one seek each, in parallel: quicker than decoding a long movie
    for a thumbnail or a filmstrip. Returns {frame: path}."""
    import concurrent.futures
    frames = sorted(set(frames))
    found = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(unpack, ffmpeg, info, seq, f, sixteen_bit, alpha, verbose,
                               base_frame): f
                   for f in frames}
        for future in concurrent.futures.as_completed(futures):
            try:
                found.update(future.result())
            except (TranscodeError, ToolError) as exc:
                log.warning("Movie: frame %d could not be unpacked: %s", futures[future], exc)
    if not found:
        raise TranscodeError('ffmpeg unpacked no frames from "{}".'.format(info.path))
    return found


def with_frames(seq, frames_folder, frames=None):
    """seq with its files in frames_folder (and, if given, only these frames)."""
    seq = replace(seq, frames_folder=Path(frames_folder))
    if frames is not None:
        seq = replace(seq, frames=tuple(sorted(frames)))
    return seq
