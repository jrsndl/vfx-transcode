"""Keywords parsed from the sequence name: version and underscore-separated parts.

    clean name "AA_BBB_C_DD":
      {namesplit1} AA   {namesplit2} BBB   {namesplit3} C   {namesplit4} DD   {namesplit5}..9 ""
      {namesplit-1} DD  {namesplit-2} C    {namesplit-3} BBB {namesplit-4} AA {namesplit-5}..-9 ""

    version (default regex "_v(\\d\\d\\d)"), name "sh010_comp_v007":
      {version_string} 007   {version_number} 7

Custom name tokens: a regex run over a source text (default the clean name) gives a new
keyword. "name_tokens" in presets.json, or -name_token NAME=REGEX:

    "shot": "^([A-Za-z0-9]+_\\d+)"                          -> {shot}
    "episode": {"regex": "_(ep\\d+)_", "source": "{meta:ImageDescription|}", "default": "ep00"}

The value is group 1 (the whole match if the regex has no group) of the first match; the
default ("" unless given) if nothing matches.
"""

import logging
import ntpath
import os
import posixpath
import re
from collections import OrderedDict
from dataclasses import dataclass

from .errors import TranscodeError

log = logging.getLogger(__name__)

DEFAULT_VERSION_REGEX = r"_v(\d\d\d)"
MAX_SPLITS = 9
SPLIT_KEYS = tuple("namesplit{}".format(i) for i in range(1, MAX_SPLITS + 1)) + \
    tuple("namesplit-{}".format(i) for i in range(1, MAX_SPLITS + 1))
# path keywords: the source's folders, the source path and its parents, the scanned base folder
MAX_PATH_SPLITS = 20
FOLDER_KEYS = tuple("sourcefolder{}".format(i) for i in range(1, MAX_PATH_SPLITS + 1)) + \
    tuple("sourcefolder-{}".format(i) for i in range(1, MAX_PATH_SPLITS + 1))
SOURCEPATH_KEYS = ("sourcepath",) + tuple("sourcepath-{}".format(i) for i in range(1, MAX_PATH_SPLITS + 1))
BASE_FOLDER_KEYS = ("base_folder",) + tuple("base_folder-{}".format(i) for i in range(1, MAX_PATH_SPLITS + 1))
PATH_KEYWORDS = FOLDER_KEYS + SOURCEPATH_KEYS + BASE_FOLDER_KEYS
# values the user types in vfx-transcode-process (-custom_destination_path / _name), for
# destinations saved in super-presets: "{custom_destination_path}/{sourcefolder-3}/"
CUSTOM_KEYWORDS = ("custom_destination_path", "custom_destination_name")
# the frame rate in use (-r, the EXR header, the movie, else 24): set once it is known
FPS_KEYWORDS = ("source_fps_float", "source_fps_int", "source_fps_frac")
# the EXR's layers (Multichannel): set once the header is read, empty for movies
LAYER_KEYWORDS = ("multi_layers_line", "multi_layers_column",
                  "multi_layerschannels_line", "multi_layerschannels_column")
NAME_KEYWORDS = ("version_string", "version_number") + SPLIT_KEYS + PATH_KEYWORDS + CUSTOM_KEYWORDS \
    + FPS_KEYWORDS + LAYER_KEYWORDS


def compile_version_regex(pattern, where="-version_regex"):
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise TranscodeError("{} is not a valid regular expression: {}".format(where, exc))


def parse_version(name, pattern=DEFAULT_VERSION_REGEX):
    """(version_string, version_number): group 1 of the regex (the whole match if it has no
    group), and the same without leading zeroes ("007" -> "7", "000" -> "0"). ("", "") if
    the name has no version. The last match wins: "v001_plate_v003" gives 003."""
    regex = compile_version_regex(pattern) if isinstance(pattern, str) else pattern
    matches = list(regex.finditer(name))
    if not matches:
        return "", ""
    match = matches[-1]
    text = match.group(1) if regex.groups else match.group(0)
    text = text or ""
    number = text.lstrip("0") or ("0" if text else "")
    return text, number


def name_splits(name):
    parts = name.split("_") if name else []
    out = {}
    for i in range(1, MAX_SPLITS + 1):
        out["namesplit{}".format(i)] = parts[i - 1] if i <= len(parts) else ""
        out["namesplit-{}".format(i)] = parts[-i] if i <= len(parts) else ""
    return out


def _flavour(path):
    """ntpath for Windows paths (on Windows, or any path with a drive letter or \\\\server),
    else posixpath."""
    if os.name == "nt" or re.match(r"^([A-Za-z]:|[\\/]{2}[^\\/])", path or ""):
        return ntpath
    return posixpath


def split_folder(folder):
    """A folder's parts, from the top: "D:\\shots\\sh010" -> ["D:", "shots", "sh010"];
    "\\\\server\\share\\sh010" -> ["\\\\server", "share", "sh010"]; "/mnt/shots" -> ["mnt", "shots"]."""
    if not folder:
        return []
    flavour = _flavour(folder)
    if flavour is ntpath:
        drive, rest = ntpath.splitdrive(folder)
        if drive.startswith(("\\\\", "//")):                  # UNC: \\server\share
            server, _sep, share = drive[2:].replace("/", "\\").partition("\\")
            head = ["\\\\" + server] + ([share] if share else [])
        else:
            head = [drive] if drive else []
        parts = [p for p in re.split(r"[\\/]+", rest) if p]
        return head + parts
    return [p for p in folder.split("/") if p]


def _strip(path, flavour):
    """No trailing separator, except on a root: "D:\\", "/", "\\\\server\\share\\"."""
    drive, rest = flavour.splitdrive(path)
    if rest.strip("\\/") == "":
        if flavour is ntpath and drive and not drive.startswith(("\\\\", "//")):
            return drive + "\\"
        if drive.startswith(("\\\\", "//")):
            return drive                          # \\server\share
        return drive + rest[:1] if (rest or not drive) else drive
    return path.rstrip("\\/")


def parents(path, count=MAX_PATH_SPLITS):
    """[path, its folder, the folder above ...]: count entries after path, "" past the top
    (the top is "D:\\", "\\\\server\\share" or "/")."""
    flavour = _flavour(path)
    out, current = [path], _strip(path, flavour) if path else ""
    for _i in range(count):
        parent = _strip(flavour.dirname(current), flavour) if current else ""
        if parent == current:
            parent = ""
        out.append(parent)
        current = parent
    return out


def path_tokens(source_path, base_folder=""):
    """{sourcefolder1..20} {sourcefolder-1..-20}: the source's folders from the top (a drive
    letter "D:", or "\\\\server", first) and from the end; {sourcepath}: the folder the source
    is in; {sourcepath-1}: the folder above it, {sourcepath-2} the one above that ...;
    {base_folder} the folder a scan started from (-base_folder, default the source's folder)
    and {base_folder-1}, -2 ... above it. Past the top, a keyword is empty."""
    source_path = source_path or ""
    flavour = _flavour(source_path)
    folder = flavour.dirname(source_path) if source_path else ""
    parts = split_folder(folder)
    out = {}
    for i in range(1, MAX_PATH_SPLITS + 1):
        out["sourcefolder{}".format(i)] = parts[i - 1] if i <= len(parts) else ""
        out["sourcefolder-{}".format(i)] = parts[-i] if i <= len(parts) else ""
    for key, value in zip(SOURCEPATH_KEYS, parents(_strip(folder, flavour) if folder else "")):
        out[key] = value
    base = base_folder or folder
    base = _strip(base, _flavour(base)) if base else ""
    for key, value in zip(BASE_FOLDER_KEYS, parents(base)):
        out[key] = value
    return out


def name_tokens(name, version_regex=DEFAULT_VERSION_REGEX):
    version_string, version_number = parse_version(name, version_regex)
    tokens = {"version_string": version_string, "version_number": version_number}
    tokens.update(name_splits(name))
    return tokens


# --- custom name tokens ---

_TOKEN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_KEYWORDS = {"clean_name", "frame_start", "frame_end", "frames", "timecode", "timecode_end",
                     "duration", "fps", "preset", "date", "user", "input", "meta", "ext", "reel",
                     "colour", "width", "height", "par", "tc", "current_frame", "frame",
                     "slate_length", "burnins"} | set(NAME_KEYWORDS)
DEFAULT_SOURCE = "{clean_name}"


@dataclass(frozen=True)
class NameRule:
    name: str
    regex: str
    source: str = DEFAULT_SOURCE      # keywords allowed, e.g. "{meta:Copyright|}"
    default: str = ""
    notes: str = ""

    def compiled(self):
        return compile_version_regex(self.regex, 'name token "{}"'.format(self.name))


def check_token_name(name, where):
    if not _TOKEN_NAME.match(name or ""):
        raise TranscodeError('{}: "{}" is not a usable keyword name (letters, digits, _; not '
                             "starting with a digit).".format(where, name))
    if name in RESERVED_KEYWORDS or name.startswith(("namesplit", "sourcefolder", "sourcepath", "base_folder",
                                                     "custom_destination", "source_fps")):
        raise TranscodeError('{}: "{}" is a built-in keyword; pick another name.'.format(where, name))


def rule_from_json(name, data, where):
    if isinstance(data, str):
        data = {"regex": data}
    if not isinstance(data, dict) or not isinstance(data.get("regex"), str) or not data["regex"]:
        raise TranscodeError('{}: name token "{}" needs a "regex" (a string, or an object with '
                             '"regex", optional "source" and "default").'.format(where, name))
    check_token_name(name, where)
    unknown = set(data) - {"regex", "source", "default", "notes"}
    if unknown:
        raise TranscodeError('{}: name token "{}": unknown key(s) {}.'.format(
            where, name, ", ".join(sorted(unknown))))
    rule = NameRule(name, data["regex"], str(data.get("source") or DEFAULT_SOURCE),
                    str(data.get("default", "")), str(data.get("notes", "")))
    rule.compiled()
    return rule


def rules_from_json(data, where):
    if not isinstance(data, dict):
        raise TranscodeError('{}: "name_tokens" must be an object {{"name": "regex", ...}}.'.format(where))
    return OrderedDict((name, rule_from_json(name, value, where)) for name, value in data.items())


def rule_from_arg(text):
    """-name_token NAME=REGEX (on the clean name). NAME= removes a presets.json token."""
    name, sep, regex = (text or "").partition("=")
    name = name.strip()
    if not sep:
        raise TranscodeError('-name_token expects NAME=REGEX, got "{}".'.format(text))
    check_token_name(name, "-name_token")
    if not regex:
        return name, None
    rule = NameRule(name, regex)
    rule.compiled()
    return name, rule


def merge_rules(preset_rules, args):
    rules = OrderedDict(preset_rules or {})
    for text in args or []:
        name, rule = rule_from_arg(text)
        if rule is None:
            rules.pop(name, None)
        else:
            rules[name] = rule
    return rules


def apply_rule(rule, context, expand):
    """The token's value: group 1 (or the whole match) of the first match in the expanded
    source, else the default."""
    source = expand(rule.source, context)
    match = rule.compiled().search(source)
    if not match:
        return rule.default, False
    return ((match.group(1) if rule.compiled().groups else match.group(0)) or ""), True


def custom_tokens(rules, context, expand):
    """{name: value} for all rules, in order; a rule may use tokens made by earlier ones."""
    context = dict(context)
    out = OrderedDict()
    for name, rule in rules.items():
        value, found = apply_rule(rule, context, expand)
        if found:
            log.info("Name token {%s} = %r", name, value)
        else:
            log.info("Name token {%s}: %r does not match %r; using %r", name, rule.regex,
                     expand(rule.source, context), value)
        out[name] = context[name] = value
    return out
