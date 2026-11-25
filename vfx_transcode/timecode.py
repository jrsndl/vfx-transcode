"""Frame rate and timecode helpers."""

import re
from fractions import Fraction


def parse_fps(value):
    """Parse '24', '25', '23.976' or '24000/1001' into a Fraction. Decimals are snapped to
    the standard rates (23.976 -> 24000/1001)."""
    try:
        fps = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        raise ValueError("Invalid frame rate {!r}. Use e.g. 24, 23.976 or 24000/1001.".format(value))
    if fps <= 0:
        raise ValueError("Frame rate must be positive, got {!r}.".format(value))
    if "." in str(value):
        fps = snap_fps(fps)        # 23.976 / 29.97 mean the NTSC rates; exact fractions stay as typed
    return fps


def fps_to_str(fps):
    """Fraction -> string ffmpeg understands ('24' or '24000/1001')."""
    if fps.denominator == 1:
        return str(fps.numerator)
    return "{}/{}".format(fps.numerator, fps.denominator)


def fps_display(fps):
    """For text: 24 -> '24', 24000/1001 -> '23.976', 25/1 -> '25'."""
    if fps.denominator == 1:
        return str(fps.numerator)
    return "{:.3f}".format(float(fps)).rstrip("0").rstrip(".")


FPS_KEYWORDS = ("source_fps_float", "source_fps_int", "source_fps_frac")


def fps_tokens(fps):
    """{source_fps_float} "23.976" (3 decimals), {source_fps_int} "24" (the closest whole
    number), {source_fps_frac} "24000/1001" ("25/1"); all "" when the rate is not known yet."""
    if fps is None:
        return {key: "" for key in FPS_KEYWORDS}
    fps = Fraction(fps)
    return {"source_fps_float": "{:.3f}".format(float(fps)),
            "source_fps_int": str(int(fps + Fraction(1, 2))),
            "source_fps_frac": "{}/{}".format(fps.numerator, fps.denominator)}


def nominal_fps(fps):
    """Integer frame rate used for timecode counting (23.976 -> 24, 29.97 -> 30)."""
    return max(1, int(fps + Fraction(1, 2)))


def frame_to_tc(frame, fps_int=24):
    """Non-drop-frame timecode for a frame count. Frame 1001 at 24 fps is 00:00:41:17."""
    frame = max(0, int(frame))
    fps_int = int(fps_int)
    seconds, frames = divmod(frame, fps_int)
    return "{:02d}:{:02d}:{:02d}:{:02d}".format(
        (seconds // 3600) % 24, (seconds // 60) % 60, seconds % 60, frames)


_TC_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})([:;.])(\d{2})$")


def is_timecode(text):
    return bool(_TC_RE.match(str(text).strip()))


def validate_timecode(text):
    text = str(text).strip()
    if not is_timecode(text):
        raise ValueError('Invalid timecode "{}", expected HH:MM:SS:FF.'.format(text))
    return text


def tc_to_frames(tc, fps_int):
    """Non-drop-frame timecode -> frame count."""
    h, m, s, _sep, f = _TC_RE.match(tc.strip()).groups()
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * int(fps_int) + int(f)


def offset_timecode(tc, frames, fps_int):
    """Shift a timecode by a number of frames, keeping its separator (; for drop-frame)."""
    sep = _TC_RE.match(tc.strip()).group(4)
    shifted = frame_to_tc(tc_to_frames(tc, fps_int) + frames + 24 * 3600 * int(fps_int), fps_int)
    return shifted[:8] + sep + shifted[9:]


def decode_smpte_bcd(value):
    """SMPTE 12M packed BCD time value (EXR timeCode) -> 'HH:MM:SS:FF'."""
    v = int(value)
    frames = (v & 0x0F) + 10 * ((v >> 4) & 0x03)
    drop = (v >> 6) & 0x01
    seconds = ((v >> 8) & 0x0F) + 10 * ((v >> 12) & 0x07)
    minutes = ((v >> 16) & 0x0F) + 10 * ((v >> 20) & 0x07)
    hours = ((v >> 24) & 0x0F) + 10 * ((v >> 28) & 0x03)
    return "{:02d}:{:02d}:{:02d}{}{:02d}".format(hours, minutes, seconds, ";" if drop else ":", frames)


# EXR timeCode is reported by OIIO as smpte:TimeCode; some writers add their own string keys.
TIMECODE_KEYS = ("smpte:TimeCode", "timecode", "timeCode", "TimeCode")


def timecode_from_attribs(attribs):
    """Find a timecode in an image header's attributes, or None."""
    candidates = [k for k in TIMECODE_KEYS if k in attribs]
    candidates += [k for k in attribs if k.lower().endswith("timecode") and k not in candidates]
    for key in candidates:
        value = attribs[key]
        if isinstance(value, str) and is_timecode(value):
            return validate_timecode(value)
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        if isinstance(value, str):
            value = value.split(",")[0].strip()
        try:
            return decode_smpte_bcd(int(value))
        except (TypeError, ValueError):
            continue
    return None


# --- frame rate from image headers ---

STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
                  Fraction(30), Fraction(48000, 1001), Fraction(48), Fraction(50),
                  Fraction(60000, 1001), Fraction(60), Fraction(120000, 1001), Fraction(120)]

# Header attribute names that hold a frame rate, compared case-insensitively, ignoring "_",
# spaces and any "prefix:" / "prefix/" (oiio:FramesPerSecond, nuke/input/frame_rate).
# Earlier names win when several are present. OpenEXR framesPerSecond (a rational) is reported
# by OIIO as FramesPerSecond; Karma writes it as a float; many pipelines use frame_rate.
FPS_NAMES = ("framespersecond", "framerate", "fps")


def _fps_name(key):
    base = re.split(r"[:/]", key)[-1]
    return base.lower().replace("_", "").replace(" ", "").replace("-", "")


def snap_fps(fps):
    """Snap to a standard rate within 0.05% (a float 23.976 -> 24000/1001)."""
    for rate in STANDARD_RATES:
        if abs(fps - rate) <= rate * Fraction(1, 2000):
            return rate
    return fps.limit_denominator(1001)


def fps_from_value(value):
    """Rational '24/1', float 24.0, '25' or [24, 1] -> Fraction, or None if unusable."""
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            fps = Fraction(int(value[0]), int(value[1]))
        elif isinstance(value, (int, float)):
            fps = Fraction(value)
        else:
            fps = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None
    if fps <= 0 or fps > 1000:
        return None
    return snap_fps(fps)


def fps_from_attribs(attribs):
    """(fps, attribute name) from an image header, or (None, None)."""
    candidates = [(FPS_NAMES.index(_fps_name(k)), k) for k in attribs if _fps_name(k) in FPS_NAMES]
    for _rank, key in sorted(candidates, key=lambda c: c[0]):
        fps = fps_from_value(attribs[key])
        if fps is not None:
            return fps, key
    return None, None
