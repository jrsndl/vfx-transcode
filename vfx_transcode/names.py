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
import re
from collections import OrderedDict
from dataclasses import dataclass

from .errors import TranscodeError

log = logging.getLogger(__name__)

DEFAULT_VERSION_REGEX = r"_v(\d\d\d)"
MAX_SPLITS = 9
SPLIT_KEYS = tuple("namesplit{}".format(i) for i in range(1, MAX_SPLITS + 1)) + \
    tuple("namesplit-{}".format(i) for i in range(1, MAX_SPLITS + 1))
NAME_KEYWORDS = ("version_string", "version_number") + SPLIT_KEYS


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
    if name in RESERVED_KEYWORDS or name.startswith("namesplit"):
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
