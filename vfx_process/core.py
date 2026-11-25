"""vfx-transcode-process logic without Qt: finding media in folders, the output path presets,
checking a super-preset against an input, the job queue's data, reading vfx-transcode's
progress from its log, and Deadline submission files. The Qt window (app.py) only shows and
edits these objects."""

import configparser
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Dict, List, Optional

from vfx_transcode import movie as mv
from vfx_transcode import studio
from vfx_transcode import super_presets as sp
from vfx_transcode.errors import TranscodeError
from vfx_transcode.names import parse_version, path_tokens
from vfx_transcode.sequence import find_sequence, format_frames
from vfx_tuner import core as tuner

quote = tuner.quote
vfx_transcode_command = tuner.vfx_transcode_command

# --- inputs -----------------------------------------------------------------------------------

KIND_TITLES = OrderedDict([("exr", "EXR sequence"), ("image", "Image sequence"),
                           ("movie", "Movie")])
# "other image" formats the folder search can look for (EXR is its own kind)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".dpx", ".tga", ".bmp", ".hdr")
_NUMBERED = re.compile(r"^(?P<base>.*?)(?P<num>\d+)$")


@dataclass
class MediaItem:
    """One input: an image sequence (path = its first frame), a single image, or a movie."""
    path: str
    kind: str                         # exr | image | movie
    name: str = ""                    # the clean name: "sh010_comp_v003"
    pattern: str = ""                 # what to show: "sh010_comp_v003.####.exr" / "a.mov"
    first: int = 0
    last: int = 0
    count: int = 0                    # frames on disk (0: a movie, not probed)
    missing: str = ""                 # "1004-1006" when frames are missing
    base_folder: str = ""             # the folder scanned to find it (-base_folder); "" = picked

    @property
    def folder(self):
        return str(Path(self.path).parent)

    @property
    def is_still(self):
        return self.kind != "movie" and self.count == 1

    def describe(self):
        if self.kind == "movie":
            return "movie"
        if self.is_still:
            return "single image"
        text = "{}-{} ({} frames)".format(self.first, self.last, self.count)
        return text + (", missing " + self.missing if self.missing else "")


def kind_of(path):
    ext = Path(str(path)).suffix.lower()
    if mv.is_movie(path):
        return "movie"
    if ext == ".exr":
        return "exr"
    if ext in IMAGE_EXTS:
        return "image"
    return ""


def describe_file(path):
    """The MediaItem a picked file belongs to: its whole sequence, or the movie."""
    path = str(path)
    kind = kind_of(path)
    if not kind:
        raise TranscodeError("{}: not an image or a movie vfx-transcode reads.".format(Path(path).name))
    if kind == "movie":
        if not Path(path).is_file():
            raise TranscodeError("not found: " + path)
        return MediaItem(path=path, kind=kind, name=Path(path).stem, pattern=Path(path).name)
    seq = find_sequence(path)
    first = seq.path(seq.start)
    pattern = seq.base + "#" * seq.zfill + seq.ext if seq.numbered else seq.base + seq.ext
    return MediaItem(path=str(first), kind=kind, name=seq.clean_name or Path(path).stem,
                     pattern=pattern, first=seq.start, last=seq.end, count=len(seq.frames),
                     missing=format_frames(seq.missing) if seq.numbered else "")


@dataclass
class ScanFilter:
    """What the folder search looks for."""
    kinds: List[str] = field(default_factory=lambda: ["exr"])
    image_exts: List[str] = field(default_factory=lambda: [".png", ".jpg", ".tif", ".dpx"])
    include: str = ""                 # name patterns, comma separated: "*beauty*, *comp*"
    exclude: str = ""                 # name patterns to leave out
    exclude_dirs: str = ".*, _*"      # folder names not searched
    min_frames: int = 2               # sequences shorter than this are left out (1: stills too)
    max_depth: int = 0                # 0: all sub-folders
    latest_only: bool = False         # of the versions of a name (_v001, _v002) the highest

    def wanted_ext(self, ext):
        ext = ext.lower()
        if ext == ".exr":
            return "exr" in self.kinds
        if ext in IMAGE_EXTS:
            exts = [e.lower() for e in self.image_exts]
            return "image" in self.kinds and (ext in exts or (ext == ".jpeg" and ".jpg" in exts)
                                              or (ext == ".tiff" and ".tif" in exts))
        return "movie" in self.kinds and ext in mv.MOVIE_EXTS


def _patterns(text):
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]


def _matches(patterns, name, rel_path):
    """A pattern with a slash is matched against the path from the searched folder, else
    against the name. Case does not matter."""
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").lower()
        target = rel_path if "/" in pattern else name
        if fnmatch.fnmatchcase(target.lower(), pattern):
            return True
    return False


def group_files(folder, names):
    """{key: [file names]} of the media files of one folder: image sequences by base name and
    extension, single images and movies on their own."""
    groups = OrderedDict()
    for name in sorted(names):
        stem, ext = os.path.splitext(name)
        if mv.is_movie(name):
            key = ("movie", name, ext.lower())
        else:
            match = _NUMBERED.match(stem)
            key = (("seq", match.group("base"), ext.lower()) if match
                   else ("still", stem, ext.lower()))
        groups.setdefault(key, []).append(name)
    return groups


def item_from_group(folder, key, names):
    kind_name, base, ext = key
    folder = Path(folder)
    if kind_name == "movie":
        return MediaItem(path=str(folder / base), kind="movie", name=Path(base).stem, pattern=base)
    kind = "exr" if ext == ".exr" else "image"
    if kind_name == "still":
        return MediaItem(path=str(folder / names[0]), kind=kind, name=base, pattern=names[0],
                         count=1)
    frames, zfill = {}, None
    for name in names:
        digits = _NUMBERED.match(os.path.splitext(name)[0]).group("num")
        frames.setdefault(int(digits), name)
        zfill = len(digits) if zfill is None else min(zfill, len(digits))
    numbers = sorted(frames)
    clean = base[:-1] if base and base[-1] in "._-" else base
    missing = [f for f in range(numbers[0], numbers[-1] + 1) if f not in frames]
    return MediaItem(path=str(folder / frames[numbers[0]]), kind=kind,
                     name=clean or folder.name, pattern=base + "#" * zfill + ext,
                     first=numbers[0], last=numbers[-1], count=len(numbers),
                     missing=format_frames(missing))


def _version_key(item):
    """(name without its version, version number) for "latest only"."""
    text, number = parse_version(item.name)
    if not text:
        return item.name.lower(), -1
    root = re.sub(r"_v" + re.escape(text) + r"(?!.*_v\d)", "_v#", item.name, count=1)
    return (str(Path(item.folder).parent).lower() + "|" + root.lower(), int(number or 0))


def latest_versions(items):
    """Of items that differ only in their version (sh010_comp_v001, _v002 ...), the highest;
    the version folders may differ too (.../v001/sh010_comp_v001.####.exr)."""
    best = OrderedDict()
    for item in items:
        root, number = _version_key(item)
        key = (root, item.kind, Path(item.pattern).suffix.lower())
        if number < 0:
            best[(item.path,)] = (item, number)
        elif key not in best or number > best[key][1]:
            best[key] = (item, number)
    keep = {id(v[0]) for v in best.values()}
    return [i for i in items if id(i) in keep]


def scan_folder(root, filt, cancelled: Callable[[], bool] = lambda: False,
                progress: Callable[[str, int], None] = lambda folder, found: None):
    """MediaItems under root (and its sub-folders) that pass the filter, in folder order."""
    root = Path(root)
    include, exclude = _patterns(filt.include), _patterns(filt.exclude)
    skip_dirs = _patterns(filt.exclude_dirs)
    found = []
    for folder, dirs, files in os.walk(str(root)):
        if cancelled():
            break
        rel_folder = os.path.relpath(folder, str(root)).replace("\\", "/")
        depth = 0 if rel_folder == "." else rel_folder.count("/") + 1
        dirs[:] = sorted(d for d in dirs if not any(fnmatch.fnmatch(d.lower(), p.lower()) for p in skip_dirs))
        if filt.max_depth and depth >= filt.max_depth:
            dirs[:] = []
        names = [f for f in files if filt.wanted_ext(os.path.splitext(f)[1])]
        for key, group in group_files(folder, names).items():
            item = item_from_group(folder, key, group)
            if item.kind != "movie" and item.count < max(1, filt.min_frames):
                continue
            rel = (item.pattern if rel_folder == "." else rel_folder + "/" + item.pattern)
            if include and not (_matches(include, item.pattern, rel) or _matches(include, item.name, rel)):
                continue
            if exclude and (_matches(exclude, item.pattern, rel) or _matches(exclude, item.name, rel)):
                continue
            found.append(item)
        progress(folder, len(found))
    return latest_versions(found) if filt.latest_only else found


DEFAULT_PATTERNS = "*.exr *.mov *.mp4"
SKIP_DIRS = (".*",)                   # hidden folders (.git ...); _folders are searched


def split_patterns(text):
    """("*.mov *.mp4 !*tech*") -> (["*.mov", "*.mp4"], ["*tech*"]): wildcards on file names,
    separated by spaces or commas; ! leaves matching files out."""
    words = [w for w in re.split(r"[\s,;]+", text or "") if w]
    include = [w for w in words if not w.startswith("!")]
    exclude = [w[1:] for w in words if w.startswith("!") and len(w) > 1]
    return include or split_patterns(DEFAULT_PATTERNS)[0], exclude


def _filter_names(names, include, exclude):
    return [f for f in names
            if any(fnmatch.fnmatchcase(f.lower(), p) for p in include)
            and not any(fnmatch.fnmatchcase(f.lower(), p) for p in exclude)]


def walk_media(root, patterns="", cancelled: Callable[[], bool] = lambda: False,
               progress: Callable[[int, int], None] = lambda scanned, found: None):
    """The listing of a folder and all its sub-folders: [(folder, [media file names])], every
    file vfx-transcode could read, whatever the patterns. Kept by the app, so a new filter
    needs no new scan. patterns only count what progress() reports as found. Hidden folders
    (starting with .) are skipped."""
    include, exclude = split_patterns(patterns)
    include, exclude = [p.lower() for p in include], [p.lower() for p in exclude]
    listing, scanned, found = [], 0, 0
    for folder, dirs, files in os.walk(str(root)):
        if cancelled():
            break
        dirs[:] = sorted(d for d in dirs if not any(fnmatch.fnmatchcase(d, p) for p in SKIP_DIRS))
        scanned += len(files)
        names = sorted(f for f in files if kind_of(f))
        if names:
            listing.append((folder, names))
            found += len(group_files(folder, _filter_names(names, include, exclude)))
        progress(scanned, found)
    return listing


def filter_listing(listing, patterns, base_folder=""):
    """MediaItems of a listing (walk_media) whose file names match the patterns; base_folder:
    the folder that was scanned, for {base_folder}."""
    include, exclude = split_patterns(patterns)
    include, exclude = [p.lower() for p in include], [p.lower() for p in exclude]
    items = []
    for folder, names in listing:
        for key, group in group_files(folder, _filter_names(names, include, exclude)).items():
            item = item_from_group(folder, key, group)
            item.base_folder = base_folder
            items.append(item)
    return items


def scan_patterns(root, text, cancelled: Callable[[], bool] = lambda: False):
    """MediaItems under root and all its sub-folders whose file names match the patterns."""
    return filter_listing(walk_media(root, text, cancelled), text, str(root))


# the folder filter for a super-preset's input type (the tuner records it)
TYPE_PATTERNS = OrderedDict([
    ("exr", "*.exr"), ("png", "*.png"), ("jpg", "*.jpg *.jpeg"), ("tif", "*.tif *.tiff"),
    ("dpx", "*.dpx"),
    ("movie", " ".join("*" + e for e in (".mov", ".mp4", ".mxf", ".m4v", ".mkv", ".avi"))),
])
KIND_PATTERNS = {"exr": "*.exr", "movie": TYPE_PATTERNS["movie"],
                 "image": "*.png *.jpg *.jpeg *.tif *.tiff *.dpx"}


def patterns_for_preset(entry, current=""):
    """The folder filter for a super-preset: the files of its input type ("input_type", else
    its "inputs" kinds), keeping the ! exclusions of the current filter. None when the
    super-preset records no input type (the filter stays as it is)."""
    input_type = (entry or {}).get("input_type", "")
    if input_type in TYPE_PATTERNS:
        text = TYPE_PATTERNS[input_type]
    else:
        kinds = [k for k in (entry or {}).get("inputs") or [] if k in KIND_PATTERNS]
        if not kinds:
            return None
        text = " ".join(KIND_PATTERNS[k] for k in kinds)
    exclude = split_patterns(current)[1]
    return " ".join([text] + ["!" + p for p in exclude])


# --- output -----------------------------------------------------------------------------------

OUTPUT_MODES = OrderedDict([
    ("source", "Next to the source"),
    ("subfolder", "In a folder next to the source"),
    ("folder", "All in one folder"),
    ("custom", "Custom path"),
])


@dataclass
class OutputSpec:
    """Where the outputs go; becomes vfx-transcode's -o (./ = the input's folder)."""
    mode: str = "source"
    subfolder: str = "review"         # mode subfolder: ./review/ (../review works too)
    folder: str = ""                  # mode folder: one folder for all outputs
    custom: str = ""                  # mode custom: any -o, keywords allowed
    name: str = ""                    # the file name, keywords allowed; "" = the source's name

    def folder_part(self):
        if self.mode == "subfolder":
            sub = self.subfolder.strip().strip("/\\") or "review"
            return "./" + sub.replace("\\", "/") + "/"
        if self.mode == "folder":
            return self.folder.strip().rstrip("/\\") + "/" if self.folder.strip() else ""
        return "./"

    def template(self):
        """-o as the user set it, without the format's extension."""
        if self.mode == "custom":
            return self.custom.strip() or "./"
        return self.folder_part() + self.name.strip()

    def check(self):
        """Why this output can't be used, or ""."""
        if self.mode == "folder" and not self.folder.strip():
            return "Choose the output folder."
        if self.mode == "custom" and not self.custom.strip():
            return "Type the output path."
        if any(c in self.name for c in '<>|"?*'):
            return "The output name has a character a file name can't have."
        return ""


# the Destination: path and name presets come from the presets file (tuner.path_menu)
destination_of = tuner.destination_of


def destination_spec(path, name):
    """OutputSpec from the Path and Name fields: an empty path is the source's folder."""
    return OutputSpec(mode="custom", custom=tuner.destination_template(path, name))


def output_arg(spec, output_type):
    """-o for this output type: the tuner's rule (a sequence gets .####.EXT)."""
    return tuner.output_arg(spec.template(), output_type)


def read_options(args, presets):
    """(vfx-transcode Config, "") for a job's options, as vfx-transcode reads them; (None,
    why) when it would stop: an unknown keyword in -o, a bad option ..."""
    import io
    from contextlib import redirect_stderr
    from vfx_transcode.cli import build_parser, config_from_args
    errors = io.StringIO()
    try:
        with redirect_stderr(errors):             # argparse prints, then exits
            return config_from_args(build_parser(presets).parse_args(list(args)), presets), ""
    except SystemExit:
        lines = [l for l in errors.getvalue().splitlines() if l.strip()]
        return None, lines[-1] if lines else "the options are not valid"
    except Exception as exc:
        return None, str(exc)


def keyword_context(item, args=None, presets=None, ext="", custom=("", ""), cfg=None):
    """The keywords vfx-transcode will have for this input before it reads the files: the name
    keywords (with the job's -version_regex), path keywords, custom destination values, date,
    user, frame range, the frame rate when -r gives it, and the presets file's name tokens
    and smart tokens. args: the job's vfx-transcode options (read as vfx-transcode would).
    What needs the files ({meta:...}, a detected frame rate) is not here."""
    import datetime
    from vfx_transcode import metadata as md
    from vfx_transcode.names import DEFAULT_VERSION_REGEX, name_tokens
    from vfx_transcode.timecode import fps_tokens
    if cfg is None and args is not None and presets is not None:
        cfg = read_options(args, presets)[0]       # a bad option: make_jobs says so
    source = os.path.join(item.folder, item.pattern)
    ctx = dict(name_tokens(item.name, cfg.version_regex if cfg else DEFAULT_VERSION_REGEX))
    ctx.update(path_tokens(source, (cfg.base_folder if cfg else "") or item.base_folder))
    ctx.update(custom_destination_path=custom[0], custom_destination_name=custom[1])
    if cfg is not None:
        ctx.update({k: v for k, v in cfg.custom_tokens.items() if v})
        if cfg.fps_arg is not None:
            ctx.update(fps_tokens(cfg.fps_arg))
    ctx.update(clean_name=item.name, ext=ext, date=datetime.date.today().isoformat(),
               user=_user(), input=os.path.basename(item.path))
    if item.kind != "movie" and item.count:
        ctx.update(frame_start=item.first, frame_end=item.last, frames=item.last - item.first + 1)
    if cfg is not None:
        ctx["preset"] = cfg.preset.name
        try:
            from vfx_transcode.names import custom_tokens
            if cfg.name_rules:
                ctx.update(custom_tokens(cfg.name_rules, dict(ctx, meta={}), md.expand))
            if cfg.smart_tokens is not None:
                from vfx_transcode.smart_tokens import evaluate_preset
                ctx.update(evaluate_preset(cfg.smart_tokens, dict(ctx, meta={}), md.expand))
        except Exception:                          # a token that needs the files: left out
            pass
    return ctx


def _user():
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return ""


def expand_keywords(text, context):
    """{keywords} filled from the context; unknown ones stay as typed."""
    from vfx_transcode import metadata as md
    return md.expand(text, context)


def predict_output(item, spec, output_type, ext, custom=("", ""), context=None):
    """Where the output lands, to show and to spot clashes: the keywords known before the run
    filled in (context: keyword_context; else the basic ones), ./ resolved against the input's
    folder. Keywords that need the files stay as they are."""
    template = output_arg(spec, output_type)
    if context is None:
        context = keyword_context(item, ext=ext, custom=custom)
    text = expand_keywords(template, dict(context, ext=ext))
    folder_like = text.endswith(("/", "\\")) or text in (".", "..")
    if re.match(r"^\.\.?([/\\]|$)", text):
        text = os.path.normpath(os.path.join(item.folder, text))
    else:
        text = os.path.normpath(text) if text else item.folder
    if folder_like:
        text = os.path.join(text, item.name + ("." + ext if output_type == "movie"
                                               else ".####." + ext))
    elif output_type == "movie" and not re.search(r"\.[A-Za-z0-9]{2,4}$", text):
        text += "." + ext
    return text


def overwrites_input(item, predicted):
    """The output would write onto the input: the movie itself, or the same frames."""
    a = os.path.normcase(os.path.normpath(predicted))
    if item.kind == "movie":
        return a == os.path.normcase(os.path.normpath(item.path))
    pattern = os.path.normcase(os.path.normpath(os.path.join(item.folder, item.pattern)))
    return a.replace("#", "") == pattern.replace("#", "")


# --- super-presets: what they make and which inputs they fit ----------------------------------

OK, WARN, ERROR = "ok", "warn", "error"


@dataclass
class Issue:
    level: str                        # warn | error
    text: str


@dataclass
class Check:
    output_type: str = "movie"        # movie | exr | png | jpg | tif | dpx
    ext: str = ""                     # the output's extension: mov, mp4, exr ...
    issues: List[Issue] = field(default_factory=list)

    @property
    def level(self):
        levels = [i.level for i in self.issues]
        return ERROR if ERROR in levels else WARN if WARN in levels else OK

    def text(self):
        return "\n".join(("Error: " if i.level == ERROR else "Note: ") + i.text for i in self.issues)


def _area(entry, name):
    """(state, args) of an area; the older "letterbox" area counts as Transform."""
    areas = entry.get("areas") or {}
    value = areas.get(name) or {}
    state, args = dict(value.get("state") or {}), list(value.get("args") or [])
    if name == "transform" and "letterbox" in areas:
        state.setdefault("letterbox", (areas["letterbox"] or {}).get("state") or {})
        args += (areas["letterbox"] or {}).get("args") or []
    return state, args


_NEUTRAL = {("-width", "0"), ("-letterbox", "0"), ("-oiio_letterbox", "0"),
            ("-burn_preset", "none"), ("-slate", "none"), ("-alpha", "drop")}


def _active_args(args):
    """Whether args do anything beyond switching things off."""
    pairs = list(zip(args[::2], args[1::2]))
    return any(p not in _NEUTRAL for p in pairs) or len(args) % 2 == 1


def output_of(entry, presets):
    """(output type, extension, issues) a super-preset makes. Super-presets saved by a newer
    tuner say the output type; for older ones a sequence preset means that sequence type,
    else a movie."""
    enc, enc_args = _area(entry, "encode")
    issues = []
    seq_preset = enc.get("seq_preset", "")
    if not seq_preset and "-seq_preset" in enc_args:
        seq_preset = enc_args[enc_args.index("-seq_preset") + 1]
    encode_preset = enc.get("encode_preset", "")
    if not encode_preset and "-preset" in enc_args:
        encode_preset = enc_args[enc_args.index("-preset") + 1]
    output_type = enc.get("output_type", "")
    if output_type not in tuner.OUTPUT_TYPES:
        output_type = ""
    if seq_preset and presets is not None:
        seq = presets.sequences.get(seq_preset)
        if seq is None:
            issues.append(Issue(ERROR, 'sequence preset "{}" is not in the presets file.'.format(seq_preset)))
        elif not output_type:
            output_type = seq.ext
        elif output_type != "movie" and seq.ext != output_type:
            issues.append(Issue(ERROR, 'sequence preset "{}" writes {}, but the super-preset makes {}.'
                                .format(seq_preset, seq.ext, output_type)))
    output_type = output_type or "movie"
    ext = output_type
    if output_type == "movie":
        ext = "mov"
        if presets is not None:
            name = encode_preset or presets.default
            preset = presets.presets.get(name)
            if preset is None:
                issues.append(Issue(ERROR, 'encode preset "{}" is not in the presets file.'.format(name)))
            else:
                ext = preset.ext
    return output_type, ext, issues


def _fake_path(kind):
    return {"exr": "x.0001.exr", "image": "x.0001.png", "movie": "x.mov"}.get(kind, "")


def check_super_preset(entry, item, presets):
    """Check: what the super-preset makes from this input, and what does not fit."""
    output_type, ext, issues = output_of(entry, presets)
    check = Check(output_type, ext, list(issues))
    add = check.issues.append
    kind = item.kind
    inputs = entry.get("inputs")
    if inputs and kind not in inputs:
        add(Issue(ERROR, "made for {} input, not {}.".format(
            " / ".join(KIND_TITLES[k].lower() + "s" for k in inputs), KIND_TITLES[kind].lower())))
    p = tuner.pipeline(_fake_path(kind), output_type)
    where = "{} to {}".format(KIND_TITLES[kind].lower() if not item.is_still else "a single image",
                              "a movie" if output_type == "movie" else output_type.upper() + " frames")

    ts, ts_args = _area(entry, "transform_sequence")
    steps = ts.get("steps") or {}
    oiio_used = (bool(steps.get("use") and steps.get("args")) or bool((ts.get("letterbox") or {}).get("enabled"))
                 if ts else _active_args(ts_args))
    if oiio_used and not p.oiio:
        add(Issue(WARN, "its Transform Sequence (oiiotool) settings are not used for {}.".format(where)))

    tf, tf_args = _area(entry, "transform")
    ff_used = (bool(((tf.get("ffmpeg") or {}).get("text") or "").strip())
               or bool((tf.get("letterbox") or {}).get("enabled"))) if tf else _active_args(tf_args)
    if ff_used and not p.ffmpeg:
        add(Issue(WARN, "its Transform (ffmpeg) settings and letterbox are not used for {}.".format(where)))

    burn, burn_args = _area(entry, "burnin")
    burn_used = (bool(burn.get("enabled")) and (bool(burn.get("preset")) or any(
        (pl or {}).get("text") for pl in (burn.get("places") or {}).values()))) if burn else _active_args(burn_args)
    if burn_used and not p.ffmpeg:
        add(Issue(WARN, "burn-ins are not drawn for {}.".format(where)))

    slate, slate_args = _area(entry, "slate")
    slate_used = bool(slate.get("enabled")) if slate else _active_args(slate_args)
    if slate_used and not p.movie:
        add(Issue(WARN, "no slate: it is only added to movies."))

    if item.is_still and output_type == "movie":
        add(Issue(WARN, "a single image makes a one-frame movie."))
    if kind == "movie" and output_type == "exr":
        add(Issue(WARN, "a movie is display-referred: its EXR frames are not scene-linear."))
    return check


def check_summary(entry, items, presets):
    """(level, text) of a super-preset for all the inputs: the worst level, and what does not
    fit, grouped so a hundred EXR sequences give one line."""
    by_text, worst = OrderedDict(), OK
    for item in items:
        check = check_super_preset(entry, item, presets)
        if check.level == ERROR or (check.level == WARN and worst == OK):
            worst = check.level
        for issue in check.issues:
            by_text.setdefault((issue.level, issue.text), 0)
            by_text[(issue.level, issue.text)] += 1
    lines = []
    for (level, text), count in by_text.items():
        prefix = "Error: " if level == ERROR else "Note: "
        lines.append(prefix + text + ("  ({} of {} inputs)".format(count, len(items))
                                      if len(items) > 1 and count != len(items) else ""))
    return worst, "\n".join(lines)


# --- tweaks: what the user overrides (all "" = as detected / the preset's) ---

FRAME_RATES = ("23.976", "24", "25", "29.97", "30", "47.952", "48", "50", "59.94", "60")
PIXEL_ASPECTS = ("1", "2", "1.8", "1.5", "1.33", "1.3", "1.25", "0.9")
ALPHA_MODES = OrderedDict([("drop", "drop: no alpha"), ("keep", "keep: always (error if it can't)"),
                           ("auto", "auto: keep when the input has one")])
COMMON_COLOURSPACES = ("ACEScg", "ACES - ACES2065-1", "Utility - Linear - sRGB",
                       "Utility - sRGB - Texture", "Utility - Rec.709 - Camera",
                       "Input - ARRI - V3 LogC (EI800) - Wide Gamut")
_TIMECODE = re.compile(r"^\d{1,2}[:;]\d\d[:;]\d\d[:;]\d\d$")


@dataclass
class Interpretation:
    """How to read the source, when the user knows better than the detection or the
    super-preset. "" = leave it to them."""
    fps: str = ""                     # -r
    par: str = ""                     # -par
    first_frame: str = ""             # -movie_first_frame (movies)
    timecode: str = ""                # -timecode HH:MM:SS:FF
    reel: str = ""                    # -reelid (+ -reelid_source arg)
    colorin: str = ""                 # -ci
    alpha: str = ""                   # -alpha drop | keep | auto
    write_timecode: str = ""          # -output_timecode "1" | "0"
    write_reel: str = ""              # -output_reelid "1" | "0"

    def changed(self):
        return sum(1 for v in asdict(self).values() if str(v).strip())

    def problems(self):
        out = []
        if self.fps.strip():
            try:
                tuner.parse_fps(self.fps.strip())
            except (ValueError, ZeroDivisionError):
                out.append("Frame rate {!r}: use e.g. 24, 23.976 or 24000/1001.".format(self.fps))
        if self.par.strip():
            try:
                from fractions import Fraction
                if Fraction(self.par.strip()) <= 0:
                    raise ValueError
            except (ValueError, ZeroDivisionError):
                out.append("Pixel aspect {!r}: use e.g. 1, 2 or 4/3.".format(self.par))
        if self.first_frame.strip() and not re.match(r"^-?\d+$", self.first_frame.strip()):
            out.append("Movie first frame {!r}: a whole number, e.g. 1001.".format(self.first_frame))
        if self.timecode.strip() and not _TIMECODE.match(self.timecode.strip()):
            out.append("Timecode {!r}: use HH:MM:SS:FF, e.g. 01:00:00:00.".format(self.timecode))
        if self.alpha and self.alpha not in ALPHA_MODES:
            out.append("Alpha {!r}: use {}.".format(self.alpha, ", ".join(ALPHA_MODES)))
        return out

    def args(self):
        """The options; they go after the super-preset's, so they win."""
        out = []
        for option, value in (("-r", self.fps), ("-par", self.par),
                              ("-movie_first_frame", self.first_frame), ("-timecode", self.timecode),
                              ("-ci", self.colorin), ("-alpha", self.alpha),
                              ("-output_timecode", self.write_timecode),
                              ("-output_reelid", self.write_reel)):
            if str(value).strip():
                out += [option, str(value).strip()]
        if self.timecode.strip():                   # a super-preset may say metadata: this wins
            out += ["-timecode_source", "arg"]
        if self.reel.strip():
            out += ["-reelid", self.reel.strip(), "-reelid_source", "arg"]
        return out


# --- jobs -------------------------------------------------------------------------------------

WAITING, RUNNING, DONE, FAILED, CANCELLED, SUBMITTED, FARM_DONE = (
    "waiting", "running", "done", "failed", "cancelled", "submitted", "farm_done")
STATUS_TITLES = OrderedDict([(WAITING, "Waiting"), (RUNNING, "Running"), (DONE, "Done"),
                             (FAILED, "Failed"), (CANCELLED, "Cancelled"),
                             (SUBMITTED, "On farm"), (FARM_DONE, "Done on farm")])
FINISHED_OK = (DONE, FARM_DONE)       # an output to play / explore


@dataclass
class Job:
    input: str                        # any file of the sequence, or the movie
    kind: str
    name: str                         # the input's clean name
    pattern: str
    frames: int                       # frames on disk; 0 = a movie, unknown
    super_preset: str
    output: str                       # -o
    output_type: str = "movie"
    predicted: str = ""               # where the output should land
    colour_preset: str = ""
    presets_file: str = ""            # "" = the one vfx-transcode finds itself
    extra: str = ""                   # more vfx-transcode options
    base_folder: str = ""             # the scanned folder it was found in (-base_folder)
    custom_path: str = ""             # {custom_destination_path}, typed by the user
    custom_name: str = ""             # {custom_destination_name}
    stills: List[str] = field(default_factory=list)   # -ot / -of PATH from the destination
    interpret: List[str] = field(default_factory=list)  # Tweaks: the user's overrides
    super_args: List[str] = field(default_factory=list)   # the super-preset's options, expanded
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = WAITING
    progress: float = 0.0             # 0..1
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    deadline_id: str = ""
    added: float = field(default_factory=time.time)
    result: str = ""                  # vfx-transcode's "Done:" line: the output written
    play: bool = False                # open the output when done
    explore: bool = False             # show it in the file browser when done
    log: List[str] = field(default_factory=list)

    def args(self, inline=False):
        """vfx-transcode options. inline: the super-preset's options written out (for Deadline,
        whose machines may not see this computer's super_presets.json)."""
        out = ["-i", self.input]
        if self.presets_file:
            out += ["-presets_file", self.presets_file]
        out += list(self.super_args) if inline else ["-super_preset", self.super_preset]
        if self.colour_preset:
            out += ["-colour_preset", self.colour_preset]
        if self.base_folder:
            out += ["-base_folder", self.base_folder]
        out += tuner.custom_args(self.custom_path, self.custom_name)
        out += ["-o", self.output] + list(self.stills) + list(self.interpret)
        return out + tuner.split_args(self.extra)

    def command(self, tool=None, inline=False):
        return (tool or vfx_transcode_command()) + self.args(inline)

    def elapsed(self, now=None):
        if not self.started:
            return 0.0
        return (self.finished or now or time.time()) - self.started

    def to_json(self):
        data = asdict(self)
        data["log"] = self.log[-5000:]           # kept for the Log window after a restart
        return data

    @classmethod
    def from_json(cls, data):
        known = {f for f in cls.__dataclass_fields__}
        job = cls(**{k: v for k, v in data.items() if k in known})
        if job.status == RUNNING:             # the app was closed while it ran
            job.status, job.progress, job.message = WAITING, 0.0, "Interrupted: will run again"
        return job


def missing_custom(dest, custom=("", "")):
    """What the user still has to type: the custom path / name the destination uses (dest: a
    DestinationState, or an OutputSpec for the output alone)."""
    texts = ([dest.output(), dest.thumbnail(), dest.filmstrip()] if hasattr(dest, "thumbnail")
             else [dest.template()])
    uses_path, uses_name = tuner.uses_custom(*texts)
    missing = []
    if uses_path and not custom[0].strip():
        missing.append("Custom path ({custom_destination_path})")
    if uses_name and not custom[1].strip():
        missing.append("Custom name ({custom_destination_name})")
    return missing


def make_jobs(items, super_name, entry, spec, presets, colour="", extra="", presets_file="",
              play=False, explore=False, custom=("", ""), dest=None, interpret=None):
    """(jobs, problems): a job per input that fits; problems: [(item, Check)] that don't,
    plus warnings. An output another job also writes is an error."""
    jobs, problems, seen = [], [], {}
    args = sp.args_for({super_name: entry}, super_name)
    # the thumbnail / filmstrip the super-preset switches on go where its destination says
    # (default: next to the output); later options win, so these replace its bare -ot / -of
    stills = []
    if dest is not None:
        if "-ot" in args:
            stills += ["-ot", dest.thumbnail()]
        if "-of" in args:
            stills += ["-of", dest.filmstrip()]
    for item in items:
        check = check_super_preset(entry, item, presets)
        job = Job(input=item.path, kind=item.kind, name=item.name, pattern=item.pattern,
                  frames=item.count, super_preset=super_name,
                  output=output_arg(spec, check.output_type), output_type=check.output_type,
                  colour_preset=colour, presets_file=presets_file,
                  extra=extra, super_args=args, play=play, explore=explore,
                  base_folder=item.base_folder,
                  custom_path=custom[0].strip(), custom_name=custom[1].strip(),
                  stills=list(stills), interpret=interpret.args() if interpret else [])
        cfg, problem = read_options(job.args(inline=True), presets) if presets is not None else (None, "")
        if problem:
            check.issues.append(Issue(ERROR, "vfx-transcode would stop: " + problem))
        context = keyword_context(item, presets=presets, ext=check.ext, custom=custom, cfg=cfg)
        predicted = job.predicted = predict_output(item, spec, check.output_type, check.ext, custom, context)
        if overwrites_input(item, predicted):
            check.issues.append(Issue(ERROR, "the output would overwrite the input: choose another "
                                             "output folder or name."))
        key = os.path.normcase(predicted)
        if key in seen:
            check.issues.append(Issue(ERROR, "{} writes the same output ({}): add a keyword such as "
                                             "{{namesplit1}} to the name, or another folder."
                                      .format(seen[key], predicted)))
        seen.setdefault(key, item.pattern)
        if check.issues:
            problems.append((item, check))
        if check.level == ERROR:
            continue
        jobs.append(job)
    return jobs, problems


QUEUE_FORMAT = "vfx-transcode-queue"
QUEUE_EXT = ".vtq"


def queue_json(jobs, note=""):
    """The queue as a file: every job with its settings, status, times and log."""
    import socket
    return OrderedDict([("format", QUEUE_FORMAT), ("version", 1),
                        ("saved", time.strftime("%Y-%m-%d %H:%M:%S")),
                        ("saved_by", _user()), ("computer", socket.gethostname()),
                        ("note", note), ("jobs", [j.to_json() for j in jobs])])


def save_queue(path, jobs, note=""):
    """Write the queue (safely: a temporary file, then replaced)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(queue_json(jobs, note), indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_queue_file(path):
    """(jobs, header) from a saved queue; raises TranscodeError when it is not one."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise TranscodeError("{} can't be read: {}".format(path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or \
            data.get("format", QUEUE_FORMAT) != QUEUE_FORMAT:
        raise TranscodeError("{} is not a vfx-transcode queue.".format(path))
    jobs = []
    for raw in data["jobs"]:
        try:
            jobs.append(Job.from_json(raw))
        except (TypeError, ValueError):
            continue                               # a job this version can't read: left out
    header = {k: v for k, v in data.items() if k != "jobs"}
    return jobs, header


def load_queue(path):
    """The app's own queue at start; empty when there is none or it can't be read."""
    try:
        return read_queue_file(path)[0]
    except TranscodeError:
        return []


def merge_jobs(current, loaded, replace=False):
    """The queue after loading: the loaded jobs replace the current ones, or follow them. A
    loaded job whose id is already in the queue gets a new id."""
    if replace:
        return list(loaded)
    ids = {j.id for j in current}
    out = list(current)
    for job in loaded:
        if job.id in ids:
            job.id = uuid.uuid4().hex[:10]
        ids.add(job.id)
        out.append(job)
    return out


def unreachable_inputs(jobs):
    """The waiting jobs whose input this computer can't see (a queue from another user)."""
    return [j for j in jobs if j.status == WAITING and not os.path.isfile(j.input)]


# --- progress: read from vfx-transcode's log --------------------------------------------------

_FRAMES_NUMBERED = re.compile(r"frames numbered (\d+)-(\d+)")
_INPUT_FRAMES = re.compile(r"^Input: .*frames \S+ \((\d+)\)")
_TRIM = re.compile(r"^Trim: frames \d+-\d+ \((\d+) of")
_COUNT = re.compile(r"^(?:Warning: )?\[(\d+)/(\d+)\]")
_CONVERT_BATCH = re.compile(r"^Converting frames (\d+)-(\d+) with oiiotool")
_IMAGE_EXTS = (".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".dpx")


class Progress:
    """Where a run is, from the lines vfx-transcode prints, and from counting the frames it
    writes while oiiotool converts a batch (it prints nothing then).

    Steps: oiiotool conversion (most of the time for EXR), then the ffmpeg encode."""

    def __init__(self, frames, kind, output_type, started=None):
        self.frames = frames or 0
        self.started = started or time.time()
        p = tuner.pipeline(_fake_path(kind), output_type)
        # share of the bar per step
        self.convert_share = 0.85 if (p.oiio and p.ffmpeg) else (0.97 if p.oiio else 0.0)
        self.stage = "Starting"
        self.value = 0.0
        self.watch_dir = None             # a folder oiiotool writes into, counted while it runs
        self.watch_prefix = ""
        self.watch_base = 0               # frames converted before this batch
        self.batch_frames = 0
        self.converted = 0
        self.work_dir = None
        self.output = ""

    def _convert_progress(self, done):
        if self.frames:
            self._set(self.convert_share * min(1.0, done / float(self.frames)))

    def _set(self, value):
        self.value = max(self.value, min(0.99, value))

    def feed(self, line):
        """Read one log line. Returns True when the stage or value changed."""
        line = line.strip()
        before = (self.stage, self.value)
        m = _FRAMES_NUMBERED.search(line) if line.startswith("Input: movie") else None
        if m:
            self.frames = int(m.group(2)) - int(m.group(1)) + 1
        m = _INPUT_FRAMES.match(line)
        if m:
            self.frames = int(m.group(1))
        m = _TRIM.match(line)
        if m:
            self.frames = int(m.group(1))
        if line.startswith("Intermediate files: "):
            self.work_dir = line[len("Intermediate files: "):].strip()
        m = _CONVERT_BATCH.match(line)
        if m:
            self.stage = "Converting (oiiotool)"
            self.watch_dir, self.watch_prefix = self.work_dir, ""
            self.watch_base = self.converted
            self.batch_frames = int(m.group(2)) - int(m.group(1)) + 1
        elif line.startswith("Writing ") and " (" in line:
            target = line[len("Writing "):line.rindex(" (")]
            self.stage = "Writing frames (oiiotool)"
            self.watch_dir = str(Path(target).parent)
            self.watch_prefix = Path(target).name.split("#")[0]
            self.watch_base = 0
            self.batch_frames = self.frames
        elif line.startswith("Movie: unpacking"):
            self.stage = "Unpacking the movie"
        elif line.startswith("Converting ") and "one by one" in line:
            self.stage = "Converting (oiiotool)"
            self.watch_dir = None
        elif line.startswith("Encoding "):
            self.stage = "Encoding (ffmpeg)"
            self.watch_dir = None
            self._set(self.convert_share if self.convert_share else 0.05)
        elif line.startswith("Done: "):
            self.output = line[len("Done: "):]
            self.stage = "Finishing"
            self.watch_dir = None
        m = _COUNT.match(line)
        if m and self.stage.startswith("Converting"):
            self.converted = max(self.converted, self.watch_base + int(m.group(1)))
            self._convert_progress(self.converted)
        return before != (self.stage, self.value)

    def poll(self):
        """Count the frames written in the watched folder since the run started."""
        if not self.watch_dir:
            return False
        count = 0
        try:
            with os.scandir(self.watch_dir) as it:
                for entry in it:
                    name = entry.name
                    if not name.lower().endswith(_IMAGE_EXTS) or not name.startswith(self.watch_prefix):
                        continue
                    try:
                        if entry.stat().st_mtime >= self.started - 1:
                            count += 1
                    except OSError:
                        pass
        except OSError:
            return False
        count = min(count, self.batch_frames or count)
        before = self.value
        self.converted = max(self.converted, self.watch_base + count)
        if self.stage.startswith("Writing"):
            if self.frames:
                self._set(min(1.0, self.converted / float(self.frames)))
        else:
            self._convert_progress(self.converted)
        return self.value != before


# --- settings: vfx-transcode.ini [process] and [deadline] -------------------------------------

FARM_EXECUTABLE = r"C:\_GFX_library\utils\vfx-transcode\vfx-transcode.exe"
FARM_DEPARTMENT = "IO"
DEADLINE_DEFAULTS = OrderedDict([
    ("command", ""),                  # deadlinecommand; "" = $DEADLINE_PATH, the usual folder, the PATH
    ("executable", FARM_EXECUTABLE),  # vfx-transcode on the render nodes ("vfx-transcode on the farm")
    ("pool", ""), ("secondary_pool", ""), ("group", ""),
    ("priority", "50"), ("machine_limit", "0"), ("department", FARM_DEPARTMENT),
    ("concurrent_tasks", "1"),
])
DEADLINE_KEY_ALIASES = {"deadlinecommand": "command"}       # older vfx-transcode.ini files
WEB_SERVICE_SECTION = "deadline_web_service"
WEB_SERVICE_DEFAULTS = OrderedDict([
    ("url", ""),                      # e.g. http://192.168.100.20:8081; "" = use deadlinecommand
    ("require_authentication", "false"),
    ("verify_ssl", "true"),
    ("user", ""),
    ("password", ""),                 # ${DEADLINE_WEB_PASSWORD}: from the environment
])
_UNSET_VAR = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[^%\s]+%")


def expand_value(value):
    """Environment variables in an ini value: ${NAME}, $NAME, %NAME%. One that is not set
    leaves nothing (not the literal ${NAME})."""
    return _UNSET_VAR.sub("", os.path.expandvars(value))


def read_ini_section(section, defaults, base=None, aliases=None):
    """The section's keys from vfx-transcode.ini next to the application, over defaults."""
    out = OrderedDict(defaults)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(studio.ini_path(base), encoding="utf-8-sig") as fh:
            parser.read_file(fh)
    except (OSError, UnicodeDecodeError, configparser.Error):
        return out
    if parser.has_section(section):
        for key, value in parser.items(section):
            key = (aliases or {}).get(key, key)
            if key in out and value.strip():
                out[key] = expand_value(value.strip().strip('"'))
    return out


def _flag(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def deadline_command(configured=""):
    """deadlinecommand: as configured, else from $DEADLINE_PATH, else the usual install
    folder, else the PATH."""
    if configured.strip():
        return configured.strip().strip('"')
    exe = "deadlinecommand.exe" if os.name == "nt" else "deadlinecommand"
    root = os.environ.get("DEADLINE_PATH", "")
    if root and Path(root, exe).is_file():
        return str(Path(root, exe))
    if os.name == "nt":
        for version in ("10", "11"):
            path = Path(r"C:\Program Files\Thinkbox\Deadline{}\bin".format(version), exe)
            if path.is_file():
                return str(path)
    return exe


@dataclass
class DeadlineSettings:
    command: str = ""
    executable: str = FARM_EXECUTABLE
    pool: str = ""
    secondary_pool: str = ""
    group: str = ""
    priority: int = 50
    machine_limit: int = 0
    department: str = FARM_DEPARTMENT
    concurrent_tasks: int = 1
    batch_name: str = ""
    comment: str = ""
    suspended: bool = False
    # [deadline_web_service]: submit over HTTP instead of deadlinecommand when url is set
    web_url: str = ""
    web_auth: bool = False
    web_verify_ssl: bool = True
    web_user: str = ""
    web_password: str = ""            # never stored by the app

    PRIVATE = ("web_password", "batch_name", "comment")      # not remembered per user

    @classmethod
    def from_ini(cls, base=None):
        s = cls()
        for key, value in read_ini_section("deadline", DEADLINE_DEFAULTS, base, DEADLINE_KEY_ALIASES).items():
            current = getattr(s, key)
            if key in ("executable", "department") and not str(value).strip():
                continue                  # empty in the ini: the built-in default
            try:
                setattr(s, key, int(value) if isinstance(current, int) and not isinstance(current, bool)
                        else value)
            except ValueError:
                pass
        web = read_ini_section(WEB_SERVICE_SECTION, WEB_SERVICE_DEFAULTS, base)
        s.web_url = web["url"].rstrip("/")
        s.web_auth = _flag(web["require_authentication"])
        s.web_verify_ssl = _flag(web["verify_ssl"])
        s.web_user, s.web_password = web["user"], web["password"]
        return s

    @property
    def via(self):
        return "web service {}".format(self.web_url) if self.web_url else "deadlinecommand"


def deadline_info(job, settings, tool=None):
    """(job info, plugin info) dicts: one task running vfx-transcode with the CommandLine
    plugin. The super-preset is written out in the arguments."""
    exe = settings.executable.strip() or (tool or vfx_transcode_command())[0]
    pre = [] if settings.executable.strip() or tool else vfx_transcode_command()[1:]
    args = pre + job.args(inline=True)
    info = OrderedDict([
        ("Plugin", "CommandLine"),
        ("Name", "vfx-transcode {} [{}]".format(job.name or job.pattern, job.super_preset)),
        ("BatchName", settings.batch_name),
        ("Comment", settings.comment),
        ("Department", settings.department),
        ("Pool", settings.pool), ("SecondaryPool", settings.secondary_pool),
        ("Group", settings.group),
        ("Priority", str(settings.priority)),
        ("MachineLimit", str(settings.machine_limit)),
        ("ConcurrentTasks", str(settings.concurrent_tasks)),
        ("Frames", "0"), ("ChunkSize", "1"),
        ("InitialStatus", "Suspended" if settings.suspended else "Active"),
    ])
    if job.predicted:
        out = Path(job.predicted)
        info["OutputDirectory0"] = str(out.parent)
        info["OutputFilename0"] = out.name
    plugin = OrderedDict([("Executable", exe), ("Arguments", quote(args)),
                          ("StartupDirectory", ""), ("ShellExecute", "False")])
    drop_empty = lambda d: OrderedDict((k, v) for k, v in d.items() if v != "")
    return drop_empty(info), drop_empty(plugin)


def deadline_files(job, settings, tool=None):
    """(job info, plugin info) as the text files deadlinecommand takes."""
    text = lambda d: "".join("{}={}\n".format(k, v) for k, v in d.items())
    return tuple(text(d) for d in deadline_info(job, settings, tool))


_JOB_ID = re.compile(r"JobID=(\S+)")


def _web_request(settings, path, data=None, timeout=60):
    """A request to the Deadline Web Service: (url, request, ssl context)."""
    import base64
    import ssl
    import urllib.request
    url = settings.web_url.rstrip("/") + path
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json"})
    if settings.web_auth:
        if not settings.web_user:
            raise TranscodeError("The Deadline web service needs a user: set user in "
                                 "vfx-transcode.ini [{}].".format(WEB_SERVICE_SECTION))
        token = base64.b64encode("{}:{}".format(settings.web_user, settings.web_password)
                                 .encode("utf-8")).decode("ascii")
        request.add_header("Authorization", "Basic " + token)
    context = None
    if url.lower().startswith("https") and not settings.web_verify_ssl:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return url, request, context


def _names(data):
    """A list of names from the web service (["none", "cpu"]) or deadlinecommand output."""
    if isinstance(data, str):
        items = data.splitlines()
    elif isinstance(data, dict):            # {"Pools": [...]} or similar
        items = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        items = data or []
    out = []
    for item in items:
        name = (item.get("Name") or item.get("name") or "") if isinstance(item, dict) else str(item)
        name = name.strip()
        if name and name not in out and not name.lower().startswith(("error", "usage")):
            out.append(name)
    return out


def deadline_lists(settings, timeout=8):
    """(pools, groups) from Deadline: the web service when it has a url, else deadlinecommand
    -Pools / -Groups. Raises TranscodeError when Deadline can't be asked."""
    if settings.web_url:
        import urllib.error
        import urllib.request
        found = []
        for path in ("/api/pools", "/api/groups"):
            url, request, context = _web_request(settings, path)
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                    text = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                raise TranscodeError("the web service answered HTTP {}{}".format(
                    exc.code, " (check user and password)" if exc.code in (401, 403) else ""))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise TranscodeError("the web service at {} can't be reached: {}".format(
                    settings.web_url, getattr(exc, "reason", exc)))
            try:
                found.append(_names(json.loads(text)))
            except ValueError:
                found.append(_names(text))
        return found[0], found[1]
    found = []
    for flag in ("-Pools", "-Groups"):
        cmd = [deadline_command(settings.command), flag]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            raise TranscodeError("deadlinecommand not found ({})".format(cmd[0]))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TranscodeError("deadlinecommand {} failed: {}".format(flag, exc))
        if result.returncode != 0:
            raise TranscodeError("deadlinecommand {} failed with exit code {}".format(flag, result.returncode))
        found.append(_names(result.stdout.decode("utf-8", errors="replace")))
    return found[0], found[1]


# Deadline's job states (the web service's "Stat" numbers)
DEADLINE_STATES = {0: "unknown", 1: "active", 2: "suspended", 3: "completed", 4: "failed",
                   5: "deleted", 6: "pending"}


def deadline_job_status(settings, job_id, timeout=15):
    """The Deadline job's state, lower case: completed, failed, active (queued or rendering),
    suspended, pending, or "not found". Raises TranscodeError when Deadline can't be asked."""
    if settings.web_url:
        import urllib.error
        import urllib.parse
        import urllib.request
        url, request, context = _web_request(settings, "/api/jobs?JobID=" + urllib.parse.quote(job_id))
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return "not found"
            raise TranscodeError("the web service answered HTTP {}".format(exc.code))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TranscodeError("the web service at {} can't be reached: {}".format(
                settings.web_url, getattr(exc, "reason", exc)))
        try:
            data = json.loads(text)
        except ValueError:
            return "unknown"
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return "not found"
        stat = data.get("Stat", data.get("Status"))
        if isinstance(stat, int) or (isinstance(stat, str) and stat.isdigit()):
            return DEADLINE_STATES.get(int(stat), "unknown")
        return str(stat or "unknown").strip().lower()
    cmd = [deadline_command(settings.command), "-GetJob", job_id]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise TranscodeError("deadlinecommand not found ({})".format(cmd[0]))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscodeError("deadlinecommand -GetJob failed: {}".format(exc))
    text = result.stdout.decode("utf-8", errors="replace")
    m = re.search(r"^\s*Status\s*=\s*(\w+)", text, re.M | re.I)
    if m:
        return m.group(1).lower()
    return "not found" if result.returncode != 0 or "error" in text.lower() else "unknown"


def output_exists(job):
    """Whether the job's output is on disk: the movie, or at least one frame of the sequence."""
    path = result_path(job, job.result)
    return bool(path) and os.path.isfile(path)


def farm_check(job, settings):
    """(status, message) for a job on the farm: Done on farm when Deadline says completed and
    the output is there; Failed when Deadline says failed, or completed without the output;
    else still On farm, with what Deadline says."""
    state = deadline_job_status(settings, job.deadline_id)
    if state == "completed":
        if output_exists(job):
            return FARM_DONE, "done on farm (Deadline job {})".format(job.deadline_id)
        return FAILED, "Deadline says completed, but the output is not there: {}".format(
            job.predicted or job.output)
    if state == "failed":
        return FAILED, "failed on Deadline (job {}): see its log in the Deadline Monitor".format(job.deadline_id)
    if state in ("not found", "deleted"):
        return SUBMITTED, "Deadline job {} not found (deleted?)".format(job.deadline_id)
    return SUBMITTED, "on Deadline: {}".format(state)


def submit_to_deadline(job, settings, work_dir, tool=None, timeout=120):
    """Submit one job, through the web service when [deadline_web_service] has a url, else
    with deadlinecommand. Returns the Deadline job id. Raises TranscodeError."""
    if settings.web_url:
        return submit_web_service(job, settings, tool, timeout)
    job_info, plugin_info = deadline_files(job, settings, tool)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    ji, pi = work_dir / "{}_job_info.job".format(job.id), work_dir / "{}_plugin_info.job".format(job.id)
    ji.write_text(job_info, encoding="utf-8")
    pi.write_text(plugin_info, encoding="utf-8")
    cmd = [deadline_command(settings.command), str(ji), str(pi)]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise TranscodeError("deadlinecommand not found ({}): set it in the Deadline settings or "
                             "in vfx-transcode.ini [deadline] command.".format(cmd[0]))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TranscodeError("deadlinecommand failed: {}".format(exc))
    text = result.stdout.decode("utf-8", errors="replace")
    m = _JOB_ID.search(text)
    if result.returncode != 0 or not m:
        raise TranscodeError("Deadline did not take the job:\n" + text.strip()[-1500:])
    return m.group(1)


def submit_web_service(job, settings, tool=None, timeout=60):
    """POST the job to the Deadline Web Service (/api/jobs). Basic authentication when the
    ini asks for it; verify_ssl false accepts any certificate (https only)."""
    import urllib.error
    import urllib.request
    job_info, plugin_info = deadline_info(job, settings, tool)
    body = json.dumps({"JobInfo": job_info, "PluginInfo": plugin_info, "AuxFiles": [],
                       "IdOnly": True}).encode("utf-8")
    url, request, context = _web_request(settings, "/api/jobs", body)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()[:800]
        hint = " (check user and password)" if exc.code in (401, 403) else ""
        raise TranscodeError("The Deadline web service refused the job: HTTP {}{}\n{}".format(
            exc.code, hint, detail))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TranscodeError("The Deadline web service at {} can't be reached: {}".format(
            settings.web_url, getattr(exc, "reason", exc)))
    try:
        data = json.loads(text)
    except ValueError:
        data = text.strip().strip('"')
    job_id = data.get("_id") if isinstance(data, dict) else (data if isinstance(data, str) else "")
    if not job_id:
        raise TranscodeError("The Deadline web service gave no job id:\n" + text[:800])
    return job_id


def local_paths(paths):
    """The paths a render node can't see: on a local drive letter that is not a mapped network
    drive (Windows), so the user can be warned before submitting."""
    out = []
    for path in paths:
        win = PureWindowsPath(path)
        if win.drive and not win.drive.startswith("\\\\") and os.name == "nt" and not _is_network_drive(win.drive):
            out.append(path)
    return out


def _is_network_drive(drive):
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4     # DRIVE_REMOTE
    except Exception:
        return True                     # can't tell: don't warn


def kill_tree(pid):
    """Stop vfx-transcode and the oiiotool / ffmpeg it started (Windows does not stop the
    children with the parent)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass


def result_path(job, done_line=""):
    """The output to open: from vfx-transcode's "Done:" line, else the predicted path. A
    sequence ("D:/out/sh010.####.exr (1001-1100)"): its first frame if it exists, else its
    folder."""
    text = (done_line or job.predicted or "").strip()
    if job.output_type != "movie":
        text = re.sub(r" \([^()]*\)$", "", text)
        folder, name = os.path.split(text)
        prefix = name.split("#")[0]
        try:
            frames = sorted(f for f in os.listdir(folder) if prefix and f.startswith(prefix)
                            and f.lower().endswith("." + job.output_type))
        except OSError:
            frames = []
        return os.path.join(folder, frames[0]) if frames else folder
    return text


def reveal(path):
    """Show a file (selected) or a folder in the system's file browser."""
    path = os.path.normpath(path)
    if os.name == "nt":
        args = ["explorer", "/select,", path] if os.path.isfile(path) else ["explorer", path]
        subprocess.Popen(args)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


def format_seconds(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}:{:02d}".format(seconds // 60, seconds % 60)
    return "{}:{:02d}:{:02d}".format(seconds // 3600, seconds // 60 % 60, seconds % 60)
