"""Multi-user safe saving of the super-presets file."""

import datetime
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vfx_transcode import safe_save, super_presets as sp


class SafeSaveTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "super_presets.json"

    def tearDown(self):
        shutil.rmtree(self.folder)

    def names(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_temp_name(self):
        now = datetime.datetime(2026, 11, 24, 16, 49)
        self.assertEqual(safe_save.temp_path(self.path, "jdoe", now).name,
                         "super_presets_jdoe_261124-1649.json")
        (self.folder / "super_presets_jdoe_261124-1649.json").write_text("x")
        made = safe_save.create_temp(self.path, b"y", "jdoe", now)
        self.assertEqual(made.name, "super_presets_jdoe_261124-1649_2.json")
        self.assertEqual((self.folder / "super_presets_jdoe_261124-1649.json").read_text(), "x")

    def test_new_file_then_safecopies(self):
        r = safe_save.safe_write_text(self.path, "one\n")
        self.assertIsNone(r.backup)
        self.assertEqual(self.names(), ["super_presets.json"])
        r = safe_save.safe_write_text(self.path, "two\n")
        self.assertEqual(r.backup.name, "super_presets_safecopy001.json")
        self.assertEqual(r.backup.read_text(), "one\n")
        r = safe_save.safe_write_text(self.path, "three\n")
        self.assertEqual(r.backup.name, "super_presets_safecopy002.json")
        self.assertEqual(self.path.read_text(), "three\n")
        self.assertEqual(self.names(), ["super_presets.json", "super_presets_safecopy001.json",
                                        "super_presets_safecopy002.json"])   # no temp left

    def test_conflict_keeps_both_files(self):
        safe_save.safe_write_text(self.path, "theirs\n")
        stamp = safe_save.file_stamp(self.path)
        self.path.write_text("someone else saved\n")
        with self.assertRaises(safe_save.SaveConflict):
            safe_save.safe_write_text(self.path, "mine\n", expect_stamp=True, stamp=(0, 0))
        self.assertEqual(self.path.read_text(), "someone else saved\n")
        self.assertNotIn("mine", "".join(p.read_text() for p in self.folder.iterdir()))

    def test_failed_write_leaves_the_file(self):
        safe_save.safe_write_text(self.path, "good\n")
        with mock.patch("os.replace", side_effect=OSError("share offline")):
            with self.assertRaises(OSError):
                safe_save.safe_write_text(self.path, "new\n")
        self.assertEqual(self.path.read_text(), "good\n")
        self.assertFalse([p for p in self.folder.iterdir() if "_261" in p.name or "_" + safe_save.user_name() in p.name])

    def test_update_redoes_after_someone_elses_save(self):
        sp.save(self.path, {"a": {"notes": "", "areas": {}}})
        real = safe_save.safe_write_text
        calls = []

        def racing(path, text, **kw):
            if not calls:                      # someone else saves "b" just before our rename
                calls.append(1)
                real(path, sp.dumps({"a": {"notes": "", "areas": {}}, "b": {"notes": "", "areas": {}}}))
                raise safe_save.SaveConflict("changed")
            return real(path, text, **kw)

        with mock.patch.object(sp.safe_save, "safe_write_text", side_effect=racing):
            sp.update(self.path, lambda p: p.__setitem__("c", {"notes": "", "areas": {}}))
        self.assertEqual(list(sp.load(self.path)), ["a", "b", "c"])

    def test_merged_local_wins(self):
        studio, local = self.folder / "studio.json", self.folder / "local.json"
        sp.save(studio, {"x": {"notes": "studio", "areas": {}}, "y": {"notes": "", "areas": {}}})
        sp.save(local, {"x": {"notes": "local", "areas": {}}})
        presets, origin = sp.load_merged([studio, local])
        self.assertEqual(presets["x"]["notes"], "local")
        self.assertEqual((origin["x"], origin["y"]), (local, studio))
