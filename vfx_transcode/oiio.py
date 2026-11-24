"""EXR -> mezzanine (PNG by default) conversion with oiiotool.

Gap-free runs of frames are converted with one `oiiotool --parallel-frames` call each,
which avoids starting oiiotool and loading the OCIO config once per frame. Single
frames, sequences with unusual names, or runs whose batch call fails are converted
one file per oiiotool call on a thread pool.
"""

import concurrent.futures
import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Dict, List

from .deep import depth_args
from .errors import ToolError
from .tools import run_tool

log = logging.getLogger(__name__)

_WILDCARD_CHARS = ("%", "#", "@")


@dataclass
class ConversionResult:
    outputs: Dict[int, Path] = field(default_factory=dict)   # frame -> mezzanine file
    failed: List[int] = field(default_factory=list)


def _posix(path):
    return Path(path).as_posix()


def process_args(cfg, pick, depth=None):
    """oiiotool arguments between the input and the output.
    depth: a deep.DepthView to render Z instead of colour."""
    if depth is not None:
        args = depth_args(depth)
    else:
        args = []
        if pick.subimage:
            args += ["--subimage", str(pick.subimage)]
        if pick.deep:
            args += ["--flatten"]
        args += ["--ch", str(pick)]
        if cfg.oiio_transform_args is not None:
            # transform preset: on the scene-linear (and still premultiplied) image
            args += cfg.oiio_transform_args
        if pick.alpha and not cfg.skip_colour:
            # EXR colour is premultiplied: resize as it is, then divide by alpha so the colour
            # transform sees straight colour. The intermediates stay straight (as PNG expects);
            # ffmpeg premultiplies again with -alpha_premult 1.
            if cfg.width > 0 and cfg.oiio_transform_args is None:
                args += ["--resize", "{}x0".format(cfg.width)]
            args += ["--unpremult"]
        if not cfg.skip_colour:
            args += colour_args(cfg)
    if depth is not None and cfg.oiio_transform_args is not None:
        args += cfg.oiio_transform_args
    elif (cfg.width > 0 and cfg.oiio_transform_args is None
          and not (depth is None and pick.alpha and not cfg.skip_colour)):
        args += ["--resize", "{}x0".format(cfg.width)]
    args += cfg.oiio_command
    if cfg.mezzanine_ext != ".exr":
        # 8-bit is fast and enough for an 8-bit h264; 16-bit is opt-in for 10-bit codecs
        args += ["-d", "uint16" if cfg.mezzanine_16bit else "uint8"]
    return args


def plain_args(cfg):
    """oiiotool steps for input that is not EXR (PNG, JPG, a movie's frames): the OIIO
    transform (or the -width resize), colour only when -ci / -co / -display / -view was given
    (the frames are display-referred already), -oiio_command."""
    args = ["--ch", cfg.alpha_strip] if cfg.alpha_strip else []
    if cfg.oiio_transform_args is not None:
        args += cfg.oiio_transform_args
    elif cfg.width > 0:
        args += ["--resize", "{}x0".format(cfg.width)]
    if cfg.colour_given and not cfg.skip_colour and not cfg.clf:
        args += colour_args(cfg)
    return args + list(cfg.oiio_command)


def colour_args(cfg):
    """--ociofiletransform CLF, --colorconvert IN OUT, or --ociodisplay:from=IN[:looks=..]
    DISPLAY VIEW. An empty display or view means the config's default."""
    if cfg.clf:
        if not cfg.clf_path:
            raise ValueError("colour_args: the CLF path is not resolved yet")
        return ["--ociofiletransform", Path(cfg.clf_path).as_posix()]
    if cfg.display_mode:
        option = "--ociodisplay:from=" + cfg.colorin
        if cfg.ocio_looks:
            option += ":looks=" + cfg.ocio_looks
        return [option, cfg.ocio_display or "", cfg.ocio_view or ""]
    return ["--colorconvert", cfg.colorin, cfg.colorout]


def per_file_command(cfg, src, dst, mid):
    return [cfg.oiiotool, "-i", _posix(src)] + mid + ["-o", _posix(dst)]


class _Target:
    """Where converted frames go: the intermediates (work_dir, the input's name and padding, the
    mezzanine format) or an output sequence (outseq.OutputSeq)."""

    def __init__(self, folder, base, zfill, ext):
        self.folder, self.base, self.zfill, self.ext = Path(folder), base, zfill, ext

    def name(self, frame):
        return "{}{:0{}d}{}".format(self.base, frame, self.zfill, self.ext)

    def path(self, frame):
        return self.folder / self.name(frame)


def _target(cfg, seq, work_dir, target=None):
    if target is not None:
        return _Target(target.folder, target.base, target.zfill, target.ext)
    return _Target(work_dir, seq.base, seq.zfill, cfg.mezzanine_ext)


def batch_patterns(seq, target, ext=None):
    """(source pattern, target pattern). target: a _Target, or a folder (with ext) for the
    intermediates."""
    if not isinstance(target, _Target):
        target = _Target(target, seq.base, seq.zfill, ext)
    return (_posix((seq.frames_folder or seq.folder) / (seq.base + "%0{}d".format(seq.zfill) + seq.ext)),
            _posix(target.folder / (target.base + "%0{}d".format(target.zfill) + target.ext)))


def batch_command(cfg, seq, work_dir, first, last, mid, target=None):
    src, dst = batch_patterns(seq, _target(cfg, seq, work_dir, target))
    cmd = [cfg.oiiotool, "--frames", "{}-{}".format(first, last), "--parallel-frames"]
    if cfg.oiio_threads > 0:
        cmd += ["--threads", str(cfg.oiio_threads)]
    return cmd + ["-i", src] + mid + ["-o", dst]


def can_batch(cfg, seq, work_dir, target=None):
    if cfg.oiio_per_file or not seq.numbered:
        return False
    out = _target(cfg, seq, work_dir, target)
    plain_src = _posix((seq.frames_folder or seq.folder) / (seq.base + seq.ext))
    plain_dst = _posix(out.folder / out.base)
    # a literal %, # or @ in the path would be read as a frame wildcard
    return not any(ch in plain_src + plain_dst for ch in _WILDCARD_CHARS)


def convert_sequence(cfg, seq, pick, work_dir, depth=None, target=None, mid=None, per_frame=None):
    """Convert all present frames. Never raises for individual frame failures;
    those are returned in ConversionResult.failed.
    target: an output sequence (outseq.OutputSeq) instead of the intermediates in work_dir;
    mid: the oiiotool steps, instead of process_args;
    per_frame: frame -> extra oiiotool arguments for that frame (e.g. its timecode). oiiotool's
    --frames batch can't vary them, so the frames are then converted one file at a time."""
    work_dir = Path(work_dir)
    out = _target(cfg, seq, work_dir, target)
    ext = out.ext
    if mid is None:
        mid = process_args(cfg, pick, depth)
    result = ConversionResult()
    out_target = target
    target = {f: out.path(f) for f in seq.frames}

    # stale files from an earlier kept run must not be mistaken for fresh output
    for path in target.values():
        if path.is_file():
            path.unlink()

    start = perf_counter()
    pending = []
    if per_frame is None and can_batch(cfg, seq, work_dir, out_target):
        for first, last in seq.runs():
            frames = range(first, last + 1)
            if first == last:
                pending.append(first)
                continue
            cmd = batch_command(cfg, seq, work_dir, first, last, mid, out_target)
            log.info("Converting frames %d-%d with oiiotool --parallel-frames", first, last)
            try:
                run_tool(cmd, "oiiotool", cfg.verbose)
            except ToolError as exc:
                log.warning("Batch conversion of frames %d-%d failed, falling back to one file "
                            "at a time.\n%s", first, last, exc)
            for f in frames:
                if target[f].is_file():
                    result.outputs[f] = target[f]
                else:
                    pending.append(f)
    else:
        pending = list(seq.frames)

    if pending:
        _convert_per_file(cfg, seq, pending, target, mid, result, per_frame)

    log.info("Conversion to %s took %.2f seconds", ext[1:], perf_counter() - start)
    return result


def _convert_per_file(cfg, seq, frames, target, mid, result, per_frame=None):
    total = len(frames)
    log.info("Converting %d file(s) one by one with %d threads%s", total, cfg.workers,
             " (each frame gets its own attributes)" if per_frame else "")

    def convert(frame):
        t0 = perf_counter()
        steps = list(mid) + (per_frame(frame) if per_frame else [])
        run_tool(per_file_command(cfg, seq.path(frame), target[frame], steps), "oiiotool", cfg.verbose)
        return perf_counter() - t0

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(convert, f): f for f in frames}
        for future in concurrent.futures.as_completed(futures):
            frame = futures[future]
            done += 1
            name = seq.name(frame)
            try:
                seconds = future.result()
            except ToolError as exc:
                result.failed.append(frame)
                log.warning("[%d/%d] %s failed: %s", done, total, name, exc)
                continue
            if target[frame].is_file():
                result.outputs[frame] = target[frame]
                log.info("[%d/%d] %s %.2fs", done, total, name, seconds)
            else:
                result.failed.append(frame)
                log.warning("[%d/%d] %s: oiiotool reported success but wrote no file", done, total, name)
    result.failed.sort()
