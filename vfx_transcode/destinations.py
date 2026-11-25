"""Destination presets: named output folders ("destination_paths") and file names
("destination_names") in the presets file, made of keywords. The tuner's Destination tab and
vfx-transcode-process offer them; a path and a name together make -o (and -ot, -of).

    "destination_paths": {
      "Source and name subfolder": {"path": "{sourcepath}/{clean_name}/", "notes": "..."}
    },
    "destination_names": {
      "Source _transcoded": {"name": "{clean_name}_transcoded"}
    }

A plain string works too ("Same folder": "./"). A path is a folder: ./ is the input's folder,
../ the folder above it; keywords as in -o. A name is the file name without its extension
("" = the input's name). Without these sections the built-in ones below are used.
"""

from collections import OrderedDict
from dataclasses import dataclass

from .errors import TranscodeError

PATHS_SECTION, NAMES_SECTION = "destination_paths", "destination_names"


@dataclass(frozen=True)
class DestinationPreset:
    name: str
    template: str                     # the path (a folder) or the file name, with keywords
    notes: str = ""


BUILTIN_PATHS = (
    ("Same folder as the source", "./", "Next to the input."),
    ("review folder next to the source", "./review/", ""),
    ("review folder beside the source folder", "./../review/", ""),
    ("Source and name subfolder", "{sourcepath}/{clean_name}/", "A folder named after the input, next to it."),
    ("Custom destination path", "{custom_destination_path}/",
     "The folder vfx-transcode-process users type (or pick) themselves."),
)
BUILTIN_NAMES = (
    ("Same name as the source", "", "The input's name."),
    ("Source _transcoded", "{clean_name}_transcoded", ""),
    ("Name + _review", "{clean_name}_review", ""),
    ("Name + date", "{clean_name}_{date}", ""),
    ("Shot + version", "{namesplit1}_{namesplit2}_v{version_string}", ""),
    ("Custom destination name", "{custom_destination_name}",
     "The name vfx-transcode-process users type themselves."),
)


def builtin(kind):
    rows = BUILTIN_PATHS if kind == "path" else BUILTIN_NAMES
    return OrderedDict((n, DestinationPreset(n, t, notes)) for n, t, notes in rows)


def from_json(data, path, kind):
    """{name: DestinationPreset} from a "destination_paths" (kind "path") or
    "destination_names" (kind "name") section; the built-in ones when it is missing."""
    section = PATHS_SECTION if kind == "path" else NAMES_SECTION
    if data is None:
        return builtin(kind)
    if not isinstance(data, dict):
        raise TranscodeError('{}: "{}" must be an object {{"name": {{"{}": "..."}}}}.'.format(path, section, kind))
    out = OrderedDict()
    for name, value in data.items():
        where = '{}: {} "{}"'.format(path, section, name)
        if isinstance(value, str):
            value = {kind: value}
        if not isinstance(value, dict) or not isinstance(value.get(kind, ""), str):
            raise TranscodeError('{} must be "text" or {{"{}": "text", "notes": "..."}}.'.format(where, kind))
        unknown = set(value) - {kind, "notes"}
        if unknown:
            raise TranscodeError("{}: unknown key(s) {}.".format(where, ", ".join(sorted(unknown))))
        template = value.get(kind, "")
        if template.count("{") != template.count("}"):
            raise TranscodeError("{}: unbalanced {{ }} in {!r}.".format(where, template))
        out[name] = DestinationPreset(name, template, str(value.get("notes", "")))
    return out


def to_json(template, kind, notes=""):
    entry = OrderedDict([(kind, template)])
    if notes:
        entry["notes"] = notes
    return entry
