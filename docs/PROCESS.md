# vfx-transcode process

A simple app for converting with super-presets, made in the tuner. Users who just need a
review movie (or frames) don't see the tuner's knobs.

```
python vfx-transcode-process.py [file or folder ...] [-presets_file PATH]
vfx-transcode-process.exe       (frozen, next to vfx-transcode.exe and the tuner)
```

It reads the same presets file and super-presets as vfx-transcode and the tuner: the
`-presets_file` option, else the studio preferences named in `vfx-transcode.ini`, else the
`presets.json` next to the application. Super-presets are read from `super_presets.json` next to
the presets file and next to the application.

## Process tab

```
Source       [ path ......................... ] [Add File] [Add Folder] [*.exr *.mov *.mp4]
Preset       [ super-preset list ]   [ what it makes, notes, how it fits the source ]
Destination  [Path preset v]                    [File name preset v]
             Path: [ ./review/ ......... ][...]  Name: [ {clean_name}_review ]
             Output: D:/shots/sh010/review/sh010_comp_v003_review.mp4
[ Process .......................................... ]  [Add to Queue]
[x] Play after conversion   [x] Explore after conversion
```

- **Source**: **Add File** (or drop a file, or type a path) uses any frame of a sequence, which
  brings in the whole sequence. Movies work too. **Add Folder** (or drop a folder) scans the
  folder and all its sub-folders in the background. The status bar shows the progress (files
  scanned, media found). The pattern field beside it then picks what to use from the scan:
  - The patterns are file-name wildcards: `*.exr`, `*.mov *.mp4`, `*beauty*.exr`. `!` leaves
    files out (`*.exr !*tech*`).
  - An image sequence counts as one input.
  - Only hidden folders (starting with `.`) are skipped; `_wip` and the like are searched.
  - Inputs found this way know the folder they were found in, which vfx-transcode gets as
    `-base_folder`. Destination and burn-ins can then use `{base_folder}` and
    `{base_folder-1}` …, besides `{sourcepath-1}` … and `{sourcefolder-1}` … (see
    [Path keywords](OPTIONS.md#name-keywords)). For example, the path
    `{base_folder}/review/{sourcefolder-2}/` collects a whole scan into one review folder.
  - The scan keeps the list of every media file it found. Changing the patterns, or picking
    another preset, filters that list again without a new scan. Adding the folder again scans
    it again (to see new files).
- **Preset**: the super-presets.
  - Each one is marked for the source: ✔ fits, ⚠ works with notes, ✖ can't be used.
  - The panel beside the list shows what the selected one makes (a `.mp4` / `.mov` movie, or
    EXR / JPG ... frames), its input type, its destination, its notes, and what doesn't fit.
  - Picking a preset sets the pattern field to the preset's input type, as recorded by the
    tuner (`input_type`: EXR gives `*.exr`, JPG `*.jpg *.jpeg`, movie `*.mov *.mp4 *.mxf ...`;
    without one, its `inputs` kinds). Your `!` exclusions stay. A preset that records neither
    leaves the field as it is.
  - Picking a preset also sets the Destination, when the super-preset has one.
  - The check covers several things:
    - encode or sequence presets that the presets file doesn't have;
    - the `inputs` kinds the super-preset is for (the tuner records them);
    - areas that don't apply to that input and output, e.g. oiiotool steps when making a movie
      from a movie, burn-ins on EXR frames, a slate on frames, or a one-frame movie from a
      single image.
- **Tweaks** (under the presets, collapsed; click to open): for when the
  detection or the super-preset gets the source wrong. Everything starts as **auto** / **as the
  preset**, which sends nothing. What you set goes to vfx-transcode after the super-preset's
  options, so it wins. The header says how many settings are changed.

  | Control | Option | Notes |
  |---|---|---|
  | Frame rate | `-r` | 23.976, 25 … or `24000/1001` |
  | Pixel aspect | `-par` | 1, 2, 1.33, `4/3` … |
  | Movie first frame | `-movie_first_frame` | movies: the first frame's number (default 1001) |
  | Timecode | `-timecode` (+ `-timecode_source arg`) | HH:MM:SS:FF |
  | Reel name | `-reelid` (+ `-reelid_source arg`) | keywords work, e.g. `{namesplit1}` |
  | Colour in | `-ci` | the list comes from the OCIO config (read when the group first opens) |
  | Alpha | `-alpha` | drop, keep or auto |
  | Write timecode / reel name | `-output_timecode` / `-output_reelid` | yes / no |

  Wrong values are flagged in the group, and stop Process until fixed. **Reset all to auto**
  clears it. The settings and whether the group is open are remembered.
- **Destination**: **Path** and **Name** are what is used. A super-preset can bring its own
  (the tuner saves it with every super-preset; in `super_presets.json` it
  is `"destination": {"path": "./review/", "name": "{clean_name}_review"}`). The selected preset's destination fills both
  fields: when it is picked, when the app starts, and when the super-presets are read again
  (Reload, or a change saved in the tuner). The user can change them; the rest of the
  super-preset still applies. A change is kept until another preset is picked, also between
  sessions. **Use preset's destination** puts the preset's back. The Output line says
  whether it shows the super-preset's destination or a changed one. The two menus
  fill the fields in too:
  - Path presets: the presets file's `destination_paths` (same folder as the source, `review`
    folders, source and name subfolder, custom destination path ...), then one folder for all
    (asks for the folder) and Custom.
  - Name presets: its `destination_names` (same name as the source, `{clean_name}_transcoded`,
    `{clean_name}_review`, date, shot + version, custom destination name ...), then Custom.

  Typing in a field switches its menu to Custom.

  The menus list the presets file's destination paths and names (the tuner's Destination tab
  saves them). The super-preset can also say where its thumbnail and filmstrip go; those
  follow the Path and Name here unless it gives them their own.

  The keywords `{custom_destination_path}` and `{custom_destination_name}` let a super-preset
  leave part of the destination to the user, e.g. Path `{custom_destination_path}/{sourcefolder-3}/`
  and Name `{custom_destination_name}_{clean_name}`. When the Path or Name uses one, a
  **Custom path** (with a folder picker) or **Custom name** field appears under them. Processing
  waits until it is filled in. The values are remembered, and go to vfx-transcode as
  `-custom_destination_path` / `-custom_destination_name` (on Deadline too). Both menus have an
  entry for them. `./` is the source's folder and `../` the
  folder above it. Keywords work as in vfx-transcode `-o`. The extension comes from the preset.
  The line below shows where the first output lands.

  The app reads each job's options as vfx-transcode will before queueing. A keyword
  vfx-transcode doesn't know in the destination, or a bad option, leaves that input out, and
  the app says why. It also checks two more things: no output may overwrite its input (for example,
  EXR to EXR in the same folder). No two inputs may write the same file. Inputs that fail are
  left out, and the app says why.
- **Process** adds a job per input and starts the queue. **Add to Queue** only adds them, so you
  can collect several batches, then press Start on the Queue tab or send them to Deadline.
- A super-preset with the Thumbnail area switched on (tuner, Thumbnail tab) also writes a thumbnail
  next to each output.
- **Play after conversion** opens the output when its job is done: a movie in the default
  player, a sequence's first frame in the default viewer. **Explore after conversion** shows it
  in Explorer. Both are stored with each job.
- Everything is remembered between sessions.

## Queue tab

Each job is a block:

```
review_h264                                              2026-11-25  11:42
In   D:/shots/sh010/comp/v003/     sh010_comp_v003.####.exr      [>] [folder]
Out  D:/shots/sh010/comp/review/   sh010_comp_v003_review.mp4    [>] [folder]
Running  [=========== 63%   Converting (oiiotool)   0:41 ...............]
```

- The top line shows the preset, and when the job was added.
- **In** and **Out** show the folder and the file name, with more room for the name. The Out
  path has its keywords filled in as vfx-transcode will: name and path keywords, `{date}`,
  the frame range, the custom destination values, the presets file's name and smart tokens,
  and the frame rate when Tweaks sets it. What only the files can tell (a detected frame rate,
  `{meta:...}`) stays as typed until the job is done; then Out shows the file vfx-transcode
  wrote. When a path
  doesn't fit, its start is cut and it is aligned right, so the end stays visible; the tooltip
  has it all.
- **[>]** opens the file with the system's program (a movie player, an image viewer).
  **[folder]** shows it in Explorer. The output buttons work once the job is done.
- The status (Waiting, Running, Done, Failed, Cancelled, On farm, Done on farm) sits beside the bar.
  Once a job is done, failed or cancelled, a **Log** button opens what vfx-transcode printed for
  it in a window of its own, with **Word wrap** (on at start) and **Copy**. The log is kept with the queue (its last 5000 lines). The
  bar shows the progress while the job runs, and afterwards the time it took (or why it
  failed).

- **Start / Pause** (the green button at the bottom of the tab): Pause lets the running jobs finish and starts no new ones. When the queue
  runs out of jobs it pauses itself, so a later **Add to Queue** doesn't start anything until
  you press Start (or Process).
- **Jobs at once**: how many conversions run side by side. oiiotool already uses every core,
  so more than 1 mostly helps with movies and short sequences.
- **Cancel** stops the selected running jobs (vfx-transcode and the oiiotool or ffmpeg it
  started) and skips the selected waiting jobs. **Retry** runs finished, failed or cancelled
  jobs again. **Remove**, **Clear finished** (done, done on farm and cancelled; jobs still on
  the farm stay), **Up** / **Down** reorder and tidy the queue.
- **Check farm status** (top right, by Send to Deadline): for each job **On farm**, asks
  Deadline (web service `/api/jobs?JobID=...`, or `deadlinecommand -GetJob`) how it is doing and
  looks for its output. Completed with the output there: **Done on farm** (play and explore
  work). Failed on Deadline, or completed without the output: **Failed**, with why. Still
  queued, rendering or suspended: stays **On farm**, the bar says what Deadline said. It asks in
  the background; the status bar sums it up.
- The log of the selected job is shown under the list. **Copy command** copies the
  vfx-transcode command lines.
- **Hide done** (off at start) leaves the jobs that are done out of the list.
- **Save Queue...** writes all the jobs to a `.vtq` file: their settings, status, progress
  message, times, Deadline job id and log (its last 5000 lines). The file also notes when, by
  whom and on which computer it was saved. One user can set up a queue, and another load it and
  process it.
- **Load Queue...** reads such a file. With jobs in the queue already, it asks whether to
  **Append** or **Replace** (Replace waits until nothing is running). Done and failed jobs keep
  their status and log; a job that was running when the file was saved comes back as waiting.
  It warns about waiting jobs whose input this computer can't find, so a queue made on another
  machine needs paths both can reach (a share, a mapped drive). Jobs run with this computer's
  vfx-transcode.
- The queue is saved, and comes back when the app is started again, without the finished
  jobs (Done, and Done on farm); Failed and Cancelled ones stay, to look at or retry, and
  On farm ones stay, to check. Jobs that
  were running when the app closed run again. (Save Queue keeps every job, finished or not.)

Progress comes from what vfx-transcode prints, and from counting the frames oiiotool writes
while it converts a batch of frames (oiiotool prints nothing then). The oiiotool step fills
most of the bar. The ffmpeg encode shows as one step near the end.

Locally, the job runs `vfx-transcode ... -super_preset NAME`. A super-preset changed after
queueing is used as it is when the job starts.

## Send to Deadline

**Send to Deadline...** submits the selected waiting jobs, or all of them, to Thinkbox Deadline.
Each job becomes one Deadline job (CommandLine plugin, one task) in one batch. The super-preset's
options are written out in full in the arguments, so the render nodes only need vfx-transcode.
They don't need this computer's `super_presets.json`.

The dialog asks for:

- batch name, comment, pool, secondary pool, group, priority, machine limit, concurrent tasks
  and department;
- whether to submit suspended;
- **vfx-transcode on the farm**: the vfx-transcode the render nodes run (`executable`, by default
  `C:\_GFX_library\utils\vfx-transcode\vfx-transcode.exe`); the department defaults to `IO`;
- how to submit: the **Deadline Web Service** (URL, and user and password when it needs
  authentication), or, with no URL, `deadlinecommand`.

When the dialog opens, it asks Deadline for its pools and groups (the web service's
`/api/pools` and `/api/groups`, or `deadlinecommand -Pools` / `-Groups`) without holding up the
dialog. If Deadline answers, Pool, Secondary pool and Group become lists of them (typing still
works); if not, they stay text fields and a line says why.

The web service needs no Deadline client on this computer. The job goes to its `/api/jobs` as
JSON, with HTTP basic authentication when `require_authentication` is on. With `verify_ssl`
off, an https server's certificate is not checked.

The starting values come from `vfx-transcode.ini` next to the application:

```
[deadline]
command = C:/Program Files/Thinkbox/Deadline10/bin/deadlinecommand.exe
executable = C:\_GFX_library\utils\vfx-transcode\vfx-transcode.exe
pool = none
group = packnuke
priority = 50
department = IO

[deadline_web_service]
url = http://192.168.100.20:8081
require_authentication = false
verify_ssl = false
user =
password = ${DEADLINE_WEB_PASSWORD}
```

Values can use environment variables (`${NAME}`, `%NAME%`); one that isn't set comes out empty.
The password is best kept in the environment like this. The app never saves it; the dialog
shows it masked, for this submission only. After that, each user's other last values are
remembered. The older key `deadlinecommand` still works for `command`.

If the web service can't be reached, or refuses the login, the app stops after the first job
and says why; the other jobs stay waiting.

Before submitting, the app warns about paths on a local drive (not a mapped network drive),
because render nodes can't reach those. Submitted jobs show **On farm** with their Deadline
job id; **Check farm status** on the Queue tab finds out when they are done. Retry puts them
back in the local queue.

## Notes

- The colour settings aren't part of a super-preset: the presets file's default is used (it
  suits ACEScg EXRs). A super-preset with a Colour area would be a natural next step.
- `-super_preset` names can't contain commas: vfx-transcode reads a comma as "several
  super-presets".
