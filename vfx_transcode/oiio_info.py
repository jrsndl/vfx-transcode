"""Reading image headers with `oiiotool --info -v -a -i:infoformat=xml`.

The XML parsing is adapted from OpenPype (MIT licence),
https://github.com/pypeclub/OpenPype (openpype/lib/transcoding.py).
"""

import json
import re
import xml.etree.ElementTree as ET

from .errors import TranscodeError
from .tools import run_tool

ARRAY_TYPE_REGEX = re.compile(r"^(int|float|string)\[\d+\]$")
XML_CHAR_REF_REGEX_HEX = re.compile(r"&#x?[0-9a-fA-F]+;")

INT_TAGS = {
    "x", "y", "z",
    "width", "height", "depth",
    "full_x", "full_y", "full_z",
    "full_width", "full_height", "full_depth",
    "tile_width", "tile_height", "tile_depth",
    "nchannels",
    "alpha_channel",
    "z_channel",
    "deep",
    "subimages",
}


class Rational:
    """Rational value such as '24/1', kept as the original string with a float value."""

    def __init__(self, string_value):
        top, _, bottom = string_value.partition("/")
        self.value = float(top) / float(bottom or 1)
        self.string_value = string_value

    def __float__(self):
        return self.value

    def __str__(self):
        return self.string_value

    def __format__(self, spec):
        return self.string_value.__format__(spec)

    def __repr__(self):
        return "<Rational {}>".format(self.string_value)


def convert_value_by_type_name(value_type, value):
    """Convert an attribute's text to a Python value based on the OIIO type name."""
    if value is None:
        return None
    if value_type in ("string", "timecode"):
        return value
    if value_type == "int":
        return int(value)
    if value_type in ("float", "double"):
        return float(value)
    if value_type in ("vec2f", "float2", "float2d"):
        return [float(item) for item in value.split(",")]
    if value_type in ("matrix", "matrixd"):
        parts = [float(p) for p in value.split(",")]
        size = {1: 1, 4: 2, 9: 3, 16: 4}.get(len(parts))
        if size is None:
            return parts
        # row-major list of rows
        return [parts[row * size:(row + 1) * size] for row in range(size)]
    if value_type == "rational2i":
        return Rational(value)
    if value_type in ("vector", "vectord"):
        output = []
        for part in (p.strip() for p in value.split(",")):
            if part in ("nan", "-nan"):
                output.append(None)
                continue
            try:
                output.append(float(part))
            except ValueError:
                output.append(part)
        return output

    match = ARRAY_TYPE_REGEX.match(value_type)
    if match:
        return [convert_value_by_type_name(match.group(1), item) for item in value.split(",")]
    return value


def parse_oiio_xml_output(xml_string):
    """Parse one <ImageSpec> block."""
    output = {}
    if not xml_string:
        return output

    # oiiotool can emit invalid character references (e.g. "&#01;") that ElementTree
    # rejects. Escape all of them; affected attribute values keep the literal text.
    xml_string = XML_CHAR_REF_REGEX_HEX.sub(lambda m: m.group(0).replace("&", "&amp;"), xml_string)

    tree = ET.fromstring(xml_string)
    attribs = {}
    output["attribs"] = attribs
    for child in tree:
        tag = child.tag
        if tag == "attrib":
            attribs[child.attrib["name"]] = convert_value_by_type_name(child.attrib["type"], child.text)
        elif tag == "channelnames":
            output[tag] = [channel.text for channel in child]
        elif tag in INT_TAGS:
            output[tag] = int(child.text)
        else:
            output[tag] = child.text
    return output


def split_imagespecs(output):
    """Pull the <ImageSpec>...</ImageSpec> blocks out of oiiotool's stdout."""
    blocks, lines, started = [], [], False
    for line in output.replace("\r\n", "\n").split("\n"):
        if not started:
            if not line.startswith("<"):
                continue
            started = True
        lines.append(line)
        if line.strip() == "</ImageSpec>":
            blocks.append("\n".join(lines))
            lines, started = [], False
    return blocks


def get_image_info(oiiotool, filepath, verbose=0):
    """Return a list of header dicts, one per subimage (EXR part)."""
    cmd = [oiiotool, "--info", "-v", "-a", "-i:infoformat=xml", filepath]
    result = run_tool(cmd, "Reading image info", verbose)
    blocks = split_imagespecs(result.stdout)
    if not blocks:
        raise TranscodeError('Failed to read image info from "{}".\nOutput:\n{}'.format(
            filepath, (result.stdout + result.stderr).strip()))
    return [parse_oiio_xml_output(block) for block in blocks]


def write_info_json(info, path):
    with open(path, "w", encoding="utf-8") as fout:
        # default=str: Rational and other custom values are written as their text
        json.dump(info, fout, sort_keys=True, indent=4, default=str)
