"""The black frame guard: frames far smaller than the others are converted again."""

from pathlib import Path
from types import SimpleNamespace

from vfx_transcode import oiio


def test_suspect_frames():
    sizes = {1: 300000, 2: 310000, 3: 2000, 4: 305000, 5: 298000}
    assert oiio.suspect_frames(sizes) == [3]
    assert oiio.suspect_frames({1: 5, 2: 300000}) == []                 # too few to judge
    assert oiio.suspect_frames({f: 1000 for f in range(10)}) == []     # all dark: fine


def test_redo_suspects(tmp_path, monkeypatch):
    target = {f: tmp_path / "out.{}.jpg".format(f) for f in range(1, 6)}
    for f, path in target.items():
        path.write_bytes(b"x" * (2000 if f in (2, 4) else 300000))
    result = oiio.ConversionResult()
    result.outputs.update(target)
    seq = SimpleNamespace(path=lambda f: Path("/src/in.{}.exr".format(f)))
    cfg = SimpleNamespace(oiiotool="oiiotool", verbose=False)
    calls = []

    def fake_run(cmd, name, verbose):
        calls.append(cmd)
        out = Path(cmd[-1])
        out.write_bytes(b"x" * (300000 if "out.2." in out.name else 2000))  # 4 is really dark
    monkeypatch.setattr(oiio, "run_tool", fake_run)
    fixed = oiio.redo_suspects(cfg, seq, target, ["--colorconvert", "a", "b"], result)
    assert fixed == [2]
    assert len(calls) == 2 and calls[0][:3] == ["oiiotool", "--threads", "1"]
    assert target[2].stat().st_size == 300000 and target[4].is_file()
    assert result.failed == []


def test_parse_threads():
    import pytest
    from vfx_transcode.cli import parse_threads
    from vfx_transcode.errors import TranscodeError
    assert parse_threads("50%", cpus=16) == 8
    assert parse_threads("25%", cpus=6) == 2            # 1.5 rounds to 2
    assert parse_threads("1%", cpus=4) == 1             # at least one
    assert parse_threads("100%", cpus=12) == 12
    assert parse_threads("0", cpus=12) == 12 and parse_threads("3", cpus=12) == 3
    for bad in ("half", "0%", "150%", "-2"):
        with pytest.raises(TranscodeError):
            parse_threads(bad, cpus=8)


def test_threads_default_is_half_of_this_machine():
    import os
    from pathlib import Path
    from vfx_transcode import cli
    from vfx_transcode.presets import load_presets
    presets = load_presets(Path(__file__).resolve().parent.parent / "presets.json")
    ns = cli.build_parser(presets).parse_args(["-i", "/x/a.0001.exr"])
    cfg = cli.config_from_args(ns, presets)
    assert cfg.oiio_threads == max(1, round((os.cpu_count() or 4) / 2))
    assert cfg.workers == cfg.oiio_threads


def test_old_super_presets_get_the_new_thread_default(tmp_path):
    import json
    from vfx_transcode import super_presets
    path = tmp_path / "super_presets.json"
    old = {"intermediate": {"state": {"oiio_threads": 0}, "args": ["-oiio_threads", "0", "-mezzanine", "png"]}}
    new = {"intermediate": {"state": {"oiio_threads": "0"}, "args": ["-oiio_threads", "0"]}}
    six = {"intermediate": {"state": {"oiio_threads": 6}, "args": ["-oiio_threads", "6"]}}
    path.write_text(json.dumps({"super_presets": {"old": {"areas": old}, "new": {"areas": new},
                                                  "six": {"areas": six}}}))
    loaded = super_presets.load(path)
    assert loaded["old"]["areas"]["intermediate"]["args"][:2] == ["-oiio_threads", "50%"]
    assert loaded["new"]["areas"]["intermediate"]["args"] == ["-oiio_threads", "0"]
    assert loaded["six"]["areas"]["intermediate"]["args"] == ["-oiio_threads", "6"]
