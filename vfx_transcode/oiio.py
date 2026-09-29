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
        args += colour_args(cfg)
    if cfg.width > 0:
        args += ["--resize", "{}x0".format(cfg.width)]
    args += cfg.oiio_command
    if cfg.mezzanine_ext != ".exr":
        # 8-bit is fast and enough for an 8-bit h264; 16-bit is opt-in for 10-bit codecs
        args += ["-d", "uint16" if cfg.mezzanine_16bit else "uint8"]
    return args


def colour_args(cfg):
    """--colorconvert IN OUT, or --ociodisplay:from=IN[:looks=..] DISPLAY VIEW.
    An empty display or view means the config's default."""
    if cfg.display_mode:
        option = "--ociodisplay:from=" + cfg.colorin
        if cfg.ocio_looks:
            option += ":looks=" + cfg.ocio_looks
        return [option, cfg.ocio_display or "", cfg.ocio_view or ""]
    return ["--colorconvert", cfg.colorin, cfg.colorout]


def per_file_command(cfg, src, dst, mid):
    return [cfg.oiiotool, "-i", _posix(src)] + mid + ["-o", _posix(dst)]


def batch_patterns(seq, work_dir, ext):
    wildcard = "%0{}d".format(seq.zfill)
    return (_posix(seq.folder / (seq.base + wildcard + seq.ext)),
            _posix(Path(work_dir) / (seq.base + wildcard + ext)))


def batch_command(cfg, seq, work_dir, first, last, mid):
    src, dst = batch_patterns(seq, work_dir, cfg.mezzanine_ext)
    cmd = [cfg.oiiotool, "--frames", "{}-{}".format(first, last), "--parallel-frames"]
    if cfg.oiio_threads > 0:
        cmd += ["--threads", str(cfg.oiio_threads)]
    return cmd + ["-i", src] + mid + ["-o", dst]


def can_batch(cfg, seq, work_dir):
    if cfg.oiio_per_file or not seq.numbered:
        return False
    plain_src = _posix(seq.folder / (seq.base + seq.ext))
    plain_dst = _posix(Path(work_dir) / seq.base)
    # a literal %, # or @ in the path would be read as a frame wildcard
    return not any(ch in plain_src + plain_dst for ch in _WILDCARD_CHARS)


def convert_sequence(cfg, seq, pick, work_dir, depth=None):
    """Convert all present frames. Never raises for individual frame failures;
    those are returned in ConversionResult.failed."""
    work_dir = Path(work_dir)
    ext = cfg.mezzanine_ext
    mid = process_args(cfg, pick, depth)
    result = ConversionResult()
    target = {f: work_dir / seq.name(f, ext) for f in seq.frames}

    # stale files from an earlier kept run must not be mistaken for fresh output
    for path in target.values():
        if path.is_file():
            path.unlink()

    start = perf_counter()
    pending = []
    if can_batch(cfg, seq, work_dir):
        for first, last in seq.runs():
            frames = range(first, last + 1)
            if first == last:
                pending.append(first)
                continue
            cmd = batch_command(cfg, seq, work_dir, first, last, mid)
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
        _convert_per_file(cfg, seq, pending, target, mid, result)

    log.info("Conversion to %s took %.2f seconds", ext[1:], perf_counter() - start)
    return result


def _convert_per_file(cfg, seq, frames, target, mid, result):
    total = len(frames)
    log.info("Converting %d file(s) one by one with %d threads", total, cfg.workers)

    def convert(frame):
        t0 = perf_counter()
        run_tool(per_file_command(cfg, seq.path(frame), target[frame], mid), "oiiotool", cfg.verbose)
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
