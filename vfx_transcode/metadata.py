"""Metadata written to the output movie.

Two sources, merged in this order (later wins):
  1. -metadata_copy SPEC   attributes copied from the EXR header
  2. -metadata KEY=VALUE   explicit values (repeatable); values may use
                           {clean_name} {frame_start} {frame_end} {timecode} {fps} {preset} {date}

SPEC is "all", or comma-separated key patterns with * wildcards; a leading "-"
excludes: "all,-husk:*" or "Artist,Software,capDate". Copying is off by default:
render headers often carry command lines, host names and internal paths.
"""

import fnmatch
import logging
import re
import string
from collections import OrderedDict

from .errors import TranscodeError

log = logging.getLogger(__name__)

MAX_VALUE_LENGTH = 1024

# Image-structure attributes that mean nothing (or something wrong) on a movie.
STRUCTURAL = {
    "compression", "PixelAspectRatio", "pixelAspectRatio", "screenWindowCenter",
    "screenWindowWidth", "chromaticities", "lineOrder", "dataWindow", "displayWindow",
    "channels", "tiles", "type", "version", "chunkCount", "name", "view", "multiView",
}
STRUCTURAL_PREFIXES = ("oiio:", "openexr:", "exr:", "smpte:")

# Containers where ffmpeg only writes arbitrary keys with -movflags +use_metadata_tags
MOV_FAMILY = {"mov", "mp4", "m4v"}
TIMECODE_TRACK_CONTAINERS = {"mov", "mp4", "m4v", "mxf"}


def header_metadata(attribs):
    """Copyable header attributes as strings: scalars only, no structure, no huge blobs."""
    out = OrderedDict()
    for key, value in attribs.items():
        if key in STRUCTURAL or key.startswith(STRUCTURAL_PREFIXES):
            continue
        if value is None or isinstance(value, (list, tuple, dict)):
            continue
        text = str(value)
        if len(text) > MAX_VALUE_LENGTH:
            log.debug("Metadata %s skipped: %d characters", key, len(text))
            continue
        out[key] = text
    return out


def select(metadata, spec):
    """Filter metadata keys by a copy spec ("" = nothing)."""
    tokens = [t.strip() for t in (spec or "").split(",") if t.strip()]
    include = ["*" if t == "all" else t for t in tokens if not t.startswith("-")]
    exclude = [t[1:] for t in tokens if t.startswith("-")]
    if not include:
        return OrderedDict()
    return OrderedDict(
        (k, v) for k, v in metadata.items()
        if any(fnmatch.fnmatchcase(k, p) for p in include)
        and not any(fnmatch.fnmatchcase(k, p) for p in exclude))


def unmatched_patterns(metadata, spec):
    """The include patterns of a copy spec that match none of the keys ("all" never counts)."""
    tokens = [t.strip() for t in (spec or "").split(",") if t.strip()]
    return [t for t in tokens if not t.startswith("-") and t != "all"
            and not any(fnmatch.fnmatchcase(k, t) for k in metadata)]


def key_prefixes(metadata, most=8):
    """How the keys start, for a hint: "unreal/", "arnold/", "Software" ..."""
    seen = []
    for key in metadata:
        cut = min([i for i in (key.find("/"), key.find(":")) if i > 0] or [len(key)])
        prefix = key[:cut + 1] if cut < len(key) else key
        if prefix not in seen:
            seen.append(prefix)
    return seen[:most]


def copy_hint(metadata, spec):
    """"" when every pattern of the spec matches; else why, e.g. 'unreal:* matches nothing;
    the keys start with: unreal/, arnold/ (try unreal/*)'."""
    missing = unmatched_patterns(metadata, spec)
    if not missing:
        return ""
    prefixes = key_prefixes(metadata)
    text = "{} match{} nothing in the header".format(", ".join(missing), "" if len(missing) > 1 else "es")
    if prefixes:
        text += "; its keys start with: {}".format(", ".join(prefixes))
        tries = []
        for pattern in missing:
            stem = re.split(r"[/:*?]", pattern, maxsplit=1)[0]
            tries += [p + "*" for p in prefixes if stem and p.lower().startswith(stem.lower())
                      and p.endswith(("/", ":"))]
        if tries:
            text += " (try {})".format(", ".join(dict.fromkeys(tries)))
    return text


def parse_pairs(pairs):
    """['title=Shot 010', 'comment=x'] -> OrderedDict. Validated early by the CLI."""
    out = OrderedDict()
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise TranscodeError('-metadata expects KEY=VALUE, got "{}".'.format(pair))
        out[key.strip()] = value
    return out


_reported = set()


def meta_lookup(attribs, spec):
    """{meta:NAME} / {meta:NAME|fallback}: the header attribute (OIIO or EXR name, any case)
    as text. Returns (value, found). Each name is logged once."""
    name, sep, fallback = (spec or "").partition("|")
    name = name.strip()
    value = header_value(attribs or {}, name) if name else ""
    found = bool(value)
    if name not in _reported:
        _reported.add(name)
        if found:
            log.info("Header value {meta:%s} = %r", name, value)
        else:     # with an explicit fallback ({meta:Artist|}) a missing value is expected
            (log.info if sep else log.warning)(
                "Header value {meta:%s} not found in the first frame; using %r", name, fallback)
    return (value if found else fallback), found


def header_attribs(info):
    """All attributes of an image header, first subimage winning."""
    attribs = OrderedDict()
    for sub in reversed(info or []):
        attribs.update(sub.get("attribs", {}))
    return attribs


def expand(value, context):
    """Fill {keywords}; unknown ones and stray braces are left as typed.
    {meta:NAME} reads the source header, from context["meta"]."""
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError:
        return value
    out = []
    for literal, field, spec, _conv in parsed:
        out.append(literal)
        if field is None:
            continue
        if field == "meta":
            out.append(meta_lookup(context.get("meta"), spec)[0])
        elif field in context:
            try:
                out.append(format(context[field], spec) if spec else str(context[field]))
            except ValueError:            # e.g. {version_number:03d} on a text value
                out.append(str(context[field]))
        else:
            out.append("{" + field + (":" + spec if spec else "") + "}")
    return "".join(out)


def build(copied, explicit, context, preset_layers=()):
    """Merge, later wins: copied header attributes, then each preset layer (encode preset
    metadata, metadata preset tags), then the explicit -metadata. An empty value removes
    the key, so "-metadata comment=" drops a preset's comment."""
    merged = OrderedDict(copied)
    for layer in list(preset_layers) + [explicit]:
        for key, value in (layer or {}).items():
            if value == "":
                merged.pop(key, None)
            else:
                merged[key] = expand(value, context)
    return merged


def oiio_args(metadata):
    """--attrib arguments for oiiotool: each tag as a string attribute of the written image.
    Expression evaluation is off around them, so braces in a value stay as they are."""
    if not metadata:
        return []
    args = ["--evaloff"]
    for key, value in metadata.items():
        args += ["--attrib:type=string", key, str(value)]
    return args + ["--evalon"]


def ffmpeg_args(metadata, timecode, ext, write_timecode, reel_name=""):
    """-metadata / -timecode arguments for the output file.

    reel_name goes on the video stream, where ffmpeg turns it into the reel name of the mov
    timecode track (tmcd source reference, read by conform tools) and of the mxf source
    package, and also into a file-level reel_name tag (the only place mp4/mkv keep it)."""
    args = []
    ext = ext.lower()
    if write_timecode and timecode:
        if ext in TIMECODE_TRACK_CONTAINERS:
            args += ["-timecode", timecode]          # real timecode track (tmcd)
        else:
            args += ["-metadata", "timecode=" + timecode]
    if reel_name:
        args += ["-metadata:s:v:0", "reel_name=" + reel_name, "-metadata", "reel_name=" + reel_name]
    for key, value in metadata.items():
        args += ["-metadata", "{}={}".format(key, value)]
    return args


REEL_ORDER = {"auto": ("arg", "exr"), "arg": ("arg",), "exr": ("exr", "arg"), "off": ()}

# Camera clip names often carry the source frame range: "Z017_C001_20190920_R[00000-02773]".
_FRAME_RANGE_SUFFIX = re.compile(r"\s*\[\s*\d+\s*(?:-\s*\d+\s*)?\]\s*$")


def clean_reel_name(value, pattern=""):
    """Reel name from a header value: with a pattern, its first group (or the whole match);
    without one, the value minus a trailing [first-last] frame range."""
    value = value.strip()
    if pattern:
        match = re.search(pattern, value)
        if not match:
            log.warning("-reelid_pattern %r does not match %r; using it unchanged.", pattern, value)
            return value
        return (match.group(1) if match.groups() else match.group(0)).strip()
    return _FRAME_RANGE_SUFFIX.sub("", value)


# OpenImageIO renames the standard OpenEXR attributes when reading; the EXR name is accepted
# too, so "-reelid_attr owner" finds OIIO's "Copyright".
EXR_TO_OIIO = {
    "owner": "Copyright", "comments": "ImageDescription", "capDate": "DateTime",
    "timeCode": "smpte:TimeCode", "keyCode": "smpte:KeyCode", "framesPerSecond": "FramesPerSecond",
    "expTime": "ExposureTime", "aperture": "FNumber",
}


def header_value(attribs, name):
    """Attribute by name (or its OIIO name for standard EXR attributes), exact first, then
    case-insensitive; stripped text or ''."""
    names = [name]
    oiio_name = next((v for k, v in EXR_TO_OIIO.items() if k.lower() == name.lower()), None)
    if oiio_name:
        names.append(oiio_name)
    value = None
    for candidate in names:
        if candidate in attribs:
            value = attribs[candidate]
            break
        match = next((k for k in attribs if k.lower() == candidate.lower()), None)
        if match:
            value = attribs[match]
            break
    if value is None or isinstance(value, (list, tuple, dict)):
        return ""
    return str(value).strip()


def merge_movflags(args, ext, needed):
    """Add +use_metadata_tags to (a single) -movflags so custom keys survive in mov/mp4."""
    if not needed or ext.lower() not in MOV_FAMILY:
        return args
    args = list(args)
    if "-movflags" in args:
        i = args.index("-movflags") + 1
        if i < len(args) and "use_metadata_tags" not in args[i]:
            args[i] = args[i] + "+use_metadata_tags"
    else:
        args += ["-movflags", "+use_metadata_tags"]
    return args
