"""Which presets file to use when -presets_file is not given.

vfx-transcode.ini next to vfx-transcode.py / vfx-transcode.exe may point to the studio
preferences, typically on a network share:

    [vfx-transcode]
    studio_prefs = \\\\server\\pipeline\\vfx-transcode

studio_prefs is a folder holding presets.json, or the presets file itself. Environment variables
(%PIPELINE_ROOT%) work, and ./ is the application folder. If the ini is missing, the path is
empty or does not exist, or the studio presets fail to load, the presets.json next to the
application is used, as before. super_presets.json is always looked for next to whichever
presets file is used.
"""

import configparser
import os
from dataclasses import dataclass, field
from typing import List, Optional

from .errors import TranscodeError
from .presets import DEFAULT_PRESETS_FILE, load_presets
from .tools import app_dir, expand_path, resolve_path

INI_NAME = "vfx-transcode.ini"
SECTION = "vfx-transcode"
KEY = "studio_prefs"
PRESETS_NAME = "presets.json"

INI_TEMPLATE = """\
; vfx-transcode.ini - read by vfx-transcode and vfx-transcode-tuner from the folder they are in.
;
; studio_prefs: the studio preferences, usually on a network share: a folder with presets.json
; (and super_presets.json), or the path of the presets file itself. Environment variables work
; (%PIPELINE_ROOT%\\vfx-transcode), and ./ is this folder.
; If it is empty, missing, or its presets.json fails to load, the presets.json next to the
; application is used. -presets_file on the command line always wins.

[vfx-transcode]
studio_prefs =
"""


@dataclass
class PresetsChoice:
    path: str                         # the presets file to load
    source: str                       # where it came from, for the log
    problems: List[str] = field(default_factory=list)   # why the studio prefs were not used
    presets: Optional[object] = None  # the studio presets, already loaded


def ini_path(base=None):
    return os.path.join(base or app_dir(), INI_NAME)


def studio_path(value, base=None):
    """The presets file a studio_prefs value names: a folder -> its presets.json."""
    path = resolve_path(expand_path(value.strip().strip('"')), base or app_dir())
    if os.path.isdir(path):
        path = os.path.join(path, PRESETS_NAME)
    return path


def read_ini(path):
    """studio_prefs from the ini, "" when the key is empty. Raises TranscodeError if the file
    can't be read."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            parser.read_file(fh)
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        raise TranscodeError("{} can't be read: {}".format(path, exc))
    if not parser.has_section(SECTION):
        raise TranscodeError("{} has no [{}] section".format(path, SECTION))
    return parser.get(SECTION, KEY, fallback="").strip()


def choose_presets(base=None):
    """The presets file to use: the studio preferences named in vfx-transcode.ini if they load,
    else the presets.json next to the application."""
    base = base or app_dir()
    local = PresetsChoice(resolve_path(DEFAULT_PRESETS_FILE, base), "next to the application")
    ini = ini_path(base)
    if not os.path.isfile(ini):
        return local
    try:
        value = read_ini(ini)
    except TranscodeError as exc:
        local.problems.append(str(exc))
        return local
    if not value:
        local.source = "next to the application ({} sets no {})".format(INI_NAME, KEY)
        return local
    path = studio_path(value, base)
    if not os.path.isfile(path):
        local.problems.append('"{}" not found (studio_prefs in {})'.format(path, INI_NAME))
        return local
    try:
        presets = load_presets(path)
    except TranscodeError as exc:
        local.problems.append("failed to load: {}".format(exc))
        return local
    return PresetsChoice(path, "studio preferences from " + INI_NAME, presets=presets)


def describe(choice):
    """One line for the log / the tuner, plus the problems."""
    text = "Presets: {} ({})".format(choice.path, choice.source)
    if choice.problems:
        text += "; " + "; ".join(choice.problems) + ", so the local presets are used"
    return text
