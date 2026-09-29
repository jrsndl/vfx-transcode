# vfx-transcode — options reference

- [How a run works](#how-a-run-works)
- [Conventions](#conventions)
- [Input and output](#input-and-output)
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
- [Encoding (ffmpeg)](#encoding-ffmpeg)
- [Presets file](#presets-file)
- [Logging and exit codes](#logging-and-exit-codes)
- [Examples](#examples)

## How a run works

1. `-i` names any one file of a sequence. The tool finds the other frames next to it and reports any holes.
2. **EXR input.** oiiotool reads the header of the sequence's first frame, whichever frame `-i` points at. The channels, frame rate, timecode, reel name and `{meta:…}` values all come from that header. It then converts every frame to an intermediate file (PNG by default) in a temporary folder, by default in your user temp folder (see `-temp_location`). The conversion applies the OCIO colour transform and resizes the image.
   **Other input** (PNG, JPG, DPX, TIFF, …) skips this step. ffmpeg reads those frames directly.
3. ffmpeg encodes the frames with the chosen preset and adds burn-ins, timecode and metadata.
4. The intermediate files are deleted unless `-temp_keep 1` is set.

## Conventions

- Options start with a single dash (`-width 1280`), as in the original script, so existing .bat files keep working.
- **Switches** (shown as `0|1` below) accept `1/0`, `true/false`, `yes/no` and `on/off`. A switch given on its own means on: `-temp_keep` is the same as `-temp_keep 1`.
- **Paths starting with `./` or `../`** are relative to the application folder (the repository root, or the folder of the built .exe), not to the folder you run the command from. Any other path is used as given.
- Forward slashes work everywhere, including on Windows.
- Quote any value that contains spaces: `-burn_tl "Shot 010"`.

## Input and output

| Option | Default | Description |
|---|---|---|
| `-i PATH` | *(required)* | Any file of the input sequence. See the sequence rules below. |
| `-o PATH` | `<input folder>/<name>.<ext>` | Output movie. `<name>` is the sequence name without its frame number and separator (`shot_010.1001.exr` → `shot_010`). `<ext>` comes from the preset or `-ff_out_ext`. Missing folders are created. |

**How the sequence is found.** The frame number is the run of digits at the end of the file name, just before the extension:
- `shot_010.1001.exr` → name `shot_010.`, frame 1001, 4-digit padding.
- `plate_0001.png` → name `plate_`, frame 1, 4-digit padding.

Every file in the same folder with the same name, the same extension (case-insensitive) and the same padding belongs to the sequence. Numbers longer than the padding also count, so frame 10000 belongs to a 4-digit sequence.

A file without a frame number (`plate.exr`) is treated as a one-frame sequence at frame 0.

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

EXRs are converted with OCIO in one of two ways.

**Colourspace to colourspace** (the default): `oiiotool --colorconvert IN OUT`.

| Option | Default | Description |
|---|---|---|
| `-ci`, `--colorin NAME` | `ACEScg` | Colourspace of the EXRs, as named in the OCIO config, e.g. `ACEScg` or `"Utility - Linear - sRGB"`. Used in both modes. |
| `-co`, `--colorout NAME` | `out_rec709` | Output colourspace, e.g. `out_rec709` or `out_srgb` (ACES 1.2 config names). It also decides the colour tags written to the movie; see [Presets file](#presets-file). |

**Display / view**: `oiiotool --ociodisplay`, the way ACES 1.3 and newer configs are meant to be used. It's switched on by `-display` and/or `-view`, and then `-colorout` is ignored.

| Option | Default | Description |
|---|---|---|
| `-display NAME` | *(not used)* | OCIO display, e.g. `"sRGB - Display"`, `"Rec.1886 Rec.709 - Display"`. `""` means the config's default display. |
| `-view NAME` | the display's default | OCIO view, e.g. `"ACES 1.0 - SDR Video"`, `"Un-tone-mapped"`. |
| `-looks LOOKS` | *(none)* | OCIO looks applied with the display transform, e.g. `"ACES 1.3 Reference Gamut Compression"`; separate several with commas. |

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

| Option | Default | Description |
|---|---|---|
| `-oiio PATH` | `./oiio/oiiotool.exe` | oiiotool executable. |
| `-ffmpeg PATH` | `./ffmpeg/ffmpeg.exe` | ffmpeg executable. |

If the configured path doesn't exist, the tool looks for `oiiotool` / `ffmpeg` on the `PATH`. If it isn't there either, the run stops with an error that names both places it looked.

## EXR conversion (oiiotool)

| Option | Default | Description |
|---|---|---|
| `-oiio_channel LIST` | *(automatic)* | Channels to show, comma separated: `R,G,B` or `diffuse.R,diffuse.G,diffuse.B`. If they aren't in the image, a warning is printed and the tool tries them anyway. |
| `-oiio_channel_use_first 0\|1` | `0` | Skip the automatic search (`channel_preferences`, below) and use the first layer. |
| `-width PIXELS` | `1920` | Output width. The height follows the aspect ratio and is rounded to an even number. `0` keeps the original size. EXRs are resized by oiiotool; other input is resized by ffmpeg. |
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
| `-timecode_source counter\|arg\|exr` | the metadata preset's, else `counter` | Where the start timecode comes from. See below. |
| `-timecode HH:MM:SS:FF` | `01:00:00:00` | Start timecode for `arg`. Giving `-timecode` selects `arg` automatically. Use `;` before the frames for drop-frame (`00:59:59;29`). |
| `-output_timecode 0\|1` | `1` | Write the timecode into the movie: a real timecode track in mov, mp4 and mxf, which editing apps read, or a `timecode` tag in other containers. |
| `-timecode_from_counter 0\|1` | — | Old switch, kept for existing scripts: `1` = `counter`, `0` = `arg`. |

The three sources:

- **`counter`**: the first frame number at the [frame rate](#frame-rate). Frame 1001 at 24 fps is `00:00:41:17`; at 25 fps it is `00:00:40:01`.
- **`arg`**: the value of `-timecode`.
- **`exr`**: read from the first frame's EXR header: `smpte:TimeCode` (OpenEXR `timeCode`), or any attribute whose name ends in "timecode". If the header has no timecode, a warning is printed and `counter` is used. EXR input only.

The log shows the timecode and its source, e.g. `Timecode: 00:00:41:17 (frame counter)`. The same value starts the `{tc}` burn-in.

## Metadata

| Option | Default | Description |
|---|---|---|
| `-metadata_preset NAME` | the presets file's `default_metadata` (`none`) | A [metadata preset](#metadata-presets): tags, copy rules, reel name and timecode settings in one name. `none` means no preset. |
| `-metadata KEY=VALUE` | *(none)* | Write a tag. Repeatable. The value may use `{clean_name}`, `{frame_start}`, `{frame_end}`, `{timecode}`, `{fps}`, `{preset}`, `{date}` and [`{meta:ATTR}`](#header-values-metaattr). `KEY=` with no value removes a tag that a preset would write. |
| `-metadata_copy SPEC` | the metadata preset's `copy`, else none | Copy attributes from the first frame's EXR header. See below. `-metadata_copy ""` switches off a preset's copying. |

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

### Metadata presets

A metadata preset bundles the metadata settings under one name in `presets.json`: tags, copy rules, the reel name settings and the timecode source. Select one with `-metadata_preset NAME`. Every command-line option overrides the matching preset field on its own:

- `-metadata KEY=VALUE` adds or replaces one tag; `-metadata KEY=` removes one.
- `-metadata_copy`, `-reelid_source`, `-reelid`, `-reelid_attr`, `-reelid_pattern` and `-timecode_source` each replace the preset's value.
- Giving `-timecode` also beats the preset's `timecode_source`.

The log names the preset in its metadata line, e.g. `Metadata: title, comment (preset review)`. `--help` lists the presets with their settings.

| Preset | Tags | Other settings | Use |
|---|---|---|---|
| `review` | `title={clean_name}`, `comment={clean_name} {frame_start}-{frame_end} @ {fps} fps, start TC {timecode}` | reel name `auto` | Internal review. |
| `conform` | `title={clean_name}` | `timecode_source exr`, `reelid_source exr`, `reelid_attr owner` | Editorial / conform: timecode and reel name from the renders. |
| `client` | `title={clean_name}` | nothing copied, `reelid_source off` | Client delivery with nothing from the render headers. |
| `archive` | `title={clean_name}`, `comment=transcoded {date}, preset {preset}` | `copy all,-husk:*,-nuke/*,-MEDIA_PATH`, reel name `auto` | Keep header information, minus command lines and internal paths. |

## Reel name

The reel name (reel ID) ties a clip back to its source in an EDL or AAF when conforming.

| Option | Default | Description |
|---|---|---|
| `-reelid_source auto\|arg\|exr\|off` | the metadata preset's, else `auto` | Where the reel name comes from; see below. |
| `-reelid TEXT` | the metadata preset's, else none | Explicit reel name. It may use the `-metadata` keywords, e.g. `-reelid "{clean_name}"`. |
| `-reelid_attr NAME` | the metadata preset's, else `owner` | EXR header attribute holding the reel name. Any attribute works, e.g. `-reelid_attr pipe/reel`. The standard OpenEXR names are also found under oiiotool's names, so `owner` also finds `Copyright`. Matching ignores case. |

| `-reelid_source` | Order | Result |
|---|---|---|
| `auto` (default) | `-reelid` → EXR attribute → none | Written when either exists; otherwise no reel name, silently. |
| `arg` | `-reelid` → none | Only the explicit value. Warns if `-reelid` is missing. |
| `exr` | EXR attribute → `-reelid` → none | The header wins. Warns if neither exists. |
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

Burn-ins can go in six places: `tl` `tc` `tr` along the top and `bl` `bc` `br` along the bottom. `tc` and `bc` are centred.

| Option | Default | Description |
|---|---|---|
| `-burn_preset NAME` | the presets file's `default_burnin` (`none`) | Burn-in preset from the [presets file](#presets-file). `none` means no burn-ins. `--help` lists the presets, their text and styles. |
| `-burn_tl`, `-burn_tc`, `-burn_tr`, `-burn_bl`, `-burn_bc`, `-burn_br TEXT` | from the burn-in preset | Text for one place. An empty value (`-burn_tl ""`) clears the preset's text there. |
| `-burn_color COLOR` | preset, else `white` | Text colour for all places: a name (`white`, `yellow`, `red`, …) or `#RRGGBB` / `0xRRGGBB`. |
| `-burn_opacity 0-1` | preset, else `1` | Opacity of text, shadow and box, for all places. `1` is solid, `0.5` half transparent. |
| `-burn_size SIZE` | preset, else `1.33%` | Text size for all places; see [Text size](#text-size). |
| `-burn_box 0\|1` | preset, else `0` | A black box behind the text instead of a drop shadow, for all places. The box has the place's opacity. |
| `-burn_style PLACE:KEY=VALUE,...` | *(none)* | Style for one place, e.g. `-burn_style "tc:color=yellow,size=2.5%,box=1"`. PLACE is `tl` … `br` or `all`; keys are `color`, `opacity`, `size` and `box`. Repeatable; later ones win. |
| `-burn_font PATH` | the burn-in preset's `font`, else `./fonts/DroidSansMono.ttf` | Font file. If it's missing, a warning is printed and ffmpeg's default font is used. |

**Style layers.** Each place gets its colour, opacity, size and box from these layers, field by field; later ones win:

1. defaults: white, opacity 1, size 1.33%, no box
2. the preset's `color` / `opacity` / `size` / `box`
3. the preset's settings for that place
4. `-burn_color` / `-burn_opacity` / `-burn_size` / `-burn_box`
5. `-burn_style` for that place

For example, `-burn_preset wip -burn_style "tc:color=red"` keeps the WIP stamp's size and box but makes it red. The log lists every place with its text and final style.

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

**New lines.** `\n` in a text starts a new line, e.g. `-burn_bc "{clean_name}\nTC {tc}"` or in `presets.json` `"tl": "{clean_name}\n{date}"`. A real line break in the JSON string (`"\n"` as JSON writes it) works too.

- Lines are aligned to match the place: left in `tl` / `bl`, centred in `tc` / `bc`, right in `tr` / `br`.
- Blocks at the bottom grow upwards and keep the same bottom margin as one line.
- A box covers the whole block.
- `{tc}` and `{current_frame}` work on any line; text after `{tc}` is still dropped.
- Centred and right-aligned multi-line text needs ffmpeg 6.1 or newer (drawtext's `text_align` option). Left-aligned and single-line text works with any version.

Any other characters are allowed, including `: ' % , [ ] ;`; the tool escapes them for ffmpeg. This includes characters inside header values. An unknown `{word}` is printed as typed, with a warning.

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

## Encoding (ffmpeg)

| Option | Default | Description |
|---|---|---|
| `-preset NAME` | the presets file's `default_encode` (`h264`) | Encode preset. `--help` lists the available ones with their notes. |
| `-presets_file PATH` | `./presets.json` | JSON file holding the encode presets, burn-in presets and channel preferences. See [Presets file](#presets-file). |
| `-ff_out_ext EXT` | the preset's `ext` | Output extension/container, e.g. `mov`, `mp4`, `mkv`, `mxf`. |
| `-ff_encode ARGS` | *(none)* | Replaces the preset's codec arguments for one run. It must contain `{videofilter}` as a separate word where the `-vf` filter goes. No colour tags are added. Example: `-ff_encode "-pix_fmt yuv420p {videofilter} -c:v libx264 -crf 23"`. |
| `-ff_filter FILTERS` | *(none)* | Extra ffmpeg video filters, inserted after scale/pad and before the burn-ins, e.g. `-ff_filter "eq=gamma=1.1"`. Used as given, so escape ffmpeg special characters yourself. |
| `-skipffmpeg 0\|1` | `0` | Only convert the EXRs. The movie isn't made and the intermediates are kept (implies `-temp_keep 1`); the log prints their folder. Add `-temp_location source` to get them next to the source files. |

Every encode also sets the colour matrix explicitly (`scale=in_range=full:in_color_matrix=bt709:out_range=tv:out_color_matrix=bt709`) and pads odd sizes to even ones.

## Presets file

`presets.json` holds the encode, burn-in and metadata presets and the channel preferences. It sits next to the application; for a built .exe, copy it next to the .exe. Edit, add or remove presets freely. If the file has a mistake, the tool reports the file and line, or the preset and field.

```json
{
  "_notes": ["free text, ignored"],
  "default_encode": "h264",
  "default_burnin": "none",
  "default_metadata": "none",
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
      "timecode_source": "exr",
      "reelid_source": "exr",
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
  }
}
```

| Top-level key | Description |
|---|---|
| `default_encode` | Encode preset used without `-preset`. Defaults to the first one. |
| `default_burnin` | Burn-in preset used without `-burn_preset`. `none` (or leaving it out) means no burn-ins. |
| `encode_presets` | The encode presets. The older key name `presets` (with `default`) is still accepted. |
| `burnin_presets` | The burn-in presets. Optional. The name `none` is reserved. |
| `default_metadata` | Metadata preset used without `-metadata_preset`. `none` (or leaving it out) means none. |
| `metadata_presets` | The metadata presets. Optional. The name `none` is reserved. |
| `channel_preferences` | Ordered channel sets for the [automatic channel choice](#exr-conversion-oiiotool). Each set is a string `"R,G,B"` or a list `["R", "G", "B"]`. Put your studio's beauty-layer names first. Optional; if it's missing, the built-in list shown there is used. |

Encode preset fields:

| Field | Required | Description |
|---|---|---|
| `ext` | yes | Output extension. |
| `encode` | yes | ffmpeg arguments between input and output. Either one string (quotes group words) or a list of arguments. Must contain `{videofilter}`. Don't put timecode or reel name here; the tool adds those. A fixed `-metadata` that belongs to the codec (like ProRes `vendor_id`) is fine. |
| `notes` | no | Description shown in `--help`. |
| `ten_bit` | no | `true` for 10-bit codecs. It only enables the `-mezzanine_16bit` tip. |
| `metadata` | no | Tags this codec always gets, as `{"key": "value"}`, e.g. `{"vendor_id": "appl"}` for ProRes. Keywords work. The metadata preset's tags and `-metadata` can override them. |
| `colour_tags` | no, default `true` | Append `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc …`. The transfer tag follows `-colorout`: `iec61966-2-1` when it contains "srgb", `bt709` otherwise. |

Metadata preset fields (all optional; any other field is an error; each has the command-line option of the same name):

| Field | Description |
|---|---|
| `tags` | `{"key": "value"}` tags written to the movie; values may use the `-metadata` keywords, including `{meta:ATTR}`. |
| `copy` | EXR attributes to copy, like `-metadata_copy`. |
| `reelid_source`, `reelid`, `reelid_attr`, `reelid_pattern` | Reel name settings, like the options of the same name. |
| `timecode_source` | `counter`, `arg` or `exr`, like `-timecode_source`. |
| `notes` | Description shown in `--help`. |

Burn-in preset fields (all optional; any other field is an error):

| Field | Description |
|---|---|
| `tl`, `tc`, `tr`, `bl`, `bc`, `br` | Each place is either its text (with the [keywords](#burn-ins)), or an object that also styles it: `{"text": "TC {tc}", "color": "yellow", "opacity": 0.8, "size": "2.2%", "box": true}`. An object without `text` only styles the place, for text that comes from the command line. Leave a place out to keep it empty. |
| `color` | Text colour for the whole preset. |
| `opacity` | 0–1 for text, shadow and box, for the whole preset. |
| `size` | [Text size](#text-size) for the whole preset: `"1.5%"`, `40` (pixels) or `"h/75"`. Default `"1.33%"`. |
| `box` | `true` for a black box behind the text, for the whole preset. |
| `font` | Font file; `./` paths are relative to the application folder. |
| `notes` | Description shown in `--help`. |

Shipped encode presets, based on the [ASWF Encoding Guidelines](https://github.com/AcademySoftwareFoundation/EncodingGuidelines):

| Preset | Ext | Notes |
|---|---|---|
| `h264` (default) | mp4 | 8-bit, all-intra (frame-accurate scrubbing), CRF 18. The original default. |
| `h264-web` | mp4 | 8-bit, long GOP, small files. |
| `h265` | mp4 | 10-bit HEVC, CRF 22, `hvc1` tag for Apple players. Not playable in Firefox. |
| `prores422hq` | mov | ProRes 422 HQ, 10-bit. |
| `prores4444` | mov | ProRes 4444, 10-bit. |

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
  -preset prores422hq -r 25 -timecode_source exr -mezzanine_16bit 1 -burn_preset editorial
```

Frame rate and timecode both taken from the renders, with 25 fps for files without a rate:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr ^
  -fps_source exr -r 25 -timecode_source exr
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
  -preset prores422hq -timecode_source exr -reelid_source exr
```

The same with an explicit reel name built from the shot name:

```
python vfx-transcode.py -i D:/shots/sh010/comp/sh010_comp.1001.exr -preset prores422hq -reelid "{clean_name}"
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
