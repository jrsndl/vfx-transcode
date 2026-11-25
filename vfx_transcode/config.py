"""Typed run configuration, built once from the command line."""

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .burnin import BURN_PLACES

BURN_CORNERS = BURN_PLACES      # older name


@dataclass
class Config:
    input: str
    output: str = ""
    fps: Fraction = Fraction(24)       # resolved rate; set from the sources below at run time
    fps_arg: Optional[Fraction] = None # -r, if given
    output_fps: Optional[Fraction] = None   # -output_fps: play the output at this rate
    source_fps: Optional[Fraction] = None   # the input's rate, once resolved
    fps_source: str = "arg"            # arg | exr | arg_only
    par_arg: Optional[Fraction] = None # -par, if given
    par_source: str = "arg"            # arg | exr | arg_only
    par_out: str = "same"              # same | square | a PAR value
    par_in: Fraction = Fraction(1)     # resolved at run time
    par_out_value: Fraction = Fraction(1)   # resolved at run time
    verbose: int = 0

    # tools (already resolved to absolute paths by the CLI)
    oiiotool: str = "oiiotool"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    tool_sources: dict = field(default_factory=dict)   # tool -> where its path came from
    ocio: str = ""

    # oiiotool / mezzanine
    colorin: str = "ACEScg"
    colorout: str = "out_rec709"
    ocio_display: Optional[str] = None # display/view mode when display or view is set
    ocio_view: Optional[str] = None
    ocio_looks: str = ""
    clf: str = ""                      # -clf path template; set = CLF instead of OCIO colourspaces
    clf_path: str = ""                 # the expanded, resolved path, set at run time
    oiio_threads: int = 0
    oiio_channel: List[str] = field(default_factory=list)
    oiio_channel_use_first: bool = False
    oiio_subimage: str = ""           # -oiio_subimage: a part's number or name; "" = all
    channel_preferences: List[List[str]] = field(default_factory=list)   # from presets.json
    oiio_command: List[str] = field(default_factory=list)
    oiio_transform: Any = None          # transforms.TransformPreset, None = -width resize
    oiio_transform_args: Optional[List[str]] = None   # the matching sub's args, set at run time
    oiio_transform_explicit: Optional[List[str]] = None   # -oiio_transform_args
    transform_choice: str = ""          # "preset / sub" that was used, for logs and -preview
    preview: str = ""                   # -preview FILE.png: one frame as the movie shows it
    preview_frame: str = "middle"       # which frame for -preview
    oiio_per_file: bool = False
    oiio_verify: bool = True           # convert again frames that came out far smaller (black)
    mezzanine: str = "png"
    mezzanine_16bit: bool = False
    width: int = 1920
    temp_location: str = "user"        # user | source
    temp: str = ".temp_png_sequence"   # folder name for temp_location source
    temp_keep: bool = False
    write_exr_info: bool = False
    missing_frames: str = "hold"   # hold | error
    deep_zrange: str = ""          # "near,far" for deep depth pictures; empty = measure

    # timecode / burn-in
    timecode: str = "01:00:00:00"
    timecode_source: str = "counter"   # counter | arg | exr
    timecode_source_given: bool = True # False: "counter" only as the default (movies use their own)
    output_timecode: bool = True
    output_reelid: bool = True         # write the reel name into the movie
    seq_metadata: bool = True          # tags into an oiiotool-written image sequence
    timecode_found: str = ""           # set at run time: where the timecode came from
    reel_found: str = ""               # set at run time: where the reel name came from

    # output metadata
    metadata_copy: str = ""            # "", "all" or key patterns, see metadata.py
    metadata: Dict[str, str] = field(default_factory=dict)   # -metadata KEY=VALUE
    metadata_preset: str = ""          # name, for logging
    preset_tags: Dict[str, str] = field(default_factory=dict)  # tags of the metadata preset
    reelid: str = ""                   # -reelid, may contain {keywords}
    reelid_source: str = "auto"        # auto | arg | exr | off
    reelid_attr: str = "owner"         # EXR attribute holding the reel name
    reelid_pattern: str = ""           # regex to extract the reel from the header value
    burn_preset: str = ""              # name, for logging only; its values are in burn*
    burn_size: str = "1.33%"           # size of the first place, kept for older callers
    burn_font: str = ""
    burn: Dict[str, str] = field(default_factory=dict)   # place -> text template
    burn_styles: Dict[str, Any] = field(default_factory=dict)   # place -> resolved BurnStyle
    burn_margins: Dict[str, str] = field(default_factory=dict)  # side -> "1.5%" / "20"; {} = defaults

    # slate
    slate: Any = None                  # slate.SlateSpec, None = no slate

    # alpha
    alpha: str = "drop"                # drop | keep (-alpha auto is resolved to one of them)
    alpha_auto: bool = False           # -alpha auto: no alpha in the input is not a warning
    alpha_strip: str = ""              # oiiotool --ch for non-EXR input with alpha and drop
    alpha_premult: bool = False        # with keep: write premultiplied colour (else straight)
    alpha_view_only: bool = False      # -preview_alpha: alpha kept for the preview picture only
    alpha_active: bool = False         # set at run time: the movie really gets an alpha channel

    # thumbnail / what to make
    movie: bool = True                 # False: -ot / -of without -o, stills only
    thumbnail: Any = None              # thumbnails.ThumbnailPreset, None = no thumbnail
    thumbnail_output: Optional[str] = None   # -ot template; "" = default place and name
    filmstrip: Any = None              # filmstrip.FilmstripPreset, None = no filmstrip
    filmstrip_output: Optional[str] = None   # -of template; "" = default place and name

    # letterbox
    letterbox: Any = None              # letterbox.LetterboxSpec, None = off
    letterbox_plan: Any = None         # letterbox.LetterboxPlan, set at run time

    # keywords from the sequence name
    version_regex: str = r"_v(\d\d\d)"
    name_rules: Dict[str, Any] = field(default_factory=dict)    # names.NameRule by keyword
    smart_tokens: Any = None                                    # smart_tokens.SmartTokenPreset
    base_folder: str = ""             # -base_folder: {base_folder}; "" = the input's folder
    custom_tokens: Dict[str, str] = field(default_factory=dict)   # {custom_destination_path} ...
    name_tokens: Dict[str, str] = field(default_factory=dict)   # set at run time

    # set at run time
    header_file: str = ""              # the file whose header was read (first frame)
    header_label: str = "EXR header"   # what the header is called in the log: or "movie"
    movie_first_frame: int = 1001      # movie input: the number of its first frame
    trim_in: Optional[int] = None      # -in: first frame to use (inclusive)
    trim_out: Optional[int] = None     # -out: last frame to use (inclusive)
    ff_transform: str = ""             # -ff_transform: ffmpeg filters replacing the -width scale
    seq_args: List[str] = field(default_factory=list)   # oiiotool output args for -o sequences
    seq_preset: str = ""               # where seq_args came from: a preset name, "-seq_args", ""
    colour_given: bool = False         # -ci / -co / -display / -view / -clf given
    seq_out_ext: str = ""              # -o names an image sequence: its extension (".exr")
    image_output: bool = False         # ffmpeg writes an image sequence (movie in -> jpg ...)
    oiio_letterbox: Any = None         # LetterboxSpec drawn by oiiotool, or None
    oiio_letterbox_args: Optional[List[str]] = None   # its --box steps, once measured
    oiio_letterbox_info: Any = None    # (kind, bar, width, height) for the log / preview
    stills_folder: Any = None          # thumbnail / filmstrip default folder (sequence output)
    skip_colour: bool = False          # no OCIO step (EXR -> EXR without -ci / -co)
    header_frame: Optional[int] = None # the frame the header belongs to (a movie: its first)
    header_attribs: Dict[str, Any] = field(default_factory=dict)   # its attributes, {meta:NAME}

    # ffmpeg
    skip_ffmpeg: bool = False
    ff_filter: str = ""
    preset: Any = None                 # presets.Preset
    ff_encode: str = ""        # user override of the preset's codec arguments
    ff_out_ext: str = "mp4"

    @property
    def display_mode(self):
        return self.ocio_display is not None or self.ocio_view is not None

    @property
    def output_colour(self):
        """Name that decides the movie's colour tags: the display, else -colorout."""
        return (self.ocio_display or "") if self.display_mode else self.colorout

    @property
    def mezzanine_ext(self):
        ext = self.mezzanine.strip().lower().lstrip(".")
        return "." + (ext if len(ext) >= 2 else "png")

    @property
    def workers(self):
        import os
        return self.oiio_threads if self.oiio_threads > 0 else (os.cpu_count() or 4)
