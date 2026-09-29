"""Keywords parsed from the sequence name: version and underscore-separated parts.

    clean name "AA_BBB_C_DD":
      {namesplit1} AA   {namesplit2} BBB   {namesplit3} C   {namesplit4} DD   {namesplit5}..9 ""
      {namesplit-1} DD  {namesplit-2} C    {namesplit-3} BBB {namesplit-4} AA {namesplit-5}..-9 ""

    version (default regex "_v(\\d\\d\\d)"), name "sh010_comp_v007":
      {version_string} 007   {version_number} 7
"""

import logging
import re

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
