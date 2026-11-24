"""Saving named presets from the tuner into presets.json."""

import shutil
import tempfile
import unittest
from pathlib import Path

from vfx_transcode import cli, presets_store as ps
from vfx_transcode.presets import load_presets
from vfx_tuner import core

REPO = Path(__file__).resolve().parent.parent


def parse(args, presets):
    ns = cli.build_parser(presets).parse_args(["-i", "x.1001.exr"] + [str(a) for a in args])
    return cli.config_from_args(ns, presets)


class PresetsStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "presets.json"
        shutil.copy(REPO / "presets.json", self.path)
        self.original = self.path.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.folder)

    def save(self, state, area, name, notes="", sub=None):
        presets = load_presets(str(self.path))
        entry = core.preset_entry(state, area, presets, notes, sub)
        result = ps.save_preset(self.path, area, name, entry, sub)
        return load_presets(str(self.path)), result

    def test_layout_unchanged_apart_from_the_preset(self):
        data, nl = ps.read_raw(self.path)
        self.assertEqual(ps.dumps(data, nl).encode(), self.original)
        state = core.TunerState()
        state.filmstrip = core.FilmstripState(tiles=10, tile_width=320, tile_height=180)
        _, result = self.save(state, "filmstrip", "ten")
        self.assertEqual(result.backup.name, "presets_safecopy001.json")
        self.assertEqual(result.backup.read_bytes(), self.original)
        import difflib
        diff = [l for l in difflib.unified_diff(self.original.decode().splitlines(),
                                                self.path.read_text().splitlines(), lineterm="", n=0)
                if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        self.assertTrue(diff and all(l.startswith("+") for l in diff), diff)   # lines only added

    def test_colour(self):
        state = core.TunerState()
        state.input.colorin, state.input.colorout = "Output - Rec.709", "ACES - ACEScg"
        presets, _ = self.save(state, "colour", "my_709_in", "Rec.709 in")
        c = presets.colours["my_709_in"]
        self.assertEqual((c.ci, c.co, c.notes, c.ocio), ("Output - Rec.709", "ACES - ACEScg", "Rec.709 in", ""))
        other = core.TunerState()
        core.colour_from_preset(other.input, c)
        self.assertEqual(other.input.colour_args(), state.input.colour_args())
        cfg = parse(["-colour_preset", "my_709_in"], presets)
        self.assertEqual((cfg.colorin, cfg.colorout, cfg.colour_given), ("Output - Rec.709", "ACES - ACEScg", True))
        cfg = parse(["-colour_preset", "my_709_in", "-co", "Output - sRGB"], presets)
        self.assertEqual(cfg.colorout, "Output - sRGB")                     # the option wins

    def test_ff_transform(self):
        state = core.TunerState()
        state.ff_transform.text = "scale=w=1000:h=-2\n# comment\npad=1000:600:(ow-iw)/2:(oh-ih)/2"
        presets, _ = self.save(state, "transform", "odd")
        loaded = core.ff_transform_from_preset(presets.ff_transforms["odd"])
        self.assertEqual(loaded.filters(), state.ff_transform.filters())
        self.assertEqual(parse(["-ff_transform_preset", "odd"], presets).ff_transform,
                         "scale=w=1000:h=-2,pad=1000:600:(ow-iw)/2:(oh-ih)/2")
        self.assertEqual(parse(["-ff_transform_preset", "hd_lanczos"], presets).ff_transform,
                         "scale=w=1920:h=-2:flags=lanczos")

    def test_burnin(self):
        presets = load_presets(str(self.path))
        state = core.TunerState()
        state.burn = core.burn_from_preset(presets.burnins["review"])
        state.burn.places["tc"].text = "{date}"
        state.burn.places["tc"].own_style, state.burn.places["tc"].color = True, "yellow"
        state.burn.size = "2%"
        presets, _ = self.save(state, "burnin", "review_dated", "with date")
        loaded = core.burn_from_preset(presets.burnins["review_dated"])
        self.assertEqual(core.burn_args(loaded), core.burn_args(state.burn))   # same drawing
        self.assertEqual(core.burn_args(loaded, presets), ["-burn_preset", "review_dated"])

    def test_slate(self):
        state = core.TunerState()
        state.slate = core.SlateState(enabled=True, title="{clean_name}", fields=[("Date", "{date}")],
                                      logo="./vendor.png", logo_position="bl", scale=0.8, rule=True)
        presets, _ = self.save(state, "slate", "vendor", "vendor slate")
        loaded = core.slate_from_spec(presets.slates["vendor"], "vendor")
        state.slate.preset = "vendor"
        expect = dict(vars(state.slate), logo=loaded.logo)     # ./ resolved against the file
        self.assertEqual(vars(loaded), expect)

    def test_filmstrip(self):
        state = core.TunerState()
        state.filmstrip = core.FilmstripState(tiles=12, columns=6, rows=2, tile_width=320,
                                              tile_height=180, quality=80, source="movie", slate=True)
        presets, _ = self.save(state, "filmstrip", "sheet")
        loaded = core.filmstrip_from_preset(presets.filmstrips["sheet"], "sheet")
        state.filmstrip.preset = "sheet"
        self.assertEqual(loaded, state.filmstrip)

    def test_transform_sequence_new_and_into_existing(self):
        presets = load_presets(str(self.path))
        state = core.TunerState()
        state.transform = core.load_transform(presets, "mygreatproject_editorial", "239")
        state.transform.args.append("--sharpen 0.2")
        # into the existing preset, replacing its sub "239" (its conditions kept)
        presets, _ = self.save(state, "transform_sequence", "mygreatproject_editorial", "sharper", "239")
        t = presets.transforms["mygreatproject_editorial"]
        self.assertEqual([s.name for s in t.subs], ["uhd", "239", "other"])
        sub = t.subs[1]
        self.assertEqual((sub.transforms[-1], len(sub.conditions), sub.description),
                         ("--sharpen 0.2", 2, "sharper"))
        # a new preset with one sub
        presets, _ = self.save(state, "transform_sequence", "new_show", "", "default")
        t = presets.transforms["new_show"]
        self.assertEqual(([s.name for s in t.subs], t.subs[0].transforms[-1]), (["default"], "--sharpen 0.2"))

    def test_bad_entry_leaves_file(self):
        with self.assertRaises(cli.TranscodeError):
            ps.save_preset(self.path, "filmstrip", "bad", {"tiles": "many"})
        self.assertEqual(self.path.read_bytes(), self.original)
        with self.assertRaises(cli.TranscodeError):
            ps.save_preset(self.path, "slate", "none", {})

    def test_exists(self):
        presets = load_presets(str(self.path))
        self.assertTrue(ps.exists(presets, "burnin", "review"))
        self.assertTrue(ps.exists(presets, "transform_sequence", "someotherproject", "1080p"))
        self.assertFalse(ps.exists(presets, "transform_sequence", "someotherproject", "x"))
        self.assertTrue(ps.exists(presets, "colour", "rec709_to_acescg"))


class SmartTokensStoreTests(PresetsStoreTests):
    def test_smart_tokens_round_trip(self):
        presets = load_presets(str(self.path))
        state = core.TunerState()
        state.tokens = core.tokens_from_preset(presets, "example_project")
        state.tokens.rows.append({"name": "client", "source": "acme", "case": "upper"})
        presets, _ = self.save(state, "smart_tokens", "acme", "for ACME")
        loaded = core.tokens_from_preset(presets, "acme")
        self.assertEqual(loaded.rows, state.tokens.rows)
        self.assertEqual(presets.smart_tokens["acme"].notes, "for ACME")
