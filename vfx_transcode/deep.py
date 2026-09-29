"""Deep EXR support.

Deep images must be flattened before oiiotool can colour-convert or resize them.
Deep files with colour channels are flattened, then converted as usual. Deep files
without colour (only alpha and Z, e.g. a Karma deep holdout with A, RA, GA, BA, Z, ZBack)
are shown as a depth picture instead:

    flatten -> Z remapped from [near, far] to [1, 0] (near is bright) -> times A

No OCIO conversion is applied to the depth picture; it is data, not colour.
"""

import logging
import math
import re
from dataclasses import dataclass

from .errors import TranscodeError
from .tools import run_tool

log = logging.getLogger(__name__)

COLOUR_REASONS = ("preferred", "requested", "requested (not found)")
_STAT_RE = re.compile(r"Stats (Min|Max):\s*([^(\r\n]+)")


@dataclass
class DepthView:
    subimage: int
    near: float
    far: float
    alpha: bool     # multiply by the flattened A channel (black background)


def is_deep(subimage_info):
    names = subimage_info.get("channelnames") or []
    return bool(subimage_info.get("deep")) or "ZBack" in names


def depth_subimage(info, preferred=0):
    """Index of a subimage with a Z channel, preferring `preferred`."""
    order = [preferred] + [i for i in range(len(info)) if i != preferred]
    for index in order:
        if index < len(info) and "Z" in (info[index].get("channelnames") or []):
            return index
    return None


def parse_printstats(text):
    """{'Min': [floats], 'Max': [floats]} from oiiotool --printstats output."""
    stats = {}
    for key, values in _STAT_RE.findall(text):
        parsed = []
        for token in values.split():
            try:
                parsed.append(float(token))
            except ValueError:
                pass
        stats.setdefault(key, parsed)
    return stats


def parse_zrange(text):
    try:
        near, far = (float(v) for v in text.replace(" ", "").split(","))
    except ValueError:
        raise TranscodeError('-deep_zrange must be "near,far", e.g. 10,5000 (got "{}").'.format(text))
    if not far > near:
        raise TranscodeError("-deep_zrange: far must be larger than near.")
    return near, far


def _stat(cfg, cmd, key):
    result = run_tool(cmd, "Measuring depth range", cfg.verbose)
    stats = parse_printstats(result.stdout + "\n" + result.stderr)
    try:
        return stats[key][0]
    except (KeyError, IndexError):
        raise TranscodeError("Could not read the depth range from oiiotool; "
                             "set it with -deep_zrange near,far.")


def measure_depth(cfg, path, subimage, alpha=True):
    """Z range of the flattened first frame, used for the whole sequence (no flicker).

    Empty pixels carry a huge Z (Karma writes 1e30), so the plain Z maximum is useless:
    near = min(Z) (empty pixels are far, so they don't affect it), far = max(Z * A)
    (empty pixels have A = 0)."""
    base = [cfg.oiiotool, "-i", path]
    if subimage:
        base += ["--subimage", str(subimage)]
    base += ["--flatten"]
    near = _stat(cfg, base + ["--ch", "Z", "--printstats"], "Min")
    if alpha:
        far = _stat(cfg, base + ["--dup", "--ch", "A", "--swap", "--ch", "Z", "--mul",
                                 "--printstats"], "Max")
    else:
        far = _stat(cfg, base + ["--ch", "Z", "--printstats"], "Max")
    if not (math.isfinite(near) and math.isfinite(far)) or far <= near:
        raise TranscodeError("Depth range {} to {} is not usable; set it with "
                             "-deep_zrange near,far.".format(near, far))
    return near, far


def depth_args(view):
    """oiiotool arguments turning the deep image into an RGB depth picture."""
    args = []
    if view.subimage:
        args += ["--subimage", str(view.subimage)]
    args += ["--flatten"]
    if view.alpha:
        # keep a copy of alpha (as RGB) below the image we work on
        args += ["--dup", "--ch", "R=A,G=A,B=A", "--swap"]
    args += ["--ch", "R=Z,G=Z,B=Z",
             "--contrast:black={:.6g}:white={:.6g}".format(view.near, view.far),
             "--clamp:min=0:max=1",
             "--invert"]
    if view.alpha:
        args += ["--mul"]
    return args


def plan_depth_view(cfg, info, pick):
    """Return a DepthView when the picked image is deep and has no colour, else None.
    Deep images with colour get pick.deep = True (flatten, then the normal path)."""
    if pick.subimage >= len(info) or not is_deep(info[pick.subimage]):
        return None
    if pick.reason in COLOUR_REASONS:
        pick.deep = True
        log.info("Deep image: flattening before colour conversion")
        return None

    subimage = depth_subimage(info, pick.subimage)
    if subimage is None:
        raise TranscodeError("Deep image has neither colour nor Z channels; "
                             "pick channels with -oiio_channel.")
    names = info[subimage].get("channelnames") or []
    alpha = "A" in names
    if cfg.deep_zrange:
        near, far = parse_zrange(cfg.deep_zrange)
        source = "-deep_zrange"
    else:
        near, far = measure_depth(cfg, cfg.header_file or cfg.input, subimage, alpha)
        source = "first frame; override with -deep_zrange near,far"
    view = DepthView(subimage, near, far, alpha)
    log.info("Deep image without colour channels: showing depth Z, near %g far %g (%s)",
             near, far, source)
    return view
