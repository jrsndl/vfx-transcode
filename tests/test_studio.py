"""vfx-transcode.ini: studio preferences, with the local presets.json as the fallback."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from vfx_transcode import cli, studio

REPO = Path(__file__).resolve().parent.parent


class StudioPrefsTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(tempfile.mkdtemp())
        self.share = Path(tempfile.mkdtemp())
        shutil.copy(REPO / "presets.json", self.app / "presets.json")
        shutil.copy(REPO / "presets.json", self.share / "presets.json")

    def tearDown(self):
        shutil.rmtree(self.app)
        shutil.rmtree(self.share)

    def ini(self, value):
        (self.app / studio.INI_NAME).write_text(
            "[vfx-transcode]\nstudio_prefs = {}\n".format(value), encoding="utf-8")

    def choose(self):
        return studio.choose_presets(str(self.app))

    def test_no_ini_is_local(self):
        c = self.choose()
        self.assertEqual(Path(c.path), self.app / "presets.json")
        self.assertEqual(c.problems, [])

    def test_folder_and_file(self):
        self.ini(self.share)
        c = self.choose()
        self.assertEqual(Path(c.path), self.share / "presets.json")
        self.assertIsNotNone(c.presets)
        self.assertIn("studio", c.source)
        self.ini(self.share / "presets.json")
        self.assertEqual(Path(self.choose().path), self.share / "presets.json")

    def test_environment_variable(self):
        os.environ["VT_TEST_SHARE"] = str(self.share)
        try:
            self.ini("$VT_TEST_SHARE" if os.name != "nt" else "%VT_TEST_SHARE%")
            self.assertEqual(Path(self.choose().path), self.share / "presets.json")
        finally:
            del os.environ["VT_TEST_SHARE"]

    def test_fallbacks(self):
        self.ini("")                                   # empty: local, no problem
        c = self.choose()
        self.assertEqual((Path(c.path), c.problems), (self.app / "presets.json", []))
        self.ini(self.share / "missing")               # not found
        c = self.choose()
        self.assertEqual(Path(c.path), self.app / "presets.json")
        self.assertIn("not found", c.problems[0])
        (self.share / "presets.json").write_text("{ broken", encoding="utf-8")
        self.ini(self.share)                           # fails to load
        c = self.choose()
        self.assertEqual(Path(c.path), self.app / "presets.json")
        self.assertIn("failed to load", c.problems[0])
        (self.app / studio.INI_NAME).write_text("no section\n", encoding="utf-8")
        c = self.choose()                              # unreadable ini
        self.assertEqual(Path(c.path), self.app / "presets.json")
        self.assertTrue(c.problems)

    def test_template_parses(self):
        (self.app / studio.INI_NAME).write_text(studio.INI_TEMPLATE, encoding="utf-8")
        self.assertEqual(studio.read_ini(str(self.app / studio.INI_NAME)), "")
        self.assertEqual(studio.read_ini(str(REPO / studio.INI_NAME)), "")

    def test_presets_file_option_wins(self):
        c = cli.presets_choice_for(["-presets_file", str(self.share / "presets.json"), "-preset", "h264"])
        self.assertEqual((Path(c.path), c.source), (self.share / "presets.json", "-presets_file"))
