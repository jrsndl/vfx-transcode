"""Choosing which EXR channels become the RGB of the review movie."""

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import List

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

    @property
    def without_alpha(self):
        """Channels to write: alpha removed, then 3 channels (RGB) or 1 (mono)."""
        kept = [c for c in self.channels if not is_alpha(c)] or list(self.channels)
        return kept[:3] if len(kept) >= 3 else kept[:1]

    def __str__(self):
        return ",".join(self.without_alpha)


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


def pick_channels(info, requested=None, use_first=False, preferences=None):
    """Pick channels from the per-subimage header list returned by get_image_info.

    requested:   explicit list from -oiio_channel (may be empty)
    use_first:   skip the preference list and take the first layer
    preferences: ordered channel sets (channel_preferences in presets.json)
    """
    if preferences is None:
        preferences = PREFERRED_CHANNELS
    subimages = [(i, sub.get("channelnames") or []) for i, sub in enumerate(info)]
    requested = [c.strip() for c in (requested or []) if c.strip()]

    if requested:
        for index, names in subimages:
            if all(c in names for c in requested):
                return ChannelPick(index, requested, "requested")
        log.warning("Requested channels %s not found in the image; trying them anyway.",
                    ",".join(requested))
        return ChannelPick(0, requested, "requested (not found)")

    if not use_first:
        for preferred in preferences:
            for index, names in subimages:
                if all(c in names for c in preferred):
                    return ChannelPick(index, list(preferred), "preferred")

    for index, names in subimages:
        if not names:
            continue
        layers = list(group_layers(names).values())
        for layer in layers:
            if len(layer) >= 3:
                return ChannelPick(index, layer, "first layer")
        return ChannelPick(index, layers[0], "first layer")

    raise ValueError("Image has no channels.")
