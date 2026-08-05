"""Shared fixtures for the shutter-clip suite.

shutter_clip.py is a single-file stdlib tool: tests import it straight
from the repo root. Encode fixtures follow the standing sandbox rule:
x264 ultrafast at tiny sizes, nothing slower ever.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shutter_clip  # noqa: E402


def make_meta(**kw) -> "shutter_clip.Meta":
    """A Meta stub with sane defaults, overridable per test."""
    m = shutter_clip.Meta()
    defaults = dict(
        path=Path("/x/clip.mp4"),
        rel="clip.mp4",
        duration=60.0,
        width=3840,
        height=2160,
        fps=30.0,
        vcodec="hevc",
        pix_fmt="yuv420p10le",
        bits=10,
        hdr=False,
        color_transfer="bt709",
        audio_codec="aac",
        vindex=0,
        aindex=1,
        size=1 << 30,
        rotated=False,
    )
    defaults.update(kw)
    for field, value in defaults.items():
        setattr(m, field, value)
    return m


def render_clip(
    path: Path,
    *,
    duration: float = 2.0,
    size: str = "320x180",
    with_audio: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-t", str(duration), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture(scope="session")
def tiny_clip(tmp_path_factory) -> Path:
    return render_clip(tmp_path_factory.mktemp("fixtures") / "tiny.mp4")
