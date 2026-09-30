"""Unit tests. Run with `python -m unittest` (or pytest) from the repository root."""

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from vfx_transcode import cli, ffmpeg, oiio
from vfx_transcode.channels import pick_channels
from vfx_transcode.config import Config
from vfx_transcode.errors import TranscodeError
from vfx_transcode.oiio_info import Rational, parse_oiio_xml_output, split_imagespecs, write_info_json
from vfx_transcode.presets import LEGACY_H264, load_presets, transfer_for
from vfx_transcode.sequence import fill_holes, find_sequence, format_frames
from vfx_transcode.timecode import fps_to_str, frame_to_tc, nominal_fps, parse_fps
from vfx_transcode.tools import resolve_path, split_args


REPO = Path(__file__).resolve().parent.parent
PRESETS = load_presets(REPO / "presets.json")


def _info(*subimage_channels):
    return [{"channelnames": list(c)} for c in subimage_channels]


class TimecodeTests(unittest.TestCase):
    def test_frame_to_tc(self):
        self.assertEqual(frame_to_tc(1001, 24), "00:00:41:17")
        self.assertEqual(frame_to_tc(0, 25), "00:00:00:00")
        self.assertEqual(frame_to_tc(24 * 3600 + 1, 24), "01:00:00:01")
        self.assertEqual(frame_to_tc(24 * 3600 * 25, 24), "01:00:00:00")   # wraps at 24h

    def test_parse_fps(self):
        self.assertEqual(parse_fps("24"), 24)
        self.assertEqual(parse_fps("24000/1001"), Fraction(24000, 1001))
        self.assertEqual(parse_fps("23.976"), Fraction(24000, 1001))  # used to crash; NTSC snap
        self.assertEqual(parse_fps("29.97"), Fraction(30000, 1001))
        self.assertEqual(parse_fps("2997/125"), Fraction(2997, 125))    # exact fraction kept
        self.assertEqual(parse_fps("12.5"), Fraction(25, 2))
        with self.assertRaises(ValueError):
            parse_fps("abc")
        with self.assertRaises(ValueError):
            parse_fps("0")

    def test_fps_helpers(self):
        self.assertEqual(fps_to_str(Fraction(24000, 1001)), "24000/1001")
        self.assertEqual(fps_to_str(Fraction(25)), "25")
        self.assertEqual(nominal_fps(Fraction(24000, 1001)), 24)
        self.assertEqual(nominal_fps(Fraction(30000, 1001)), 30)


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, *names):
        for n in names:
            (self.dir / n).write_bytes(b"")

    def test_holes_and_padding(self):
        self.touch("shot_010.1001.exr", "shot_010.1002.exr", "shot_010.1004.exr",
                   "shot_010.v2.exr", "shot_010.01003.exr", "other.1003.exr", "shot_010.1003.png")
        seq = find_sequence(self.dir / "shot_010.1002.exr")
        self.assertEqual(seq.frames, (1001, 1002, 1004))
        self.assertEqual(seq.missing, [1003])
        self.assertEqual(seq.runs(), [(1001, 1002), (1004, 1004)])
        self.assertEqual(seq.clean_name, "shot_010")
        self.assertEqual(seq.name(1003, ".png"), "shot_010.1003.png")

    def test_unnumbered_single_file(self):
        # used to crash with an unbound variable
        self.touch("plate.exr")
        seq = find_sequence(self.dir / "plate.exr")
        self.assertFalse(seq.numbered)
        self.assertEqual(seq.frames, (0,))
        self.assertEqual(seq.name(0), "plate.exr")

    def test_missing_input(self):
        with self.assertRaises(TranscodeError):
            find_sequence(self.dir / "nope.1001.exr")

    def test_fill_holes(self):
        files = fill_holes(range(1, 7), {2: "b", 3: "c", 5: "e"})
        self.assertEqual(files, ["b", "b", "c", "c", "e", "e"])

    def test_format_frames(self):
        self.assertEqual(format_frames([1, 2, 3, 7, 9, 10]), "1-3,7,9-10")


class ChannelTests(unittest.TestCase):
    def test_prefers_rgba_over_first_layer(self):
        # the old picker always took the first layer (AO here)
        pick = pick_channels(_info(["AO.R", "AO.G", "AO.B", "AO.A",
                                    "rgba.R", "rgba.G", "rgba.B", "rgba.A"]))
        self.assertEqual(str(pick), "rgba.R,rgba.G,rgba.B")

    def test_priority_order_not_last_match(self):
        pick = pick_channels(_info(["R", "G", "B", "beauty.R", "beauty.G", "beauty.B"]))
        self.assertEqual(str(pick), "R,G,B")

    def test_plain_rgba(self):
        self.assertEqual(str(pick_channels(_info(["R", "G", "B", "A"]))), "R,G,B")

    def test_multipart(self):
        pick = pick_channels(_info(["depth.Z"], ["rgba.R", "rgba.G", "rgba.B"]))
        self.assertEqual(pick.subimage, 1)

    def test_requested(self):
        pick = pick_channels(_info(["N.X", "N.Y", "N.Z", "R", "G", "B"]), ["N.X", "N.Y", "N.Z"])
        self.assertEqual(str(pick), "N.X,N.Y,N.Z")

    def test_use_first_and_fallbacks(self):
        info = _info(["diffuse.R", "diffuse.G", "diffuse.B", "R", "G", "B"])
        self.assertEqual(str(pick_channels(info, use_first=True)), "diffuse.R,diffuse.G,diffuse.B")
        self.assertEqual(str(pick_channels(_info(["Y"]))), "Y")
        self.assertEqual(str(pick_channels(_info(["depth.Z", "spec.R", "spec.G", "spec.B"]))),
                         "spec.R,spec.G,spec.B")

    def test_custom_preferences_from_file(self):
        info = _info(["beauty.R", "beauty.G", "beauty.B", "R", "G", "B"])
        self.assertEqual(str(pick_channels(info, preferences=PRESETS.channel_preferences)), "R,G,B")
        self.assertEqual(str(pick_channels(info, preferences=[["beauty.R", "beauty.G", "beauty.B"]])),
                         "beauty.R,beauty.G,beauty.B")

    def test_alpha_removal_is_exact(self):
        # 'beta' ends in 'a' but is not alpha; the old regex removed it
        pick = pick_channels(_info(["x.alpha", "x.beta", "x.gamma"]), ["x.alpha", "x.beta", "x.gamma"])
        self.assertEqual(str(pick), "x.alpha,x.beta,x.gamma")


class OiioInfoTests(unittest.TestCase):
    XML = """noise before
<?xml version="1.0"?>
<ImageSpec version="26">
<x>0</x><width>1920</width><height>1080</height>
<channelnames><channelname>R</channelname><channelname>G</channelname></channelnames>
<attrib name="FramesPerSecond" type="rational2i">24/1</attrib>
<attrib name="worldToCamera" type="matrix">1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16</attrib>
<attrib name="weird" type="string">a&#01;b</attrib>
</ImageSpec>"""

    def test_parse(self):
        blocks = split_imagespecs(self.XML + "\n" + self.XML)
        self.assertEqual(len(blocks), 2)
        info = parse_oiio_xml_output(blocks[0])
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["channelnames"], ["R", "G"])
        self.assertAlmostEqual(float(info["attribs"]["FramesPerSecond"]), 24.0)
        self.assertEqual(info["attribs"]["worldToCamera"][0], [1, 2, 3, 4])   # rows, not columns

    def test_json_with_rational(self):
        # json.dump used to crash on the Rational attribute
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "info.json"
            write_info_json([{"attribs": {"fps": Rational("24/1")}}], path)
            self.assertEqual(json.loads(path.read_text())[0]["attribs"]["fps"], "24/1")


class EscapingTests(unittest.TestCase):
    def test_ffmpeg_docs_example(self):
        # from the ffmpeg-filters documentation, "Notes on filtergraph escaping"
        value = "this is a 'string': may contain one, or more, special characters"
        level1 = ffmpeg.escape_option_value(value)
        self.assertEqual(level1, r"this is a \'string\'\: may contain one, or more, special characters")
        self.assertEqual(ffmpeg.escape_filtergraph("text=" + level1),
                         r"text=this is a \\\'string\\\'\\: may contain one\, or more\, special characters")


def _cfg(**kw):
    base = dict(input="in.1001.exr", oiiotool="oiiotool", ffmpeg="ffmpeg",
                burn={"tl": "", "tr": "", "bl": "", "br": ""}, preset=PRESETS.get("h264"))
    base.update(kw)
    return Config(**base)


def _ctx():
    return ffmpeg.BurnContext("shot 010", 1001, 1100, "00:00:41:17", Fraction(24))


class BurnStyleTests(unittest.TestCase):
    def parse(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_layers(self):
        from vfx_transcode.burnin import BurnStyle, resolve_styles
        styles = resolve_styles(BurnStyle(opacity=0.8, size="3%"),
                                {"tc": BurnStyle(color="yellow", box=True)},
                                BurnStyle(size="40"), {"tc": BurnStyle(opacity=1.0)})
        self.assertEqual(styles["tl"], BurnStyle("white", 0.8, "40", False))    # cli size beats preset
        self.assertEqual(styles["tc"], BurnStyle("yellow", 1.0, "40", True))      # place overrides

    def test_command_line(self):
        cfg = self.parse("-burn_tc", "{date}", "-burn_color", "#ffcc00", "-burn_opacity", "0.5",
                         "-burn_style", "tc:color=red,box=1,size=30", "-burn_style", "all:size=50")
        self.assertEqual(cfg.burn["tc"], "{date}")
        tc, bl = cfg.burn_styles["tc"], cfg.burn_styles["bl"]
        self.assertEqual((tc.color, tc.opacity, tc.size, tc.box), ("red", 0.5, "50", True))  # later -burn_style wins
        self.assertEqual((bl.color, bl.size, bl.box), ("#ffcc00", "50", False))
        for bad in (("-burn_opacity", "2"), ("-burn_color", "not a colour"), ("-burn_size", "0"),
                    ("-burn_style", "middle:size=3"), ("-burn_style", "tc:weight=bold"),
                    ("-burn_style", "tc:box=maybe")):
            with self.assertRaises(TranscodeError):
                self.parse(*bad)

    def test_size_forms(self):
        from vfx_transcode.burnin import parse_size, size_expression, size_pixels
        self.assertEqual(parse_size(40), "40")
        self.assertEqual(parse_size("40px"), "40")
        self.assertEqual(parse_size(" 1.5 % "), "1.5%")
        self.assertEqual(parse_size("h/75"), "h/75")
        self.assertEqual(parse_size(12.5), "12.5")
        for bad in ("0", "0%", "150%", "big", "-3", "h/0", True):
            with self.assertRaises(TranscodeError):
                parse_size(bad)
        self.assertEqual(size_expression("1.5%"), "h*0.015")
        self.assertEqual(size_expression("h/75"), "h/75")
        self.assertEqual(size_expression("40"), "40")
        self.assertAlmostEqual(size_pixels("1.5%", 1080), 16.2)
        self.assertAlmostEqual(size_pixels("h/75", 1080), 14.4)
        self.assertEqual(size_pixels("40", 1080), 40)

    def test_drawtext_options(self):
        from vfx_transcode.burnin import BurnStyle
        vf = ffmpeg.build_drawtext("{clean_name}", "tc", _ctx(), "", BurnStyle("yellow", 0.5, "h/40", True), 1080)
        self.assertIn("fontcolor=yellow@0.5", vf)
        self.assertIn("boxcolor=black@0.5", vf)
        self.assertIn("box=1", vf)
        self.assertIn("x=(w-tw)/2", vf)                      # centred
        self.assertIn("fontsize=h/40", vf)
        self.assertNotIn("shadowx", vf)
        vf = ffmpeg.build_drawtext("x", "bc", _ctx(), "", BurnStyle("white", 1.0, "1.5%", False))
        self.assertIn("fontsize=h*0.015", vf)
        vf = ffmpeg.build_drawtext("x", "bc", _ctx(), "", BurnStyle("white", 1.0, "36", False))
        self.assertIn("fontsize=36", vf)
        self.assertIn("shadowcolor=black@1", vf)
        self.assertIn("y=h-(h*0.015)-lh", vf)

    def test_preset_file_styles(self):
        base = '{"encode_presets": {"x": {"ext": "mp4", "encode": "{videofilter}"}}, "burnin_presets": {"b": %s}}'
        p = load_presets(PresetFileTests.write(self, base % '{"opacity": 0.7, "tl": "a", "tc": {"text": "b", "color": "yellow", "box": true}, "bc": {"size": 20}}'))
        b = p.get_burnin("b")
        self.assertEqual(b.corners, {"tl": "a", "tc": "b"})
        self.assertEqual(b.style.opacity, 0.7)
        self.assertEqual(b.place_styles["tc"].color, "yellow")
        for bad in ('{"tc": {"text": "x", "weight": 1}}', '{"tc": 5}', '{"opacity": 3}', '{"color": "no such"}',
                    '{"box": "maybe"}'):
            with self.assertRaises(TranscodeError):
                load_presets(PresetFileTests.write(self, base % bad))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()


class MetaKeywordTests(unittest.TestCase):
    HEADER = {"Copyright": "Z017_C001_20190920_R[00000-02773]", "cameraModel": "DJI Zemuse X7",
              "nuke/node_hash": "db920c72b1fecf8c", "FramesPerSecond": Rational("24/1"),
              "isoSpeed": 200.0, "smpte:TimeCode": "00:00:41:17"}

    def test_lookup(self):
        from vfx_transcode import metadata as md
        self.assertEqual(md.meta_lookup(self.HEADER, "owner"), ("Z017_C001_20190920_R[00000-02773]", True))
        self.assertEqual(md.meta_lookup(self.HEADER, "CAMERAMODEL")[0], "DJI Zemuse X7")
        self.assertEqual(md.meta_lookup(self.HEADER, "smpte:TimeCode")[0], "00:00:41:17")
        self.assertEqual(md.meta_lookup(self.HEADER, "FramesPerSecond")[0], "24/1")
        self.assertEqual(md.meta_lookup(self.HEADER, "nope|n/a"), ("n/a", False))
        self.assertEqual(md.meta_lookup({}, "nope"), ("", False))

    def test_in_metadata_values(self):
        from vfx_transcode import metadata as md
        out = md.expand("{clean_name} shot on {meta:cameraModel} ({meta:lens|unknown lens})",
                        {"clean_name": "sh010", "meta": self.HEADER})
        self.assertEqual(out, "sh010 shot on DJI Zemuse X7 (unknown lens)")

    def test_in_burnin(self):
        ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24), header=self.HEADER)
        opts = dict(ffmpeg.expand_burn_template("{meta:owner} {current_frame} 100%", ctx))
        self.assertEqual(opts["text"], "Z017_C001_20190920_R[00000-02773] %{frame_num} 100\\%")
        vf = ffmpeg.build_drawtext("{meta:owner}", "tc", ctx, "", __import__(
            "vfx_transcode.burnin", fromlist=["x"]).DEFAULT_STYLE)
        self.assertIn(r"\[00000-02773\]", vf)        # brackets escaped for the filtergraph

    def test_timecode_header_frame(self):
        seq = type("S", (), {"start": 1001, "frame_of": lambda self, p: 1003})()
        cfg = _cfg(timecode_source="exr")
        info = [{"attribs": {"smpte:TimeCode": "10:00:00:10"}}]
        self.assertEqual(cli.resolve_timecode(cfg, seq, info, header_frame=1001)[0], "10:00:00:10")
        self.assertEqual(cli.resolve_timecode(cfg, seq, info)[0], "10:00:00:08")   # from -i frame


class DisplayViewTests(unittest.TestCase):
    def parse(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_colorspace_mode_default(self):
        cfg = self.parse()
        self.assertFalse(cfg.display_mode)
        self.assertEqual(oiio.colour_args(cfg), ["--colorconvert", "ACEScg", "out_rec709"])
        self.assertEqual(cfg.output_colour, "out_rec709")

    def test_display_view(self):
        cfg = self.parse("-ocio", "ocio://studio-config-latest", "-display", "sRGB - Display",
                         "-view", "ACES 1.0 - SDR Video", "-looks", "ACES 1.3 Reference Gamut Compression")
        self.assertEqual(oiio.colour_args(cfg), ["--ociodisplay:from=ACEScg:looks=ACES 1.3 Reference Gamut Compression",
                                                 "sRGB - Display", "ACES 1.0 - SDR Video"])
        self.assertEqual(cfg.ocio, "ocio://studio-config-latest")
        self.assertIn("iec61966-2-1", ffmpeg.encode_args(cfg, "VF"))          # tags follow the display
        cfg = self.parse("-display", "Rec.1886 Rec.709 - Display")
        self.assertEqual(oiio.colour_args(cfg), ["--ociodisplay:from=ACEScg", "Rec.1886 Rec.709 - Display", ""])
        self.assertIn("bt709", ffmpeg.encode_args(cfg, "VF")[-1])
        cfg = self.parse("-view", "Un-tone-mapped")                               # default display
        self.assertEqual(oiio.colour_args(cfg)[1:], ["", "Un-tone-mapped"])
        self.assertTrue(cli.HDR_OR_WIDE.search("Rec.2100-PQ - Display"))
        self.assertFalse(cli.HDR_OR_WIDE.search("sRGB - Display"))

    def test_ocio_info_needs_no_input(self):
        ns = cli.build_parser(PRESETS).parse_args(["-ocio_info"])
        self.assertIsNone(ns.i)
        self.assertEqual(cli.main(["-v", "0"]), cli.EXIT_ERROR)                   # no -i, no -ocio_info


class PixelAspectTests(unittest.TestCase):
    def test_parse(self):
        from vfx_transcode.aspect import parse_par
        self.assertEqual(parse_par("2"), 2)
        self.assertEqual(parse_par("1.33"), Fraction(4, 3))        # anamorphic 1.33x
        self.assertEqual(parse_par("1.333"), Fraction(4, 3))
        self.assertEqual(parse_par("4/3"), Fraction(4, 3))
        self.assertEqual(parse_par("1.3"), Fraction(13, 10))       # kept as typed
        self.assertEqual(parse_par(2.0), 2)
        for bad in ("abc", "0", "-2", "50", "1/0"):
            with self.assertRaises(TranscodeError):
                parse_par(bad)

    def test_sources(self):
        from vfx_transcode.aspect import resolve_par
        hdr2 = {"PixelAspectRatio": 2.0}
        cases = [("arg", None, hdr2, (2, "EXR")), ("arg", Fraction(1), hdr2, (1, "-par")),
                 ("exr", Fraction(1), hdr2, (2, "EXR")), ("exr", Fraction(4, 3), {}, (Fraction(4, 3), "-par")),
                 ("arg_only", None, hdr2, (1, "default")), ("arg", None, {}, (1, "default")),
                 ("arg", None, {"pixelAspectRatio": "1.33"}, (Fraction(4, 3), "EXR"))]
        for source, arg, header, expected in cases:
            with self.subTest(source=source, arg=arg, header=header):
                cfg = _cfg(par_source=source, par_arg=arg, header_attribs=header)
                par, origin = resolve_par(cfg)
                self.assertEqual((par, origin.split(" ")[0]), expected)

    def test_output(self):
        from vfx_transcode.aspect import output_par
        self.assertEqual(output_par(Fraction(2), "same"), 2)
        self.assertEqual(output_par(Fraction(2), "square"), 1)
        self.assertEqual(output_par(Fraction(2), "1.33"), Fraction(4, 3))

    def test_filter_order(self):
        cfg = _cfg(par_in=Fraction(2), par_out_value=Fraction(1),
                   burn={"tl": "x", "tr": "", "bl": "", "br": ""})
        vf = ffmpeg.build_video_filter(cfg, _ctx(), 0)
        parts = vf.split(",")
        self.assertTrue(parts[0].startswith("scale=w=iw:h=trunc(ih*1/2/2)*2"))   # reformat first
        sar = next(i for i, p in enumerate(parts) if p.startswith("setsar"))
        self.assertTrue(parts[sar - 1].startswith("pad="))                        # tag after pad
        self.assertTrue(parts[sar + 1].startswith("setparams"))                   # frame colour tags
        self.assertTrue(parts[sar + 2].startswith("drawtext"))
        self.assertIn("setsar=sar=1/1", vf)
        vf = ffmpeg.build_video_filter(_cfg(par_in=Fraction(2), par_out_value=Fraction(2)), _ctx(), 0)
        self.assertNotIn("trunc(ih", vf)                                           # same: no reformat
        self.assertIn("setsar=sar=2/1", vf)

    def test_cli(self):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-par", "1.33", "-par_out", "square"])
        cfg = cli.config_from_args(ns, PRESETS)
        self.assertEqual((cfg.par_arg, cfg.par_out, cfg.par_source), (Fraction(4, 3), "square", "arg"))
        for bad in (["-par", "x"], ["-par_out", "wide"]):
            with self.assertRaises(TranscodeError):
                cli.config_from_args(cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + bad), PRESETS)


class BurnInTests(unittest.TestCase):
    def test_edge_spaces_are_kept(self):
        self.assertEqual(ffmpeg.escape_option_value(" TC "), "\\ TC\\ ")

    def test_keywords(self):
        opts = dict(ffmpeg.expand_burn_template("{clean_name} {current_frame}/{frame_end}", _ctx()))
        self.assertEqual(opts["text"], "shot 010 %{frame_num}/1100")
        self.assertEqual(opts["start_number"], "1001")

    def test_tc_is_a_real_timecode(self):
        opts = dict(ffmpeg.expand_burn_template("TC {tc}", _ctx()))
        self.assertEqual(opts["text"], "TC ")
        self.assertEqual(opts["timecode"], "00:00:41:17")
        self.assertEqual(opts["rate"], "24")

    def test_static_keywords(self):
        ctx = ffmpeg.BurnContext("sh", 1, 9, "01:00:00:00", Fraction(24000, 1001), "h264", "2026-09-29")
        opts = dict(ffmpeg.expand_burn_template("{fps} fps {date} {preset} {timecode}", ctx))
        self.assertEqual(opts["text"], "23.976 fps 2026-09-29 h264 01:00:00:00")
        self.assertNotIn("timecode", opts)          # {timecode} is static, only {tc} runs

    def test_new_lines(self):
        from vfx_transcode.burnin import DEFAULT_STYLE
        opts = dict(ffmpeg.expand_burn_template(r"{clean_name}\nTC {tc}", _ctx()))
        self.assertEqual(opts["text"], "shot 010\nTC ")                  # real newline, TC appended
        self.assertEqual(dict(ffmpeg.expand_burn_template("a\nb", _ctx()))["text"], "a\nb")  # JSON "\n"
        vf = ffmpeg.build_drawtext(r"a\nb\nc", "br", _ctx(), "", DEFAULT_STYLE)
        self.assertIn("y=h-(h*0.015)-3*lh", vf)                           # 3 lines grow upwards
        self.assertIn("text_align=R", vf)
        vf = ffmpeg.build_drawtext(r"a\nb", "tc", _ctx(), "", DEFAULT_STYLE)
        self.assertIn("text_align=C", vf)
        self.assertIn("y=h*0.015", vf)
        vf = ffmpeg.build_drawtext(r"a\nb", "tl", _ctx(), "", DEFAULT_STYLE)
        self.assertNotIn("text_align", vf)                                # left is the default
        vf = ffmpeg.build_drawtext("one line", "br", _ctx(), "", DEFAULT_STYLE)
        self.assertNotIn("text_align", vf)                                # single line: unchanged
        self.assertIn("y=h-(h*0.015)-lh", vf)

    def test_percent_and_unknown(self):
        opts = dict(ffmpeg.expand_burn_template("100% {nope}", _ctx()))
        self.assertEqual(opts["text"], "100\\% {nope}")

    def test_filter_keeps_spaces_in_one_argument(self):
        cfg = _cfg(burn={"tl": "{clean_name}", "tr": "", "bl": "", "br": "{current_frame}"},
                   burn_font="")
        vf = ffmpeg.build_video_filter(cfg, _ctx(), 0)
        cmd = ffmpeg.build_command(cfg, "C:/My Shots/list.txt", "C:/My Shots/out.mp4", vf, "01:00:00:00")
        self.assertIn(vf, cmd)                            # -vf value is one argument
        self.assertEqual(cmd[-1], "C:/My Shots/out.mp4")
        self.assertIn("text=shot 010", vf)


class CommandTests(unittest.TestCase):
    def test_default_preset_matches_old_default(self):
        cfg = _cfg()
        args = ffmpeg.encode_args(cfg, "VF")
        old = ffmpeg.split_args(LEGACY_H264.replace("{videofilter}", "-vf VF"))
        self.assertEqual(sorted(args), sorted(old))

    def test_user_encode_overrides_and_no_tags(self):
        cfg = _cfg(ff_encode='-c:v libx264 {videofilter} -x264-params "a=1:b=2"')
        self.assertEqual(ffmpeg.encode_args(cfg, "VF"),
                         ["-c:v", "libx264", "-vf", "VF", "-x264-params", "a=1:b=2"])

    def test_prores_vendor_tag(self):
        from vfx_transcode import metadata as md
        prores = PRESETS.get("prores422hq")
        self.assertEqual(prores.metadata, {"vendor_id": "appl"})
        self.assertNotIn("vendor_id=appl", " ".join(prores.encode))     # moved out of "encode"
        tags = md.build({}, {}, {}, [prores.metadata])
        cmd = ffmpeg.build_command(_cfg(preset=prores, ff_out_ext="mov"), "l", "o.mov", "VF",
                                   "01:00:00:00", tags)
        self.assertIn("vendor_id=appl", cmd)
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+use_metadata_tags")   # so it is kept

    def test_srgb_transfer(self):
        self.assertEqual(transfer_for("out_srgb"), "iec61966-2-1")
        self.assertEqual(transfer_for("Output - sRGB"), "iec61966-2-1")
        self.assertEqual(transfer_for("out_rec709"), "bt709")
        for preset in PRESETS.presets.values():
            self.assertIn("{videofilter}", preset.encode)

    def test_timecode_track_or_tag(self):
        cmd = ffmpeg.build_command(_cfg(ff_out_ext="mov"), "l.txt", "o.mov", "VF", "01:00:00:00")
        self.assertEqual(cmd[cmd.index("-timecode") + 1], "01:00:00:00")
        cmd = ffmpeg.build_command(_cfg(ff_out_ext="mkv"), "l.txt", "o.mkv", "VF", "01:00:00:00")
        self.assertNotIn("-timecode", cmd)
        self.assertIn("timecode=01:00:00:00", cmd)
        cmd = ffmpeg.build_command(_cfg(ff_out_ext="mov", output_timecode=False), "l", "o.mov", "VF", "01:00:00:00")
        self.assertNotIn("-timecode", cmd)

    def test_metadata_args_and_movflags(self):
        cmd = ffmpeg.build_command(_cfg(), "l.txt", "o.mp4", "VF", "01:00:00:00",
                                   {"title": "sh010", "husk:x": "a b"})
        self.assertEqual(cmd.count("-movflags"), 1)       # merged, not a second -movflags
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "faststart+use_metadata_tags")
        self.assertIn("title=sh010", cmd)
        self.assertIn("husk:x=a b", cmd)

    def test_oiio_args(self):
        from vfx_transcode.channels import ChannelPick
        pick = ChannelPick(1, ["rgba.R", "rgba.G", "rgba.B", "rgba.A"], "preferred")
        args = oiio.process_args(_cfg(), pick)
        self.assertEqual(args[:4], ["--subimage", "1", "--ch", "rgba.R,rgba.G,rgba.B"])
        self.assertEqual(args[-2:], ["-d", "uint8"])
        self.assertEqual(oiio.process_args(_cfg(mezzanine_16bit=True), pick)[-2:], ["-d", "uint16"])
        deep = ChannelPick(0, ["R", "G", "B", "A"], "preferred", deep=True)
        self.assertEqual(oiio.process_args(_cfg(), deep)[:3], ["--flatten", "--ch", "R,G,B"])

    def test_split_args_keeps_backslashes(self):
        self.assertEqual(split_args(r'--ociofiletransform "C:\luts\my look.cube"'),
                         ["--ociofiletransform", r"C:\luts\my look.cube"])


class DeepTests(unittest.TestCase):
    KARMA = [{"deep": 1, "channelnames": ["A", "BA", "GA", "RA", "Z", "ZBack"]}]

    def test_no_colour_becomes_depth_view(self):
        from vfx_transcode import deep
        pick = pick_channels(self.KARMA)          # would be BA,GA,RA: alphas, not colour
        view = deep.plan_depth_view(_cfg(deep_zrange="10,500"), self.KARMA, pick)
        self.assertEqual((view.near, view.far, view.alpha), (10, 500, True))
        args = oiio.process_args(_cfg(), pick, view)
        self.assertEqual(args[:6], ["--flatten", "--dup", "--ch", "R=A,G=A,B=A", "--swap", "--ch"])
        self.assertIn("--contrast:black=10:white=500", args)
        self.assertNotIn("--colorconvert", args)
        self.assertEqual(args[args.index("--invert") + 1], "--mul")

    def test_deep_with_colour_is_flattened_and_converted(self):
        from vfx_transcode import deep
        info = [{"deep": 1, "channelnames": ["R", "G", "B", "A", "Z", "ZBack"]}]
        pick = pick_channels(info)
        self.assertIsNone(deep.plan_depth_view(_cfg(), info, pick))
        args = oiio.process_args(_cfg(), pick)
        self.assertEqual(args[:3], ["--flatten", "--ch", "R,G,B"])
        self.assertIn("--colorconvert", args)

    def test_flat_image_untouched(self):
        from vfx_transcode import deep
        info = _info(["R", "G", "B"])
        self.assertIsNone(deep.plan_depth_view(_cfg(), info, pick_channels(info)))

    def test_printstats_and_zrange(self):
        from vfx_transcode import deep
        out = "Stats for x:\n    Stats Min: 0.000000 (float)\n    Stats Max: 845.3 (float)\n"
        self.assertEqual(deep.parse_printstats(out), {"Min": [0.0], "Max": [845.3]})
        self.assertEqual(deep.parse_zrange(" 1, 2e3"), (1.0, 2000.0))
        for bad in ("5", "a,b", "10,1"):
            with self.assertRaises(TranscodeError):
                deep.parse_zrange(bad)


class TimecodeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        for f in (1001, 1002, 1003):
            (d / "sh.{}.exr".format(f)).write_bytes(b"")
        self.seq = find_sequence(d / "sh.1003.exr")
        self.input = str(d / "sh.1003.exr")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sources(self):
        info = [{"attribs": {"smpte:TimeCode": "10:00:00:10"}}]
        cfg = _cfg(input=self.input, timecode_source="counter")
        self.assertEqual(cli.resolve_timecode(cfg, self.seq, info)[0], "00:00:41:17")
        cfg = _cfg(input=self.input, timecode_source="arg", timecode="02:00:00:00")
        self.assertEqual(cli.resolve_timecode(cfg, self.seq, info)[0], "02:00:00:00")
        # header of frame 1003 says 10:00:00:10, so the first frame (1001) is 2 frames earlier
        cfg = _cfg(input=self.input, timecode_source="exr")
        self.assertEqual(cli.resolve_timecode(cfg, self.seq, info), ("10:00:00:08", "EXR header"))
        self.assertEqual(cli.resolve_timecode(cfg, self.seq, [{"attribs": {}}])[1], "frame counter")

    def test_header_formats(self):
        from vfx_transcode.timecode import decode_smpte_bcd, timecode_from_attribs
        self.assertEqual(decode_smpte_bcd(0x01000000), "01:00:00:00")
        self.assertEqual(timecode_from_attribs({"smpte:TimeCode": [0x10594523, 0]}), "10:59:45:23")
        self.assertEqual(timecode_from_attribs({"nuke/timecode": "01:02:03:04"}), "01:02:03:04")
        self.assertIsNone(timecode_from_attribs({"DateTime": "2026:09:25 11:56:58"}))


class FpsSourceTests(unittest.TestCase):
    HDR25 = [{"attribs": {"FramesPerSecond": Rational("25/1")}}]
    KARMA = [{"attribs": {"FramesPerSecond": 23.976}}]      # Karma writes a float
    NONE = [{"attribs": {}}]

    def fps(self, source, arg, info):
        cfg = _cfg(fps_source=source, fps_arg=None if arg is None else Fraction(arg))
        fps, origin = cli.resolve_fps(cfg, info)
        return str(fps), origin.split(" ")[0]

    def test_matrix(self):
        # (source, -r, header) -> (fps, origin)
        cases = [
            ("arg", 24, self.HDR25, ("24", "-r")),        # explicit always wins (default)
            ("arg", None, self.HDR25, ("25", "EXR")),     # no -r: header before the 24 default
            ("arg", None, self.NONE, ("24", "default")),
            ("exr", 24, self.HDR25, ("25", "EXR")),       # header overrides -r
            ("exr", 30, self.NONE, ("30", "-r")),         # no header: -r as fallback
            ("exr", None, self.NONE, ("24", "default")),
            ("exr", None, None, ("24", "default")),       # non-EXR input
            ("arg_only", None, self.HDR25, ("24", "default")),   # header ignored
            ("arg_only", 25, self.NONE, ("25", "-r")),
            ("exr", None, self.KARMA, ("24000/1001", "EXR")),
        ]
        for source, arg, info, expected in cases:
            with self.subTest(source=source, arg=arg):
                self.assertEqual(self.fps(source, arg, info), expected)

    def test_header_values(self):
        from vfx_transcode.timecode import fps_from_attribs, fps_from_value
        self.assertEqual(fps_from_value(29.97), Fraction(30000, 1001))
        self.assertEqual(fps_from_value([24000, 1001]), Fraction(24000, 1001))
        self.assertEqual(fps_from_value(12.5), Fraction(25, 2))
        self.assertIsNone(fps_from_value("abc"))
        self.assertIsNone(fps_from_value(0))
        self.assertEqual(fps_from_attribs({"frameRate": "25", "x": 1}), (25, "frameRate"))
        self.assertEqual(fps_from_attribs({"frame_rate": 25.0}), (25, "frame_rate"))
        self.assertEqual(fps_from_attribs({"nuke/input/frame_rate": "23.976"})[0], Fraction(24000, 1001))
        self.assertEqual(fps_from_attribs({"frame_rate": 30, "FramesPerSecond": Rational("24/1")}),
                         (24, "FramesPerSecond"))     # FramesPerSecond preferred
        self.assertEqual(fps_from_attribs({"frame_rate_mode": 25}), (None, None))
        self.assertEqual(fps_from_attribs({"FramesPerSecond": "junk"}), (None, None))

    def test_cli(self):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"])
        cfg = cli.config_from_args(ns, PRESETS)
        self.assertEqual((cfg.fps_arg, cfg.fps_source, cfg.fps), (None, "arg", 24))
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-r", "23.976", "-fps_source", "exr"])
        cfg = cli.config_from_args(ns, PRESETS)
        self.assertEqual((cfg.fps_arg, cfg.fps_source), (Fraction(24000, 1001), "exr"))


class MetadataTests(unittest.TestCase):
    ATTRIBS = {"Artist": "bagel", "husk:command": "/opt/hfs/bin/husk ...", "compression": "zip",
               "oiio:ColorSpace": "raw", "worldToCamera": [[1, 0], [0, 1]],
               "renderTime": 44.7, "husk:render_stats": "x" * 5000, "FramesPerSecond": Rational("24/1")}

    def test_copy_filters(self):
        from vfx_transcode import metadata as md
        header = md.header_metadata(self.ATTRIBS)
        self.assertEqual(list(header), ["Artist", "husk:command", "renderTime", "FramesPerSecond"])
        self.assertEqual(md.select(header, ""), {})
        self.assertEqual(list(md.select(header, "all,-husk:*")), ["Artist", "renderTime", "FramesPerSecond"])
        self.assertEqual(list(md.select(header, "Artist, render*")), ["Artist", "renderTime"])
        self.assertEqual(header["FramesPerSecond"], "24/1")

    def test_pairs_and_expansion(self):
        from vfx_transcode import metadata as md
        pairs = md.parse_pairs(["title={clean_name} v2", "comment=a=b", "x={unknown}"])
        out = md.build({"Artist": "bagel", "title": "old"}, pairs, {"clean_name": "sh010"})
        self.assertEqual(out, {"Artist": "bagel", "title": "sh010 v2", "comment": "a=b", "x": "{unknown}"})
        with self.assertRaises(TranscodeError):
            md.parse_pairs(["novalue"])


class ReelNameTests(unittest.TestCase):
    def setUp(self):
        self.seq = type("S", (), {"clean_name": "sh010", "start": 1001, "end": 1010})()

    def reel(self, source, arg="", info=None, attr="owner"):
        cfg = _cfg(reelid_source=source, reelid=arg, reelid_attr=attr)
        return cli.resolve_reel_name(cfg, self.seq, info, "01:00:00:00")[0]

    def test_sources(self):
        owner = [{"attribs": {"Copyright": "A001C003"}}]      # OIIO's name for EXR "owner"
        self.assertEqual(self.reel("auto", "R1", owner), "R1")          # explicit first
        self.assertEqual(self.reel("auto", "", owner), "A001C003")
        self.assertEqual(self.reel("auto", "", [{"attribs": {}}]), "")  # nothing written
        self.assertEqual(self.reel("exr", "R1", owner), "A001C003")     # header first
        self.assertEqual(self.reel("exr", "R1", [{"attribs": {}}]), "R1")
        self.assertEqual(self.reel("arg", "", owner), "")
        self.assertEqual(self.reel("arg", "{clean_name}_R", owner), "sh010_R")   # keywords
        self.assertEqual(self.reel("off", "R1", owner), "")
        other = [{"attribs": {"pipe/reel": " B002 "}}]
        self.assertEqual(self.reel("exr", "", other, attr="pipe/reel"), "B002")
        self.assertEqual(self.reel("exr", "", [{"attribs": {"owner": "C3"}}]), "C3")  # raw name too

    def test_clean_camera_clip_name(self):
        from vfx_transcode import metadata as md
        icoi = "Z017_C001_20190920_R[00000-02773]"          # owner of the ICOI2 sample
        self.assertEqual(md.clean_reel_name(icoi), "Z017_C001_20190920_R")
        self.assertEqual(md.clean_reel_name("A001 [12]"), "A001")
        self.assertEqual(md.clean_reel_name("A[1]B"), "A[1]B")           # only a trailing range
        self.assertEqual(md.clean_reel_name(icoi, r"^(Z\d+_C\d+)"), "Z017_C001")
        self.assertEqual(md.clean_reel_name(icoi, r"C\d+"), "C001")       # whole match
        self.assertEqual(md.clean_reel_name(icoi, r"^nomatch"), icoi)
        info = [{"attribs": {"Copyright": icoi}}]
        self.assertEqual(self.reel("exr", "", info), "Z017_C001_20190920_R")
        self.assertEqual(self.reel("exr", "{clean_name}", [{"attribs": {"Copyright": "[1-2]"}}]),
                         "sh010")                                         # empty after cleaning

    def test_ffmpeg_args(self):
        cmd = ffmpeg.build_command(_cfg(ff_out_ext="mov"), "l.txt", "o.mov", "VF", "01:00:00:00",
                                   {}, "A001C003")
        i = cmd.index("-metadata:s:v:0")
        self.assertEqual(cmd[i + 1], "reel_name=A001C003")
        self.assertIn("reel_name=A001C003", cmd[i + 2:])
        self.assertIn("+use_metadata_tags", "".join(cmd))
        cmd = ffmpeg.build_command(_cfg(ff_out_ext="mov"), "l.txt", "o.mov", "VF", "01:00:00:00")
        self.assertNotIn("-metadata:s:v:0", cmd)


class MetadataPresetTests(unittest.TestCase):
    def parse(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_default_is_none(self):
        cfg = self.parse()
        self.assertEqual((cfg.metadata_preset, cfg.preset_tags, cfg.metadata_copy), ("", {}, ""))
        self.assertEqual((cfg.reelid_source, cfg.reelid_attr, cfg.timecode_source), ("auto", "owner", "counter"))

    def test_conform_preset_and_overrides(self):
        cfg = self.parse("-metadata_preset", "conform")
        self.assertEqual((cfg.timecode_source, cfg.reelid_source, cfg.reelid_attr), ("exr", "exr", "owner"))
        self.assertEqual(cfg.preset_tags, {"title": "{clean_name}"})
        cfg = self.parse("-metadata_preset", "conform", "-reelid_source", "off", "-timecode", "02:00:00:00")
        self.assertEqual((cfg.reelid_source, cfg.timecode_source), ("off", "arg"))   # command line wins
        cfg = self.parse("-metadata_preset", "archive", "-metadata_copy", "")
        self.assertEqual(cfg.metadata_copy, "")                                     # "" overrides the preset
        self.assertEqual(self.parse("-metadata_preset", "archive").metadata_copy, "all,-husk:*,-nuke/*,-MEDIA_PATH")
        self.assertEqual(self.parse("-metadata_preset", "none").metadata_preset, "")
        with self.assertRaises(TranscodeError):
            self.parse("-metadata_preset", "nope")

    def test_layer_order_and_removal(self):
        from vfx_transcode import metadata as md
        out = md.build({"title": "from exr", "Artist": "bagel"},
                       md.parse_pairs(["comment=", "extra=1"]), {"clean_name": "sh010"},
                       [{"vendor_id": "appl", "title": "enc"},
                        {"title": "{clean_name}", "comment": "preset"}])
        self.assertEqual(out, {"title": "sh010", "Artist": "bagel", "vendor_id": "appl", "extra": "1"})


class PresetFileTests(unittest.TestCase):
    def write(self, text):
        path = Path(self.tmp.name) / "p.json"
        path.write_text(text, encoding="utf-8")
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_file(self):
        self.assertEqual(PRESETS.default, "h264")
        self.assertIn("prores422hq", PRESETS.presets)
        self.assertEqual(PRESETS.default_burnin, "")          # "none" in the file
        self.assertEqual(PRESETS.get_burnin("client").size, "1.1%")
        self.assertIsNone(PRESETS.get_burnin("none"))
        for b in PRESETS.burnins.values():                    # every keyword is a known one
            for text in b.corners.values():
                for _lit, fieldname, _spec, _conv in __import__("string").Formatter().parse(text):
                    if fieldname:
                        self.assertIn(fieldname, ffmpeg.BURN_KEYWORDS)

    def test_old_key_names_still_load(self):
        from vfx_transcode.channels import PREFERRED_CHANNELS
        p = load_presets(self.write('{"default": "x", "presets": {"x": {"ext": "mp4", '
                                    '"encode": "{videofilter}"}}}'))
        self.assertEqual((p.default, p.burnins), ("x", {}))
        self.assertEqual(p.channel_preferences, PREFERRED_CHANNELS)   # built-in fallback

    def test_channel_preferences(self):
        from vfx_transcode.channels import PREFERRED_CHANNELS
        self.assertEqual(PRESETS.channel_preferences, PREFERRED_CHANNELS)  # shipped = built-in
        base = '{"encode_presets": {"x": {"ext": "mp4", "encode": "{videofilter}"}}, %s}'
        p = load_presets(self.write(base % '"channel_preferences": ["beauty.R, beauty.G,beauty.B", ["Y"]]'))
        self.assertEqual(p.channel_preferences, [["beauty.R", "beauty.G", "beauty.B"], ["Y"]])
        for bad in ('[]', '"R,G,B"', '[""]', '[["R", 1]]', '[5]'):
            with self.assertRaises(TranscodeError):
                load_presets(self.write(base % ('"channel_preferences": ' + bad)))

    def test_metadata_preset_errors(self):
        base = '{"encode_presets": {"x": {"ext": "mp4", "encode": "{videofilter}"%s}}%s}'
        p = load_presets(self.write(base % (', "metadata": {"vendor_id": "appl", "n": 5}',
                                            ', "metadata_presets": {"m": {"tags": {"a": "b"}, "reelid_source": "exr"}}')))
        self.assertEqual(p.get("x").metadata, {"vendor_id": "appl", "n": "5"})
        self.assertEqual(p.get_metadata_preset("m").reelid_source, "exr")
        for enc, extra in (('', ', "metadata_presets": {"m": {"tagz": {}}}'),             # unknown field
                           ('', ', "metadata_presets": {"m": {"reelid_source": "maybe"}}'),
                           ('', ', "metadata_presets": {"m": {"tags": {"a": [1]}}}'),
                           ('', ', "metadata_presets": {"m": {"reelid_pattern": "("}}'),
                           ('', ', "metadata_presets": {"none": {}}'),
                           ('', ', "default_metadata": "nope"'),
                           (', "metadata": "vendor_id=appl"', '')):
            with self.assertRaises(TranscodeError):
                load_presets(self.write(base % (enc, extra)))

    def test_burnin_errors(self):
        base = '{"encode_presets": {"x": {"ext": "mp4", "encode": "{videofilter}"}}, %s}'
        for bad in ('"burnin_presets": {"b": {"top": "x"}}',           # unknown field
                    '"burnin_presets": {"b": {"size": "big"}}',
                    '"burnin_presets": {"none": {"tl": "x"}}',         # reserved name
                    '"default_burnin": "nope"'):
            with self.assertRaises(TranscodeError):
                load_presets(self.write(base % bad))
        with self.assertRaises(TranscodeError):
            PRESETS.get_burnin("nope")

    def test_list_form_and_errors(self):
        p = load_presets(self.write('{"presets": {"x": {"ext": ".mkv", "notes": ["a", "b"], '
                                    '"encode": ["-c:v", "ffv1", "{videofilter}"]}}}'))
        self.assertEqual((p.default, p.get("x").ext, p.get("x").notes), ("x", "mkv", "a b"))
        for bad in ('{"presets": {"x": {"ext": "mp4", "encode": "-c:v libx264"}}}',   # no {videofilter}
                    '{"presets": {"x": {"encode": "{videofilter}"}}}',                # no ext
                    '{"presets": {}}', "{not json"):
            with self.assertRaises(TranscodeError):
                load_presets(self.write(bad))
        with self.assertRaises(TranscodeError):
            p.get("nope")


class CliTests(unittest.TestCase):
    def parse(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_timecode_source_rules(self):
        self.assertEqual(self.parse().timecode_source, "counter")
        self.assertEqual(self.parse("-timecode", "02:00:00:00").timecode_source, "arg")
        self.assertEqual(self.parse("-timecode_from_counter", "0").timecode_source, "arg")
        self.assertEqual(self.parse("-timecode_source", "exr").timecode_source, "exr")
        with self.assertRaises(TranscodeError):
            self.parse("-timecode", "1:00:00")

    def test_burn_preset_and_overrides(self):
        cfg = self.parse()
        self.assertEqual(set(cfg.burn.values()), {""})                       # default: none
        self.assertEqual(sorted(cfg.burn), sorted(["tl", "tc", "tr", "bl", "bc", "br", "wm"]))
        self.assertTrue(cfg.burn_font.endswith("DroidSansMono.ttf"))
        cfg = self.parse("-burn_preset", "review", "-burn_tl", "{clean_name} v2", "-burn_bl", "")
        self.assertEqual({k: v for k, v in cfg.burn.items() if v},
                         {"tl": "{clean_name} v2", "tr": "TC {tc}", "br": "{current_frame}"})
        self.assertEqual((cfg.burn_size, cfg.burn_preset), ("1.33%", "review"))
        cfg = self.parse("-burn_preset", "client")
        self.assertEqual(cfg.burn_size, "1.1%")
        self.assertEqual(self.parse("-burn_preset", "client", "-burn_size", "40").burn_size, "40")
        self.assertEqual(self.parse("-burn_size", "2%").burn_size, "2%")
        self.assertEqual(self.parse("-burn_size", "h/75").burn_size, "h/75")
        self.assertEqual(self.parse("-burn_br", "{current_frame}").burn["br"], "{current_frame}")
        with self.assertRaises(TranscodeError):
            self.parse("-burn_preset", "nope")

    def test_preset_flag_not_mistaken_for_presets_file(self):
        presets, err = cli.load_presets_for(["-preset", "h265", "-i", "x"])
        self.assertIsNone(err)

    def test_bad_reel_pattern(self):
        with self.assertRaises(TranscodeError):
            self.parse("-reelid_pattern", "(")

    def test_temp_location_default(self):
        self.assertEqual(self.parse().temp_location, "user")
        self.assertEqual(self.parse("-temp_location", "source").temp_location, "source")

    def test_old_style_switches(self):
        cfg = self.parse("-temp_keep", "1", "-write_exr_info", "1", "-v", "1", "-skipffmpeg", "0")
        self.assertTrue(cfg.temp_keep)
        self.assertTrue(cfg.write_exr_info)
        self.assertEqual(cfg.verbose, 1)          # int now; '1' <= 1 used to raise TypeError
        self.assertFalse(cfg.skip_ffmpeg)
        self.assertTrue(self.parse("-skipffmpeg").temp_keep)

    def test_empty_channel_means_automatic(self):
        self.assertEqual(self.parse().oiio_channel, [])
        self.assertEqual(self.parse().channel_preferences, PRESETS.channel_preferences)

    def test_preset_sets_extension(self):
        self.assertEqual(self.parse("-preset", "prores422hq").ff_out_ext, "mov")
        self.assertEqual(self.parse().preset.name, "h264")
        self.assertEqual(self.parse("-preset", "prores422hq", "-ff_out_ext", "mxf").ff_out_ext, "mxf")

    def test_temp_must_stay_inside_input_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            seq = type("S", (), {"folder": Path(tmp)})()
            for bad in ("/abs/path", "\\root", "C:rel", "C:/abs", "../up", "a/../../b"):
                with self.assertRaises(TranscodeError):
                    cli.prepare_work_dir(_cfg(temp=bad, temp_location="source"), seq)
            work, created = cli.prepare_work_dir(_cfg(temp=".tmp", temp_location="source"), seq)
            self.assertTrue(created and work.is_dir())
            self.assertEqual(work, Path(tmp) / ".tmp")

    def test_user_temp(self):
        seq = type("S", (), {"folder": Path("/nowhere"), "clean_name": "sh'ot 010"})()
        a, created = cli.prepare_work_dir(_cfg(), seq)             # default: user temp
        b, _ = cli.prepare_work_dir(_cfg(), seq)
        try:
            self.assertTrue(created and a.is_dir() and a != b)     # private folder per run
            self.assertEqual(a.parent, Path(tempfile.gettempdir()) / "vfx-transcode")
            self.assertTrue(a.name.startswith("sh_ot_010_"))
            (a / "partial.png").write_bytes(b"x")                 # e.g. a failed frame
            cli.cleanup([], a, True, whole_folder=True)
            self.assertFalse(a.exists())
        finally:
            cli.cleanup([], a, True, whole_folder=True)
            cli.cleanup([], b, True, whole_folder=True)

    def test_resolve_path(self):
        self.assertEqual(resolve_path("./oiio/oiiotool.exe", "/app"), str(Path("/app/oiio/oiiotool.exe")))
        self.assertEqual(resolve_path("../tools/ffmpeg", "/app/bin"), str(Path("/app/tools/ffmpeg")))
        self.assertEqual(resolve_path("C:/ffmpeg.exe", "/app"), "C:/ffmpeg.exe")


class SlateTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_slate_timecode(self):
        from vfx_transcode.slate import slate_timecode
        self.assertEqual(slate_timecode("00:00:41:17", 24), "00:00:41:16")
        self.assertEqual(slate_timecode("01:00:00:00", 24), "00:59:59:23")
        self.assertEqual(slate_timecode("00:00:00:00", 24), "00:00:00:00")   # never below zero
        self.assertEqual(slate_timecode("00:00:01;00", 30), "00:00:00;29")   # keeps the separator

    def test_parse_probe(self):
        from vfx_transcode.slate import FrameFormat, parse_probe
        self.assertEqual(parse_probe("  Stream #0:0: Video: png, rgb48be(pc), 1920x1080, 25 tbr"),
                         FrameFormat(1920, 1080, "rgb48be"))
        self.assertEqual(parse_probe("Stream #0:0: Video: mjpeg (Baseline), yuvj420p(pc, "
                                     "bt470bg/unknown/unknown), 2048x858 [SAR 1:1 DAR 1024:429]"),
                         FrameFormat(2048, 858, "yuvj420p"))
        self.assertEqual(parse_probe("Stream #0:0: Video: tiff, rgb24, 1280x721, 25 tbr"),
                         FrameFormat(1280, 721, "rgb24"))
        self.assertIsNone(parse_probe("No such file"))

    def test_pick_thumbnail(self):
        from vfx_transcode.slate import pick_thumbnail
        frames = [1001, 1002, 1003, 1005, 1006]
        available = {f: "f" for f in frames}
        self.assertEqual(pick_thumbnail("middle", frames, available), 1003)
        self.assertEqual(pick_thumbnail("first", frames, available), 1001)
        self.assertEqual(pick_thumbnail("last", frames, available), 1006)
        self.assertEqual(pick_thumbnail("1004", frames, available), 1003)   # hole: nearest
        self.assertIsNone(pick_thumbnail("none", frames, available))
        self.assertEqual(pick_thumbnail("middle", frames, {1006: "f"}), 1006)  # failed frames

    def test_fields_and_hide_empty(self):
        from vfx_transcode import metadata as md
        from vfx_transcode.slate import SlateSpec, apply_field_options, expand_all
        fields = apply_field_options({"Date": "{date}", "Artist": "{meta:Artist|}", "Codec": "x"},
                                     ["Codec=", "Notes=take 2", "Date=today"])
        self.assertEqual(list(fields.items()),
                         [("Date", "today"), ("Artist", "{meta:Artist|}"), ("Notes", "take 2")])
        spec = SlateSpec(title="{clean_name}", fields=fields)
        title, _sub, rows = expand_all(spec, {"clean_name": "sh010", "meta": {}}, md.expand)
        self.assertEqual(title, "sh010")
        self.assertEqual(rows, [("Date", "today"), ("Notes", "take 2")])    # no Artist: hidden
        spec = SlateSpec(fields=fields, hide_empty=False)
        self.assertEqual(len(expand_all(spec, {"meta": {}}, md.expand)[2]), 3)
        with self.assertRaises(TranscodeError):
            apply_field_options({}, ["no equals sign"])

    def test_layout_fits_and_shrinks(self):
        from vfx_transcode.slate import SlateSpec, layout
        spec = SlateSpec()
        rows = [("Row{}".format(i), "value") for i in range(8)]
        lay = layout(spec, 1920, 1080, "title", "", rows, True)
        self.assertTrue(all(t.y < 1080 * 0.93 for t in lay.texts))
        self.assertTrue(lay.thumb[0] + lay.thumb[2] <= 1920 * 0.95 + 1)
        many = [("Row{}".format(i), "value") for i in range(60)]
        small = layout(spec, 1920, 1080, "title", "", many, False)
        self.assertLess(small.texts[-1].size, lay.texts[-1].size)            # shrunk to fit
        self.assertTrue(all(t.y < 1080 for t in small.texts))
        long = layout(spec, 1920, 1080, "t", "", [("Notes", "word " * 80)], True)
        self.assertGreater(len([t for t in long.texts if t.x == long.texts[-1].x]), 2)  # wrapped

    def test_slate_command(self):
        from vfx_transcode.slate import FrameFormat, SlateSpec, build_slate_command, canvas_size, layout
        fmt = FrameFormat(1024, 429, "rgb48be")
        cw, ch = canvas_size(fmt, Fraction(2))
        self.assertEqual((cw, ch), (2048, 429))                   # laid out at display width
        spec = SlateSpec()
        lay = layout(spec, cw, ch, "it's 50%", "", [("Notes", "a:b, c;d")], True)
        cmd = build_slate_command("ffmpeg", spec, fmt, Fraction(2), lay, "t.png", "s.png")
        source = cmd[cmd.index("-f") + 3]
        self.assertIn("s=2048x429", source)
        self.assertTrue(source.endswith("format=rgb24"))        # odd heights stay odd
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("scale=w=1024:h=429", graph)               # squeezed back to stored pixels
        self.assertTrue(graph.endswith("format=pix_fmts=rgb48be"))
        self.assertIn("[thumb]", graph)
        self.assertIn("50" + "\\" * 4 + "%", graph)             # % escaped for drawtext
        no_thumb = build_slate_command("ffmpeg", spec, FrameFormat(1920, 1080, "rgb24"), Fraction(1),
                                       layout(spec, 1920, 1080, "t", "", [], False), None, "s.png")
        self.assertEqual(no_thumb.count("-i"), 1)
        self.assertNotIn("scale=w=", no_thumb[no_thumb.index("-filter_complex") + 1])

    def test_movie_size(self):
        from vfx_transcode.slate import FrameFormat, movie_size
        self.assertEqual(movie_size(FrameFormat(1280, 721, "rgb24"), Fraction(1), Fraction(1), 0),
                         (1280, 722))
        self.assertEqual(movie_size(FrameFormat(2048, 858, "rgb24"), Fraction(2), Fraction(1), 0),
                         (2048, 428))
        self.assertEqual(movie_size(FrameFormat(4096, 2160, "rgb24"), Fraction(1), Fraction(1), 1920),
                         (1920, 1012))

    def test_cli(self):
        self.assertIsNone(self._cfg().slate)                    # default_slate none
        cfg = self._cfg("-slate", "generic", "-slate_field", "Notes=v2", "-slate_field", "Codec=",
                        "-slate_thumbnail", "first", "-slate_size", "30", "-slate_title", "T")
        self.assertEqual(cfg.slate.name, "generic")
        self.assertEqual(list(cfg.slate.fields)[-1], "Notes")
        self.assertNotIn("Codec", cfg.slate.fields)
        self.assertEqual((cfg.slate.thumbnail, cfg.slate.size, cfg.slate.title), ("first", "30", "T"))
        self.assertFalse(cfg.slate.burnin)
        plain = self._cfg("-slate_field", "Shot={clean_name}")  # slate options alone: plain slate
        self.assertEqual(list(plain.slate.fields), ["Shot"])
        self.assertIsNone(self._cfg("-slate", "none", "-slate_field", "A=b").slate)
        for bad in (["-slate", "nope"], ["-slate", "generic", "-slate_thumbnail", "centre"],
                    ["-slate", "generic", "-slate_size", "big"]):
            with self.assertRaises(TranscodeError):
                self._cfg(*bad)

    def test_presets(self):
        self.assertEqual(PRESETS.default_slate, "")
        self.assertIn("generic", PRESETS.slates)
        self.assertIn("Slate presets", PRESETS.describe())
        with tempfile.TemporaryDirectory() as tmp:
            base = json.loads((REPO / "presets.json").read_text(encoding="utf-8"))
            for broken in ({"x": {"colour": "red"}}, {"x": {"thumbnail": "centre"}},
                           {"x": {"background": "not a colour!"}}, {"none": {}}):
                data = dict(base, slate_presets=broken)
                path = Path(tmp) / "p.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(TranscodeError):
                    load_presets(path)
            data = dict(base, default_slate="missing")
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(TranscodeError):
                load_presets(path)

    def test_burnin_counters_start_at_slate(self):
        ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24), lead_frames=1,
                                 movie_timecode="00:00:41:16")
        options = dict(ffmpeg.expand_burn_template("{current_frame}", ctx))
        self.assertEqual(options["start_number"], "1000")
        self.assertEqual(dict(ffmpeg.expand_burn_template("TC {tc}", ctx))["timecode"], "00:00:41:16")
        self.assertIn("00:00:41:17", dict(ffmpeg.expand_burn_template("{timecode}", ctx))["text"])
        style = ffmpeg.DEFAULT_STYLE
        self.assertIn("enable=gte(n\\,1)", ffmpeg.build_drawtext("{current_frame}", "br", ctx, "", style))
        ctx.skip_lead = False
        self.assertNotIn("enable", ffmpeg.build_drawtext("{current_frame}", "br", ctx, "", style))
        plain = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24))
        self.assertEqual(dict(ffmpeg.expand_burn_template("{current_frame}", plain))["start_number"], "1001")
        self.assertNotIn("enable", ffmpeg.build_drawtext("x", "br", plain, "", style))


class SlateStyleTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def _layout(self, **style):
        from vfx_transcode.slate import SlateSpec, layout
        spec = SlateSpec(**style)
        rows = [("Date", "2026-09-29"), ("Frames", "1001 - 1010")]
        return layout(spec, 1920, 1080, "sh010", "comp", rows, True)

    def test_centred_by_default(self):
        lay = self._layout()
        top = min(t.y for t in lay.texts)
        bottom = max(lay.thumb[1] + lay.thumb[3], max(t.y + t.size for t in lay.texts))
        self.assertAlmostEqual(top, 1080 - bottom, delta=0.03 * 1080)    # same space above and below
        top_aligned = self._layout(valign="top")
        self.assertLess(min(t.y for t in top_aligned.texts), top)

    def test_scale(self):
        full, half = self._layout(), self._layout(scale=0.5)
        for h, f in zip(half.texts, full.texts):
            self.assertAlmostEqual(h.size, f.size / 2, delta=1)
        self.assertAlmostEqual(half.thumb[2], full.thumb[2] / 2, delta=2)
        # the half-size layout sits in the middle of the picture
        xs = [t.x for t in half.texts]
        self.assertGreater(min(xs), 1920 * 0.25)
        self.assertLess(half.thumb[0] + half.thumb[2], 1920 * 0.75 + 2)
        self.assertEqual((half.width, half.height), (1920, 1080))          # canvas unchanged
        pixels = self._layout(size="40", scale=0.5)
        self.assertEqual(pixels.texts[-1].size, 20)                        # pixel sizes scale too

    def test_sizes_and_colours(self):
        lay = self._layout(title_size="10%", subtitle_size="20", size="30",
                           label_color="#444444", value_color="yellow", title_color="red",
                           subtitle_color="blue")
        by_text = {t.text: t for t in lay.texts}
        self.assertEqual((by_text["sh010"].size, by_text["sh010"].color), (108, "red"))
        self.assertEqual((by_text["comp"].size, by_text["comp"].color), (20, "blue"))
        self.assertEqual((by_text["Date"].size, by_text["Date"].color), (30, "#444444"))
        self.assertEqual(by_text["2026-09-29"].color, "yellow")

    def test_parse(self):
        from vfx_transcode.slate import parse_scale, parse_valign, style_values
        self.assertEqual([parse_scale(v) for v in ("50%", "50", "0.5", "1", "100%")],
                         [0.5, 0.5, 0.5, 1.0, 1.0])
        for bad in ("0", "150%", "big", "2%"):
            with self.assertRaises(TranscodeError):
                parse_scale(bad)
        self.assertEqual(parse_valign("Centre"), "center")
        with self.assertRaises(TranscodeError):
            parse_valign("bottom")
        self.assertEqual(style_values({"color": "red"}, "x"), {"title_color": "red", "value_color": "red"})
        with self.assertRaises(TranscodeError):
            style_values({"label_color": "not a colour"}, "x")

    def test_cli(self):
        cfg = self._cfg("-slate", "generic", "-slate_scale", "50%", "-slate_valign", "top",
                        "-slate_title_size", "4%", "-slate_subtitle_size", "30",
                        "-slate_size", "2%", "-slate_value_color", "#eeeeee",
                        "-slate_label_color", "#555555",
                        "-slate_style", "title_color=yellow,background=#000000",
                        "-slate_style", "label_color=#666666")
        sl = cfg.slate
        self.assertEqual((sl.scale, sl.valign, sl.title_size, sl.subtitle_size, sl.size),
                         (0.5, "top", "4%", "30", "2%"))
        self.assertEqual((sl.value_color, sl.label_color, sl.title_color, sl.background),
                         ("#eeeeee", "#666666", "yellow", "#000000"))      # -slate_style wins
        self.assertEqual(self._cfg("-slate_scale", "80%").slate.scale, 0.8)   # plain slate
        for bad in (["-slate", "generic", "-slate_style", "font=x.ttf"],
                    ["-slate", "generic", "-slate_style", "nope=1"],
                    ["-slate", "generic", "-slate_valign", "bottom"]):
            with self.assertRaises(TranscodeError):
                self._cfg(*bad)

    def test_preset_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = json.loads((REPO / "presets.json").read_text(encoding="utf-8"))
            path = Path(tmp) / "p.json"
            data = dict(base, slate_presets={"x": {"scale": "60%", "valign": "top", "color": "red",
                                                   "label_color": "#333333", "title_size": 50}})
            path.write_text(json.dumps(data), encoding="utf-8")
            sl = load_presets(path).slates["x"]
            self.assertEqual((sl.scale, sl.valign, sl.title_color, sl.value_color, sl.label_color,
                              sl.title_size), (0.6, "top", "red", "red", "#333333", "50"))
            data["slate_presets"] = {"x": {"scale": "300%"}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(TranscodeError):
                load_presets(path)


class FrameColourTagTests(unittest.TestCase):
    def _filter(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        cfg = cli.config_from_args(ns, PRESETS)
        ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24))
        return ffmpeg.build_video_filter(cfg, ctx, 0)

    def test_frames_tagged_like_the_encoder(self):
        self.assertIn("setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
                      self._filter())
        self.assertIn("color_trc=iec61966-2-1", self._filter("-co", "out_srgb"))
        self.assertIn("color_trc=iec61966-2-1", self._filter("-display", "sRGB - Display"))
        self.assertNotIn("setparams", self._filter("-ff_encode", "{videofilter} -c:v libx264"))


class SlateRuleTests(unittest.TestCase):
    def _graph(self, **style):
        from vfx_transcode.slate import FrameFormat, SlateSpec, build_slate_command, layout
        spec = SlateSpec(**style)
        lay = layout(spec, 1920, 1080, "t", "", [("A", "b")], True)
        cmd = build_slate_command("ffmpeg", spec, FrameFormat(1920, 1080, "rgb24"), Fraction(1),
                                  lay, "t.png", "s.png")
        return cmd[cmd.index("-filter_complex") + 1]

    def test_no_thumbnail_frame_and_rule_off_by_default(self):
        graph = self._graph()
        self.assertNotIn("pad=", graph)
        self.assertNotIn("drawbox", graph)
        self.assertIn("drawbox", self._graph(rule=True))

    def test_rule_options(self):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-slate", "generic", "-slate_rule"])
        self.assertTrue(cli.config_from_args(ns, PRESETS).slate.rule)
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-slate", "generic",
                                                   "-slate_style", "rule=1"])
        self.assertTrue(cli.config_from_args(ns, PRESETS).slate.rule)
        self.assertFalse(PRESETS.slates["generic"].rule)


class LetterboxTests(unittest.TestCase):
    def test_parse(self):
        from vfx_transcode.letterbox import parse_ratio, parse_rgba
        for text, value in (("2.39", 2.39), ("2.39:1", 2.39), ("16:9", 16 / 9), ("16/9", 16 / 9),
                            ("1.85", 1.85), ("239/100", 2.39)):
            self.assertAlmostEqual(parse_ratio(text), value)
        for bad in ("wide", "0", "2:0", "-1"):
            with self.assertRaises(TranscodeError):
                parse_ratio(bad)
        self.assertEqual(parse_rgba("0,0,0,128", "x"), (0, 0, 0, 128))
        self.assertEqual(parse_rgba("255, 0, 0", "x"), (255, 0, 0, 255))
        self.assertEqual(parse_rgba("#ff000080", "x"), (255, 0, 0, 128))
        for bad in ("0,0", "0,0,0,256", "red", "1,2,3,4,5"):
            with self.assertRaises(TranscodeError):
                parse_rgba(bad, "x")

    def test_plan(self):
        from vfx_transcode.letterbox import LetterboxSpec, plan
        lb = plan(LetterboxSpec(ratio=2.39), 1920, 1080)
        self.assertEqual((lb.kind, lb.bar), ("letterbox", 138))        # (1080 - 1920/2.39) / 2
        self.assertEqual([(b.x, b.y, b.w, b.h) for b in lb.boxes],
                         [("0", "0", "iw", "138"), ("0", "ih-138", "iw", "138")])
        self.assertEqual(lb.boxes[0].color, "0x000000@1")
        pb = plan(LetterboxSpec(ratio=4 / 3), 1920, 1080)
        self.assertEqual((pb.kind, pb.bar), ("pillarbox", 240))
        self.assertEqual(pb.boxes[1].x, "iw-240")
        self.assertEqual(plan(LetterboxSpec(ratio=16 / 9), 1920, 1080).kind, "none")
        # anamorphic: 2048x858 at 2:1 displays 4.77:1, so 2.39 is narrower -> pillarbox
        ana = plan(LetterboxSpec(ratio=2.39), 2048, 858, Fraction(2))
        self.assertEqual((ana.kind, ana.bar), ("pillarbox", 511))
        lined = plan(LetterboxSpec(ratio=2.39, fill=(0, 0, 0, 128), line=4,
                                   line_color=(255, 0, 0, 255)), 1920, 1080)
        self.assertEqual(len(lined.boxes), 4)
        self.assertEqual((lined.boxes[2].y, lined.boxes[2].h), ("134", "4"))    # inner edge
        self.assertEqual(lined.boxes[2].color, "0xFF0000@1")
        self.assertEqual(lined.boxes[0].color, "0x000000@0.502")

    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_cli_and_filter(self):
        from vfx_transcode.letterbox import plan
        self.assertIsNone(self._cfg().letterbox)
        self.assertIsNone(self._cfg("-letterbox_ratio", "2").letterbox)        # off: ignored
        cfg = self._cfg("-letterbox", "-letterbox_ratio", "1.85", "-letterbox_color", "0,0,0,100",
                        "-letterbox_line", "2", "-letterbox_line_color", "#ffffff", "-burn_tl", "x")
        self.assertAlmostEqual(cfg.letterbox.ratio, 1.85)
        self.assertEqual((cfg.letterbox.fill, cfg.letterbox.line), ((0, 0, 0, 100), 2))
        cfg.letterbox_plan = plan(cfg.letterbox, 1920, 1080)
        ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24), lead_frames=1)
        vf = ffmpeg.build_video_filter(cfg, ctx, 0)
        self.assertLess(vf.index("drawbox"), vf.index("drawtext"))            # under the burn-ins
        self.assertIn("enable=gte(n\\,1)", vf[vf.index("drawbox"):vf.index("drawtext")])  # not on the slate
        self.assertEqual(self._cfg("-letterbox").letterbox.ratio, 2.39)        # default ratio
        with self.assertRaises(TranscodeError):
            self._cfg("-letterbox", "-letterbox_line", "-1")


class NameKeywordTests(unittest.TestCase):
    def test_splits(self):
        from vfx_transcode.names import name_tokens
        t = name_tokens("AA_BBB_C_DD")
        self.assertEqual([t["namesplit{}".format(i)] for i in range(1, 6)], ["AA", "BBB", "C", "DD", ""])
        self.assertEqual([t["namesplit-{}".format(i)] for i in range(1, 6)], ["DD", "C", "BBB", "AA", ""])
        self.assertEqual(t["namesplit9"], "")
        self.assertEqual(t["namesplit-9"], "")
        self.assertEqual(name_tokens("")["namesplit1"], "")

    def test_version(self):
        from vfx_transcode.names import name_tokens, parse_version
        self.assertEqual(parse_version("sh010_comp_v007"), ("007", "7"))
        self.assertEqual(parse_version("sh010_comp_v000"), ("000", "0"))
        self.assertEqual(parse_version("sh010_comp_v120"), ("120", "120"))
        self.assertEqual(parse_version("sh010_comp"), ("", ""))
        self.assertEqual(parse_version("v001_plate_v003"), ("003", "3"))           # last one wins
        self.assertEqual(parse_version("sh010_comp_v0042", r"_v(\d+)"), ("0042", "42"))
        self.assertEqual(parse_version("sh010.V12", r"[vV]\d+"), ("V12", "V12"))  # no group: match
        self.assertEqual(name_tokens("a_v005")["version_number"], "5")

    def test_used_everywhere(self):
        from vfx_transcode import metadata as md
        from vfx_transcode.names import name_tokens
        tokens = name_tokens("sh010_comp_v007")
        ctx = ffmpeg.BurnContext("sh010_comp_v007", 1001, 1010, "00:00:41:17", Fraction(24),
                                 tokens=tokens)
        text = dict(ffmpeg.expand_burn_template("{namesplit1} {namesplit-2} v{version_number}", ctx))["text"]
        self.assertEqual(text, "sh010 comp v7")
        self.assertEqual(md.expand("{namesplit-1}/{version_string}", tokens), "v007/007")
        self.assertEqual(md.expand("{version_number:03d}", tokens), "7")            # no crash

    def test_cli(self):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-version_regex", r"_V(\d+)"])
        self.assertEqual(cli.config_from_args(ns, PRESETS).version_regex, r"_V(\d+)")
        with self.assertRaises(TranscodeError):
            ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-version_regex", "(("])
            cli.config_from_args(ns, PRESETS)

    def test_container_from_output_name(self):
        def ext(*argv):
            ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
            return cli.config_from_args(ns, PRESETS).ff_out_ext
        self.assertEqual(ext(), "mp4")
        self.assertEqual(ext("-o", "out/x.mov"), "mov")
        self.assertEqual(ext("-o", "x.mov", "-ff_out_ext", "mxf"), "mxf")


class BurnMarginTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_parse_and_expression(self):
        from vfx_transcode.burnin import margin_expression, parse_margin
        self.assertEqual([parse_margin(v) for v in ("20", 20, "2%", "0", 0, "h/50")],
                         ["20", "20", "2%", "0", "0", "h/50"])
        for bad in ("-5", "wide", "200%"):
            with self.assertRaises(TranscodeError):
                parse_margin(bad)
        self.assertEqual(margin_expression("2%", "left"), "w*0.02")     # sides: of the width
        self.assertEqual(margin_expression("2%", "top"), "h*0.02")      # top/bottom: of the height
        self.assertEqual(margin_expression("30", "right"), "30")

    def test_positions(self):
        margins = {"top": "40", "bottom": "3%", "left": "0", "right": "25"}
        self.assertEqual(ffmpeg.burn_position("tl", margins), ("0", "40"))
        self.assertEqual(ffmpeg.burn_position("tc", margins), ("(w-tw)/2", "40"))
        self.assertEqual(ffmpeg.burn_position("br", margins), ("w-25-tw", "h-(h*0.03)-lh"))
        self.assertEqual(ffmpeg.burn_position("bl", margins, 3), ("0", "h-(h*0.03)-3*lh"))

    def test_layers(self):
        cfg = self._cfg()
        self.assertEqual(cfg.burn_margins, {"top": "1.5%", "bottom": "1.5%", "left": "1%", "right": "1%"})
        cfg = self._cfg("-burn_margin", "30", "-burn_margin_left", "5%")
        self.assertEqual(cfg.burn_margins, {"top": "30", "bottom": "30", "left": "5%", "right": "30"})
        with tempfile.TemporaryDirectory() as tmp:
            base = json.loads((REPO / "presets.json").read_text(encoding="utf-8"))
            base["burnin_presets"]["m"] = {"tl": "x", "margin": "2%", "margin_bottom": 50}
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            presets = load_presets(path)
            self.assertEqual(presets.burnins["m"].margins,
                             {"top": "2%", "bottom": "50", "left": "2%", "right": "2%"})
            ns = cli.build_parser(presets).parse_args(["-i", "x.1001.exr", "-burn_preset", "m",
                                                       "-burn_margin_top", "0"])
            cfg = cli.config_from_args(ns, presets)
            self.assertEqual(cfg.burn_margins, {"top": "0", "bottom": "50", "left": "2%", "right": "2%"})
            ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24))
            vf = ffmpeg.build_video_filter(cfg, ctx, 0)
            self.assertIn("x=w*0.02:y=0", vf)
            base["burnin_presets"]["m"] = {"tl": "x", "margin_middle": 5}
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaises(TranscodeError):
                load_presets(path)


class WatermarkTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_position_ignores_margins(self):
        self.assertEqual(ffmpeg.burn_position("wm", {"top": "300", "left": "300"}), ("(w-tw)/2", "(h-lh)/2"))
        self.assertEqual(ffmpeg.burn_position("wm", None, 2), ("(w-tw)/2", "(h-2*lh)/2"))
        vf = ffmpeg.build_drawtext(r"A\nB", "wm", _ctx(), "", ffmpeg.DEFAULT_STYLE)
        self.assertIn("text_align=C", vf)

    def test_own_size_and_opacity(self):
        from vfx_transcode.burnin import BurnStyle, resolve_styles
        styles = resolve_styles(BurnStyle(size="1.1%", opacity=0.8, color="yellow", box=True))
        self.assertEqual((styles["wm"].size, styles["wm"].opacity), ("8%", 0.25))
        self.assertEqual((styles["wm"].color, styles["wm"].box), ("yellow", True))   # inherited
        self.assertEqual(styles["tl"].size, "1.1%")
        styles = resolve_styles(None, {"wm": BurnStyle(size="12%")}, BurnStyle(opacity=0.5),
                                {"wm": BurnStyle(opacity=0.1)})
        self.assertEqual((styles["wm"].size, styles["wm"].opacity), ("12%", 0.1))

    def test_cli_and_preset(self):
        cfg = self._cfg("-burn_wm", "DRAFT", "-burn_size", "2%")
        self.assertEqual(cfg.burn["wm"], "DRAFT")
        self.assertEqual(cfg.burn_styles["wm"].size, "8%")
        cfg = self._cfg("-burn_wm", "DRAFT", "-burn_style", "wm:size=5%,opacity=0.5,color=red")
        self.assertEqual((cfg.burn_styles["wm"].size, cfg.burn_styles["wm"].opacity,
                          cfg.burn_styles["wm"].color), ("5%", 0.5, "red"))
        cfg = self._cfg("-burn_preset", "confidential")
        self.assertEqual(cfg.burn["wm"], "CONFIDENTIAL")
        ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24), lead_frames=1)
        vf = ffmpeg.build_video_filter(cfg, ctx, 0)
        wm = [f for f in vf.split(",drawtext") if "CONFIDENTIAL" in f][0]
        self.assertIn("fontsize=h*0.08", wm)
        self.assertIn("white@0.25", wm)
        self.assertIn("enable=gte(n", wm)                                      # not on the slate


class AlphaTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_find_alpha(self):
        from vfx_transcode.channels import ChannelPick, find_alpha
        info = [{"channelnames": ["rgba.R", "rgba.G", "rgba.B", "rgba.A", "Z"]},
                {"channelnames": ["beauty.R", "beauty.G", "beauty.B", "beauty.A"]}]
        self.assertEqual(find_alpha(ChannelPick(0, ["rgba.R", "rgba.G", "rgba.B", "rgba.A"], "p"), info), "rgba.A")
        self.assertEqual(find_alpha(ChannelPick(0, ["rgba.R", "rgba.G", "rgba.B"], "p"), info), "rgba.A")
        self.assertEqual(find_alpha(ChannelPick(1, ["beauty.R", "beauty.G", "beauty.B"], "p"), info), "beauty.A")
        self.assertIsNone(find_alpha(ChannelPick(0, ["Z"], "p"), info))
        pick = ChannelPick(0, ["rgba.R", "rgba.G", "rgba.B", "rgba.A"], "p")
        self.assertEqual(str(pick), "rgba.R,rgba.G,rgba.B")                  # dropped by default
        pick.alpha = "rgba.A"
        self.assertEqual(str(pick), "rgba.R,rgba.G,rgba.B,rgba.A")
        mono = ChannelPick(0, ["Y", "A"], "p", alpha="A")
        self.assertEqual(str(mono), "Y,Y,Y,A")

    def test_oiiotool_order(self):
        from vfx_transcode.channels import ChannelPick
        cfg = self._cfg("-preset", "prores4444", "-alpha", "keep", "-width", "1280")
        pick = ChannelPick(0, ["R", "G", "B", "A"], "p", alpha="A")
        args = oiio.process_args(cfg, pick)
        order = [args.index(a) for a in ("--ch", "--resize", "--unpremult", "--colorconvert")]
        self.assertEqual(order, sorted(order))                              # resize premultiplied
        self.assertEqual(args.count("--resize"), 1)
        plain = oiio.process_args(self._cfg("-width", "1280"), ChannelPick(0, ["R", "G", "B"], "p"))
        self.assertNotIn("--unpremult", plain)
        self.assertLess(plain.index("--colorconvert"), plain.index("--resize"))   # unchanged

    def test_validation(self):
        self.assertEqual(self._cfg().alpha, "drop")
        cfg = self._cfg("-preset", "prores4444", "-alpha", "keep", "-alpha_premult")
        self.assertEqual((cfg.alpha, cfg.alpha_premult), ("keep", True))
        with self.assertRaises(TranscodeError):
            self._cfg("-alpha", "keep")                                      # h264: no alpha
        with self.assertRaises(TranscodeError):
            self._cfg("-preset", "prores4444", "-alpha", "keep", "-mezzanine", "jpg")
        self._cfg("-alpha", "keep", "-ff_encode", "-pix_fmt yuva420p {videofilter} -c:v libvpx-vp9")

    def test_encode_and_filter(self):
        cfg = self._cfg("-preset", "prores4444", "-alpha", "keep", "-alpha_premult")
        ctx = ffmpeg.BurnContext("sh", 1001, 1010, "00:00:41:17", Fraction(24))
        self.assertIn("yuv444p10le", ffmpeg.encode_args(cfg, "vf"))            # no alpha found yet
        cfg.alpha_active = True
        args = ffmpeg.encode_args(cfg, "vf")
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuva444p10le")
        vf = ffmpeg.build_video_filter(cfg, ctx, 0)
        self.assertTrue(vf.startswith("format=pix_fmts=gbrap16le,premultiply=inplace=1,"))
        cfg.alpha_premult = False
        self.assertNotIn("premultiply", ffmpeg.build_video_filter(cfg, ctx, 0))

    def test_letterbox_with_alpha(self):
        from vfx_transcode.letterbox import LetterboxSpec, filters, plan
        lb = plan(LetterboxSpec(ratio=2.39, fill=(0, 0, 0, 128)), 1920, 1080)
        plain = filters(lb, 1)
        self.assertTrue(all(f.startswith("drawbox") for f in plain))
        graph = filters(lb, 1, alpha=True)
        self.assertEqual(len(graph), 1)
        g = graph[0]
        self.assertTrue(g.startswith("format=pix_fmts=yuva444p10le[lbm0];"))
        self.assertIn("s=1936x138", g)                                        # full width + spare
        self.assertIn("y=main_h-138", g)
        self.assertIn("[lbm0][lbs0]overlay=", g)
        self.assertFalse(g.endswith("]"))                                     # chain continues
        self.assertEqual(g.count("overlay="), 2)

    def test_has_alpha(self):
        from vfx_transcode.slate import has_alpha
        for fmt, expected in (("rgba", True), ("rgba64be", True), ("ya8", True), ("gbrap16le", True),
                              ("yuva444p10le", True), ("rgb24", False), ("rgb48be", False),
                              ("yuv420p", False), ("gray", False)):
            self.assertEqual(has_alpha(fmt), expected, fmt)


class OiioTransformTests(unittest.TestCase):
    PRESET = {"name": "p", "description": "d", "subs": [
        {"name": "uhd", "conditions": [{"left": "{input_width}x{input_height}", "op": "==", "right": "3840x2160"}],
         "transforms": ["--rangecompress", "--resize 1920x1080", "--rangeexpand"]},
        {"name": "239", "conditions": [{"left": "{input_width}", "op": "==", "right": "4080"},
                                       {"left": "{input_height}", "op": "==", "right": "2160"}],
         "transforms": ["--cut 3840x2160+120+0", "--resize 1920x1080"]},
        {"name": "not-anamorphic", "conditions": [{"left": "{input_par}", "op": "!=", "right": "2"},
                                                  {"left": "{meta:owner|}", "op": "!=", "right": ""}],
         "transforms": ["--resize {namesplit1}x0"]},
        {"name": "other", "conditions": [], "transforms": ["--resize 0x1080", "--crop 1920x1080"]}]}

    def _pick(self, width, height, par=1, owner="", name="1280_x_v001"):
        from vfx_transcode import metadata as md
        from vfx_transcode.names import name_tokens
        from vfx_transcode.transforms import input_tokens, parse_transform_presets, select, transform_args
        preset = parse_transform_presets([self.PRESET], "test")["p"]
        context = dict(name_tokens(name))
        context["meta"] = {"owner": owner} if owner else {}
        context.update(input_tokens([{"width": width, "height": height}], Fraction(par)))
        sub, _details = select(preset, context, md.expand)
        return (sub.name, transform_args(sub, context, md.expand)) if sub else (None, None)

    def test_first_match_wins(self):
        self.assertEqual(self._pick(3840, 2160), ("uhd", ["--rangecompress", "--resize", "1920x1080",
                                                           "--rangeexpand"]))
        self.assertEqual(self._pick(4080, 2160)[0], "239")
        self.assertEqual(self._pick(4080, 1716)[0], "other")        # AND: one condition fails
        self.assertEqual(self._pick(2048, 858, owner="A001")[1], ["--resize", "1280x0"])  # keywords
        self.assertEqual(self._pick(2048, 858, par=2, owner="A001")[0], "other")        # != fails

    def test_input_tokens(self):
        from vfx_transcode.transforms import input_tokens, number_text
        t = input_tokens([{"width": 4400, "height": 2300, "full_width": 4096, "full_height": 2160}],
                         Fraction(4, 3))
        self.assertEqual((t["input_width"], t["input_height"], t["input_data_width"], t["input_par"]),
                         ("4096", "2160", "4400", "1.3333"))
        self.assertEqual([number_text(v) for v in (Fraction(2), Fraction(1), Fraction(3, 2))],
                         ["2", "1", "1.5"])

    def test_no_match(self):
        from vfx_transcode import metadata as md
        from vfx_transcode.transforms import parse_transform_presets, select
        preset = parse_transform_presets([{"name": "x", "subs": [{"name": "a", "conditions": [
            {"left": "{input_width}", "op": "==", "right": "1"}], "transforms": []}]}], "t")["x"]
        self.assertEqual(select(preset, {"input_width": "2"}, md.expand), (None, []))

    def test_parse_errors(self):
        from vfx_transcode.transforms import parse_transform_presets
        good = {"name": "x", "subs": [{"name": "a", "transforms": ["--resize 10x10"]}]}
        parse_transform_presets([good], "t")
        for bad in ([{"subs": good["subs"]}],                                      # no name
                    [good, good],                                                  # duplicate
                    [{"name": "x", "subs": []}],                                   # no subs
                    [{"name": "x", "subs": [{"conditions": [{"left": "a", "op": ">", "right": "b"}]}]}],
                    [{"name": "x", "subs": [{"conditions": [{"left": "a", "op": "=="}]}]}],
                    [{"name": "x", "subs": [{"transforms": [1, 2]}]}],
                    [{"name": "none", "subs": good["subs"]}]):
            with self.assertRaises(TranscodeError):
                parse_transform_presets(bad, "t")

    def test_oiiotool_args_and_ffmpeg_width(self):
        from vfx_transcode.channels import ChannelPick
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr", "-oiio_transform",
                                                   "mygreatproject_editorial", "-width", "1280"])
        cfg = cli.config_from_args(ns, PRESETS)
        self.assertEqual(cfg.oiio_transform.name, "mygreatproject_editorial")
        cfg.oiio_transform_args = ["--resize", "1920x1080"]
        args = oiio.process_args(cfg, ChannelPick(0, ["R", "G", "B"], "p"))
        self.assertEqual(args.count("--resize"), 1)                               # no -width resize
        self.assertLess(args.index("1920x1080"), args.index("--colorconvert"))    # scene-linear
        alpha = oiio.process_args(cfg, ChannelPick(0, ["R", "G", "B", "A"], "p", alpha="A"))
        self.assertLess(alpha.index("1920x1080"), alpha.index("--unpremult"))
        self.assertIsNone(PRESETS.get_transform("none"))
        with self.assertRaises(TranscodeError):
            PRESETS.get_transform("nope")


class ClfTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_colour_args(self):
        from vfx_transcode.channels import ChannelPick
        cfg = self._cfg("-clf", "./luts/{namesplit1}.clf", "-display", "sRGB - Display")
        self.assertEqual(cfg.clf, "./luts/{namesplit1}.clf")
        cfg.clf_path = "D:/luts/sh010.clf"
        args = oiio.process_args(cfg, ChannelPick(0, ["R", "G", "B"], "p"))
        self.assertEqual(args[args.index("--ociofiletransform") + 1], "D:/luts/sh010.clf")
        self.assertNotIn("--ociodisplay:from=ACEScg", " ".join(args))
        self.assertNotIn("--colorconvert", args)
        self.assertEqual(cli.describe_colour(cfg), 'CLF "D:/luts/sh010.clf"')
        cfg.clf_path = ""
        with self.assertRaises(ValueError):
            oiio.colour_args(cfg)                       # never silently without the CLF

    def test_resolve(self):
        from vfx_transcode.names import name_tokens
        from vfx_transcode.sequence import Sequence
        with tempfile.TemporaryDirectory() as tmp:
            lut = Path(tmp) / "sh010" / "DJI_comp.clf"
            lut.parent.mkdir()
            lut.write_text("<ProcessList/>")
            cfg = self._cfg("-clf", str(Path(tmp)) + "/{namesplit1}/{meta:camera}_{namesplit2}.clf")
            cfg.name_tokens = name_tokens("sh010_comp_v003")
            cfg.header_attribs = {"camera": "DJI"}
            seq = Sequence(Path(tmp), "sh010_comp_v003.", ".exr", 4, [1001, 1002])
            info = [{"width": 1920, "height": 1080}]
            self.assertEqual(Path(cli.resolve_clf(cfg, seq, info)), lut)
            cfg.clf = str(Path(tmp)) + "/{namesplit1}/{nope}.clf"
            with self.assertRaises(TranscodeError) as err:
                cli.resolve_clf(cfg, seq, info)
            self.assertIn("{nope}", str(err.exception))


class OutputPathTests(unittest.TestCase):
    def _resolve(self, template, ext="mp4", folder=None):
        from vfx_transcode.sequence import Sequence
        cfg = Config(input="x", output=template, ff_out_ext=ext)
        seq = Sequence(Path(folder or "/shows/abc/sh010/comp/v003"), "sh010_comp_v003.", ".exr", 4,
                       (1001, 1002))
        context = {"clean_name": "sh010_comp_v003", "namesplit1": "sh010", "version_number": "3",
                   "ext": ext, "input_width": "4096", "meta": {"owner": "A001"}}
        return cli.resolve_output(cfg, seq, "sh010_comp_v003", context)

    def test_source_relative(self):
        self.assertEqual(self._resolve("./review.mp4"), Path("/shows/abc/sh010/comp/v003/review.mp4"))
        self.assertEqual(self._resolve("./../../review/"),
                         Path("/shows/abc/sh010/review/sh010_comp_v003.mp4"))
        self.assertEqual(self._resolve("../out/{namesplit1}_v{version_number}"),
                         Path("/shows/abc/sh010/comp/out/sh010_v3.mp4"))
        self.assertEqual(self._resolve(".."), Path("/shows/abc/sh010/comp/sh010_comp_v003.mp4"))

    def test_absolute_keywords_and_extension(self):
        self.assertEqual(self._resolve("/dailies/{meta:owner}/{clean_name}_{input_width}.{ext}", "mov"),
                         Path("/dailies/A001/sh010_comp_v003_4096.mov"))
        self.assertEqual(self._resolve("/dailies/x.MP4"), Path("/dailies/x.MP4"))     # kept
        self.assertEqual(self._resolve("/dailies/x.mp4", "mov"), Path("/dailies/x.mp4"))  # warns
        self.assertEqual(self._resolve("/dailies/sh010.v3"), Path("/dailies/sh010.v3.mp4"))
        with tempfile.TemporaryDirectory() as tmp:                                   # existing folder
            self.assertEqual(self._resolve(tmp), Path(tmp) / "sh010_comp_v003.mp4")
        self.assertEqual(self._resolve(""), Path("/shows/abc/sh010/comp/v003/sh010_comp_v003.mp4"))

    def test_checks(self):
        cli.check_output_template("./../{clean_name}_{meta:owner}_{namesplit-2}.{ext}")
        for bad in ("./{nope}.mp4", "./{clean_name"):
            with self.assertRaises(TranscodeError):
                cli.check_output_template(bad)
        self.assertEqual(cli.container_from_output("./{clean_name}.mov"), ".mov")
        self.assertEqual(cli.container_from_output("./{clean_name}.v{version_number}"), "")
        self.assertEqual(cli.container_from_output("./{clean_name}.{ext}"), "")


class ThumbnailTests(unittest.TestCase):
    def _cfg(self, *argv):
        ns = cli.build_parser(PRESETS).parse_args(["-i", "x.1001.exr"] + list(argv))
        return cli.config_from_args(ns, PRESETS)

    def test_what_is_made(self):
        cfg = self._cfg()
        self.assertEqual((cfg.movie, cfg.thumbnail), (True, None))            # movie, default place
        cfg = self._cfg("-ot")
        self.assertEqual((cfg.movie, cfg.thumbnail.name, cfg.thumbnail_output), (False, "middle1280", ""))
        cfg = self._cfg("-o", "./", "-ot", "./thumbs/")
        self.assertTrue(cfg.movie)
        self.assertEqual(cfg.thumbnail_output, "./thumbs/")
        cfg = self._cfg("-ot", "-thumbnail_preset", "start512", "-thumbnail_frame", "1010")
        self.assertEqual((cfg.thumbnail.name, cfg.thumbnail.frame, cfg.thumbnail.width), ("start512", "1010", 512))
        for bad in (["-ot", "./{nope}"], ["-ot", "-thumbnail_preset", "nope"],
                    ["-ot", "-thumbnail_frame", "centre"]):
            with self.assertRaises(TranscodeError):
                self._cfg(*bad)

    def test_choose_frame(self):
        from vfx_transcode.thumbnails import choose_frame
        frames = [1001, 1002, 1003, 1005, 1006, 1007]
        self.assertEqual(choose_frame("middle", frames)[0], 1003)       # 1004 is a hole: nearest
        self.assertEqual(choose_frame("first", frames)[0], 1001)
        self.assertEqual(choose_frame("last", frames)[0], 1007)
        self.assertEqual(choose_frame("1004", frames)[0], 1003)
        self.assertEqual(choose_frame("largest", frames, {1001: 10, 1005: 99, 1006: 50})[0], 1005)
        frame, how = choose_frame("largest", frames, {1001: 10, 1002: 10})
        self.assertEqual(frame, 1003)
        self.assertIn("same size", how)
        self.assertIn("needs the movie", choose_frame("largest", frames, None)[1])

    def test_oiiotool_command(self):
        from vfx_transcode.thumbnails import ThumbnailPreset, oiiotool_command
        movie = ["--ch", "R,G,B,A", "--unpremult", "--colorconvert", "ACEScg", "out_srgb", "-d", "uint16"]
        cmd = oiiotool_command("oiiotool", "a.1005.exr", "t.jpg", movie,
                               ThumbnailPreset("t", width=640, quality=80), Fraction(2), alpha=True)
        self.assertNotIn("uint16", cmd)                                   # the movie's depth is gone
        self.assertEqual(cmd[-2:], ["-o", "t.jpg"])
        self.assertIn("0,1,2", cmd)                                       # jpg: no alpha
        self.assertLess(cmd.index("--pixelaspect"), cmd.index("640x0"))   # un-squeeze, then size
        self.assertIn("jpeg:80", cmd)
        png = oiiotool_command("oiiotool", "a.exr", "t.png", movie,
                               ThumbnailPreset("t", format="png", compression=9, bits=16), alpha=True)
        self.assertNotIn("0,1,2", png)                                    # png keeps alpha
        self.assertNotIn("--resize", png)                                 # movie size
        self.assertEqual(png[png.index("png:compressionLevel") + 1], "9")
        self.assertEqual(png[png.index("-d") + 1], "uint16")

    def test_ffmpeg_command(self):
        from vfx_transcode.thumbnails import ThumbnailPreset, ffmpeg_command, jpeg_qscale
        cmd = ffmpeg_command("ffmpeg", "p.1001.png", "t.jpg", ThumbnailPreset("t", height=300), Fraction(2))
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=w=trunc(iw*2/1/2)*2:h=ih", vf)
        self.assertIn("scale=w=-1:h=300", vf)
        self.assertEqual((jpeg_qscale(100), jpeg_qscale(1)), (2, 31))
        png = ffmpeg_command("ffmpeg", "p.png", "t.png", ThumbnailPreset("t", format="png"), keep_alpha=True)
        self.assertIn("format=pix_fmts=rgba", png[png.index("-vf") + 1])

    def test_presets(self):
        from vfx_transcode.thumbnails import presets_from_json
        self.assertEqual(PRESETS.default_thumbnail, "middle1280")
        self.assertEqual(PRESETS.thumbnails["largest1920"].frame, "largest")
        for bad in ({"x": {"format": "gif"}}, {"x": {"quality": 0}}, {"x": {"compression": 10}},
                    {"x": {"format": "jpg", "bits": 16}}, {"x": {"width": -1}}, {"x": {"size": 5}}):
            with self.assertRaises(TranscodeError):
                presets_from_json(bad, "t")

    def test_path(self):
        from vfx_transcode.sequence import Sequence
        seq = Sequence(Path("/shows/sh010/v003"), "sh010_v003.", ".exr", 4, (1001, 1002))
        context = {"clean_name": "sh010_v003", "frame": 1002, "ext": "jpg", "meta": {}}
        resolve = lambda t, folder=None: cli.resolve_path_template(
            t, seq, "sh010_v003.jpg", "jpg", cli._THUMB_EXT_RE, "-ot", context, folder)
        self.assertEqual(resolve(""), Path("/shows/sh010/v003/sh010_v003.jpg"))
        self.assertEqual(resolve("", "/movies"), Path("/movies/sh010_v003.jpg"))     # next to movie
        self.assertEqual(resolve("./../thumbs/{clean_name}_{frame}"), Path("/shows/sh010/thumbs/sh010_v003_1002.jpg"))
        self.assertEqual(resolve("/t/x.jpeg"), Path("/t/x.jpeg"))


if __name__ == "__main__":
    unittest.main()
