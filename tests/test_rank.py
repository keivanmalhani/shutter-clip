"""Tests for the rank subcommand, the shutter-select integration.

shutter_clip.py is a single stdlib-only file, loaded via importlib so the
repo needs no packaging changes. These are the first tests in the repo; the
rest of the quality-bar suite is tracked as open debt in the spec.

Run against an alternate build with SHUTTER_CLIP_PATH=/path/to/file.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

_TARGET = Path(
    os.environ.get(
        "SHUTTER_CLIP_PATH", Path(__file__).parent.parent / "shutter_clip.py"
    )
)
_SPEC = importlib.util.spec_from_file_location("shutter_clip", _TARGET)
sc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sc)


def seg(**overrides):
    row = {
        "index": 0,
        "t_in": 0.0,
        "t_out": 10.0,
        "duration": 10.0,
        "klass": "speech",
        "transcript": "the canyon light was unbelievable",
        "words_per_second": 2.0,
        "rms_db": -18.0,
        "clipped": False,
        "silence_ratio": 0.05,
        "noise_margin_db": 25.0,
        "sharpness": 200.0,
        "crushed_frac": 0.01,
        "blown_frac": 0.0,
        "motion": 0.05,
        "frames_sampled": 8,
        "face_ratio": None,
    }
    row.update(overrides)
    return row


def write_shoot(tmp_path, named_segments):
    """Dummy videos plus a shutter-select cache dir keyed the way
    selects_cache_payload expects: sha1 of the resolved video path, with
    source mtime and size recorded in the payload."""
    root = tmp_path / "shoot"
    cache = root / "_selects" / "cache"
    cache.mkdir(parents=True)
    for name, segments in named_segments:
        video = root / name
        video.write_bytes(b"\0" * 2048)  # find_videos wants >= 1KB
        st = video.stat()
        key = hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()[:16]
        payload = {
            "schema_version": 1,
            "source": {
                "path": str(video.resolve()),
                "name": name,
                "mtime": st.st_mtime,
                "size": st.st_size,
            },
            "segments": segments,
        }
        (cache / (key + ".json")).write_text(json.dumps(payload))
    return root


def rank_opts(root, **overrides):
    opts = argparse.Namespace(
        root=str(root),
        exclude=[],
        dest=None,
        selects=None,
        analyze=False,
        select_bin="shutter-select",
        select_args="",
        top=0,
        clip_len=0.0,
        min_len=4.0,
        max_len=18.0,
    )
    for key, value in overrides.items():
        setattr(opts, key, value)
    return opts


# ---------------------------------------------------------------- units


def test_percentile_ranks_averages_ties():
    assert sc.percentile_ranks([]) == []
    assert sc.percentile_ranks([9.0]) == [0.5]
    assert sc.percentile_ranks([30.0, 10.0, 20.0]) == [1.0, 0.0, 0.5]
    # Duplicate copies of a clip share the average rank instead of one
    # copy winning free points on walk order.
    assert sc.percentile_ranks([10.0, 10.0, 20.0]) == [0.25, 0.25, 1.0]


def test_duration_fit_band_and_gentle_decay():
    assert sc.social_duration_fit(3.0, 6.0, 18.0) == 0.5
    assert sc.social_duration_fit(10.0, 6.0, 18.0) == 1.0
    assert sc.social_duration_fit(300.0, 6.0, 18.0) == 0.35  # floor, never 0


def test_exposure_and_audio_extractors():
    assert sc.exposure_quality(seg()) > 0.9
    assert sc.exposure_quality(seg(crushed_frac=0.5)) == 0.0
    assert sc.audio_quality_raw(seg()) == pytest.approx(24.0)


def test_social_skips_gate_before_scoring():
    assert sc.social_skips(seg(), 4.0) == []
    assert sc.social_skips(seg(frames_sampled=0), 4.0)
    assert sc.social_skips(seg(t_out=2.0), 4.0)  # under min_len
    assert any("clipped" in r for r in sc.social_skips(seg(clipped=True), 4.0))
    assert any("quiet" in r for r in sc.social_skips(seg(rms_db=-50.0), 4.0))
    # b-roll is never audio-gated
    assert sc.social_skips(seg(klass="broll", clipped=True, rms_db=-50.0), 4.0) == []


def test_clip_window_speech_hooks_start_broll_centers():
    short = seg(t_in=10.0, t_out=22.0)
    assert sc.clip_window(short, 14.0) == (10.0, 12.0)  # whole segment

    long_speech = seg(t_in=10.0, t_out=100.0)
    assert sc.clip_window(long_speech, 15.0) == (10.0, 15.0)

    long_broll = seg(klass="broll", t_in=0.0, t_out=60.0)
    start, length = sc.clip_window(long_broll, 20.0)
    assert length == 20.0
    assert start == pytest.approx(20.0)  # centered


def test_social_score_orders_ladder_and_isolates_classes():
    rows = []
    for i in range(6):
        rows.append(
            seg(
                index=i,
                noise_margin_db=10.0 + 2 * i,
                words_per_second=1.0 + 0.2 * i,
                sharpness=100.0 + 10 * i,
                motion=0.02 + 0.01 * i,
                crushed_frac=max(0.0, 0.1 - 0.02 * i),
            )
        )
    for i in range(4):
        rows.append(
            seg(
                index=10 + i,
                klass="broll",
                transcript="",
                words_per_second=0.0,
                sharpness=50.0 + i,
                motion=0.01 * (i + 1),
                duration=8.0,
            )
        )
    sc.social_score(rows, 6.0, 18.0)
    speech = [r for r in rows if r["klass"] == "speech"]
    broll = [r for r in rows if r["klass"] == "broll"]
    assert [r["index"] for r in sorted(speech, key=lambda r: r["social_score"])] == list(range(6))
    assert max(r["social_percentile"] for r in speech) == 1.0
    assert max(r["social_percentile"] for r in broll) == 1.0
    assert all(0.0 <= r["social_score"] <= 1.0 for r in rows)


# ---------------------------------------------------------------- cache gate


def test_selects_cache_payload_statuses(tmp_path):
    root = write_shoot(tmp_path, [("a.mov", [seg()])])
    cache = root / "_selects" / "cache"
    video = root / "a.mov"

    payload, status = sc.selects_cache_payload(cache, video)
    assert status == "ok" and payload["segments"]

    _, status = sc.selects_cache_payload(cache, root / "missing.mov")
    assert status == "missing"

    key = hashlib.sha1(str(video.resolve()).encode()).hexdigest()[:16]
    target = cache / (key + ".json")
    body = json.loads(target.read_text())
    body["schema_version"] = 99
    target.write_text(json.dumps(body))
    _, status = sc.selects_cache_payload(cache, video)
    assert status == "schema"

    body["schema_version"] = 1
    body["source"]["size"] = 1  # no longer matches the file on disk
    target.write_text(json.dumps(body))
    _, status = sc.selects_cache_payload(cache, video)
    assert status == "stale"


# ---------------------------------------------------------------- cmd_rank


def test_cmd_rank_writes_cut_compatible_picks_and_ranking_json(tmp_path, capsys):
    root = write_shoot(
        tmp_path,
        [
            ("great_take.mov", [seg(words_per_second=3.0, noise_margin_db=30.0)]),
            ("clipped_take.mov", [seg(clipped=True)]),
            ("some_broll.mov", [
                seg(klass="broll", transcript="", words_per_second=0.0,
                    motion=0.09, duration=8.0, t_out=8.0),
            ]),
        ],
    )
    sc.cmd_rank(rank_opts(root))
    capsys.readouterr()

    rank_dir = root / "_phone-ready" / "rank"
    report = (rank_dir / "report.txt").read_text()
    assert "clipped" in report  # the skip is reported, never hidden
    picks = (rank_dir / "picks.txt").read_text().splitlines()
    pick_lines = [l for l in picks if l and not l.startswith("#")]
    assert len(pick_lines) == 2  # clipped take excluded
    for line in pick_lines:
        assert sc.PICK_RE.match(line), line

    ranking = json.loads((rank_dir / "ranking.json").read_text())
    assert ranking["selects_schema"] == 1
    assert len(ranking["clips"]) == 2
    assert len(ranking["skipped"]) == 1
    assert ranking["weights"]["speech"] == sc.SOCIAL_SPEECH_WEIGHTS


def test_cmd_rank_top_flag_caps_picks(tmp_path, capsys):
    segments = [
        seg(index=i, t_in=i * 20.0, t_out=i * 20.0 + 10.0,
            words_per_second=1.0 + i)
        for i in range(6)
    ]
    root = write_shoot(tmp_path, [("interview.mov", segments)])
    sc.cmd_rank(rank_opts(root, top=2))
    capsys.readouterr()
    picks = (root / "_phone-ready" / "rank" / "picks.txt").read_text().splitlines()
    assert len([l for l in picks if l and not l.startswith("#")]) == 2


def test_cmd_rank_dies_plainly_without_analysis(tmp_path, capsys):
    root = tmp_path / "bare"
    root.mkdir()
    (root / "clip.mov").write_bytes(b"\0" * 2048)
    with pytest.raises(SystemExit):
        sc.cmd_rank(rank_opts(root))
    err = capsys.readouterr().err
    assert "shutter-select analyze" in err
