"""Building and running the ffmpeg encode.

The command is always built as an argument list (never a string that is split
later), so paths and burn-in text may contain spaces, quotes, colons and commas.
"""

import logging
import string
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .errors import TranscodeError
from . import metadata as md
from .burnin import BURN_PLACES, DEFAULT_STYLE, BurnStyle, size_expression, size_pixels
from .presets import colour_tags, transfer_for
from .timecode import fps_display, fps_to_str
from .tools import run_tool, split_args

log = logging.getLogger(__name__)

BURN_POSITIONS = {                       # x, y; tc/bc are centred on the text width
    "tl": ("w*0.01", "lh"),
    "tc": ("(w-tw)/2", "lh"),
    "tr": ("w-w*0.01-tw", "lh"),
    "bl": ("w*0.01", "h-(2*lh)"),
    "bc": ("(w-tw)/2", "h-(2*lh)"),
    "br": ("w-w*0.01-tw", "h-(2*lh)"),
}


# --- escaping (see "Notes on filtergraph escaping" in the ffmpeg-filters docs) ---

def escape_text_expansion(text):
    """drawtext text expansion level: backslash and % are special."""
    return text.replace("\\", "\\\\").replace("%", "\\%")


def escape_option_value(value):
    """Level 1: a value inside a filter's key=value list. Leading/trailing spaces are
    escaped too, otherwise ffmpeg trims them ("TC " would lose its space)."""
    text = str(value).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    stripped = text.lstrip(" ")
    text = "\\ " * (len(text) - len(stripped)) + stripped
    stripped = text.rstrip(" ")
    return stripped + "\\ " * (len(text) - len(stripped))


def escape_filtergraph(description):
    """Level 2: a filter description inside a filtergraph."""
    for ch in ("\\", "'", "[", "]", ",", ";"):
        description = description.replace(ch, "\\" + ch)
    return description


def make_filter(name, options):
    """Build one escaped filter from (key, value) pairs."""
    body = ":".join("{}={}".format(k, escape_option_value(v)) for k, v in options)
    return escape_filtergraph("{}={}".format(name, body)) if body else name


# --- burn-in ---

@dataclass
class BurnContext:
    clean_name: str
    frame_start: int
    frame_end: int
    timecode: str
    fps: Fraction
    preset: str = ""
    date: str = ""
    header: dict = field(default_factory=dict)   # source header attributes, for {meta:NAME}
    lead_frames: int = 0     # frames before the first picture (1 with a slate): the running
                             # counters start that many frames earlier
    movie_timecode: str = "" # timecode of the movie's first frame (the slate), if different
    skip_lead: bool = True   # no burn-ins on the lead frames
    tokens: dict = field(default_factory=dict)   # name keywords: {version_string}, {namesplit1} ...

    @property
    def counter_start(self):
        return self.frame_start - self.lead_frames

    @property
    def running_timecode(self):
        return self.movie_timecode or self.timecode

    def static_values(self):
        values = dict(self.tokens)
        values.update({"clean_name": self.clean_name, "frame_start": self.frame_start,
                       "frame_end": self.frame_end, "timecode": self.timecode,
                       "fps": fps_display(self.fps), "preset": self.preset, "date": self.date})
        return values


BURN_KEYWORDS = ("clean_name", "current_frame", "frame_start", "frame_end", "tc",
                 "timecode", "fps", "date", "preset", "meta")


def expand_burn_template(template, ctx):
    """Turn a user template into drawtext options.

    Running:  {current_frame} {tc}
    Static:   {clean_name} {frame_start} {frame_end} {timecode} {fps} {date} {preset}
    "\n" (backslash n, as typed on the command line or in JSON) starts a new line.
    Returns a list of (key, value) option pairs (text, start_number, timecode, rate).
    """
    template = template.replace("\\n", "\n")
    static = ctx.static_values()
    text, options, tc_seen = "", [], False
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        log.warning("Burn-in: unbalanced braces in %r, used as plain text", template)
        parsed = [(template, None, None, None)]
    for literal, field_name, spec, _conversion in parsed:
        if tc_seen:
            if literal or field_name:
                log.warning("Burn-in: text after {tc} is dropped (drawtext always puts the "
                            "timecode at the end): %r", template)
            break
        text += escape_text_expansion(literal)
        if field_name is None:
            continue
        if field_name == "meta":
            text += escape_text_expansion(md.meta_lookup(ctx.header, spec)[0])
        elif field_name == "current_frame":
            text += "%{frame_num}"
            options.append(("start_number", str(ctx.counter_start)))
        elif field_name == "tc":
            options += [("timecode", ctx.running_timecode), ("rate", fps_to_str(ctx.fps))]
            tc_seen = True
        elif field_name in static:
            value = static[field_name]
            try:
                value = format(value, spec) if spec else str(value)
            except ValueError:
                value = str(value)
            text += escape_text_expansion(value)
        else:
            log.warning("Burn-in: unknown keyword {%s} in %r, kept as text", field_name, template)
            text += escape_text_expansion("{" + field_name + (":" + spec if spec else "") + "}")
    return [("text", text)] + options


def _alpha(opacity):
    return "{:.3g}".format(opacity)


def build_drawtext(template, place, ctx, font, style, picture_height=1080):
    """One drawtext filter. style: a fully resolved burnin.BurnStyle."""
    options = []
    if font:
        options.append(("fontfile", Path(font).as_posix()))
    alpha = _alpha(style.opacity)
    options += [("fontsize", size_expression(style.size)),
                ("fontcolor", "{}@{}".format(style.color, alpha))]
    if style.box:
        # black box, same opacity as the text; padding about a quarter of the text height
        border = max(2, int(round(size_pixels(style.size, picture_height) / 4)))
        options += [("box", "1"), ("boxcolor", "black@" + alpha), ("boxborderw", str(border))]
    else:
        options += [("shadowcolor", "black@" + alpha), ("shadowx", "2"), ("shadowy", "1")]
    text_options = expand_burn_template(template, ctx)
    options += text_options
    lines = dict(text_options)["text"].count("\n") + 1
    x, y = BURN_POSITIONS[place]
    if lines > 1:
        if place.startswith("b"):
            y = "h-({}*lh)".format(lines + 1)      # grow upwards, same bottom margin as one line
        if place[1] in "cr":
            options.append(("text_align", "C" if place[1] == "c" else "R"))   # ffmpeg 6.1+
    options += [("x", x), ("y", y)]
    if ctx.lead_frames and ctx.skip_lead:
        # the counters still run on the skipped frames, so the first picture shows the right values
        options.append(("enable", "gte(n,{})".format(ctx.lead_frames)))
    return make_filter("drawtext", options)


def build_video_filter(cfg, ctx, scale_width):
    """scale (+ bt709 matrix) -> pad to even size -> user filter -> burn-ins.

    scale_width: target width, or 0 to keep the size (EXRs are already resized by oiiotool).
    """
    scale = [("in_range", "full"), ("in_color_matrix", "bt709"),
             ("out_range", "tv"), ("out_color_matrix", "bt709")]
    if scale_width > 0:
        scale = [("w", str(scale_width)), ("h", "-2")] + scale
    from .aspect import reformat_filters, sar_filter
    filters = reformat_filters(cfg.par_in, cfg.par_out_value)
    filters += [make_filter("scale", scale),
                make_filter("pad", [("width", "ceil(iw/2)*2"), ("height", "ceil(ih/2)*2")]),
                sar_filter(cfg.par_out_value)]         # also tags 1:1, so nothing is inherited
    if cfg.preset.colour_tags and not cfg.ff_encode.strip():
        # Tag the frames too, not only the encoder: newer ffmpeg (seen with 7.1) takes the
        # colour tags from the frames, i.e. from the intermediate PNGs' gamma chunks
        # ("unknown/linear"), and ignores -color_trc / -color_primaries.
        filters.append(make_filter("setparams", [
            ("range", "tv"), ("color_primaries", "bt709"),
            ("color_trc", transfer_for(cfg.output_colour)), ("colorspace", "bt709")]))
    if cfg.letterbox_plan is not None:
        from .letterbox import filters as letterbox_filters
        filters += letterbox_filters(cfg.letterbox_plan, ctx.lead_frames)   # not on the slate
    if cfg.ff_filter.strip():
        filters.append(cfg.ff_filter.strip())   # user's raw filtergraph, used as given

    font = cfg.burn_font
    if font and not Path(font).is_file() and any(cfg.burn.values()):
        log.warning('Burn-in font "%s" not found, using ffmpeg default font.', font)
        font = ""
    # output height is only known to ffmpeg; estimate it for the box padding
    height = (cfg.width * 9 // 16) if cfg.width else 1080
    for place in BURN_PLACES:
        template = cfg.burn.get(place)
        if template:
            style = cfg.burn_styles.get(place) or DEFAULT_STYLE.over(BurnStyle(size=str(cfg.burn_size)))
            filters.append(build_drawtext(template, place, ctx, font, style, height))
    return ",".join(filters)


# --- input list and command ---

def _concat_quote(path):
    return "'" + Path(path).resolve().as_posix().replace("'", "'\\''") + "'"


def write_concat_list(frame_files, fps, list_path):
    """One entry per output frame. Holes are filled by repeating a file."""
    duration = float(1 / fps)
    lines = ["ffconcat version 1.0"]
    for path in frame_files:
        lines.append("file " + _concat_quote(path))
        lines.append("duration {:.10f}".format(duration))
    Path(list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def encode_args(cfg, video_filter):
    """Codec arguments: -ff_encode if given (used as is), else the preset + colour tags."""
    user = bool(cfg.ff_encode.strip())
    tokens = split_args(cfg.ff_encode) if user else list(cfg.preset.encode)
    args = []
    placed = False
    for token in tokens:
        if token == "{videofilter}":
            args += ["-vf", video_filter]
            placed = True
        elif "{videofilter}" in token:
            raise TranscodeError("-ff_encode: {videofilter} must be a separate word.")
        else:
            args.append(token)
    if not placed:
        log.warning("-ff_encode has no {videofilter}; scaling and burn-ins are not applied.")
    if not user and cfg.preset.colour_tags:
        args += colour_tags(cfg.output_colour)
    return args


def build_command(cfg, list_file, output_file, video_filter, timecode, metadata=None, reel_name=""):
    cmd = [cfg.ffmpeg, "-hide_banner", "-y",
           "-f", "concat", "-safe", "0", "-r", fps_to_str(cfg.fps), "-i", Path(list_file).as_posix()]
    metadata = metadata or {}
    encode = encode_args(cfg, video_filter)
    preset_metadata = any(a.startswith("-metadata") for a in encode)   # e.g. ProRes vendor_id
    encode = md.merge_movflags(encode, cfg.ff_out_ext,
                               bool(metadata) or bool(reel_name) or preset_metadata)
    cmd += encode
    cmd += ["-map_metadata", "-1"]    # nothing inherited from the intermediate frames
    cmd += md.ffmpeg_args(metadata, timecode, cfg.ff_out_ext, cfg.output_timecode, reel_name)
    cmd.append(Path(output_file).as_posix())
    return cmd


def encode(cfg, cmd):
    log.info("Encoding %s", cmd[-1])
    run_tool(cmd, "ffmpeg", cfg.verbose)
