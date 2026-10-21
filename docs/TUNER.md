# vfx-transcode tuner

A small PySide6 app for tuning presets by eye. It edits the transform, burn-in, slate, letterbox
and metadata settings, runs vfx-transcode on one frame of your test footage, and shows the
result exactly as the movie would show it. When a setting looks right, copy the command-line
options or the `presets.json` snippet from the **Output** tab.

```
uv sync --extra tuner
uv run vfx-transcode-tuner D:/shots/sh010/comp/sh010_comp_v003.1001.exr [D:/ref/sh010_ref.png]
```

Without uv: `pip install PySide6`, then `python vfx-transcode-tuner.py ...`.

- **input1**: the test footage, any frame of a sequence. Required; it can also be chosen on the Input tab.
- **input2**: an image to compare to. Optional. PNG, JPG or TIFF, anything Qt can read (not EXR).

## How it works

Every refresh runs `vfx-transcode ... -preview FILE.png` (see [OPTIONS.md](OPTIONS.md#preview)):

1. vfx-transcode converts one frame with oiiotool, with the same channels, transform, colour or CLF, and alpha as the movie.
2. It runs the movie's ffmpeg chain on it: scale, pixel format, letterbox and burn-ins.
3. It writes the frame as PNG, the slate as a second PNG, and a JSON file with the input header and the output metadata.

The frame is picked like a thumbnail: middle by default, or first, last or a frame number on the Input tab. The converted frame is cached, so changes to burn-ins, slate, letterbox or metadata refresh in about a second, without running oiiotool again.

The **Filmstrip** view runs `vfx-transcode ... -of FILE.jpg` instead (see [OPTIONS.md](OPTIONS.md#filmstrip)) with the Filmstrip tab's settings. With tiles from the `source` frames only the filmstrip is made, which is quick. With tiles from the `intermediates` or the `movie`, or with the slate as the first tile, the whole movie is made as well (in the tuner's temp folder), with the burn-ins, slate and letterbox, so that refresh takes as long as a real run.

The command the tuner runs is shown in the log under the viewer. By default it is this Python with `vfx-transcode.py` from the same folder; the **vfx-transcode command** field on the Input tab can point to a built `vfx-transcode.exe` instead.

## Viewer

- **Pan**: drag. **Zoom**: mouse wheel (under the cursor). **Fit** and **1:1** in the toolbar.
- **View**: **Image** (the previewed frame), **Slate** (the slate frame; switch the slate on in the Slate tab) or **Filmstrip**. Image and Slate come from the same preview run, so switching between them is instant. Switching to a view whose settings changed since its last run refreshes it (with **Refresh after edit** on). The view is fitted to the window when it changes.
- **Compare mix** (Image and Slate views): with a compare image, 0 shows the result and 100 the compare image. Values in between dissolve. The compare image is scaled to the result's size.
- Anamorphic output (pixel aspect other than 1) is shown un-squeezed.
- The log under the viewer shows each vfx-transcode run; **Clear** empties it. The window uses a dark theme.
- **Refresh** runs the preview of the view on screen; **Refresh after edit** runs it half a second after each change. A change made while a preview is running restarts it.

## Tabs

- **Input**: test footage, compare image, preview frame, presets file, encode preset (it sets the pixel format, e.g. 4:2:0 for h264), colour (`-ci`, `-co`, `-display`, `-view`, `-looks`, `-clf`, `-ocio`), and any other vfx-transcode options.
- **Transform**:
  - Pick a transform preset and a sub-preset to see its conditions, and which sub-preset vfx-transcode would choose for this input (after the first refresh).
  - **Load sub-preset** copies its oiiotool steps into the editable list. Any sub-preset can be loaded and edited, including one that would not be chosen for this input; the preview uses the list as it is.
  - **Use these oiiotool steps** switches the list on or off.
  - **Final ffmpeg resize** is `-width`.
- **Burn-in**:
  - Load a burn-in preset, then edit the text of the seven places (six plus the watermark) and the style for all places: colour, opacity, size, box.
  - Tick **own** on a place to give it its own style.
  - Margins, font.
- **Slate**: switch on, load a preset, and edit title, subtitle, rows (add / remove / reorder), thumbnail, sizes, scale, vertical placement, colours, the line under the title, hide-empty, and burn-ins on the slate.
- **Filmstrip**: load a preset, and edit the number of tiles, tile size, grid (columns x rows, `auto` follows the tiles), jpg quality, where the tiles come from (`source`, `intermediates`, `movie`) and the slate as the first tile. **Add the filmstrip to the full command** puts `-of` and these settings into the Output tab's full command.
- **Letterbox**: ratio, bar colour and opacity, line thickness, colour and opacity.
- **Metadata**: metadata preset, extra tags, timecode and reel name settings.
- **Output**: what to copy.
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
