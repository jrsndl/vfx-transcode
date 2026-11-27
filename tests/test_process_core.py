import json
import os
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path

from vfx_process import core
from vfx_transcode import super_presets as sp
from vfx_transcode.errors import TranscodeError
from vfx_transcode.presets import load_presets

REPO = Path(__file__).resolve().parent.parent
PRESETS = load_presets(str(REPO / "presets.json"))


def touch(folder, *names):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")


def entry(encode=None, **areas):
    out = OrderedDict([("notes", ""), ("areas", OrderedDict())])
    out["areas"]["encode"] = {"state": encode or {}, "args": []}
    for name, state in areas.items():
        out["areas"][name] = {"state": state, "args": []}
    return out


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        touch(self.root / "sh010" / "comp" / "v001", *["sh010_comp_v001.{:04d}.exr".format(f) for f in range(1001, 1011)])
        touch(self.root / "sh010" / "comp" / "v002", *["sh010_comp_v002.{:04d}.exr".format(f) for f in (1001, 1002, 1005)])
        touch(self.root / "sh010" / "plate", *["plate.{:04d}.dpx".format(f) for f in range(1, 4)])
        touch(self.root / "sh010" / "plate", "notes.txt", "ref.jpg")
        touch(self.root / "edit", "cut_v003.mov")
        touch(self.root / "_trash", *["junk.{:04d}.exr".format(f) for f in range(5)])

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, filt):
        return sorted(i.pattern for i in core.scan_folder(self.root, filt))

    def test_default_finds_exr_sequences_not_in_skipped_folders(self):
        self.assertEqual(self.names(core.ScanFilter()),
                         ["sh010_comp_v001.####.exr", "sh010_comp_v002.####.exr"])

    def test_kinds_and_stills(self):
        filt = core.ScanFilter(kinds=["image", "movie"], min_frames=1)
        self.assertEqual(self.names(filt), ["cut_v003.mov", "plate.####.dpx", "ref.jpg"])
        filt.min_frames = 2
        self.assertEqual(self.names(filt), ["cut_v003.mov", "plate.####.dpx"])

    def test_include_exclude_and_paths(self):
        self.assertEqual(self.names(core.ScanFilter(include="*v002*")), ["sh010_comp_v002.####.exr"])
        self.assertEqual(self.names(core.ScanFilter(exclude="*V001*")), ["sh010_comp_v002.####.exr"])
        self.assertEqual(self.names(core.ScanFilter(include="*/v001/*")), ["sh010_comp_v001.####.exr"])

    def test_latest_only_across_version_folders(self):
        items = core.scan_folder(self.root, core.ScanFilter(latest_only=True))
        self.assertEqual([i.pattern for i in items], ["sh010_comp_v002.####.exr"])
        self.assertEqual(items[0].missing, "1003-1004")
        self.assertEqual((items[0].first, items[0].last, items[0].count), (1001, 1005, 3))
        self.assertEqual(items[0].name, "sh010_comp_v002")

    def test_depth(self):
        filt = core.ScanFilter(kinds=["exr", "movie"], max_depth=1)
        self.assertEqual(self.names(filt), ["cut_v003.mov"])

    def test_patterns(self):
        found = sorted(i.pattern for i in core.scan_patterns(self.root, "*.mov *.dpx"))
        self.assertEqual(found, ["cut_v003.mov", "plate.####.dpx"])
        found = sorted(i.pattern for i in core.scan_patterns(self.root, ""))   # default *.exr *.mov *.mp4
        self.assertEqual(found, ["cut_v003.mov", "junk.####.exr", "sh010_comp_v001.####.exr",
                                 "sh010_comp_v002.####.exr"])                  # _trash is searched
        found = sorted(i.pattern for i in core.scan_patterns(self.root, "*.exr, !*v001* !junk*"))
        self.assertEqual(found, ["sh010_comp_v002.####.exr"])

    def test_listing_is_filtered_again_without_a_scan(self):
        counts = []
        listing = core.walk_media(self.root, "*.exr", progress=lambda n, f: counts.append((n, f)))
        self.assertEqual(counts[-1], (24, 3))                 # 24 files looked at, 3 EXR sequences
        self.assertEqual(sorted(i.pattern for i in core.filter_listing(listing, "*.dpx *.jpg")),
                         ["plate.####.dpx", "ref.jpg"])

    def test_patterns_for_preset(self):
        self.assertEqual(core.patterns_for_preset({"input_type": "jpg"}, "*.exr !*tech*"), "*.jpg *.jpeg !*tech*")
        self.assertEqual(core.patterns_for_preset({"inputs": ["exr"]}), "*.exr")
        self.assertIsNone(core.patterns_for_preset({"areas": {}}))
        d = core.destination_of({"destination": {"path": "./review/"}})
        self.assertEqual((d.path, d.name, d.thumb_path), ("./review/", "", ""))

    def test_base_folder_and_path_keywords(self):
        items = core.scan_patterns(self.root, "*.mov")
        self.assertEqual(items[0].base_folder, str(self.root))
        spec = core.destination_spec("{base_folder}/review/{sourcefolder-1}", "{clean_name}")
        jobs, _problems = core.make_jobs(items, "s", entry({}), spec, PRESETS)
        self.assertEqual(jobs[0].predicted, os.path.join(str(self.root), "review", "edit", "cut_v003.mp4"))
        args = jobs[0].args()
        self.assertEqual(args[args.index("-base_folder") + 1], str(self.root))

    def test_custom_destination(self):
        items = core.scan_patterns(self.root, "*.mov")
        spec = core.destination_spec("{custom_destination_path}/{sourcefolder-1}", "{custom_destination_name}_{clean_name}")
        self.assertEqual(len(core.missing_custom(spec, ("", ""))), 2)
        self.assertEqual(core.missing_custom(spec, ("X", "Y")), [])
        out = os.path.join(str(self.root), "dl")
        jobs, _p = core.make_jobs(items, "s", entry({}), spec, PRESETS, custom=(out, "client"))
        self.assertEqual(jobs[0].predicted, os.path.join(out, "edit", "client_cut_v003.mp4"))
        args = jobs[0].args()
        self.assertEqual(args[args.index("-custom_destination_path") + 1], out)
        self.assertEqual(args[args.index("-custom_destination_name") + 1], "client")
        self.assertIn("-custom_destination_name", jobs[0].args(inline=True))      # Deadline too

    def test_describe_file(self):
        item = core.describe_file(self.root / "sh010" / "comp" / "v001" / "sh010_comp_v001.1005.exr")
        self.assertEqual((item.kind, item.first, item.last, item.count), ("exr", 1001, 1010, 10))
        self.assertTrue(item.path.endswith("sh010_comp_v001.1001.exr"))
        self.assertEqual(core.describe_file(self.root / "edit" / "cut_v003.mov").kind, "movie")


class OutputTests(unittest.TestCase):
    item = core.MediaItem(path=os.path.join("D:", os.sep, "sh", "sh010.1001.exr"), kind="exr",
                          name="sh010", pattern="sh010.####.exr", first=1001, last=1010, count=10)

    def test_templates(self):
        self.assertEqual(core.output_arg(core.OutputSpec(), "movie"), "./")
        self.assertEqual(core.output_arg(core.OutputSpec(mode="subfolder", subfolder="../review"), "movie"),
                         "./../review/")
        self.assertEqual(core.output_arg(core.OutputSpec(mode="folder", folder="X:/out/"), "jpg"),
                         "X:/out/{clean_name}.####.jpg")
        self.assertEqual(core.output_arg(core.OutputSpec(name="{clean_name}_review"), "movie"),
                         "./{clean_name}_review")
        self.assertIn("folder", core.OutputSpec(mode="folder").check())

    def test_destination_fields(self):
        self.assertEqual(core.output_arg(core.destination_spec("", ""), "movie"), "./")
        self.assertEqual(core.output_arg(core.destination_spec("X:/out", "{clean_name}_r"), "movie"),
                         "X:/out/{clean_name}_r")
        self.assertEqual(core.output_arg(core.destination_spec("./review/", ""), "exr"),
                         "./review/{clean_name}.####.exr")

    def test_predict_and_overwrite(self):
        folder = self.item.folder
        p = core.predict_output(self.item, core.OutputSpec(), "movie", "mp4")
        self.assertEqual(p, os.path.join(folder, "sh010.mp4"))
        p = core.predict_output(self.item, core.OutputSpec(), "exr", "exr")
        self.assertEqual(p, os.path.join(folder, "sh010.####.exr"))
        self.assertTrue(core.overwrites_input(self.item, p))
        p = core.predict_output(self.item, core.OutputSpec(mode="subfolder"), "exr", "exr")
        self.assertFalse(core.overwrites_input(self.item, p))


class CheckTests(unittest.TestCase):
    exr = core.MediaItem(path="a.1001.exr", kind="exr", name="a", pattern="a.####.exr", count=10)
    mov = core.MediaItem(path="a.mov", kind="movie", name="a", pattern="a.mov")
    still = core.MediaItem(path="a.png", kind="image", name="a", pattern="a.png", count=1)

    def test_output_from_encode(self):
        self.assertEqual(core.output_of(entry({"encode_preset": "prores422hq"}), PRESETS)[:2], ("movie", "mov"))
        self.assertEqual(core.output_of(entry({}), PRESETS)[:2], ("movie", "mp4"))       # the default h264
        self.assertEqual(core.output_of(entry({"seq_preset": "jpg_high"}), PRESETS)[:2], ("jpg", "jpg"))
        self.assertEqual(core.output_of(entry({"output_type": "exr"}), PRESETS)[:2], ("exr", "exr"))
        out = core.output_of(entry({"output_type": "png", "seq_preset": "jpg_high"}), PRESETS)
        self.assertEqual(out[2][0].level, core.ERROR)
        self.assertEqual(core.output_of(entry({"encode_preset": "nope"}), PRESETS)[2][0].level, core.ERROR)

    def test_areas_that_do_not_apply(self):
        e = entry({}, transform_sequence={"steps": {"use": True, "args": ["--resize", "50%"]}},
                  burnin={"enabled": True, "preset": "", "places": {"tl": {"text": "{clean_name}"}}})
        self.assertEqual(core.check_super_preset(e, self.exr, PRESETS).level, core.OK)
        check = core.check_super_preset(e, self.mov, PRESETS)
        self.assertEqual(check.level, core.WARN)
        self.assertIn("Transform Sequence", check.text())
        check = core.check_super_preset(entry({"output_type": "exr"}, burnin=e["areas"]["burnin"]["state"]),
                                        self.exr, PRESETS)
        self.assertIn("burn-ins", check.text())
        self.assertIn("one-frame", core.check_super_preset(entry({}), self.still, PRESETS).text())

    def test_inputs_limit(self):
        e = entry({})
        e["inputs"] = ["exr"]
        self.assertEqual(core.check_super_preset(e, self.mov, PRESETS).level, core.ERROR)
        level, text = core.check_summary(e, [self.exr, self.mov], PRESETS)
        self.assertEqual(level, core.ERROR)
        self.assertIn("1 of 2", text)

    def test_make_jobs(self):
        items = [self.exr, core.MediaItem(path="b/a.1001.exr", kind="exr", name="a",
                                          pattern="a.####.exr", count=3)]
        spec = core.OutputSpec(mode="folder", folder="X:/out")
        jobs, problems = core.make_jobs(items, "s", entry({}), spec, PRESETS)
        self.assertEqual(len(jobs), 1)                        # the second writes the same file
        self.assertIn("same output", problems[0][1].text())
        job = jobs[0]
        self.assertEqual(job.args()[:6], ["-i", "a.1001.exr", "-super_preset", "s", "-o", "X:/out/"])
        self.assertNotIn("-super_preset", job.args(inline=True))


class SuperPresetFileTests(unittest.TestCase):
    def test_inputs_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "super_presets.json"
            e = entry({})
            e["inputs"] = ["exr", "image"]
            e["destination"] = {"path": "./review/", "name": "{clean_name}_r"}
            sp.save(path, {"x": e})
            back = sp.load(path)["x"]
            self.assertEqual(back["inputs"], ["exr", "image"])
            self.assertEqual(dict(back["destination"]), {"path": "./review/", "name": "{clean_name}_r"})


class TunerOutputTypeTests(unittest.TestCase):
    def test_encode_area_keeps_the_output_type(self):
        from vfx_tuner import core as tuner
        state = tuner.TunerState()
        state.input.output_type = "jpg"
        e = tuner.super_preset_entry(state, ["encode"])
        self.assertEqual(core.output_of(e, PRESETS)[:2], ("jpg", "jpg"))
        other = tuner.TunerState()
        tuner.apply_super_preset(other, e)
        self.assertEqual(other.input.output_type, "jpg")
        other.input.output_type = "exr"
        tuner.apply_super_preset(other, {"areas": {"encode": {"state": {}}}})   # an older one
        self.assertEqual(other.input.output_type, "exr")


class TunerDestinationTests(unittest.TestCase):
    def test_saved_with_the_super_preset(self):
        from vfx_tuner import core as tuner
        state = tuner.TunerState()
        state.dest = tuner.DestinationState(path="./review/", name="{clean_name}_r", thumb_path="X:/thumbs/")
        e = tuner.super_preset_entry(state, ["encode"], destination=state.dest)
        d = tuner.destination_of(e)
        self.assertEqual((d.path, d.name, d.thumb_path, d.film_path), ("./review/", "{clean_name}_r", "X:/thumbs/", ""))
        self.assertNotIn("filmstrip", e["destination"])         # as the output: not written
        self.assertIsNone(tuner.destination_of(tuner.super_preset_entry(state, ["encode"])))
        with tempfile.TemporaryDirectory() as tmp:                 # through the file
            path = Path(tmp) / "super_presets.json"
            sp.save(path, {"x": e})
            back = core.destination_of(sp.load(path)["x"])
            self.assertEqual((back.path, back.name, back.thumb_path), ("./review/", "{clean_name}_r", "X:/thumbs/"))

    def test_menus_and_template(self):
        from vfx_tuner import core as tuner
        self.assertEqual(tuner.destination_template("", ""), "./")
        self.assertEqual(tuner.destination_template("X:\\out", "a"), "X:\\out/a")
        paths, names = tuner.path_menu(PRESETS), tuner.name_menu(PRESETS)
        self.assertEqual(paths["Source and name subfolder"], "{sourcepath}/{clean_name}/")
        self.assertEqual(names["Source _transcoded"], "{clean_name}_transcoded")
        self.assertEqual(list(paths)[-2:], [tuner.ONE_FOLDER, tuner.CUSTOM])
        self.assertEqual(tuner.preset_index(paths, "./review/", True), 1)
        self.assertEqual(tuner.preset_index(paths, "X:/out/", True), len(paths) - 1)
        self.assertEqual(tuner.preset_index(names, ""), 0)

    def test_thumbnail_and_filmstrip_paths(self):
        from vfx_tuner import core as tuner
        d = tuner.DestinationState(path="./review/", name="{clean_name}_t")
        self.assertEqual((d.thumbnail(), d.filmstrip()), ("./review/{clean_name}_t", "./review/{clean_name}_t_filmstrip"))
        d.thumb_path, d.film_name = "X:/thumbs/", "{clean_name}_strip"
        self.assertEqual((d.thumbnail(), d.filmstrip()), ("X:/thumbs/{clean_name}_t", "./review/{clean_name}_strip"))
        self.assertEqual(tuner.DestinationState().filmstrip(), "./")      # CLI default name

    def test_process_jobs_put_stills_where_the_preset_says(self):
        from vfx_tuner import core as tuner
        state = tuner.TunerState()
        state.thumb.enabled = True
        state.filmstrip.enabled = True
        state.dest = tuner.DestinationState(thumb_path="X:/thumbs/")
        e = tuner.super_preset_entry(state, ["thumbnail", "filmstrip"], destination=state.dest)
        dest = core.destination_of(e)
        dest.path, dest.name = "./out/", "{clean_name}_v"           # what the user typed
        item = core.MediaItem(path="a.1001.exr", kind="exr", name="a", pattern="a.####.exr", count=3)
        jobs, _p = core.make_jobs([item], "x", e, core.destination_spec(dest.path, dest.name), PRESETS, dest=dest)
        args = jobs[0].args()
        self.assertEqual(args[args.index("-o"):args.index("-o") + 6],
                         ["-o", "./out/{clean_name}_v", "-ot", "X:/thumbs/{clean_name}_v",
                          "-of", "./out/{clean_name}_v_filmstrip"])
        from vfx_transcode.cli import build_parser
        ns = build_parser(PRESETS).parse_args(args)                # the later -ot / -of win
        self.assertEqual((ns.ot, ns.of), ("X:/thumbs/{clean_name}_v", "./out/{clean_name}_v_filmstrip"))

    def test_save_and_delete_presets(self):
        from vfx_transcode import presets_store
        from vfx_transcode.presets import load_presets
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "presets.json"
            shutil.copy(str(REPO / "presets.json"), str(path))
            presets_store.save_preset(path, "destination_path", "Dailies", {"path": "X:/dailies/{date}/"})
            self.assertEqual(load_presets(str(path)).dest_paths["Dailies"].template, "X:/dailies/{date}/")
            presets_store.delete_preset(path, "destination_name", "Name + date")
            names = load_presets(str(path)).dest_names
            self.assertNotIn("Name + date", names)
            self.assertIn("Source _transcoded", names)


class ThumbnailTests(unittest.TestCase):
    def test_command_and_super_preset(self):
        from vfx_tuner import core as tuner
        from vfx_transcode.cli import build_parser
        state = tuner.TunerState()
        state.input.input1 = "a.1001.exr"
        self.assertNotIn("-ot", tuner.full_args(state, PRESETS))
        state.thumb.enabled, state.thumb.preset = True, "start512"
        args = tuner.full_args(state, PRESETS)
        self.assertEqual(args[-4:], ["-ot", "./", "-thumbnail_preset", "start512"])
        ns = build_parser(PRESETS).parse_args(args)          # next to the output
        self.assertEqual((ns.ot, ns.thumbnail_preset, ns.o), ("./", "start512", "./"))
        e = tuner.super_preset_entry(state, ["thumbnail"])
        self.assertEqual(sp.args_for({"x": e}, "x"), ["-ot", "-thumbnail_preset", "start512"])
        other = tuner.TunerState()
        tuner.apply_super_preset(other, e)
        self.assertEqual((other.thumb.enabled, other.thumb.preset), (True, "start512"))
        ns = build_parser(PRESETS).parse_args(sp.args_for({"x": e}, "x") + ["-i", "a.exr", "-o", "./"])
        self.assertEqual((ns.ot, ns.o), ("", "./"))         # super-preset options go first


class ProgressTests(unittest.TestCase):
    def test_lines(self):
        p = core.Progress(100, "exr", "movie")
        p.feed("Input: D:/sh/a.1001.exr  frames 1001-1050 (50)")
        self.assertEqual(p.frames, 50)
        p.feed("Converting 50 file(s) one by one with 8 threads")
        p.feed("[25/50] a.1025.exr 0.20s")
        self.assertAlmostEqual(p.value, 0.85 * 0.5)
        p.feed("Encoding D:/sh/a.mp4")
        self.assertEqual(p.stage, "Encoding (ffmpeg)")
        p.feed("Done: D:/sh/a.mp4")
        self.assertEqual(p.output, "D:/sh/a.mp4")

    def test_poll_counts_new_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            touch(tmp, "old.0001.exr")
            old = time.time() - 100
            os.utime(os.path.join(tmp, "old.0001.exr"), (old, old))
            p = core.Progress(4, "exr", "movie")
            p.feed("Intermediate files: " + tmp)
            p.feed("Converting frames 1-4 with oiiotool --parallel-frames")
            touch(tmp, "a.0001.exr", "a.0002.exr")
            self.assertTrue(p.poll())
            self.assertAlmostEqual(p.value, 0.85 * 0.5)


class DeadlineTests(unittest.TestCase):
    def test_files(self):
        job = core.Job(input="//srv/sh/a.1001.exr", kind="exr", name="a", pattern="a.####.exr",
                       frames=10, super_preset="s", output="./", super_args=["-alpha", "drop"],
                       predicted="//srv/sh/a.mp4")
        settings = core.DeadlineSettings(executable="//srv/tools/vfx-transcode.exe", pool="comp",
                                         priority=60, batch_name="b")
        info, plugin = core.deadline_files(job, settings)
        self.assertIn("Plugin=CommandLine\n", info)
        self.assertIn("Pool=comp\n", info)
        self.assertIn("Priority=60\n", info)
        self.assertNotIn("Group=", info)
        self.assertIn("Executable=//srv/tools/vfx-transcode.exe\n", plugin)
        self.assertIn("-alpha drop", plugin)
        self.assertNotIn("-super_preset", plugin)

    def test_queue_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = core.Job(input="a", kind="exr", name="a", pattern="a", frames=1,
                           super_preset="s", output="./", status=core.RUNNING)
            core.save_queue(Path(tmp) / "q.json", [job])
            back = core.load_queue(Path(tmp) / "q.json")
            self.assertEqual(back[0].status, core.WAITING)
            self.assertEqual(back[0].id, job.id)


class DeadlineWebServiceTests(unittest.TestCase):
    """Submission over HTTP, against a small local server that answers like Deadline's
    /api/jobs."""

    def setUp(self):
        import http.server
        import threading
        received = self.received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, self.headers.get("Authorization"), body))
                if self.headers.get("Authorization") == "Basic bm86bm8=":       # no:no
                    self.send_response(401)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"_id": "5f1d2c3b4a"}).encode())

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:{}".format(self.server.server_port)
        self.job = core.Job(input="//srv/sh/a.1001.exr", kind="exr", name="a", pattern="a.####.exr",
                            frames=10, super_preset="s", output="./", super_args=["-alpha", "drop"])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_submit(self):
        s = core.DeadlineSettings(executable="//srv/vfx-transcode.exe", pool="none", group="packnuke",
                                  web_url=self.url + "/")
        self.assertEqual(core.submit_to_deadline(self.job, s, "unused"), "5f1d2c3b4a")
        path, auth, body = self.received[0]
        self.assertEqual((path, auth), ("/api/jobs", None))
        self.assertEqual((body["JobInfo"]["Pool"], body["JobInfo"]["Group"], body["JobInfo"]["Plugin"]),
                         ("none", "packnuke", "CommandLine"))
        self.assertIn("-alpha drop", body["PluginInfo"]["Arguments"])
        self.assertTrue(body["IdOnly"])

    def test_authentication(self):
        s = core.DeadlineSettings(executable="x", web_url=self.url, web_auth=True, web_user="me",
                                  web_password="pw")
        core.submit_to_deadline(self.job, s, "unused")
        self.assertEqual(self.received[0][1], "Basic bWU6cHc=")                  # me:pw
        s.web_user, s.web_password = "no", "no"
        with self.assertRaises(TranscodeError) as ctx:
            core.submit_to_deadline(self.job, s, "unused")
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_unreachable(self):
        s = core.DeadlineSettings(executable="x", web_url="http://127.0.0.1:1")
        with self.assertRaises(TranscodeError) as ctx:
            core.submit_to_deadline(self.job, s, "unused", timeout=5)
        self.assertIn("can't be reached", str(ctx.exception))

    def test_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "vfx-transcode.ini").write_text(
                "[deadline]\ndeadlinecommand = D:/dl.exe\npool = none\npriority = 60\n"
                "[deadline_web_service]\nurl = http://x:8081/\nrequire_authentication = true\n"
                "verify_ssl = false\nuser = farm\npassword = ${VT_TEST_UNSET_PASSWORD}\n")
            s = core.DeadlineSettings.from_ini(tmp)
            self.assertEqual((s.command, s.pool, s.priority), ("D:/dl.exe", "none", 60))   # old key read
            self.assertEqual((s.web_url, s.web_auth, s.web_verify_ssl, s.web_user, s.web_password),
                             ("http://x:8081", True, False, "farm", ""))


class InterpretationTests(unittest.TestCase):
    def test_args_and_problems(self):
        self.assertEqual(core.Interpretation().args(), [])
        self.assertEqual(core.Interpretation().changed(), 0)
        i = core.Interpretation(fps="23.976", par="2", first_frame="1", timecode="01:00:00:00",
                                reel="{namesplit1}", colorin="Utility - Linear - sRGB", alpha="keep",
                                write_timecode="0", write_reel="1")
        self.assertEqual(i.changed(), 9)
        self.assertEqual(i.problems(), [])
        self.assertEqual(i.args(), ["-r", "23.976", "-par", "2", "-movie_first_frame", "1",
                                    "-timecode", "01:00:00:00", "-ci", "Utility - Linear - sRGB",
                                    "-alpha", "keep", "-output_timecode", "0", "-output_reelid", "1",
                                    "-timecode_source", "arg", "-reelid", "{namesplit1}", "-reelid_source", "arg"])
        bad = core.Interpretation(fps="fast", par="0", first_frame="x", timecode="1:2", alpha="maybe")
        self.assertEqual(len(bad.problems()), 5)

    def test_wins_over_the_super_preset(self):
        from vfx_transcode.cli import build_parser
        e = entry({"alpha": "drop", "write_timecode": True, "write_reelid": True})
        e["areas"]["encode"]["args"] = ["-alpha", "drop", "-output_timecode", "1", "-output_reelid", "1"]
        e["areas"]["metadata"] = {"state": {}, "args": ["-timecode_source", "metadata", "-reelid_source", "auto"]}
        item = core.MediaItem(path="a.1001.exr", kind="exr", name="a", pattern="a.####.exr", count=3)
        i = core.Interpretation(fps="25", alpha="auto", write_timecode="0", reel="R1", timecode="10:00:00:00")
        jobs, _p = core.make_jobs([item], "s", e, core.destination_spec("./", ""), PRESETS, interpret=i)
        ns = build_parser(PRESETS).parse_args(jobs[0].args(inline=True))
        self.assertEqual((ns.fps, ns.alpha, ns.output_timecode, ns.output_reelid, ns.reelid, ns.timecode),
                         ("25", "auto", False, True, "R1", "10:00:00:00"))
        self.assertEqual((ns.reelid_source, ns.timecode_source), ("arg", "arg"))
        from vfx_transcode.cli import timecode_source_from_args
        self.assertEqual(timecode_source_from_args(ns), "arg")     # not the preset's "metadata"


class KeywordPreviewTests(unittest.TestCase):
    def test_queue_paths_have_the_keywords_filled(self):
        import datetime
        item = core.MediaItem(path=os.path.join("D:", os.sep, "shows", "abc", "sh010", "comp", "sh010_comp_v003.1001.exr"),
                              kind="exr", name="sh010_comp_v003", pattern="sh010_comp_v003.####.exr",
                              first=1001, last=1100, count=100)
        spec = core.destination_spec("./review/{date}/", "{namesplit1}_v{version_string}_{frame_start}_{source_fps_int}")
        interpret = core.Interpretation(fps="25")
        jobs, _p = core.make_jobs([item], "s", entry({}), spec, PRESETS, interpret=interpret)
        today = datetime.date.today().isoformat()
        self.assertEqual(jobs[0].predicted, os.path.join(item.folder, "review", today, "sh010_v003_1001_25.mp4"))
        # without -r the rate is known only when the job runs: left as typed until then
        jobs, _p = core.make_jobs([item], "s", entry({}), spec, PRESETS)
        self.assertTrue(jobs[0].predicted.endswith("_1001_{source_fps_int}.mp4"))

    def test_a_keyword_vfx_transcode_does_not_know_is_caught(self):
        item = core.MediaItem(path="a.1001.exr", kind="exr", name="a", pattern="a.####.exr", first=1, last=2, count=2)
        jobs, problems = core.make_jobs([item], "s", entry({}), core.destination_spec("./", "{frames}"), PRESETS)
        self.assertEqual(jobs, [])
        self.assertIn("unknown keyword(s) {frames}", problems[0][1].text())

    def test_presets_name_tokens(self):
        import shutil
        from vfx_transcode.presets import load_presets
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "presets.json"
            data = json.loads((REPO / "presets.json").read_text(encoding="utf-8"))
            data["name_tokens"] = {"shot": "^([a-z]+\\d+)"}
            path.write_text(json.dumps(data), encoding="utf-8")
            presets = load_presets(str(path))
            item = core.MediaItem(path="sh010_comp_v003.1001.exr", kind="exr", name="sh010_comp_v003",
                                  pattern="sh010_comp_v003.####.exr", first=1, last=2, count=2)
            jobs, _p = core.make_jobs([item], "s", entry({}), core.destination_spec("X:/out/", "{shot}"), presets)
            self.assertEqual(jobs[0].predicted, os.path.normpath("X:/out/sh010.mp4"))


class QueueFileTests(unittest.TestCase):
    def job(self, status, **kw):
        return core.Job(input=kw.pop("input", "a.1001.exr"), kind="exr", name="a", pattern="a.####.exr",
                        frames=3, super_preset="s", output="./", status=status, **kw)

    def test_round_trip_keeps_status_and_log(self):
        done = self.job(core.DONE, log=["> vfx-transcode ...", "Done: X:/a.mp4"], result="X:/a.mp4",
                        started=10.0, finished=25.0, message="X:/a.mp4")
        failed = self.job(core.FAILED, log=["Error: no frames"], message="no frames")
        running = self.job(core.RUNNING)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.vtq"
            core.save_queue(path, [done, failed, running], note="for the night shift")
            jobs, header = core.read_queue_file(path)
        self.assertEqual(header["format"], core.QUEUE_FORMAT)
        self.assertEqual(header["note"], "for the night shift")
        self.assertEqual([j.status for j in jobs], [core.DONE, core.FAILED, core.WAITING])
        self.assertEqual((jobs[0].log, jobs[0].result, jobs[0].elapsed()),
                         (done.log, "X:/a.mp4", 15.0))
        self.assertEqual(jobs[1].message, "no frames")

    def test_merge_and_checks(self):
        a, b = self.job(core.WAITING), self.job(core.WAITING, input="/no/such/file.exr")
        b.id = a.id
        merged = core.merge_jobs([a], [b])
        self.assertEqual(len(merged), 2)
        self.assertNotEqual(merged[0].id, merged[1].id)
        self.assertEqual(core.merge_jobs([a], [b], replace=True), [b])
        self.assertEqual(core.unreachable_inputs(merged), merged)        # neither file exists here
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "x.json"
            bad.write_text('{"something": 1}')
            with self.assertRaises(TranscodeError):
                core.read_queue_file(bad)
            old = Path(tmp) / "old.json"                                   # the app's queue before
            old.write_text(json.dumps({"jobs": [a.to_json()]}))
            self.assertEqual(len(core.load_queue(old)), 1)


class IntermediateTests(unittest.TestCase):
    def test_options_reach_vfx_transcode(self):
        from vfx_tuner import core as tuner
        from vfx_transcode.cli import build_parser, config_from_args
        state = tuner.TunerState()
        state.input.input1 = "a.1001.exr"
        self.assertEqual(state.intermediate.args(), [])                 # the defaults: nothing sent
        m = state.intermediate
        m.mezzanine, m.sixteen_bit, m.location, m.temp, m.keep, m.oiio_threads, m.per_file = \
            "tif", True, "source", "_mezz", True, 8, True
        cfg = config_from_args(build_parser(PRESETS).parse_args(tuner.full_args(state, PRESETS)), PRESETS)
        self.assertEqual((cfg.mezzanine_ext, cfg.mezzanine_16bit, cfg.temp_location, cfg.temp, cfg.temp_keep,
                          cfg.oiio_threads, cfg.oiio_per_file),
                         (".tif", True, "source", "_mezz", True, 8, True))
        self.assertIn("16-bit", tuner.IntermediateState(mezzanine="jpg", sixteen_bit=True).note())

    def test_super_preset_area(self):
        from vfx_tuner import core as tuner
        state = tuner.TunerState()
        state.intermediate.mezzanine, state.intermediate.sixteen_bit = "tif", True
        e = tuner.super_preset_entry(state, list(sp.AREAS))
        args = sp.args_for({"x": e}, "x")
        self.assertEqual(args[args.index("-mezzanine") + 1], "tif")
        self.assertEqual(args[args.index("-mezzanine_16bit") + 1], "1")
        other = tuner.TunerState()
        tuner.apply_super_preset(other, e)
        self.assertEqual((other.intermediate.mezzanine, other.intermediate.sixteen_bit), ("tif", True))
        with tempfile.TemporaryDirectory() as tmp:                      # the file accepts the area
            sp.save(Path(tmp) / "s.json", {"x": e})
            self.assertIn("intermediate", sp.load(Path(tmp) / "s.json")["x"]["areas"])


class MultichannelTests(unittest.TestCase):
    INFO = [{"channelnames": ["R", "G", "B", "A", "Z"], "attribs": {"name": "beauty"}},
            {"channelnames": ["R", "G", "B", "A"], "attribs": {"name": "diffuse"}},
            {"channelnames": ["crypto.R", "crypto.G", "crypto.B", "depth.Z"], "attribs": {"name": "utils"}}]

    def test_pick_in_a_part(self):
        from vfx_transcode.channels import pick_channels
        self.assertEqual(pick_channels(self.INFO).subimage, 0)                  # automatic: first fit
        pick = pick_channels(self.INFO, subimage="diffuse")
        self.assertEqual((pick.subimage, pick.without_alpha), (1, ["R", "G", "B"]))
        pick = pick_channels(self.INFO, ["R", "G", "B"], subimage="1")          # same names, part 1
        self.assertEqual(pick.subimage, 1)
        pick = pick_channels(self.INFO, ["depth.Z"])
        self.assertEqual((pick.subimage, pick.without_alpha), (2, ["depth.Z"]))   # mono
        self.assertEqual(pick_channels(self.INFO, use_first=True, subimage="utils").without_alpha,
                         ["crypto.R", "crypto.G", "crypto.B"])
        with self.assertRaises(ValueError):
            pick_channels(self.INFO, subimage="specular")

    def test_tuner_state(self):
        from vfx_tuner import core as tuner
        from vfx_transcode.cli import build_parser, config_from_args
        state = tuner.TunerState()
        state.input.input1 = "a.1001.exr"
        self.assertEqual(state.channels.args(), [])
        state.channels = tuner.ChannelsState(mode="pick", subimage="diffuse", channels="R,G,B,A")
        args = tuner.full_args(state, PRESETS)
        cfg = config_from_args(build_parser(PRESETS).parse_args(args), PRESETS)
        self.assertEqual((cfg.oiio_channel, cfg.oiio_subimage), (["R", "G", "B", "A"], "diffuse"))
        self.assertIn("-oiio_channel", tuner.preview_command(state, "p.png"))   # the preview too
        self.assertIn("part 1 diffuse", tuner.describe_pick(self.INFO, state.channels, PRESETS))
        state.channels = tuner.ChannelsState(mode="pick", channels="depth.Z")
        self.assertIn("grey", tuner.describe_pick(self.INFO, state.channels, PRESETS))
        e = tuner.super_preset_entry(state, ["channels"])
        other = tuner.TunerState()
        tuner.apply_super_preset(other, e)
        self.assertEqual((other.channels.mode, other.channels.channels), ("pick", "depth.Z"))
        # a super-preset in auto mode clears what an earlier one set
        ns = build_parser(PRESETS).parse_args(["-oiio_channel", "X", "-oiio_subimage", "2"]
                                               + tuner.ChannelsState().full_args() + ["-i", "a.exr"])
        self.assertEqual((ns.oiio_channel, ns.oiio_subimage, ns.oiio_channel_use_first), ("", "", False))


class ChannelOrderTests(unittest.TestCase):
    INFO = MultichannelTests.INFO

    def test_cli(self):
        from vfx_transcode.cli import build_parser, config_from_args
        cfg = config_from_args(build_parser(PRESETS).parse_args(
            ["-i", "a.exr", "-channel_order", "crypto.R,crypto.G,crypto.B; R,G,B"]), PRESETS)
        self.assertEqual(cfg.channel_preferences, [["crypto.R", "crypto.G", "crypto.B"], ["R", "G", "B"]])
        from vfx_transcode.channels import pick_channels
        self.assertEqual(pick_channels(self.INFO, preferences=cfg.channel_preferences).subimage, 2)
        cfg = config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr"]), PRESETS)
        self.assertEqual(cfg.channel_preferences, PRESETS.channel_preferences)   # the file's
        with self.assertRaises(Exception):
            config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr", "-channel_preferences", "nope"]), PRESETS)

    def test_presets_save_delete_and_use(self):
        import shutil
        from vfx_transcode import presets_store
        from vfx_transcode.presets import load_presets
        from vfx_transcode.cli import build_parser, config_from_args
        from vfx_tuner import core as tuner
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "presets.json"
            shutil.copy(str(REPO / "presets.json"), str(path))
            state = tuner.TunerState()
            state.channels.order = ["diffuse.R,diffuse.G,diffuse.B", "R,G,B"]
            entry = tuner.preset_entry(state, "channel_order", PRESETS, "for lighting")
            presets_store.save_preset(path, "channel_order", "lighting", entry)
            presets = load_presets(str(path))
            self.assertEqual(presets.channel_orders["lighting"].as_lists(),
                             [["diffuse.R", "diffuse.G", "diffuse.B"], ["R", "G", "B"]])
            self.assertEqual(presets.channel_orders["lighting"].notes, "for lighting")
            cfg = config_from_args(build_parser(presets).parse_args(["-i", "a.exr", "-channel_preferences", "lighting"]), presets)
            self.assertEqual(cfg.channel_preferences[0], ["diffuse.R", "diffuse.G", "diffuse.B"])
            presets_store.delete_preset(path, "channel_order", "lighting")
            self.assertNotIn("lighting", load_presets(str(path)).channel_orders)

    def test_tuner_args_and_super_preset(self):
        from vfx_tuner import core as tuner
        from vfx_transcode.cli import build_parser, config_from_args
        c = tuner.ChannelsState(order=["crypto.R,crypto.G,crypto.B", "R,G,B"])
        self.assertEqual(c.args(), ["-channel_order", "crypto.R,crypto.G,crypto.B; R,G,B"])
        c.order_preset, c.order_loaded = "lighting", list(c.order)          # loaded, unchanged
        self.assertEqual(c.args(), ["-channel_preferences", "lighting"])
        self.assertIn("part 2 utils", tuner.describe_pick(self.INFO, c, PRESETS))
        state = tuner.TunerState()
        state.channels = c
        e = tuner.super_preset_entry(state, ["channels"])
        args = sp.args_for({"x": e}, "x")
        self.assertEqual(args[args.index("-channel_order") + 1], "crypto.R,crypto.G,crypto.B; R,G,B")
        cfg = config_from_args(build_parser(PRESETS).parse_args(args + ["-i", "a.exr"]), PRESETS)
        self.assertEqual(cfg.channel_preferences[0], ["crypto.R", "crypto.G", "crypto.B"])   # no file needed
        other = tuner.TunerState()
        tuner.apply_super_preset(other, e)
        self.assertEqual(other.channels.order, c.order)


class OverlayTests(unittest.TestCase):
    def test_output_rate_shown_only_when_it_differs(self):
        from vfx_tuner import core as tuner
        report = {"keywords": {"input_width": 3840, "input_height": 2160}, "input": {"par": 1},
                  "output": {"width": 1920, "height": 1080, "par": 1, "fps": "25", "source_fps": "24"}}
        self.assertEqual(tuner.overlay_lines(report),
                         [("input", "3840x2160 @ 24fps"), ("output", "1920x1080 @ 25fps (4.2% faster)")])
        report["output"].update(fps="23.976", source_fps="25")
        self.assertIn("slower", tuner.overlay_lines(report)[1][1])
        report["output"].update(fps="25", source_fps="25")
        self.assertEqual(tuner.overlay_lines(report)[1], ("output", "1920x1080"))


class InfoRowsTests(unittest.TestCase):
    def test_hide_empty(self):
        from vfx_tuner import core as tuner
        rows = [("Input", "a", "1"), ("Input", "b", ""), ("Keywords", "{c}", "  "), ("Output", "d", "0")]
        self.assertEqual(tuner.non_empty_rows(rows), [("Input", "a", "1"), ("Output", "d", "0")])


if __name__ == "__main__":
    unittest.main()


class DeadlineListsTests(unittest.TestCase):
    def test_from_web_service(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"/api/pools": '["none","cpu","gpu"]', "/api/groups": '["none","packnuke"]'}.get(self.path)
                self.send_response(200 if body else 404)
                self.end_headers()
                self.wfile.write((body or "").encode())

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            s = core.DeadlineSettings(web_url="http://127.0.0.1:{}".format(server.server_port))
            self.assertEqual(core.deadline_lists(s), (["none", "cpu", "gpu"], ["none", "packnuke"]))
        finally:
            server.shutdown()

    def test_from_deadlinecommand(self):
        import stat
        import tempfile
        folder = Path(tempfile.mkdtemp())
        script = folder / "deadlinecommand"
        script.write_text('#!/bin/sh\nif [ "$1" = "-Pools" ]; then printf "none\\ncpu\\n"; '
                          'else printf "packnuke\\n"; fi\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        if os.name == "nt":
            self.skipTest("shell script")
        s = core.DeadlineSettings(command=str(script))
        self.assertEqual(core.deadline_lists(s), (["none", "cpu"], ["packnuke"]))

    def test_unreachable(self):
        s = core.DeadlineSettings(web_url="http://127.0.0.1:9")
        with self.assertRaises(TranscodeError):
            core.deadline_lists(s, timeout=2)

    def test_farm_defaults(self):
        s = core.DeadlineSettings()
        self.assertEqual((s.executable, s.department), (core.FARM_EXECUTABLE, "IO"))


class FarmCheckTests(unittest.TestCase):
    def _server(self, stat):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = "[]" if stat is None else json.dumps([{"_id": "abc", "Stat": stat}])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return core.DeadlineSettings(web_url="http://127.0.0.1:{}".format(server.server_port))

    def _job(self, exists):
        folder = Path(tempfile.mkdtemp())
        out = folder / "sh010.mov"
        if exists:
            out.write_bytes(b"x")
        return core.Job(input="/x/a.0001.exr", kind="exr", name="sh010", pattern="a.####.exr", frames=1,
                        super_preset="x", output=str(out), predicted=str(out), status=core.SUBMITTED,
                        deadline_id="abc")

    def test_completed_with_output(self):
        status, _m = core.farm_check(self._job(True), self._server(3))
        self.assertEqual(status, core.FARM_DONE)
        self.assertEqual(core.STATUS_TITLES[status], "Done on farm")
        self.assertEqual(core.STATUS_TITLES[core.SUBMITTED], "On farm")

    def test_completed_without_output(self):
        status, message = core.farm_check(self._job(False), self._server(3))
        self.assertEqual(status, core.FAILED)
        self.assertIn("not there", message)

    def test_failed_active_missing(self):
        self.assertEqual(core.farm_check(self._job(True), self._server(4))[0], core.FAILED)
        status, message = core.farm_check(self._job(True), self._server(1))
        self.assertEqual((status, message), (core.SUBMITTED, "on Deadline: active"))
        self.assertIn("not found", core.farm_check(self._job(True), self._server(None))[1])

    def test_deadlinecommand_getjob(self):
        import stat
        if os.name == "nt":
            self.skipTest("shell script")
        folder = Path(tempfile.mkdtemp())
        script = folder / "deadlinecommand"
        script.write_text('#!/bin/sh\nprintf "ID=abc\\nName=x\\nStatus=Completed\\n"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        s = core.DeadlineSettings(command=str(script))
        self.assertEqual(core.deadline_job_status(s, "abc"), "completed")


class FilesFilterTests(unittest.TestCase):
    LISTING = [("/proj/shots/sh010/comp", ["sh010_beauty.0001.exr", "sh010_beauty.0002.exr",
                                           "sh010_tech.0001.exr"]),
               ("/proj/shots/old", ["sh005_beauty.0001.exr"]),
               ("/proj/shots/sh020", ["sh020.mov"])]

    def names(self, patterns, leave_out=""):
        return sorted(i.pattern for i in core.filter_listing(self.LISTING, patterns, "/proj/shots", leave_out))

    def test_plain_word_matches_in_name(self):
        self.assertEqual(self.names("beauty"), ["sh005_beauty.####.exr", "sh010_beauty.####.exr"])
        self.assertEqual(self.names("*.exr !tech"), ["sh005_beauty.####.exr", "sh010_beauty.####.exr"])

    def test_leave_out_whole_path(self):
        self.assertEqual(self.names("*.exr *.mov", "*/old/*"),
                         ["sh010_beauty.####.exr", "sh010_tech.####.exr", "sh020.mov"])
        self.assertEqual(self.names("*.exr *.mov", "OLD sh020"),
                         ["sh010_beauty.####.exr", "sh010_tech.####.exr"])
        self.assertTrue(core.path_excluded("D:\\\\proj\\\\old\\\\a.exr", ["*/old/*"]))


class ThumbnailColourTests(unittest.TestCase):
    def test_colour_args(self):
        args = ["-preset", "h264", "-ci", "ACEScg", "-co", "Output - Rec.709", "-view", "Film",
                "-burn_preset", "none", "-looks", ""]
        self.assertEqual(core.colour_args(args), ["-ci", "ACEScg", "-co", "Output - Rec.709",
                                                  "-view", "Film", "-looks", ""])
        self.assertEqual(core.colour_args([]), [])
        entry = {"areas": {"colour": {"state": {}, "args": ["-ci", "ACEScg", "-co", "sRGB"]},
                           "encode": {"state": {}, "args": ["-preset", "h264"]}}}
        self.assertEqual(core.preset_colour_args(entry), ["-ci", "ACEScg", "-co", "sRGB"])
        self.assertEqual(core.preset_colour_args(None), [])


class PartialScanTests(unittest.TestCase):
    def test_partial_listing_grows(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        for sub in ("a", "b", "c"):
            (root / sub).mkdir()
            (root / sub / "x.0001.exr").write_bytes(b"")
        seen = []
        listing = core.walk_media(root, "*.exr", partial=lambda l: seen.append(len(l)))
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(len(listing), 3)


class ParallelScanTests(unittest.TestCase):
    def tree(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        for rel in ("B/sh020", "a/sh010", "a/sh010/render", ".git/objects", "_old"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        for rel in ("B/sh020/x.0001.exr", "B/sh020/x.0002.exr", "a/sh010/clip.MOV",
                    "a/sh010/render/b.0001.exr", "a/sh010/notes.txt", ".git/objects/y.exr",
                    "_old/z.png", "top.exr"):
            (root / rel).write_bytes(b"")
        return root

    def test_same_as_os_walk_in_folder_order(self):
        root = self.tree()
        expected = []
        for folder, dirs, files in os.walk(str(root)):
            dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=str.lower)
            names = sorted(f for f in files if core.kind_of(f))
            if names:
                expected.append((folder, names))
        for workers in (1, 4):
            self.assertEqual(core.walk_media(root, workers=workers), expected)
        rels = [os.path.relpath(f, str(root)) for f, _ in expected]
        self.assertEqual(rels[0], ".")
        self.assertNotIn(".git", " ".join(rels))
        self.assertIn("_old", rels)

    def test_progress_counts_files_and_found(self):
        seen = []
        core.walk_media(self.tree(), "*.exr", progress=lambda s, f: seen.append((s, f)))
        self.assertEqual(seen[-1], (7, 3))     # 7 files outside .git; x.####, b.#### and top

    def test_cancel_and_missing_root(self):
        self.assertEqual(core.walk_media(self.tree(), cancelled=lambda: True), [])
        with self.assertRaises(OSError):
            core.walk_media("/no/such/folder/here")

    def test_kind_of(self):
        self.assertEqual([core.kind_of(n) for n in ("a.EXR", "b.mov", "c.dpx", "d.nk", "e")],
                         ["exr", "movie", "image", "", ""])


class NamesAndTests(unittest.TestCase):
    LISTING = [("/p/a", ["sh010_beauty_v003.0001.exr", "sh010_beauty_v002.0001.exr",
                         "sh010_depth_v003.0001.exr", "sh020_beauty_v003.mov", "notes_v003.txt"])]

    def names(self, patterns):
        return sorted(i.pattern for i in core.filter_listing(self.LISTING, patterns, "/p"))

    def test_words_and_together(self):
        self.assertEqual(self.names("*.exr beauty v003"), ["sh010_beauty_v003.####.exr"])
        self.assertEqual(self.names("beauty v003"), ["sh010_beauty_v003.####.exr", "sh020_beauty_v003.mov"])

    def test_file_types_are_alternatives(self):
        self.assertEqual(self.names("*.exr *.mov v003"),
                         ["sh010_beauty_v003.####.exr", "sh010_depth_v003.####.exr", "sh020_beauty_v003.mov"])
        self.assertEqual(self.names("*.exr *.mov sh020"), ["sh020_beauty_v003.mov"])
        self.assertEqual(self.names("*.exr !depth v003"), ["sh010_beauty_v003.####.exr"])
