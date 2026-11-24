# vfx-transcode — options reference

- [How a run works](#how-a-run-works)
- [Conventions](#conventions)
- [Input and output](#input-and-output)
- [Movie input](#movie-input)
- [Trim](#trim)
- [Image sequence output](#image-sequence-output)
- [Super-presets](#super-presets)
- [Frame rate](#frame-rate)
- [Pixel aspect ratio](#pixel-aspect-ratio)
- [Colour](#colour)
- [Tools](#tools)
- [EXR conversion (oiiotool)](#exr-conversion-oiiotool)
- [Missing and failed frames](#missing-and-failed-frames)
- [Deep EXRs](#deep-exrs)
- [Timecode](#timecode)
- [Metadata](#metadata)
- [Reel name](#reel-name)
- [Burn-ins](#burn-ins)
- [Slate](#slate)
- [Letterbox](#letterbox)
- [Alpha](#alpha)
- [Thumbnail](#thumbnail)
- [Filmstrip](#filmstrip)
- [Preview](#preview)
- [Encoding (ffmpeg)](#encoding-ffmpeg)
- [Presets file](#presets-file)
- [Logging and exit codes](#logging-and-exit-codes)
- [Examples](#examples)

## How a run works

1. `-i` names any one file of a sequence. The tool finds the other frames next to it and reports any holes.
2. **EXR input.** oiiotool reads the header of the sequence's first frame, whichever frame `-i` points at. The channels, frame rate, timecode, reel name and `{meta:…}` values all come from that header. It then converts every frame to an intermediate file (PNG by default) in a temporary folder, by default in your user temp folder (see `-temp_location`). The conversion applies the OCIO colour transform and resizes the image.
   **Other input** (PNG, JPG, DPX, TIFF, …) skips this step. ffmpeg reads those frames directly.
3. With a [slate](#slate), ffmpeg draws one extra frame, matching the intermediate frames, and puts it before the first picture.
4. ffmpeg encodes the frames with the chosen preset and adds burn-ins, timecode and metadata.
5. The intermediate files are deleted unless `-temp_keep 1` is set.

## Conventions

- Options start with a single dash (`-width 1280`), as in the original script, so existing .bat files keep working.
- **Switches** (shown as `0|1` below) accept `1/0`, `true/false`, `yes/no` and `on/off`. A switch given on its own means on: `-temp_keep` is the same as `-temp_keep 1`.
- **Paths starting with `./` or `../`** are relative to the application folder (the repository root, or the folder of the built .exe), not to the folder you run the command from. Any other path is used as given.
- Forward slashes work everywhere, including on Windows.
- Quote any value that contains spaces: `-burn_tl "Shot 010"`.

## Input and output

| Option | Default | Description |
|---|---|---|
| `-i PATH` | *(required)* | Any file of the input sequence, or a movie file (see [Movie input](#movie-input)). See the sequence rules below. |
| `-o PATH` | `<input folder>/<name>.<ext>` | Output movie: a file or a folder, with keywords. See [Output path](#output-path) below. `<name>` is the sequence name without its frame number and separator (`shot_010.1001.exr` → `shot_010`). |
| `-version_regex REGEX` | `_v(\d\d\d)` | How the version is found in the sequence name for the [name keywords](#name-keywords). |
| `-smart_tokens NAME` | the presets file's `default_smart_tokens` (`none`) | A [smart tokens](#smart-tokens) preset: per-project keyword rules with conditions, regex replacement and case change. `none` means none. |
| `-smart_tokens_def JSON` | *(none)* | Smart token definitions given directly: a JSON list like a preset's `tokens`, e.g. `-smart_tokens_def "[{\"name\":\"client\",\"source\":\"acme\",\"case\":\"upper\"}]"`. Wins over `-smart_tokens`. The tuner sends this when its Tokens tab is edited. |
| `-name_token NAME=REGEX` | the presets file's `name_tokens` | A [custom keyword](#custom-name-tokens) `{NAME}` cut out of the sequence name by a regex. Repeatable; `NAME=` removes one from the presets file. |

**How the sequence is found.** The frame number is the run of digits at the end of the file name, just before the extension:
- `shot_010.1001.exr` → name `shot_010.`, frame 1001, 4-digit padding.
- `plate_0001.png` → name `plate_`, frame 1, 4-digit padding.

Every file in the same folder with the same name, the same extension (case-insensitive) and the same padding belongs to the sequence. Numbers longer than the padding also count, so frame 10000 belongs to a 4-digit sequence.

A file without a frame number (`plate.exr`) is treated as a one-frame sequence at frame 0.

### Output path

`-o` is a template:

- **Keywords** work in folders and file name:
  - `{clean_name}`, `{frame_start}`, `{frame_end}`, `{timecode}`, `{fps}`, `{preset}`, `{date}`, `{reel}`;
  - `{ext}` (the container extension);
  - `{input_width}`, `{input_height}`, `{input_par}` (EXR input);
  - [`{meta:ATTR}`](#header-values-metaattr) and the [name keywords](#name-keywords).
  
  An unknown keyword stops the run before anything is converted.
- **`./` and `../` are relative to the input sequence's folder**, and `..` steps up from there: `-o "./../review/"` writes next to the frames' folder, in `review`. This differs from `-ffmpeg`, `-ocio`, `-presets_file` and `-clf`, where `./` means the application folder. Absolute paths are used as they are; other relative paths (`out/x.mov`) are relative to the folder the command runs in.
- **A folder** gets the default file name `<name>.<ext>`. An `-o` ends in a folder when it ends in `/` or `\`, is `.` or `..`, or names an existing folder.
- **The extension is added** when the name doesn't end in a container extension (mov, mp4, mxf, mkv, webm, …): `-o "./{clean_name}.v{version_number}"` gives `sh010_comp.v3.mp4`.
- **The container** follows `-ff_out_ext`, else a container extension written in `-o` (`-o review.mov` makes a mov even with the h264 preset), else the preset's `ext`. If the name ends in a different container extension than `-ff_out_ext`, the name is kept and a warning printed, because ffmpeg picks the container from the file name.
- Missing folders are created. The log shows the result, e.g. `Output: "./../review/{namesplit1}/" -> D:/shots/sh010/review/sh010/sh010_comp_v003.mp4`.

Examples, for frames in `D:/shows/abc/sh010/comp/v003/sh010_comp_v003.1001.exr`:

| `-o` | Movie |
|---|---|
| *(none)* | `D:/shows/abc/sh010/comp/v003/sh010_comp_v003.mp4` |
| `./../../review/` | `D:/shows/abc/sh010/review/sh010_comp_v003.mp4` |
| `./../review/{namesplit1}_v{version_number}` | `D:/shows/abc/sh010/comp/review/sh010_v3.mp4` |
| `D:/dailies/{date}/{clean_name}.mov` | `D:/dailies/2026-09-30/sh010_comp_v003.mov` (a mov) |

## Movie input

`-i` can be a movie file instead of a frame of a sequence: `.mov .mp4 .m4v .mxf .mkv .avi .webm .mts .m2ts .mpg .mpeg .ts .wmv .flv .y4m .gif`, recognised by the extension.

```
vfx-transcode -i D:/edit/sh020_comp_v002.mov -o ./review/ -slate generic -burn_preset review
```

- **Probing:** ffprobe reads the movie's size, frame rate, frame count, pixel aspect (SAR), timecode and tags (the [`tools.ffprobe`](#tools) path, else `ffprobe` on the PATH). The log shows it, e.g. `Input: movie D:/edit/sh020_comp_v002.mov  1920x1080 prores yuv422p10le, 23.976 fps, 48 frames, timecode 01:00:10:00, frames numbered 1001-1048`.
- **Frames:** ffmpeg unpacks the frames into the intermediate folder as a numbered sequence named after the movie (`sh020_comp_v002.1001.png` …), in the `-mezzanine` format (`png`, `tif`, `jpg` or `bmp`; anything else falls back to `png`). `-mezzanine_16bit 1` keeps 10-bit sources at 16 bits. From there the movie goes the same way as PNG / JPG input, so slate, burn-ins, letterbox, alpha, thumbnail, filmstrip and preview all work as usual.
  - Outputs and `./` paths are relative to the movie's folder, as for a sequence.
  - YUV is read as the movie is tagged. Untagged movies are read as Rec.709 (HD and larger) or Rec.601 (SD).
- **Only what's needed:**
  - A [preview](#preview) unpacks only its one frame (a frame-exact seek), so scrubbing in the tuner is quick even on long movies.
  - A thumbnail or filmstrip without `-o` unpacks only the frames it uses, when that's under an eighth of the movie.
  - The movie itself unpacks every frame, which needs disk space: about 3–6 MB a frame for 1080p PNG. `-mezzanine jpg` is much smaller, at a small quality cost.
- **Colour:** the frames are used as they are, as display-referred pictures, the same as PNG input. OCIO and CLF settings are not applied (the log says so).
- **Audio:** not carried over.

| Option | Default | Description |
|---|---|---|
| `-movie_first_frame N` | `1001` | The frame number of the movie's first frame, for burn-ins (`{current_frame}`, `{frame_start}`), `-thumbnail_frame`, the slate (frame N−1) and the frame counter timecode. |

Header values come from the movie, so the same rules as for an EXR header apply:
- **Frame rate:** the movie's own rate takes the place of the EXR header rate, so [`-fps_source`](#frame-rate) works as usual (by default `-r` wins, else the movie's rate).
- **Timecode:** when no timecode source is chosen (no `-timecode_source`, `-timecode` or metadata preset), the movie's own timecode is used if it has one; otherwise the frame counter, as for sequences.
- **Pixel aspect:** from the movie's sample aspect ratio, following [`-par_source`](#pixel-aspect-ratio).
- **Keywords:** `{meta:movie:codec}`, `{meta:movie:pix_fmt}`, `{meta:movie:frames}`, `{meta:movie:width}`, `{meta:movie:height}`, `{meta:movie:file}`, and the movie's tags as `{meta:movie:TAG}` (e.g. `{meta:movie:encoder}`), plus `{meta:timecode}` and `{meta:FramesPerSecond}`.

## Trim

| Option | Default | Description |
|---|---|---|
| `-in FRAME` | the first frame | The first frame to use, by frame number (inclusive). |
| `-out FRAME` | the last frame | The last frame to use, by frame number (inclusive). |

- **Works for sequences and movies.** A movie's frames are numbered from [`-movie_first_frame`](#movie-input), and only the trimmed range is unpacked. Either option alone trims one end.
- **Everything follows the trim:** the frame range in burn-ins (`{frame_start}`, `{frame_end}`), the slate (frame `-in` − 1), the frame-counter timecode, and the thumbnail and filmstrip frames.
- **Timecode:** a timecode from a header (EXR or movie) is offset to the trimmed first frame. A movie that starts at `01:00:10:00`, trimmed with `-in 1011`, starts at `01:00:10:10`.
- **Errors:** a range with no frames, or `-in` after `-out`, stops the run.
- **Preview:** a [preview](#preview) can show a frame outside the trim, so the tuner can scrub there to find a new In or Out. The burn-ins still show the trimmed range.

## Image sequence output

`-o` naming an image file writes an image sequence instead of a movie: `.exr`, `.png`, `.jpg`, `.tif`, `.dpx` or `.bmp`.

```
vfx-transcode -i D:/shots/sh010/sh010_comp_v003.1001.exr -o "./../jpg/{clean_name}.####.jpg" -in 1001 -out 1050
vfx-transcode -i D:/edit/sh020.mov -o "D:/plates/sh020/sh020.%04d.exr" -ci "Output - Rec.709" -co ACEScg
```

- **Naming:** the frame-number pattern sets the padding: `####` (4 digits), `%05d` (5), `@@@@` (4). Without one, `.####` is added before the extension, with the input's padding. A folder (`./out/`) gets `<name>.####.<ext>`. Keywords work as for a movie `-o`.
- **Frame numbers** are the input's (trimmed): `sh010.1001.exr` becomes `sh010.1001.jpg`. Holes in the input stay holes.
- **Safety:** an output that would overwrite the input frames stops the run.

**Which tool runs:**

| Input | Output | Tools |
|---|---|---|
| EXR sequence | image sequence (EXR or not) | oiiotool only |
| PNG / JPG / TIF … sequence | image sequence | oiiotool only |
| movie | EXR sequence | ffmpeg unpacks the frames, oiiotool writes the EXRs |
| movie | PNG / JPG / TIF / DPX sequence | ffmpeg only |

Without ffmpeg there is no ffmpeg step at all: burn-ins, `-letterbox`, `-ff_transform` and `-ff_filter` aren't applied (the log says so). Use [`-oiio_letterbox`](#letterbox) and an [OIIO transform](#oiio-transforms) instead. Image sequences get no slate and no container metadata. When ffmpeg writes the sequence (a movie to JPG …), burn-ins, the ffmpeg letterbox and `-ff_transform` work as for a movie.

**Colour:**
- EXR input to a non-EXR sequence: the usual conversion (`-ci` → `-co`, or display/view, or `-clf`), as for a movie.
- EXR to EXR: no colour conversion unless `-ci`, `-co`, `-display`, `-view` or `-clf` is given. Alpha stays premultiplied.
- PNG / JPG / movie frames through oiiotool: no colour conversion unless `-ci` / `-co` (or `-display` / `-view`) is given, e.g. `-ci "Output - Rec.709" -co ACEScg` for a movie to linear EXRs. `-clf` only applies to EXR input.

| Option | Default | Description |
|---|---|---|
| `-seq_preset NAME` | the presets file's `default_sequence_presets` entry for the format | A [sequence preset](#sequence-presets): the oiiotool output settings (bit depth, compression, quality). A preset for another format prints a warning. `--help` lists them. |
| `-seq_args ARGS` | *(the preset)* | oiiotool output arguments for one run, replacing the sequence preset, e.g. `-seq_args "-d half --compression dwab:45"`. Without a preset or `-seq_args`, the built-in settings apply: exr `-d half --compression zip`; jpg `-d uint8 --compression jpeg:95`; png / tif `-d uint8` (`uint16` with `-mezzanine_16bit 1`); dpx `-d uint10`. |

The log names what was used, e.g. `Writing D:/out/sh010.####.exr (exr_dwab30_half: -d half --compression dwab:30)`.

### Sequence presets

The shipped `sequence_presets`, with `default_sequence_presets` setting exr → `exr_zip16_half`, jpg → `jpg_high` and png → `png_8bit`. TIF and DPX have no default preset, so they use the built-in settings.

| Preset | Format | oiiotool arguments | Use |
|---|---|---|---|
| `jpg_high` | jpg | `-d uint8 --compression jpeg:95 --attrib jpeg:subsampling 4:4:4` | High quality, no chroma subsampling |
| `jpg_medium` | jpg | `-d uint8 --compression jpeg:80 --attrib jpeg:subsampling 4:2:0` | Small files for review and web |
| `png_8bit` | png | `-d uint8` | |
| `png_16bit` | png | `-d uint16` | 10-bit sources, grading |
| `exr_piz_half` | exr | `-d half --compression piz` | Lossless; good for grainy plates |
| `exr_zip1_half` | exr | `-d half --compression zips` | Lossless ZIP, 1 scanline (ZIPS): fast to read in compositing |
| `exr_zip16_half` | exr | `-d half --compression zip` | Lossless ZIP, 16 scanlines: smaller than ZIPS |
| `exr_dwab30_half` | exr | `-d half --compression dwab:30` | Lossy DWAB, high quality, small |
| `exr_dwab60_half` | exr | `-d half --compression dwab:60` | Lossy DWAB, smaller files |

Fields: `notes`; `ext` (exr, jpg, png, tif, dpx or bmp; jpeg and tiff are read as jpg and tif); `args` (one string or a list).
- EXR compressions: `zips`, `zip`, `piz`, `dwaa:Q` / `dwab:Q` (Q = quality; 45 is OpenEXR's default), `pxr24`, `b44`, `none`.
- JPEG: `--compression jpeg:QUALITY` and `--attrib jpeg:subsampling 4:4:4|4:2:2|4:2:0`.
- A `default_sequence_presets` entry must name a preset of that format.

The presets are oiiotool settings, so they apply wherever oiiotool writes the sequence. When ffmpeg writes it (a movie to JPG / PNG …), its fixed formats apply (see above).

When ffmpeg writes the sequence, the formats are fixed: JPG `mjpeg -q:v 2` (4:4:4), PNG / TIF 8-bit RGB (16-bit with `-mezzanine_16bit 1`), DPX 10-bit.

A [preview](#preview) follows the same tools. An oiiotool-only preview writes the frame in the output format, then shows it as 8-bit PNG. An EXR's values are shown clipped, with no view transform.

## Super-presets

A super-preset stores the settings of several areas together under one name: encode, transform, burn-in, slate, filmstrip, metadata and letterbox. The [tuner](TUNER.md) saves them in `super_presets.json`, next to the presets file. Each area holds the tuner's values and the matching vfx-transcode options, so a super-preset made in the tuner also works on the command line:

```
vfx-transcode -i D:/shots/sh010/sh010_comp_v003.1001.exr -super_preset dailies -o ./review/
```

| Option | Default | Description |
|---|---|---|
| `-super_preset NAME[,NAME]` | *(none)* | The options of the super-preset's areas are put in front of the command line's. An option on the command line wins over the super-preset's; options that add up (`-metadata`, `-slate_field`, `-burn_style`) add to it. Several names, comma separated or with `-super_preset` repeated, are applied in order, so later ones win. |
| `-super_presets_file PATH` | `super_presets.json` next to the presets file (plus the local one, see below) | Another super-presets file. |

- The areas' options are written out in full, so a super-preset doesn't depend on which presets file is loaded. The exceptions are the encode preset, which is stored by name (`-preset h264`), and a transform preset loaded in the tuner and left unchanged, which is stored by name (`-oiio_transform NAME`) so vfx-transcode still picks the sub-preset for each input by its conditions.
- The input, colour settings, frame rate and output path are not part of a super-preset.
- An unknown name stops the run before anything is done, and lists the names in the file.
- The file is plain JSON. vfx-transcode reads only each area's `args`; the tuner reads its `state`. Hand edits to `args` work on the command line but the tuner won't see them: save the super-preset again from the tuner instead.

**Where they are saved.** With [studio preferences](#which-presets-file-is-used) in use, `super_presets.json` is the one beside the studio `presets.json`, so everyone shares it. If the tuner can't write there (no write access, share offline), it offers the local `super_presets.json` next to the application instead. The local file is also read, but only on that computer. Its entries are listed in italic, and one with the same name as a studio entry wins there; `-super_preset NAME` on the command line sees it too.

**Saving is multi-user safe**, so several people can save to the shared file at the same time:

1. The new file is written to a temporary file in the same folder, named after the user and the time (`super_presets_jdoe_261124-1649.json`), and read back to check it.
2. For the moment of the swap, a lock file (`super_presets.json.lock`, with the user and time in it) keeps others waiting; a lock older than a minute is from a crash and is removed.
3. If someone else saved since the file was read, the tuner reads it again and redoes its change, so their super-presets are kept.
4. The current file is copied to `super_presets_safecopyNNN.json` (the next free number), then the temporary file takes its name in one rename. The old file is copied rather than renamed away first, so others never find the file missing.

A failed save leaves the file as it was. Safe copies are never deleted by the tools; clear out old ones by hand when you like.

## Frame rate

| Option | Default | Description |
|---|---|---|
| `-r`, `--fps RATE` | *(none)* | Frame rate given on the command line: `24`, `25`, `23.976`, `24000/1001`, … |
| `-fps_source arg\|exr\|arg_only` | `arg` | Which source wins when both `-r` and the EXR header give a rate. |

There are two possible sources, plus 24 as the last resort:

- **`-r`**, the explicit argument.
- **The EXR header** of the first frame, EXR input only. An attribute is used when its name, ignoring case, `_`, spaces and any `prefix:` or `prefix/`, is `FramesPerSecond`, `frame_rate` or `fps`. That covers OpenEXR `framesPerSecond` (reported by OIIO as `FramesPerSecond`; Karma writes it as a float), `frame_rate`, `frameRate`, `FrameRate`, `oiio:FramesPerSecond`, `nuke/input/frame_rate` and the like. If several exist, `FramesPerSecond` is used first, then `frame_rate`, then `fps`. Values can be rationals (`24/1`), floats (`23.976`) or text (`"25"`).

`-fps_source` sets the order they are tried in:

| `-fps_source` | Order | Use when |
|---|---|---|
| `arg` (default) | `-r` → EXR header → 24 | The rate you type always wins. Without `-r` the header is used, and 24 only if neither exists. |
| `exr` | EXR header → `-r` → 24 | Trust the renders. `-r` is only the fallback for files without a rate. |
| `arg_only` | `-r` → 24 | Ignore the header completely. This is how older versions behaved. |

Everything uses the resolved rate: the movie's frame rate, frame durations, the timecode counter, the `{tc}` burn-in, and `{fps}` in burn-ins and metadata. The log shows the rate and its source, e.g. `Frame rate: 25 (EXR header FramesPerSecond)`. When both sources exist and disagree, the log also says which one was ignored.

**How values are read.** Decimal values are snapped to the standard rates. From `-r` or the header, `23.976` and `23.976023…` both become exactly 24000/1001, and `29.97` becomes 30000/1001. Snapped rates are 23.976, 24, 25, 29.97, 30, 47.952, 48, 50, 59.94, 60, 119.88 and 120. Other decimals are kept, e.g. `12.5`. A fraction typed with `-r`, such as `24000/1001` or `2997/125`, is used exactly as written.

**Timecode counting** uses the rounded rate: 23.976 counts as 24 and 29.97 as 30, non-drop-frame.

## Pixel aspect ratio

VFX material is often anamorphic (pixel aspect 2.0, or 1.33 for 1.33x lenses), and the value in the file's header isn't always right.

| Option | Default | Description |
|---|---|---|
| `-par PAR` | *(none)* | Input pixel aspect ratio: `1`, `2`, `1.33`, `4/3`, … `1.33` and `1.333` mean exactly 4/3; other decimals are used as typed. |
| `-par_source arg\|exr\|arg_only` | `arg` | Which source wins; same idea as `-fps_source`. |
| `-par_out same\|square\|PAR` | `same` | Pixel aspect of the movie; see below. |

Input sources:

| `-par_source` | Order |
|---|---|
| `arg` (default) | `-par` → EXR header → 1 |
| `exr` | EXR header → `-par` → 1 |
| `arg_only` | `-par` → 1; the header is ignored |

The header value is the first frame's `PixelAspectRatio` (OpenEXR `pixelAspectRatio`). Since headers are sometimes wrong, `arg` (the default) lets `-par` fix it. The log shows the value, its source and any ignored one, e.g. `Pixel aspect: 2 (-par)`.

Output:

| `-par_out` | Result |
|---|---|
| `same` (default) | Pixels are kept as they are. The movie is tagged with the input pixel aspect (its SAR), so players stretch it correctly. |
| `square` | Reformatted for square pixels, so it looks right everywhere, even in players that ignore the tag. |
| a value, e.g. `2` | Reformatted for that pixel aspect and tagged with it. |

**Reformatting** keeps the output width (`-width`) and scales the height by output PAR / input PAR, so the picture keeps its shape on screen. Anamorphic 2.0 → square halves the height; 2048×858 at `-width 1024` becomes 1024×214. The height is rounded to an even number. The movie is always tagged with its pixel aspect, 1:1 included.

**Burn-ins** are drawn in the stored pixels, so with a non-square output they look stretched by the pixel aspect on playback; the tool warns. Use `-par_out square` for burnt-in review movies of anamorphic material.

## Colour

EXRs are converted in one of three ways: colourspace to colourspace, display / view, or a CLF file.

**Colourspace to colourspace** (the default): `oiiotool --colorconvert IN OUT`.

| Option | Default | Description |
|---|---|---|
| `-colour_preset NAME` | *(none)* | A [colour preset](#colour-and-ffmpeg-transform-presets): named `-ci` / `-co` / `-display` / `-view` / `-looks` / `-clf` / `-ocio` values from the presets file, e.g. `-colour_preset rec709_to_acescg`. The options given on the command line win over the preset's. Counts as a colour given (so non-EXR input through oiiotool is converted). `-color_preset` works too. |
| `-ci`, `--colorin NAME` | `ACEScg` | Colourspace of the EXRs, as named in the OCIO config, e.g. `ACEScg` or `"Utility - Linear - sRGB"`. Used in both modes. |
| `-co`, `--colorout NAME` | `out_rec709` | Output colourspace, e.g. `out_rec709` or `out_srgb` (ACES 1.2 config names). It also decides the colour tags written to the movie; see [Presets file](#presets-file). |

**Display / view**: `oiiotool --ociodisplay`, the way ACES 1.3 and newer configs are meant to be used. It's switched on by `-display` and/or `-view`, and then `-colorout` is ignored.

| Option | Default | Description |
|---|---|---|
| `-display NAME` | *(not used)* | OCIO display, e.g. `"sRGB - Display"`, `"Rec.1886 Rec.709 - Display"`. `""` means the config's default display. |
| `-view NAME` | the display's default | OCIO view, e.g. `"ACES 1.0 - SDR Video"`, `"Un-tone-mapped"`. |
| `-looks LOOKS` | *(none)* | OCIO looks applied with the display transform, e.g. `"ACES 1.3 Reference Gamut Compression"`; separate several with commas. |

**CLF file**: `oiiotool --ociofiletransform FILE`, for a show LUT delivered as CLF (Common LUT Format, the ACES/ASC LUT file). The CLF replaces the colour conversion; it is either a CLF or `-ci`/`-co` / display-view, never both.

| Option | Default | Description |
|---|---|---|
| `-clf PATH` | *(not used)* | CLF file to apply. The path may use keywords: the [name keywords](#name-keywords), `{clean_name}`, `{frame_start}`, `{frame_end}`, `{input_width}`, `{input_height}`, `{input_par}` and [`{meta:ATTR}`](#header-values-metaattr), e.g. `"D:/show/luts/{namesplit1}/{namesplit2}.clf"` or `"./luts/{meta:reel}.clf"`. `./` is relative to the application folder. |

- The path is expanded with the first frame's header and checked before any frame is converted. A missing file stops the run, and the error shows the expanded path and any unknown keyword.
- The log shows both, e.g. `CLF "D:/show/luts/{namesplit1}.clf" -> D:/show/luts/sh010.clf`.
- The CLF gets the image as it comes from the EXR (after the channel choice and any [OIIO transform](#oiio-transforms); with [alpha](#alpha) kept, un-premultiplied), so it must expect that input, usually scene-linear, and output display-ready values.
- `-display`, `-view` and `-looks` are ignored with a warning. `-co` only names the output for the movie's colour tags: a name with "srgb" gives sRGB tags, anything else Rec.709.
- No OCIO config is needed; a missing `-ocio` file is not an error with `-clf`.
- The slate's `{colour}` shows `CLF <file name>`.

Example with the ACES 1.3 studio config that is built into OCIO 2.2+:

```
python vfx-transcode.py -i D:/shots/sh010/sh010_comp.1001.exr -ocio ocio://studio-config-latest ^
  -ci ACEScg -display "sRGB - Display" -view "ACES 1.0 - SDR Video"
```

The movie's colour tags follow the display: a display with "sRGB" in its name gives `iec61966-2-1`, any other gives `bt709`. Tags for HDR and wide-gamut displays (PQ, HLG, Rec.2100, P3, XYZ) are not generated. The tool warns, and the right tags go in `-ff_encode` or a custom preset with `colour_tags: false`. The log shows the conversion used, e.g. `Colour: ACEScg -> display "sRGB - Display", view "ACES 1.0 - SDR Video"`.

| Option | Default | Description |
|---|---|---|
| `-ocio PATH` | `./ocio/aces_1.2cg/config.ocio` | OCIO config used by oiiotool. It can also be a built-in config URI, such as `ocio://studio-config-latest` (ACES 1.3+; needs an oiiotool built with OCIO 2.2 or newer). If the file doesn't exist but the `OCIO` environment variable is set, that config is used and a warning is printed. |
| `-ocio_info` | | Print the colourspaces, displays, views and looks of the config (`oiiotool --colorconfiginfo`) and exit. `-i` isn't needed. Use it to find the exact names for `-ci`, `-co`, `-display`, `-view` and `-looks`. |

Colour conversion applies to EXR input only. Other formats are assumed to be display-ready already. Deep EXRs without colour are shown as depth, without any OCIO conversion.

## Tools

The paths to the executables can be set in the [presets file](#presets-file)'s `tools` section, so a studio or project presets file can point to its own builds:

```json
"tools": {
  "ffmpeg": "D:/apps/ffmpeg-7.1/bin/ffmpeg.exe",
  "ffprobe": "D:/apps/ffmpeg-7.1/bin/ffprobe.exe",
  "oiiotool": "%OIIO_ROOT%/bin/oiiotool.exe"
}
```

| Option | Default | Description |
|---|---|---|
| `-oiio PATH` | the presets file's `tools.oiiotool`, else `./oiio/oiiotool.exe` | oiiotool executable. |
| `-ffmpeg PATH` | the presets file's `tools.ffmpeg`, else `./ffmpeg/ffmpeg.exe` | ffmpeg executable. |
| `-ffprobe PATH` | the presets file's `tools.ffprobe`, else `./ffmpeg/ffprobe.exe` | ffprobe executable. Configured for tools and scripts that need it; vfx-transcode itself reads image sizes with `ffmpeg -i` and doesn't run ffprobe yet. |

- **Which path wins:** the command-line option, then the presets file's `tools`, then the built-in default. A tool left out of `tools`, or set to `""`, uses the default.
- **Path forms:** `./` and `../` are relative to the application folder (not the presets file or the current folder). Environment variables (`%VAR%`, `$VAR`, `${VAR}`) and `~` are expanded, on the command line too. In JSON, write Windows paths with `/` or doubled backslashes: `"D:/apps/ffmpeg.exe"` or `"D:\\apps\\ffmpeg.exe"`.
- **Not found:** if the file doesn't exist, the tool looks for `oiiotool` / `ffmpeg` / `ffprobe` on the `PATH`. If it isn't there either, the run stops with an error that names the path, where it came from (e.g. `presets.json "tools"`) and the PATH. `-v` logs which executable each run uses.
- `--help` lists the presets file's `tools`.

Before converting any frame, the tool asks ffmpeg for its filter list (`ffmpeg -filters`) and stops if a filter the movie needs is missing, naming what needs it. The usual case is `drawtext`, which burn-ins and the slate need: it comes with libfreetype, and some ffmpeg builds (minimal or "essentials" static builds) leave it out. Use a "full" build (gyan.dev, BtbN) or turn off the burn-ins and the slate. The other filters checked are `color`, `overlay`, `drawbox`, `premultiply` and `setparams`, each only when used. If the list can't be read, the check is skipped with a warning.

## EXR conversion (oiiotool)

| Option | Default | Description |
|---|---|---|
| `-oiio_channel LIST` | *(automatic)* | Channels to show, comma separated: `R,G,B` or `diffuse.R,diffuse.G,diffuse.B`. If they aren't in the image, a warning is printed and the tool tries them anyway. |
| `-oiio_channel_use_first 0\|1` | `0` | Skip the automatic search (`channel_preferences`, below) and use the first layer. |
| `-width PIXELS` | `1920` | Output width. The height follows the aspect ratio and is rounded to an even number. `0` keeps the original size. EXRs are resized by oiiotool; other input is resized by ffmpeg. With an [OIIO transform](#oiio-transforms), ffmpeg scales the transform's output to this width. |
| `-mezzanine EXT` | `png` | Format of the intermediate frames: `png`, `tif`, `jpg`, … `tif` is often faster than `png` because it skips PNG compression. |
| `-mezzanine_16bit 0\|1` | `0` | Write 16-bit intermediates. The default 8-bit is faster and is all an 8-bit h264 needs. Turn this on for 10-bit presets (`h265`, `prores*`, `dnxhr-hqx`) if you want real 10-bit input. |
| `-oiio_command ARGS` | *(none)* | Extra oiiotool arguments, added after the colour conversion and resize, e.g. `-oiio_command "--ociolook \"Film Look\""`. Quotes group words; backslashes are kept as typed. |
| `-oiio_threads N` | `0` | Number of parallel oiiotool jobs, and oiiotool's `--threads`. `0` = number of CPU cores. |
| `-oiio_per_file 0\|1` | `0` | Run one oiiotool per frame instead of `--parallel-frames` batches. Mainly for troubleshooting. |
| `-temp_location user\|source` | `user` | Where the intermediate files go; see below. |
| `-temp NAME` | `.temp_png_sequence` | With `-temp_location source`: the folder created inside the input folder. It must be a relative name; absolute paths, drive letters and `..` are refused. An empty value (`-temp ""`) writes into the input folder itself, but only if that wouldn't overwrite existing files. Ignored with `-temp_location user`. |
| `-temp_keep 0\|1` | `0` | Keep the intermediate files. The log prints where they are. |
| `-write_exr_info 0\|1` | `0` | Save the full EXR header of the first frame, as read by oiiotool, to `<first frame>.json`. Useful for finding channel and metadata names. |
| `-deep_zrange NEAR,FAR` | *(measured)* | See [Deep EXRs](#deep-exrs). |
| `-missing_frames hold\|error` | `hold` | See [Missing and failed frames](#missing-and-failed-frames). |

**Where intermediate files go.**

- **`-temp_location user`** (the default) creates a new, uniquely named folder for each run: `<user temp>\vfx-transcode\<name>_<random>`, e.g. `C:\Users\you\AppData\Local\Temp\vfx-transcode\sh010_comp_k3j9x2`. The user temp folder is normally on a local disk, which is usually much faster than writing thousands of files next to renders on a network share. Several runs can go at once without clashing. After the run the whole folder is deleted, including partial files from failed frames. The user temp folder is taken from the `TEMP` / `TMP` environment variables on Windows (`TMPDIR` on Linux/macOS), so point those at a bigger or faster disk if needed.
- **`-temp_location source`** writes into the `-temp` folder next to the source files, as older versions did. This is useful when the local disk is small, or to look at the intermediates next to the renders with `-temp_keep 1`. Only files from this run are deleted, then the folder if the run created it and it is empty.

Both locations print the folder in the log (`Intermediate files: …`).

**Automatic channel choice.** The ordered list of channel sets is set by `channel_preferences` in the [presets file](#presets-file). The first set whose channels all exist in the image (in any EXR part) wins. The shipped order is:

1. `rgba.R,G,B(,A)`
2. `rgb.R,G,B(,A)`
3. `R,G,B(,A)`
4. `r,g,b(,a)`
5. `beauty.R,G,B(,A)`

If no set matches, the tool takes the first layer with at least three channels, or the first layer if none has three. Alpha is always dropped. At most three channels are shown, or one for a mono image. `--help` prints the current list.

Multi-part EXRs are supported: the part that holds the chosen channels is selected with `--subimage`.

**Batching.** EXRs are converted with one `oiiotool --parallel-frames` call for each run of frames without a hole. This avoids starting oiiotool and loading the OCIO config once per frame. Single frames, and names containing `%`, `#` or `@`, are converted one file at a time.

### OIIO transforms

VFX projects often need a different resize or crop for each kind of input: UHD plates scaled to 1080p, 4080-wide plates cut to 3840 first, everything else fitted to 1080 lines. A transform preset in `presets.json` (`oiio_transforms`) holds these rules, and `-oiio_transform NAME` picks one. EXR input only.

| Option | Default | Description |
|---|---|---|
| `-oiio_transform NAME` | the presets file's `default_oiio_transform` (`none`) | Transform preset. `none` = off, the usual `-width` resize. `--help` lists the presets with their sub-presets and conditions. |
| `-oiio_transform_args "ARGS"` | *(none)* | oiiotool transform steps used as they are, without a preset or conditions, e.g. `-oiio_transform_args "--rangecompress --resize 0x1080 --rangeexpand"`. Keywords work. Wins over `-oiio_transform`; like a transform preset, it replaces oiiotool's `-width` resize and ffmpeg then scales to `-width`. The tuner writes this when a sub-preset was edited. |

**How a sub-preset is chosen.** A preset has a list of sub-presets (`subs`). They are checked from top to bottom, and the **first** one whose conditions **all** hold (logical AND) is used; the rest are not checked. A sub-preset without conditions always matches, so put one last as the catch-all. If none matches, a warning is printed and the normal `-width` resize is used.

**Conditions** compare two strings, after [keyword](#burn-ins) expansion:

```json
{"left": "{input_width}x{input_height}", "op": "==", "right": "3840x2160"}
```

- `op` is `==` (equal) or `!=` (not equal). Both sides are trimmed of spaces at the ends; otherwise the comparison is exact text.
- Keywords:
  - `{input_width}` and `{input_height}` are the size of the first frame's display window.
  - `{input_data_width}` and `{input_data_height}` are the size of its data window.
  - `{input_par}` is the [input pixel aspect](#pixel-aspect-ratio) as a plain number: `1`, `2`, `1.3333`.
  - Also available: `{clean_name}`, `{frame_start}`, `{frame_end}`, [`{meta:ATTR}`](#header-values-metaattr) and the [name keywords](#name-keywords).
- An unknown keyword is compared as typed, with a warning.

**Transforms** are oiiotool arguments. Each list entry can hold one argument or several words (`"--resize 1920x1080"`), and keywords work there too (`"--cut {input_width}x2000+0+80"`). They run on the scene-linear image, right after the channels are picked and before the colour transform. With [alpha](#alpha) they run while the image is still premultiplied. They replace oiiotool's `-width` resize.

**The final size.** After the transform, ffmpeg scales the result to `-width`, whatever size the transform produced; the height follows the aspect. `-width 0` keeps the transform's output size. With the default `-width 1920` and a transform that already makes 1920×1080, nothing changes. The slate and letterbox follow the real size of the converted frames.

The log shows the choice, e.g. `OIIO transform "mygreatproject_editorial": 4080x2160 input -> sub "239" (4080 == 4080; 2160 == 2160): --rangecompress --cut 3840x2160+120+0 --resize 1920x1080 --rangeexpand`. With `-v 1`, every sub-preset that was checked is listed.

oiiotool notes:
- `--rangecompress` / `--rangeexpand` around a resize reduce ringing on bright HDR highlights.
- **`--crop` vs `--cut`.** `--crop` only shrinks the *data window*; the display window keeps its size. PNG (and the other intermediate formats) can't store windows, so the frame is written at the full display size, with black outside the crop. For example, `--resize 0x1080 --crop 1920x1080` on a 4312×2160 plate gives a 2156×1080 frame. `--cut` crops and makes the result the new picture size (origin and display window reset), so use `--cut` to change the size.
- **Centred cut.** oiiotool can compute the offset itself with its own `{...}` expressions, which pass through the keyword expansion untouched: `"--cut 1920x1080+{(TOP.width-1920)/2}+0"` cuts the centre 1920 pixels of the current image. The shipped `other` / `1080p` subs do this after `--resize 0x1080`.

## Missing and failed frames

`-missing_frames hold` (the default) keeps the movie at the full frame range:

- **Holes in the sequence** are filled by repeating the previous frame. A warning lists the missing frames.
- **Frames oiiotool fails on:**
  - If a batch fails, its frames are converted again one at a time.
  - Frames that still fail are replaced by their neighbour.
  - The run then ends with exit code 2.

`-missing_frames error` stops the run instead, on the first hole or failed frame.

## Deep EXRs

Deep images are detected from the header: its "deep" flag, or a `ZBack` channel.

**Deep files with colour channels** are flattened (`--flatten`) and then converted like any other EXR.

**Deep files without colour channels**, such as a Karma deep holdout with `A RA GA BA Z ZBack`, become a depth picture:

- The image is flattened.
- `Z` is remapped from *near…far* to 1…0, so near is bright.
- The result is multiplied by `A`, so empty areas are black.
- There is no OCIO conversion, because this is data, not colour.

| Option | Default | Description |
|---|---|---|
| `-deep_zrange NEAR,FAR` | *(measured)* | Depth range, e.g. `-deep_zrange 30,2000`. |

If `-deep_zrange` isn't set, the range is measured on the first frame and used for the whole sequence, so brightness doesn't flicker:

- **near** is the smallest Z.
- **far** is the largest Z × A, which ignores empty pixels (Karma gives them Z = 1e30).

The log prints the range it used.

## Timecode

| Option | Default | Description |
|---|---|---|
| `-timecode_source counter\|arg\|metadata` | the metadata preset's, else `counter` | Where the start timecode comes from. See below. `exr` is the old name of `metadata` and still works. |
| `-timecode HH:MM:SS:FF` | `01:00:00:00` | Start timecode for `arg`. Giving `-timecode` selects `arg` automatically. Use `;` before the frames for drop-frame (`00:59:59;29`). |
| `-output_timecode 0\|1` | `1` | Write the timecode into the movie: a real timecode track in mov, mp4 and mxf, which editing apps read, or a `timecode` tag in other containers. In an EXR or DPX [image sequence](#image-sequence-output) written by oiiotool, each frame gets its own timecode (`smpte:TimeCode`, counting up from the start timecode). oiiotool's `--frames` batch can't give each frame a different value, so these frames are converted one file at a time, `-workers` in parallel. EXR input whose header has a timecode, with `-timecode_source metadata`, keeps each frame's own. `0` removes any timecode carried over from the input header. |
| `-timecode_from_counter 0\|1` | — | Old switch, kept for existing scripts: `1` = `counter`, `0` = `arg`. |

The three sources:

- **`counter`**: the first frame number at the [frame rate](#frame-rate). Frame 1001 at 24 fps is `00:00:41:17`; at 25 fps it is `00:00:40:01`.
- **`arg`**: the value of `-timecode`.
- **`metadata`** (old name `exr`): read from the first frame's EXR header: `smpte:TimeCode` (OpenEXR `timeCode`), or any attribute whose name ends in "timecode"; for movie input, the movie's timecode. If there is none, a warning is printed and `counter` is used. EXR or movie input.

The log shows the timecode and its source, e.g. `Timecode: 00:00:41:17 (frame counter)`. The same value starts the `{tc}` burn-in.

With a [slate](#slate) the movie starts one frame earlier, so its timecode track starts at the slate's timecode (start − 1). The pictures keep the timecode described here.

## Metadata

| Option | Default | Description |
|---|---|---|
| `-metadata_preset NAME` | the presets file's `default_metadata` (`none`) | A [metadata preset](#metadata-presets): tags, copy rules, reel name and timecode settings in one name. `none` means no preset. |
| `-metadata KEY=VALUE` | *(none)* | Write a tag. Repeatable. The value may use `{clean_name}`, `{frame_start}`, `{frame_end}`, `{frames}`, `{timecode}`, `{fps}`, `{preset}`, `{date}`, [`{meta:ATTR}`](#header-values-metaattr), the [name keywords](#name-keywords) and the [delivery keywords](#delivery-keywords). `KEY=` with no value removes a tag that a preset would write. |
| `-metadata_copy SPEC` | the metadata preset's `copy`, else none | Copy attributes from the first frame's EXR header. See below. `-metadata_copy ""` switches off a preset's copying. |
| `-seq_metadata 0\|1` | `1` | [Image sequence](#image-sequence-output) written by oiiotool: write the tags into each image. See below. |

Examples:

```
-metadata "title={clean_name}" -metadata "comment=lighting v47, start {timecode}"
-metadata_copy "all,-husk:*"
-metadata_copy "Artist,DateTime,renderTime"
```

**`-metadata_copy` SPEC:**

- `all` copies everything that can be copied.
- Otherwise, list comma-separated key patterns, where `*` matches anything. A leading `-` excludes: `all,-husk:*` means everything except the `husk:` keys. Patterns are case-sensitive.
- Only single values of up to 1024 characters are copied. Matrices, vectors and huge blobs (such as Karma's `husk:render_stats`) are skipped.
- Image-structure attributes are never copied: compression, windows, chromaticities, `oiio:*`, `openexr:*`, `smpte:*`.
- Copying is off by default, because render headers often contain command lines, host names and job paths.
- Keys are the names oiiotool reports, which for the standard OpenEXR attributes differ from the names in the file: `owner` → `Copyright`, `comments` → `ImageDescription`, `capDate` → `DateTime`, `timeCode` → `smpte:TimeCode`, `framesPerSecond` → `FramesPerSecond`. `-write_exr_info 1` saves the header with the names as reported.

**Order.** When several sources give the same key, the later one in this order wins:

1. copied header attributes
2. the encode preset's `metadata`
3. the metadata preset's `tags`
4. `-metadata` on the command line

An empty value at any level removes the key. mov and mp4 need `-movflags +use_metadata_tags` to keep custom keys; the tool adds it, merged with any `-movflags` the preset already has. mkv keeps any key as it is. The log lists the keys that were written.

**Image sequences.** When oiiotool writes an image sequence (EXR input, PNG / JPG … input, or a movie to EXR), the metadata preset's `tags` and `-metadata` are written into every image as string attributes (`--attrib:type=string KEY VALUE`). Keywords are filled in first, the same as for a movie (`{timecode}` is the start timecode); braces left in a value are written as they are. `-seq_metadata 0` writes none.

- **EXR** keeps every key, e.g. `-metadata "vfx:shot={namesplit1}" -metadata "vfx:version={version_number}"`.
- **PNG, TIF, JPG** keep what their format can store: OIIO maps the standard names (`ImageDescription`, `Artist`, `Copyright`, `DateTime` …); other keys may be dropped by the format.
- The encode preset's `metadata` is not used (it is codec settings for the movie), and nothing is copied with `-metadata_copy`: oiiotool already keeps the input header's attributes as far as the output format can.
- A movie to a PNG / JPG / TIF sequence is written by ffmpeg, whose image files carry no tags; the log says so.

The log lists them, e.g. `Metadata: title, vfx:shot written into each exr`.

### Delivery keywords

Some delivery specs (streaming services in particular) want the movie to say how it was made. These keywords work in `-metadata` values and metadata preset `tags`:

| Keyword | Value |
|---|---|
| `{frames}` | Number of frames, `{frame_end} - {frame_start} + 1`. |
| `{slate_length}` | `1` with a slate, `0` without. |
| `{burnins}` | `true` if any burn-in or watermark is drawn, else `false`. |
| `{colour}` | The colour conversion, e.g. `ACEScg -> display "sRGB - Display", view "ACES 1.0 - SDR Video"` or `CLF show_lut.clf`. Empty for non-EXR input, and an empty tag is not written. |

The `delivery` metadata preset writes them all, as `source_frame_rate`, `first_frame`, `last_frame`, `frame_count`, `slate_length`, `display_type` and `watermarking`. Rename the keys in `presets.json` if your spec uses other names.

### Metadata presets

A metadata preset bundles the metadata settings under one name in `presets.json`: tags, copy rules, the reel name settings and the timecode source. Select one with `-metadata_preset NAME`. Every command-line option overrides the matching preset field on its own:

- `-metadata KEY=VALUE` adds or replaces one tag; `-metadata KEY=` removes one.
- `-metadata_copy`, `-reelid_source`, `-reelid`, `-reelid_attr`, `-reelid_pattern` and `-timecode_source` each replace the preset's value.
- Giving `-timecode` also beats the preset's `timecode_source`.

The log names the preset in its metadata line, e.g. `Metadata: title, comment (preset review)`. `--help` lists the presets with their settings.

| Preset | Tags | Other settings | Use |
|---|---|---|---|
| `review` | `title={clean_name}`, `comment={clean_name} {frame_start}-{frame_end} @ {fps} fps, start TC {timecode}` | reel name `auto` | Internal review. |
| `conform` | `title={clean_name}` | `timecode_source metadata`, `reelid_source metadata`, `reelid_attr owner` | Editorial / conform: timecode and reel name from the renders. |
| `client` | `title={clean_name}` | nothing copied, `reelid_source off` | Client delivery with nothing from the render headers. |
| `delivery` | `title`, `source_frame_rate={fps}`, `first_frame`, `last_frame`, `frame_count`, `slate_length`, `display_type={colour}`, `watermarking={burnins}` | nothing copied, reel name `auto` | Delivery tags, see [delivery keywords](#delivery-keywords). |
| `archive` | `title={clean_name}`, `comment=transcoded {date}, preset {preset}` | `copy all,-husk:*,-nuke/*,-MEDIA_PATH`, reel name `auto` | Keep header information, minus command lines and internal paths. |

## Reel name

The reel name (reel ID) ties a clip back to its source in an EDL or AAF when conforming.

| Option | Default | Description |
|---|---|---|
| `-reelid_source auto\|arg\|metadata\|off` | the metadata preset's, else `auto` | Where the reel name comes from; see below. |
| `-output_reelid 0\|1` | `1` | Write the reel name into the movie, or into each image of an EXR sequence written by oiiotool (`reelName`, the OpenEXR standard attribute). Other image formats have no reel name field. `0`: it is still found (log, slate, `{reel}` keyword) but not written; for EXR, a `reelName` carried over from the input is removed. |
| `-reelid TEXT` | the metadata preset's, else none | Explicit reel name. It may use the `-metadata` keywords, e.g. `-reelid "{clean_name}"`. |
| `-reelid_attr NAME` | the metadata preset's, else `owner` | EXR header attribute holding the reel name. Any attribute works, e.g. `-reelid_attr pipe/reel`. The standard OpenEXR names are also found under oiiotool's names, so `owner` also finds `Copyright`. Matching ignores case. |

| `-reelid_source` | Order | Result |
|---|---|---|
| `auto` (default) | `-reelid` → EXR attribute → none | Written when either exists; otherwise no reel name, silently. |
| `arg` | `-reelid` → none | Only the explicit value. Warns if `-reelid` is missing. |
| `metadata` (old name `exr`) | EXR attribute → `-reelid` → none | The header wins. Warns if neither exists. |
| `off` | — | No reel name is written. |

The first frame's header is used, EXR input only. The log shows the value and its source, e.g. `Reel name: A001C003 (EXR header owner)`.

Where it ends up, checked with ffprobe:

| Container | Stored as |
|---|---|
| mov | The reel name of the timecode track (QuickTime source reference, the `name` atom in the tmcd track), which Resolve, Premiere and Avid read when conforming. It needs the timecode track, so keep `-output_timecode 1` (the default); without it only the file tag below is written, with a warning. |
| mxf | Reel name of the source package. |
| mp4, mkv, … | A file-level `reel_name` tag only. ffmpeg doesn't write a reel name into an mp4 timecode track, so many conform tools won't see it. The tool warns; use mov or mxf for conforming. |

mov and mxf also get the file-level `reel_name` tag. The value is written as given, with no length limit. CMX3600 EDLs only keep 8 characters, so keep reel names short when they have to go through one.

## Burn-ins

Burn-ins can go in seven places: `tl` `tc` `tr` along the top, `bl` `bc` `br` along the bottom, and `wm`, the watermark, in the middle of the picture. `tc` and `bc` are centred.

**Watermark (`wm`).** Usually big, see-through text across the picture, e.g. `-burn_wm "CONFIDENTIAL"`.
- It is centred horizontally and vertically; multi-line text (`\n`) is centred line by line. Margins do not apply.
- By default it is 8% of the picture height at opacity 0.25. The preset-wide size and opacity (and `-burn_size` / `-burn_opacity`) do **not** change it, so a preset with small corner text still gets a big watermark. Set its own size and opacity in the preset (`"wm": {"text": "DRAFT", "size": "12%", "opacity": 0.15}`) or with `-burn_style "wm:size=12%,opacity=0.15"`.
- Colour and box follow the preset and the all-place options like any other place.
- Like the other burn-ins it is drawn over the letterbox and not on the slate.

| Option | Default | Description |
|---|---|---|
| `-burn_preset NAME` | the presets file's `default_burnin` (`none`) | Burn-in preset from the [presets file](#presets-file). `none` means no burn-ins. `--help` lists the presets, their text and styles. |
| `-burn_tl`, `-burn_tc`, `-burn_tr`, `-burn_bl`, `-burn_bc`, `-burn_br`, `-burn_wm TEXT` | from the burn-in preset | Text for one place. An empty value (`-burn_tl ""`) clears the preset's text there. |
| `-burn_color COLOR` | preset, else `white` | Text colour for all places: a name (`white`, `yellow`, `red`, …) or `#RRGGBB` / `0xRRGGBB`. |
| `-burn_opacity 0-1` | preset, else `1` | Opacity of text, shadow and box, for all places. `1` is solid, `0.5` half transparent. |
| `-burn_size SIZE` | preset, else `1.33%` | Text size for all places; see [Text size](#text-size). |
| `-burn_box 0\|1` | preset, else `0` | A black box behind the text instead of a drop shadow, for all places. The box has the place's opacity. |
| `-burn_style PLACE:KEY=VALUE,...` | *(none)* | Style for one place, e.g. `-burn_style "tc:color=yellow,size=2.5%,box=1"`. PLACE is `tl` … `br`, `wm` or `all`; keys are `color`, `opacity`, `size` and `box`. Repeatable; later ones win. |
| `-burn_margin SIZE` | preset, else 1.5% top/bottom, 1% left/right | Empty space between the picture edge and the text, all four sides. Pixels (`20`) or percent: `2%` is 2% of the picture **width** for left and right, 2% of the **height** for top and bottom. `0` puts the text right at the edge. |
| `-burn_margin_top`, `-burn_margin_bottom`, `-burn_margin_left`, `-burn_margin_right SIZE` | as above | One side only; wins over `-burn_margin`. Top applies to `tl` `tc` `tr`, bottom to `bl` `bc` `br`, left to `tl` `bl`, right to `tr` `br`; the centred places ignore left and right. |
| `-burn_font PATH` | the burn-in preset's `font`, else `./fonts/DroidSansMono.ttf` | Font file. If it's missing, a warning is printed and ffmpeg's default font is used. |

**Style layers.** Each place gets its colour, opacity, size and box from these layers, field by field; later ones win:

1. defaults: white, opacity 1, size 1.33%, no box
2. the preset's `color` / `opacity` / `size` / `box`
3. the preset's settings for that place
4. `-burn_color` / `-burn_opacity` / `-burn_size` / `-burn_box`
5. `-burn_style` for that place

For example, `-burn_preset wip -burn_style "tc:color=red"` keeps the WIP stamp's size and box but makes it red. The log lists every place with its text and final style.

**Margins** are layered the same way, side by side: defaults (1.5% top and bottom, 1% left and right) < the preset's `margin` < the preset's `margin_top` … `margin_right` < `-burn_margin` < `-burn_margin_top` … The margin is measured to the text itself; with `box`, the box border reaches a little further. Multi-line text at the bottom grows upwards, so its last line keeps the bottom margin. Margins other than the defaults are listed in the log.

### Text size

`size` (and `-burn_size`, `-burn_style …:size=`) takes three forms:

| Form | Example | Font size |
|---|---|---|
| percent of the picture height | `1.5%` | height × 0.015; 16 px at 1080, 32 px at 2160 |
| pixels | `40` or `40px` | always 40 px, whatever the resolution |
| height divided by N (older form) | `h/75` | height / 75; 14.4 px at 1080 |

The default is `1.33%`, the same as the older default `h/75`. Percentages and `h/N` scale with the output, which is usually what you want for burn-ins, since `-width` and `-par_out square` change the output height. Pixels stay fixed. "Picture height" is the height of the encoded movie.

**Changed meaning:** a bare number used to mean `h/N` and now means pixels. Older scripts with e.g. `-burn_size 75` should use `-burn_size h/75` or `-burn_size 1.33%`.

Keywords in the text:

| Keyword | Becomes |
|---|---|
| `{clean_name}` | Sequence name without frame number and separator (`shot_010`). |
| `{current_frame}` | The running frame number, starting at the first frame. |
| `{tc}` | The running timecode, starting at the movie's start timecode. Anything written after `{tc}` in the same place is dropped, because ffmpeg always places the timecode at the end. |
| `{frame_start}`, `{frame_end}` | First and last frame. Format specs work: `{frame_start:05d}`. |
| `{timecode}` | The start timecode, as fixed text. |
| `{fps}` | The frame rate as a number: `24`, `25`, `23.976`. |
| `{date}` | Today's date, `YYYY-MM-DD`. |
| `{preset}` | Name of the encode preset. |
| `{meta:ATTR}` | An attribute of the source header, e.g. `{meta:owner}`, `{meta:cameraModel}`, `{meta:nuke/input/filename}`. See [Header values](#header-values-metaattr). |
| `{version_string}`, `{version_number}`, `{namesplit1}` … `{namesplit-9}` | Parts of the sequence name; see [Name keywords](#name-keywords). |
| `{shot}`, `{task}`, … | Your own [name tokens](#custom-name-tokens) and [smart tokens](#smart-tokens). |

**New lines.** `\n` in a text starts a new line, e.g. `-burn_bc "{clean_name}\nTC {tc}"` or in `presets.json` `"tl": "{clean_name}\n{date}"`. A real line break in the JSON string (`"\n"` as JSON writes it) works too.

- Lines are aligned to match the place: left in `tl` / `bl`, centred in `tc` / `bc`, right in `tr` / `br`.
- Blocks at the bottom grow upwards and keep the same bottom margin as one line.
- A box covers the whole block.
- `{tc}` and `{current_frame}` work on any line; text after `{tc}` is still dropped.
- Centred and right-aligned multi-line text needs ffmpeg 6.1 or newer (drawtext's `text_align` option). Left-aligned and single-line text works with any version.

Any other characters are allowed, including `: ' % , [ ] ;`; the tool escapes them for ffmpeg. This includes characters inside header values. An unknown `{word}` is printed as typed, with a warning.

### Name keywords

These keywords come from the sequence name (the clean name, without frame number). They work everywhere keywords do: burn-ins, slate, `-metadata` values, metadata preset tags and `-reelid`.

**Version.** `-version_regex` (default `_v(\d\d\d)`) finds the version. If the name matches more than once, the last match counts.

| Keyword | `sh010_comp_v007` | Meaning |
|---|---|---|
| `{version_string}` | `007` | What group 1 of the regex matched (the whole match if the regex has no group). |
| `{version_number}` | `7` | The same without leading zeroes; `000` gives `0`. |

Both are empty if the name has no version, so a slate row `"Version": "{version_number}"` is then hidden. For other naming, e.g. `-version_regex "_v(\d+)"` for any number of digits, or `"[._]V(\d+)"` for `shot.V12`.

**Name splits.** The name split at `_`, counted from the start (`{namesplit1}` … `{namesplit9}`) or from the end (`{namesplit-1}` … `{namesplit-9}`). Parts that don't exist are empty.

| Name `AA_BBB_C_DD` | 1 | 2 | 3 | 4 | 5–9 |
|---|---|---|---|---|---|
| `{namesplitN}` | `AA` | `BBB` | `C` | `DD` | *(empty)* |
| `{namesplit-N}` | `DD` | `C` | `BBB` | `AA` | *(empty)* |

For example, for `ICOI2_005_033_renderCompSecondary_v000`, `-burn_tl "{namesplit2}_{namesplit3}"` burns in `005_033` and `-reelid "{namesplit1}"` gives the reel `ICOI2`.

### Custom name tokens

When the name keywords aren't enough, define your own keyword with a regular expression. `{NAME}` is group 1 of the first match (the whole match if the regex has no group). If nothing matches, it is empty, or the rule's `default`.

On the command line, the regex runs on the clean name:

```
-name_token "shot=^([A-Z0-9]+_\d+_\d+)"      ICOI2_005_033_comp_v002 -> {shot} = ICOI2_005_033
-name_token "pass=render([A-Za-z]+)_v"         ..._renderCompSecondary_v000 -> {pass} = CompSecondary
```

In `presets.json`, the top-level `name_tokens` object holds rules that always apply. A rule is either the regex, or an object that can also read another text:

```json
"name_tokens": {
  "shot": "^([A-Za-z0-9]+_\\d+_\\d+)",
  "task": {"regex": "_(comp|lgt|fx)(?:_|$)", "default": "none"},
  "camera": {"regex": "^(\\w+?)_", "source": "{meta:owner|}", "notes": "A001 from A001_C003_..."}
}
```

- `source` is the text searched, with keywords (default `{clean_name}`). It can use `{meta:ATTR}`, the name keywords and tokens defined earlier in the list.
- In JSON, a backslash is written twice: `\\d` for `\d`.
- `-name_token` adds a rule or replaces one of the same name; `-name_token NAME=` removes it.
- Names are letters, digits and `_`; built-in keyword names can't be reused.

The shipped `presets.json` defines `shot` (`SHOW_SEQ_SHOT` at the start of the name) and `task` (a department word such as `comp` or `lgt`). The log shows each token's value. Tokens work wherever the name keywords do: burn-ins, slate, metadata, `-reelid`, `-o` and `-ot`.

### Smart tokens

Smart tokens are custom keywords with more control than `-name_token`: conditions, any source text, a regex replacement and a case change. They live in `presets.json` only, grouped into named presets, typically one per project since each project names things its own way. Select one with `-smart_tokens NAME`, or set `default_smart_tokens`.

A preset is a list of definitions, processed **top to bottom**:

- Each definition sets one keyword. The same name may appear several times with different conditions; the last definition that applies wins.
- A definition is skipped if a condition fails, or if its regex doesn't match (in extract mode). The token then keeps what it had: an earlier value, or nothing.
- A later definition can use the tokens set above it, in its source, conditions and repl.
- A token that no definition sets is left as typed (`{task}`), with a warning.

| Field | Default | Meaning |
|---|---|---|
| `name` | *(required)* | The keyword: `"shot"` makes `{shot}`. Built-in keyword names can't be used. |
| `conditions` | none (always applies) | As in [OIIO transforms](#oiio-transforms): `[{"left": "{input_width}", "op": "==", "right": "4096"}]`, `==` or `!=`, keywords on both sides, all must hold. |
| `source` | `{clean_name}` | The text to work on. Any keyword, see below. |
| `regex` | none | Without a regex the value is the source (after the case change). |
| `repl` | `\1` | Python replacement: `\1`, `\2`, `\g<name>`; keywords are expanded, e.g. `{project}_\1`. Without a group in the regex, the default is the whole match. |
| `mode` | `extract` | `extract`: the first match, turned into `repl`. `replace`: every match in the source replaced by `repl` (`re.sub`); needs a regex. |
| `case` | `as_is` | `as_is`, `lower`, `upper` or `title` (`Title Case`), applied last. |
| `notes` | | Free text. |

Remember to double backslashes in JSON: `"\\d+"`, `"\\1"`.

**Keywords in sources, repl and conditions.** Smart tokens are worked out once the first frame's header is read, before any frame is converted, so they can be used in the [OIIO transform](#oiio-transforms) conditions too. Available then: `{clean_name}`, `{frame_start}`, `{frame_end}`, `{frames}`, `{date}`, `{user}`, `{preset}`, `{input}` (first file name), `{input_path}` (its full path, with `/`), `{input_folder}` (its folder name), `{input_width}`, `{input_height}`, `{input_par}` and the other input sizes, `{meta:ATTR}`, the [name keywords](#name-keywords), the `-name_token` / `name_tokens` keywords, and smart tokens set above. Timecode, fps and reel name are worked out later and can't be used.

**Where they work.** Everywhere the name keywords do: burn-ins, slate, metadata tags, `-reelid`, `-o`, `-ot`, and OIIO transform conditions and arguments. They are applied after `name_tokens` / `-name_token`, so a smart token with the same name replaces those.

The shipped `presets.json` has an `example_project` preset for names like `ICOI2_005_033_renderCompSecondary_v000`:

```json
"smart_tokens": {
  "example_project": {
    "tokens": [
      {"name": "project", "source": "{namesplit1}", "case": "upper"},
      {"name": "shot", "regex": "^([A-Za-z0-9]+)_(\\d+)_(\\d+)", "repl": "\\1_\\2_\\3"},
      {"name": "shot_label", "source": "{shot}", "regex": "_", "repl": " ", "mode": "replace"},
      {"name": "task", "source": "other"},
      {"name": "task", "regex": "_render([A-Z][a-z]+)", "case": "lower"},
      {"name": "task", "conditions": [{"left": "{namesplit-2}", "op": "==", "right": "plate"}],
       "source": "plate"},
      {"name": "version", "source": "v{version_string}"},
      {"name": "version", "conditions": [{"left": "{version_string}", "op": "==", "right": ""}],
       "source": "noversion"}
    ]
  }
}
```

| Name | `{project}` | `{shot}` | `{shot_label}` | `{task}` | `{version}` |
|---|---|---|---|---|---|
| `ICOI2_005_033_renderCompSecondary_v000` | `ICOI2` | `ICOI2_005_033` | `ICOI2 005 033` | `comp` | `v000` |
| `abc_010_020_plate_v001` | `ABC` | `abc_010_020` | `abc 010 020` | `plate` | `v001` |
| `abc_010_020_lookdev` | `ABC` | `abc_010_020` | `abc 010 020` | `other` | `noversion` |

The pattern for a fallback is to put it first and the specific cases after it, as `task` does. The log lists each token's final value; `-v` shows every definition, applied or skipped, and why.

### Header values `{meta:ATTR}`

`{meta:ATTR}` inserts an attribute from the EXR header of the sequence's **first frame**, whichever frame `-i` points at. The value is fixed text for the whole movie. It works in burn-ins, `-metadata` values, metadata preset `tags` and `-reelid`.

- `ATTR` is found by its oiiotool name or its OpenEXR name, ignoring case, so `{meta:owner}` finds `Copyright`. Names with `:` or `/` work as they are: `{meta:smpte:TimeCode}`, `{meta:nuke/input/filename}`.
- `{meta:ATTR|fallback}` gives text to use when the attribute is missing or empty, e.g. `{meta:shoot_scene_location|unknown location}`. Without a fallback a missing attribute becomes empty text.
- The log shows each value once (`Header value {meta:owner} = '…'`) and warns about missing ones.
- The raw value is used; the reel name's frame-range cleaning doesn't apply here.
- Only single values are available (text, numbers, rationals like `24/1`), not matrices or lists.
- Only EXR headers are read; for other input every `{meta:…}` is its fallback.
- `-write_exr_info 1` saves the header as JSON, which is the easiest way to find attribute names.

Example: `-burn_tl "{meta:owner}" -burn_tc "{meta:cameraModel} / {meta:lensModel}" -burn_bl "{meta:nuke/input/filename|}"`.

## Slate

A slate is one information frame placed before the first picture: shot name, frame range, timecode, format, colour, reel, notes and a thumbnail of the shot. It follows the usual VFX conventions:

- **Frame and timecode.** The slate is frame *start − 1*, and its timecode is the start timecode − 1. The movie's timecode track starts at the slate, so the first picture keeps its own timecode, e.g. slate `00:00:41:16`, first picture `00:00:41:17`. A start timecode of `00:00:00:00` can't go lower, so the slate also gets `00:00:00:00`. Because a movie's timecode runs continuously, the pictures then start at `00:00:00:01`; a warning says so, and `{timecode}` shows that value everywhere.
- **Same format as the pictures.** The slate is written next to the intermediate frames with the same width, height and pixel format. It goes through the same scale, pixel aspect reformat and colour tags. After drawing it, the tool checks the size and stops with an error if it doesn't match.
- **Pixel aspect.** For anamorphic input the slate is laid out at display proportions (width × pixel aspect) and squeezed to the stored width. Text and thumbnail therefore look right once the player un-squeezes the movie, and with `-par_out square` as well.
- **Burn-ins** are not drawn on the slate. Their counters still count it, so the first picture shows the right `{current_frame}` and `{tc}`. `-slate_burnin 1` draws them on the slate too.
- **Colour.** The thumbnail is taken from the converted intermediates, so it is in the output colourspace like the rest of the movie. The slate's own colours (background, text) are output values and are not colour-converted.

| Option | Default | Description |
|---|---|---|
| `-slate NAME` | the presets file's `default_slate` (`none`) | Slate preset from the [presets file](#presets-file); `none` means no slate; `plain` means a slate from the `-slate_*` options only (title `{clean_name}`, no rows until `-slate_field` adds them), ignoring `default_slate`. `--help` lists the presets and their rows. |
| `-slate_title TEXT` | the preset's, else `{clean_name}` | Big first line. |
| `-slate_subtitle TEXT` | the preset's | Smaller line under the title. |
| `-slate_field "LABEL=TEXT"` | *(none)* | Adds a row, or replaces the row with that label: `-slate_field "Notes=first comp pass"`. `"LABEL="` removes a row. Repeatable; new rows go at the end. |
| `-slate_thumbnail middle\|first\|last\|none\|FRAME` | the preset's, else `middle` | Which frame is shown as a thumbnail. If that frame is a hole or failed, the nearest existing frame is used. |
| `-slate_scale PERCENT` | the preset's, else `100%` | Size of the whole slate layout: text, spacing, the rule and the thumbnail. `100%` fills the picture; `50%` is everything at half size, centred in the picture. `50%`, `50` and `0.5` all mean half. From 5% to 100%. |
| `-slate_valign center\|top` | the preset's, else `center` | Vertical placement of the content: centred (same space above and below), or from the top. |
| `-slate_title_size SIZE` | the preset's, else `5%` | Title text size: percent of the picture height or pixels, as in [Text size](#text-size). |
| `-slate_subtitle_size SIZE` | the preset's, else `3.2%` | Subtitle text size. |
| `-slate_size SIZE` | the preset's, else `2.5%` | Text size of the rows, labels and values. |
| `-slate_value_color COLOR` | the preset's, else `white` | Colour of the row values. |
| `-slate_rule 0\|1` | the preset's, else `0` | A line under the title, in `line_color`. |
| `-slate_label_color COLOR` | the preset's, else `#7a7a7a` | Colour of the row labels (the field names), usually a darker grey. |
| `-slate_style "KEY=VALUE,..."` | *(none)* | Any style setting, e.g. `-slate_style "title_color=yellow,background=#000000"`. Keys: `background`, `title_color`, `subtitle_color`, `value_color`, `label_color`, `line_color`, `title_size`, `subtitle_size`, `size`, `scale`, `valign`, `rule` (1/0), `hide_empty` (1/0), `logo_position`, `logo_size`, `logo_x`, `logo_y`, and `color` (title and values). Repeatable; wins over the options above. |
| `-slate_burnin 0\|1` | `0` | Draw the burn-ins on the slate as well. |
| `-slate_logo PATH` | the preset's, else none | Vendor logo drawn on the slate: an image, best a PNG with alpha (its transparent parts show the slate background). `./` is the application folder; environment variables work. `-slate_logo ""` removes a preset's logo. A missing file gives a warning and a slate without the logo. |
| `-slate_logo_position POS` | the preset's, else `tr` | `tl` `tc` `tr` / `ml` `mc` `mr` / `bl` `bc` `br`: top / middle / bottom, then left / centre / right. Words work too: `top-right`, `bottom-left`, `center`, `bottom`. |
| `-slate_logo_size SIZE` | the preset's, else `10%` | Logo height: percent of the picture height, or pixels. The width follows the logo's proportions. |
| `-slate_logo_x SIZE`, `-slate_logo_y SIZE` | the preset's, else `4%` and `5%` | Distance from the left or right edge (percent of the width, or pixels) and from the top or bottom edge (percent of the height, or pixels). `0` is allowed. Not used along a centred axis. |

The logo is placed on the whole slate, not inside the `-slate_scale` box, and is drawn over the thumbnail and under the text. Anamorphic slates are laid out at display proportions, so the logo keeps its shape after un-squeezing.

The `-slate_*` options without `-slate` make a plain slate: title `{clean_name}`, only the rows you give, and a thumbnail. With `-slate none` they are ignored, with a warning. `-skipffmpeg` makes no slate, because ffmpeg draws it.

**Keywords** for the title, subtitle and rows:

| Keyword | Value |
|---|---|
| `{clean_name}` | Sequence name without frame number. |
| `{frame_start}`, `{frame_end}`, `{frames}` | First and last picture frame, and their count (holes included). |
| `{timecode}`, `{timecode_end}` | Timecode of the first and last picture. |
| `{duration}` | Length as timecode, e.g. `00:00:04:12`. |
| `{fps}` | Frame rate. |
| `{width}`, `{height}`, `{par}` | Size and pixel aspect of the encoded movie. |
| `{colour}` | The colour transform, e.g. `ACEScg -> out_rec709`; empty for non-EXR input. |
| `{reel}` | The [reel name](#reel-name), empty if none. |
| `{preset}`, `{date}`, `{user}`, `{input}` | Encode preset, today's date, login name, the `-i` file name. |
| `{meta:ATTR}`, `{meta:ATTR\|fallback}` | Any [header value](#header-values-metaattr) of the first frame. |
| `{version_string}`, `{version_number}`, `{namesplit1}` … `{namesplit-9}` | The [name keywords](#name-keywords). |

`\n` starts a new line. Long values wrap next to the thumbnail. **Rows whose value comes out empty are left out** (unless the preset sets `"hide_empty": false`). So `"Artist": "{meta:Artist|}"` shows the row only when the header has an Artist, and a `Notes` row appears once you give it text. If the rows don't fit the picture height, all text shrinks until they do.

**Scale.** Sizes given in percent are percent of the picture height, and `-slate_scale` multiplies everything: with `-slate_scale 50%`, a title of `5%` is drawn at 2.5% of the height, and pixel sizes are halved as well. Spacing and thumbnail shrink the same way, and the smaller layout is centred in the picture. The background always fills the whole frame.

Shipped slate presets:

| Preset | Rows | Thumbnail |
|---|---|---|
| `generic` | subtitle from `ImageDescription`; Date, Artist, Frames, Timecode, Duration, Format, Colour, Codec, Reel | middle |
| `client` | Date, Duration, Notes (bigger text, 2.6%) | middle |
| `editorial` | Reel, TC in, TC out, Frames, FPS, Format, Date | none |

The layout (title, label/value rows, thumbnail, rows hidden when empty) follows the HTML slate of [ynput/LabLib](https://github.com/ynput/LabLib). LabLib renders HTML in a headless browser; here ffmpeg draws the slate, so nothing else needs to be installed.

## Letterbox

The letterbox covers part of the picture with bars, to show how it looks at a target aspect ratio. Which kind of bars you get follows from the movie's **display** ratio (width × pixel aspect ÷ height):

- target ratio **wider** than the picture (2.39 on a 16:9 movie): bars top and bottom, a **letterbox**.
- target ratio **narrower** (4:3 on a 16:9 movie, or 2.39 on an anamorphic 2:1 movie that displays at about 4.8:1): bars left and right, a **pillarbox**.
- the same ratio (within a pixel): no bars, and the log says so.

The bars are drawn on the encoded size, after scaling (`-width` or `-ff_transform`), pixel aspect reformat and padding. They are drawn before `-ff_filter` and before the burn-ins, so burn-ins are drawn on top of them. The [slate](#slate) never gets bars.

There are two letterboxes, which can be used separately or together:
- **`-letterbox`** is drawn by ffmpeg, on the movie (or a movie → JPG … sequence).
- **`-oiio_letterbox`** is drawn by oiiotool, on the converted frames, so it needs no ffmpeg. It works for EXR input, and for every oiiotool-only [image sequence output](#image-sequence-output).
  - It measures the frames' size after the OIIO transform (one frame, with `--echo`), then draws exact `--box` bars.
  - Colours are in the output's values: black is black, but other colours in a linear EXR are linear values.
  - Its options mirror the ffmpeg ones: `-oiio_letterbox 0|1`, `-oiio_letterbox_ratio`, `-oiio_letterbox_color`, `-oiio_letterbox_line`, `-oiio_letterbox_line_color`.

| Option | Default | Description |
|---|---|---|
| `-letterbox 0\|1` | `0` | Turn the letterbox on. The other options only apply when it is on; giving them alone prints a warning. |
| `-letterbox_ratio RATIO` | `2.39` | Target ratio: `2.39`, `2.39:1`, `1.85`, `16:9`, `16/9`, `4:3` … |
| `-letterbox_color R,G,B,A` | `0,0,0,255` | Bar colour, 0–255 per channel. Alpha below 255 makes the bars see-through: `0,0,0,160` darkens the covered part instead of hiding it. `R,G,B` (solid) and `#RRGGBBAA` work too. |
| `-letterbox_line PIXELS` | `0` | A line along the inner edge of each bar, next to the visible picture, in movie pixels. `0` = no line. It is drawn inside the bar, so it never covers the visible picture. |
| `-letterbox_line_color R,G,B,A` | `255,255,255,255` | Colour of that line, same format as `-letterbox_color`. |

The log shows the result, e.g. `Letterbox 2.39: letterbox, bars of 138 px top and bottom, line 2 px`.

## Alpha

By default the output has no alpha channel, whatever the input has. `-alpha keep` carries the input's alpha into the output, e.g. for comp elements in ProRes 4444. `-alpha auto` does the same when it can, and quietly does without when it can't.

| Option | Default | Description |
|---|---|---|
| `-alpha drop\|keep\|auto` | `drop` | `drop`: no alpha in the output. `keep` writes the input alpha to the output. A movie's encode preset must be able to store alpha: presets with `alpha_pix_fmt` (shipped: `prores4444`, marked `[alpha]` in `--help`), or `-ff_encode` with an alpha pixel format (e.g. `-pix_fmt yuva420p … -c:v libvpx-vp9` for WebM). An [image sequence](#image-sequence-output) must be `exr`, `png` or `tif`. Otherwise the tool stops with an error before converting anything. The intermediates must be `png` or `tif`. `auto`: as `keep` when the output can store alpha, else as `drop`, with an info line saying why (no error); an input without alpha is an info line too, where `keep` warns. |
| `-alpha_premult 0\|1` | `0` | Only with `-alpha keep`. `0`: the colour is written **straight** (unpremultiplied), which is what most players and editors expect in a mov. `1`: the colour is **premultiplied** by alpha. |

How it works:

- **EXR input.** The alpha channel next to the chosen colour channels is used (`rgba.A` for `rgba.R,G,B`, `A` for `R,G,B`, `beauty.A` …). EXR colour is premultiplied, so oiiotool resizes first, then divides by alpha (`--unpremult`), then applies the colour transform to the straight colour. The intermediate PNGs are straight, as PNG expects. If there is no alpha channel, or the picture is a deep depth view, a warning is printed and the movie gets no alpha.
- **Other input** (PNG, TIFF …) is used as it is and taken to be straight, as PNG defines. Whether it has alpha is read from the first frame.
- **Image sequence output.** oiiotool writes the alpha into EXR (premultiplied, as EXR expects), PNG and TIF (straight). For PNG / TIFF input with `drop`, oiiotool removes the alpha channel (`--ch R,G,B`). A movie to PNG / TIF sequence is written by ffmpeg as `rgba` (`rgba64` with `-mezzanine_16bit 1`).
- **Premultiplied output** is done by ffmpeg (`premultiply`, in 16-bit RGB) at the start of the filter chain.
- **Burn-ins** are drawn opaque where their text is, also over transparent parts. **Letterbox bars** are composited over the picture with their own opacity, so they also show over transparent parts. The **slate** is fully opaque.
- Colour in fully transparent pixels (alpha 0) can't survive straight output: additive glows with no alpha disappear. That is a property of straight alpha, not of the tool.

The log shows the result, e.g. `Alpha: kept (rgba.A), written straight (unpremultiplied), yuva444p10le`.

## Thumbnail

A thumbnail is a still image (jpg or png) that goes with the movie, or is made on its own.

- **Same look as the movie.** For EXRs it is a separate oiiotool run on one source frame, with the same processing: channels, alpha, [OIIO transform](#oiio-transforms), [colour or CLF](#colour) and `-oiio_command`. After that come the thumbnail's own size and file format. Burn-ins, letterbox and slate are not part of it. It doesn't use the movie's intermediate files. Other input (PNG, JPG …) is scaled by ffmpeg.
- **Thumbnail only.** `-ot` without `-o` makes only the thumbnail: no movie, no intermediate frames.

| Options | Result |
|---|---|
| neither `-o` nor `-ot` | the movie, at its default place |
| `-o …` | the movie |
| `-ot …` | only the thumbnail |
| `-o … -ot …` | movie and thumbnail; `-o ./` = the movie at its default place |

| Option | Default | Description |
|---|---|---|
| `-ot [PATH]` | *(no thumbnail)* | Write a thumbnail. PATH works like [`-o`](#output-path): file or folder, `./` and `../` relative to the input folder, keywords, plus `{frame}` (the frame used). The extension (`.jpg` / `.png`) is added if missing. `-ot` without PATH puts `<name>.<ext>` next to the movie, or in the input folder when there is no movie. |
| `-thumbnail_preset NAME` | the presets file's `default_thumbnail` (`middle1280`) | Thumbnail preset: frame, size, format and compression. `--help` lists them. |
| `-thumbnail_frame middle\|first\|last\|largest\|FRAME` | the preset's | Which frame. `middle` (the frame nearest the middle of the range), `first`, `last`, a frame number (the nearest existing frame), or `largest`, described below. |

**`largest`.** After oiiotool has converted the movie's frames, the tool picks the frame whose converted image is the biggest file. File size roughly follows detail, so this is the busiest frame. It falls back to `middle` when all files are the same size, and when there is no movie (thumbnail only), because the converted frames are needed. The log says which.

**Size.** The preset's `width` / `height` replace the movie's `-width` resize, so there is one resize, not two; an OIIO transform still applies. `0` means "follow the other side"; both `0` gives the movie's size. Anamorphic input is made square-pixel (oiiotool `--pixelaspect 1`), since thumbnails are viewed as plain images.

**Alpha.** With `-alpha keep`, a png thumbnail keeps the alpha (straight). A jpg thumbnail has none.

The log shows the choice and the file, e.g. `Thumbnail: frame 1005 (middle), 1280xauto, jpg quality 90 -> D:/shots/sh010/sh010_comp_v003.jpg`.

Shipped thumbnail presets:

| Preset | Frame | Size | Format |
|---|---|---|---|
| `middle1280` (default) | middle | 1280 wide | jpg, quality 90 |
| `start512` | first | 512 wide | jpg, quality 85 |
| `largest1920` | largest | 1920 wide | jpg, quality 92 |
| `middle_png` | middle | the movie's size | png, compression 6 |

## Filmstrip

A filmstrip is one jpg showing the whole shot at a glance: evenly spaced frames, each in a tile of the same size, laid out in a grid. By default that's 24 frames of 480x270 in one row (11520x270).

- **Same look as the thumbnail.** It is display-referred, in the same output colourspace as the movie and the [thumbnail](#thumbnail). Anamorphic pictures are made square-pixel.
- **Where the tiles come from** (`-filmstrip_source`):

  | Source | Tiles from | Burn-ins, letterbox | Needs |
  |---|---|---|---|
  | `source` (default) | The source frames. EXR: one oiiotool run per tile with the movie's processing (channels, alpha, [OIIO transform](#oiio-transforms), [colour or CLF](#colour), `-oiio_command`); the tile size replaces the movie's `-width` resize. Other input: one ffmpeg run. | no | nothing: also works without a movie |
  | `intermediates` | The frames oiiotool converted for the movie, before ffmpeg. No second colour conversion, so it's quicker than `source` for EXRs. | no | EXR input, with the movie (`-o`) or `-skipffmpeg`. For other input, the source frames *are* the intermediates. |
  | `movie` | The encoded movie, read back by ffmpeg (Rec.709 YUV to RGB). Exactly what the movie shows, codec included. | yes | the movie (`-o`, without `-skipffmpeg`) |

  When a source isn't available in a run, the source frames are used, with a warning. EXR tiles are made in parallel (as many at a time as `-oiio_threads`, default all cores) in the intermediate folder, and one more oiiotool run puts them together with `--mosaic`.
- **Slate.** Left out by default. With `-filmstrip_slate 1` (or `"slate": true`) the slate is the first tile and the frames fill the other tiles; the grid keeps its size. It needs a run that makes the slate: the movie with a slate. Otherwise all tiles are frames, with a warning.
- **Tiles.** Each frame is fitted into the tile with its aspect ratio kept; the rest of the tile is black (oiiotool `--fit:pad=1`, ffmpeg `scale` + `pad`). Mono EXR channels are shown as grey.
- **Frames.** The tiles are spread evenly over the frames on disk, from the first frame to the last, both included, and no frame is used twice. A sequence with fewer frames than tiles uses every frame, and the rest of the cells stay black, as do the cells of a grid bigger than the tile count. A tile whose frame fails to convert stays black, with a warning.
- **Filmstrip only.** `-of` without `-o` makes only the filmstrip (with `-ot` as well: the thumbnail and the filmstrip). Use `-o ./` for the movie too, as with `-ot`.

| Option | Default | Description |
|---|---|---|
| `-of [PATH]` | *(no filmstrip)* | Write a filmstrip. PATH works like [`-ot`](#thumbnail): file or folder, `./` and `../` relative to the input folder, keywords. The extension `.jpg` is added if missing. `-of` without PATH puts `<name>_filmstrip.jpg` next to the movie, or in the input folder when there is no movie. |
| `-filmstrip_preset NAME` | the presets file's `default_filmstrip` (`row24`) | Filmstrip preset: tile count, tile size, grid and jpg quality. `--help` lists them. |
| `-filmstrip_tiles N` | the preset's (`24`) | Number of tiles, i.e. frames, 1–1000. Without `-filmstrip_grid` the strip stays one row (a preset with a full grid keeps its columns). |
| `-filmstrip_tile WxH` | the preset's (`480x270`) | Size of one tile in pixels. |
| `-filmstrip_quality 1-100` | the preset's (`90`) | jpg quality. |
| `-filmstrip_source source\|intermediates\|movie` | the preset's (`source`) | Where the tiles come from, see above. |
| `-filmstrip_slate 0\|1` | the preset's (`0`) | `1`: the slate is the first tile. |
| `-filmstrip_grid COLSxROWS` | one row | Layout, filled left to right, then top to bottom. `6x4`; `6x` (or `6x0`) = 6 columns and as many rows as needed; `x2` = 2 rows. Without `-filmstrip_tiles`, a full grid sets the tile count to its cells (`-filmstrip_grid 8x3` = 24 tiles). A grid with fewer cells than tiles is an error. |

The log shows the result, e.g. `Filmstrip: 24 frames (1001-1100 evenly spaced) from the source, 24 tiles of 480x270, 24x1 grid (11520x270), jpg quality 90 -> D:/shots/sh010/sh010_comp_v003_filmstrip.jpg`, or `Filmstrip: slate + 23 frames ... from the movie ...`.

Shipped filmstrip presets:

| Preset | Tiles | Tile | Grid | Size |
|---|---|---|---|---|
| `row24` (default) | 24 | 480x270 | 24x1 | 11520x270, jpg 90 |
| `grid6x4` | 24 | 480x270 | 6x4 | 2880x1080, jpg 90 |
| `row12_small` | 12 | 320x180 | 12x1 | 3840x180, jpg 85 |
| `movie6x4` | slate + 23 frames, from the movie | 480x270 | 6x4 | 2880x1080, jpg 85 |

## Preview

`-preview FILE.png` makes one frame instead of the movie, exactly as the movie would show it. The [tuner](TUNER.md) uses it; it is also handy for checking settings from a script.

- The frame is chosen like a [thumbnail](#thumbnail) frame: `-thumbnail_frame` (default `middle`; `first`, `last` or a frame number). `largest` falls back to middle.
- oiiotool converts only that frame, with the movie's processing. The converted frame is cached by its exact oiiotool settings (in `vfx-transcode/preview-cache` in the user temp folder, last 60 frames). Changes to burn-ins, slate, letterbox or metadata therefore don't convert it again.
- ffmpeg runs the movie's filter chain on it: scale, pad, pixel aspect, letterbox, burn-ins. It converts the result to the encode preset's pixel format and back to RGB, so chroma subsampling and video range look like the movie; codec compression is not simulated.
- `{current_frame}` and `{tc}` show the previewed frame's own number and timecode.
- Output files:
  - `FILE.png`: the frame.
  - `FILE_slate.png`: the slate, when there is one.
  - `FILE.json`: the input header, the keyword values, the output size, pixel aspect, pixel format, frame rate, timecode, reel name, colour, transform, letterbox and metadata tags.
- `-o`, `-ot` and `-of` are ignored with `-preview`.

## Encoding (ffmpeg)

| Option | Default | Description |
|---|---|---|
| `-preset NAME` | the presets file's `default_encode` (`h264`) | Encode preset. `--help` lists the available ones with their notes. |
| `-presets_file PATH` | `./presets.json` | JSON file holding the encode, burn-in, metadata and slate presets and the channel preferences. See [Presets file](#presets-file). |
| `-ff_out_ext EXT` | the preset's `ext` | Output extension/container, e.g. `mov`, `mp4`, `mkv`, `mxf`. |
| `-ff_encode ARGS` | *(none)* | Replaces the preset's codec arguments for one run. It must contain `{videofilter}` as a separate word where the `-vf` filter goes. No colour tags are added. Example: `-ff_encode "-pix_fmt yuv420p {videofilter} -c:v libx264 -crf 23"`. |
| `-ff_transform_preset NAME` | *(none)* | An [ffmpeg transform preset](#colour-and-ffmpeg-transform-presets) from the presets file, e.g. `hd_fit_pad`. `-ff_transform` wins. |
| `-ff_transform FILTERS` | *(none)* | The ffmpeg transform: filters that replace the `-width` scale, e.g. `-ff_transform "scale=w=2048:h=-2:flags=lanczos"` or a `zscale` chain. Separate filters with commas or new lines; a line starting with `#` is skipped. They run after the pixel aspect reformat and before the colour-matrix conversion, letterbox and burn-ins. `-width` still sets oiiotool's resize of EXRs (`-width 0` leaves it all to `-ff_transform`). |
| `-ff_filter FILTERS` | *(none)* | Extra ffmpeg video filters, inserted after scale/pad and before the burn-ins, e.g. `-ff_filter "eq=gamma=1.1"`. Used as given, so escape ffmpeg special characters yourself. |
| `-skipffmpeg 0\|1` | `0` | Only convert the EXRs. The movie isn't made and the intermediates are kept (implies `-temp_keep 1`); the log prints their folder. Add `-temp_location source` to get them next to the source files. |

With the preset's colour tags, the frames themselves are tagged too (`setparams`). Newer ffmpeg builds (seen with 7.1) take the colour tags from the frames, i.e. from the intermediate PNGs, whose gamma chunk can make the movie come out as "unknown/linear" despite `-color_trc`.

Every encode also sets the colour matrix explicitly (`scale=in_range=full:in_color_matrix=bt709:out_range=tv:out_color_matrix=bt709`) and pads odd sizes to even ones.

## Presets file

`presets.json` holds the encode, burn-in and metadata presets and the channel preferences. Edit, add or remove presets freely. If the file has a mistake, the tool reports the file and line, or the preset and field.

### Colour and ffmpeg transform presets

`colour_presets` name colour settings for the OCIO config; `ff_transforms` name `-ff_transform` filter chains. The shipped file has some of each (the colour names are the ACES 1.2 config's):

| Colour preset | Settings |
|---|---|
| `acescg_to_rec709` | `-ci "ACES - ACEScg" -co "Output - Rec.709"` (the usual review look) |
| `acescg_to_srgb` | `-ci "ACES - ACEScg" -co "Output - sRGB"` |
| `aces2065_to_rec709` | `-ci "ACES - ACES2065-1" -co "Output - Rec.709"` |
| `rec709_to_acescg` | `-ci "Output - Rec.709" -co "ACES - ACEScg"` (inverse output transform; bright values can go well above 1.0) |
| `srgb_to_acescg` | `-ci "Output - sRGB" -co "ACES - ACEScg"` |
| `srgb_texture_to_acescg` | `-ci "Utility - sRGB - Texture" -co "ACES - ACEScg"` (plain sRGB curve, for textures and matte paintings) |
| `logc3_to_rec709` | `-ci "Input - ARRI - V3 LogC (EI800) - Wide Gamut" -co "Output - Rec.709"` |
| `acescg_display_rec709` | `-ci "ACES - ACEScg" -display ACES -view Rec.709` |

```json
"colour_presets": {
  "rec709_to_acescg": {"notes": "Rec.709 video into ACEScg", "ci": "Output - Rec.709", "co": "ACES - ACEScg"}
},
"ff_transforms": {
  "hd_fit_pad": {
    "notes": "Fit inside 1920x1080 keeping the aspect, black bars to fill.",
    "filters": ["scale=w=1920:h=1080:force_original_aspect_ratio=decrease", "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"]
  }
}
```

| Colour preset field | Description |
|---|---|
| `ci`, `co` | Colourspace in / out, as `-ci` / `-co`. |
| `display`, `view`, `looks` | As `-display` / `-view` / `-looks`, instead of `co`. |
| `clf` | A CLF file, as `-clf`; not with `display`, `view` or `looks`. |
| `ocio` | The OCIO config, as `-ocio`. Left out: `-ocio` or the default config. |
| `notes` | Shown in `--help` and the tuner. |

An ffmpeg transform preset has `filters` (one filter per entry, or one string; entries starting with `#` are skipped) and `notes`. The shipped ones: `hd_lanczos` (1920 wide), `hd720_bicubic` (1280 wide), `2k_zscale` (2048 wide, zscale spline36) and `hd_fit_pad` (fit inside 1920x1080 with black bars).

### Saving presets from the tuner

The tuner's **Save as preset...** buttons (Color, Transform, Transform Sequence, Burn-in, Slate, Filmstrip) write a named preset into the presets file in use. With [studio preferences](#which-presets-file-is-used), that is the studio `presets.json`, so everyone gets it.

- A new name adds a preset; an existing name replaces it (the tuner asks first). Transform Sequence saves a sub-preset: into an existing transform preset (replacing a sub-preset of the same name, which keeps its conditions) or as a new transform preset with that one sub-preset.
- The save is [multi-user safe](#super-presets), like the super-presets: a temporary file first, a short lock, someone else's save merged in, the previous version kept as `presets_safecopyNNN.json`.
- Before the new file replaces the old one, it is loaded as vfx-transcode would load it; if that fails, nothing changes.
- The file is rewritten with its own layout (two-space indents, short lists of small objects such as conditions on one line), so the only lines that change are the saved preset's. Everything else, `_notes` and the `tools` paths included, stays as it is.
- If the presets file can't be written (no write access to the share), the tuner says so and nothing is saved. Ask whoever looks after the studio preferences, or point the Presets file field at a file you can write.

### Which presets file is used

1. `-presets_file PATH`, when given (`./` = the application folder).
2. The **studio preferences** named in `vfx-transcode.ini`, when that file exists next to `vfx-transcode.py` / `vfx-transcode.exe`, its path exists and its presets file loads.
3. `presets.json` next to the application, as before. A built .exe needs it copied next to the .exe.

`vfx-transcode.ini` (the shipped one has an empty path, so step 3 is used):

```ini
[vfx-transcode]
studio_prefs = \\server\pipeline\vfx-transcode
```

- `studio_prefs` is a folder holding `presets.json`, or the path of the presets file itself. Environment variables work (`%PIPELINE_ROOT%\vfx-transcode`), and `./` is the application folder.
- If the path is empty, the local `presets.json` is used without a message. If it doesn't exist, or the file fails to load (a JSON mistake, the share is offline), the local `presets.json` is used and a warning says why, e.g. `Warning: Studio preferences not used: "\\server\pipeline\vfx-transcode\presets.json" not found (studio_prefs in vfx-transcode.ini); using D:\apps\vfx-transcode\presets.json`.
- When the studio preferences are used, the log says so: `Presets: \\server\...\presets.json (studio preferences from vfx-transcode.ini)`.
- `super_presets.json` is read from the folder of the presets file used, so studio super-presets live on the share next to the studio `presets.json`.
- `./` in the presets file's `tools` paths still means the application folder, not the share. Give studio tools as absolute or UNC paths.
- The tuner uses the same order; its Presets file field shows the default it found.

```json
{
  "_notes": ["free text, ignored"],
  "default_encode": "h264",
  "default_burnin": "none",
  "default_metadata": "none",
  "default_slate": "none",
  "channel_preferences": ["rgba.R,rgba.G,rgba.B,rgba.A", "R,G,B,A", "R,G,B", "beauty.R,beauty.G,beauty.B"],
  "encode_presets": {
    "my-proxy": {
      "notes": "Shown in --help. A string or a list of strings.",
      "ext": "mov",
      "encode": "-pix_fmt yuv422p {videofilter} -c:v prores_ks -profile:v 0 -vendor apl0",
      "ten_bit": false,
      "colour_tags": true,
      "metadata": {"vendor_id": "appl"}
    }
  },
  "metadata_presets": {
    "my-conform": {
      "notes": "Shown in --help.",
      "tags": {"title": "{clean_name}", "comment": "vfx v{frame_start}"},
      "timecode_source": "metadata",
      "reelid_source": "metadata",
      "reelid_attr": "owner"
    }
  },
  "burnin_presets": {
    "my-review": {
      "notes": "Shown in --help.",
      "tl": "{clean_name}",
      "tr": "TC {tc}",
      "br": "{current_frame} / {frame_end}",
      "size": "2%"
    }
  },
  "slate_presets": {
    "my-slate": {
      "notes": "Shown in --help.",
      "title": "{clean_name}",
      "subtitle": "{meta:ImageDescription|}",
      "fields": {"Date": "{date}", "Frames": "{frame_start} - {frame_end}", "Artist": "{meta:Artist|}"},
      "thumbnail": "middle",
      "size": "2.5%"
    }
  }
}
```

| Top-level key | Description |
|---|---|
| `tools` | Paths to `ffmpeg`, `ffprobe` and `oiiotool`, see [Tools](#tools). Optional; any other key is an error. |
| `default_encode` | Encode preset used without `-preset`. Defaults to the first one. |
| `sequence_presets` | oiiotool output settings for image sequence outputs, see [Sequence presets](#sequence-presets). |
| `default_sequence_presets` | `{"exr": NAME, "jpg": NAME, …}`: the sequence preset each format uses without `-seq_preset`. |
| `default_burnin` | Burn-in preset used without `-burn_preset`. `none` (or leaving it out) means no burn-ins. |
| `encode_presets` | The encode presets. The older key name `presets` (with `default`) is still accepted. |
| `burnin_presets` | The burn-in presets. Optional. The name `none` is reserved. |
| `default_metadata` | Metadata preset used without `-metadata_preset`. `none` (or leaving it out) means none. |
| `metadata_presets` | The metadata presets. Optional. The name `none` is reserved. |
| `default_slate` | Slate preset used without `-slate`. `none` (or leaving it out) means no slate. |
| `slate_presets` | The slate presets. Optional. The name `none` is reserved. |
| `default_oiio_transform` | Transform preset used without `-oiio_transform`. `none` (or leaving it out) means none. |
| `default_thumbnail` | Thumbnail preset used without `-thumbnail_preset`. |
| `thumbnail_presets` | The [thumbnail](#thumbnail) presets, see below. |
| `default_filmstrip` | Filmstrip preset used without `-filmstrip_preset`. Without one, the built-in default: 24 tiles of 480x270 in one row, jpg 90. |
| `filmstrip_presets` | The [filmstrip](#filmstrip) presets, see below. The name `none` is reserved. |
| `oiio_transforms` | A list of [OIIO transform](#oiio-transforms) presets: `{"name": ..., "description": ..., "subs": [{"name": ..., "description": ..., "conditions": [...], "transforms": [...]}]}`. Optional. The name `none` is reserved. |
| `smart_tokens`, `default_smart_tokens` | [Smart tokens](#smart-tokens) presets, `{"name": {"notes": ..., "tokens": [...]}}`, and the one used without `-smart_tokens` (`none` = none). Optional. |
| `name_tokens` | [Custom name tokens](#custom-name-tokens): `{"name": "regex"}` or `{"name": {"regex": ..., "source": ..., "default": ...}}`. Optional. |
| `channel_preferences` | Ordered channel sets for the [automatic channel choice](#exr-conversion-oiiotool). Each set is a string `"R,G,B"` or a list `["R", "G", "B"]`. Put your studio's beauty-layer names first. Optional; if it's missing, the built-in list shown there is used. |

Encode preset fields:

| Field | Required | Description |
|---|---|---|
| `ext` | yes | Output extension. |
| `encode` | yes | ffmpeg arguments between input and output. Either one string (quotes group words) or a list of arguments. Must contain `{videofilter}`. Don't put timecode or reel name here; the tool adds those. A fixed `-metadata` that belongs to the codec (like ProRes `vendor_id`) is fine. |
| `notes` | no | Description shown in `--help`. |
| `ten_bit` | no | `true` for 10-bit codecs. It only enables the `-mezzanine_16bit` tip. |
| `metadata` | no | Tags this codec always gets, as `{"key": "value"}`, e.g. `{"vendor_id": "appl"}` for ProRes. Keywords work. The metadata preset's tags and `-metadata` can override them. |
| `alpha_pix_fmt` | no | Pixel format used instead of the `-pix_fmt` in `encode` when [alpha](#alpha) is kept, e.g. `"yuva444p10le"` for ProRes 4444. Without it the preset cannot store alpha. |
| `colour_tags` | no, default `true` | Append `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc …`. The transfer tag follows `-colorout`: `iec61966-2-1` when it contains "srgb", `bt709` otherwise. |

Metadata preset fields (all optional; any other field is an error; each has the command-line option of the same name):

| Field | Description |
|---|---|
| `tags` | `{"key": "value"}` tags written to the movie; values may use the `-metadata` keywords, including `{meta:ATTR}`. |
| `copy` | EXR attributes to copy, like `-metadata_copy`. |
| `reelid_source`, `reelid`, `reelid_attr`, `reelid_pattern` | Reel name settings, like the options of the same name. |
| `timecode_source` | `counter`, `arg` or `metadata` (old name `exr`), like `-timecode_source`. |
| `notes` | Description shown in `--help`. |

Burn-in preset fields (all optional; any other field is an error):

| Field | Description |
|---|---|
| `tl`, `tc`, `tr`, `bl`, `bc`, `br`, `wm` | Each place is either its text (with the [keywords](#burn-ins)), or an object that also styles it: `{"text": "TC {tc}", "color": "yellow", "opacity": 0.8, "size": "2.2%", "box": true}`. An object without `text` only styles the place, for text that comes from the command line. Leave a place out to keep it empty. |
| `color` | Text colour for the whole preset. |
| `opacity` | 0–1 for text, shadow and box, for the whole preset. |
| `size` | [Text size](#text-size) for the whole preset: `"1.5%"`, `40` (pixels) or `"h/75"`. Default `"1.33%"`. |
| `box` | `true` for a black box behind the text, for the whole preset. |
| `margin`, `margin_top`, `margin_bottom`, `margin_left`, `margin_right` | Space between the picture edge and the text: pixels (`20`) or percent (`"2%"`, of the width for left/right, of the height for top/bottom). `margin` sets all four; a side wins over it. |
| `font` | Font file; `./` paths are relative to the application folder. |
| `notes` | Description shown in `--help`. |

Slate preset fields (all optional; any other field is an error):

| Field | Description |
|---|---|
| `title`, `subtitle` | Text with the [slate keywords](#slate). Title default `{clean_name}`. |
| `fields` | Rows in order, `{"Label": "value", ...}`; values use the slate keywords. |
| `hide_empty` | `true` (default): rows whose value comes out empty are left out. |
| `thumbnail` | `middle` (default), `first`, `last`, `none` or a frame number. |
| `title_size`, `subtitle_size`, `size` | Text sizes of the title (`"5%"`), subtitle (`"3.2%"`) and rows (`"2.5%"`): percent of the picture height, or pixels. |
| `scale` | Whole layout, `"100%"` default; `"50%"` is half size, centred. |
| `valign` | `center` (default) or `top`. |
| `background` | Canvas colour, default `#1b1b1b`. |
| `title_color`, `subtitle_color` | Default `white` and `#b4b4b4`. |
| `value_color` | Row values, default `white`. |
| `label_color` | Row labels (field names), default `#7a7a7a`. |
| `rule` | `true` draws a line under the title, default `false`. |
| `line_color` | Colour of that line, default `#5a5a5a`. |
| `color` | Shortcut: sets `title_color` and `value_color`. |
| `font` | Font file; default the burn-in font. |
| `logo` | Vendor logo image (PNG with alpha). `./` and `../` are relative to the presets file's folder (so a studio presets file on a share keeps its logo beside it), else the application folder. |
| `logo_position`, `logo_size`, `logo_x`, `logo_y` | As `-slate_logo_position`, `-slate_logo_size`, `-slate_logo_x`, `-slate_logo_y`. |
| `notes` | Description shown in `--help`. |

Thumbnail preset fields (all optional; any other field is an error):

| Field | Description |
|---|---|
| `frame` | `middle` (default), `first`, `last`, `largest` or a frame number. |
| `width`, `height` | Size in pixels; `0` follows the other side, both `0` = the movie's size. |
| `format` | `jpg` (default) or `png`. |
| `quality` | jpg quality 1–100, default 90. |
| `compression` | png compression 0 (fastest, biggest) – 9 (slowest, smallest), default 6. |
| `bits` | png bit depth 8 (default) or 16. jpg is always 8-bit. |
| `notes` | Description shown in `--help`. |

Filmstrip preset fields (all optional; any other field is an error):

| Field | Description |
|---|---|
| `tiles` | Number of tiles (frames), 1–1000. Default: `columns` × `rows` when both are set, else 24. |
| `tile_width`, `tile_height` | Size of one tile, default 480 × 270. |
| `columns`, `rows` | The grid. Both `0` (default): one row. One of them `0`: as many as the tiles need. Both set: must have at least `tiles` cells. |
| `quality` | jpg quality 1–100, default 90. |
| `source` | `source` (default), `intermediates` or `movie`: where the tiles come from. |
| `slate` | `true`: the slate is the first tile (when the run has one). Default `false`. |
| `notes` | Description shown in `--help`. |

Shipped encode presets, based on the [ASWF Encoding Guidelines](https://github.com/AcademySoftwareFoundation/EncodingGuidelines):

| Preset | Ext | Notes |
|---|---|---|
| `h264` (default) | mp4 | 8-bit, all-intra (frame-accurate scrubbing), CRF 18. The original default. |
| `h264-web` | mp4 | 8-bit, long GOP, small files. |
| `h265` | mp4 | 10-bit HEVC, CRF 22, `hvc1` tag for Apple players. Not playable in Firefox. |
| `prores422hq` | mov | ProRes 422 HQ, 10-bit. |
| `prores4444` | mov | ProRes 4444, 10-bit. Keeps alpha with `-alpha keep` (`yuva444p10le`). |

The ProRes presets add `-vendor apl0`, which marks the ProRes frames themselves as Apple-made, and `"metadata": {"vendor_id": "appl"}`. With ffmpeg 6.1 the latter only ends up as a file-level `vendor_id` tag. The vendor field of the mov video track stays `FFMP`, because ffmpeg's mov writer hard-codes it ([ffmpeg-user, 2021](https://ffmpeg.org/pipermail/ffmpeg-user/2021-May/053003.html)). Test 14 in `test_conversion.bat` reports what your ffmpeg writes. Custom keys like this are kept in mov/mp4 because the tool adds `-movflags +use_metadata_tags` whenever it writes tags.
| `dnxhr-sq` | mov | DNxHR SQ, 8-bit 4:2:2. |
| `dnxhr-hqx` | mov | DNxHR HQX, 10-bit 4:2:2. |

Shipped burn-in presets:

| Preset | Top | Bottom | Style |
|---|---|---|---|
| `frame` | | right: `{current_frame}` | |
| `review` | left: `{clean_name}`, right: `TC {tc}` | left: `{frame_start}-{frame_end} @ {fps} fps`, right: `{current_frame}` | |
| `review-boxed` | as `review`, plus centre: `{date}` | as `review` | box, opacity 0.75 |
| `dailies` | left: `{clean_name}`, right: `{date}` | centre: `TC {tc}` (big, boxed), right: `{current_frame} / {frame_end}` | |
| `editorial` | | left: `{clean_name}`, right: `TC {tc}` | |
| `client` | | centre: `{clean_name}   {date}` | small (1.1%), opacity 0.8 |
| `confidential` | | centre: `{clean_name}   {date}`; watermark `CONFIDENTIAL` | as `client`; watermark 8%, opacity 0.25 |
| `wip` | left: `{clean_name}`, centre: `WIP - NOT FOR APPROVAL` (yellow, big, boxed), right: `TC {tc}` | right: `{current_frame}` | |

## Logging and exit codes

| Option | Default | Description |
|---|---|---|
| `-v`, `--verbose [N]` | `0` | `1` (or plain `-v`): also print the configuration and every oiiotool/ffmpeg command. `2`: also print the tools' own output. |
| `--version` | | Print the version. |
| `-h`, `--help` | | Print all options and the available presets. |

When a tool fails, its last 25 output lines are printed regardless of `-v`.

| Exit code | Meaning |
|---|---|
| `0` | Done. |
| `1` | Error; nothing usable was written. |
| `2` | Movie written, but some frames failed to convert and were replaced by neighbours. |
| `130` | Interrupted (Ctrl+C). |

## Examples

Default h264 review movie with the `review` burn-ins:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -burn_preset review
```

ProRes for editorial, 25 fps, timecode from the EXR header, 16-bit intermediates, editorial burn-ins:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr ^
  -preset prores422hq -r 25 -timecode_source metadata -mezzanine_16bit 1 -burn_preset editorial
```

Frame rate and timecode both taken from the renders, with 25 fps for files without a rate:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr ^
  -fps_source exr -r 25 -timecode_source metadata
```

Your own burn-ins without a preset:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr ^
  -burn_tl "{clean_name}" -burn_tr "TC {tc}" -burn_br "{current_frame} / {frame_end}"
```

A utility pass, choosing the channels by hand, with a fixed timecode and a title:

```
python vfx-transcode.py -i D:/renders/sh010_utils.1001.exr -oiio_channel N.x,N.y,N.z ^
  -timecode 10:00:00:00 -metadata "title={clean_name} normals"
```

ProRes for conforming, with the reel name from the EXR `owner` attribute and the timecode from the header:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr ^
  -preset prores422hq -timecode_source metadata -reelid_source metadata
```

The same with an explicit reel name built from the shot name:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -preset prores422hq -reelid "{clean_name}"
```

Review movie with a slate: note on the slate, burn-ins only on the pictures:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -slate generic ^
  -slate_field "Notes=first comp pass, check the helmet edge" -burn_preset review
```

Editorial ProRes with a slate carrying the reel name and source timecode:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -preset prores422hq ^
  -metadata_preset conform -slate editorial
```

Review with a see-through 2.39 letterbox and a thin grey edge, burn-ins on top:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -burn_preset review ^
  -letterbox 1 -letterbox_ratio 2.39 -letterbox_color 0,0,0,160 -letterbox_line 2 -letterbox_line_color 128,128,128,255
```

Anamorphic 2.0 plates (the header wrongly says 1), made square for review:

```
python vfx-transcode.py -i D:/plates/sh010_plate.1001.exr -par 2 -par_out square -burn_preset review
```

ACES 1.3 display/view instead of an output colourspace:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -ocio ocio://studio-config-latest ^
  -display "Rec.1886 Rec.709 - Display" -view "ACES 1.0 - SDR Video" -preset prores422hq
```

sRGB output for the web, 1280 wide, copying the artist and render time:

```
python vfx-transcode.py -i D:/renders/sh010_beauty.1001.exr -preset h264-web -co out_srgb ^
  -width 1280 -metadata_copy "Artist,renderTime"
```

Deep EXR as a depth picture with a fixed range:

```
python vfx-transcode.py -i D:/renders/sh010_deep.1001.exr -deep_zrange 30,2000
```

`^` continues a command on the next line in Windows `cmd`; in PowerShell use a backtick `` ` ``.

`test_conversion.bat` in the repository root runs the unit tests and a set of conversions on the sample media in `media\` (not in git). It writes the movies, one log per test and an ffmpeg dump of each movie (`*_probe.txt`) to `media\_test_output`. Tests 8–13 cover frame rate, timecode and reel name, test 14 the ProRes vendor, 15–18 the metadata presets, 19 the burn-in places and styles, 20 `{meta:ATTR}` in burn-ins and metadata, 21 `-ocio_info`, 22 display/view with the ACES 1.3 studio config, 23–25 pixel aspect ratio, and 26 multi-line burn-ins. They also check that the expected lines are in the log (e.g. `Frame rate: 25 (-r)`) and that the expected values are in the movie. A missing line is reported as `[FAIL]` in `summary.txt`.
