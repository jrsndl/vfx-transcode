# vfx-transcode tuner

A small PySide6 app for tuning presets by eye, and for converting. It edits the transform,
burn-in, slate, filmstrip, letterbox and metadata settings, runs vfx-transcode on one frame of
your input (a sequence or a movie file), and shows the result exactly as the movie would show
it. Drag the timeline under the picture to look at any frame. When the settings look right,
save them as a **super-preset**, press **Process** to make the movie, or copy the command-line
options or `presets.json` snippets from the **Presets** tab. The presets file is the one vfx-transcode would use: the tuner's own `-presets_file` if it was started with one, else the studio preferences named in `vfx-transcode.ini`, else `presets.json` next to the application (see [OPTIONS.md](OPTIONS.md#which-presets-file-is-used)). The Presets tab shows which file that is and why; if the studio preferences could not be used, the status bar says why.

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
- **Show overlay**: a small label over the top right of the picture with the input and output size, e.g. `input: 3840x2160 pa 1.3 @ 24fps` / `output: 1920x1080 pa 1.0`. The pixel aspect is shown only when the input's or the output's is not 1. It is drawn by the tuner, not part of the image.
- **Compare mix** (Image and Slate views): 0 shows the result and 100 the compare frame. Values in between dissolve. The compare frame is scaled to the result's size. By default it is the **source**: the input's own frame at the previewed frame, unmodified (no transform, resize, burn-ins or slate; EXR gets the Color tab's colour conversion so it can be looked at). See **Compare** on the Input tab.
- Anamorphic output (pixel aspect other than 1) is shown un-squeezed.
- **Trim**: **In** and **Out** on the timeline (the **I** and **O** keys, or the buttons; the keys don't fire while you type in a field) mark the frames to use (`-in` / `-out`). The kept range is shaded, In is green, Out is red, and the frame count is shown next to the buttons. Setting In after Out (or Out before In) clears the other one; **Clear** removes the trim. The preview can still show any frame; its burn-ins show the trimmed range.
- **Timeline** under the picture: the input's frames, first to last (a movie: from **Movie: first frame number**). Drag the handle and let go to preview that frame; click beside the handle or use the arrow / Page keys to step, and the preview follows after a short pause; or type a frame number. It shows the frame's timecode: from the movie's timecode, or, after the first preview, the timecode vfx-transcode uses (the frame counter by default). Frames missing from a sequence show the nearest frame. In the Filmstrip view the timeline sets the frame for the Image and Slate views.
- Under the timeline, two tabs: **Super-presets** (below) and **Log**, which shows each vfx-transcode run; **Clear** empties it. The window uses a dark theme.
- **Refresh** runs the preview of the view on screen; **Refresh after edit** runs it half a second after each change. A change made while a preview is running restarts it.

## Super-presets and Process

The **Super-presets** tab under the timeline saves the current settings of several areas under one name, and loads them back. See [OPTIONS.md](OPTIONS.md#super-presets) for the file and for using super-presets on the command line (`-super_preset NAME`).

- **The list** shows the super-presets in `super_presets.json` next to the presets file (the path is shown under the buttons): the studio one when `vfx-transcode.ini` names studio preferences, plus any local ones next to the application, in italic (hover for the file). **Save** writes to the studio file, multi-user safe, keeping the previous version as `super_presets_safecopyNNN.json`; if the studio file can't be written, it offers the local file instead. **Delete** removes the entry from the file it came from. See [OPTIONS.md](OPTIONS.md#super-presets). Selecting one fills in its name and notes, and ticks the areas it holds.
- **Areas**: Encode (the encode and sequence presets, alpha), Transform Sequence (oiiotool steps and letterbox), Transform (ffmpeg filters and letterbox), Burn-in (including on/off), Slate, Filmstrip, Metadata. Tick the ones to save or load. Super-presets saved with the earlier Letterbox area load it into Transform.
- **Save** stores the ticked areas of the current settings under **Name** (asks before replacing one); with Name empty, it asks for the name. **Load** copies the ticked areas of the selected super-preset into the tabs and refreshes; the other areas stay as they are. **Delete** removes the selected one.
- The input, colour, frame rate, trim, output type and output path are not part of a super-preset.

**Output type** (on the Output tab): a movie (the encode preset), or an EXR, PNG, JPG, TIF or DPX sequence with a sequence preset (see [OPTIONS.md](OPTIONS.md#image-sequence-output)). It decides which tools run, and the tabs that don't apply are greyed out:

| Input | Output | Runs | Transform Sequence | Transform, Burn-in | Slate, Metadata |
|---|---|---|---|---|---|
| movie | movie | ffmpeg | – | yes | yes |
| movie | PNG / JPG / TIF / DPX | ffmpeg | – | yes | – |
| movie | EXR | (unpack) + oiiotool | yes | – | – |
| EXR sequence | movie | oiiotool + ffmpeg | yes | yes | yes |
| other sequence | movie | ffmpeg | – | yes | yes |
| any sequence | any sequence | oiiotool | yes | – | – |

**Process** runs the whole conversion with the current settings: the full command of the Presets tab with **Output (-o)** as the output path (a sequence: a folder gets `{clean_name}.####.EXT`, a name gets `.####.EXT`) (`./` = next to the input; a folder ends in `/`; keywords work). The filmstrip is made too when **Add the filmstrip to the full command** is ticked on the Filmstrip tab. While it runs, the button turns into **Cancel**, a busy bar runs, and the last line vfx-transcode printed shows beside it; everything it prints goes to the Log tab. When it finishes, the line shows `Done:` and the movie's path, or that it failed. One conversion at a time; previews keep working while it runs. Closing the window during a conversion asks first.

## Tabs

- **Input**:
  - **Input**: any frame of a sequence, or a movie. The line under it shows what was found: the sequence (pattern, range, frame count, missing frames), a single still, or the movie's size, codec, frame rate, frame count and timecode.
  - **Frame rate (-r)**: `auto` (the EXR header's or the movie's rate, else 24), a common rate, or any rate typed in (`24000/1001`). **Detect** reads it from the movie or the first EXR's header, shows where it came from and puts it in the field.
  - **Pixel aspect (-par)**: `auto` (the EXR header's `PixelAspectRatio` or the movie's sample aspect ratio, else 1), a common value (2, 1.8, 1.5, 1.33, 1.3, 1.25 …; 1.33 means exactly 4/3, 1.3 is 13/10) or any value typed in (`4/3`). Set it when the header doesn't carry it: PNG / JPG / TIF frames never do, and many EXRs and movies don't. A value set here wins over the header. **Detect** reads it from the movie or the first EXR's header and puts it in the field. The preview shows anamorphic output un-squeezed.
  - **Movie: first frame number**: the frame number of a movie's first frame (`-movie_first_frame`, default 1001).
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
- **Output**:
  - **Output type** (first): a movie, or an EXR, PNG, JPG, TIF or DPX sequence. It decides which tools run (shown under it) and which tabs are greyed out; see the table above. Choosing another type resets the sequence preset to that format's default.
  - **Encode preset** (movie output): `-preset`; empty = the presets file's default, with its notes. It sets the codec, container and pixel format, so the preview shows e.g. h264's 4:2:0.
  - **Sequence preset** (image sequence output written by oiiotool): `-seq_preset`, the presets of the chosen format only (e.g. `exr_piz_half`, `exr_zip1_half`, `exr_zip16_half`, `exr_dwab30_half`, `exr_dwab60_half` for EXR). The first entry is the format's default. The line under it shows the preset's notes and oiiotool arguments. Greyed out when ffmpeg writes the sequence (a movie to JPG …).
  - **Write timecode / Write reel name** (both on by default): `-output_timecode 0` and `-output_reelid 0` when unticked. They work for a movie and for an image sequence written by oiiotool whose format can store them: EXR (timecode and reel name), DPX (timecode). The grey line under them says what the chosen output keeps; a switch the output can't use is greyed out. Burn-ins and the slate still show both. When the reel name source is `auto` and a preview finds no reel name, Write reel name is unticked (the status bar says so); if a later preview finds one, it is ticked again, unless you unticked it yourself.
  - **Alpha**: *No alpha* (`-alpha drop`, the default) or *Keep alpha if the input has one* (`-alpha auto`, see [OPTIONS.md](OPTIONS.md#alpha)). The line under it says what happens with the chosen output: kept (EXR, PNG, TIF, or an encode preset marked `[alpha]` such as `prores4444`), or why the output can't store it (h264, JPG, DPX …). Saved with the Encode area of a super-preset.
  - **Metadata** section (last on the tab): metadata preset and extra tags (KEY / VALUE, keywords work; an empty value removes a preset's tag). Used for a movie, and for an image sequence written by oiiotool, where the tags go into each image (EXR keeps all; see [OPTIONS.md](OPTIONS.md#metadata)). Greyed out when ffmpeg writes the images (a movie to JPG …), which carry no tags.
  - The output path is under the picture, beside **Process**, with a reminder of the output type.
- **Loading presets**: the preset dropdowns (Color, Transform Sequence, Transform, Burn-in, Slate, Filmstrip) load a preset as soon as you pick it; there are no Load buttons. The first entry, *(pick a preset to load)*, shows that nothing was loaded yet. Picking a slate preset also switches the slate on.
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
- **Presets**: what to copy.
  - **Full command**: the whole vfx-transcode command for the movie with these settings (`-o ./` = next to the source).
  - One box per area with its command-line options, and for transform, burn-in, slate and filmstrip a `presets.json` snippet (the edited sub-preset or preset) to paste into your presets file.
  - A loaded preset that was not changed is written as its name (`-burn_preset review`, `-slate generic`, `-oiio_transform NAME` when the loaded sub-preset is the one that would be chosen). Otherwise the settings are written out in full.
  - **For a .bat file** writes `%` as `%%`.
- **Info**: the metadata viewer, with a search box that filters keys and values (any case; a section name shows the whole section).
  - Input: the first frame's header and the previewed frame.
  - Output: size, pixel aspect, pixel format, frame rate, timecode, reel name, colour, transform and letterbox, and the metadata tags written to the movie.
  - Keywords: the values of `{input_width}`, `{namesplit1}` and the other keywords for this input.

## Notes

- The preview's colours go through the encode preset's pixel format and back to RGB, so chroma subsampling and video range look like the movie. Codec compression is not simulated.
- `{current_frame}` and `{tc}` show the previewed frame's own number and timecode.
- The tuner's temporary files are in the user temp folder and are removed when it closes. The preview cache (`vfx-transcode/preview-cache` in the temp folder) keeps the last 60 converted frames.
