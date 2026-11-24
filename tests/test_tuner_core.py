"""Tests for the Qt-free part of the tuner."""

import json
import unittest
from pathlib import Path

from vfx_transcode import cli
from vfx_transcode.presets import load_presets
from vfx_tuner import core

REPO = Path(__file__).resolve().parent.parent
PRESETS = load_presets(REPO / "presets.json")


def parse(args):
    """The tuner's options must be valid vfx-transcode options."""
    ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + [str(a) for a in args])
    return cli.config_from_args(ns, PRESETS)


class TunerCoreTests(unittest.TestCase):
    def test_burn_roundtrip(self):
        state = core.burn_from_preset(PRESETS.burnins["dailies"])
        self.assertEqual(core.burn_args(state, PRESETS), ["-burn_preset", "dailies"])   # unchanged
        state.places["tl"].text = "{namesplit1}"
        state.places["wm"].text = "DRAFT"
        args = core.burn_args(state, PRESETS)
        self.assertEqual(args[:2], ["-burn_preset", "none"])
        cfg = parse(args)
        self.assertEqual(cfg.burn["tl"], "{namesplit1}")
        self.assertEqual(cfg.burn["bc"], "TC {tc}")
        self.assertTrue(cfg.burn_styles["bc"].box)                    # dailies' own bc style kept
        self.assertEqual(cfg.burn_styles["bc"].size, "2.2%")
        self.assertEqual((cfg.burn_styles["wm"].size, cfg.burn_styles["wm"].opacity), ("8%", 0.25))
        json.dumps(core.burn_json(state))

    def test_burn_margins_and_empty(self):
        state = core.BurnState()
        self.assertEqual(core.burn_args(state), ["-burn_preset", "none"])
        state.places["br"].text = "{current_frame}"
        state.margins["bottom"] = "40"
        cfg = parse(core.burn_args(state))
        self.assertEqual(cfg.burn_margins["bottom"], "40")

    def test_slate(self):
        state = core.slate_from_spec(PRESETS.slates["generic"], "generic")
        self.assertEqual(core.slate_args(state, PRESETS), ["-slate", "generic"])
        state.fields.append(("Notes", "comp v2"))
        state.scale, state.rule, state.hide_empty = 0.5, True, False
        cfg = parse(core.slate_args(state, PRESETS))
        sl = cfg.slate
        self.assertEqual(list(sl.fields)[-1], "Notes")
        self.assertEqual(list(sl.fields)[:2], ["Date", "Artist"])
        self.assertEqual((sl.scale, sl.rule, sl.hide_empty), (0.5, True, False))
        self.assertIsNone(parse(core.slate_args(core.SlateState(enabled=False))).slate)

    def test_transform(self):
        t = core.load_transform(PRESETS, "mygreatproject_editorial", "239", 1920)
        self.assertEqual(t.cli_args("239"), ["-oiio_transform", "mygreatproject_editorial"])
        self.assertEqual(t.cli_args("other")[0], "-oiio_transform_args")   # not the active one
        t.args.append("--resize 1000x0")
        args = t.cli_args("239")
        self.assertEqual(args[0], "-oiio_transform_args")
        cfg = parse(t.preview_args())
        self.assertEqual(cfg.oiio_transform_explicit[-2:], ["--resize", "1000x0"])
        self.assertIn("--cut", cfg.oiio_transform_explicit)
        self.assertEqual(core.TransformState().preview_args(), [])
        self.assertEqual(core.transform_json(PRESETS, t)["conditions"][0]["right"], "4080")

    def test_active_sub(self):
        keywords = {"input_width": "4080", "input_height": "2160", "input_par": "1"}
        self.assertEqual(core.active_sub_name(PRESETS, "mygreatproject_editorial", keywords, {}), "239")
        keywords["input_width"] = "3840"
        self.assertEqual(core.active_sub_name(PRESETS, "mygreatproject_editorial", keywords, {}), "uhd")
        self.assertIsNone(core.active_sub_name(PRESETS, "", keywords, {}))

    def test_letterbox_and_metadata(self):
        lb = core.LetterboxState(enabled=True, ratio="1.85", color="#102030", opacity=0.5,
                                 line=2, line_color="#ff0000")
        cfg = parse(core.letterbox_args(lb))
        self.assertEqual(cfg.letterbox.fill, (16, 32, 48, 128))
        self.assertEqual(cfg.letterbox.line_color, (255, 0, 0, 255))
        m = core.MetadataState(preset="review", tags=[("comment", "tuned {clean_name}")],
                               reelid_source="exr")
        cfg = parse(core.metadata_args(m))
        self.assertEqual((cfg.metadata_preset, cfg.metadata["comment"], cfg.reelid_source),
                         ("review", "tuned {clean_name}", "exr"))

    def test_preview_and_output(self):
        state = core.TunerState()
        state.input.input1 = "D:/shots/sh010.1001.exr"
        state.input.colorin = "ACEScg"
        state.burn.places["tl"].text = "{clean_name}"
        cmd = core.preview_command(state, "C:/tmp/p.png", tool=["vfx-transcode"])
        self.assertEqual(cmd[-2:], ["-preview", "C:/tmp/p.png"])
        ns = cli.build_parser(PRESETS).parse_args(cmd[1:])
        cfg = cli.config_from_args(ns, PRESETS)
        self.assertEqual(cfg.preview_frame, "middle")
        full, sections = core.output_sections(state, PRESETS)
        self.assertIn("-burn_tl {clean_name}", full)
        self.assertEqual(set(sections), {"transform", "transform_sequence", "burnin", "slate",
                                         "metadata", "filmstrip"})
        self.assertIn("-width 0", full)

    def test_metadata_rows(self):
        report = {"frame": 1005, "input": {"header": {"owner": "A001", "FramesPerSecond": "24/1"}},
                  "output": {"width": 1920, "metadata": {"title": "x"}, "letterbox": {"kind": "letterbox", "bar": 10}},
                  "keywords": {"input_width": "4096"}}
        rows = core.metadata_rows(report)
        self.assertIn(("Input header", "owner", "A001"), rows)
        self.assertIn(("Output metadata", "title", "x"), rows)
        self.assertIn(("Keywords", "{input_width}", "4096"), rows)


    def test_filter_rows(self):
        rows = [("Input header", "owner", "A001"), ("Input header", "cameraModel", "DJI"),
                ("Output", "width", "1920")]
        self.assertEqual(core.filter_rows(rows, ""), rows)
        self.assertEqual(core.filter_rows(rows, "CAMERA"), [rows[1]])          # key, any case
        self.assertEqual(core.filter_rows(rows, "a00"), [rows[0]])             # value
        self.assertEqual(core.filter_rows(rows, "output"), [rows[2]])          # section name
        self.assertEqual(core.filter_rows(rows, "nothing"), [])


if __name__ == "__main__":
    unittest.main()


class TunerFilmstripTests(unittest.TestCase):
    def test_args_roundtrip(self):
        state = core.filmstrip_from_preset(PRESETS.filmstrips["grid6x4"], "grid6x4")
        self.assertEqual(core.filmstrip_args(state, PRESETS), ["-filmstrip_preset", "grid6x4"])
        state.quality, state.slate, state.source = 70, True, "movie"
        cfg = parse(["-of"] + core.filmstrip_args(state, PRESETS))
        f = cfg.filmstrip
        self.assertEqual((f.layout(), f.quality, f.slate, f.source), ((24, 6, 4), 70, True, "movie"))
        row = core.FilmstripState(tiles=12)
        self.assertEqual(parse(["-of"] + core.filmstrip_args(row)).filmstrip.layout(), (12, 12, 1))
        rows = core.FilmstripState(tiles=10, rows=2)
        self.assertEqual(parse(["-of"] + core.filmstrip_args(rows)).filmstrip.layout(), (10, 5, 2))
        json.dumps(core.filmstrip_json(state))

    def test_commands(self):
        state = core.TunerState()
        state.input.input1 = "x.1001.exr"
        cmd = core.filmstrip_command(state, "p_filmstrip.jpg", "p_movie.mp4", tool=["vt"])
        self.assertIn("-of", cmd)
        self.assertNotIn("-o", cmd)                                   # source tiles: no movie
        parse(cmd[3:])
        state.filmstrip.source = "movie"
        cmd = core.filmstrip_command(state, "p_filmstrip.jpg", "p_movie.mp4", tool=["vt"])
        self.assertEqual(cmd[cmd.index("-o") + 1], "p_movie.mp4")
        self.assertTrue(parse(cmd[3:]).movie)
        state.filmstrip = core.FilmstripState(slate=True)
        self.assertIn("-o", core.filmstrip_command(state, "f.jpg", "m.mp4", tool=["vt"]))

    def test_output_sections(self):
        state = core.TunerState()
        state.input.input1 = "x.1001.exr"
        full, sections = core.output_sections(state, PRESETS)
        self.assertNotIn("-of", full)
        self.assertIn("-of", sections["filmstrip"][0])
        state.filmstrip.enabled = True
        full, _sections = core.output_sections(state, PRESETS)
        self.assertIn("-of -filmstrip_tiles 24", full)


class TunerInputTests(unittest.TestCase):
    def test_describe_sequence_and_still(self):
        import tempfile
        folder = Path(tempfile.mkdtemp())
        for f in (1001, 1002, 1004):
            (folder / "sh010_v001.{}.png".format(f)).write_bytes(b"x")
        (folder / "single.png").write_bytes(b"x")
        info = core.describe_input(str(folder / "sh010_v001.1002.png"), PRESETS)
        self.assertEqual((info.kind, info.first, info.last, info.frames), ("sequence", 1001, 1004, (1001, 1002, 1004)))
        self.assertIn("sh010_v001.####.png", info.text)
        self.assertIn("missing 1003", info.text)
        self.assertEqual(core.describe_input(str(folder / "single.png"), PRESETS).kind, "still")
        self.assertTrue(core.describe_input(str(folder / "nope.png")).error)
        self.assertEqual(core.describe_input("").kind, "")

    def test_fps_and_movie_args(self):
        state = core.InputState(input1="a.1001.exr", fps="23.976")
        self.assertEqual(state.args()[:4], ["-i", "a.1001.exr", "-r", "23.976"])
        state.par = "2"
        cfg = parse(state.args()[2:])
        self.assertEqual(cfg.par_arg, 2)
        self.assertEqual(core.detect_par("")[0], None)
        from fractions import Fraction
        self.assertEqual(core.par_text(Fraction(4, 3)), "4/3")
        self.assertEqual(core.par_text(Fraction(9, 5)), "1.8")
        self.assertEqual(parse(["-par", core.par_text(Fraction(4, 3))]).par_arg, Fraction(4, 3))
        self.assertNotIn("-movie_first_frame", core.InputState(input1="m.mov").args())
        self.assertIn("-movie_first_frame", core.InputState(input1="m.mov", movie_first_frame=1).args())
        self.assertNotIn("-movie_first_frame", core.InputState(input1="a.exr", movie_first_frame=1).args())

    def test_frame_timecode(self):
        from fractions import Fraction
        self.assertEqual(core.frame_timecode(1001, 1001, "01:00:10:00", Fraction(24000, 1001)), "01:00:10:00")
        self.assertEqual(core.frame_timecode(1025, 1001, "01:00:10:00", "24"), "01:00:11:00")
        self.assertEqual(core.frame_timecode(1001, 1001, "", "24"), "00:00:41:17")   # frame counter
        self.assertEqual(core.frame_timecode(1001, 1001, "", None), "")


class TunerSuperPresetTests(unittest.TestCase):
    def _state(self):
        state = core.TunerState()
        state.input.input1 = "x.1001.exr"
        state.input.encode_preset = "h264"
        state.burn = core.burn_from_preset(PRESETS.burnins["dailies"])
        state.burn.places["tl"].text = "{clean_name} {tc}"
        state.burn.margins["left"] = "3%"
        state.slate = core.slate_from_spec(PRESETS.slates["generic"], "generic")
        state.slate.fields = [("Artist", "{user}"), ("Notes", "x")]
        state.letterbox = core.LetterboxState(enabled=True, ratio="2.39", line=2)
        state.metadata = core.MetadataState(tags=[("comment", "hi")], timecode_source="exr")
        state.filmstrip = core.FilmstripState(enabled=True, tiles=12, source="movie", slate=True)
        state.transform = core.TransformState(use=True, args=["--resize 1280x0"])
        state.ff_transform = core.FFTransformState("scale=w=1280:h=-2\n# x\nsetsar=1")
        state.oiio_letterbox = core.LetterboxState(enabled=True, ratio="1.85")
        return state

    def test_roundtrip(self):
        state = self._state()
        entry = core.super_preset_entry(state, core.SUPER_AREAS, "notes")
        json.loads(json.dumps(entry))                                   # plain JSON
        other = core.TunerState()
        loaded = core.apply_super_preset(other, json.loads(json.dumps(entry)))
        self.assertEqual(loaded, list(core.SUPER_AREAS))
        for name in ("transform", "burn", "slate", "filmstrip", "metadata", "letterbox",
                     "ff_transform", "oiio_letterbox"):
            self.assertEqual(getattr(other, name), getattr(state, name), name)
        self.assertEqual(other.input.encode_preset, "h264")
        self.assertEqual(other.input.input1, "")                        # the input is not part of it

    def test_some_areas(self):
        state = self._state()
        entry = core.super_preset_entry(state, ["burnin", "transform"])
        self.assertEqual(list(entry["areas"]), ["transform", "burnin"])
        other = core.TunerState()
        self.assertEqual(core.apply_super_preset(other, entry, ["transform", "slate"]), ["transform"])
        self.assertEqual(other.letterbox, state.letterbox)
        self.assertEqual(other.ff_transform, state.ff_transform)
        self.assertEqual(other.burn, core.TunerState().burn)

    def test_args_parse(self):
        state = self._state()
        entry = core.super_preset_entry(state, core.SUPER_AREAS)
        args = []
        for area in core.SUPER_AREAS:
            args += entry["areas"][area]["args"]
        cfg = parse(args)
        self.assertEqual(cfg.preset.name, "h264")
        self.assertEqual(cfg.burn["tl"], "{clean_name} {tc}")
        self.assertIsNotNone(cfg.slate)
        self.assertEqual(cfg.filmstrip.layout()[0], 12)
        self.assertEqual(cfg.filmstrip.source, "movie")
        off = parse(core.area_args(core.TunerState(), "transform") + core.area_args(core.TunerState(), "transform_sequence"))
        self.assertIsNone(off.letterbox)
        self.assertIsNone(off.oiio_letterbox)
        self.assertEqual(cfg.ff_transform, "scale=w=1280:h=-2,setsar=1")
        self.assertIsNotNone(cfg.oiio_letterbox)
        loaded = core.load_transform(PRESETS, *self._first_transform())
        state.transform = loaded
        self.assertEqual(core.area_args(state, "transform_sequence")[2:4], ["-oiio_transform", loaded.preset])

    def _first_transform(self):
        name = next(iter(PRESETS.transforms))
        return name, PRESETS.transforms[name].subs[0].name

    def test_process_command(self):
        state = self._state()
        cmd = core.process_command(state, PRESETS, "D:/out/", tool=["vt"])
        self.assertEqual(cmd[0], "vt")
        self.assertEqual(cmd[cmd.index("-o") + 1], "D:/out/")
        self.assertIn("-of", cmd)
        self.assertTrue(parse(cmd[3:]).movie)


class TunerPipelineTests(unittest.TestCase):
    def test_matrix(self):
        P = core.pipeline
        # (oiiotool, ffmpeg) by input and output, as specified
        cases = [("a.mov", "movie", (False, True)), ("a.mov", "jpg", (False, True)),
                 ("a.mov", "exr", (True, False)), ("a.1001.exr", "movie", (True, True)),
                 ("a.1001.png", "movie", (False, True)), ("a.1001.exr", "exr", (True, False)),
                 ("a.1001.exr", "png", (True, False)), ("a.1001.png", "jpg", (True, False)),
                 ("a.1001.png", "exr", (True, False))]
        for path, out, (oiio, ffmpeg) in cases:
            p = P(path, out)
            self.assertEqual((p.oiio, p.ffmpeg), (oiio, ffmpeg), (path, out))
        self.assertTrue(P("a.mov", "movie").movie)
        self.assertFalse(P("a.mov", "jpg").movie)

    def test_output_arg(self):
        self.assertEqual(core.output_arg("./", "movie"), "./")
        self.assertEqual(core.output_arg("./", "exr"), "./{clean_name}.####.exr")
        self.assertEqual(core.output_arg("D:/out/", "png"), "D:/out/{clean_name}.####.png")
        self.assertEqual(core.output_arg("D:/out/sh010", "jpg"), "D:/out/sh010.####.jpg")
        self.assertEqual(core.output_arg("D:/out/sh010.%05d", "jpg"), "D:/out/sh010.%05d.jpg")
        self.assertEqual(core.output_arg("D:/out/a.####.exr", "exr"), "D:/out/a.####.exr")

    def test_commands_follow_pipeline(self):
        state = core.TunerState()
        state.input.input1 = "a.1001.png"
        state.input.output_type = "exr"
        state.burn.places["tl"].text = "x"
        state.letterbox.enabled = True
        state.oiio_letterbox.enabled = True
        state.ff_transform.text = "scale=w=100:h=-2"
        state.input.trim_in, state.input.trim_out = 1005, 1010
        cmd = core.preview_command(state, "p.png", tool=["vt"])
        self.assertNotIn("-burn_tl", cmd)
        self.assertNotIn("-ff_transform", cmd)
        self.assertNotIn("-letterbox", cmd)
        self.assertIn("-oiio_letterbox", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], "preview.####.exr")
        cfg = parse(cmd[3:])
        self.assertEqual((cfg.trim_in, cfg.trim_out, cfg.seq_out_ext, cfg.width), (1005, 1010, ".exr", 0))
        state.input.output_type = "movie"
        cmd = core.process_command(state, PRESETS, "./", tool=["vt"])
        self.assertIn("-ff_transform", cmd)
        self.assertIn("-letterbox", cmd)
        self.assertNotIn("-oiio_letterbox", cmd)              # png to a movie: no oiiotool
        state.burn.enabled = False
        self.assertEqual(core.burn_args(state.burn, PRESETS), ["-burn_preset", "none"])

    def test_legacy_super_preset(self):
        old = {"areas": {"letterbox": {"state": {"enabled": True, "ratio": "1.85"}, "args": []},
                         "transform": {"state": {"use": True, "args": ["--resize 10x0"], "width": 1920},
                                       "args": []}}}
        state = core.TunerState()
        loaded = core.apply_super_preset(state, old)
        self.assertEqual(sorted(loaded), ["transform", "transform_sequence"])
        self.assertEqual(state.letterbox.ratio, "1.85")
        self.assertEqual(state.transform.args, ["--resize 10x0"])


class TunerSequencePresetTests(unittest.TestCase):
    def test_lists_and_args(self):
        names, default = core.sequence_presets_for(PRESETS, "exr")
        self.assertIn("exr_piz_half", names)
        self.assertNotIn("jpg_high", names)
        self.assertEqual(default, "exr_zip16_half")
        self.assertEqual(core.sequence_presets_for(PRESETS, "movie"), ([], ""))
        state = core.TunerState()
        state.input.input1 = "a.1001.exr"
        state.input.output_type = "exr"
        state.input.seq_preset = "exr_dwab60_half"
        cmd = core.process_command(state, PRESETS, "./", tool=["vt"])
        self.assertEqual(cmd[cmd.index("-seq_preset") + 1], "exr_dwab60_half")
        self.assertEqual(parse(cmd[3:]).seq_args[-1], "dwab:60")
        state.input.output_type = "movie"
        self.assertNotIn("-seq_preset", core.process_command(state, PRESETS, "./", tool=["vt"]))
        entry = core.super_preset_entry(state, ["encode"])
        other = core.TunerState()
        core.apply_super_preset(other, entry)
        self.assertEqual(other.input.seq_preset, "exr_dwab60_half")


class TunerDropTests(unittest.TestCase):
    def _folder(self, *names):
        import tempfile
        folder = Path(tempfile.mkdtemp())
        for name in names:
            (folder / name).write_bytes(b"x")
        return folder

    def test_is_media(self):
        folder = self._folder("a.1001.exr", "b.mov", "notes.txt", "c.PNG")
        self.assertTrue(core.is_media(folder / "a.1001.exr"))
        self.assertTrue(core.is_media(folder / "b.mov"))
        self.assertTrue(core.is_media(folder / "c.PNG"))
        self.assertFalse(core.is_media(folder / "notes.txt"))
        self.assertFalse(core.is_media(folder))
        self.assertFalse(core.is_media(folder / "missing.exr"))

    def test_folder(self):
        one_seq = self._folder("sh010.1001.exr", "sh010.1002.exr", "sh010.1003.exr", "readme.txt")
        found, what = core.media_in_folder(one_seq)
        self.assertEqual(found.name, "sh010.1001.exr")
        self.assertIn("sequence", what)
        self.assertEqual(core.media_in_folder(self._folder("cut.mov", "x.json"))[0].name, "cut.mov")
        self.assertEqual(core.media_in_folder(self._folder("0001.png", "0002.png"))[0].name, "0001.png")
        self.assertEqual(core.media_in_folder(self._folder("plate.exr"))[0].name, "plate.exr")
        two = self._folder("a.1001.exr", "a.1002.exr", "b.1001.exr")
        found, what = core.media_in_folder(two)
        self.assertIsNone(found)
        self.assertIn("2 media items", what)
        self.assertIsNone(core.media_in_folder(self._folder("a.1001.exr", "cut.mov"))[0])
        self.assertIsNone(core.media_in_folder(self._folder("notes.txt"))[0])
        self.assertIsNone(core.media_in_folder(self._folder("a.1001.exr", "a.1001.png"))[0])
        self.assertIn("folder", core.describe_input(str(one_seq)).error)


class TunerColourTests(unittest.TestCase):
    def _state(self, path, out):
        state = core.TunerState()
        state.input.input1, state.input.output_type = path, out
        return state

    def test_defaults(self):
        i = core.InputState()
        self.assertEqual((i.colorout, i.ocio), ("Output - Rec.709", "./ocio/aces_1.2cg/config.ocio"))
        self.assertEqual(i.colour_args(), ["-co", "Output - Rec.709", "-ocio", "./ocio/aces_1.2cg/config.ocio"])

    def test_when_used(self):
        cases = [("a.1001.exr", "movie", (True, True)), ("a.1001.exr", "jpg", (True, True)),
                 ("a.1001.exr", "exr", (False, False)), ("a.1001.png", "exr", (False, False)),
                 ("a.mov", "exr", (False, False)), ("a.mov", "movie", (False, False)),
                 ("a.1001.png", "movie", (False, False))]
        for path, out, expected in cases:
            self.assertEqual(core.colour_use(self._state(path, out)), expected, (path, out))
        state = self._state("a.mov", "exr")
        state.input.convert_colour = True
        self.assertEqual(core.colour_use(state), (True, False))
        state = self._state("a.mov", "movie")
        state.input.convert_colour = True
        self.assertEqual(core.colour_use(state), (False, False))     # ffmpeg only: no OCIO

    def test_commands(self):
        state = self._state("a.1001.exr", "movie")
        cmd = core.preview_command(state, "p.png", tool=["vt"])
        self.assertEqual(cmd[cmd.index("-co") + 1], "Output - Rec.709")
        cfg = parse(cmd[3:])
        self.assertEqual(cfg.colorout, "Output - Rec.709")
        state = self._state("a.1001.exr", "exr")
        cfg = parse(core.preview_command(state, "p.png", tool=["vt"])[3:])
        self.assertFalse(cfg.colour_given)                           # EXR to EXR: no conversion
        state.input.convert_colour = True
        state.input.colorin = "ACES - ACEScg"
        state.input.colorout = "ACES - ACES2065-1"
        cmd = core.process_command(state, PRESETS, "./", tool=["vt"])
        cfg = parse(cmd[3:])
        self.assertTrue(cfg.colour_given)
        self.assertEqual((cfg.colorin, cfg.colorout), ("ACES - ACEScg", "ACES - ACES2065-1"))
        self.assertNotIn("-co", core.preview_command(self._state("a.mov", "movie"), "p.png", tool=["vt"]))


class TunerOcioScanTests(unittest.TestCase):
    SAMPLE = """OpenColorIO 2.3.1
Color config: ocio://studio-config-v1.0.0_aces-v1.3_ocio-v2.1
Known color spaces: 
    - "ACES2065-1" (linear)
      aliases: "aces2065_1", "ACES - ACES2065-1"
    - "ACEScg" (linear)
    - "Old space" (inactive)
    - "sRGB - Texture"
    - Raw
Known roles:
    - aces_interchange -> ACES2065-1
    - scene_linear -> ACEScg
Known looks:
    - "ACES 1.3 Reference Gamut Compression"
Known displays: (* indicates default)
    - "sRGB - Display" (*)
      views: "ACES 1.0 - SDR Video" (*), "Un-tone-mapped", Raw
    - "Rec.1886 Rec.709 - Display"
      views: "ACES 1.0 - SDR Video", "ACES 1.0 - SDR Video (D60 sim on D65)" (*),
      "Un-tone-mapped", Raw
Named transforms:
    - "Utility - Curve - sRGB"
"""

    def test_colorconfiginfo(self):
        info = core.parse_colorconfiginfo(self.SAMPLE)
        self.assertEqual(info.colorspaces, ["ACES2065-1", "ACEScg", "sRGB - Texture", "Raw"])
        self.assertEqual(info.roles["scene_linear"], "ACEScg")
        self.assertEqual(info.looks, ["ACES 1.3 Reference Gamut Compression"])
        self.assertEqual(list(info.displays), ["sRGB - Display", "Rec.1886 Rec.709 - Display"])
        self.assertEqual(info.displays["Rec.1886 Rec.709 - Display"],
                         ["ACES 1.0 - SDR Video", "ACES 1.0 - SDR Video (D60 sim on D65)",
                          "Un-tone-mapped", "Raw"])
        self.assertEqual(info.default_display, "sRGB - Display")
        self.assertEqual(info.default_views["Rec.1886 Rec.709 - Display"], "ACES 1.0 - SDR Video (D60 sim on D65)")
        self.assertEqual(len(info.views()), 4)                 # all views, once each

    def test_ocio_file(self):
        text = """ocio_profile_version: 2
roles:
  scene_linear: ACEScg
inactive_colorspaces: [Old]
active_views: [Film, Raw]
displays:
  sRGB:
    - !<View> {name: Raw, colorspace: Raw}
    - !<View> {name: Film, view_transform: x, display_colorspace: y}
  Rec709:
    - !<View> {name: Film, colorspace: z}
looks:
  - !<Look>
    name: Warm
colorspaces:
  - !<ColorSpace>
    name: ACEScg
    family: ACES
  - !<ColorSpace>
    name: "Output - Rec.709"
  - !<ColorSpace>
    name: Old
  - !<ColorSpace> {name: Raw, isdata: true}
"""
        info = core.parse_ocio_file(text)
        self.assertEqual(info.colorspaces, ["ACEScg", "Output - Rec.709", "Raw"])
        self.assertEqual(info.looks, ["Warm"])
        self.assertEqual(info.displays, {"sRGB": ["Film", "Raw"], "Rec709": ["Film"]})
        self.assertEqual(info.roles["scene_linear"], "ACEScg")
        self.assertEqual(info.default_display, "sRGB")

    def test_scan(self):
        self.assertIn("not found", core.scan_ocio("/nowhere/config.ocio").error)
        import tempfile
        folder = Path(tempfile.mkdtemp())
        (folder / "config.ocio").write_text("roles:\n  default: raw\ncolorspaces:\n  - !<ColorSpace>\n    name: raw\n")
        info = core.scan_ocio(str(folder / "config.ocio"))     # no oiiotool here: read the file
        self.assertEqual(info.colorspaces, ["raw"])


class AlphaTests(unittest.TestCase):
    def test_cli_auto_resolves_by_output(self):
        self.assertEqual(parse(["-alpha", "auto", "-preset", "prores4444"]).alpha, "keep")
        cfg = parse(["-alpha", "auto"])                      # h264: no alpha, no error
        self.assertEqual((cfg.alpha, cfg.alpha_auto), ("drop", True))
        self.assertEqual(parse(["-alpha", "auto", "-o", "out/a.####.png"]).alpha, "keep")
        self.assertEqual(parse(["-alpha", "auto", "-o", "out/a.####.exr"]).alpha, "keep")
        self.assertEqual(parse(["-alpha", "auto", "-o", "out/a.####.jpg"]).alpha, "drop")
        self.assertEqual(parse(["-alpha", "keep", "-o", "out/a.####.tif"]).alpha, "keep")
        with self.assertRaises(cli.TranscodeError):
            parse(["-alpha", "keep", "-o", "out/a.####.jpg"])
        with self.assertRaises(cli.TranscodeError):
            parse(["-alpha", "keep"])
        self.assertFalse(parse(["-alpha", "auto", "-alpha_premult", "1"]).alpha_premult)

    def test_ffmpeg_image_alpha(self):
        from vfx_transcode import outseq
        self.assertIn("rgba", outseq.ffmpeg_output_args(".png", alpha=True))
        self.assertIn("rgba64be", outseq.ffmpeg_output_args(".png", True, True))
        self.assertIn("rgba64le", outseq.ffmpeg_output_args(".tif", True, True))
        self.assertIn("rgb24", outseq.ffmpeg_output_args(".png"))

    def test_tuner_alpha(self):
        state = core.TunerState()
        state.input.input1 = "x.1001.exr"
        self.assertNotIn("-alpha", core.pipeline_args(state))
        self.assertEqual(core.alpha_note(state, PRESETS), "")
        state.input.alpha = "auto"
        args = core.pipeline_args(state)
        self.assertEqual(args[args.index("-alpha") + 1], "auto")
        parse(args)
        self.assertIn("cannot store alpha", core.alpha_note(state, PRESETS))
        state.input.encode_preset = "prores4444"
        self.assertIn("Kept", core.alpha_note(state, PRESETS))
        state.input.output_type = "jpg"
        self.assertIn("JPG cannot", core.alpha_note(state, PRESETS))
        state.input.output_type = "png"
        self.assertIn("Kept", core.alpha_note(state, PRESETS))

    def test_super_preset_alpha(self):
        state = core.TunerState()
        state.input.alpha = "auto"
        entry = core.super_preset_entry(state, ["encode"])
        self.assertIn("-alpha", entry["areas"]["encode"]["args"])
        other = core.TunerState()
        core.apply_super_preset(other, json.loads(json.dumps(entry)))
        self.assertEqual(other.input.alpha, "auto")
        core.apply_super_preset(other, {"areas": {"encode": {"state": {"encode_preset": ""}}}})
        self.assertEqual(other.input.alpha, "drop")


class TimecodeAutoTests(unittest.TestCase):
    def test_auto_timecode(self):
        A = core.auto_timecode
        self.assertEqual(A("sequence", True, "01:00:10:00"), ("metadata", None))
        self.assertEqual(A("sequence", True, None), ("counter", None))
        self.assertEqual(A("still", True, None), ("counter", None))
        self.assertEqual(A("sequence", False, None), ("counter", None))
        self.assertEqual(A("movie", False, "01:00:10:00"), ("metadata", None))
        self.assertEqual(A("movie", False, None), ("arg", "01:00:00:00"))

    def test_timecode_args(self):
        m = core.MetadataState(timecode_source="metadata", timecode="01:00:00:00", reelid_source="auto")
        args = core.timecode_args(m)
        self.assertEqual(args, ["-timecode_source", "metadata", "-reelid_source", "auto"])
        cfg = parse(args)
        self.assertEqual((cfg.timecode_source, cfg.reelid_source), ("exr", "auto"))
        m.timecode_source = "arg"
        cfg = parse(core.timecode_args(m))
        self.assertEqual((cfg.timecode_source, cfg.timecode), ("arg", "01:00:00:00"))
        m.timecode_source = "exr"                            # the old name still loads
        self.assertIn("metadata", core.timecode_args(m))
        self.assertEqual(parse(["-reelid_source", "metadata"]).reelid_source, "exr")
        self.assertEqual(parse(["-timecode_source", "exr"]).timecode_source, "exr")

    def test_timecode_args_without_movie(self):
        state = core.TunerState()
        state.input.input1 = "a.mov"
        state.input.output_type = "jpg"                      # ffmpeg writes it: burn-in timecode
        state.metadata.timecode_source = "counter"
        self.assertIn("-timecode_source", core.pipeline_args(state))
        state.input.output_type = "exr"                      # oiiotool: for {timecode} in tags
        self.assertIn("-timecode_source", core.pipeline_args(state))

    def test_presets_metadata_name(self):
        self.assertEqual(PRESETS.metadata_presets["conform"].timecode_source, "exr")
        self.assertEqual(PRESETS.metadata_presets["conform"].reelid_source, "exr")


class MovieWriteTests(unittest.TestCase):
    def test_write_switches(self):
        state = core.TunerState()
        state.input.input1 = "x.1001.exr"
        args = core.pipeline_args(state)
        self.assertNotIn("-output_timecode", args)           # on: the default, not sent
        state.input.write_timecode = False
        state.input.write_reelid = False
        args = core.pipeline_args(state)
        cfg = parse(args)
        self.assertEqual((cfg.output_timecode, cfg.output_reelid), (False, False))
        state.input.output_type = "exr"                      # oiiotool sequence: sent too
        self.assertIn("-output_reelid", core.pipeline_args(state))
        self.assertEqual(core.write_support(state)[:2], (True, True))
        state.input.output_type = "dpx"
        self.assertEqual(core.write_support(state)[:2], (True, False))
        state.input.output_type = "png"
        self.assertEqual(core.write_support(state)[:2], (False, False))
        state.input.input1 = "a.mov"                         # ffmpeg writes the JPGs: not sent
        state.input.output_type = "jpg"
        self.assertNotIn("-output_reelid", core.pipeline_args(state))
        self.assertEqual(core.write_support(state)[:2], (False, False))
        state.input.output_type = "png"
        entry = json.loads(json.dumps(core.super_preset_entry(state, ["encode"])))
        self.assertIn("-output_reelid", entry["areas"]["encode"]["args"])
        other = core.TunerState()
        core.apply_super_preset(other, entry)
        self.assertEqual((other.input.write_timecode, other.input.write_reelid), (False, False))

    def test_reel_not_written(self):
        from vfx_transcode import ffmpeg
        cfg = parse(["-output_reelid", "0", "-reelid", "R001"])
        cmd = ffmpeg.build_command(cfg, "list.txt", "out.mov", "null", "01:00:00:00", {}, "R001")
        self.assertNotIn("reel_name=R001", " ".join(cmd))
        cfg = parse(["-reelid", "R001"])
        cmd = ffmpeg.build_command(cfg, "list.txt", "out.mov", "null", "01:00:00:00", {}, "R001")
        self.assertIn("reel_name=R001", " ".join(cmd))


class FoundTextTests(unittest.TestCase):
    def test_found_text(self):
        state = core.TunerState()
        state.metadata.reelid_source = "auto"
        state.metadata.timecode_source = "metadata"
        self.assertEqual(core.found_text({}, state), "")
        report = {"output": {"timecode": "01:00:10:00", "timecode_source": "movie",
                             "reel_name": "A001C003", "reel_source": "EXR header owner"}}
        text = core.found_text(report, state)
        self.assertIn("Timecode: metadata → 01:00:10:00 (movie)", text)
        self.assertIn("Reel name: auto → A001C003 (EXR header owner)", text)
        report["output"].update(reel_name="", reel_source="none found")
        self.assertIn("auto → none", core.found_text(report, state))
        report["output"].update(reel_source="")
        self.assertIn("not used", core.found_text(report, state))
        state.metadata.reelid_source = "off"
        self.assertIn("Reel name: off", core.found_text(report, state))


class SequenceMetadataTests(unittest.TestCase):
    def test_oiio_args(self):
        from vfx_transcode import metadata as md
        self.assertEqual(md.oiio_args({}), [])
        self.assertEqual(md.oiio_args({"a": "x {y}"}),
                         ["--evaloff", "--attrib:type=string", "a", "x {y}", "--evalon"])

    def test_tuner_sends_tags_for_oiio_sequences(self):
        state = core.TunerState()
        state.input.input1 = "x.1001.exr"
        state.input.output_type = "exr"
        state.metadata.tags = [("vfx:shot", "{clean_name}")]
        args = core.pipeline_args(state)
        self.assertIn("vfx:shot={clean_name}", args)
        self.assertFalse(parse(args).seq_metadata is False)
        state.input.input1 = "a.mov"
        state.input.output_type = "jpg"                      # ffmpeg writes it: no tags
        self.assertNotIn("vfx:shot={clean_name}", core.pipeline_args(state))

    def test_reel_found_none(self):
        state = core.TunerState()
        state.metadata.reelid_source = "auto"
        none = {"output": {"reel_name": "", "reel_source": "none found"}}
        found = {"output": {"reel_name": "A001", "reel_source": "-reelid"}}
        self.assertTrue(core.reel_found_none(none, state))
        self.assertFalse(core.reel_found_none(found, state))
        self.assertIsNone(core.reel_found_none({}, state))
        self.assertIsNone(core.reel_found_none({"output": {"reel_source": ""}}, state))
        state.metadata.reelid_source = "metadata"            # only auto
        self.assertIsNone(core.reel_found_none(none, state))


class SequenceStampTests(unittest.TestCase):
    def test_sequence_timecode_and_reel(self):
        cfg = parse(["-o", "out/a.####.exr"])
        cfg.fps = 24
        seq = type("S", (), {"start": 1001, "is_exr": False})()
        per_frame, fixed = cli.sequence_timecode(cfg, seq, "01:00:00:00")
        self.assertEqual(fixed, [])
        self.assertEqual(per_frame(1001)[-1], "01:00:00:00")
        self.assertEqual(per_frame(1025)[-1], "01:00:01:00")
        self.assertEqual(per_frame(1001)[:2], ["--attrib:type=timecode", "smpte:TimeCode"])
        self.assertIn("reelName", cli.sequence_reel(cfg, "R001"))
        self.assertEqual(cli.sequence_reel(cfg, ""), [])
        cfg = parse(["-o", "out/a.####.exr", "-output_timecode", "0", "-output_reelid", "0"])
        self.assertEqual(cli.sequence_timecode(cfg, seq, "01:00:00:00"), (None, ["--eraseattrib", "smpte:TimeCode"]))
        self.assertEqual(cli.sequence_reel(cfg, "R001"), ["--eraseattrib", "reelName"])
        cfg = parse(["-o", "out/a.####.png"])
        self.assertEqual(cli.sequence_timecode(cfg, seq, "01:00:00:00"), (None, []))
        self.assertEqual(cli.sequence_reel(cfg, "R001"), [])

    def test_exr_input_keeps_its_own_timecode(self):
        cfg = parse(["-o", "out/a.####.exr", "-timecode_source", "metadata"])
        cfg.fps, cfg.header_label, cfg.timecode_found = 24, "EXR header", "EXR header"
        seq = type("S", (), {"start": 1001, "is_exr": True})()
        self.assertEqual(cli.sequence_timecode(cfg, seq, "01:00:00:00"), (None, []))
        cfg.timecode_found = "frame counter"                 # header had none: write one
        self.assertIsNotNone(cli.sequence_timecode(cfg, seq, "00:00:41:17")[0])


class SlateLogoTests(unittest.TestCase):
    def test_positions_and_overlay(self):
        from vfx_transcode import slate
        P = slate.parse_logo_position
        self.assertEqual([P("tr"), P("top-right"), P("Bottom Left"), P("center"), P("bottom")],
                         ["tr", "tr", "bl", "mc", "bc"])
        with self.assertRaises(cli.TranscodeError):
            P("upper")
        spec = slate.SlateSpec(logo="x.png", logo_position="br", logo_x="4%", logo_y="20")
        self.assertEqual(slate.logo_overlay_xy(spec, 2000, 1000), ("W-w-80", "H-h-20"))
        spec = slate.SlateSpec(logo_position="mc", logo_x="0")
        self.assertEqual(slate.logo_overlay_xy(spec, 2000, 1000), ("(W-w)/2", "(H-h)/2"))

    def test_cli_and_tuner(self):
        cfg = parse(["-slate", "plain", "-slate_logo", "/abs/logo.png", "-slate_logo_position",
                     "bottom-left", "-slate_logo_size", "8%", "-slate_logo_x", "0"])
        s = cfg.slate
        self.assertEqual((s.logo, s.logo_position, s.logo_size, s.logo_x), ("/abs/logo.png", "bl", "8%", "0"))
        state = core.SlateState(enabled=True, logo="/abs/logo.png", logo_position="mc", logo_size="15%")
        cfg = parse(core.slate_args(state))
        self.assertEqual((cfg.slate.logo, cfg.slate.logo_position, cfg.slate.logo_size),
                         ("/abs/logo.png", "mc", "15%"))
        self.assertNotIn("-slate_logo", core.slate_args(core.SlateState(enabled=True)))
        self.assertEqual(core.slate_json(state)["logo_position"], "mc")

    def test_preset_logo_beside_presets_file(self):
        import tempfile, shutil, os
        from vfx_transcode.presets import preset_relative_path
        folder = tempfile.mkdtemp()
        try:
            open(os.path.join(folder, "logo.png"), "wb").close()
            got = preset_relative_path("./logo.png", os.path.join(folder, "presets.json"))
            self.assertEqual(Path(got), Path(folder) / "logo.png")
        finally:
            shutil.rmtree(folder)


class OverlayTests(unittest.TestCase):
    def test_overlay_lines(self):
        self.assertEqual(core.overlay_lines(None), [])
        report = {"keywords": {"input_width": "3840", "input_height": "2160"},
                  "input": {"par": "1.3"},
                  "output": {"width": 1920, "height": 1080, "par": 1.0, "fps": "24"}}
        self.assertEqual(core.overlay_lines(report),
                         [("input", "3840x2160 pa 1.3 @ 24fps"), ("output", "1920x1080 pa 1.0")])
        report["input"]["par"] = "1"
        self.assertEqual(core.overlay_lines(report),
                         [("input", "3840x2160 @ 24fps"), ("output", "1920x1080")])
        report["input"]["par"] = "4/3 (1.33)"
        report["output"]["par"] = 1.3333333
        self.assertEqual(core.overlay_lines(report)[1], ("output", "1920x1080 pa 1.33"))

    def test_overlay_input_size_fallback(self):
        report = {"keywords": {"input_width": ""}, "input": {"par": "1.3"},
                  "output": {"width": 1920, "height": 1080, "par": 1.3, "fps": "25"}}
        self.assertEqual(core.overlay_lines(report, (1920, 1080))[0], ("input", "1920x1080 pa 1.3 @ 25fps"))


class CompareTests(unittest.TestCase):
    def test_compare_command(self):
        state = core.TunerState()
        state.input.input1 = "shot.1001.exr"
        state.input.fps = "25"
        cmd = core.compare_command(state, 1010, "cmp.png", ["vt"])
        self.assertEqual(cmd[:3], ["vt", "-i", "shot.1001.exr"])
        for part in (["-thumbnail_frame", "1010"], ["-oiio_transform", "none"], ["-burn_preset", "none"],
                     ["-slate", "none"], ["-width", "0"], ["-r", "25"], ["-preview", "cmp.png"]):
            self.assertIn(part, [cmd[i:i + len(part)] for i in range(len(cmd))])
        self.assertIn("-co", cmd)                            # EXR: the colour conversion
        state.input.compare_source = False
        state.input.input2 = "edit.mov"
        self.assertEqual(core.compare_input(state), "edit.mov")
        cmd = core.compare_command(state, 1010, "cmp.png", ["vt"])
        self.assertEqual(cmd[2], "edit.mov")
        self.assertNotIn("-r", cmd)                          # its own rate
        self.assertTrue(core.compare_is_plain_image("ref.png", "still"))
        self.assertFalse(core.compare_is_plain_image("plate.1001.png", "sequence"))
        self.assertFalse(core.compare_is_plain_image("still.exr", "still"))
