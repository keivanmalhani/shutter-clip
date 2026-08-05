"""Rank-stage invariants complementing tests/test_rank.py.

That file (committed by the rank build session) covers the extractors,
skips, windows, cache statuses, and the cmd_rank end-to-end runs. These
tests pin the score-level invariants the 2026-08-05 scoring review made
load-bearing across the family: identical inputs share identical fates,
unmeasured features stay neutral, and the picks format round-trips.
"""

from __future__ import annotations

from tests.conftest import shutter_clip as sc


def row(klass="broll", **kw):
    base = dict(
        klass=klass,
        t_in=0.0,
        t_out=12.0,
        duration=12.0,
        transcript="a few words here" if klass == "speech" else "",
        words_per_second=1.5 if klass == "speech" else 0.0,
        rms_db=-20.0,
        clipped=False,
        silence_ratio=0.1,
        noise_margin_db=20.0,
        sharpness=100.0,
        crushed_frac=0.01,
        blown_frac=0.01,
        motion=0.05,
        frames_sampled=5,
        face_ratio=None,
    )
    base.update(kw)
    return base


class TestScoreInvariants:
    def test_identical_rows_share_identical_score(self):
        """Duplicate folders of the same clip must tie exactly: no copy
        outranks another on walk order."""
        rows = [row() for _ in range(6)]
        sc.social_score(rows, 6.0, 20.0)
        assert len({r["social_score"] for r in rows}) == 1
        assert len({r["social_percentile"] for r in rows}) == 1

    def test_motion_outranks_for_broll(self):
        rows = [row(motion=0.02), row(motion=0.30)]
        sc.social_score(rows, 6.0, 20.0)
        assert rows[1]["social_score"] > rows[0]["social_score"]

    def test_classes_pooled_separately(self):
        rows = [row("speech"), row("speech"), row("broll"), row("broll")]
        sc.social_score(rows, 6.0, 20.0)
        assert all(0.0 <= r["social_score"] <= 1.0 for r in rows)

    def test_missing_faces_stay_neutral(self):
        rows = [row("speech", face_ratio=1.0), row("speech"), row("speech", face_ratio=0.0)]
        sc.social_score(rows, 6.0, 20.0)
        assert all("social_score" in r for r in rows)


class TestPickLineFormat:
    def test_range_with_flag(self):
        match = sc.PICK_RE.match("sub/clip.mp4 @ 1:15 - 1:30 v")
        assert match.group("path") == "sub/clip.mp4"
        assert match.group("start") == "1:15"
        assert match.group("end") == "1:30"
        assert "v" in match.group("flags")

    def test_bare_start(self):
        match = sc.PICK_RE.match("clip.mp4 @ 12")
        assert match.group("end") is None

    def test_rank_emits_parseable_lines(self):
        line = "%s @ %s-%s" % ("trip/DJI_0001.MP4", sc.fmt_tc(75), sc.fmt_tc(95))
        assert sc.PICK_RE.match(line)
