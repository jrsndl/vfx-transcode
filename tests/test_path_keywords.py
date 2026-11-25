import os
import unittest

from vfx_transcode import names
from vfx_transcode.cli import build_parser, check_output_template, config_from_args, source_path
from vfx_transcode.names import path_tokens
from vfx_transcode.presets import load_presets
from vfx_transcode.sequence import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRESETS = load_presets(str(REPO / "presets.json"))


def folders(t, n=5):
    return ([t["sourcefolder{}".format(i)] for i in range(1, n + 1)],
            [t["sourcefolder-{}".format(i)] for i in range(1, n + 1)])


class PathTokenTests(unittest.TestCase):
    def test_drive_letter(self):
        t = path_tokens(r"D:\shots\sh010\comp\sh010.####.exr")
        self.assertEqual(folders(t), (["D:", "shots", "sh010", "comp", ""], ["comp", "sh010", "shots", "D:", ""]))
        self.assertEqual([t["sourcepath"], t["sourcepath-1"], t["sourcepath-2"], t["sourcepath-3"],
                          t["sourcepath-4"]],
                         [r"D:\shots\sh010\comp", r"D:\shots\sh010", r"D:\shots", "D:\\", ""])
        self.assertEqual((t["base_folder"], t["base_folder-1"]), (r"D:\shots\sh010\comp", r"D:\shots\sh010"))

    def test_unc(self):
        t = path_tokens(r"\\srv\proj\shots\sh010\a.mov", r"\\srv\proj\shots")
        self.assertEqual(folders(t)[0], ["\\\\srv", "proj", "shots", "sh010", ""])
        self.assertEqual(folders(t)[1][:4], ["sh010", "shots", "proj", "\\\\srv"])
        self.assertEqual([t["sourcepath"], t["sourcepath-1"], t["sourcepath-2"], t["sourcepath-3"]],
                         [r"\\srv\proj\shots\sh010", r"\\srv\proj\shots", r"\\srv\proj", ""])
        self.assertEqual([t["base_folder"], t["base_folder-1"], t["base_folder-2"]],
                         [r"\\srv\proj\shots", r"\\srv\proj", ""])

    def test_posix(self):
        t = path_tokens("/mnt/proj/sh010/a.####.exr", "/mnt/proj/")
        self.assertEqual(folders(t, 4), (["mnt", "proj", "sh010", ""], ["sh010", "proj", "mnt", ""]))
        self.assertEqual([t["sourcepath"], t["sourcepath-2"], t["sourcepath-3"], t["sourcepath-4"]],
                         ["/mnt/proj/sh010", "/mnt", "/", ""])
        self.assertEqual([t["base_folder"], t["base_folder-1"]], ["/mnt/proj", "/mnt"])

    def test_every_keyword_has_a_value(self):
        t = path_tokens(r"D:\a.mov")
        self.assertEqual(set(t), set(names.PATH_KEYWORDS))
        self.assertEqual(t["sourcefolder20"], "")


class CliTests(unittest.TestCase):
    def test_output_template_and_option(self):
        check_output_template("{sourcepath-2}/review/{sourcefolder-1}_{namesplit1}")
        check_output_template("{base_folder}/../review/{base_folder-1}")
        with self.assertRaises(Exception):
            check_output_template("{sourcefolder21}")
        ns = build_parser(PRESETS).parse_args(["-i", "a.exr", "-base_folder", "shots"])
        self.assertEqual(config_from_args(ns, PRESETS).base_folder, os.path.abspath("shots"))

    def test_source_path_of_a_sequence(self):
        seq = Sequence(Path("/mnt/sh"), "a.", ".exr", 4, (1001, 1002))
        cfg = type("C", (), {"input": "/mnt/sh/a.1001.exr"})()
        self.assertEqual(source_path(cfg, seq, None), os.path.join("/mnt/sh", "a.####.exr"))

    def test_custom_destination(self):
        check_output_template("{custom_destination_path}/{sourcefolder-3}/{custom_destination_name}")
        ns = build_parser(PRESETS).parse_args(["-i", "a.exr", "-custom_destination_path", "X:/dailies",
                                              "-custom_destination_name", " client "])
        cfg = config_from_args(ns, PRESETS)
        self.assertEqual(cfg.custom_tokens, {"custom_destination_path": "X:/dailies",
                                             "custom_destination_name": "client"})
        cfg = config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr"]), PRESETS)
        self.assertEqual(cfg.custom_tokens["custom_destination_path"], "")   # empty when not given

    def test_tuner_sends_them(self):
        from vfx_tuner import core as tuner
        self.assertEqual(tuner.uses_custom("{custom_destination_path}/x", "a"), (True, False))
        state = tuner.TunerState()
        state.input.input1 = "a.1001.exr"
        state.dest.path, state.dest.custom_path = "{custom_destination_path}/", "X:/d"
        args = tuner.full_args(state, PRESETS)
        self.assertEqual(args[args.index("-custom_destination_path") + 1], "X:/d")
        self.assertNotIn("-custom_destination_name", args)

    def test_fps_keywords(self):
        from fractions import Fraction
        from vfx_transcode.cli import resolve_fps
        from vfx_transcode.timecode import fps_tokens
        self.assertEqual(fps_tokens(Fraction(24000, 1001)),
                         {"source_fps_float": "23.976", "source_fps_int": "24", "source_fps_frac": "24000/1001"})
        self.assertEqual(fps_tokens(Fraction(25)),
                         {"source_fps_float": "25.000", "source_fps_int": "25", "source_fps_frac": "25/1"})
        self.assertEqual(fps_tokens(Fraction(30000, 1001))["source_fps_int"], "30")
        self.assertEqual(fps_tokens(None)["source_fps_frac"], "")
        check_output_template("./{clean_name}_{source_fps_int}fps")
        for argv, header, frac in ((["-r", "23.976"], None, "24000/1001"),           # the user's choice
                                   ([], {"framesPerSecond": [25, 1]}, "25/1"),        # detected
                                   ([], None, "24/1")):                               # the default
            cfg = config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr"] + argv), PRESETS)
            info = [{"attribs": header}] if header else None
            cfg.fps = resolve_fps(cfg, info)[0]
            self.assertEqual(cfg.name_tokens["source_fps_frac"], frac)

    def test_output_fps(self):
        from fractions import Fraction
        from vfx_transcode.cli import resolve_fps, retime_timecode, parse_output_fps
        self.assertIsNone(parse_output_fps("same"))
        self.assertEqual(parse_output_fps("23.976"), Fraction(24000, 1001))
        cfg = config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr", "-output_fps", "25"]), PRESETS)
        fps, where = resolve_fps(cfg, [{"attribs": {"framesPerSecond": [24, 1]}}])
        self.assertEqual(fps, Fraction(25))
        self.assertIn("source 24", where)
        self.assertEqual(cfg.name_tokens["source_fps_int"], "24")       # the source's rate stays
        self.assertEqual(retime_timecode(cfg, "01:00:00:12"), "01:00:00:12")   # fits 25 fps
        cfg = config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr", "-output_fps", "25"]), PRESETS)
        cfg.source_fps = Fraction(30)
        # frame 27 at 30 fps does not exist at 25: the same second, the frame scaled
        self.assertEqual(retime_timecode(cfg, "01:00:00:27"), "01:00:00:22")
        self.assertEqual(retime_timecode(cfg, "01:00:00:29"), "01:00:00:24")
        cfg = config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr"]), PRESETS)
        self.assertEqual(resolve_fps(cfg, None)[0], Fraction(24))      # same: unchanged
        with self.assertRaises(Exception):
            config_from_args(build_parser(PRESETS).parse_args(["-i", "a.exr", "-output_fps", "fast"]), PRESETS)

    def test_tuner_output_fps(self):
        from vfx_tuner import core as tuner
        from vfx_transcode import super_presets as sp
        state = tuner.TunerState()
        state.input.input1 = "a.1001.exr"
        self.assertNotIn("-output_fps", tuner.full_args(state, PRESETS))
        state.input.output_fps = "25"
        args = tuner.full_args(state, PRESETS)
        self.assertEqual(args[args.index("-output_fps") + 1], "25")
        e = tuner.super_preset_entry(state, ["encode"])
        self.assertIn("25", sp.args_for({"x": e}, "x"))
        other = tuner.TunerState()
        tuner.apply_super_preset(other, e)
        self.assertEqual(other.input.output_fps, "25")

    def test_reserved(self):
        with self.assertRaises(Exception):
            names.check_token_name("sourcefolder3", "test")


if __name__ == "__main__":
    unittest.main()
