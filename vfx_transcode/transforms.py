"""oiiotool transform presets: resize / crop / reformat chosen per input.

A preset has sub-presets, checked top to bottom; the first whose conditions all hold is used
(no conditions = always matches, a catch-all at the end). A condition compares two strings
after keyword expansion:

    {"left": "{input_width}x{input_height}", "op": "==", "right": "3840x2160"}

The chosen sub-preset's "transforms" are oiiotool arguments; they replace the -width resize
and run on the scene-linear image, before the colour transform.
"""

import logging
import string
from collections import OrderedDict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

from .errors import TranscodeError
from .tools import split_args

log = logging.getLogger(__name__)

NO_TRANSFORM = "none"
OPERATORS = ("==", "!=")
INPUT_KEYWORDS = ("input_width", "input_height", "input_par", "input_data_width",
                  "input_data_height")


@dataclass(frozen=True)
class Condition:
    left: str
    op: str
    right: str

    def describe(self):
        return "{} {} {}".format(self.left, self.op, self.right)


@dataclass(frozen=True)
class SubPreset:
    name: str
    conditions: Tuple[Condition, ...] = ()
    transforms: Tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class TransformPreset:
    name: str
    subs: Tuple[SubPreset, ...] = ()
    description: str = ""


# --- presets.json ---

def _text(value):
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _condition(data, where):
    if not isinstance(data, dict):
        raise TranscodeError('{} must be an object like {{"left": "{{input_width}}", "op": "==", '
                             '"right": "3840"}}.'.format(where))
    unknown = sorted(set(data) - {"left", "op", "right"})
    missing = [k for k in ("left", "op", "right") if k not in data]
    if unknown or missing:
        raise TranscodeError("{}: needs left, op and right{}.".format(
            where, " (unknown: {})".format(", ".join(unknown)) if unknown else ""))
    op = str(data["op"]).strip()
    if op not in OPERATORS:
        raise TranscodeError('{}: op must be == or !=, got "{}".'.format(where, data["op"]))
    return Condition(str(data["left"]), op, str(data["right"]))


def _sub(data, where):
    if not isinstance(data, dict):
        raise TranscodeError("{} must be an object.".format(where))
    unknown = sorted(set(data) - {"name", "description", "notes", "conditions", "transforms"})
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use name, description, conditions, "
                             "transforms.".format(where, ", ".join(unknown)))
    name = str(data.get("name") or "").strip()
    where = '{} "{}"'.format(where, name) if name else where
    conditions = data.get("conditions") or []
    if not isinstance(conditions, list):
        raise TranscodeError("{}: conditions must be a list.".format(where))
    transforms = data.get("transforms", [])
    if isinstance(transforms, str):
        transforms = [transforms]
    if not isinstance(transforms, list) or not all(isinstance(t, str) for t in transforms):
        raise TranscodeError('{}: transforms must be a list of oiiotool arguments, e.g. '
                             '["--resize 1920x1080"].'.format(where))
    return SubPreset(name=name or "(unnamed)",
                     conditions=tuple(_condition(c, "{}: condition {}".format(where, i))
                                      for i, c in enumerate(conditions, 1)),
                     transforms=tuple(t for t in transforms if t.strip()),
                     description=_text(data.get("description") or data.get("notes")))


def parse_transform_presets(data, path):
    """The "oiio_transforms" list (or {name: {...}} object) -> OrderedDict name -> preset."""
    if isinstance(data, dict):
        data = [dict(value, name=value.get("name", key)) if isinstance(value, dict) else value
                for key, value in data.items()]
    if not isinstance(data, list):
        raise TranscodeError('{}: "oiio_transforms" must be a list of presets.'.format(path))
    presets = OrderedDict()
    for index, item in enumerate(data, 1):
        where = '{}: oiio_transforms entry {}'.format(path, index)
        if not isinstance(item, dict):
            raise TranscodeError("{} must be an object.".format(where))
        unknown = sorted(set(item) - {"name", "description", "notes", "subs"})
        if unknown:
            raise TranscodeError("{}: unknown field(s) {}; use name, description, subs."
                                 .format(where, ", ".join(unknown)))
        name = str(item.get("name") or "").strip()
        if not name:
            raise TranscodeError("{} has no name.".format(where))
        if name == NO_TRANSFORM:
            raise TranscodeError('{}: "{}" is reserved (it switches transforms off).'.format(where, name))
        if name in presets:
            raise TranscodeError('{}: oiio transform "{}" is defined twice.'.format(path, name))
        where = '{}: oiio transform "{}"'.format(path, name)
        subs = item.get("subs") or []
        if not isinstance(subs, list) or not subs:
            raise TranscodeError("{}: subs must be a non-empty list.".format(where))
        presets[name] = TransformPreset(
            name=name, description=_text(item.get("description") or item.get("notes")),
            subs=tuple(_sub(s, "{}: sub {}".format(where, i)) for i, s in enumerate(subs, 1)))
    return presets


# --- evaluation ---

def number_text(value):
    """1920 -> "1920", 2.0 -> "2", 4/3 -> "1.3333"."""
    value = Fraction(value).limit_denominator(100000) if not isinstance(value, Fraction) else value
    if value.denominator == 1:
        return str(value.numerator)
    return "{:.4f}".format(float(value)).rstrip("0").rstrip(".")


def input_tokens(info, par):
    """{input_width} {input_height} (display window), {input_data_width} {input_data_height}
    (data window) of the first frame, and {input_par}."""
    spec = info[0] if info else {}
    data_w, data_h = spec.get("width"), spec.get("height")
    full_w = spec.get("full_width") or data_w
    full_h = spec.get("full_height") or data_h
    return {"input_width": "" if full_w is None else str(full_w),
            "input_height": "" if full_h is None else str(full_h),
            "input_data_width": "" if data_w is None else str(data_w),
            "input_data_height": "" if data_h is None else str(data_h),
            "input_par": number_text(par)}


def _unknown_keywords(text, context):
    try:
        fields = [f for _l, f, _s, _c in string.Formatter().parse(text) if f is not None]
    except ValueError:
        return []
    return [f for f in fields if f != "meta" and f not in context]


def evaluate(condition, context, expand):
    left = expand(condition.left, context).strip()
    right = expand(condition.right, context).strip()
    result = (left == right) if condition.op == "==" else (left != right)
    return result, "{} {} {}".format(left, condition.op, right)


def select(preset, context, expand):
    """(sub-preset, [what was compared]) for the first sub whose conditions all hold,
    or (None, []) if none does."""
    reported = set()
    for sub in preset.subs:
        details = []
        matched = True
        for condition in sub.conditions:
            for side in (condition.left, condition.right):
                for name in _unknown_keywords(side, context):
                    if name not in reported:
                        reported.add(name)
                        log.warning('OIIO transform "%s": unknown keyword {%s} in a condition; '
                                    "it is compared as typed.", preset.name, name)
            ok, text = evaluate(condition, context, expand)
            details.append(text)
            if not ok:
                matched = False
                break                              # AND: the first failing condition decides
        log.debug('OIIO transform "%s": sub "%s" %s (%s)', preset.name, sub.name,
                  "matches" if matched else "does not match", "; ".join(details) or "no conditions")
        if matched:
            return sub, details
    return None, []


def transform_args(sub, context, expand):
    """oiiotool arguments of the sub-preset, with keywords expanded. Each entry may hold
    several words ("--resize 1920x1080")."""
    args = []
    for entry in sub.transforms:
        args += split_args(expand(entry, context))
    return args
