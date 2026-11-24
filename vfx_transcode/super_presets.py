"""Super-presets: the settings of several areas (encode, transform sequence (oiiotool),
transform (ffmpeg), burn-in, slate, filmstrip, metadata) saved together under one name, in
super_presets.json next to the presets file. The two transform areas include their letterbox.

vfx-transcode-tuner writes them. Each area holds two things:
  state  the tuner's values, to load them back into the tuner exactly;
  args   the matching vfx-transcode options, for `vfx-transcode -super_preset NAME`.

On the command line the super-preset's options go in front of the others, so an option given
on the command line wins over the super-preset's (options that add up, such as -metadata or
-slate_field, add to it).

File layout:
  {"super_presets": {"NAME": {"notes": "...", "areas": {"burnin": {"state": {...},
                                                                   "args": ["-burn_preset", ...]}}}}}
"""

import json
import os
from collections import OrderedDict
from pathlib import Path

from . import safe_save
from .errors import TranscodeError

AREAS = ("encode", "transform_sequence", "transform", "burnin", "slate", "filmstrip", "metadata")
AREA_TITLES = OrderedDict([("encode", "Encode"), ("transform_sequence", "Transform Sequence"),
                           ("transform", "Transform"), ("burnin", "Burn-in"), ("slate", "Slate"),
                           ("filmstrip", "Filmstrip"), ("metadata", "Metadata")])
# written by older tuners: "letterbox" (now part of Transform); read, never written
LEGACY_AREAS = ("letterbox",)
FILE_NAME = "super_presets.json"
NOTES = [
    "Super-presets for vfx-transcode, written by vfx-transcode-tuner.",
    "Use one on the command line with -super_preset NAME (several: NAME1,NAME2; later ones win).",
    "Each area has the tuner's values (state) and the matching vfx-transcode options (args).",
    "vfx-transcode reads only args; the tuner reads state. Edit args by hand if you like, but the",
    "tuner will not see the change: save the super-preset again from the tuner instead.",
]


def default_path(presets_path):
    """super_presets.json in the folder of the presets file."""
    return Path(presets_path).resolve().parent / FILE_NAME


def local_path():
    """super_presets.json next to the application's own presets.json: where a super-preset
    goes when the studio folder can't be written."""
    from .presets import DEFAULT_PRESETS_FILE
    from .tools import app_dir, resolve_path
    return default_path(resolve_path(DEFAULT_PRESETS_FILE, app_dir()))


def search_paths(presets_path):
    """The files super-presets are read from: next to the presets file in use (the studio
    preferences, if vfx-transcode.ini names them) and, if that is another folder, the local
    file next to the application."""
    main, local = default_path(presets_path), local_path()
    return [main] if main == local else [main, local]


def load_merged(paths):
    """(presets, {name: file}) from several files. A later file (the local one) wins over an
    earlier one for the same name: a local copy is a deliberate override on this computer."""
    presets, origin = OrderedDict(), {}
    for path in paths:
        for name, entry in load(path).items():
            presets[name] = entry
            origin[name] = Path(path)
    return presets, origin


def load(path):
    """{name: {"notes": str, "areas": {area: {"state": dict, "args": [str]}}}}; empty when the
    file does not exist."""
    path = Path(path)
    if not path.is_file():
        return OrderedDict()
    try:
        with open(str(path), encoding="utf-8-sig") as fh:
            data = json.load(fh, object_pairs_hook=OrderedDict)
    except ValueError as exc:
        raise TranscodeError("{}: {}".format(path, exc))
    raw = data.get("super_presets") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise TranscodeError('{}: no "super_presets" object.'.format(path))
    out = OrderedDict()
    for name, entry in raw.items():
        where = '{}: super-preset "{}"'.format(path, name)
        if not isinstance(entry, dict) or not isinstance(entry.get("areas", {}), dict):
            raise TranscodeError("{} must be an object with \"areas\".".format(where))
        areas = OrderedDict()
        for area, value in entry.get("areas", {}).items():
            if area not in AREAS + LEGACY_AREAS:
                raise TranscodeError("{}: unknown area {}; use {}.".format(where, area, ", ".join(AREAS)))
            if not isinstance(value, dict):
                raise TranscodeError("{}: area {} must be an object.".format(where, area))
            args = value.get("args", [])
            if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
                raise TranscodeError("{}: {} args must be a list of text.".format(where, area))
            areas[area] = OrderedDict([("state", value.get("state") or {}), ("args", list(args))])
        out[name] = OrderedDict([("notes", str(entry.get("notes", ""))), ("areas", areas)])
    return out


def dumps(presets):
    data = OrderedDict([("_notes", NOTES), ("super_presets", presets)])
    return json.dumps(data, indent=2) + "\n"


def save(path, presets):
    """Write all super-presets, multi-user safe (see safe_save): a temporary file first, the
    previous version kept as super_presets_safecopyNNN.json. Returns a safe_save.SaveResult."""
    return safe_save.safe_write_text(path, dumps(presets))


UPDATE_ATTEMPTS = 5


def update(path, change):
    """Read the file, apply change(presets) (it edits the dict in place), save. If someone
    else saved in between, read again and redo the change, so their super-presets are kept.
    Returns a safe_save.SaveResult."""
    for attempt in range(UPDATE_ATTEMPTS):
        stamp = safe_save.file_stamp(path)
        presets = load(path)
        change(presets)
        try:
            return safe_save.safe_write_text(path, dumps(presets), expect_stamp=True, stamp=stamp)
        except safe_save.SaveConflict:
            if attempt == UPDATE_ATTEMPTS - 1:
                raise


def args_for(presets, names, path="super_presets.json"):
    """The vfx-transcode options of one or more super-presets (comma separated), in order."""
    out = []
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        if name not in presets:
            raise TranscodeError('Unknown super-preset "{}". Available: {} (from {})'.format(
                name, ", ".join(presets) or "none", path))
        for area in AREAS + LEGACY_AREAS:
            entry = presets[name]["areas"].get(area)
            if entry:
                out += entry["args"]
    return out


def expand_argv(argv, presets_path):
    """argv with -super_preset NAME replaced by the super-preset's options, put in front so
    the command line's own options win. -super_presets_file PATH picks another file."""
    argv = list(argv)
    names, file_arg, rest = [], None, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-super_preset", "-super_presets_file") and i + 1 < len(argv):
            if arg == "-super_preset":
                names.append(argv[i + 1])
            else:
                file_arg = argv[i + 1]
            i += 2
            continue
        rest.append(arg)
        i += 1
    if not names:
        return rest if file_arg else argv
    if file_arg:
        path = Path(file_arg)
        presets = load(path)
    else:
        paths = search_paths(presets_path)
        presets = load_merged(paths)[0]
        path = " + ".join(str(p) for p in paths)
    extra = []
    for name in names:
        extra += args_for(presets, name, path)
    return extra + rest
