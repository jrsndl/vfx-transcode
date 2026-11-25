"""Choosing which EXR channels become the RGB of the review movie."""

import fnmatch
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)

# Built-in default, used when presets.json has no "channel_preferences".
# Checked in order; the first set fully present in any subimage wins.
PREFERRED_CHANNELS = [
    ["rgba.R", "rgba.G", "rgba.B", "rgba.A"],
    ["rgba.R", "rgba.G", "rgba.B"],
    ["rgb.R", "rgb.G", "rgb.B", "rgb.A"],
    ["rgb.R", "rgb.G", "rgb.B"],
    ["R", "G", "B", "A"],
    ["r", "g", "b", "a"],
    ["R", "G", "B"],
    ["r", "g", "b"],
    ["beauty.R", "beauty.G", "beauty.B", "beauty.A"],
    ["beauty.R", "beauty.G", "beauty.B"],
]


@dataclass
class ChannelPick:
    subimage: int
    channels: List[str]
    reason: str
    deep: bool = False
    alpha: Optional[str] = None      # alpha channel to keep (-alpha keep), None = drop it

    @property
    def output_channels(self):
        """--ch list: RGB (mono repeated to RGB when there is alpha), plus alpha if kept."""
        colour = self.without_alpha
        if not self.alpha:
            return colour
        if len(colour) == 1:
            colour = colour * 3
        return colour + [self.alpha]

    @property
    def without_alpha(self):
        """Channels to write: alpha removed, then 3 channels (RGB) or 1 (mono)."""
        kept = [c for c in self.channels if not is_alpha(c)] or list(self.channels)
        return kept[:3] if len(kept) >= 3 else kept[:1]

    def __str__(self):
        return ",".join(self.output_channels)


def is_alpha(channel):
    return channel.rsplit(".", 1)[-1] in ("A", "a")


def layer_of(channel):
    return channel.rsplit(".", 1)[0] if "." in channel else ""


def group_layers(channels):
    """['AO.R', 'AO.G', 'R', 'G'] -> {'AO': ['AO.R', 'AO.G'], '': ['R', 'G']}"""
    layers = OrderedDict()
    for channel in channels:
        layers.setdefault(layer_of(channel), []).append(channel)
    return layers


WILDCARDS = "*?["


def has_wildcard(name):
    return any(ch in name for ch in WILDCARDS)


def _channel_of(name):
    return name.rsplit(".", 1)[-1] if "." in name else name


def match_set(preferred, names):
    """The channels of `names` a preference set stands for, or None when they are not all
    there. Names may hold wildcards (* ? [abc], case counts): "Actor*.R" matches
    "ActorHitProxyMask00.R", the first match in the image's channel order wins. When the
    set's channels share one wildcard layer ("Actor*.R", "Actor*.G", "Actor*.B"), they come
    from the same layer: the first layer that has all of them."""
    if not any(has_wildcard(p) for p in preferred):
        return list(preferred) if all(c in names for c in preferred) else None
    layers = {layer_of(p) for p in preferred}
    if len(layers) == 1 and has_wildcard(next(iter(layers))) and all("." in p for p in preferred):
        seen = []
        for name in names:
            if not fnmatch.fnmatchcase(name, preferred[0]) or layer_of(name) in seen:
                continue
            layer = layer_of(name)
            seen.append(layer)
            found = _match_each(["{}.{}".format(layer, _channel_of(p)) for p in preferred], names)
            if found is not None:
                return found
        return None
    return _match_each(preferred, names)


def _match_each(patterns, names):
    """Each pattern's first matching channel not used yet, or None if one has none."""
    found = []
    for pattern in patterns:
        hit = next((n for n in names if n not in found and fnmatch.fnmatchcase(n, pattern)), None)
        if hit is None:
            return None
        found.append(hit)
    return found


# --- layer keywords for burn-ins and the slate ---

NO_LAYER = "(no layer)"
LAYER_KEYWORDS = ("multi_layers_line", "multi_layers_column",
                  "multi_layerschannels_line", "multi_layerschannels_column")


def layer_tokens(info=None):
    """{multi_layers_line} "beauty, diffuse, (no layer)", {multi_layers_column} the same one
    per line, {multi_layerschannels_line} "beauty[R,G,B], (no layer)[A]" and
    {multi_layerschannels_column}: the layers of all the image's parts, in the order they
    come; empty without a header (movies)."""
    layers = OrderedDict()
    for sub in info or []:
        for name in sub.get("channelnames") or []:
            channels = layers.setdefault(layer_of(name) or NO_LAYER, [])
            if _channel_of(name) not in channels:
                channels.append(_channel_of(name))
    names = list(layers)
    with_channels = ["{}[{}]".format(layer, ",".join(chans)) for layer, chans in layers.items()]
    return {"multi_layers_line": ", ".join(names), "multi_layers_column": "\n".join(names),
            "multi_layerschannels_line": ", ".join(with_channels),
            "multi_layerschannels_column": "\n".join(with_channels)}


def find_alpha(pick, info):
    """The alpha channel belonging to the picked colour channels: one in the pick, else A/a
    of the same layer in the same subimage. None if there is none."""
    in_pick = [c for c in pick.channels if is_alpha(c)]
    if in_pick:
        return in_pick[0]
    names = (info[pick.subimage].get("channelnames") or []) if pick.subimage < len(info) else []
    layer = layer_of(pick.without_alpha[0]) if pick.without_alpha else ""
    for letter in ("A", "a"):
        name = "{}.{}".format(layer, letter) if layer else letter
        if name in names:
            return name
    return None


def subimage_name(sub, index):
    """A part's name: the EXR "name" attribute (OIIO: oiio:subimagename), else "".""" 
    attribs = (sub or {}).get("attribs") or {}
    return str(attribs.get("oiio:subimagename") or attribs.get("name") or "")


def parts(info):
    """[(index, name, [channels])] of the subimages (EXR parts)."""
    return [(i, subimage_name(sub, i), list(sub.get("channelnames") or [])) for i, sub in enumerate(info)]


def find_subimage(info, wanted):
    """The index of the part given by number or name (case ignored); ValueError if none."""
    text = str(wanted).strip()
    found = parts(info)
    if text.isdigit() and int(text) < len(found):
        return int(text)
    for index, name, _channels in found:
        if name and name.lower() == text.lower():
            return index
    raise ValueError("-oiio_subimage {}: no such part. The parts: {}".format(text, ", ".join(
        "{} {}".format(i, name or "(no name)") for i, name, _c in found)))


def pick_channels(info, requested=None, use_first=False, preferences=None, subimage=""):
    """Pick channels from the per-subimage header list returned by get_image_info.

    requested:   explicit list from -oiio_channel (may be empty)
    use_first:   skip the preference list and take the first layer
    preferences: ordered channel sets (channel_preferences in presets.json)
    subimage:    a part's number or name (-oiio_subimage): only that part is searched
    """
    if preferences is None:
        preferences = PREFERRED_CHANNELS
    subimages = [(i, sub.get("channelnames") or []) for i, sub in enumerate(info)]
    if str(subimage or "").strip():
        only = find_subimage(info, subimage)
        subimages = [s for s in subimages if s[0] == only]
    requested = [c.strip() for c in (requested or []) if c.strip()]

    if requested:
        for index, names in subimages:
            if all(c in names for c in requested):
                return ChannelPick(index, requested, "requested")
        log.warning("Requested channels %s not found in the image; trying them anyway.",
                    ",".join(requested))
        return ChannelPick(subimages[0][0] if subimages else 0, requested, "requested (not found)")

    if not use_first:
        for preferred in preferences:
            for index, names in subimages:
                found = match_set(list(preferred), names)
                if found is not None:
                    reason = "preferred" if found == list(preferred) else \
                        "preferred ({})".format(",".join(preferred))
                    return ChannelPick(index, found, reason)

    for index, names in subimages:
        if not names:
            continue
        layers = list(group_layers(names).values())
        for layer in layers:
            if len(layer) >= 3:
                return ChannelPick(index, layer, "first layer")
        return ChannelPick(index, layers[0], "first layer")

    raise ValueError("Image has no channels.")
