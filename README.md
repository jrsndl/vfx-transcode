# vfx-transcode

Turns an image sequence into a review movie. OpenEXRs go through `oiiotool`, which picks the channels, applies the OCIO colour transform and resizes. `ffmpeg` then encodes them with burn-ins, timecode and metadata. Other formats (PNG, JPG, DPX, TIFF) go straight to ffmpeg.

**All options are documented in [docs/OPTIONS.md](docs/OPTIONS.md).**

## Quick start

```
python vfx-transcode.py -i D:/shots/sh010/sh010_comp.1001.exr -burn_preset review
python vfx-transcode.py -i D:/shots/sh010/sh010_comp.1001.exr -preset prores422hq -timecode_source exr
python vfx-transcode.py --help
```

Point `-i` at any frame; the whole sequence is found automatically. The movie is written next to the input unless `-o` says otherwise.

## Features

- Automatic sequence detection, including holes, which are filled by holding the previous frame.
- Automatic channel choice (configurable order in `presets.json`), multi-part EXRs, and depth pictures from deep EXRs.
- Fast EXR conversion: one `oiiotool --parallel-frames` call per run of frames without a hole, falling back to one file at a time, with intermediate files in your local user temp folder (or next to the sources).
- Encode and burn-in presets in an editable [`presets.json`](presets.json); the encode presets are based on the [ASWF Encoding Guidelines](https://github.com/AcademySoftwareFoundation/EncodingGuidelines).
- OCIO colour conversion by input/output colourspace, or by display/view (ACES 1.3+ configs, built-in `ocio://` configs), with `-ocio_info` to list the names.
- OIIO transform presets: resize/crop rules chosen per input (size, pixel aspect, header values), first matching rule wins.
- Pixel aspect ratio from an argument or the header; output with the same pixel aspect (tagged), reformatted to square pixels, or to a given pixel aspect.
- Frame rate and timecode from an argument or the EXR header, with a switch for which one wins; timecode is written as a real timecode track in mov/mp4/mxf.
- Metadata: your own `KEY=VALUE` tags, optional copying from the EXR header, and metadata presets (`review`, `conform`, `client`, `delivery`, `archive`); `delivery` writes the tags delivery specs ask for (source frame rate, frame range, slate length, display colour, burn-ins present).
- Keywords from the sequence name: version, name parts, your own regex tokens (`{shot}`, `{task}` …), and per-project smart token presets (conditions, regex replacement, case change) for burn-ins, slate, metadata, output paths and transform conditions.
- Checks before converting that ffmpeg has the filters the movie needs (e.g. `drawtext` for burn-ins and the slate).
- Reel name for conforming (explicit, or from the EXR `owner` or any other attribute), written into the mov timecode track and mxf source package.
- Burn-ins in six places (corners plus top/bottom centre) and a centred watermark, with name, frame counter, frame range, running timecode, fps and date; colour, opacity, size and black box per preset or per place.
- Letterbox or pillarbox bars for a target ratio, optionally see-through and with an edge line; burn-ins stay on top.
- Alpha: dropped by default, or kept into ProRes 4444 (or any alpha codec), straight or premultiplied.
- A tuner app (PySide6) to tune presets by eye on a test frame, with compare image and copy-paste output: see [docs/TUNER.md](docs/TUNER.md).
- Thumbnails (jpg/png) with the same look as the movie, from the middle, first, last or busiest frame; or thumbnails only.
- VFX slate: one frame before the pictures (frame and timecode start − 1), same size and pixel aspect as the pictures, with a shot thumbnail and rows from presets, header values or the command line.

## Layout

```
vfx-transcode.py      entry script (also used by the PyInstaller spec)
vfx_transcode/        the package; python -m vfx_transcode works too
vfx-transcode-tuner.py  the tuner app (needs PySide6); vfx_tuner/ is its package
presets.json          encode, burn-in, metadata and slate presets, channel preferences
docs/OPTIONS.md       options reference
docs/TUNER.md         the tuner
tests/                unit tests
test_conversion.bat   end-to-end test on the sample media in ./media
oiio/ ffmpeg/ ocio/ fonts/   tools and data used by default; only their README is in git
media/                sample sequences for test_conversion.bat (not in git)
```

Needs Python 3.9 or newer; the tool itself uses only the standard library. Tools are looked up relative to the application folder first, then on the `PATH`.

## Development

The environment is managed with [uv](https://docs.astral.sh/uv/). vfx-transcode itself needs nothing
beyond the standard library; the environment adds pytest and PyInstaller, and PySide6 for the tuner.

```
uv sync --extra tuner                         # makes .venv: pytest, pyinstaller, PySide6
uv run vfx-transcode -i D:/shots/sh010.1001.exr   # or: uv run python vfx-transcode.py ...
uv run vfx-transcode-tuner                    # the tuner; or: uv run python vfx-transcode-tuner.py
uv run pytest                                 # unit tests
test_conversion.bat                           # end-to-end on ./media, logs in media\_test_output
build_exe.bat                                 # both executables into dist\vfx-transcode
```

`build_exe.bat` runs `uv sync --extra tuner`, then PyInstaller twice: `vfx-transcode.exe` as a single
console file (from `vfx-transcode.spec`) and `vfx-transcode-tuner.exe` as a windowed one-folder build
(its `_internal` folder must stay next to it). Both land in `dist\vfx-transcode\` with `presets.json`,
`docs`, and the `ffmpeg`, `oiio`, `ocio` and `fonts` folders; `build_exe.bat notools` skips those four
and leaves only their READMEs. The frozen tuner runs the `vfx-transcode.exe` beside it, and both look
for `./presets.json` and the tool folders next to the executables, so the folder can be copied anywhere.

`uv sync` without `--extra tuner` leaves PySide6 out. The Python version comes from `.python-version`;
`uv python pin 3.11` changes it. Commit `uv.lock` so everyone gets the same versions.
