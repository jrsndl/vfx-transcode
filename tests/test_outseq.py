"""Sequence output, trim, ffmpeg transform, oiiotool letterbox (no tools needed)."""

import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from vfx_transcode import cli, outseq
from vfx_transcode.config import Config
from vfx_transcode.errors import TranscodeError
from vfx_transcode.letterbox import LetterboxSpec
from vfx_transcode.oiio import batch_patterns, _Target, plain_args, process_args
from vfx_transcode.sequence import Sequence
from tests.test_movie import _presets


def cfg_for(*argv):
    ns = cli.build_parser(None).parse_args(["-i", "x.1001.exr"] + list(argv))
    return cli.config_from_args(ns, _presets())


class OutputSequenceTests(unittest.TestCase):
    def test_sequence_ext(self):
        for text, ext in (("./out/{clean_name}.####.exr", ".exr"), ("a.%04d.JPG", ".jpg"),
                          ("x.png", ".png"), ("./", ""), ("out.mov", ""), ("", ""), ("d/", "")):
            self.assertEqual(outseq.sequence_ext(text), ext, text)
        self.assertEqual(cfg_for("-o", "a.####.dpx").seq_out_ext, ".dpx")
        self.assertEqual(cfg_for("-o", "a.mov").seq_out_ext, "")

    def test_from_path(self):
        out = outseq.from_path("/o/sh010.####.exr")
        self.assertEqual((out.base, out.zfill, out.ext, out.name(1001)), ("sh010.", 4, ".exr", "sh010.1001.exr"))
        self.assertEqual(outseq.from_path("/o/a_%05d.png").name(7), "a_00007.png")
        self.assertEqual(outseq.from_path("/o/a.@@@.png").zfill, 3)
        self.assertEqual(outseq.from_path("/o/plate.jpg", 6).name(12), "plate.000012.jpg")
        self.assertEqual(outseq.from_path("/o/plate_.jpg").name(1), "plate_0001.jpg")
        with self.assertRaises(TranscodeError):
            outseq.from_path("/o/####.exr")

    def test_output_args(self):
        self.assertEqual(outseq.oiio_output_args(".exr"), ["-d", "half", "--compression", "zip"])
        self.assertEqual(outseq.oiio_output_args(".png", sixteen_bit=True), ["-d", "uint16"])
        self.assertEqual(outseq.oiio_output_args(".exr", ["-d", "float"]), ["-d", "float"])
        self.assertIn("mjpeg", outseq.ffmpeg_output_args(".jpg"))
        with self.assertRaises(TranscodeError):
            outseq.ffmpeg_output_args(".exr")

    def test_resolve_output_seq(self):
        seq = Sequence(Path("/shows/sh010/v003"), "sh010_v003.", ".exr", 4, (1001, 1002))
        cfg = cfg_for("-o", "./../out/{clean_name}.####.png")
        out = cli.resolve_output_seq(cfg, seq, {"clean_name": "sh010_v003", "meta": {}})
        self.assertEqual(out.path(1001), Path("/shows/sh010/out/sh010_v003.1001.png"))
        cfg = cfg_for("-o", "./")
        cfg.seq_out_ext = ".jpg"
        self.assertEqual(cli.resolve_output_seq(cfg, seq, {"meta": {}}).name(1001), "sh010_v003.1001.jpg")
        cfg = cfg_for("-o", "/shows/sh010/v003/sh010_v003.####.exr")
        with self.assertRaises(TranscodeError):                 # onto the input frames
            cli.resolve_output_seq(cfg, seq, {"meta": {}})


class TrimTests(unittest.TestCase):
    def test_trim(self):
        seq = Sequence(Path("/s"), "a.", ".exr", 4, (1001, 1002, 1004, 1005, 1006))
        self.assertEqual(cli.trim_sequence(cfg_for(), seq), seq)
        self.assertEqual(cli.trim_sequence(cfg_for("-in", "1002", "-out", "1005"), seq).frames,
                         (1002, 1004, 1005))
        self.assertEqual(cli.trim_sequence(cfg_for("-in", "1004"), seq).frames, (1004, 1005, 1006))
        for bad in (["-in", "1010"], ["-in", "1005", "-out", "1002"]):
            with self.assertRaises(TranscodeError):
                cli.trim_sequence(cfg_for(*bad), seq)


class FfmpegTransformTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cli.ff_transform_from_args("scale=w=1920:h=-2,\n# note\n\nsetsar=1\n"),
                         "scale=w=1920:h=-2,setsar=1")
        self.assertEqual(cfg_for("-ff_transform", "zscale=w=1280:h=-2").ff_transform, "zscale=w=1280:h=-2")

    def test_filter_chain(self):
        from vfx_transcode import ffmpeg
        cfg = cfg_for("-ff_transform", "zscale=w=1280:h=-2:f=spline36")
        cfg.par_in = cfg.par_out_value = Fraction(1)
        ctx = ffmpeg.BurnContext("a", 1001, 1010, "00:00:00:00", Fraction(24), "h264", "d", {})
        chain = ffmpeg.build_video_filter(cfg, ctx, 1920)
        self.assertIn("zscale=w=1280:h=-2:f=spline36,scale=in_range=full", chain)
        self.assertNotIn("w=1920", chain)                       # -width scale replaced
        cfg.image_output = True
        chain = ffmpeg.build_video_filter(cfg, ctx, 0)
        self.assertNotIn("out_range=tv", chain)
        self.assertNotIn("setparams", chain)


class OiioLetterboxTests(unittest.TestCase):
    def test_options(self):
        self.assertIsNone(cfg_for().oiio_letterbox)
        spec = cfg_for("-oiio_letterbox", "1", "-oiio_letterbox_ratio", "1.85",
                       "-oiio_letterbox_color", "0,0,0,128").oiio_letterbox
        self.assertAlmostEqual(spec.ratio, 1.85)
        self.assertEqual(spec.fill, (0, 0, 0, 128))

    def test_boxes(self):
        spec = LetterboxSpec(ratio=2.39, line=2)
        args, kind, bar = outseq.letterbox_args(spec, 1920, 1080, 3)
        self.assertEqual((kind, bar), ("letterbox", 138))
        self.assertEqual(args[:4], ["--box:color=0,0,0,1:fill=1", "0,0,1919,137",
                                    "--box:color=0,0,0,1:fill=1", "0,942,1919,1079"])
        self.assertEqual(args[5], "0,136,1919,137")              # line on the inner edge
        self.assertEqual(args[4], "--box:color=1,1,1,1:fill=1")
        args, kind, bar = outseq.letterbox_args(LetterboxSpec(ratio=1.0, fill=(0, 0, 0, 128)), 1920, 1080, 4)
        self.assertEqual((kind, bar), ("pillarbox", 420))
        self.assertEqual(args[0], "--box:color=0,0,0,1,0.502:fill=1")
        self.assertEqual(outseq.letterbox_args(LetterboxSpec(ratio=1.7778), 1920, 1080, 3)[1], "none")
        args, kind, _ = outseq.letterbox_args(LetterboxSpec(ratio=2.39), 1000, 1080, 3, Fraction(2))
        self.assertEqual(kind, "letterbox")                      # 2000x1080 displayed


class OiioStepsTests(unittest.TestCase):
    def test_skip_colour_and_plain(self):
        from vfx_transcode.channels import ChannelPick
        pick = ChannelPick(0, ["R", "G", "B"], "t")
        cfg = cfg_for("-o", "a.####.exr")
        self.assertTrue(cfg.seq_out_ext == ".exr" and not cfg.colour_given)
        cfg.skip_colour = True
        self.assertNotIn("--colorconvert", process_args(cfg, pick))
        cfg.skip_colour = False
        self.assertIn("--colorconvert", process_args(cfg, pick))
        cfg = cfg_for("-width", "0")
        self.assertEqual(plain_args(cfg), [])
        cfg = cfg_for("-ci", "Output - Rec.709", "-co", "ACEScg", "-width", "1280")
        self.assertEqual(plain_args(cfg), ["--resize", "1280x0", "--colorconvert", "Output - Rec.709", "ACEScg"])

    def test_batch_patterns(self):
        seq = Sequence(Path("/in"), "a.", ".png", 4, (1, 2), frames_folder=Path("/tmp/w"))
        src, dst = batch_patterns(seq, _Target("/out", "b_", 5, ".exr"))
        self.assertEqual((src, dst), ("/tmp/w/a.%04d.png", "/out/b_%05d.exr"))


if __name__ == "__main__":
    unittest.main()


class SequencePresetTests(unittest.TestCase):
    def test_file(self):
        presets = _presets()
        self.assertEqual(presets.default_sequences["exr"], "exr_zip16_half")
        self.assertEqual(presets.sequences["exr_zip1_half"].args, ("-d", "half", "--compression", "zips"))
        self.assertEqual(presets.sequences["exr_dwab60_half"].args[-1], "dwab:60")
        self.assertIn("4:4:4", presets.sequences["jpg_high"].args)
        self.assertEqual(presets.sequences["png_16bit"].args, ("-d", "uint16"))

    def test_resolution(self):
        cfg = cfg_for("-o", "a.####.exr")
        self.assertEqual((cfg.seq_preset, cfg.seq_args), ("exr_zip16_half", ["-d", "half", "--compression", "zip"]))
        cfg = cfg_for("-o", "a.####.exr", "-seq_preset", "exr_dwab30_half")
        self.assertEqual(cfg.seq_args[-1], "dwab:30")
        cfg = cfg_for("-o", "a.####.exr", "-seq_preset", "exr_dwab30_half", "-seq_args", "-d float")
        self.assertEqual((cfg.seq_preset, cfg.seq_args), ("-seq_args", ["-d", "float"]))
        cfg = cfg_for("-o", "a.####.tif")
        self.assertEqual((cfg.seq_preset, cfg.seq_args), ("", []))          # no default: built-in
        cfg = cfg_for("-o", "a.####.jpeg")
        self.assertEqual(cfg.seq_preset, "jpg_high")
        with self.assertRaises(TranscodeError):
            cfg_for("-o", "a.####.exr", "-seq_preset", "nope")

    def test_bad_presets(self):
        from vfx_transcode.outseq import sequence_presets_from_json
        good = {"a": {"ext": "exr", "args": "-d half"}}
        self.assertEqual(sequence_presets_from_json(good, {"exr": "a"}, "t")[1], {"exr": "a"})
        for data, defaults in (({"a": {"ext": "mov", "args": ""}}, {}),
                               ({"a": {"ext": "exr", "nope": 1}}, {}),
                               (good, {"png": "a"}), (good, {"exr": "b"})):
            with self.assertRaises(TranscodeError):
                sequence_presets_from_json(data, defaults, "t")
