"""Smart tokens: per-project keyword rules, from presets.json only.

A smart token preset is a list of definitions processed top to bottom:

    "smart_tokens": {
      "mygreatproject": {
        "notes": "...",
        "tokens": [
          {"name": "shot", "source": "{clean_name}", "regex": "^(\\w+?)_(\\d+)_(\\d+)",
           "repl": "\\1_\\2_\\3"},
          {"name": "shot", "conditions": [{"left": "{namesplit1}", "op": "==", "right": "plate"}],
           "source": "{namesplit2}", "case": "upper"}
        ]
      }
    }

Each definition:
  name        keyword it sets ({shot}); may appear several times (later ones win when applied)
  conditions  optional, like oiio_transforms: all must hold, else the definition is skipped
  source      text to work on, any keywords (default "{clean_name}"); earlier smart tokens too
  regex       optional; without one the whole source is the value
  repl        what the value becomes. extract mode (default): match.expand(repl) of the first
              match; empty = "\\1" (or the whole match if the regex has no group). No match =
              the definition is skipped. replace mode: re.sub(regex, repl, source), all matches.
              Keywords are expanded in repl ("{project}_\\1"); the regex is used as typed.
  mode        extract (default) | replace
  case        as_is (default) | lower | upper | title

A skipped definition leaves the token as it was: undefined, or the value of an earlier one.
"""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Tuple

from .errors import TranscodeError
from .names import check_token_name
from .transforms import Condition, _condition, evaluate

log = logging.getLogger(__name__)

NO_SMART_TOKENS = "none"
CASES = ("as_is", "lower", "upper", "title")
MODES = ("extract", "replace")
DEFAULT_SOURCE = "{clean_name}"
FIELDS = {"name", "conditions", "source", "regex", "repl", "mode", "case", "notes", "description"}


@dataclass(frozen=True)
class SmartToken:
    name: str
    conditions: Tuple[Condition, ...] = ()
    source: str = DEFAULT_SOURCE
    regex: str = ""
    repl: str = ""
    mode: str = "extract"
    case: str = "as_is"
    notes: str = ""
    where: str = ""

    def compiled(self):
        return re.compile(self.regex) if self.regex else None


@dataclass(frozen=True)
class SmartTokenPreset:
    name: str
    tokens: Tuple[SmartToken, ...] = ()
    notes: str = ""

    def names(self):
        return list(OrderedDict.fromkeys(t.name for t in self.tokens))


# --- presets.json ---

def _case(value, where):
    text = str(value or "as_is").strip().lower().replace(" ", "_").replace("-", "_")
    text = {"asis": "as_is", "lowercase": "lower", "lower_case": "lower", "uppercase": "upper",
            "upper_case": "upper", "titlecase": "title", "title_case": "title"}.get(text, text)
    if text not in CASES:
        raise TranscodeError('{}: case must be one of {}, got "{}".'.format(
            where, " | ".join(CASES), value))
    return text


def _token(data, where):
    if not isinstance(data, dict):
        raise TranscodeError("{} must be an object.".format(where))
    unknown = sorted(set(data) - FIELDS)
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
            where, ", ".join(unknown), ", ".join(sorted(FIELDS - {"description"}))))
    name = str(data.get("name") or "").strip()
    check_token_name(name, where)
    where = '{} "{}"'.format(where, name)
    conditions = data.get("conditions") or []
    if not isinstance(conditions, list):
        raise TranscodeError("{}: conditions must be a list.".format(where))
    mode = str(data.get("mode") or "extract").strip().lower()
    if mode not in MODES:
        raise TranscodeError('{}: mode must be extract or replace, got "{}".'.format(where, mode))
    regex = str(data.get("regex") or "")
    try:
        compiled = re.compile(regex) if regex else None
    except re.error as exc:
        raise TranscodeError("{}: regex is not valid: {}".format(where, exc))
    if mode == "replace" and not regex:
        raise TranscodeError("{}: mode replace needs a regex.".format(where))
    repl = str(data.get("repl") or "")
    if compiled is not None and mode == "extract":
        _check_repl(compiled, repl, where)
    return SmartToken(
        name=name,
        conditions=tuple(_condition(c, "{}: condition {}".format(where, i))
                         for i, c in enumerate(conditions, 1)),
        source=str(data["source"]) if data.get("source") is not None else DEFAULT_SOURCE,
        regex=regex, repl=repl, mode=mode, case=_case(data.get("case"), where),
        notes=str(data.get("notes") or data.get("description") or ""), where=where)


def _check_repl(compiled, repl, where):
    """Group references in repl must exist: "\\3" with two groups fails early, not mid-run."""
    if not repl:
        return                        # "\\1", or the whole match when there is no group
    refs = [int(a or b) for a, b in re.findall(r"\\(\d+)|\\g<(\d+)>", repl)]
    too_high = [r for r in refs if r > compiled.groups]
    if too_high:
        raise TranscodeError("{}: repl refers to group {} but the regex has {} group(s).".format(
            where, too_high[0], compiled.groups))


def parse_smart_token_presets(data, path):
    """{"name": {"notes": ..., "tokens": [...]}} -> OrderedDict name -> SmartTokenPreset.
    A bare list is accepted for a preset: {"name": [...]}."""
    if not isinstance(data, dict):
        raise TranscodeError('{}: "smart_tokens" must be an object {{"preset name": {{"tokens": '
                             '[...]}}}}.'.format(path))
    presets = OrderedDict()
    for name, item in data.items():
        where = '{}: smart tokens "{}"'.format(path, name)
        if name == NO_SMART_TOKENS:
            raise TranscodeError('{}: the name "none" is reserved.'.format(where))
        if isinstance(item, list):
            item = {"tokens": item}
        if not isinstance(item, dict):
            raise TranscodeError("{} must be an object with a tokens list.".format(where))
        unknown = sorted(set(item) - {"notes", "description", "tokens"})
        if unknown:
            raise TranscodeError("{}: unknown field(s) {}; use notes, tokens.".format(
                where, ", ".join(unknown)))
        tokens = item.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise TranscodeError("{}: tokens must be a non-empty list.".format(where))
        presets[name] = SmartTokenPreset(
            name=name, notes=str(item.get("notes") or item.get("description") or ""),
            tokens=tuple(_token(t, "{}: token {}".format(where, i)) for i, t in enumerate(tokens, 1)))
    return presets


# --- evaluation ---

def change_case(text, case):
    if case == "lower":
        return text.lower()
    if case == "upper":
        return text.upper()
    if case == "title":
        return text.title()
    return text


def apply_token(token, context, expand):
    """(value, why): value None when the definition is skipped (a condition fails, or the
    regex does not match in extract mode)."""
    for condition in token.conditions:
        ok, text = evaluate(condition, context, expand)
        if not ok:
            return None, "condition {} is false".format(text)
    source = expand(token.source, context)
    regex = token.compiled()
    repl = expand(token.repl, context) if token.repl else ""
    try:
        if regex is None:
            value = source
        elif token.mode == "replace":
            value = regex.sub(repl, source)
        else:
            match = regex.search(source)
            if not match:
                return None, "{!r} does not match {!r}".format(token.regex, source)
            if not repl:
                repl = "\\1" if regex.groups else "\\g<0>"
            value = match.expand(repl)
    except (re.error, IndexError) as exc:
        raise TranscodeError("{}: repl {!r}: {}".format(token.where, token.repl, exc))
    return change_case(value, token.case), "source {!r}".format(source)


def evaluate_preset(preset, context, expand):
    """{name: value} of the tokens that got a value, processed top to bottom; each applied
    definition is visible to the ones below it."""
    context = dict(context)
    out = OrderedDict()
    for index, token in enumerate(preset.tokens, 1):
        value, why = apply_token(token, context, expand)
        if value is None:
            log.debug('Smart tokens "%s" #%d {%s}: skipped, %s', preset.name, index, token.name, why)
            continue
        log.debug('Smart tokens "%s" #%d {%s} = %r (%s)', preset.name, index, token.name, value, why)
        out[token.name] = context[token.name] = value
    for name, value in out.items():
        log.info("Smart token {%s} = %r", name, value)
    missing = [n for n in preset.names() if n not in context]
    for name in missing:
        log.warning('Smart tokens "%s": no definition of {%s} applied; it stays as typed.',
                    preset.name, name)
    return out
