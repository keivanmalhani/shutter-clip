#!/usr/bin/env python3
"""shutter-clip: zero-edit social clips straight from a footage drive.

Point it at a drive or folder of camera originals. It inventories, converts,
auto-cuts, and exports phone-ready files you can AirDrop and post as-is.

Subcommands:
  scan     inventory every video: codec, resolution, fps, duration, flags
  mirror   phone-ready 1080p copy of every clip into _phone-ready/library/
  publish  motion-ranked best moments, named readably and organized into
           platform packs (tiktok + reels, shorts + stories) by content type
  clips    plain auto-cut into short pieces via scene detection
  sheet    build a self-contained HTML contact sheet for picking moments
  cut      export exact moments listed in a picks file

Design rules:
  - Horizontal 1920x1080 is the default output. Vertical 1080x1920 center
    crop is opt-in via --vertical (adds) or --vertical-only (replaces).
  - HEVC with the hvc1 tag by default: half the size of H.264, plays
    natively on iPhone. Hardware encoder (VideoToolbox) is used on macOS,
    libx265/libx264 elsewhere. Override with --encoder.
  - Outputs are additive. Sources are never modified or deleted.
  - Stdlib only. Requires ffmpeg and ffprobe on PATH (brew install ffmpeg).

Copyright: MIT. Part of the shutter-* family.
"""

import argparse
import base64
import html
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mts", ".avi", ".mkv"}
OUT_ROOT_NAME = "_phone-ready"
# Folder names skipped everywhere, matched case-insensitively as substrings.
DEFAULT_EXCLUDE_SUBSTRINGS = ["do not include", "do not use"]

TONEMAP_ZSCALE = (
    "zscale=transfer=linear:npl=100,format=gbrpf32le,"
    "zscale=primaries=bt709,tonemap=hable:desat=0,"
    "zscale=transfer=bt709:matrix=bt709:range=tv,format=yuv420p"
)
TONEMAP_PLACEBO = (
    "libplacebo=tonemapping=auto:colorspace=bt709:color_primaries=bt709:"
    "color_trc=bt709:range=tv:format=yuv420p"
)

# ---------------------------------------------------------------- utilities


def die(msg, code=2):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)


def which_or_die():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(
                tool + " not found on PATH. On macOS run:  brew install ffmpeg"
            )


def run(cmd, capture=True, ok_codes=(0,)):
    """Run a command list. Returns CompletedProcess. Raises on bad exit."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if proc.returncode not in ok_codes:
        tail = ""
        if proc.stderr:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            tail = " | ".join(tail[-4:])
        raise RuntimeError(
            "command failed (%d): %s :: %s"
            % (proc.returncode, " ".join(map(str, cmd[:6])), tail)
        )
    return proc


def fmt_tc(seconds, always_hours=False):
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h or always_hours:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def parse_tc(text):
    """Accept ss, ss.s, mm:ss, h:mm:ss."""
    text = text.strip()
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError("bad timecode: " + text)


def fmt_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return (
                "%d %s" % (nbytes, unit)
                if unit == "B"
                else "%.1f %s" % (nbytes, unit)
            )
        nbytes /= 1024.0
    return "?"


# ---------------------------------------------------------------- probing


class Meta:
    """Everything we need to know about one source video."""

    __slots__ = (
        "path",
        "rel",
        "duration",
        "width",
        "height",
        "fps",
        "vcodec",
        "pix_fmt",
        "bits",
        "hdr",
        "color_transfer",
        "audio_codec",
        "vindex",
        "aindex",
        "size",
        "rotated",
    )


def probe(path, root):
    p = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    data = json.loads(p.stdout.decode("utf-8", "replace"))
    streams = data.get("streams", [])

    vstreams = []
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        if s.get("disposition", {}).get("attached_pic"):
            continue
        if s.get("codec_name") in ("mjpeg", "png", "bmp", "gif"):
            continue
        vstreams.append(s)
    if not vstreams:
        return None
    v = max(vstreams, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))

    astreams = [s for s in streams if s.get("codec_type") == "audio"]

    m = Meta()
    m.path = Path(path)
    m.rel = str(Path(path).relative_to(root))
    m.vindex = v.get("index", 0)
    m.aindex = astreams[0].get("index") if astreams else None
    m.audio_codec = astreams[0].get("codec_name") if astreams else None
    m.vcodec = v.get("codec_name", "?")
    m.pix_fmt = v.get("pix_fmt", "") or ""
    m.bits = 12 if "12" in m.pix_fmt else 10 if "10" in m.pix_fmt else 8
    m.color_transfer = v.get("color_transfer", "") or ""
    m.hdr = m.color_transfer in ("smpte2084", "arib-std-b67")

    w = v.get("width") or 0
    h = v.get("height") or 0
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rot = int(sd["rotation"])
            except (TypeError, ValueError):
                rot = 0
    m.rotated = rot % 180 != 0
    m.width, m.height = (h, w) if m.rotated else (w, h)

    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = v.get(key, "")
        if raw and raw != "0/0":
            num, _, den = raw.partition("/")
            try:
                den_f = float(den or 1)
                if den_f:
                    fps = float(num) / den_f
                    break
            except ValueError:
                pass
    m.fps = fps

    dur = data.get("format", {}).get("duration") or v.get("duration")
    try:
        m.duration = float(dur)
    except (TypeError, ValueError):
        m.duration = None
    m.size = m.path.stat().st_size
    return m


# ---------------------------------------------------------------- discovery


def find_videos(root, excludes):
    root = Path(root)
    if not root.is_dir():
        die("not a folder: %s" % root)
    excl = [e.lower() for e in DEFAULT_EXCLUDE_SUBSTRINGS + list(excludes)]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            low = d.lower()
            if d.startswith(".") or d.startswith("_"):
                continue
            if any(e in low for e in excl):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f.startswith(".") or f.startswith("._"):
                continue
            if Path(f).suffix.lower() not in VIDEO_EXTS:
                continue
            full = Path(dirpath) / f
            try:
                if full.stat().st_size < 1024:
                    continue
            except OSError:
                continue
            found.append(full)
    found.sort()
    return found


def probe_all(paths, root):
    metas = []
    for p in paths:
        try:
            m = probe(p, root)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print("warn: cannot probe %s (%s)" % (p.name, exc), file=sys.stderr)
            continue
        if m is None or m.duration is None or m.duration < 0.5:
            print("warn: skipping %s (no usable video)" % p.name, file=sys.stderr)
            continue
        metas.append(m)
    return metas


# ---------------------------------------------------------------- encoding


_CAPS = {}


def ff_caps():
    if not _CAPS:
        enc = run(["ffmpeg", "-hide_banner", "-encoders"]).stdout.decode()
        flt = run(["ffmpeg", "-hide_banner", "-filters"]).stdout.decode()
        _CAPS["encoders"] = set(re.findall(r"^\s*[A-Z.]{6}\s+(\S+)", enc, re.M))
        _CAPS["filters"] = set(re.findall(r"^\s*[A-Z.|]{3}\s+(\S+)", flt, re.M))
    return _CAPS


def choose_encoder(requested):
    caps = ff_caps()["encoders"]
    order = {
        "auto": ["hevc_videotoolbox", "libx265", "libx264"],
        "hevc-vt": ["hevc_videotoolbox"],
        "h264-vt": ["h264_videotoolbox"],
        "x265": ["libx265"],
        "x264": ["libx264"],
    }[requested]
    for name in order:
        if name in caps:
            return name
    die("no encoder available for --encoder %s (have: %s)"
        % (requested, ", ".join(sorted(c for c in caps if "26" in c or "videotoolbox" in c))))


def encoder_video_args(enc, meta, tonemapped, bitrate, crf, preset="medium"):
    """Return the -c:v section for this encoder and source."""
    ten_bit = meta.bits >= 10 and not tonemapped
    args = []
    if enc == "hevc_videotoolbox":
        args += ["-c:v", enc, "-b:v", bitrate, "-tag:v", "hvc1"]
        args += ["-pix_fmt", "p010le" if ten_bit else "nv12"]
    elif enc == "h264_videotoolbox":
        args += ["-c:v", enc, "-b:v", bitrate]
        args += ["-pix_fmt", "nv12"]
    elif enc == "libx265":
        args += ["-c:v", enc, "-crf", str(crf), "-preset", preset,
                 "-tag:v", "hvc1", "-x265-params", "log-level=error"]
        args += ["-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
    else:  # libx264
        args += ["-c:v", enc, "-crf", str(crf), "-preset", preset]
        args += ["-pix_fmt", "yuv420p"]
    return args


def audio_args(meta):
    if meta.aindex is None:
        return ["-an"]
    if meta.audio_codec == "aac":
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def escape_filter_path(path):
    p = str(path).replace("\\", "/")
    p = p.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
    return p


def vf_chain(meta, mode, lut=None):
    """Build the -vf chain. mode is 'h' or 'v'. Returns (chain, tonemapped)."""
    parts = []
    tonemapped = False
    if meta.hdr:
        filters = ff_caps()["filters"]
        if "zscale" in filters:
            parts.append(TONEMAP_ZSCALE)
            tonemapped = True
        elif "libplacebo" in filters:
            parts.append(TONEMAP_PLACEBO)
            tonemapped = True
        else:
            print(
                "warn: %s is HDR and this ffmpeg lacks zscale/libplacebo, "
                "keeping HDR as-is" % meta.rel,
                file=sys.stderr,
            )
    if lut:
        parts.append("lut3d=file='%s'" % escape_filter_path(lut))
    portrait = meta.height > meta.width
    if mode == "h":
        # For portrait sources the "phone-ready" target is 1080 wide,
        # not 1920 wide, otherwise a 9:16 source becomes 1920x3414.
        if portrait:
            parts.append("scale=min(1080\\,iw):-2")
        else:
            parts.append("scale=min(1920\\,iw):-2")
    else:  # vertical 9:16
        if portrait:
            parts.append("scale=min(1080\\,iw):-2")
        else:
            parts.append("crop=trunc(ih*9/32)*2:ih,scale=1080:1920")
    parts.append("setsar=1")
    return ",".join(parts), tonemapped


def transcode(meta, out_path, mode, opts, start=None, dur=None):
    """Encode one output file. Returns seconds elapsed."""
    chain, tonemapped = vf_chain(meta, mode, opts.lut)
    enc_args = encoder_video_args(
        opts.encoder_name, meta, tonemapped, opts.bitrate, opts.crf, opts.preset
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if start is not None:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", str(meta.path)]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-map", "0:%d" % meta.vindex]
    if meta.aindex is not None:
        cmd += ["-map", "0:%d" % meta.aindex]
    cmd += ["-vf", chain]
    cmd += enc_args
    cmd += audio_args(meta)
    if not meta.hdr or tonemapped:
        cmd += [
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
        ]
        # videotoolbox ignores the flags above, so stamp bt709 into the
        # bitstream VUI directly. x264/x265 honor the flags and skip this.
        if opts.encoder_name == "hevc_videotoolbox":
            cmd += ["-bsf:v", "hevc_metadata=colour_primaries=1:"
                    "transfer_characteristics=1:matrix_coefficients=1"]
        elif opts.encoder_name == "h264_videotoolbox":
            cmd += ["-bsf:v", "h264_metadata=colour_primaries=1:"
                    "transfer_characteristics=1:matrix_coefficients=1"]
    cmd += ["-map_metadata", "0", "-write_tmcd", "0",
            "-movflags", "+faststart", "-f", "mp4"]
    tmp = out_path.with_name(out_path.name + ".part")
    cmd += [str(tmp)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        run(cmd)
    except RuntimeError:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, out_path)
    return time.time() - t0


def copy_cut(meta, out_path, start, dur):
    """Keyframe-aligned stream copy. Instant, keeps original quality/res."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", "%.3f" % start, "-i", str(meta.path), "-t", "%.3f" % dur,
        "-map", "0:%d" % meta.vindex,
    ]
    if meta.aindex is not None:
        cmd += ["-map", "0:%d" % meta.aindex]
    cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    if meta.vcodec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-movflags", "+faststart", "-f", "mp4"]
    tmp = out_path.with_name(out_path.name + ".part")
    cmd += [str(tmp)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(cmd)
    except RuntimeError:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, out_path)


def modes_for(opts):
    if getattr(opts, "vertical_only", False):
        return ["v"]
    if getattr(opts, "vertical", False):
        return ["h", "v"]
    return ["h"]


def suffix_for(mode):
    return "_1080.mp4" if mode == "h" else "_1080v.mp4"


# ---------------------------------------------------------------- scan


def frame_stats(meta, at):
    """signalstats on one frame. Returns dict of floats or None."""
    try:
        p = run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", "%.3f" % at, "-i", str(meta.path),
                "-map", "0:%d" % meta.vindex, "-frames:v", "1",
                "-vf", "signalstats,metadata=mode=print:file=-",
                "-f", "null", "-",
            ]
        )
    except RuntimeError:
        return None
    stats = {}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        mm = re.search(r"lavfi\.signalstats\.(\w+)=([-\d.]+)", line)
        if mm:
            try:
                stats[mm.group(1)] = float(mm.group(2))
            except ValueError:
                pass
    return stats or None


def looks_flat(meta):
    """Heuristic: log/flat profiles have no true blacks or whites."""
    stats = frame_stats(meta, (meta.duration or 2) * 0.5)
    if not stats or "YMIN" not in stats or "YMAX" not in stats:
        return False
    peak = float(2 ** meta.bits - 1)
    ymin = stats["YMIN"] / peak
    ymax = stats["YMAX"] / peak
    return ymin > 0.10 and ymax < 0.88


def cmd_scan(opts):
    videos = find_videos(opts.root, opts.exclude)
    metas = probe_all(videos, opts.root)
    if not metas:
        die("no videos found under %s" % opts.root)
    total_dur = 0.0
    total_size = 0
    print()
    print("%-52s %-11s %-6s %-10s %8s %9s  %s"
          % ("file", "res", "fps", "codec", "dur", "size", "flags"))
    print("-" * 110)
    for m in metas:
        flags = []
        if m.bits > 8:
            flags.append("%dbit" % m.bits)
        if m.hdr:
            flags.append("HDR")
        elif not opts.fast and looks_flat(m):
            flags.append("flat?")
        if m.aindex is None:
            flags.append("no-audio")
        if m.height > m.width:
            flags.append("vertical")
        name = m.rel if len(m.rel) <= 52 else "..." + m.rel[-49:]
        print("%-52s %-11s %-6s %-10s %8s %9s  %s"
              % (
                  name,
                  "%dx%d" % (m.width, m.height),
                  "%.5g" % m.fps,
                  m.vcodec,
                  fmt_tc(m.duration),
                  fmt_size(m.size),
                  " ".join(flags),
              ))
        total_dur += m.duration
        total_size += m.size
    print("-" * 110)
    print("%d videos, %s of footage, %s"
          % (len(metas), fmt_tc(total_dur, True), fmt_size(total_size)))
    print()


# ---------------------------------------------------------------- mirror


def out_root(opts):
    return Path(opts.dest) if opts.dest else Path(opts.root) / OUT_ROOT_NAME


def fresh(out_path, src_path):
    try:
        return out_path.stat().st_mtime >= src_path.stat().st_mtime
    except OSError:
        return False


def cmd_mirror(opts):
    videos = find_videos(opts.root, opts.exclude)
    metas = probe_all(videos, opts.root)
    if not metas:
        die("no videos found under %s" % opts.root)
    dest = out_root(opts) / "library"
    modes = modes_for(opts)
    jobs = []
    for m in metas:
        for mode in modes:
            out = dest / Path(m.rel).parent / (Path(m.rel).stem + suffix_for(mode))
            if not opts.force and fresh(out, m.path):
                continue
            jobs.append((m, mode, out))
    skipped = len(metas) * len(modes) - len(jobs)
    print("mirror: %d to convert, %d already fresh, encoder %s"
          % (len(jobs), skipped, opts.encoder_name))
    done_bytes = 0
    for i, (m, mode, out) in enumerate(jobs, 1):
        try:
            secs = transcode(m, out, mode, opts)
        except RuntimeError as exc:
            print("FAIL %s (%s)" % (m.rel, exc), file=sys.stderr)
            continue
        size = out.stat().st_size
        done_bytes += size
        print("[%d/%d] %s -> %s  (%s, %.1fx realtime)"
              % (i, len(jobs), m.rel, out.name, fmt_size(size),
                 (m.duration or 0) / max(secs, 0.01)))
    print("done. outputs in: %s  (%s new)" % (dest, fmt_size(done_bytes)))


# ---------------------------------------------------------------- publish


CAM_BUCKETS = (
    (re.compile(r"^DJI[_-]", re.I), "drone aerials"),
    (re.compile(r"^(DSCF|FUJI)", re.I), "camera footage"),
    (re.compile(r"^C\d{4}", re.I), "camera footage"),
    (re.compile(r"^IMG[_E-]", re.I), "phone clips"),
)


def content_bucket(meta):
    stem = Path(meta.rel).name
    for pat, bucket in CAM_BUCKETS:
        if pat.match(stem):
            return bucket
    top = Path(meta.rel).parts[0].lower() if len(Path(meta.rel).parts) > 1 else ""
    if "drone" in top:
        return "drone aerials"
    if "iphone" in top or "phone" in top:
        return "phone clips"
    return "camera footage"


def motion_profile(meta, use_hwaccel=True):
    """One decode pass. Returns list of (t, scene_score, yavg) per frame."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if use_hwaccel:
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += [
        "-i", str(meta.path), "-map", "0:%d" % meta.vindex,
        "-vf",
        "scale=160:-2,select=gte(scene\\,0),signalstats,"
        "metadata=mode=print:file=-",
        "-an", "-f", "null", "-",
    ]
    try:
        p = run(cmd)
    except RuntimeError:
        if use_hwaccel:
            return motion_profile(meta, use_hwaccel=False)
        raise
    frames = []
    t = scene = yavg = None
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        mm = re.search(r"pts_time:([\d.]+)", line)
        if mm:
            if t is not None and scene is not None:
                frames.append((t, scene, yavg if yavg is not None else 0.0))
            t, scene, yavg = float(mm.group(1)), None, None
            continue
        mm = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if mm:
            scene = float(mm.group(1))
            continue
        mm = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", line)
        if mm:
            yavg = float(mm.group(1))
    if t is not None and scene is not None:
        frames.append((t, scene, yavg if yavg is not None else 0.0))
    return frames


def bin_series(frames, duration, step=0.5):
    """Average per time bin. Returns (times, motion, luma) parallel lists."""
    nbins = max(1, int(duration / step))
    acc = [[0.0, 0.0, 0] for _ in range(nbins)]
    for t, scene, yavg in frames:
        i = min(nbins - 1, int(t / step))
        acc[i][0] += scene
        acc[i][1] += yavg
        acc[i][2] += 1
    times, motion, luma = [], [], []
    for i, (s, y, n) in enumerate(acc):
        if n == 0:
            continue
        times.append(i * step)
        motion.append(s / n)
        luma.append(y / n)
    return times, motion, luma


def pick_windows(meta, frames, target_len, min_len, cut_thr, max_picks):
    """Rank candidate windows by motion, mid-file bias, dark penalty.

    Returns [(start, length, score)] non-overlapping, best first.
    """
    dur = meta.duration
    times, motion, luma = bin_series(frames, dur)
    if not times:
        return []
    step = times[1] - times[0] if len(times) > 1 else 0.5
    med_luma = sorted(luma)[len(luma) // 2] or 1.0

    # Segment boundaries at hard cuts so picks never straddle a cut.
    bounds = [0.0]
    for t, m in zip(times, motion):
        if m >= cut_thr and t - bounds[-1] > 1.0:
            bounds.append(t)
    bounds.append(dur)

    edge = max(3.0, dur * 0.08)
    candidates = []
    for a, b in zip(bounds, bounds[1:]):
        seg_len = b - a
        if seg_len < min_len:
            continue
        length = min(target_len, seg_len)
        starts = []
        s = a
        while s + length <= b + 0.01:
            starts.append(s)
            s += 1.0
        if not starts:
            starts = [a]
        for s in starts:
            idx = [i for i, t in enumerate(times) if s <= t < s + length]
            if not idx:
                continue
            mscore = sum(motion[i] for i in idx) / len(idx)
            lscore = sum(luma[i] for i in idx) / len(idx)
            w = 1.0
            if s < edge or (s + length) > dur - edge:
                w *= 0.6
            if lscore < 0.3 * med_luma:
                w *= 0.4
            candidates.append((s, length, mscore * w))
    candidates.sort(key=lambda c: -c[2])
    picks = []
    for s, length, score in candidates:
        if len(picks) >= max_picks:
            break
        if any(s < ps + pl + 2.0 and ps < s + length + 2.0 for ps, pl, _ in picks):
            continue
        picks.append((s, length, score))
    picks.sort(key=lambda p: p[0])
    return picks


def n_picks_for(duration):
    if duration < 75:
        return 1
    if duration < 180:
        return 2
    if duration < 360:
        return 3
    return 4


def nice_pick_name(meta, k, total, start, length, mode):
    trip = Path(meta.rel).parts[0].strip() if len(Path(meta.rel).parts) > 1 \
        else meta.path.parent.name.strip()
    stem = Path(meta.rel).stem
    mins, secs = int(start) // 60, int(start) % 60
    orient = "horizontal" if mode == "h" else "vertical"
    name = "%s - pick %d of %d - %ds - %s at %dm%02ds - %s.mp4" % (
        trip, k, total, round(length), stem, mins, secs, orient
    )
    # exFAT-safe: strip characters that break on camera drives
    return re.sub(r'[:*?"<>|/\\]', "-", name)


PLATFORM_DIR = {"h": "tiktok + reels", "v": "shorts + stories - vertical"}

PHONE_READY_README = """\
WHAT IS IN THIS FOLDER
======================

post-ready/
    Finished clips, ready to AirDrop and post. Nothing needs editing.
    tiktok + reels/            horizontal picks, grouped by content type
    shorts + stories - vertical/  the same moments as 9:16 vertical crops
    Names read: trip - pick 2 of 3 - 14s - DJI_0603 at 3m10s - horizontal
    That means: 14 second clip, taken 3m10s into DJI_0603, second-best
    of three picks from that video.

library/
    Every original converted once to phone-friendly 1080p, same folder
    layout as the drive. Grab full videos here.

contact-sheet.html
    Click frames of the long videos to hand-pick exact moments, copy the
    list, and the cut command exports them.

Originals are never touched. Everything in here can be regenerated.
"""


def cmd_publish(opts):
    videos = find_videos(opts.root, opts.exclude)
    metas = probe_all(videos, opts.root)
    metas = [m for m in metas if m.duration >= opts.min_source]
    if not metas:
        die("no videos of %ds+ found under %s" % (opts.min_source, opts.root))
    base = out_root(opts)
    dest = base / "post-ready"
    modes = ["h"] if opts.horizontal_only else ["h", "v"]
    (base).mkdir(parents=True, exist_ok=True)
    (base / "README.txt").write_text(PHONE_READY_README, encoding="utf-8")
    total_out = 0
    for fi, m in enumerate(metas, 1):
        try:
            frames = motion_profile(m)
        except RuntimeError as exc:
            print("FAIL analyze %s (%s)" % (m.rel, exc), file=sys.stderr)
            continue
        picks = pick_windows(
            m, frames, opts.target_len, opts.min_len, opts.scene,
            n_picks_for(m.duration) if opts.max_picks == 0 else opts.max_picks,
        )
        if not picks:
            print("[%d/%d] %s: no usable window" % (fi, len(metas), m.rel))
            continue
        bucket = content_bucket(m)
        print("[%d/%d] %s: %d picks (%s)"
              % (fi, len(metas), m.rel, len(picks), bucket))
        portrait = m.height > m.width
        for k, (start, length, _score) in enumerate(picks, 1):
            if portrait:
                # Already vertical: one encode, a copy in each pack.
                name = nice_pick_name(m, k, len(picks), start, length, "v")
                first = dest / PLATFORM_DIR["h"] / bucket / name
                second = dest / PLATFORM_DIR["v"] / bucket / name
                if opts.force or not fresh(first, m.path):
                    try:
                        transcode(m, first, "h", opts, start=start, dur=length)
                        total_out += 1
                    except RuntimeError as exc:
                        print("FAIL %s pick %d (%s)" % (m.rel, k, exc),
                              file=sys.stderr)
                        continue
                if first.exists() and (opts.force or not fresh(second, m.path)):
                    second.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(first, second)
                continue
            for mode in modes:
                out = (dest / PLATFORM_DIR[mode] / bucket /
                       nice_pick_name(m, k, len(picks), start, length, mode))
                if not opts.force and fresh(out, m.path):
                    continue
                try:
                    transcode(m, out, mode, opts, start=start, dur=length)
                    total_out += 1
                except RuntimeError as exc:
                    print("FAIL %s pick %d (%s)" % (m.rel, k, exc),
                          file=sys.stderr)
    print("done. %d clips in: %s" % (total_out, dest))


# ---------------------------------------------------------------- clips


def scene_times(meta, threshold):
    """Timestamps of detected scene changes, seconds, sorted."""
    try:
        p = run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(meta.path), "-map", "0:%d" % meta.vindex,
                "-vf",
                "select=gt(scene\\,%.3f),metadata=mode=print:file=-" % threshold,
                "-an", "-f", "null", "-",
            ]
        )
    except RuntimeError:
        return []
    times = []
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        mm = re.search(r"pts_time:([\d.]+)", line)
        if mm:
            times.append(float(mm.group(1)))
    return sorted(set(times))


def build_segments(duration, cuts, min_len, max_len):
    """Turn cut points into postable segments within [min_len, max_len]."""
    bounds = [0.0] + [t for t in cuts if 0.5 < t < duration - 0.5] + [duration]
    segments = []
    for a, b in zip(bounds, bounds[1:]):
        length = b - a
        if length < min_len:
            continue
        if length <= max_len:
            segments.append((a, length))
        else:
            n = math.ceil(length / max_len)
            piece = length / n
            if piece < min_len:
                n = max(1, int(length // min_len))
                piece = length / n
            for k in range(n):
                segments.append((a + k * piece, piece))
    return segments


def sample_evenly(items, cap):
    if cap <= 0 or len(items) <= cap:
        return items
    idx = [round(i * (len(items) - 1) / (cap - 1)) for i in range(cap)]
    return [items[i] for i in sorted(set(idx))]


def cmd_clips(opts):
    videos = find_videos(opts.root, opts.exclude)
    metas = probe_all(videos, opts.root)
    metas = [m for m in metas if m.duration >= opts.min_source]
    if not metas:
        die("no videos found under %s" % opts.root)
    dest = out_root(opts) / "_clips"
    modes = ["orig"] if opts.copy else modes_for(opts)
    total = 0
    for m in metas:
        if m.duration < opts.min_len:
            continue
        if opts.interval:
            cuts = []
            segs = build_segments(m.duration, [], opts.min_len, opts.interval)
        else:
            cuts = scene_times(m, opts.scene)
            segs = build_segments(m.duration, cuts, opts.min_len, opts.max_len)
        segs = sample_evenly(segs, opts.max_clips)
        if not segs:
            continue
        kind = "scene cuts" if cuts else "interval"
        print("%s: %d clips (%s)" % (m.rel, len(segs), kind))
        for n, (start, dur) in enumerate(segs, 1):
            stamp = "%02d%02d" % (int(start) // 60, int(start) % 60)
            base = "%s_c%02d_%s" % (Path(m.rel).stem, n, stamp)
            outdir = dest / Path(m.rel).parent
            try:
                if opts.copy:
                    copy_cut(m, outdir / (base + ".mp4"), start, dur)
                    total += 1
                else:
                    for mode in modes:
                        suffix = ".mp4" if mode == "h" else "_v.mp4"
                        transcode(
                            m, outdir / (base + suffix), mode, opts,
                            start=start, dur=dur,
                        )
                        total += 1
            except RuntimeError as exc:
                print("FAIL %s clip %d (%s)" % (m.rel, n, exc), file=sys.stderr)
    print("done. %d clips in: %s" % (total, dest))


# ---------------------------------------------------------------- sheet


SHEET_CSS = """
body { background:#111; color:#ddd; font:14px -apple-system,Helvetica,sans-serif;
       margin:0; padding:24px; }
h1 { font-size:20px; } h2 { font-size:15px; color:#8ab4f8; margin:28px 0 8px;
     border-bottom:1px solid #333; padding-bottom:4px; }
.clip { margin:0 0 18px; }
.clipname { font-weight:600; margin-bottom:2px; }
.meta { color:#888; font-size:12px; margin-bottom:6px; }
.strip { display:flex; flex-wrap:wrap; gap:6px; }
figure { margin:0; cursor:pointer; }
figure img { display:block; border:2px solid transparent; border-radius:4px; }
figure.on img { border-color:#8ab4f8; }
figcaption { text-align:center; font-size:11px; color:#aaa; padding-top:2px; }
#pickbox { position:sticky; bottom:0; background:#1b1b1b; border-top:1px solid #333;
           padding:12px; margin:24px -24px -24px; }
textarea { width:100%; height:90px; background:#0d0d0d; color:#9ecbff;
           border:1px solid #333; font:12px monospace; box-sizing:border-box; }
button { background:#8ab4f8; color:#000; border:0; border-radius:4px;
         padding:6px 14px; font-weight:600; cursor:pointer; margin-top:6px; }
.hint { color:#777; font-size:12px; margin-top:6px; }
"""

SHEET_JS = """
var picks = [];
function toggle(el, line) {
  var i = picks.indexOf(line);
  if (i >= 0) { picks.splice(i, 1); el.classList.remove('on'); }
  else { picks.push(line); el.classList.add('on'); }
  document.getElementById('picks').value = picks.join('\\n');
}
function copyPicks() {
  var ta = document.getElementById('picks');
  ta.select();
  try { navigator.clipboard.writeText(ta.value); } catch (e) {}
  try { document.execCommand('copy'); } catch (e) {}
}
"""


def thumb_jpeg(meta, at, width):
    p = run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", "%.3f" % at, "-i", str(meta.path),
            "-map", "0:%d" % meta.vindex, "-frames:v", "1",
            "-vf", "scale=%d:-2" % width, "-q:v", "7",
            "-f", "mjpeg", "pipe:1",
        ]
    )
    return p.stdout


def cmd_sheet(opts):
    videos = find_videos(opts.root, opts.exclude)
    metas = probe_all(videos, opts.root)
    metas = [m for m in metas if m.duration >= opts.min_source]
    if not metas:
        die("no videos found under %s" % opts.root)
    out_path = (
        Path(opts.out) if opts.out else out_root(opts) / "contact-sheet.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    by_folder = {}
    for m in metas:
        by_folder.setdefault(str(Path(m.rel).parent), []).append(m)

    blocks = []
    n_thumbs = 0
    for folder in sorted(by_folder):
        blocks.append("<h2>%s</h2>" % html.escape(folder if folder != "." else "top level"))
        for m in by_folder[folder]:
            count = max(4, min(opts.thumbs, int(m.duration // 3) + 1))
            times = [
                m.duration * (0.03 + 0.94 * i / max(count - 1, 1))
                for i in range(count)
            ]
            figs = []
            for t in times:
                try:
                    raw = thumb_jpeg(m, t, opts.width)
                except RuntimeError:
                    continue
                b64 = base64.b64encode(raw).decode("ascii")
                line = "%s @ %s" % (m.rel, fmt_tc(t))
                figs.append(
                    "<figure onclick=\"toggle(this,'%s')\">"
                    "<img src='data:image/jpeg;base64,%s' width='%d'>"
                    "<figcaption>%s</figcaption></figure>"
                    % (
                        html.escape(line, quote=True).replace("'", "&#39;"),
                        b64,
                        opts.width,
                        fmt_tc(t),
                    )
                )
                n_thumbs += 1
            meta_line = "%dx%d  %s  %s  %s" % (
                m.width, m.height, m.vcodec, fmt_tc(m.duration), fmt_size(m.size)
            )
            blocks.append(
                "<div class='clip'><div class='clipname'>%s</div>"
                "<div class='meta'>%s</div><div class='strip'>%s</div></div>"
                % (html.escape(Path(m.rel).name), meta_line, "".join(figs))
            )
            print("sheet: %s (%d thumbs)" % (m.rel, count))

    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>shutter-clip contact sheet</title>"
        "<style>%s</style><script>%s</script></head><body>"
        "<h1>contact sheet: %s</h1>"
        "<div class='meta'>%d videos. Click frames to build a picks list, "
        "then send it back or save as picks.txt and run the cut command.</div>"
        "%s"
        "<div id='pickbox'><textarea id='picks' spellcheck='false' "
        "placeholder='clicked frames appear here'></textarea><br>"
        "<button onclick='copyPicks()'>Copy picks</button>"
        "<div class='hint'>Line format: path @ m:ss  (add -m:ss for an end "
        "time, trailing v for vertical). Feed to: shutter_clip.py cut picks.txt ROOT"
        "</div></div>"
        "</body></html>"
    ) % (SHEET_CSS, SHEET_JS, html.escape(str(opts.root)), len(metas), "".join(blocks))

    out_path.write_text(doc, encoding="utf-8")
    print("done. %d thumbs -> %s (%s)"
          % (n_thumbs, out_path, fmt_size(out_path.stat().st_size)))


# ---------------------------------------------------------------- cut


PICK_RE = re.compile(
    r"^(?P<path>.+?)\s*@\s*(?P<start>[\d:.]+)\s*"
    r"(?:-\s*(?P<end>[\d:.]+))?\s*(?P<flags>[vhVH\s]*)$"
)


def cmd_cut(opts):
    picks_path = Path(opts.picks)
    if not picks_path.is_file():
        die("picks file not found: %s" % picks_path)
    videos = find_videos(opts.root, opts.exclude)
    by_rel = {}
    by_name = {}
    for p in videos:
        rel = str(p.relative_to(opts.root))
        by_rel[rel.lower()] = p
        by_name.setdefault(p.name.lower(), p)
    dest = out_root(opts) / "_cuts"
    made = 0
    for raw in picks_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        mm = PICK_RE.match(line)
        if not mm:
            print("warn: cannot parse pick: %s" % line, file=sys.stderr)
            continue
        rel = mm.group("path").strip()
        src = by_rel.get(rel.lower()) or by_name.get(Path(rel).name.lower())
        if src is None:
            print("warn: no such video: %s" % rel, file=sys.stderr)
            continue
        try:
            start = parse_tc(mm.group("start"))
            end = parse_tc(mm.group("end")) if mm.group("end") else None
        except ValueError as exc:
            print("warn: %s" % exc, file=sys.stderr)
            continue
        meta = probe(src, opts.root)
        if meta is None or meta.duration is None:
            print("warn: cannot probe %s" % rel, file=sys.stderr)
            continue
        dur = (end - start) if end else opts.length
        dur = max(1.0, min(dur, meta.duration - start))
        if dur <= 0:
            print("warn: start beyond end of %s" % rel, file=sys.stderr)
            continue
        flags = (mm.group("flags") or "").lower()
        if "v" in flags and "h" in flags:
            modes = ["h", "v"]
        elif "v" in flags:
            modes = ["v"]
        else:
            modes = modes_for(opts)
        stamp = "%02d%02d" % (int(start) // 60, int(start) % 60)
        for mode in modes:
            suffix = "_%s%s.mp4" % (stamp, "" if mode == "h" else "_v")
            out = dest / (Path(meta.rel).stem + suffix)
            try:
                transcode(meta, out, mode, opts, start=start, dur=dur)
                made += 1
                print("cut %s @ %s +%.1fs -> %s"
                      % (meta.rel, fmt_tc(start), dur, out.name))
            except RuntimeError as exc:
                print("FAIL %s (%s)" % (line, exc), file=sys.stderr)
    print("done. %d cuts in: %s" % (made, dest))


# ---------------------------------------------------------------- main


def add_common(sp, root=True):
    if root:
        sp.add_argument("root", help="footage drive or folder")
    sp.add_argument("--exclude", action="append", default=[],
                    help="skip folders whose name contains this (repeatable)")
    sp.add_argument("--dest", default=None,
                    help="output root (default: ROOT/%s)" % OUT_ROOT_NAME)
    sp.add_argument("--encoder", default="auto",
                    choices=["auto", "hevc-vt", "h264-vt", "x265", "x264"],
                    help="video encoder (default: auto)")
    sp.add_argument("--bitrate", default="10M",
                    help="bitrate for videotoolbox encoders (default 10M)")
    sp.add_argument("--crf", type=int, default=21,
                    help="quality for libx265/libx264 (default 21)")
    sp.add_argument("--preset", default="medium",
                    help="speed preset for libx265/libx264 (default medium)")
    sp.add_argument("--lut", default=None,
                    help="apply a .cube LUT to every output (for log footage)")
    sp.add_argument("-V", "--vertical", action="store_true",
                    help="also produce a 1080x1920 vertical center crop")
    sp.add_argument("--vertical-only", action="store_true",
                    help="produce only the vertical version")


def main(argv=None):
    which_or_die()
    ap = argparse.ArgumentParser(
        prog="shutter_clip.py",
        description="zero-edit social clips straight from a footage drive",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="inventory all videos")
    add_common(sp)
    sp.add_argument("--fast", action="store_true",
                    help="skip the flat-profile frame check")

    sp = sub.add_parser("mirror", help="phone-ready 1080p copy of every clip")
    add_common(sp)
    sp.add_argument("--force", action="store_true",
                    help="re-encode even if the output is fresh")

    sp = sub.add_parser(
        "publish",
        help="motion-ranked picks, organized by platform and content type",
    )
    add_common(sp)
    sp.add_argument("--min-source", type=float, default=45.0,
                    help="only pick from videos this long or longer (default 45)")
    sp.add_argument("--target-len", type=float, default=14.0,
                    help="preferred clip length seconds (default 14)")
    sp.add_argument("--min-len", type=float, default=6.0,
                    help="shortest usable clip seconds (default 6)")
    sp.add_argument("--scene", type=float, default=0.35,
                    help="hard-cut threshold, picks never straddle one")
    sp.add_argument("--max-picks", type=int, default=0,
                    help="picks per video, 0 = scale with duration (default)")
    sp.add_argument("--horizontal-only", action="store_true",
                    help="skip the vertical variants")
    sp.add_argument("--force", action="store_true",
                    help="re-encode even if the output is fresh")

    sp = sub.add_parser("clips", help="auto-cut videos into short pieces")
    add_common(sp)
    sp.add_argument("--min-len", type=float, default=6.0,
                    help="shortest clip seconds (default 6)")
    sp.add_argument("--max-len", type=float, default=18.0,
                    help="longest clip seconds (default 18)")
    sp.add_argument("--scene", type=float, default=0.30,
                    help="scene change threshold 0..1 (default 0.30)")
    sp.add_argument("--interval", type=float, default=None,
                    help="skip scene detection, chunk every N seconds")
    sp.add_argument("--max-clips", type=int, default=15,
                    help="cap clips per video, 0 = no cap (default 15)")
    sp.add_argument("--copy", action="store_true",
                    help="stream-copy original quality, keyframe-aligned cuts")
    sp.add_argument("--min-source", type=float, default=0.0,
                    help="only auto-cut videos at least this many seconds long")

    sp = sub.add_parser("sheet", help="HTML contact sheet for picking moments")
    add_common(sp)
    sp.add_argument("--min-source", type=float, default=0.0,
                    help="only sheet videos at least this many seconds long")
    sp.add_argument("--thumbs", type=int, default=10,
                    help="max thumbnails per video (default 10)")
    sp.add_argument("--width", type=int, default=320,
                    help="thumbnail width px (default 320)")
    sp.add_argument("--out", default=None,
                    help="output html path (default: _phone-ready/contact-sheet.html)")

    sp = sub.add_parser("cut", help="export moments from a picks file")
    sp.add_argument("picks", help="picks file (from the contact sheet)")
    add_common(sp)
    sp.add_argument("--length", type=float, default=12.0,
                    help="clip seconds when a pick has no end time (default 12)")

    opts = ap.parse_args(argv)
    opts.encoder_name = choose_encoder(opts.encoder)
    if opts.command in ("mirror", "cut") or (
        opts.command == "clips" and not opts.copy
    ):
        print("encoder: %s" % opts.encoder_name)

    {
        "scan": cmd_scan,
        "mirror": cmd_mirror,
        "publish": cmd_publish,
        "clips": cmd_clips,
        "sheet": cmd_sheet,
        "cut": cmd_cut,
    }[opts.command](opts)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
