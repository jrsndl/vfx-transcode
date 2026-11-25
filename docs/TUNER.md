# vfx-transcode tuner

A small PySide6 app for tuning presets by eye, and for converting. It edits the transform,
burn-in, slate, filmstrip, letterbox and metadata settings, runs vfx-transcode on one frame of
your input (a sequence or a movie file), and shows the result exactly as the movie would show
it. Drag the timeline under the picture to look at any frame. When the settings look right,
save them as a **super-preset**, press **Process** to make the movie, or copy the command-line
options from the **Presets** tab (the full command), or save presets with the **Save as preset...** buttons. The presets file is the one vfx-transcode would use: the tuner's own `-presets_file` if it was started with one, else the studio preferences named in `vfx-transcode.ini`, else `presets.json` next to the application (see [OPTIONS.md](OPTIONS.md#which-presets-file-is-used)). The Presets tab shows which file that is and why; if the studio preferences could not be used, the status bar says why.

```
uv sync --extra tuner
uv run vfx-transcode-tuner D:/shots/sh010/comp/sh010_comp_v003.1001.exr [D:/ref/sh010_ref.png]
```

Without uv: `pip install PySide6`, then `python vfx-transcode-tuner.py ...`.

- **input1**: the input: any frame of a sequence, or a movie file (`.mov`, `.mp4`, `.mxf` …). Optional; it can also be chosen on the Input tab, or dropped on the window.

**Drag and drop**: drop a file or a folder anywhere on the window (the picture, a tab, a text field) to make it the input.
- **A file** is used when it's an image (EXR, PNG, JPG, TIF, DPX, BMP, TGA, HDR) or a movie; anything else is refused, with a note in the toolbar. A frame of a sequence brings in the whole sequence, as when picked with **...**.
- **A folder** is searched (not its sub-folders). If it holds exactly one movie, one image sequence or one single image, that is used. Otherwise (none, or several), the folder goes into the Input field, the line under it says what was found, and **...** opens in that folder so you can pick the file.
- **input2**: something to compare to: an image, a movie or a sequence. Optional; given, the tuner compares to it instead of the source.

## How it works

Every refresh runs `vfx-transcode ... -preview FILE.png` (see [OPTIONS.md](OPTIONS.md#preview)):

1. vfx-transcode converts one frame with oiiotool, with the same channels, transform, colour or CLF, and alpha as the movie.
2. It runs the movie's ffmpeg chain on it: scale, pixel format, letterbox and burn-ins.
3. It writes the frame as PNG, the slate as a second PNG, and a JSON file with the input header and the output metadata.

A new input starts on its middle frame; click the timeline to preview another one. For a movie, only that one frame is unpacked (see [OPTIONS.md](OPTIONS.md#movie-input)). The converted frame is cached, so changes to burn-ins, slate, letterbox or metadata refresh in about a second, without running oiiotool again.

The **Filmstrip** view runs `vfx-transcode ... -of FILE.jpg` instead (see [OPTIONS.md](OPTIONS.md#filmstrip)) with the Filmstrip tab's settings. With tiles from the `source` frames only the filmstrip is made, which is quick. With tiles from the `intermediates` or the `movie`, or with the slate as the first tile, the whole movie is made as well (in the tuner's temp folder), with the burn-ins, slate and letterbox, so that refresh takes as long as a real run.

The command the tuner runs is shown in the log under the viewer. By default it is this Python with `vfx-transcode.py` from the same folder; the **vfx-transcode command** field on the Presets tab can point to a built `vfx-transcode.exe` instead.

## Viewer

- **Pan**: drag. **Zoom**: mouse wheel (under the cursor). **Fit** and **1:1** in the toolbar.
- **View**: **Image** (the previewed frame), **Slate** (the slate frame; switch the slate on in the Slate tab) or **Filmstrip**. Image and Slate come from the same preview run, so switching between them is instant. Switching to a view whose settings changed since its last run refreshes it (with **Refresh after edit** on). The view is fitted to the window when it changes.
- **Show overlay**: a small label over the top right of the picture with the input and output size, e.g. `input: 3840x2160 pa 1.3 @ 24fps` / `output: 1920x1080 pa 1.0`. The pixel aspect is shown only when the input's or the output's is not 1. When the output plays at another frame rate (Output tab, **Frame rate**), the output line shows it, e.g. `output: 1920x1080 @ 25fps (4.2% faster)`; the input line always shows the input's rate. When the output gets a timecode (a movie, or an EXR / DPX sequence, with Output tab **Write timecode** on), a third line shows the previewed frame's timecode in the output and where it starts, e.g. `timecode: 01:00:00:12 (starts 01:00:00:00)`. It is drawn by the tuner, not part of the image.
- **Alpha** (shown when the input has an alpha channel): **RGB** shows the colour without alpha, **Alpha** shows the alpha channel as grey (white = opaque), **Checkerboard** shows the picture over a grey checkerboard. **Premultiply** (off by default) multiplies the colour by alpha for the display, in RGB and Checkerboard; off, the colour is shown as stored. The preview always carries the input's alpha (`-preview_alpha 1`), also when the output drops it; then a note says *view only* (set Output tab, **Alpha: auto** with an output that can store alpha to keep it). These change the view only, not the output.
- **Compare mix** (Image and Slate views): 0 shows the result and 100 the compare frame. Values in between dissolve. The compare frame is scaled to the result's size. By default it is the **source**: the input's own frame at the previewed frame, unmodified (no transform, resize, burn-ins or slate; EXR gets the Color tab's colour conversion so it can be looked at). See **Compare** on the Input tab.
- Anamorphic output (pixel aspect other than 1) is shown un-squeezed.
- **Trim**: **In** and **Out** on the timeline (the **I** and **O** keys, or the buttons; the keys don't fire while you type in a field) mark the frames to use (`-in` / `-out`). The kept range is shaded, In is green, Out is red, and the frame count is shown next to the buttons. Setting In after Out (or Out before In) clears the other one; **Clear** removes the trim. The preview can still show any frame; its burn-ins show the trimmed range.
- **Timeline** under the picture: the input's frames, first to last (a movie: from **Movie: first frame number**). Drag the handle and let go to preview that frame; click beside the handle or use the arrow / Page keys to step, and the preview follows after a short pause; or type a frame number. It shows the frame's timecode: from the movie's timecode, or, after the first preview, the timecode vfx-transcode uses (the frame counter by default). Frames missing from a sequence show the nearest frame. In the Filmstrip view the timeline sets the frame for the Image and Slate views.
- Under the timeline, two tabs: **Super-presets** (below) and **Log**, which shows each vfx-transcode run; **Clear** empties it. The window uses a dark theme.
- **Refresh** runs the preview of the view on screen; **Refresh after edit** runs it half a second after each change. A change made while a preview is running restarts it.

## Super-presets and Process

The **Super-presets** tab under the timeline saves the current settings of several areas under one name, and loads them back. See [OPTIONS.md](OPTIONS.md#super-presets) for the file and for using super-presets on the command line (`-super_preset NAME`).

- **The list** shows the super-presets in `super_presets.json` next to the presets file (the path is shown under the buttons): the studio one when `vfx-transcode.ini` names studio preferences, plus any local ones next to the application, in italic (hover for the file). Under the list, a panel shows the selected one's notes, the input type it is for, its areas, its destination and its file; **Load**, **Duplicate** and **Delete** are under it. Drag the divider beside the list to make it wider, and the one under it to give the notes more room. Selecting one fills in its name and notes on the right, where **Save** is; the Notes field grows with the panel.
- **A super-preset holds everything**: Input (frame rate, pixel aspect, a movie's first frame number; `auto` writes nothing, so the input's own values apply), Color (`-ci`, `-co`, display, view, looks, CLF, OCIO config, Convert colour; written only where the colour is used), Tokens (the smart token rows as `-smart_tokens_def`, the version regex and the name tokens), Encode (the output type, the encode and sequence presets, alpha, output pixel aspect, missing frames, the output frame rate), Intermediate (format, bit depth, location), Multichannel (part and channels), Transform Sequence (oiiotool steps and letterbox), Transform (ffmpeg filters and letterbox), Burn-in (including on/off), Slate, Filmstrip, Thumbnail, Metadata, and the **Destination** (output, thumbnail and filmstrip paths and names: not a vfx-transcode area, it is where the [process app](PROCESS.md) puts the output by default; its users can change it there). Super-presets saved with the earlier Letterbox area load it into Transform; older ones that hold only some areas load those.
- **Save** stores all the current settings under **Name** (asks before replacing one; with Name empty, it asks for the name), with the **Notes** (several lines). It writes to the studio file, multi-user safe, keeping the previous version as `super_presets_safecopyNNN.json`; if the studio file can't be written, it offers the local file instead. The super-preset also records the **Input type** it was made for: `"input_type"` (e.g. `png`) for the tuner, and `"inputs"` (`exr`, `image` or `movie`) for the [process app](PROCESS.md), which offers it only for that kind of input.
- **Load** copies the selected super-preset into all the tabs and refreshes. With no input, it also sets the Input type it was made for; with an input, the file decides, and the status line says when the super-preset was made for another type.
- **Duplicate** saves a copy of the selected super-preset under a new name (it suggests "NAME copy"), to start a variant from. **Delete** removes the selected one from the file it came from. See [OPTIONS.md](OPTIONS.md#super-presets).
- The input, colour, frame rate and trim are not part of a super-preset. The output type is saved with Encode (super-presets saved before that keep the current output type when loaded).
- Super-presets are what the [process app](PROCESS.md) offers to its users. There, it checks each one against the inputs: what it makes, and which areas don't apply. `"inputs": ["exr", "image", "movie"]` in `super_presets.json` limits a super-preset to those inputs; the tuner writes the one kind it was saved for, and you can widen it by hand (e.g. `["exr", "image"]`).

**Output type** (on the Output tab): a movie (the encode preset), or an EXR, PNG, JPG, TIF or DPX sequence with a sequence preset (see [OPTIONS.md](OPTIONS.md#image-sequence-output)). It decides which tools run, and the tabs that don't apply are greyed out:

| Input | Output | Runs | Transform Sequence | Transform, Burn-in | Slate, Metadata |
|---|---|---|---|---|---|
| movie | movie | ffmpeg | – | yes | yes |
| movie | PNG / JPG / TIF / DPX | ffmpeg | – | yes | – |
| movie | EXR | (unpack) + oiiotool | yes | – | – |
| EXR sequence | movie | oiiotool + ffmpeg | yes | yes | yes |
| other sequence | movie | ffmpeg | – | yes | yes |
| any sequence | any sequence | oiiotool | yes | – | – |

**Process** runs the whole conversion with the current settings: the full command of the Presets tab, written where the **Destination** tab says. The filmstrip is made too when **Add the filmstrip to the full command** is ticked on the Filmstrip tab. While it runs, the button turns into **Cancel**, a busy bar runs, and the last line vfx-transcode printed shows beside it; everything it prints goes to the Log tab. When it finishes, the line shows `Done:` and the movie's path, or that it failed. One conversion at a time; previews keep working while it runs. Closing the window during a conversion asks first.

## Tabs

The tabs on the right show their names in three rows, so all of them can be seen at once. A tab that doesn't apply to the input and output is greyed out (hover for why). A name with ` *` (e.g. **Encode \***) has settings changed since its preset was loaded or saved, not saved as a preset yet: Color, Tokens, Multichannel (channel order), Encode, Destination, Metadata, Transform Sequence, Transform, Burn-in, Slate, Filmstrip, Thumbnail. A name in *italics* is not used: switched off or empty (Burn-in off or without text, Slate, Filmstrip and Thumbnail off, no transform or letterbox, Color not used for this input and output, no tokens, metadata preset `none` with no tags).

- **Input**:
  - **Input**: any frame of a sequence, or a movie. The line under it shows what was found: the sequence (pattern, range, frame count, missing frames), a single still, or the movie's size, codec, frame rate, frame count and timecode.
  - **Input type**: movie, EXR, PNG, JPG, TIF or DPX sequence. Set from the input file and greyed out while there is one. With no input, choose it: the tabs then grey out and the output options follow as for that kind of input, so super-presets can be set up without footage.
  - **Frame rate (-r)**: `auto` (the EXR header's or the movie's rate, else 24), a common rate, or any rate typed in (`24000/1001`). **Detect** reads it from the movie or the first EXR's header, shows where it came from and puts it in the field.
  - **Pixel aspect (-par)**: `auto` (the EXR header's `PixelAspectRatio` or the movie's sample aspect ratio, else 1), a common value (2, 1.8, 1.5, 1.33, 1.3, 1.25 …; 1.33 means exactly 4/3, 1.3 is 13/10) or any value typed in (`4/3`). Set it when the header doesn't carry it: PNG / JPG / TIF frames never do, and many EXRs and movies don't. A value set here wins over the header. **Detect** reads it from the movie or the first EXR's header and puts it in the field. The preview shows anamorphic output un-squeezed.
  - **Movie: first frame number**: the frame number of a movie's first frame (`-movie_first_frame`, default 1001).
  - **Missing frames** (`-missing_frames`): holes in the sequence or frames that fail to convert either hold the previous frame (the default) or stop with an error. Saved with super-presets in the Encode area.
  - **Compare to source** (on by default): Compare mix dissolves to the unmodified input frame. Off: to the **Compare to** file, which can be an image (shown as it is), a movie or a sequence (the same frame number as the preview, or the nearest one it has). Choosing a compare file unticks Compare to source. The compare frame is made by vfx-transcode in the background after each preview, only when the frame or the compare settings changed.
  - **Timecode and reel name** section (used for a movie, burn-ins and the slate; greyed out when only oiiotool runs): timecode source (`counter`, `arg`, `metadata`) and timecode, reel name source (`auto`, `arg`, `metadata`, `off`) and reel name. A new input sets the sources, with a line saying why:

    | Input | Timecode source |
    |---|---|
    | EXR with a timecode in the header | `metadata` |
    | EXR without | `counter` |
    | PNG / JPG / TIF … sequence | `counter` |
    | Movie with a timecode | `metadata` |
    | Movie without | `arg`, timecode `01:00:00:00` |

    The reel name source is set to `auto`. Change either afterwards if needed; it is set again only when the input changes. The Timecode field is greyed out unless the source is `arg`. **Last preview** shows what the sources gave in the last preview, e.g. `Reel name: auto → A001C003 (EXR header owner)` or `auto → none (no Reel name given and none in the header)`, and `Timecode: metadata → 01:00:10:00 (movie)`.
- **Color** (greyed out when oiiotool doesn't run, e.g. a movie or PNG sequence to a movie):
  - **Colour preset**: pick one of the presets file's colour presets (e.g. `rec709_to_acescg`) and it is loaded into the fields below; **Save as preset...** saves the fields as a named colour preset.
  - `-ci` colour in, `-co` colour out (display-referred), `-display`, `-view`, `-looks`, `-clf`, `-ocio` config.
  - **Dropdowns from the config:** `-ci`, `-co`, `-display`, `-view` and `-looks` are editable dropdowns filled from the `-ocio` config.
    - `-ci` and `-co` list the colour spaces (inactive ones left out), then the roles.
    - `-view` lists the chosen display's views, or every view when no display is set.
    - The line under `-ocio` sums it up, e.g. `22 colour spaces, 1 display (read from the file); default display ACES, view Rec.709`.
    - Any other name can still be typed; the first, empty entry means vfx-transcode's default.
  - **Reading the config:** at start, when the `-ocio` field changes, and with **Scan**, which reads it again even if it hasn't changed.
    - Normally through `oiiotool --colorconfiginfo`, the same OCIO vfx-transcode uses, so built-in `ocio://` configs work too.
    - Without oiiotool, a `.ocio` file is read directly; that covers the usual OCIO v1 / v2 layout.
  - **Starting values:** `-co` is **Output - Rec.709** and `-ocio` is `./ocio/aces_1.2cg/config.ocio`, the ACES 1.2 config in the application's `ocio` folder. `-ci` is empty, i.e. vfx-transcode's default, ACEScg.
  - **When they're used** (the line at the top of the tab says which applies):
    - EXR input to a movie or a display format (JPG, PNG …): always.
    - EXR to EXR, and PNG / JPG / movie frames going through oiiotool (e.g. a movie to EXRs): only with **Convert colour** ticked. Otherwise the tuner leaves out the colour options, and vfx-transcode doesn't convert those.
- **Tokens**: the smart tokens (see [OPTIONS.md](OPTIONS.md#smart-tokens)), per-project keywords such as `{shot}`, `{task}` or `{client}`, made from the name or other keywords.
  - **Smart tokens**: *(file default)*, `none` or a preset from the presets file (e.g. `example_project`); picking one loads its definitions. **Save as preset...** saves the table as a smart tokens preset.
  - **The table**: one row per definition, processed top to bottom (a later row that applies wins; rows can use the tokens above them). Columns: `name` (the keyword it sets), `conditions` (e.g. `{namesplit-2} == plate; {version_string} !=`, all must hold, `;` between), `source` (empty = `{clean_name}`), `regex`, `repl`, `mode` (extract / replace), `case` (as_is / lower / upper / title), `notes`. Hover a column heading for help. **Add**, **Duplicate**, **Remove**, **Up**, **Down**.
  - Unchanged, the preset is sent by name (`-smart_tokens NAME`); edited, the rows are sent as `-smart_tokens_def JSON`.
  - **Values in the last preview**: what each token came out as for this input, or *(not set)* when no row applied.
  - **Name tokens** section: **Version regex** (`-version_regex`, empty = the default `_v(\d\d\d)`) finds the version for `{version_string}` / `{version_number}`. The table adds custom keywords `{NAME}` from a regex on the sequence name (`-name_token NAME=REGEX`, group 1 or the whole match); they add to or override the presets file's name tokens (listed under the table), and an empty regex removes one. Kept when another smart tokens preset is loaded.
- **Intermediate**: the frames oiiotool converts EXRs into, and ffmpeg unpacks movies into, before ffmpeg encodes them: their quality, size and speed, and where they go. Saved with super-presets as the Intermediate area.
  - **Format** (`-mezzanine`): PNG (lossless, the default), TIF (lossless, larger, quick to write), JPG (small and quick, lossy, 8-bit), BMP (uncompressed, quickest, largest) or EXR (float; EXR input only, a movie unpacks to PNG).
  - **16-bit** (`-mezzanine_16bit`, PNG and TIF): slower and larger, but a 10-bit preset (ProRes, DNxHR, H.265 10-bit) gets real 10-bit input. With 8-bit, a 10-bit movie input is cut to 8 bits too.
  - **Location** (`-temp_location`): a new folder in your temp folder for each run (the default, usually a fast local disk), or a folder next to the source (`-temp`, its name; empty: the input folder itself). **Keep** (`-temp_keep`) leaves the files after the run.
  - **oiiotool**: **Threads** (`-oiio_threads`: 25%, 50% (the default), 75%, 100% of the threads of the machine that runs the conversion, worked out there, or type a number; 0 = all) and **One oiiotool per file** (`-oiio_per_file`) instead of `--parallel-frames` batches, slower, for when the batches fail. **Convert suspected black frames again** (`-oiio_verify`, on by default): frames that come out far smaller than the others (oiiotool's random black frames) are converted again one at a time; the log names the frames it fixed.
  - Only what differs from the defaults goes into the command. The line at the bottom says what the choice means.
- **Multichannel** (EXR input; greyed out otherwise): which part and channels of the EXR become the picture.
  - The tree shows the input's parts, their layers and channels, read from its header (again with **Read the channels again**). Click a layer to use its R, G, B and A; click a single channel (`depth.Z`, `N.x`) to show it as grey; click a part to look only in it.
  - **Use**: *Automatic* (the first set of the presets file's `channel_preferences` found; the list is shown), *The first layer* (`-oiio_channel_use_first`), or *These channels* (`-oiio_channel`): R, G, B and an optional A, or one channel with **One channel, shown as grey**.
  - **Part** (`-oiio_subimage`): all parts in order, or one, for multi-part EXRs whose parts use the same channel names.
  - **Automatic order** (shown with *Automatic*): the channel sets tried, in order, before falling back to the first layer. **Preset** picks a named order from the presets file (`channel_preference_presets`; the first entry is its `channel_preferences`), with **Save as preset...** and **Delete**. Edit the list here: double-click a set to type it (`beauty.R,beauty.G,beauty.B,beauty.A`), drag or **Up** / **Down** to reorder, **Add the layer** adds the layer last clicked in the tree, **Reset** goes back to the file's list. A preset left unchanged is sent as `-channel_preferences NAME`, an edited order as `-channel_order`; super-presets write the order out in full.
  - The line under it says exactly what vfx-transcode will use, e.g. `R,G,B (requested, part 1 diffuse)`, or why it can't. The preview shows the chosen channels too. Saved with super-presets as the Multichannel area.
  - The automatic order's channel names may hold wildcards (`*`, `?`, `[abc]`; case counts): `Actor*.R,Actor*.G,Actor*.B` uses the first layer starting with `Actor` that has R, G and B. The first set found ends the search. The keywords `{multi_layers_line}`, `{multi_layers_column}`, `{multi_layerschannels_line}` and `{multi_layerschannels_column}` list the input's layers (and their channels) for burn-ins and the slate; the Info tab shows them.
  - **Deep EXR**: **Depth range (near,far)** (`-deep_zrange`) for deep EXRs without colour, shown as depth; empty = measured on the first frame.
- **Output**:
  - **Output type** (first): a movie, or an EXR, PNG, JPG, TIF or DPX sequence. It decides which tools run (shown under it) and which tabs are greyed out; see the table above. Choosing another type resets the sequence preset to that format's default.
  - **Encode preset** (movie output): `-preset`; empty = the presets file's default, with its notes. It sets the codec, container and pixel format, so the preview shows e.g. h264's 4:2:0.
  - **Sequence preset** (image sequence output written by oiiotool): `-seq_preset`, the presets of the chosen format only (e.g. `exr_piz_half`, `exr_zip1_half`, `exr_zip16_half`, `exr_dwab30_half`, `exr_dwab60_half` for EXR). The first entry is the format's default. The line under it shows the preset's notes and oiiotool arguments. Greyed out when ffmpeg writes the sequence (a movie to JPG …).
  - **Frame rate** (`-output_fps`): **same as input** (the default), or a rate from the list (any rate can be typed). Every frame is kept and played at that rate, e.g. 24 → 25 plays 4.2 % faster. The line under it says how much faster or slower, when the input's rate is known. Saved with super-presets (the Encode area). See [OPTIONS.md](OPTIONS.md#output-frame-rate).
  - **Write timecode / Write reel name** (both on by default): `-output_timecode 0` and `-output_reelid 0` when unticked. They work for a movie and for an image sequence written by oiiotool whose format can store them: EXR (timecode and reel name), DPX (timecode). The grey line under them says what the chosen output keeps; a switch the output can't use is greyed out. Burn-ins and the slate still show both. When the reel name source is `auto` and a preview finds no reel name, Write reel name is unticked (the status bar says so); if a later preview finds one, it is ticked again, unless you unticked it yourself.
  - **Alpha**: *No alpha* (`-alpha drop`, the default), *Keep alpha if the input has one* (`-alpha auto`) or *Keep alpha; stop if the output cannot store it* (`-alpha keep`), see [OPTIONS.md](OPTIONS.md#alpha). **Premultiplied colour** (`-alpha_premult`, with alpha kept) writes colour premultiplied by alpha instead of straight. The line under it says what happens with the chosen output.
  - **Output pixel aspect** (`-par_out`): `same` (the default: keep the pixels, tag the output with the input's pixel aspect), `square` (reformat for square pixels; the height is scaled) or a pixel aspect (`2`, `4/3` …) to reformat for. Saved with super-presets in the Encode area.
  - The output path is under the picture, beside **Process**, with a reminder of the output type.
- **Encode** (after Output): write new presets. **Movie encode preset** (ffmpeg, `encode_presets`): pick a preset to start from (its notes are shown under the dropdown), then edit the **Container** (mov, mp4, mxf …), the **ffmpeg arguments** (`{videofilter}`, a word of its own, is where the filters go) and the **Alpha pixel format** used when alpha is kept (empty: the codec cannot store alpha). `ten_bit` follows the `-pix_fmt` in the arguments (10, 12 or 16 bits); a loaded preset's `colour_tags` and codec tags (`metadata`, e.g. ProRes `vendor_id`) are kept when it is saved again, a new preset gets colour tags and no codec tags. **Image sequence preset** (oiiotool, `sequence_presets`): the **Format** and the **oiiotool arguments** (`-d half --compression dwaa:45` …). The line under each says whether the presets file would take it. **Save as preset...** saves it (asking for a name and notes) and picks it on the Output tab (a sequence preset when the output type is its format).
- **Loading presets**: the preset dropdowns (Color, Transform Sequence, Transform, Burn-in, Slate, Filmstrip, Thumbnail, Encode, Metadata) load a preset as soon as you pick it; there are no Load buttons. The first entry, *(pick a preset to load)*, shows that nothing was loaded yet. Picking a slate preset also switches the slate on.
- **Saving presets**: the **Save as preset...** buttons ask for a name (pick an existing one to replace it) and notes, and save into the presets file in use, the studio one when `vfx-transcode.ini` names it, multi-user safe. The previous version is kept as `presets_safecopyNNN.json`. See [OPTIONS.md](OPTIONS.md#saving-presets-from-the-tuner).
- **Transform Sequence** (oiiotool; greyed out when oiiotool doesn't run):
  - Pick a transform preset and a sub-preset to see its conditions, and which sub-preset vfx-transcode would choose for this input (after the first refresh).
  - Picking a transform preset loads its first sub-preset; picking a sub-preset loads that one: its oiiotool steps go into the editable list. **Save as preset...** saves the list as a sub-preset: into an existing transform preset (a sub-preset of the same name is replaced and keeps its conditions; a new one gets the loaded sub-preset's conditions) or as a new transform preset. Any sub-preset can be loaded and edited, including one that would not be chosen for this input; the preview uses the list as it is.
  - **Use these oiiotool steps** switches the list on or off. Off, the frames keep their size: the tuner always sends `-width 0`, so all scaling comes from the two transform tabs.
  - **Letterbox (oiiotool)**: drawn on the converted frames (`-oiio_letterbox`), so it works without ffmpeg.
- **Transform** (ffmpeg; greyed out when ffmpeg doesn't run):
  - **ffmpeg transform**: scale / zscale filters, one per line (`#` = comment), sent as `-ff_transform`. Empty = no scaling. **Preset**: picking one of the presets file's ffmpeg transform presets puts it in (`hd_lanczos`, `hd720_bicubic`, `2k_zscale`, `hd_fit_pad` are shipped, the former examples); **Save as preset...** saves the text as a named one.
  - **Letterbox (ffmpeg)**: ratio, bar colour and opacity, line thickness, colour and opacity (`-letterbox`).
- **Burn-in** (greyed out when ffmpeg doesn't run):
  - **Burn-ins on** switches all burn-ins on or off (off sends `-burn_preset none`).
  - Pick a burn-in preset to load it (or save the current burn-ins as one with **Save as preset...**), then edit the text of the seven places (six plus the watermark) and the style for all places: colour, opacity, size, box.
  - Tick **own** on a place to give it its own style.
  - Margins, font.
- **Slate** (movie output only): switch on, load a preset (**Save as preset...** saves the slate as one; "burn-ins on the slate" is an option, not part of a preset), and edit title, subtitle, rows (add / remove / reorder), thumbnail, sizes, scale, vertical placement, colours, the line under the title, hide-empty, and burn-ins on the slate. **Logo**: a vendor logo image (PNG with alpha), its position (top / middle / bottom, left / centre / right), height (% of the picture height or pixels) and distance from the edges (x, y); the logo controls are greyed out until an image is chosen.
- **Filmstrip**: load a preset (or save the settings as one with **Save as preset...**), and edit the number of tiles, tile size, grid (columns x rows, `auto` follows the tiles), jpg quality, where the tiles come from (`source`, `intermediates`, `movie`) and the slate as the first tile. **Add the filmstrip to the full command** puts `-of` and these settings into the Presets tab's full command.
- **Destination** (after Encode): where the output goes, and the thumbnail and filmstrip.
  - **Output**: a **Path** (the folder: `./` is next to the input, `../` the folder above; `{sourcepath}`, `{sourcefolder-1}`, `{base_folder}` … work) and a **Name** (the file name without extension; empty for the input's name). A sequence gets `.####.EXT`. Each has a preset menu with **Save as preset...** and **Delete**: the presets are the `destination_paths` and `destination_names` of the presets file, e.g. "Source and name subfolder" `{sourcepath}/{clean_name}/` and "Source _transcoded" `{clean_name}_transcoded`. The path menu also has **One folder for all...** (asks for the folder) and **Custom**; typing in a field switches its menu to Custom. The line underneath shows the `-o`, `-ot` and `-of` that result, and the output type.
  - `{custom_destination_path}` and `{custom_destination_name}` leave that part to the process app's user. The tuner then shows a Custom path / Custom name field, for a value to test with; Process needs one.
  - **Thumbnail** and **Filmstrip**: a Path and a Name menu each, from the same presets, or **(as the output)**. With both as the output, the thumbnail gets the output's name (`.jpg`), and the filmstrip the output's name + `_filmstrip`.
  - Saved with every super-preset (output, thumbnail and filmstrip).
- **Metadata** (after Destination): picking a **Metadata preset** loads it into the tab: its tags, attributes copied and reel attribute / pattern (its timecode and reel sources go to the Input tab's fields). Empty is the presets file's default, `none` no preset. While unchanged, only `-metadata_preset NAME` is written; once edited, `-metadata_preset none` and the tab's settings (a note says so). **Save as preset...** saves the tab, with the Input tab's timecode and reel sources when set, as a metadata preset (`metadata_presets`). The tab: **Copy EXR attributes** (`-metadata_copy`: `all`, or key patterns with `*` and `-` to exclude, e.g. `all,-husk:*`) — the line under it says how many attributes of the last preview's header it copies, or that a pattern matches nothing and how the keys start (e.g. Unreal's are `unreal/...`, so `unreal/*`) and the tags (KEY / VALUE, keywords work). Used for a movie, and for an image sequence written by oiiotool, where the tags go into each image (EXR keeps all). **Reel name from the EXR header**: the **Attribute** (`-reelid_attr`, default `owner`) and **Pattern** (`-reelid_pattern`) the reel name is read with, when the reel source is auto or metadata. Empty fields use the metadata preset's values. Saved with super-presets as the Metadata area.
- **Thumbnail**: **Write a thumbnail** adds `-ot` to the full command and to Process: one jpg or png of a frame, with the movie's colour and transform but no burn-ins, next to the output. **Preset** loads a thumbnail preset (`-thumbnail_preset`; the first entry is the presets file's default) into the settings below: **Frame** (middle, first, last, largest = the busiest, or a frame number), **Size** (auto = follow the other side; both auto = the movie's size), **Format** (jpg / png), **jpg quality**, **png compression** and **png bits**. Edit them freely: while they match the preset only `-thumbnail_preset NAME` is written; once changed, the settings go out as `-thumbnail_frame`, `-thumbnail_size`, `-thumbnail_format` and `-thumbnail_quality` or `-thumbnail_compression` / `-thumbnail_bits`, and a note says so. **Save as preset...** saves them as a named thumbnail preset into the presets file (`thumbnail_presets`). Saved with super-presets as the Thumbnail area, so the process app writes it too.
- **Presets**: the **vfx-transcode command** the tuner runs, the presets file in use, and the **Full command** for these settings (**For a .bat file** writes `%` as `%%`).
  - **Full command**: the whole vfx-transcode command for the movie with these settings (`-o ./` = next to the source). Each option is coloured by the tab it comes from, with a key right above the box (only the tabs in the command); copying gives plain text.
  - A loaded preset that was not changed is written as its name (`-burn_preset review`, `-slate generic`, `-oiio_transform NAME` when the loaded sub-preset is the one that would be chosen). Otherwise the settings are written out in full.
- **Info** (under the picture, between Super-presets and Log): the metadata viewer, with a search box that filters keys and values (any case; a section name shows the whole section), and **hide empty**, which leaves out keys whose value is empty.
  - Input: the first frame's header and the previewed frame.
  - Output: size, pixel aspect, pixel format, frame rate, timecode, reel name, colour, transform and letterbox, and the metadata tags written to the movie.
  - Keywords: the values of `{input_width}`, `{namesplit1}` and the other keywords for this input.

## Notes

- The preview's colours go through the encode preset's pixel format and back to RGB, so chroma subsampling and video range look like the movie. Codec compression is not simulated.
- `{current_frame}` and `{tc}` show the previewed frame's own number and timecode.
- The tuner's temporary files are in the user temp folder and are removed when it closes. The preview cache (`vfx-transcode/preview-cache` in the temp folder) keeps the last 60 converted frames.
