# shutter-clip

Zero-edit social clips straight from a footage drive. Point it at the SSD,
get phone-ready files you can AirDrop and post as-is. Nothing is uploaded
anywhere and source files are never touched.

Part of the shutter-* family (shutter-cull, shutter-mcp). Phase 0 of the
social clip engine.

## Requirements

- ffmpeg and ffprobe on PATH. macOS: `brew install ffmpeg`
- python3 (stdlib only, no pip installs)

## Quick start

```zsh
python3 shutter_clip.py scan "/Volumes/Crucial X10"
```

```zsh
python3 shutter_clip.py mirror "/Volumes/Crucial X10"
```

Outputs land in `_phone-ready/` on the drive, mirroring the folder layout.
Folders whose name contains "do not include" or "do not use" are always
skipped, plus anything passed via `--exclude`.

## Subcommands

| command | what it does |
| --- | --- |
| `scan` | inventory: codec, resolution, fps, duration, HDR / 10-bit / flat-profile / no-audio flags |
| `mirror` | 1080p phone-ready copy of every clip. Incremental: re-runs only convert new files |
| `clips` | auto-cut every video into 6-18 s pieces at scene changes, interval fallback for continuous shots |
| `sheet` | self-contained HTML contact sheet. Click frames to build a picks list |
| `cut` | export the exact moments listed in a picks file |

## Output format

- Horizontal 1920x1080 is the default. `--vertical` adds a 1080x1920
  center crop, `--vertical-only` replaces.
- HEVC with the `hvc1` tag: half the size of H.264, native on iPhone.
  Encoder is picked automatically: VideoToolbox hardware on macOS
  (about 5-10x realtime), libx265/libx264 elsewhere. Override with
  `--encoder x264` if any player complains.
- HLG/PQ HDR sources are tone-mapped to SDR bt709 automatically.
- Log footage: pass `--lut yourlook.cube` to bake in a look.

## Picking exact moments

```zsh
python3 shutter_clip.py sheet "/Volumes/Crucial X10"
```

Open `_phone-ready/contact-sheet.html`, click the frames you like, hit
Copy picks, save as `picks.txt`, then:

```zsh
python3 shutter_clip.py cut picks.txt "/Volumes/Crucial X10"
```

Pick lines look like `folder/clip.MOV @ 1:42` (12 s from there),
`... @ 1:42-1:55` for an exact range, trailing ` v` for vertical.

## Notes

- `clips --copy` stream-copies the original 4K with zero quality loss and
  near-zero time, but cuts snap to keyframes, so starts can be off by a
  second or two. Default mode re-encodes and is frame-accurate.
- `clips` decodes every file for scene detection. On a big drive expect
  minutes on Apple Silicon, much longer without hardware decode.
- `scan` samples one frame per file for the flat-profile guess.
  `--fast` skips that.

## License

MIT
