"""Finding an image sequence on disk from one of its files.

Replaces the fileseq dependency: a directory scan with a strict name match is
simpler, handles holes explicitly and does not rely on private attributes.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from .errors import TranscodeError

_NUMBERED_RE = re.compile(r"^(?P<base>.*?)(?P<num>\d+)$")
SEPARATORS = "._-"


@dataclass(frozen=True)
class Sequence:
    folder: Path
    base: str          # everything before the frame number, separator included ("shot_010.")
    ext: str           # extension as found on disk (".exr")
    zfill: int         # frame number padding, 0 for a single un-numbered file
    frames: Tuple[int, ...]  # frames present on disk, sorted

    @property
    def numbered(self):
        return self.zfill > 0

    @property
    def start(self):
        return self.frames[0]

    @property
    def end(self):
        return self.frames[-1]

    @property
    def clean_name(self):
        """Base name without the trailing separator ("shot_010")."""
        if self.numbered and self.base and self.base[-1] in SEPARATORS:
            return self.base[:-1]
        return self.base

    @property
    def is_exr(self):
        return self.ext.lower() == ".exr"

    def name(self, frame, ext=None):
        ext = self.ext if ext is None else ext
        if not self.numbered:
            return self.base + ext
        return "{}{:0{}d}{}".format(self.base, frame, self.zfill, ext)

    def path(self, frame):
        return self.folder / self.name(frame)

    def frame_of(self, path):
        """Frame number of a file of this sequence, or None."""
        stem = os.path.splitext(Path(path).name)[0]
        digits = stem[len(self.base):] if self.numbered and stem.startswith(self.base) else ""
        return int(digits) if digits.isdigit() else None

    @property
    def full_range(self):
        return list(range(self.start, self.end + 1))

    @property
    def missing(self):
        present = set(self.frames)
        return [f for f in self.full_range if f not in present]

    def runs(self):
        """Contiguous runs of present frames as (first, last) tuples."""
        return frame_runs(self.frames)


def fill_holes(frames, available):
    """Map every frame in `frames` to a file, holding the previous available file over
    holes (the next one if the very first frames are missing)."""
    if not available:
        return []
    first_available = available[min(available)]
    files, last = [], None
    for frame in frames:
        if frame in available:
            last = available[frame]
        files.append(last if last is not None else first_available)
    return files


def frame_runs(frames) -> List[Tuple[int, int]]:
    runs = []
    for f in sorted(frames):
        if runs and f == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], f)
        else:
            runs.append((f, f))
    return runs


def format_frames(frames):
    """[1, 2, 3, 7] -> '1-3,7'"""
    return ",".join(str(a) if a == b else "{}-{}".format(a, b) for a, b in frame_runs(frames))


def find_sequence(input_file):
    """Find the sequence the given file belongs to."""
    path = Path(input_file)
    if not path.is_file():
        raise TranscodeError('Input file "{}" does not exist.'.format(input_file))
    folder = path.parent
    stem, ext = os.path.splitext(path.name)

    m = _NUMBERED_RE.match(stem)
    if not m:
        # single, un-numbered image
        return Sequence(folder, stem, ext, 0, (0,))

    base, num = m.group("base"), m.group("num")
    zfill = len(num)
    frames = set()
    with os.scandir(folder) as it:
        for entry in it:
            e_stem, e_ext = os.path.splitext(entry.name)
            if e_ext.lower() != ext.lower() or not e_stem.startswith(base):
                continue
            digits = e_stem[len(base):]
            if not (digits.isascii() and digits.isdigit()):
                continue
            # same padding, or a longer un-padded number (frame 10000 in a 4-padded sequence)
            if len(digits) == zfill or (len(digits) > zfill and digits[0] != "0"):
                if entry.is_file():
                    frames.add(int(digits))
    frames.add(int(num))
    return Sequence(folder, base, ext, zfill, tuple(sorted(frames)))
