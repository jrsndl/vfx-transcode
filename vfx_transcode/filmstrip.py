"""Filmstrip: one jpg of evenly spaced frames, laid out as a grid of equal tiles.

Display-referred, like the thumbnail. Each frame is fitted into its tile with black bars; cells
without a frame are black. Where the tiles come from (-filmstrip_source):

  source         the source frames, with the movie's processing; works without a movie.
                 EXR: one oiiotool run per tile (in parallel), then oiiotool --mosaic.
                 Other input: one ffmpeg run (concat list, scale, pad, tile).
  intermediates  the frames oiiotool converted for the movie (EXR with a movie or
                 -skipffmpeg): no second colour conversion. Other input: as source.
  movie          the encoded movie, by ffmpeg: burn-ins, letterbox and codec included.

The slate is left out unless asked for (slate): then it takes the first tile.
"""

import concurrent.futures
import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from .errors import TranscodeError, ToolError
from .thumbnails import _int, _without_depth, jpeg_qscale
from .tools import run_tool

log = logging.getLogger(__name__)

NO_FILMSTRIP = "none"
DEFAULT_TILES = 24
DEFAULT_TILE = (480, 270)
MAX_TILES = 1000
SOURCES = ("source", "intermediates", "movie")
PRESET_FIELDS = ("notes", "tiles", "tile_width", "tile_height", "columns", "rows", "quality",
                 "source", "slate")
TILE_EXT = ".tif"            # 8-bit tiles: fast to write and read back


@dataclass(frozen=True)
class FilmstripPreset:
    name: str
    tiles: int = 0             # 0 = columns x rows when both are set, else 24
    tile_width: int = DEFAULT_TILE[0]
    tile_height: int = DEFAULT_TILE[1]
    columns: int = 0           # 0 = follow the tile count (rows 0 too: one row)
    rows: int = 0              # 0 = as many as the tiles need
    quality: int = 90          # jpg, 1-100
    source: str = "source"     # source | intermediates | movie
    slate: bool = False        # the slate as the first tile
    notes: str = ""

    ext = "jpg"

    def layout(self):
        """(tiles, columns, rows), checked."""
        columns, rows = self.columns, self.rows
        tiles = self.tiles or (columns * rows if columns and rows else DEFAULT_TILES)
        if not columns and not rows:
            columns, rows = tiles, 1
        elif not rows:
            rows = math.ceil(tiles / columns)
        elif not columns:
            columns = math.ceil(tiles / rows)
        if columns * rows < tiles:
            raise TranscodeError("Filmstrip: a {}x{} grid has {} cells, too few for {} tiles."
                                 .format(columns, rows, columns * rows, tiles))
        return tiles, columns, rows

    def size(self):
        _tiles, columns, rows = self.layout()
        return columns * self.tile_width, rows * self.tile_height

    def describe(self):
        tiles, columns, rows = self.layout()
        width, height = self.size()
        return "{} tiles of {}x{}, {}x{} grid ({}x{}), jpg quality {}, from the {}{}".format(
            tiles, self.tile_width, self.tile_height, columns, rows, width, height, self.quality,
            self.source, ", slate first" if self.slate else "")


# --- parsing ---

def parse_size(value, where, allow_zero=False):
    """"480x270" -> (480, 270); with allow_zero "6x" / "6x0" / "x2" leave a side open."""
    text = str(value).strip().lower().replace("*", "x")
    match = re.fullmatch(r"(\d*)\s*x\s*(\d*)", text)
    if match:
        a, b = (int(g) if g else 0 for g in match.groups())
        if (allow_zero and (a or b)) or (a and b):
            if a <= 65536 and b <= 65536:
                return a, b
    raise TranscodeError('{}: use WIDTHxHEIGHT{}, got "{}".'.format(
        where, " (0 or nothing leaves a side open, e.g. 6x)" if allow_zero else ", e.g. 480x270",
        value))


def parse_tiles(value, where="-filmstrip_tiles"):
    try:
        number = int(str(value).strip())
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_TILES:
        raise TranscodeError('{}: a whole number from 1 to {}, got "{}".'.format(where, MAX_TILES, value))
    return number


def parse_quality(value, where="-filmstrip_quality"):
    try:
        number = int(str(value).strip())
    except ValueError:
        number = 0
    if not 1 <= number <= 100:
        raise TranscodeError('{}: a whole number from 1 to 100, got "{}".'.format(where, value))
    return number


def parse_source(value, where="-filmstrip_source"):
    text = str(value).strip().lower()
    if text not in SOURCES:
        raise TranscodeError('{}: use {}, got "{}".'.format(where, ", ".join(SOURCES), value))
    return text


def preset_from_json(name, data, path):
    where = '{}: filmstrip preset "{}"'.format(path, name)
    if name == NO_FILMSTRIP:
        raise TranscodeError('{}: "{}" is reserved.'.format(path, name))
    if not isinstance(data, dict):
        raise TranscodeError("{} must be an object.".format(where))
    unknown = sorted(set(data) - set(PRESET_FIELDS))
    if unknown:
        raise TranscodeError("{}: unknown field(s) {}; use {}.".format(
            where, ", ".join(unknown), ", ".join(PRESET_FIELDS)))
    slate = data.get("slate", False)
    if not isinstance(slate, bool):
        raise TranscodeError("{}: slate must be true or false, got {!r}.".format(where, slate))
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = " ".join(str(n) for n in notes)
    preset = FilmstripPreset(
        name=name, tiles=_int(data, "tiles", 0, MAX_TILES, where, 0),
        tile_width=_int(data, "tile_width", 1, 65536, where, DEFAULT_TILE[0]),
        tile_height=_int(data, "tile_height", 1, 65536, where, DEFAULT_TILE[1]),
        columns=_int(data, "columns", 0, MAX_TILES, where, 0),
        rows=_int(data, "rows", 0, MAX_TILES, where, 0),
        quality=_int(data, "quality", 1, 100, where, 90),
        source=parse_source(data.get("source", "source"), where + ": source"),
        slate=slate, notes=str(notes))
    try:
        preset.layout()
    except TranscodeError as exc:
        raise TranscodeError("{}: {}".format(where, exc))
    return preset


def presets_from_json(data, path):
    if not isinstance(data, dict):
        raise TranscodeError('{}: "filmstrip_presets" must be an object.'.format(path))
    return OrderedDict((name, preset_from_json(name, value, path)) for name, value in data.items())


def apply_options(preset, tiles=None, tile=None, grid=None, quality=None, source=None, slate=None):
    """The command line over the preset. A grid without -filmstrip_tiles sets the tile count
    to its cells; -filmstrip_tiles without a grid makes one row unless the preset has a grid."""
    changes = {}
    if tile is not None:
        changes["tile_width"], changes["tile_height"] = parse_size(tile, "-filmstrip_tile")
    if grid is not None:
        changes["columns"], changes["rows"] = parse_size(grid, "-filmstrip_grid", allow_zero=True)
        if tiles is None:
            changes["tiles"] = 0
    if tiles is not None:
        changes["tiles"] = parse_tiles(tiles)
        if grid is None and preset.columns and preset.rows:
            # the preset's full grid would not follow the new count; keep its columns
            changes["rows"] = 0
    if quality is not None:
        changes["quality"] = parse_quality(quality)
    if source is not None:
        changes["source"] = parse_source(source)
    if slate is not None:
        changes["slate"] = bool(slate)
    result = replace(preset, **changes)
    result.layout()                                      # fail early on a too small grid
    return result


# --- which frames ---

def choose_frames(frames, tiles):
    """tiles frames, evenly spaced over the frames, first and last included. Fewer frames than
    tiles: all of them (the rest of the cells stay black)."""
    frames = sorted(frames)
    if tiles <= 0:
        return []
    if len(frames) <= tiles:
        return frames
    if tiles == 1:
        return [frames[(len(frames) - 1) // 2]]
    step = (len(frames) - 1) / (tiles - 1)
    return [frames[int(round(i * step))] for i in range(tiles)]


# --- oiiotool ---

def channel_args(alpha=False, mono=False):
    """Make a tile RGB: drop alpha (jpg has none), repeat mono, so tiles and black cells fit."""
    if alpha:
        return ["--ch", "0,1,2"]
    if mono:
        return ["--ch", "0,0,0"]
    return []


def oiiotool_tile_command(oiiotool, src, dst, mid_args, preset, par=Fraction(1)):
    """One tile: mid_args (processing and channels), square pixels, fitted into the tile with
    black bars, 8-bit."""
    args = list(mid_args)
    if par != 1:
        args += ["--attrib:type=float", "PixelAspectRatio", "{:g}".format(float(par)),
                 "--pixelaspect", "1"]
    args += ["--fit:pad=1", "{}x{}".format(preset.tile_width, preset.tile_height), "-d", "uint8"]
    return [oiiotool, "-i", Path(src).as_posix()] + args + ["-o", Path(dst).as_posix()]


def source_tile_args(movie_args, alpha=False, mono=False):
    """A source EXR tile: the movie's processing without its intermediate bit depth."""
    return _without_depth(movie_args) + channel_args(alpha, mono)


def oiiotool_mosaic_command(oiiotool, tiles, dst, preset):
    """tiles: one file per cell in reading order, None for a black cell. Plain file names keep
    the command short (run it in the tiles' folder)."""
    _count, columns, rows = preset.layout()
    cells = list(tiles) + [None] * (columns * rows - len(tiles))
    cmd = [oiiotool]
    black = "{}x{}".format(preset.tile_width, preset.tile_height)
    for tile in cells:
        if tile is None:
            cmd += ["--pattern", "constant:color=0,0,0", black, "3"]
        else:
            cmd += ["-i", Path(tile).as_posix()]
    return cmd + ["--mosaic", "{}x{}".format(columns, rows), "-d", "uint8",
                  "--compression", "jpeg:{}".format(preset.quality), "-o", Path(dst).as_posix()]


def render_oiiotool(oiiotool, jobs, dst, preset, work_dir, par, workers=1, verbose=0, keep=False,
                    name="filmstrip"):
    """jobs: [(label, source file, mid_args)] in cell order. The tiles are made in parallel in
    work_dir, then put together. A tile that fails stays black, with a warning. Returns the
    labels of the failed tiles."""
    work_dir = Path(work_dir)
    stem = re.sub(r"[^\w.-]", "_", name or "filmstrip")
    targets = [work_dir / "{}.fs{:04d}{}".format(stem, i, TILE_EXT) for i in range(len(jobs))]

    def make(i):
        _label, src, mid = jobs[i]
        run_tool(oiiotool_tile_command(oiiotool, src, targets[i], mid, preset, par),
                 "oiiotool (filmstrip tile)", verbose)

    failed = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(make, i): i for i in range(len(jobs))}
            for future in concurrent.futures.as_completed(futures):
                i = futures[future]
                try:
                    future.result()
                except ToolError as exc:
                    log.warning("Filmstrip: %s failed, its tile stays black: %s", jobs[i][0], exc)
                if not targets[i].is_file():
                    failed.append(i)
        cells = [t.name if t.is_file() else None for t in targets]
        if not any(cells):
            raise TranscodeError("Filmstrip: no tile could be made.")
        run_tool(oiiotool_mosaic_command(oiiotool, cells, Path(dst).resolve(), preset),
                 "oiiotool (filmstrip)", verbose, cwd=str(work_dir))
    finally:
        if not keep:
            for t in targets:
                try:
                    t.unlink()
                except OSError:
                    pass
    return [jobs[i][0] for i in sorted(failed)]


# --- ffmpeg ---

def _fit_filters(preset, par):
    from .ffmpeg import make_filter
    w, h = preset.tile_width, preset.tile_height
    steps = []
    if par != 1:
        steps.append(make_filter("scale", [("w", "trunc(iw*{}/{}/2)*2".format(
            par.numerator, par.denominator)), ("h", "ih")]))
    return steps + [
        make_filter("scale", [("w", str(w)), ("h", str(h)), ("force_original_aspect_ratio", "decrease")]),
        make_filter("pad", [("w", str(w)), ("h", str(h)), ("x", "(ow-iw)/2"), ("y", "(oh-ih)/2"),
                            ("color", "black")]),
        make_filter("setsar", [("sar", "1")])]


def _tile_filter(preset):
    from .ffmpeg import make_filter
    _count, columns, rows = preset.layout()
    # the tile filter fills the cells it gets no frame for with black
    return make_filter("tile", [("layout", "{}x{}".format(columns, rows)), ("color", "black")])


def _jpg_out(preset, dst):
    return ["-frames:v", "1", "-update", "1", "-q:v", str(jpeg_qscale(preset.quality)),
            Path(dst).as_posix()]


def ffmpeg_files_command(ffmpeg, list_file, dst, preset, par=Fraction(1)):
    """Image files (an ffconcat list, in cell order): fitted, padded, tiled in one run."""
    from .ffmpeg import make_filter
    vf = ",".join(_fit_filters(preset, par) + [make_filter("format", [("pix_fmts", "rgb24")]),
                                               _tile_filter(preset)])
    return ([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i",
             Path(list_file).as_posix(), "-vf", vf] + _jpg_out(preset, dst))


def ffmpeg_movie_command(ffmpeg, movie, indices, dst, preset, par=Fraction(1)):
    """Frames of the encoded movie (0-based indices, in cell order) in one run. The movie's
    YUV is read as Rec.709, as written."""
    from .ffmpeg import make_filter
    select = make_filter("select", [("expr", "+".join("eq(n,{})".format(i) for i in indices))])
    to_rgb = [make_filter("scale", [("in_color_matrix", "bt709")]),
              make_filter("format", [("pix_fmts", "rgb24")])]
    vf = ",".join([select] + to_rgb + _fit_filters(preset, par) + [_tile_filter(preset)])
    return ([ffmpeg, "-hide_banner", "-y", "-i", Path(movie).as_posix(), "-an", "-vf", vf]
            + _jpg_out(preset, dst))
