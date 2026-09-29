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
    ocio: str = ""

    # oiiotool / mezzanine
    colorin: str = "ACEScg"
    colorout: str = "out_rec709"
    ocio_display: Optional[str] = None # display/view mode when display or view is set
    ocio_view: Optional[str] = None
    ocio_looks: str = ""
    oiio_threads: int = 0
    oiio_channel: List[str] = field(default_factory=list)
    oiio_channel_use_first: bool = False
    channel_preferences: List[List[str]] = field(default_factory=list)   # from presets.json
    oiio_command: List[str] = field(default_factory=list)
    oiio_per_file: bool = False
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
    output_timecode: bool = True

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

    # slate
    slate: Any = None                  # slate.SlateSpec, None = no slate

    # letterbox
    letterbox: Any = None              # letterbox.LetterboxSpec, None = off
    letterbox_plan: Any = None         # letterbox.LetterboxPlan, set at run time

    # keywords from the sequence name
    version_regex: str = r"_v(\d\d\d)"
    name_tokens: Dict[str, str] = field(default_factory=dict)   # set at run time

    # set at run time
    header_file: str = ""              # the file whose header was read (first frame)
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
