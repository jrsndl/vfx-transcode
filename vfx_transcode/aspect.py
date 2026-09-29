"""Pixel aspect ratio (PAR).

Input PAR: -par, or the header's PixelAspectRatio, or 1 (order set by -par_source).
Output (-par_out):
    same     keep the pixels, tag the movie with the input PAR
    square   reformat for square pixels
    VALUE    reformat for that PAR and tag it
Reformatting keeps the output width (-width) and scales the height by par_out / par_in,
so the picture keeps its shape on screen: anamorphic 2.0 -> square halves the height.
"""

import logging
from fractions import Fraction

from .errors import TranscodeError
from .metadata import header_value

log = logging.getLogger(__name__)

PAR_ORDER = {"arg": ("arg", "exr"), "exr": ("exr", "arg"), "arg_only": ("arg",)}
HEADER_NAMES = ("PixelAspectRatio", "pixelAspectRatio", "oiio:PixelAspectRatio")
FOUR_THIRDS = Fraction(4, 3)


def parse_par(value, where="-par"):
    """'2', '1.33', '4/3', 2.0 -> Fraction. 1.33 / 1.333 mean 4/3 (anamorphic 1.33x)."""
    try:
        par = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise TranscodeError('{}: "{}" is not a pixel aspect ratio (e.g. 1, 2, 1.33, 4/3).'
                             .format(where, value))
    if not 0.1 <= par <= 10:
        raise TranscodeError('{}: pixel aspect ratio {} is out of range (0.1-10).'.format(where, value))
    if "." in str(value) and abs(par - FOUR_THIRDS) < Fraction(1, 200):
        return FOUR_THIRDS
    return par.limit_denominator(1000)


def par_display(par):
    if par.denominator == 1:
        return str(par.numerator)
    if par == FOUR_THIRDS:
        return "4/3 (1.33)"
    return "{:.4g}".format(float(par))


def header_par(attribs):
    for name in HEADER_NAMES:
        value = header_value(attribs or {}, name)
        if value:
            try:
                return parse_par(value, "header " + name), name
            except TranscodeError as exc:
                log.warning("%s; ignored", exc)
    return None, None


def resolve_par(cfg):
    """(input PAR, source) following -par_source; 1 as the last resort."""
    header, name = (None, None)
    if cfg.par_source != "arg_only":
        header, name = header_par(cfg.header_attribs)
    for source in PAR_ORDER[cfg.par_source]:
        if source == "arg" and cfg.par_arg is not None:
            if header is not None and header != cfg.par_arg:
                log.info("Pixel aspect: EXR header %s ignored (-par_source %s)",
                         par_display(header), cfg.par_source)
            return cfg.par_arg, "-par"
        if source == "exr" and header is not None:
            if cfg.par_arg is not None and cfg.par_arg != header:
                log.info("Pixel aspect: -par %s ignored (-par_source exr)", par_display(cfg.par_arg))
            return header, "EXR header " + name
    return Fraction(1), "default"


def output_par(par_in, par_out_option):
    """-par_out same|square|VALUE -> output PAR."""
    option = (par_out_option or "same").strip().lower()
    if option == "same":
        return par_in
    if option == "square":
        return Fraction(1)
    return parse_par(option, "-par_out")


def reformat_filters(par_in, par_out):
    """Height reformat (goes before the colour scale), or nothing when the PAR stays."""
    from .ffmpeg import make_filter      # local import: ffmpeg imports this module
    factor = par_out / par_in
    if factor == 1:
        return []
    return [make_filter("scale", [
        ("w", "iw"), ("h", "trunc(ih*{}/{}/2)*2".format(factor.numerator, factor.denominator))])]


def sar_filter(par_out):
    """The pixel aspect tag. Goes after every scale: ffmpeg's scale changes the SAR itself."""
    from .ffmpeg import make_filter
    return make_filter("setsar", [("sar", "{}/{}".format(par_out.numerator, par_out.denominator))])
