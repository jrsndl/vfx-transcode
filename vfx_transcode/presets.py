"""Encode, burn-in and metadata presets and channel preferences, loaded from a user-editable
JSON file (presets.json)."""

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .burnin import BURN_PLACES, PLACE_NAMES, STYLE_FIELDS, BurnStyle, style_from_values
from .errors import TranscodeError
from .tools import split_args

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


# Fields of a metadata preset; the command-line options with the same meaning override them.
METADATA_PRESET_FIELDS = ("notes", "tags", "copy", "reelid_source", "reelid", "reelid_attr",
                          "reelid_pattern", "timecode_source")
METADATA_CHOICES = {"reelid_source": ("auto", "arg", "exr", "off"),
                    "timecode_source": ("counter", "arg", "exr")}


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
                    + list(self.metadata_presets))
        lines = ["Encode presets (-preset), default {}:".format(self.default)]
        lines += ["  {:<{}}  .{:<4} {}".format(p.name, width, p.ext, p.notes)
                  for p in self.presets.values()]
        if self.burnins:
            lines += ["", "Burn-in presets (-burn_preset), default {}:".format(
                self.default_burnin or NO_BURNIN)]
            for b in self.burnins.values():
                lines.append("  {:<{}}  {}".format(b.name, width, b.notes))
                if not b.style.is_empty():
                    lines.append("  {:<{}}    style: {}".format("", width, b.style.describe()))
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
                  metadata=metadata)


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
    allowed = set(BURN_PLACES) | set(STYLE_FIELDS) | {"font", "notes"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
            where, ", ".join(unknown), ", ".join(list(BURN_PLACES) + list(STYLE_FIELDS) + ["font", "notes"])))
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
                      font=data.get("font") or None, notes=str(notes))


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
    metadata_presets = OrderedDict((name, _metadata_preset_from_json(name, m, path))
                                   for name, m in (data.get("metadata_presets") or {}).items())
    default_metadata = data.get("default_metadata") or ""
    if default_metadata and default_metadata != NO_BURNIN and default_metadata not in metadata_presets:
        raise TranscodeError('{}: default metadata preset "{}" is not defined.'.format(
            path, default_metadata))
    return PresetFile(str(path), default, presets, burnins,
                      "" if default_burnin == NO_BURNIN else default_burnin, channel_preferences,
                      metadata_presets, "" if default_metadata == NO_BURNIN else default_metadata)


def transfer_for(colorout):
    """ffmpeg -color_trc tag matching the OCIO output colourspace."""
    return "iec61966-2-1" if "srgb" in colorout.lower().replace(" ", "") else "bt709"


def colour_tags(colorout):
    return ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", transfer_for(colorout)]
