"""Movie input and super-presets (no ffmpeg needed)."""

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from vfx_transcode import cli, movie as mv, super_presets as sp
from vfx_transcode.errors import TranscodeError

PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "prores", "width": 1920, "height": 1080,
         "pix_fmt": "yuv422p10le", "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001",
         "nb_frames": "48", "duration": "2.002", "sample_aspect_ratio": "1:1",
         "color_space": "bt709", "tags": {"timecode": "01:00:10:00", "encoder": "Apple ProRes"}},
        {"codec_type": "audio", "codec_name": "pcm_s24le"},
        {"codec_type": "data", "tags": {"timecode": "01:00:10:00"}},
    ],
    "format": {"duration": "2.002", "tags": {"com.apple.quicktime.artist": "comp"}},
}


class MovieTests(unittest.TestCase):
    def test_is_movie(self):
        for name in ("a.mov", "B.MP4", "c.mxf", "d.mkv"):
            self.assertTrue(mv.is_movie(name))
        for name in ("a.1001.exr", "b.png", "c.mov.json"):
            self.assertFalse(mv.is_movie(name))

    def test_probe(self):
        info = mv.parse_probe(PROBE, "/m/sh020_v002.mov")
        self.assertEqual((info.width, info.height, info.frames), (1920, 1080, 48))
        self.assertEqual(info.fps, Fraction(24000, 1001))
        self.assertEqual(info.timecode, "01:00:10:00")
        self.assertFalse(info.alpha)
        header = info.header()[0]["attribs"]
        self.assertEqual(header["FramesPerSecond"], "24000/1001")
        self.assertEqual(header["timecode"], "01:00:10:00")
        self.assertEqual(header["movie:codec"], "prores")
        self.assertEqual(header["movie:com.apple.quicktime.artist"], "comp")
        self.assertNotIn("PixelAspectRatio", header)

    def test_probe_fallbacks(self):
        data = json.loads(json.dumps(PROBE))
        video = data["streams"][0]
        del video["nb_frames"]
        video["sample_aspect_ratio"] = "2:1"
        video["pix_fmt"] = "yuva444p10le"
        video["tags"] = {"timecode": "01:00:10;00"}            # drop-frame separator
        info = mv.parse_probe(data, "x.mov")
        self.assertEqual(info.frames, 48)                       # duration x fps
        self.assertEqual(info.par, 2)
        self.assertTrue(info.alpha)
        self.assertEqual(info.header()[0]["attribs"]["PixelAspectRatio"], 2.0)
        with self.assertRaises(TranscodeError):
            mv.parse_probe({"streams": [{"codec_type": "audio"}]}, "x.mov")

    def test_sequence_and_unpack_command(self):
        info = mv.parse_probe(PROBE, "/m/sh020_v002.mov")
        seq = mv.movie_sequence("/m/sh020_v002.mov", info, 1001, ".exr")
        self.assertEqual((seq.ext, seq.start, seq.end, seq.clean_name), (".png", 1001, 1048, "sh020_v002"))
        self.assertEqual(seq.folder, Path("/m"))
        seq = mv.with_frames(seq, "/tmp/w")
        self.assertEqual(seq.path(1001), Path("/tmp/w/sh020_v002.1001.png"))
        self.assertEqual(seq.folder, Path("/m"))                # outputs stay next to the movie
        cmd = mv.unpack_command("ffmpeg", info, seq)
        self.assertNotIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-start_number") + 1], "1001")
        self.assertTrue(cmd[-1].endswith("sh020_v002.%04d.png"))
        self.assertIn("in_color_matrix=bt709", cmd[cmd.index("-vf") + 1])
        one = mv.unpack_command("ffmpeg", info, seq, 1031)
        self.assertAlmostEqual(float(one[one.index("-ss") + 1]), 29.75 * 1001 / 24000, places=5)
        self.assertEqual(one[-1], str(Path("/tmp/w/sh020_v002.1031.png")))
        self.assertNotIn("-ss", mv.unpack_command("ffmpeg", info, seq, 1001))
        sixteen = mv.unpack_command("ffmpeg", info, seq, 1001, sixteen_bit=True, alpha=True)
        self.assertIn("rgba64be", sixteen[sixteen.index("-vf") + 1])

    def test_options(self):
        ns = cli.build_parser(None).parse_args(["-i", "a.mov"])
        self.assertEqual(cli.config_from_args(ns, _presets()).movie_first_frame, 1001)
        ns = cli.build_parser(None).parse_args(["-i", "a.mov", "-movie_first_frame", "1"])
        cfg = cli.config_from_args(ns, _presets())
        self.assertEqual(cfg.movie_first_frame, 1)
        self.assertFalse(cfg.timecode_source_given)
        ns = cli.build_parser(None).parse_args(["-i", "a.mov", "-timecode_source", "counter"])
        self.assertTrue(cli.config_from_args(ns, _presets()).timecode_source_given)


def _presets():
    from vfx_transcode.presets import load_presets
    return load_presets(Path(__file__).resolve().parent.parent / "presets.json")


class SuperPresetFileTests(unittest.TestCase):
    def test_roundtrip_and_expand(self):
        folder = Path(tempfile.mkdtemp())
        presets = {"dailies": {"notes": "n", "areas": {
            "encode": {"state": {"encode_preset": "h264"}, "args": ["-preset", "h264"]},
            "burnin": {"state": {}, "args": ["-burn_preset", "none", "-burn_tl", "{clean_name}"]}}},
            "slate": {"notes": "", "areas": {"slate": {"state": {}, "args": ["-slate", "generic"]}}}}
        sp.save(folder / sp.FILE_NAME, presets)
        loaded = sp.load(folder / sp.FILE_NAME)
        self.assertEqual(list(loaded), ["dailies", "slate"])
        self.assertEqual(sp.load(folder / "missing.json"), {})
        argv = ["-i", "x.exr", "-super_preset", "dailies,slate", "-burn_tl", "mine"]
        out = sp.expand_argv(argv, folder / "presets.json")
        self.assertEqual(out, ["-preset", "h264", "-burn_preset", "none", "-burn_tl", "{clean_name}",
                               "-slate", "generic", "-i", "x.exr", "-burn_tl", "mine"])
        other = folder / "sub"
        other.mkdir()
        sp.save(other / "mine.json", {"x": {"notes": "", "areas": {}}})
        self.assertEqual(sp.expand_argv(["-super_presets_file", str(other / "mine.json"),
                                         "-super_preset", "x", "-i", "a"], folder / "p.json"),
                         ["-i", "a"])
        self.assertEqual(sp.expand_argv(["-i", "a"], folder / "p.json"), ["-i", "a"])
        with self.assertRaises(TranscodeError):
            sp.expand_argv(["-super_preset", "nope"], folder / "presets.json")
        (folder / "bad.json").write_text('{"super_presets": {"a": {"areas": {"colour": {}}}}}')
        with self.assertRaises(TranscodeError):
            sp.load(folder / "bad.json")

    def test_cli_parses_expanded(self):
        folder = Path(tempfile.mkdtemp())
        sp.save(folder / sp.FILE_NAME, {"look": {"notes": "", "areas": {
            "letterbox": {"state": {}, "args": ["-letterbox", "1", "-letterbox_ratio", "2.39"]}}}})
        argv = sp.expand_argv(["-i", "x.1001.exr", "-super_preset", "look"], folder / "presets.json")
        ns = cli.build_parser(None).parse_args(argv)
        cfg = cli.config_from_args(ns, _presets())
        self.assertIsNotNone(cfg.letterbox)


if __name__ == "__main__":
    unittest.main()
