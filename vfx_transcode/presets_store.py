"""Saving named presets into the presets file (the studio preferences when vfx-transcode.ini
names them), multi-user safe (see safe_save).

The file is read again just before saving, only the one preset is put in, and the result is
checked by loading it as vfx-transcode would before it replaces the file. The JSON keeps the
file's own layout: two-space indents, and short lists of small objects (such as transform
conditions) on one line, so a save changes only the lines of the preset saved.
"""

import json
from collections import OrderedDict
from pathlib import Path

from . import safe_save
from .errors import TranscodeError

# tuner area -> presets file section
SECTIONS = OrderedDict([("colour", "colour_presets"), ("transform", "ff_transforms"),
                        ("transform_sequence", "oiio_transforms"), ("burnin", "burnin_presets"),
                        ("slate", "slate_presets"), ("filmstrip", "filmstrip_presets")])
RESERVED = {"none", "plain", ""}
INLINE_WIDTH = 100
UPDATE_ATTEMPTS = 5


# --- JSON layout ---

def _scalar(value):
    return not isinstance(value, (dict, list))


def _inline_list(value, used):
    """A list of objects holding only plain values, short enough to keep the whole line
    (`used` characters before it) within INLINE_WIDTH: on one line."""
    if not value or not all(isinstance(v, dict) and all(_scalar(x) for x in v.values()) for v in value):
        return None
    text = json.dumps(value, ensure_ascii=False)
    return text if used + len(text) + 1 <= INLINE_WIDTH else None


def _dump(value, indent, used=0):
    pad, inner = " " * indent, " " * (indent + 2)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = []
        for k, v in value.items():
            key = "{}{}: ".format(inner, json.dumps(k, ensure_ascii=False))
            items.append(key + _dump(v, indent + 2, len(key)))
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        inline = _inline_list(value, used or indent)
        if inline is not None:
            return inline
        return "[\n" + ",\n".join(inner + _dump(v, indent + 2, len(inner)) for v in value) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def dumps(data, newline="\n"):
    return (_dump(data, 0) + "\n").replace("\n", newline)


# --- reading ---

def read_raw(path):
    """(OrderedDict, newline) of the presets file as it is on disk."""
    raw = Path(path).read_bytes().decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    try:
        return json.loads(raw, object_pairs_hook=OrderedDict), newline
    except ValueError as exc:
        raise TranscodeError("{}: {}".format(path, exc))


# --- putting a preset in ---

def put(data, area, name, entry, sub=None):
    """Put one preset into the file's data. transform_sequence: entry is a sub-preset; it
    replaces the sub of the same name in preset `name`, or is added to it, or makes a new
    preset with that one sub (sub = the sub-preset's name)."""
    section = SECTIONS[area]
    if area == "transform_sequence":
        transforms = data.setdefault(section, [])
        if not isinstance(transforms, list):
            raise TranscodeError('"{}" must be a list.'.format(section))
        preset = next((t for t in transforms if isinstance(t, dict) and t.get("name") == name), None)
        if preset is None:
            preset = OrderedDict([("name", name), ("description", entry.get("description", "")),
                                  ("subs", [])])
            transforms.append(preset)
        subs = preset.setdefault("subs", [])
        for i, existing in enumerate(subs):
            if existing.get("name") == sub:
                subs[i] = entry
                break
        else:
            subs.append(entry)
        return
    presets = data.setdefault(section, OrderedDict())
    if not isinstance(presets, dict):
        raise TranscodeError('"{}" must be an object.'.format(section))
    presets[name] = entry


def exists(presets, area, name, sub=None):
    """Whether the loaded presets (PresetFile) already have this name (and sub-preset)."""
    if presets is None:
        return False
    table = {"colour": presets.colours, "transform": presets.ff_transforms,
             "transform_sequence": presets.transforms, "burnin": presets.burnins,
             "slate": presets.slates, "filmstrip": presets.filmstrips}[area]
    if name not in table:
        return False
    if area == "transform_sequence":
        return any(s.name == sub for s in table[name].subs)
    return True


def check_name(name):
    name = (name or "").strip()
    if name.lower() in RESERVED:
        raise TranscodeError('"{}" can\'t be a preset name.'.format(name))
    return name


def save_preset(path, area, name, entry, sub=None):
    """Save one preset into the presets file at `path`: read, put, check that it loads,
    write multi-user safe. Someone else's save in between is merged (read again, put again).
    Returns safe_save.SaveResult."""
    from .presets import load_presets
    name = check_name(name)
    if area == "transform_sequence":
        sub = check_name(sub)
    path = Path(path)

    def validate(tmp):
        try:
            load_presets(str(tmp))
        except TranscodeError as exc:
            raise TranscodeError("The saved presets would not load, nothing was changed: {}"
                                 .format(str(exc).replace(str(tmp), path.name)))

    for attempt in range(UPDATE_ATTEMPTS):
        stamp = safe_save.file_stamp(path)
        data, newline = read_raw(path) if path.is_file() else (OrderedDict(), "\n")
        put(data, area, name, entry, sub)
        try:
            return safe_save.safe_write_text(path, dumps(data, newline), expect_stamp=True,
                                             stamp=stamp, validate=validate)
        except safe_save.SaveConflict:
            if attempt == UPDATE_ATTEMPTS - 1:
                raise
