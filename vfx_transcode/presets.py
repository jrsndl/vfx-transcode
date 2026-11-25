"""Encode, burn-in and metadata presets and channel preferences, loaded from a user-editable
JSON file (presets.json)."""

import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .burnin import (BURN_PLACES, MARGIN_FIELDS, PLACE_NAMES, STYLE_FIELDS, BurnStyle,
                     margins_from_values, parse_size, style_from_values)
from .errors import TranscodeError
from .names import NameRule, rules_from_json
from .smart_tokens import NO_SMART_TOKENS, SmartTokenPreset, parse_smart_token_presets
from .slate import (NO_SLATE, SLATE_PRESET_FIELDS, SlateSpec, parse_fields, parse_thumbnail,
                    style_values)
from .tools import split_args
from .transforms import NO_TRANSFORM, TransformPreset, parse_transform_presets
from .thumbnails import ThumbnailPreset, presets_from_json
from .filmstrip import FilmstripPreset, presets_from_json as filmstrips_from_json
from . import destinations
from .tools import TOOLS

DEFAULT_PRESETS_FILE = "./presets.json"

# The original -ff_encode default, kept to test that the h264 preset still matches it.
LEGACY_H264 = ("-pix_fmt yuv420p {videofilter} -c:v libx264 -g 1 -preset slower -crf 18 "
               "-color_range tv -color_primaries bt709 -color_trc bt709 -colorspace bt709 "
               "-movflags faststart")


@dataclass(frozen=True)
class Preset:
    name: str
    ext: str
    encode: Tuple[str, ...]      # ffmpeg arguments, one of them "{videofilter}"
    ten_bit: bool = False
    colour_tags: bool = True
    notes: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)   # codec-level tags, e.g. vendor_id
    alpha_pix_fmt: str = ""      # pixel format used instead of -pix_fmt when alpha is kept;
                                 # "" = the codec cannot store alpha


# Fields of a metadata preset; the command-line options with the same meaning override them.
METADATA_PRESET_FIELDS = ("notes", "tags", "copy", "reelid_source", "reelid", "reelid_attr",
                          "reelid_pattern", "timecode_source")
METADATA_CHOICES = {"reelid_source": ("auto", "arg", "metadata", "exr", "off"),
                    "timecode_source": ("counter", "arg", "metadata", "exr")}


@dataclass(frozen=True)
class MetadataPreset:
    name: str
    tags: Dict[str, str] = field(default_factory=dict)   # KEY -> VALUE, keywords allowed
    copy: Optional[str] = None             # -metadata_copy spec
    reelid_source: Optional[str] = None
    reelid: Optional[str] = None
    reelid_attr: Optional[str] = None
    reelid_pattern: Optional[str] = None
    timecode_source: Optional[str] = None
    notes: str = ""


BURN_CORNERS = BURN_PLACES      # older name
NO_BURNIN = "none"


@dataclass(frozen=True)
class BurnPreset:
    name: str
    corners: Dict[str, str] = field(default_factory=dict)   # place -> text template
    style: BurnStyle = field(default_factory=BurnStyle)     # preset-wide color/opacity/size/box
    place_styles: Dict[str, BurnStyle] = field(default_factory=dict)   # place -> overrides
    margins: Dict[str, str] = field(default_factory=dict)   # side -> value, only those set
    font: Optional[str] = None
    notes: str = ""

    @property
    def size(self):
        return self.style.size


@dataclass
class PresetFile:
    path: str
    default: str
    presets: "OrderedDict[str, Preset]"
    burnins: "OrderedDict[str, BurnPreset]" = field(default_factory=OrderedDict)
    default_burnin: str = ""
    channel_preferences: List[List[str]] = field(default_factory=list)
    metadata_presets: "OrderedDict[str, MetadataPreset]" = field(default_factory=OrderedDict)
    default_metadata: str = ""
    slates: "OrderedDict[str, SlateSpec]" = field(default_factory=OrderedDict)
    default_slate: str = ""
    transforms: "OrderedDict[str, TransformPreset]" = field(default_factory=OrderedDict)
    default_transform: str = ""
    thumbnails: "OrderedDict[str, ThumbnailPreset]" = field(default_factory=OrderedDict)
    default_thumbnail: str = ""
    name_rules: "OrderedDict[str, NameRule]" = field(default_factory=OrderedDict)
    smart_tokens: "OrderedDict[str, SmartTokenPreset]" = field(default_factory=OrderedDict)
    default_smart_tokens: str = ""
    sequences: "OrderedDict[str, Any]" = field(default_factory=OrderedDict)   # outseq.SequencePreset
    default_sequences: "OrderedDict[str, str]" = field(default_factory=OrderedDict)  # ext -> name
    filmstrips: "OrderedDict[str, FilmstripPreset]" = field(default_factory=OrderedDict)
    default_filmstrip: str = ""
    tools: "OrderedDict[str, str]" = field(default_factory=OrderedDict)   # name -> path
    colours: "OrderedDict[str, ColourPreset]" = field(default_factory=OrderedDict)
    ff_transforms: "OrderedDict[str, FFTransformPreset]" = field(default_factory=OrderedDict)
    # destination_paths / destination_names: named -o folders and file names (destinations.py)
    dest_paths: "OrderedDict[str, Any]" = field(default_factory=lambda: destinations.builtin("path"))
    dest_names: "OrderedDict[str, Any]" = field(default_factory=lambda: destinations.builtin("name"))
    # named orders for the automatic channel search ("channel_preference_presets"):
    # name -> ChannelOrderPreset; -channel_preferences NAME picks one
    channel_orders: "OrderedDict[str, Any]" = field(default_factory=OrderedDict)

    def get_filmstrip(self, name):
        """Filmstrip preset by name; the built-in defaults (24 tiles of 480x270 in one row)
        for "" when the file has none."""
        if not name:
            return FilmstripPreset(name="(built-in)")
        try:
            return self.filmstrips[name]
        except KeyError:
            raise TranscodeError('Unknown filmstrip preset "{}". Available: {} (from {})'.format(
                name, ", ".join(self.filmstrips) or "none", self.path))

    def get_smart_tokens(self, name):
        """Smart token preset by name; None for "" or "none"."""
        if not name or name == NO_SMART_TOKENS:
            return None
        try:
            return self.smart_tokens[name]
        except KeyError:
            raise TranscodeError('Unknown smart tokens preset "{}". Available: {} (from {})'.format(
                name, ", ".join(list(self.smart_tokens) + [NO_SMART_TOKENS]), self.path))

    def get_thumbnail(self, name):
        """Thumbnail preset by name; the built-in defaults for "" when the file has none."""
        if not name:
            return ThumbnailPreset(name="(built-in)")
        try:
            return self.thumbnails[name]
        except KeyError:
            raise TranscodeError('Unknown thumbnail preset "{}". Available: {} (from {})'.format(
                name, ", ".join(self.thumbnails) or "none", self.path))

    def get_transform(self, name):
        """oiio transform preset by name; None for "" or "none"."""
        if not name or name == NO_TRANSFORM:
            return None
        try:
            return self.transforms[name]
        except KeyError:
            raise TranscodeError('Unknown oiio transform "{}". Available: {} (from {})'.format(
                name, ", ".join(list(self.transforms) + [NO_TRANSFORM]), self.path))

    def get_slate(self, name):
        """Slate preset by name; None for "" or "none"."""
        if not name or name == NO_SLATE:
            return None
        try:
            return self.slates[name]
        except KeyError:
            raise TranscodeError('Unknown slate preset "{}". Available: {} (from {})'.format(
                name, ", ".join(list(self.slates) + [NO_SLATE]), self.path))

    def get_metadata_preset(self, name):
        """Metadata preset by name; None for "" or "none"."""
        if not name or name == NO_BURNIN:
            return None
        try:
            return self.metadata_presets[name]
        except KeyError:
            raise TranscodeError('Unknown metadata preset "{}". Available: {} (from {})'.format(
                name, ", ".join(list(self.metadata_presets) + [NO_BURNIN]), self.path))

    def get(self, name):
        try:
            return self.presets[name]
        except KeyError:
            raise TranscodeError('Unknown preset "{}". Available: {} (from {})'.format(
                name, ", ".join(self.presets), self.path))

    def get_burnin(self, name):
        """Burn-in preset by name; None for "" or "none"."""
        if not name or name == NO_BURNIN:
            return None
        try:
            return self.burnins[name]
        except KeyError:
            raise TranscodeError('Unknown burn-in preset "{}". Available: {} (from {})'.format(
                name, ", ".join(list(self.burnins) + [NO_BURNIN]), self.path))

    def describe(self):
        width = max(len(n) for n in list(self.presets) + list(self.burnins)
                    + list(self.metadata_presets) + list(self.slates) + list(self.transforms)
                    + list(self.thumbnails) + list(self.filmstrips) + list(self.smart_tokens)
                    + list(self.sequences) + list(self.colours) + list(self.ff_transforms))
        lines = []
        if self.tools:
            lines += ["Tools (presets file \"tools\"; -ffmpeg / -ffprobe / -oiio override them):"]
            lines += ["  {:<{}}  {}".format(n, width, p) for n, p in self.tools.items()]
            lines.append("")
        lines += ["Encode presets (-preset), default {}:".format(self.default)]
        lines += ["  {:<{}}  .{:<4} {}{}".format(p.name, width, p.ext, p.notes,
                                                 " [alpha]" if p.alpha_pix_fmt else "")
                  for p in self.presets.values()]
        if self.burnins:
            lines += ["", "Burn-in presets (-burn_preset), default {}:".format(
                self.default_burnin or NO_BURNIN)]
            for b in self.burnins.values():
                lines.append("  {:<{}}  {}".format(b.name, width, b.notes))
                if not b.style.is_empty():
                    lines.append("  {:<{}}    style: {}".format("", width, b.style.describe()))
                if b.margins:
                    lines.append("  {:<{}}    margins: {}".format("", width, ", ".join(
                        "{}={}".format(k, v) for k, v in b.margins.items())))
                for place in BURN_PLACES:
                    if b.corners.get(place):
                        extra = b.place_styles.get(place)
                        lines.append("  {:<{}}    {}: {}{}".format(
                            "", width, place, b.corners[place],
                            "   [{}]".format(extra.describe()) if extra and not extra.is_empty() else ""))
        if self.metadata_presets:
            lines += ["", "Metadata presets (-metadata_preset), default {}:".format(
                self.default_metadata or NO_BURNIN)]
            for m in self.metadata_presets.values():
                lines.append("  {:<{}}  {}".format(m.name, width, m.notes))
                settings = ["{}={}".format(k, v) for k, v in m.tags.items()]
                settings += ["{}: {}".format(f, getattr(m, f)) for f in METADATA_PRESET_FIELDS[2:]
                             if getattr(m, f) not in (None, "")]
                for item in settings:
                    lines.append("  {:<{}}    {}".format("", width, item))
        if self.slates:
            lines += ["", "Slate presets (-slate), default {}:".format(self.default_slate or NO_SLATE)]
            for sl in self.slates.values():
                lines.append("  {:<{}}  {}".format(sl.name, width, sl.notes))
                rows = ["title: " + sl.title] + (["subtitle: " + sl.subtitle] if sl.subtitle else [])
                rows += ["{}: {}".format(k, v) for k, v in sl.fields.items()]
                rows.append("thumbnail: " + sl.thumbnail)
                for item in rows:
                    lines.append("  {:<{}}    {}".format("", width, item))
        if self.thumbnails:
            lines += ["", "Thumbnail presets (-thumbnail_preset, with -ot), default {}:".format(
                self.default_thumbnail or "(built-in: middle frame, movie size, jpg 90)")]
            for t in self.thumbnails.values():
                lines.append("  {:<{}}  {}".format(t.name, width, t.notes))
                lines.append("  {:<{}}    {}".format("", width, t.describe()))
        if self.sequences:
            lines += ["", "Sequence presets (-seq_preset, for an image sequence -o), defaults: {}:".format(
                ", ".join("{} {}".format(e, n) for e, n in self.default_sequences.items()) or "none")]
            for q in self.sequences.values():
                lines.append("  {:<{}}  {}".format(q.name, width, q.notes))
                lines.append("  {:<{}}    {}".format("", width, q.describe()))
        if self.filmstrips:
            lines += ["", "Filmstrip presets (-filmstrip_preset, with -of), default {}:".format(
                self.default_filmstrip or "(built-in: 24 tiles of 480x270, one row, jpg 90)")]
            for f in self.filmstrips.values():
                lines.append("  {:<{}}  {}".format(f.name, width, f.notes))
                lines.append("  {:<{}}    {}".format("", width, f.describe()))
        if self.colours:
            lines += ["", "Colour presets (-colour_preset; -ci, -co ... given on the command line win):"]
            for c in self.colours.values():
                lines.append("  {:<{}}  {}".format(c.name, width, c.notes))
                lines.append("  {:<{}}    {}".format("", width, c.describe()))
        if self.ff_transforms:
            lines += ["", "ffmpeg transform presets (-ff_transform_preset; -ff_transform wins):"]
            for t in self.ff_transforms.values():
                lines.append("  {:<{}}  {}".format(t.name, width, t.notes))
                lines.append("  {:<{}}    {}".format("", width, t.chain()))
        if self.transforms:
            lines += ["", "OIIO transforms (-oiio_transform), default {}; first matching sub wins:"
                      .format(self.default_transform or NO_TRANSFORM)]
            for t in self.transforms.values():
                lines.append("  {:<{}}  {}".format(t.name, width, t.description))
                for sub in t.subs:
                    when = " and ".join(c.describe() for c in sub.conditions) or "always"
                    lines.append("  {:<{}}    {}: if {} -> {}".format(
                        "", width, sub.name, when, " ".join(sub.transforms)))
        if self.name_rules:
            lines += ["", "Name tokens (name_tokens; -name_token adds or overrides):"]
            for r in self.name_rules.values():
                lines.append("  {{{}}} = regex {} on {}{}".format(
                    r.name, r.regex, r.source, ", default {!r}".format(r.default) if r.default else ""))
        if self.smart_tokens:
            lines += ["", "Smart tokens (-smart_tokens), default {}; top to bottom, last applied wins:"
                      .format(self.default_smart_tokens or NO_SMART_TOKENS)]
            for st in self.smart_tokens.values():
                lines.append("  {:<{}}  {}".format(st.name, width, st.notes))
                for t in st.tokens:
                    what = t.source
                    if t.regex:
                        what += " ~ {}{}{}".format("s/" if t.mode == "replace" else "", t.regex,
                                                   " -> '{}'".format(t.repl) if t.repl else "")
                    when = " and ".join(c.describe() for c in t.conditions)
                    lines.append("  {:<{}}    {{{}}} = {}{}{}".format(
                        "", width, t.name, what, "" if t.case == "as_is" else ", " + t.case,
                        "  if " + when if when else ""))
        if self.channel_preferences:
            lines += ["", "Automatic channel choice (channel_preferences), first found wins:"]
            lines += ["  " + ",".join(chans) for chans in self.channel_preferences]
        return "\n".join(lines)


def _preset_from_json(name, data, path):
    if not isinstance(data, dict):
        raise TranscodeError('{}: preset "{}" must be an object.'.format(path, name))
    missing = [k for k in ("ext", "encode") if k not in data]
    if missing:
        raise TranscodeError('{}: preset "{}" is missing {}.'.format(path, name, ", ".join(missing)))
    encode = data["encode"]
    if isinstance(encode, str):
        args = split_args(encode)
    elif isinstance(encode, list) and all(isinstance(a, str) for a in encode):
        args = list(encode)
    else:
        raise TranscodeError('{}: preset "{}": "encode" must be a string or a list of strings.'
                             .format(path, name))
    if "{videofilter}" not in args:
        raise TranscodeError('{}: preset "{}": "encode" needs {{videofilter}} as a separate word.'
                             .format(path, name))
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = " ".join(str(n) for n in notes)
    metadata = _tags_from_json(data.get("metadata", {}), path, 'preset "{}": "metadata"'.format(name))
    return Preset(name=name, ext=str(data["ext"]).lstrip("."), encode=tuple(args),
                  ten_bit=bool(data.get("ten_bit", False)),
                  colour_tags=bool(data.get("colour_tags", True)), notes=str(notes),
                  metadata=metadata, alpha_pix_fmt=str(data.get("alpha_pix_fmt") or ""))


def _tags_from_json(data, path, where):
    """{"key": "value"} with text or number values -> OrderedDict of strings."""
    if not isinstance(data, dict):
        raise TranscodeError('{}: {} must be an object like {{"title": "{{clean_name}}"}}.'
                             .format(path, where))
    out = OrderedDict()
    for key, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise TranscodeError('{}: {}: value of "{}" must be text or a number.'.format(path, where, key))
        out[str(key)] = str(value)
    return out


def _metadata_preset_from_json(name, data, path):
    where = 'metadata preset "{}"'.format(name)
    if name == NO_BURNIN:
        raise TranscodeError('{}: "{}" is reserved (it means no metadata preset).'.format(path, name))
    if not isinstance(data, dict):
        raise TranscodeError("{}: {} must be an object.".format(path, where))
    unknown = sorted(set(data) - set(METADATA_PRESET_FIELDS))
    if unknown:
        raise TranscodeError("{}: {}: unknown field(s) {}; use {}.".format(
            path, where, ", ".join(unknown), ", ".join(METADATA_PRESET_FIELDS)))
    values = {}
    for key in METADATA_PRESET_FIELDS[2:]:
        if key in data:
            value = data[key]
            if not isinstance(value, str):
                raise TranscodeError('{}: {}: "{}" must be text.'.format(path, where, key))
            if key in METADATA_CHOICES and value not in METADATA_CHOICES[key]:
                raise TranscodeError('{}: {}: "{}" must be one of {}.'.format(
                    path, where, key, ", ".join(METADATA_CHOICES[key])))
            if key in METADATA_CHOICES and value == "metadata":
                value = "exr"                 # "metadata" is the new name of "exr"
            values[key] = value
    if "reelid_pattern" in values and values["reelid_pattern"]:
        import re
        try:
            re.compile(values["reelid_pattern"])
        except re.error as exc:
            raise TranscodeError('{}: {}: "reelid_pattern": {}'.format(path, where, exc))
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = " ".join(str(n) for n in notes)
    return MetadataPreset(name=name, tags=_tags_from_json(data.get("tags", {}), path, where + ': "tags"'),
                          notes=str(notes), **values)


def _burnin_from_json(name, data, path):
    where = '{}: burn-in preset "{}"'.format(path, name)
    if name == NO_BURNIN:
        raise TranscodeError('{}: "{}" is reserved (it switches burn-ins off).'.format(path, name))
    if not isinstance(data, dict):
        raise TranscodeError("{} must be an object.".format(where))
    allowed = set(BURN_PLACES) | set(STYLE_FIELDS) | set(MARGIN_FIELDS) | {"font", "notes"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
            where, ", ".join(unknown), ", ".join(list(BURN_PLACES) + list(STYLE_FIELDS)
                                                 + list(MARGIN_FIELDS) + ["font", "notes"])))
    margins = margins_from_values(data, where)
    style = style_from_values({k: data.get(k) for k in STYLE_FIELDS}, where)
    texts, place_styles = OrderedDict(), OrderedDict()
    for place in BURN_PLACES:
        value = data.get(place)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            texts[place] = value
            continue
        place_where = "{}: {} ({})".format(where, place, PLACE_NAMES[place])
        if not isinstance(value, dict):
            raise TranscodeError('{} must be text or an object like {{"text": "{{clean_name}}", '
                                 '"color": "yellow"}}.'.format(place_where))
        bad = sorted(set(value) - ({"text"} | set(STYLE_FIELDS)))
        if bad:
            raise TranscodeError("{}: unknown field(s) {}; use text, {}.".format(
                place_where, ", ".join(bad), ", ".join(STYLE_FIELDS)))
        if value.get("text"):
            texts[place] = str(value["text"])
        place_style = style_from_values({k: value.get(k) for k in STYLE_FIELDS}, place_where)
        if not place_style.is_empty():
            place_styles[place] = place_style
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = " ".join(str(n) for n in notes)
    return BurnPreset(name=name, corners=dict(texts), style=style, place_styles=dict(place_styles),
                      margins=margins, font=data.get("font") or None, notes=str(notes))


def preset_relative_path(value, presets_path):
    """A file named in the presets file: environment variables expanded; ./ and ../ are the
    presets file's folder (so a studio presets file on a share can keep its logo beside it),
    falling back to the application folder."""
    import os
    from .tools import app_dir, expand_path, resolve_path
    text = expand_path(value.strip())
    if text == "." or text.startswith(("./", ".\\", "../", "..\\")):
        beside = resolve_path(text, os.path.dirname(os.path.abspath(str(presets_path))))
        if os.path.exists(beside):
            return beside
        return resolve_path(text, app_dir())
    return text


def _slate_from_json(name, data, path):
    where = '{}: slate preset "{}"'.format(path, name)
    if name == NO_SLATE:
        raise TranscodeError('{}: "{}" is reserved (it switches the slate off).'.format(path, name))
    if not isinstance(data, dict):
        raise TranscodeError("{} must be an object.".format(where))
    unknown = sorted(set(data) - set(SLATE_PRESET_FIELDS))
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
            where, ", ".join(unknown), ", ".join(SLATE_PRESET_FIELDS)))
    values = {}
    for key in ("title", "subtitle"):
        if data.get(key) is not None:
            values[key] = str(data[key])
    if data.get("fields") is not None:
        values["fields"] = parse_fields(data["fields"], where)
    if data.get("thumbnail") is not None:
        values["thumbnail"] = parse_thumbnail(data["thumbnail"], where + ": thumbnail")
    if data.get("hide_empty") is not None:
        values["hide_empty"] = bool(data["hide_empty"])
    values.update(style_values(data, where))
    if data.get("font"):
        values["font"] = str(data["font"])
    if data.get("logo"):
        values["logo"] = preset_relative_path(str(data["logo"]), path)
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = " ".join(str(n) for n in notes)
    return SlateSpec(name=name, notes=str(notes), **values)


def _channel_preferences_from_json(data, path):
    """[["rgba.R", ...], "R,G,B", ...] -> list of channel lists."""
    if not isinstance(data, list) or not data:
        raise TranscodeError('{}: "channel_preferences" must be a non-empty list.'.format(path))
    out = []
    for index, item in enumerate(data, 1):
        if isinstance(item, str):
            chans = [c.strip() for c in item.split(",") if c.strip()]
        elif isinstance(item, list) and all(isinstance(c, str) for c in item):
            chans = [c.strip() for c in item if c.strip()]
        else:
            chans = []
        if not chans:
            raise TranscodeError('{}: "channel_preferences" entry {} must be channel names, '
                                 'e.g. "R,G,B" or ["R", "G", "B"].'.format(path, index))
        out.append(chans)
    return out


@dataclass(frozen=True)
class ChannelOrderPreset:
    """A named order for the automatic channel search: the sets tried, first found wins."""
    name: str
    sets: Tuple[Tuple[str, ...], ...]
    notes: str = ""

    def as_lists(self):
        return [list(s) for s in self.sets]


def channel_orders_from_json(data, path):
    """"channel_preference_presets": {"name": {"notes": "...", "channels": ["R,G,B", ...]}};
    a plain list works too."""
    out = OrderedDict()
    if data is None:
        return out
    if not isinstance(data, dict):
        raise TranscodeError('{}: "channel_preference_presets" must be an object.'.format(path))
    for name, value in data.items():
        where = '{}: channel preference preset "{}"'.format(path, name)
        if isinstance(value, list):
            value = {"channels": value}
        if not isinstance(value, dict) or "channels" not in value:
            raise TranscodeError('{} needs "channels": ["R,G,B", ...].'.format(where))
        sets = _channel_preferences_from_json(value["channels"], where)
        out[name] = ChannelOrderPreset(name, tuple(tuple(s) for s in sets), str(value.get("notes", "")))
    return out


def parse_channel_order(text):
    """-channel_order "R,G,B,A; beauty.R,beauty.G,beauty.B" -> [[...], [...]]; sets are
    separated by ; or |, channels by commas."""
    sets = []
    for part in re.split(r"[;|\n]", text or ""):
        chans = [c.strip() for c in part.split(",") if c.strip()]
        if chans:
            sets.append(chans)
    return sets


def parse_tools(data, path):
    """The "tools" section: {"ffmpeg": PATH, "ffprobe": PATH, "oiiotool": PATH}, all optional.
    An empty path means "not set" (the built-in default)."""
    if data is None:
        return OrderedDict()
    if not isinstance(data, dict):
        raise TranscodeError('{}: "tools" must be an object like {{"ffmpeg": "./ffmpeg/ffmpeg.exe"}}.'
                             .format(path))
    unknown = sorted(k for k in data if k not in TOOLS and k != "notes")
    if unknown:
        raise TranscodeError('{}: "tools": unknown tool(s) {}; use {}.'.format(
            path, ", ".join(unknown), ", ".join(TOOLS)))
    tools = OrderedDict()
    for name in TOOLS:
        value = data.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise TranscodeError('{}: "tools": {} must be a path (text), got {!r}.'.format(
                path, name, value))
        if value.strip():
            tools[name] = value.strip()
    return tools


def load_presets(path):
    try:
        # utf-8-sig: tolerate the BOM Notepad may add
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh, object_pairs_hook=OrderedDict)
    except FileNotFoundError:
        raise TranscodeError('Presets file "{}" not found.'.format(path))
    except json.JSONDecodeError as exc:
        raise TranscodeError("{}: line {} column {}: {}".format(path, exc.lineno, exc.colno, exc.msg))
    if not isinstance(data, dict):
        raise TranscodeError("{}: expected a JSON object.".format(path))
    raw = data.get("encode_presets") or data.get("presets")   # "presets": older name
    if not raw:
        raise TranscodeError('{}: no "encode_presets" found.'.format(path))
    presets = OrderedDict((name, _preset_from_json(name, p, path)) for name, p in raw.items())
    default = data.get("default_encode") or data.get("default") or next(iter(presets))
    if default not in presets:
        raise TranscodeError('{}: default encode preset "{}" is not defined.'.format(path, default))

    burnins = OrderedDict((name, _burnin_from_json(name, b, path))
                          for name, b in (data.get("burnin_presets") or {}).items())
    default_burnin = data.get("default_burnin") or ""
    if default_burnin and default_burnin != NO_BURNIN and default_burnin not in burnins:
        raise TranscodeError('{}: default burn-in preset "{}" is not defined.'.format(
            path, default_burnin))
    if "channel_preferences" in data:
        channel_preferences = _channel_preferences_from_json(data["channel_preferences"], path)
    else:
        from .channels import PREFERRED_CHANNELS          # older files: built-in list
        channel_preferences = [list(c) for c in PREFERRED_CHANNELS]
    channel_orders = channel_orders_from_json(data.get("channel_preference_presets"), path)
    metadata_presets = OrderedDict((name, _metadata_preset_from_json(name, m, path))
                                   for name, m in (data.get("metadata_presets") or {}).items())
    default_metadata = data.get("default_metadata") or ""
    if default_metadata and default_metadata != NO_BURNIN and default_metadata not in metadata_presets:
        raise TranscodeError('{}: default metadata preset "{}" is not defined.'.format(
            path, default_metadata))
    slates = OrderedDict((name, _slate_from_json(name, sl, path))
                         for name, sl in (data.get("slate_presets") or {}).items())
    default_slate = data.get("default_slate") or ""
    if default_slate and default_slate != NO_SLATE and default_slate not in slates:
        raise TranscodeError('{}: default slate preset "{}" is not defined.'.format(path, default_slate))
    thumbnails = presets_from_json(data.get("thumbnail_presets") or {}, path)
    default_thumbnail = data.get("default_thumbnail") or ""
    if default_thumbnail and default_thumbnail not in thumbnails:
        raise TranscodeError('{}: default thumbnail preset "{}" is not defined.'.format(
            path, default_thumbnail))
    from .outseq import sequence_presets_from_json
    sequences, default_sequences = sequence_presets_from_json(
        data.get("sequence_presets"), data.get("default_sequence_presets"), path)
    filmstrips = filmstrips_from_json(data.get("filmstrip_presets") or {}, path)
    default_filmstrip = data.get("default_filmstrip") or ""
    if default_filmstrip and default_filmstrip not in filmstrips:
        raise TranscodeError('{}: default filmstrip preset "{}" is not defined.'.format(
            path, default_filmstrip))
    raw_transforms = data.get("oiio_transforms", data.get("oiiotransforms"))
    transforms = parse_transform_presets(raw_transforms, path) if raw_transforms else OrderedDict()
    default_transform = data.get("default_oiio_transform") or ""
    if default_transform and default_transform != NO_TRANSFORM and default_transform not in transforms:
        raise TranscodeError('{}: default oiio transform "{}" is not defined.'.format(
            path, default_transform))
    name_rules = rules_from_json(data["name_tokens"], path) if "name_tokens" in data else OrderedDict()
    smart = parse_smart_token_presets(data["smart_tokens"], path) if data.get("smart_tokens") else OrderedDict()
    default_smart = data.get("default_smart_tokens") or ""
    if default_smart and default_smart != NO_SMART_TOKENS and default_smart not in smart:
        raise TranscodeError('{}: default smart tokens preset "{}" is not defined.'.format(
            path, default_smart))
    return PresetFile(str(path), default, presets, burnins,
                      "" if default_burnin == NO_BURNIN else default_burnin, channel_preferences,
                      metadata_presets, "" if default_metadata == NO_BURNIN else default_metadata,
                      slates, "" if default_slate == NO_SLATE else default_slate,
                      transforms, "" if default_transform == NO_TRANSFORM else default_transform,
                      thumbnails, default_thumbnail, name_rules, smart,
                      "" if default_smart == NO_SMART_TOKENS else default_smart,
                      filmstrips=filmstrips, default_filmstrip=default_filmstrip,
                      tools=parse_tools(data.get("tools"), path),
                      sequences=sequences, default_sequences=default_sequences,
                      colours=colour_presets_from_json(data.get("colour_presets"), path),
                      ff_transforms=ff_transforms_from_json(data.get("ff_transforms"), path),
                      channel_orders=channel_orders,
                      dest_paths=destinations.from_json(data.get(destinations.PATHS_SECTION), path, "path"),
                      dest_names=destinations.from_json(data.get(destinations.NAMES_SECTION), path, "name"))


# --- colour presets ---

COLOUR_FIELDS = ("ci", "co", "display", "view", "looks", "clf", "ocio")
COLOUR_OPTION = {"ci": "-ci", "co": "-co", "display": "-display", "view": "-view",
                 "looks": "-looks", "clf": "-clf", "ocio": "-ocio"}


@dataclass(frozen=True)
class ColourPreset:
    """Named colour settings: the -ci / -co / -display / -view / -looks / -clf / -ocio
    values, e.g. "rec709_to_acescg". Empty fields are not set."""
    name: str
    notes: str = ""
    ci: str = ""
    co: str = ""
    display: str = ""
    view: str = ""
    looks: str = ""
    clf: str = ""
    ocio: str = ""

    def values(self):
        return OrderedDict((f, getattr(self, f)) for f in COLOUR_FIELDS if getattr(self, f))

    def describe(self):
        return " ".join("{} {}".format(COLOUR_OPTION[f], json.dumps(v) if " " in v else v)
                        for f, v in self.values().items()) or "(nothing set)"


def colour_presets_from_json(data, path):
    out = OrderedDict()
    if data is None:
        return out
    if not isinstance(data, dict):
        raise TranscodeError('{}: "colour_presets" must be an object.'.format(path))
    for name, entry in data.items():
        where = '{}: colour preset "{}"'.format(path, name)
        if not isinstance(entry, dict):
            raise TranscodeError("{} must be an object.".format(where))
        unknown = sorted(set(entry) - set(COLOUR_FIELDS) - {"notes"})
        if unknown:
            raise TranscodeError("{}: unknown field(s) {}; use notes, {}.".format(
                where, ", ".join(unknown), ", ".join(COLOUR_FIELDS)))
        values = {}
        for key in COLOUR_FIELDS:
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                raise TranscodeError('{}: "{}" must be text.'.format(where, key))
            values[key] = (value or "").strip()
        if values["clf"] and (values["display"] or values["view"] or values["looks"]):
            raise TranscodeError("{}: clf can't be combined with display, view or looks.".format(where))
        out[name] = ColourPreset(name=name, notes=_notes_text(entry.get("notes", "")), **values)
    return out


# --- ffmpeg transform presets ---

@dataclass(frozen=True)
class FFTransformPreset:
    """Named -ff_transform: ffmpeg scale / zscale filters, one per list entry."""
    name: str
    notes: str = ""
    filters: tuple = ()

    def chain(self):
        lines = [l.strip().rstrip(",") for l in self.filters]
        return ",".join(l for l in lines if l and not l.startswith("#"))


def ff_transforms_from_json(data, path):
    out = OrderedDict()
    if data is None:
        return out
    if not isinstance(data, dict):
        raise TranscodeError('{}: "ff_transforms" must be an object.'.format(path))
    for name, entry in data.items():
        where = '{}: ffmpeg transform preset "{}"'.format(path, name)
        if not isinstance(entry, dict):
            raise TranscodeError("{} must be an object.".format(where))
        unknown = sorted(set(entry) - {"notes", "filters"})
        if unknown:
            raise TranscodeError('{}: unknown field(s) {}; use notes, filters.'.format(
                where, ", ".join(unknown)))
        filters = entry.get("filters", [])
        if isinstance(filters, str):
            filters = [filters]
        if not (isinstance(filters, list) and all(isinstance(f, str) for f in filters)):
            raise TranscodeError('{}: "filters" must be text or a list of text.'.format(where))
        out[name] = FFTransformPreset(name=name, notes=_notes_text(entry.get("notes", "")),
                                      filters=tuple(filters))
    return out


def _notes_text(notes):
    return " ".join(str(n) for n in notes) if isinstance(notes, list) else str(notes)


def transfer_for(colorout):
    """ffmpeg -color_trc tag matching the OCIO output colourspace."""
    return "iec61966-2-1" if "srgb" in colorout.lower().replace(" ", "") else "bt709"


def colour_tags(colorout):
    return ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", transfer_for(colorout)]
