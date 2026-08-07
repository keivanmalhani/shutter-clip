#!/usr/bin/env python3
"""shutter-clip: zero-edit social clips straight from a footage drive.

Point it at a drive or folder of camera originals. It inventories, converts,
auto-cuts, and exports phone-ready files you can AirDrop and post as-is.

Subcommands:
  scan     inventory every video: codec, resolution, fps, duration, flags
  mirror   phone-ready 1080p copy of every clip into _phone-ready/library/
  publish  motion-ranked best moments, named readably and organized into
           platform packs (tiktok + reels, shorts + stories) by content type
  rank     deep-rank footage using shutter-select's analysis (speech, audio
           quality, sharpness, motion): ranked report + picks file for cut
  clips    plain auto-cut into short pieces via scene detection
  sheet    build a self-contained HTML contact sheet for picking moments
  cut      export exact moments listed in a picks file
  frames   dump 3-frame review strips for every published pick
  curate   apply keep/kill/top verdicts from a frames review

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
import hashlib
import html
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

__version__ = "0.1.0"

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


# ---------------------------------------------------------------- cache


META_FIELDS = (
    "duration", "width", "height", "fps", "vcodec", "pix_fmt", "bits",
    "hdr", "color_transfer", "audio_codec", "vindex", "aindex", "size",
    "rotated",
)


class Cache:
    """Probe + motion-profile cache so re-runs skip the expensive passes.

    Lives at <out_root>/.shutter-cache.json. Keyed by rel|size|mtime so a
    replaced or re-copied file re-probes automatically.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {"version": 1, "probe": {}, "profile": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if loaded.get("version") == 1:
                self.data = loaded
        except (OSError, ValueError):
            pass
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(path, root):
        st = Path(path).stat()
        return "%s|%d|%d" % (
            Path(path).relative_to(root), st.st_size, int(st.st_mtime)
        )

    def get_meta(self, key, path, root):
        d = self.data["probe"].get(key)
        if d is None:
            return None
        m = Meta()
        m.path = Path(path)
        m.rel = str(Path(path).relative_to(root))
        for f in META_FIELDS:
            setattr(m, f, d.get(f))
        return m

    def put_meta(self, key, meta):
        with self.lock:
            self.data["probe"][key] = {
                f: getattr(meta, f) for f in META_FIELDS
            }

    def get_profile(self, key, fast=False):
        space = "profile_fast" if fast else "profile"
        return self.data.setdefault(space, {}).get(key)

    def put_profile(self, key, series, fast=False):
        space = "profile_fast" if fast else "profile"
        with self.lock:
            self.data.setdefault(space, {})[key] = [
                [round(x, 3) for x in series[0]],
                [round(x, 4) for x in series[1]],
                [round(x, 2) for x in series[2]],
            ]

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print("warn: cache save failed (%s)" % exc, file=sys.stderr)


def open_cache(opts):
    return Cache(out_root(opts) / ".shutter-cache.json")


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


def probe_all(paths, root, cache=None):
    metas = []
    for p in paths:
        key = None
        if cache is not None:
            try:
                key = Cache.key(p, root)
            except OSError:
                continue
            m = cache.get_meta(key, p, root)
            if m is not None:
                cache.hits += 1
                if m.duration and m.duration >= 0.5:
                    metas.append(m)
                continue
        try:
            m = probe(p, root)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print("warn: cannot probe %s (%s)" % (p.name, exc), file=sys.stderr)
            continue
        if m is None or m.duration is None or m.duration < 0.5:
            print("warn: skipping %s (no usable video)" % p.name, file=sys.stderr)
            continue
        if cache is not None and key is not None:
            cache.put_meta(key, m)
            cache.misses += 1
        metas.append(m)
    if cache is not None and cache.misses:
        cache.save()
    if cache is not None:
        print("probe cache: %d hits, %d fresh" % (cache.hits, cache.misses))
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


def vf_chain(meta, mode, lut=None, canvas=None):
    """Build the -vf chain. mode is 'h' or 'v'. Returns (chain, tonemapped).

    canvas=(w, h) forces an exact fill-crop output at 30 fps 8-bit,
    used for montage segments so concat gets identical streams.
    """
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
    if canvas:
        cw, ch = canvas
        parts.append(
            "scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,fps=30,format=yuv420p,setsar=1" % (cw, ch, cw, ch)
        )
        return ",".join(parts), tonemapped
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


def transcode(meta, out_path, mode, opts, start=None, dur=None,
              canvas=None, no_audio=False, hwaccel=True):
    """Encode one output file. Returns seconds elapsed."""
    chain, tonemapped = vf_chain(meta, mode, opts.lut, canvas)
    enc_args = encoder_video_args(
        opts.encoder_name, meta, tonemapped or bool(canvas),
        opts.bitrate, opts.crf, opts.preset
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    # Hardware decode. Without -hwaccel_output_format frames land back
    # in system memory, so the normal filter chain still applies.
    # Software decode of 4K 10-bit pins every core: measured 2.7
    # files/min vs 21 files/min on the M3 Pro.
    if hwaccel and not getattr(opts, "no_hwaccel", False):
        cmd += ["-hwaccel", "videotoolbox"]
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
    cmd += ["-an"] if no_audio else audio_args(meta)
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
            try:
                tmp.unlink()
            except OSError:
                pass
        if hwaccel and not getattr(opts, "no_hwaccel", False):
            # Some codecs/profiles have no hardware decoder. Fall back.
            return transcode(meta, out_path, mode, opts, start=start,
                             dur=dur, canvas=canvas, no_audio=no_audio,
                             hwaccel=False)
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
    metas = probe_all(videos, opts.root, open_cache(opts))
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
    metas = probe_all(videos, opts.root, open_cache(opts))
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
    done_bytes = [0]
    lock = threading.Lock()

    def mirror_one(args):
        i, (m, mode, out) = args
        try:
            secs = transcode(m, out, mode, opts)
        except RuntimeError as exc:
            with lock:
                print("FAIL %s (%s)" % (m.rel, exc), file=sys.stderr)
            return
        size = out.stat().st_size
        with lock:
            done_bytes[0] += size
            print("[%d/%d] %s -> %s  (%s, %.1fx realtime)"
                  % (i, len(jobs), m.rel, out.name, fmt_size(size),
                     (m.duration or 0) / max(secs, 0.01)))

    with ThreadPoolExecutor(max_workers=max(1, opts.encode_workers)) as ex:
        list(ex.map(mirror_one, enumerate(jobs, 1)))
    print("done. outputs in: %s  (%s new)" % (dest, fmt_size(done_bytes[0])))


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


def motion_profile(meta, use_hwaccel=True, fast=False):
    """One decode pass. Returns list of (t, scene_score, yavg) per frame.

    fast=True decodes keyframes only (-skip_frame nokey): ~1-2 samples
    per second instead of every frame, an order of magnitude faster on
    4K sources. Scene scores ride higher because frames are further
    apart; pick_windows compensates on the cut threshold.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if use_hwaccel:
        cmd += ["-hwaccel", "videotoolbox"]
    if fast:
        cmd += ["-skip_frame", "nokey"]
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
            return motion_profile(meta, use_hwaccel=False, fast=fast)
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


def pick_windows(meta, series, target_len, min_len, cut_thr, max_picks,
                 edge_weight=0.6):
    """Rank candidate windows by motion, mid-file bias, dark penalty.

    series is (times, motion, luma) from bin_series or the cache.
    Returns [(start, length, score)] non-overlapping, best first.
    """
    dur = meta.duration
    times, motion, luma = series
    if not times:
        return []
    if len(motion) > 4:
        # First samples after a decoder start can carry a garbage-high
        # scene score; clamp them so takeoff frames never win on noise.
        med = sorted(motion)[len(motion) // 2]
        motion = [min(x, med * 3) for x in motion[:2]] + list(motion[2:])
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
                w *= edge_weight
            if lscore < 0.3 * med_luma:
                w *= 0.4
            # Absolute darkness floor: a uniformly dark file has a dark
            # median too, so the relative rule alone lets night duds win.
            if lscore < 32.0 * (2 ** (meta.bits - 8)):
                w *= 0.15
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
    tiktok + reels/               horizontal picks, grouped by content type
    shorts + stories - vertical/  the same moments as 9:16 vertical crops
    Both packs also hold montages/: one ~30s auto-edit per trip folder,
    5 second cuts, chronological, silent on purpose - add a trending
    sound in the app, native audio boosts reach.
    Pick names read: trip - pick 2 of 3 - 14s - DJI_0603 at 3m10s
    Meaning: 14 second clip, taken 3m10s into DJI_0603, second-best of
    three picks from that video.

library/
    Every original converted once to phone-friendly 1080p, same folder
    layout as the drive. Grab full videos here.

contact-sheet.html
    Click frames of the long videos to hand-pick exact moments, copy the
    list, and the cut command exports them.

Originals are never touched. Everything in here can be regenerated.
See WHY.txt for the reasoning behind formats and sizes.
"""

WHY_TXT = """\
WHY THESE FORMATS
=================

1080p everywhere: TikTok and Instagram re-encode every upload to about
1080p, so uploading 4K buys nothing on those apps. 1080p files are a
tenth the size, AirDrop in seconds, and look identical after the app
re-encode. The 4K originals stay untouched on the drive, and library/
keeps a browsable 1080p twin of every one.

HEVC with the hvc1 tag: half the file size of H.264 at the same quality,
decoded natively by every iPhone since the 7. If some app ever rejects
one, rerun with --encoder x264.

Horizontal first: your call, and the tiktok + reels pack keeps it.
Every pick also gets an automatic 9:16 center-crop twin in the shorts
pack because stories and YT Shorts require vertical.

Pick length ~14s: short clips get watched to the end, and completion
rate is the strongest ranking signal on TikTok. Montages run ~30s with
5s cuts for the edited feel without editing.

Same look for every folder on purpose: a consistent grade across the
feed reads as a style. Sources differ (4K drone, 6K Fuji, vertical
phone) but every export lands on the same canvas rules, so nothing
looks out of place next to anything else.

Vertical sources are never stretched: portrait files keep their framing
and land at 1080 wide. Landscape files are center-cropped for vertical
twins. HDR iPhone clips currently stay HDR (this ffmpeg build cannot
tone-map); they look right on iPhone.
"""


def top_folder(meta):
    parts = Path(meta.rel).parts
    return parts[0].strip() if len(parts) > 1 else meta.path.parent.name.strip()


def montage_out(dest, mode, folder, seconds):
    orient = "horizontal" if mode == "h" else "vertical"
    name = "%s - montage - %ds - %s.mp4" % (folder, round(seconds), orient)
    name = re.sub(r'[:*?"<>|/\\]', "-", name)
    return dest / PLATFORM_DIR[mode] / "montages" / name


def title_card(text, canvas, opts, meta, out_path, tmpdir):
    """1.2s black title card. Needs drawtext; returns False if absent."""
    if "drawtext" not in ff_caps()["filters"]:
        return False
    txt = tmpdir / "title.txt"
    txt.write_text(text, encoding="utf-8")
    vf = (
        "drawtext=textfile='%s':fontcolor=white:fontsize=%d:"
        "x=(w-text_w)/2:y=(h-text_h)/2,format=yuv420p"
        % (escape_filter_path(txt), canvas[1] // 16)
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi",
        "-i", "color=c=black:size=%dx%d:rate=30:duration=1.2"
              % (canvas[0], canvas[1]),
        "-vf", vf,
    ]
    cmd += encoder_video_args(
        opts.encoder_name, meta, True, opts.bitrate, opts.crf, opts.preset
    )
    cmd += ["-an", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-movflags", "+faststart", "-f", "mp4",
            str(out_path)]
    try:
        run(cmd)
        return True
    except RuntimeError as exc:
        print("warn: title card failed (%s)" % exc, file=sys.stderr)
        return False


def build_montage(folder, windows, mode, opts, dest, base):
    """windows: [(meta, start)] chronological. Concat 5s segments.

    Loop-close appends a 2.5s reprise of the opening shot so the video
    lands back where it started and loops cleanly on TikTok.
    """
    canvas = (1920, 1080) if mode == "h" else (1080, 1920)
    seg_len = opts.montage_seg
    segs = [(m, start, seg_len) for m, start in windows]
    total = seg_len * len(windows)
    if getattr(opts, "loop_close", True) and windows:
        segs.append((windows[0][0], windows[0][1], 2.5))
        total += 2.5
    out = montage_out(dest, mode, folder, total)
    if not opts.force and out.exists():
        return False
    tmpdir = base / ".tmp-montage" / re.sub(r"[^A-Za-z0-9]+", "-", folder + mode)
    tmpdir.mkdir(parents=True, exist_ok=True)
    seg_paths = []
    try:
        if getattr(opts, "title_cards", False):
            card = tmpdir / "seg_card.mp4"
            if title_card(folder, canvas, opts, windows[0][0], card, tmpdir):
                seg_paths.append(card)
        for i, (m, start, dur) in enumerate(segs):
            seg = tmpdir / ("seg%02d.mp4" % i)
            transcode(m, seg, mode, opts, start=start, dur=dur,
                      canvas=canvas, no_audio=True)
            seg_paths.append(seg)
        lst = tmpdir / "list.txt"
        lst.write_text(
            "".join("file '%s'\n" % str(p).replace("'", "'\\''")
                    for p in seg_paths),
            encoding="utf-8",
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out.with_name(out.name + ".part")
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4",
            str(tmp_out),
        ])
        os.replace(tmp_out, out)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def pick_candidates(metas, min_source):
    """Depth: every file >= min_source. Breadth: every folder covered."""
    by_folder = {}
    for m in metas:
        by_folder.setdefault(top_folder(m), []).append(m)
    chosen = []
    for folder, group in sorted(by_folder.items()):
        deep = [m for m in group if m.duration >= min_source]
        if len(deep) < 2:
            floor = sorted(
                (m for m in group if m.duration >= 10 and m not in deep),
                key=lambda m: -m.duration,
            )[: 2 - len(deep)]
            deep = deep + floor
        chosen.extend(deep)
    return chosen


BUCKET_TARGET = {
    "drone aerials": 20.0,
    "camera footage": 12.0,
    "phone clips": 8.0,
}


def cmd_publish(opts):
    videos = find_videos(opts.root, opts.exclude)
    cache = open_cache(opts)
    metas = probe_all(videos, opts.root, cache)
    candidates = pick_candidates(metas, opts.min_source)
    if not candidates:
        die("no usable videos under %s" % opts.root)
    base = out_root(opts)
    dest = base / "post-ready"
    modes = ["h"] if opts.horizontal_only else ["h", "v"]
    base.mkdir(parents=True, exist_ok=True)
    (base / "README.txt").write_text(PHONE_READY_README, encoding="utf-8")
    (dest).mkdir(parents=True, exist_ok=True)
    (dest / "WHY.txt").write_text(WHY_TXT, encoding="utf-8")

    fast = not opts.full_analysis

    def analyze_one(m):
        try:
            key = Cache.key(m.path, opts.root)
            full = cache.get_profile(key, False)
            if full is not None:
                # A full-decode profile is strictly better; use it if
                # any previous run already paid for it.
                return m, full, None, False
            series = cache.get_profile(key, fast)
            if series is None:
                cache.put_profile(
                    key,
                    bin_series(motion_profile(m, fast=fast), m.duration),
                    fast,
                )
                # Read back so fresh and cached runs see identical
                # rounded values and pick identical windows.
                series = cache.get_profile(key, fast)
            return m, series, None, fast
        except (RuntimeError, OSError) as exc:
            return m, None, exc, fast

    analyzed = []
    done_n = 0
    print("analyzing %d candidates, %d workers"
          % (len(candidates), opts.workers))
    with ThreadPoolExecutor(max_workers=opts.workers) as ex:
        for m, series, err, used_fast in ex.map(analyze_one, candidates):
            done_n += 1
            if err is not None:
                print("FAIL analyze %s (%s)" % (m.rel, err), file=sys.stderr)
                continue
            analyzed.append((m, series, used_fast))
            if done_n % 20 == 0:
                print("analyzed %d/%d" % (done_n, len(candidates)))
                cache.save()
    cache.save()

    total_out = 0
    plan = []
    for m, series, used_fast in analyzed:
        bucket = content_bucket(m)
        t_len = opts.target_len or BUCKET_TARGET.get(bucket, 14.0)
        cap = n_picks_for(m.duration) if opts.max_picks == 0 else opts.max_picks
        if m.duration < opts.min_source:
            cap = 1
        # Keyframe-sampled scores ride higher, so hard cuts need a
        # higher bar in fast mode.
        thr = opts.scene if not used_fast else max(opts.scene, 0.55)
        # Keyframe sampling over-rewards takeoff/landing churn, so damp
        # the file edges harder in fast mode.
        edge_w = 0.6 if not used_fast else 0.35
        picks = pick_windows(m, series, t_len, opts.min_len, thr, cap,
                             edge_weight=edge_w)
        if not picks and m.duration >= opts.min_len:
            picks = [(0.0, min(m.duration, t_len), 0.0)]
        if not picks:
            continue
        plan.append([m, bucket, picks])

    # Cross-folder dedupe: the same source file re-copied into several
    # folders yields identical picks. Claim each (stem, second) once,
    # graded folders win: master > original > plain > insta > copy.
    def folder_priority(name):
        f = name.lower()
        if "master" in f:
            return 0
        if "original" in f:
            return 1
        if "insta version" in f:
            return 3
        if "copy" in f:
            return 4
        return 2

    def norm_stem(m):
        return re.sub(r"-\d+$", "", Path(m.rel).stem.upper())

    claimed = {}
    dropped = 0
    for item in sorted(plan, key=lambda it: folder_priority(top_folder(it[0]))):
        m, bucket, picks = item
        folder = top_folder(m)
        kept = []
        for pk in picks:
            key = (norm_stem(m), int(round(pk[0])))
            owner = claimed.setdefault(key, folder)
            if owner == folder:
                kept.append(pk)
            else:
                dropped += 1
        item[2] = kept
    plan = [it for it in plan if it[2]]
    if dropped:
        print("dedupe: skipped %d duplicate picks from copy folders" % dropped)

    folder_windows = {}
    for m, bucket, picks in plan:
        for start, length, score in picks[:2]:
            folder_windows.setdefault(top_folder(m), []).append(
                (score, m, start, length)
            )
    print("encode plan: %d videos, %d picks"
          % (len(plan), sum(len(p) for _, _, p in plan)))

    print_lock = threading.Lock()

    def encode_item(args):
        pi, (m, bucket, picks) = args
        with print_lock:
            print("[%d/%d] %s: %d picks (%s)"
                  % (pi, len(plan), m.rel, len(picks), bucket))
        made = 0
        portrait = m.height > m.width
        for k, (start, length, _score) in enumerate(picks, 1):
            if portrait:
                # Already vertical: one encode, a copy in each pack.
                name = nice_pick_name(m, k, len(picks), start, length, "v")
                if (dest / "b-sides" / name).exists():
                    continue  # curated away, stays away
                first = dest / PLATFORM_DIR["h"] / bucket / name
                second = dest / PLATFORM_DIR["v"] / bucket / name
                if opts.force or not fresh(first, m.path):
                    try:
                        transcode(m, first, "h", opts, start=start, dur=length)
                        made += 1
                    except RuntimeError as exc:
                        with print_lock:
                            print("FAIL %s pick %d (%s)" % (m.rel, k, exc),
                                  file=sys.stderr)
                        continue
                if first.exists() and (opts.force or not fresh(second, m.path)):
                    second.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(first, second)
                continue
            for mode in modes:
                name = nice_pick_name(m, k, len(picks), start, length, mode)
                if (dest / "b-sides" / name).exists():
                    continue  # curated away, stays away
                out = dest / PLATFORM_DIR[mode] / bucket / name
                if not opts.force and fresh(out, m.path):
                    continue
                try:
                    transcode(m, out, mode, opts, start=start, dur=length)
                    made += 1
                except RuntimeError as exc:
                    with print_lock:
                        print("FAIL %s pick %d (%s)" % (m.rel, k, exc),
                              file=sys.stderr)
        return made

    workers = max(1, opts.encode_workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for made in ex.map(encode_item, enumerate(plan, 1)):
            total_out += made

    if opts.montage_len > 0:
        want = max(3, int(round(opts.montage_len / opts.montage_seg)))
        for folder in sorted(folder_windows):
            pool = folder_windows[folder]
            for mode in modes:
                usable = [
                    (score, m, start, length)
                    for score, m, start, length in pool
                    # HDR sources would need a tonemap to sit next to SDR
                    # cuts in one file; skip them until zscale is present.
                    if not m.hdr and (mode == "v" or m.width >= m.height)
                ]
                usable.sort(key=lambda w: -w[0])
                seen = {}
                take = []
                for score, m, start, length in usable:
                    if seen.get(m.rel, 0) >= 2:
                        continue
                    seen[m.rel] = seen.get(m.rel, 0) + 1
                    take.append((m, start + max(0.0, (length - opts.montage_seg) / 2)))
                    if len(take) >= want:
                        break
                if len(take) < 3:
                    continue
                take.sort(key=lambda w: (w[0].rel, w[1]))
                try:
                    made = build_montage(folder, take, mode, opts, dest, base)
                    if made:
                        total_out += 1
                        print("montage: %s (%s, %d cuts)"
                              % (folder, mode, len(take)))
                except RuntimeError as exc:
                    print("FAIL montage %s %s (%s)" % (folder, mode, exc),
                          file=sys.stderr)

    print("done. %d clips in: %s" % (total_out, dest))


# ---------------------------------------------------------------- rank
#
# Idea 5 phase 1: consume shutter-select's per-file analysis JSON and
# re-rank every analyzed segment with social weights. The contract is the
# cache JSON only (schema_version 1): shutter-select is never imported,
# and when --analyze is passed its CLI runs as a subprocess. Raw features
# are stored in the cache precisely so this re-weighting needs no
# re-analysis (see the shutter-select spec, "Cache JSON contract").


SELECTS_SCHEMA = 1
SELECTS_DIR_NAME = "_selects"

# Social weight sets. Same percentile-normalize-within-class pattern as
# shutter-select's scoring, different priorities: for feed clips motion
# and hook energy outrank the interview virtues. Weights of features a
# segment is missing (faces off, say) redistribute proportionally.
SOCIAL_SPEECH_WEIGHTS = {
    "audio_quality": 0.25,
    "speech_density": 0.15,
    "motion": 0.15,
    "sharpness": 0.15,
    "exposure": 0.10,
    "duration_fit": 0.10,
    "faces": 0.10,
}
SOCIAL_BROLL_WEIGHTS = {
    "motion": 0.40,
    "sharpness": 0.25,
    "exposure": 0.15,
    "duration_fit": 0.20,
}
MIN_SPEECH_RMS_DB = -35.0  # matches shutter-select's hard-fail line


def percentile_ranks(values):
    """Rank positions scaled 0..1. One value ranks 0.5.

    Ties share their average rank, a deliberate divergence from
    shutter-select's index-order ties: footage drives hold identical
    copies of the same clip in several folders, and index-order ties
    hand one copy free rank points over the other. Averaging keeps the
    ordering content-only and independent of walk order.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: (values[i], i))
    ranks = [0.0] * n
    pos = 0
    while pos < n:
        j = pos
        while j + 1 < n and values[order[j + 1]] == values[order[pos]]:
            j += 1
        avg = (pos + j) / 2.0 / (n - 1)
        for k in range(pos, j + 1):
            ranks[order[k]] = avg
        pos = j + 1
    return ranks


def exposure_quality(seg):
    """1.0 is clean, 0.0 is badly crushed or blown out."""
    return 1.0 - min(
        1.0,
        seg.get("crushed_frac", 0.0) * 3.0 + seg.get("blown_frac", 0.0) * 4.0,
    )


def social_duration_fit(duration, lo, hi):
    """Peak inside the postable band. Long takes decay gently, never to
    zero: they are trimmable, and phase 2 sub-clips them on words."""
    if duration <= 0:
        return 0.0
    if duration < lo:
        return duration / lo
    if duration <= hi:
        return 1.0
    return max(0.35, 1.0 - (duration - hi) / 60.0)


def audio_quality_raw(seg):
    """Speech-over-noise margin, punished by dead air. Same shape as
    shutter-select's extractor so the two tools agree on what clean is."""
    return seg.get("noise_margin_db", 0.0) - 20.0 * seg.get("silence_ratio", 0.0)


def selects_cache_payload(cache_dir, path):
    """Load one shutter-select cache JSON for a source file.

    Mirrors shutter-select's own validation: schema gate first, then
    mtime plus size, so a re-copied or re-graded file reads as stale
    here exactly when it would over there. Returns (payload, status),
    status one of ok, missing, stale, schema.
    """
    key = hashlib.sha1(
        str(Path(path).resolve()).encode("utf-8")
    ).hexdigest()[:16]
    target = Path(cache_dir) / (key + ".json")
    if not target.exists():
        return None, "missing"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "stale"
    if payload.get("schema_version") != SELECTS_SCHEMA:
        return None, "schema"
    recorded = payload.get("source", {})
    try:
        st = Path(path).stat()
    except OSError:
        return None, "stale"
    if abs(recorded.get("mtime", -1) - st.st_mtime) > 1e-6:
        return None, "stale"
    if recorded.get("size") != st.st_size:
        return None, "stale"
    return payload, "ok"


def run_select_analyze(opts, selects_dir):
    """Run shutter-select analyze as a subprocess. The only integration
    point is its CLI plus the cache files it writes."""
    cmd = shlex.split(opts.select_bin) + ["analyze", str(opts.root)]
    cmd += ["--out", str(selects_dir)]
    if opts.select_args:
        cmd += shlex.split(opts.select_args)
    print("rank: running %s" % " ".join(cmd))
    try:
        proc = subprocess.run(cmd)
    except FileNotFoundError:
        die(
            "shutter-select not found (looked for: %s).\n"
            "Install it (https://github.com/keivanmalhani/shutter-select) "
            "or run the analysis yourself:\n"
            "  shutter-select analyze %s" % (opts.select_bin, opts.root)
        )
    if proc.returncode != 0:
        die("shutter-select analyze failed (exit %d)" % proc.returncode)


def social_score(rows, min_len, max_len):
    """Score every segment row in place: social_score 0..1 plus
    social_percentile within its class. Percentiles are run-relative,
    the shutter-cull/select pattern: no absolute thresholds on
    scene-dependent features."""
    extractors = {
        "audio_quality": audio_quality_raw,
        "speech_density": lambda s: s.get("words_per_second", 0.0),
        "sharpness": lambda s: s.get("sharpness", 0.0),
        "motion": lambda s: s.get("motion", 0.0),
        "exposure": exposure_quality,
        "duration_fit": lambda s: social_duration_fit(
            s.get("duration", 0.0), min_len, max_len
        ),
        "faces": lambda s: s.get("face_ratio"),
    }
    by_class = {}
    for i, row in enumerate(rows):
        by_class.setdefault(row.get("klass", "broll"), []).append(i)
    for klass, indices in by_class.items():
        weights = (
            SOCIAL_SPEECH_WEIGHTS if klass == "speech" else SOCIAL_BROLL_WEIGHTS
        )
        per_feature = {}
        for feature in weights:
            raws = [(extractors[feature](rows[i]), i) for i in indices]
            present = [(v, i) for v, i in raws if v is not None]
            ranks = percentile_ranks([v for v, _ in present])
            table = {i: None for i in indices}
            for (_, i), rank in zip(present, ranks):
                table[i] = rank
            per_feature[feature] = table
        composites = []
        for i in indices:
            avail = {
                f: w for f, w in weights.items()
                if per_feature[f][i] is not None
            }
            total = sum(avail.values())
            if total <= 0:
                comp = 0.5
            else:
                comp = sum(
                    per_feature[f][i] * (w / total) for f, w in avail.items()
                )
            rows[i]["social_score"] = round(comp, 4)
            composites.append(comp)
        for i, pct in zip(indices, percentile_ranks(composites)):
            rows[i]["social_percentile"] = round(pct, 4)


def social_skips(row, min_len):
    """Reasons a segment is unpostable regardless of its score. Skips are
    decided before scoring so unpostable segments never distort the
    percentile pool the survivors are ranked in."""
    reasons = []
    if row.get("frames_sampled", 0) == 0:
        reasons.append("no frame could be decoded")
    if row["t_out"] - row["t_in"] < min_len:
        reasons.append("shorter than the %ds minimum" % round(min_len))
    if row.get("klass") == "speech":
        if row.get("clipped"):
            reasons.append("audio clipped, distorted at full volume")
        if row.get("rms_db", 0.0) < MIN_SPEECH_RMS_DB:
            reasons.append("speech too quiet to post")
    return reasons


def clip_window(row, t_len):
    """Postable window inside a segment. Speech hooks live at the start
    of a take, so speech windows anchor there; b-roll centers. Word-level
    windowing is phase 2."""
    t_in = row["t_in"]
    dur = row["t_out"] - t_in
    if dur <= t_len + 2.0:
        return t_in, dur
    if row.get("klass") == "speech":
        return t_in, t_len
    return t_in + (dur - t_len) / 2.0, t_len


def rank_reason(row):
    bits = []
    label = "speech takes" if row.get("klass") == "speech" else "b-roll moments"
    bits.append("#%d of %d %s" % (row.get("class_rank", 0),
                                  row.get("class_total", 0), label))
    text = (row.get("transcript") or "").strip()
    if text:
        snippet = text if len(text) <= 47 else text[:44] + "..."
        bits.append('"%s"' % snippet)
    return ", ".join(bits)


def cmd_rank(opts):
    root = Path(opts.root)
    selects_dir = (
        Path(opts.selects) if opts.selects else root / SELECTS_DIR_NAME
    )
    if opts.analyze:
        run_select_analyze(opts, selects_dir)
    cache_dir = selects_dir / "cache"
    videos = find_videos(opts.root, opts.exclude)
    if not videos:
        die("no videos found under %s" % opts.root)

    ranked_sources = []
    missing = []
    stale = []
    schema_odd = 0
    for p in videos:
        payload, status = selects_cache_payload(cache_dir, p)
        if status == "ok":
            ranked_sources.append((p, payload))
        elif status == "missing":
            missing.append(p)
        elif status == "schema":
            schema_odd += 1
        else:
            stale.append(p)

    if not ranked_sources:
        hint = (
            "rerun with --analyze"
            if not opts.analyze
            else "check the shutter-select output above"
        )
        die(
            "no usable shutter-select analysis under %s "
            "(%d videos found, %d unanalyzed, %d stale). "
            "Run:  shutter-select analyze %s   or %s"
            % (cache_dir, len(videos), len(missing), len(stale),
               opts.root, hint)
        )

    rows = []
    for p, payload in ranked_sources:
        rel = str(p.relative_to(root))
        for seg in payload.get("segments", []):
            row = dict(seg)
            row["rel"] = rel
            row["path"] = str(p)
            rows.append(row)
    if not rows:
        die("analysis found no segments to rank under %s" % opts.root)

    skipped = []
    usable = []
    for row in rows:
        reasons = social_skips(row, opts.min_len)
        if reasons:
            row["skip_reasons"] = reasons
            skipped.append(row)
        else:
            usable.append(row)
    if not usable:
        why = sorted({r for row in skipped for r in row["skip_reasons"]})
        die(
            "all %d segments are unpostable (%s). Lower --min-len?"
            % (len(rows), "; ".join(why))
        )
    social_score(usable, opts.min_len, opts.max_len)
    usable.sort(key=lambda r: -r["social_score"])
    class_totals = {}
    for row in usable:
        k = row.get("klass", "broll")
        class_totals[k] = class_totals.get(k, 0) + 1
        row["class_rank"] = class_totals[k]
    for row in usable:
        row["class_total"] = class_totals[row.get("klass", "broll")]

    # One window per ranked segment, target length by content type
    # unless overridden, the publish convention.
    for row in usable:
        m = Meta()
        m.path = Path(row["path"])
        m.rel = row["rel"]
        bucket = content_bucket(m)
        t_len = opts.clip_len or BUCKET_TARGET.get(bucket, 14.0)
        start, length = clip_window(row, t_len)
        row["bucket"] = bucket
        row["window_start"] = round(start, 2)
        row["window_len"] = round(length, 2)

    top_n = opts.top if opts.top > 0 else len(usable)
    picked = usable[:top_n]

    out_dir = out_root(opts) / "rank"
    out_dir.mkdir(parents=True, exist_ok=True)

    picks_path = out_dir / "picks.txt"
    lines = [
        "# shutter-clip rank: best moments first, shutter-select scored.",
        "# Feed to: shutter_clip.py cut %s %s" % (picks_path, opts.root),
        "",
    ]
    for n, row in enumerate(picked, 1):
        lines.append(
            "# %d: score %d, %s, %s"
            % (n, round(row["social_score"] * 100), row["bucket"],
               rank_reason(row))
        )
        end = row["window_start"] + row["window_len"]
        lines.append(
            "%s @ %s-%s" % (row["rel"], fmt_tc(row["window_start"]),
                            fmt_tc(end))
        )
    picks_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ranking_path = out_dir / "ranking.json"
    ranking_path.write_text(
        json.dumps(
            {
                "tool": "shutter-clip rank",
                "selects_schema": SELECTS_SCHEMA,
                "root": str(root),
                "weights": {
                    "speech": SOCIAL_SPEECH_WEIGHTS,
                    "broll": SOCIAL_BROLL_WEIGHTS,
                },
                "clips": usable,
                "skipped": skipped,
            },
            indent=1,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    report_lines = []
    show = min(len(picked), 20)
    print()
    print("%-4s %-5s %-7s %-5s %s" % ("rank", "score", "class", "len", "clip"))
    print("-" * 78)
    for n, row in enumerate(picked, 1):
        line = "%-4d %-5d %-7s %-5s %s @ %s  (%s)" % (
            n,
            round(row["social_score"] * 100),
            row.get("klass", "broll"),
            "%ds" % round(row["window_len"]),
            row["rel"],
            fmt_tc(row["window_start"]),
            rank_reason(row),
        )
        report_lines.append(line)
        if n <= show:
            print(line)
    if len(picked) > show:
        print("... %d more in %s" % (len(picked) - show, out_dir / "report.txt"))
    print()
    for row in skipped:
        report_lines.append(
            "skip %s @ %s  (%s)"
            % (row["rel"], fmt_tc(row["t_in"]), "; ".join(row["skip_reasons"]))
        )
    coverage = "ranked %d segments from %d of %d videos" % (
        len(usable), len(ranked_sources), len(videos)
    )
    extras = []
    if skipped:
        extras.append("%d unpostable" % len(skipped))
    if missing:
        extras.append("%d not analyzed" % len(missing))
    if stale:
        extras.append("%d stale" % len(stale))
    if schema_odd:
        extras.append("%d newer-schema" % schema_odd)
    if extras:
        coverage += " (" + ", ".join(extras) + ")"
    print(coverage)
    report_lines.append("")
    report_lines.append(coverage)
    for p in missing:
        report_lines.append("not analyzed: %s" % p.relative_to(root))
    for p in stale:
        report_lines.append("stale analysis: %s" % p.relative_to(root))
    (out_dir / "report.txt").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )
    if missing or stale:
        print(
            "hint: %d file(s) need (re)analysis, rerun with --analyze "
            "or run: shutter-select analyze %s" % (len(missing) + len(stale),
                                                   opts.root)
        )
    print("done. picks -> %s" % picks_path)


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
    metas = probe_all(videos, opts.root, open_cache(opts))
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


# ---------------------------------------------------------------- frames / curate


def h_pack(opts):
    return out_root(opts) / "post-ready" / PLATFORM_DIR["h"]


def twin_of(opts, h_path):
    """The vertical twin of a horizontal pack file, if it exists."""
    rel = h_path.relative_to(h_pack(opts))
    name = rel.name.replace(" - horizontal.mp4", " - vertical.mp4")
    return out_root(opts) / "post-ready" / PLATFORM_DIR["v"] / rel.parent / name


def cmd_frames(opts):
    pack = h_pack(opts)
    if not pack.is_dir():
        die("no horizontal pack yet, run publish first")
    review = out_root(opts) / ".review"
    review.mkdir(parents=True, exist_ok=True)
    mpath = review / "manifest.json"
    manifest = {}
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    known = set(manifest.values())
    next_id = max((int(k) for k in manifest), default=0) + 1
    made = 0
    for f in sorted(pack.rglob("*.mp4")):
        if f.name.startswith("._"):
            continue
        rel = str(f.relative_to(pack))
        if rel in known:
            continue
        fid = "%04d" % next_id
        try:
            run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-y", "-i", str(f),
                "-vf", "select=eq(n\\,15)+eq(n\\,%d)+eq(n\\,%d),"
                       "scale=420:-2,tile=3x1"
                       % (30 * 6, 30 * 12),
                "-frames:v", "1", str(review / (fid + ".jpg")),
            ])
        except RuntimeError as exc:
            print("warn: strip failed for %s (%s)" % (rel, exc),
                  file=sys.stderr)
            continue
        manifest[fid] = rel
        next_id += 1
        made += 1
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print("frames: %d new strips, %d total -> %s" % (made, len(manifest), review))


def cmd_curate(opts):
    """Apply verdicts: '0012 kill' / '0007 top 3' / unlisted = keep."""
    review = out_root(opts) / ".review"
    manifest = json.loads((review / "manifest.json").read_text(encoding="utf-8"))
    pack = h_pack(opts)
    bsides = out_root(opts) / "post-ready" / "b-sides"
    topdir = out_root(opts) / "post-ready" / "post first - top picks"
    killed = topped = 0
    for raw in Path(opts.verdicts).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        fid, verdict = parts[0], parts[1].lower()
        rel = manifest.get(fid)
        if rel is None:
            print("warn: unknown id %s" % fid, file=sys.stderr)
            continue
        h_file = pack / rel
        if verdict == "kill":
            for f in (h_file, twin_of(opts, h_file)):
                if f.exists():
                    dst = bsides / f.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(f, dst)
            killed += 1
        elif verdict == "top":
            rank = int(parts[2]) if len(parts) > 2 else 99
            if h_file.exists():
                topdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(h_file, topdir / ("%02d - %s" % (rank, h_file.name)))
                topped += 1
    print("curate: %d killed to b-sides, %d in post first" % (killed, topped))


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
    sp.add_argument("--no-hwaccel", action="store_true",
                    help="software decode only, use if playback is odd")
    sp.add_argument("--encode-workers", type=int, default=2,
                    help="parallel encodes on the hardware encoder (default 2)")
    sp.add_argument("-V", "--vertical", action="store_true",
                    help="also produce a 1080x1920 vertical center crop")
    sp.add_argument("--vertical-only", action="store_true",
                    help="produce only the vertical version")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shutter_clip.py",
        description="zero-edit social clips straight from a footage drive",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    sp.add_argument("--target-len", type=float, default=0.0,
                    help="clip length seconds, 0 = per type: drone 20, "
                         "camera 12, phone 8")
    sp.add_argument("--workers", type=int, default=3,
                    help="parallel analysis decodes (default 3)")
    sp.add_argument("--full-analysis", action="store_true",
                    help="decode every frame for scoring instead of "
                         "keyframes only (slower, marginally finer)")
    sp.add_argument("--min-len", type=float, default=6.0,
                    help="shortest usable clip seconds (default 6)")
    sp.add_argument("--scene", type=float, default=0.35,
                    help="hard-cut threshold, picks never straddle one")
    sp.add_argument("--max-picks", type=int, default=0,
                    help="picks per video, 0 = scale with duration (default)")
    sp.add_argument("--horizontal-only", action="store_true",
                    help="skip the vertical variants")
    sp.add_argument("--montage-len", type=float, default=30.0,
                    help="target seconds per folder montage, 0 disables")
    sp.add_argument("--montage-seg", type=float, default=5.0,
                    help="seconds per montage cut (default 5)")
    sp.add_argument("--no-loop-close", dest="loop_close",
                    action="store_false", default=True,
                    help="skip the 2.5s reprise of the opening shot")
    sp.add_argument("--title-cards", action="store_true",
                    help="1.2s trip title card at montage start")
    sp.add_argument("--force", action="store_true",
                    help="re-encode even if the output is fresh")

    sp = sub.add_parser(
        "rank",
        help="deep-rank moments using shutter-select analysis",
    )
    add_common(sp)
    sp.add_argument("--selects", default=None,
                    help="shutter-select output dir (default: ROOT/%s)"
                         % SELECTS_DIR_NAME)
    sp.add_argument("--analyze", action="store_true",
                    help="run 'shutter-select analyze' first (subprocess)")
    sp.add_argument("--select-bin", default="shutter-select",
                    help="shutter-select command to run with --analyze")
    sp.add_argument("--select-args", default="",
                    help="extra args for analyze, e.g. \"--model small\"")
    sp.add_argument("--top", type=int, default=20,
                    help="picks to keep in picks.txt, 0 = all (default 20)")
    sp.add_argument("--clip-len", type=float, default=0.0,
                    help="pick window seconds, 0 = per type: drone 20, "
                         "camera 12, phone 8")
    sp.add_argument("--min-len", type=float, default=6.0,
                    help="shortest postable seconds (default 6)")
    sp.add_argument("--max-len", type=float, default=18.0,
                    help="ideal longest seconds before decay (default 18)")

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

    sp = sub.add_parser("frames", help="dump review strips for every pick")
    add_common(sp)

    sp = sub.add_parser("curate", help="apply keep/kill/top verdicts")
    sp.add_argument("verdicts", help="verdict file: '0012 kill' or '0007 top 3'")
    add_common(sp)

    opts = ap.parse_args(argv)
    # ffmpeg is only required once a real subcommand is running. Checking it
    # before parse_args made --help and --version die on a machine without it,
    # which is exactly the machine where you want to read the help.
    which_or_die()
    opts.encoder_name = choose_encoder(opts.encoder)
    if opts.command in ("mirror", "cut") or (
        opts.command == "clips" and not opts.copy
    ):
        print("encoder: %s" % opts.encoder_name)

    {
        "scan": cmd_scan,
        "mirror": cmd_mirror,
        "publish": cmd_publish,
        "rank": cmd_rank,
        "clips": cmd_clips,
        "sheet": cmd_sheet,
        "cut": cmd_cut,
        "frames": cmd_frames,
        "curate": cmd_curate,
    }[opts.command](opts)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
