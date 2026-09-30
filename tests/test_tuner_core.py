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
        self.assertEqual(t.cli_args("239"), ["-oiio_transform", "mygreatproject_editorial", "-width", "1920"])
        self.assertEqual(t.cli_args("other")[0], "-oiio_transform_args")   # not the active one
        t.args.append("--resize 1000x0")
        args = t.cli_args("239")
        self.assertEqual(args[0], "-oiio_transform_args")
        cfg = parse(t.preview_args())
        self.assertEqual(cfg.oiio_transform_explicit[-2:], ["--resize", "1000x0"])
        self.assertIn("--cut", cfg.oiio_transform_explicit)
        self.assertEqual(parse(core.TransformState(width=1280).preview_args()).width, 1280)
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
        self.assertEqual(set(sections), {"transform", "burnin", "slate", "letterbox", "metadata"})

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
