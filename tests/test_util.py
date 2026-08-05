"""Timecode and size formatting plus discovery skip rules."""

from __future__ import annotations

import pytest

from tests.conftest import shutter_clip as sc


class TestTimecode:
    def test_fmt_short(self):
        assert sc.fmt_tc(0) == "0:00"
        assert sc.fmt_tc(75) == "1:15"
        assert sc.fmt_tc(3599) == "59:59"

    def test_fmt_hours(self):
        assert sc.fmt_tc(3600) == "1:00:00"
        assert sc.fmt_tc(75, always_hours=True) == "0:01:15"

    def test_fmt_negative_clamps(self):
        assert sc.fmt_tc(-5) == "0:00"

    def test_parse_all_shapes(self):
        assert sc.parse_tc("12") == 12.0
        assert sc.parse_tc("12.5") == 12.5
        assert sc.parse_tc("1:15") == 75.0
        assert sc.parse_tc("1:02:03") == 3723.0

    def test_parse_roundtrip(self):
        assert sc.parse_tc(sc.fmt_tc(3723)) == 3723.0

    def test_parse_garbage_raises(self):
        with pytest.raises(ValueError):
            sc.parse_tc("1:2:3:4")


class TestSize:
    def test_units(self):
        assert sc.fmt_size(512) == "512 B"
        assert sc.fmt_size(2048) == "2.0 KB"
        assert sc.fmt_size(3 << 20) == "3.0 MB"
        assert sc.fmt_size(5 << 30) == "5.0 GB"


class TestFindVideos:
    def _touch(self, path, size=4096):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * size)
        return path

    def test_skip_rules(self, tmp_path):
        keep = self._touch(tmp_path / "trip" / "DJI_0001.MP4")
        self._touch(tmp_path / ".hidden" / "x.mp4")
        self._touch(tmp_path / "_phone-ready" / "y.mp4")
        self._touch(tmp_path / "old DO NOT INCLUDE" / "z.mp4")
        self._touch(tmp_path / "do not use these" / "w.mov")
        self._touch(tmp_path / "._DJI_0002.MP4")
        self._touch(tmp_path / "notes.txt")
        self._touch(tmp_path / "stub.mp4", size=100)
        assert sc.find_videos(tmp_path, []) == [keep]

    def test_extra_excludes_and_sorting(self, tmp_path):
        b = self._touch(tmp_path / "b.mp4")
        a = self._touch(tmp_path / "a" / "a.mov")
        self._touch(tmp_path / "rejects" / "r.mp4")
        assert sc.find_videos(tmp_path, ["rejects"]) == [a, b]


class TestFresh:
    def test_fresh_only_when_output_newer(self, tmp_path):
        import os

        src = tmp_path / "src.mp4"
        out = tmp_path / "out.mp4"
        src.write_bytes(b"x")
        out.write_bytes(b"y")
        os.utime(src, (1000, 1000))
        os.utime(out, (2000, 2000))
        assert sc.fresh(out, src)
        os.utime(out, (500, 500))
        assert not sc.fresh(out, src)
        assert not sc.fresh(tmp_path / "missing.mp4", src)
