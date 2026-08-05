"""End-to-end: real ffmpeg on tiny fixtures, x264 ultrafast only."""

from __future__ import annotations

import shutil

from tests.conftest import render_clip, shutter_clip as sc


def test_probe_real_clip(tiny_clip, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "trip" / "DJI_0001.MP4"
    target.parent.mkdir()
    shutil.copy(tiny_clip, target)

    meta = sc.probe(target, root)
    assert meta is not None
    assert meta.rel == "trip/DJI_0001.MP4"
    assert 1.5 < meta.duration < 2.5
    assert meta.width == 320 and meta.height == 180
    assert meta.audio_codec


def test_scan_end_to_end(tiny_clip, tmp_path, capsys):
    root = tmp_path / "root"
    (root / "trip").mkdir(parents=True)
    shutil.copy(tiny_clip, root / "trip" / "DJI_0001.MP4")
    shutil.copy(tiny_clip, root / "trip" / "IMG_0002.MOV")

    sc.main(["scan", str(root)])
    out = capsys.readouterr().out
    assert "DJI_0001.MP4" in out
    assert "IMG_0002.MOV" in out


def test_cut_from_picks_file(tiny_clip, tmp_path, capsys):
    root = tmp_path / "root"
    (root / "trip").mkdir(parents=True)
    shutil.copy(tiny_clip, root / "trip" / "DJI_0001.MP4")

    picks = tmp_path / "picks.txt"
    picks.write_text(
        "# best moments\n"
        "trip/DJI_0001.MP4 @ 0.2 - 1.2\n",
        encoding="utf-8",
    )

    sc.main([
        "cut", str(picks), str(root),
        "--encoder", "x264", "--preset", "ultrafast",
    ])
    out = capsys.readouterr().out
    assert "done. 1 cuts" in out
    cuts = list((root / "_phone-ready" / "_cuts").glob("*.mp4"))
    assert len(cuts) == 1
    assert cuts[0].stat().st_size > 0


def test_cut_warns_on_unknown_video(tiny_clip, tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(tiny_clip, root / "real.mp4")
    picks = tmp_path / "picks.txt"
    picks.write_text("ghost.mp4 @ 1\n", encoding="utf-8")

    sc.main(["cut", str(picks), str(root),
             "--encoder", "x264", "--preset", "ultrafast"])
    captured = capsys.readouterr()
    assert "no such video" in captured.err
    assert "done. 0 cuts" in captured.out


def test_clips_copy_mode(tmp_path, capsys):
    """clips --copy on a hard-cut fixture: stream copies, no re-encode."""
    root = tmp_path / "root"
    root.mkdir()
    import subprocess

    # two visually distinct 3 s halves so scene detection finds the cut
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=red:size=320x180:rate=30,format=yuv420p",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-filter_complex",
            "[0:v]trim=duration=3[a];[1:v]trim=duration=3[b];[a][b]concat=n=2:v=1[v]",
            "-map", "[v]", "-map", "2:a", "-t", "6",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            str(root / "C0001.MP4"),
        ],
        check=True, capture_output=True, timeout=120,
    )

    sc.main([
        "clips", str(root), "--copy",
        "--min-len", "2", "--max-len", "4",
    ])
    out = capsys.readouterr().out
    made = list((root / "_phone-ready" / "_clips").rglob("*.mp4"))
    assert made, out
