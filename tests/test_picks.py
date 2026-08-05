"""Motion scoring: binning, window ranking, pick counts, naming, buckets."""

from __future__ import annotations

from tests.conftest import make_meta, shutter_clip as sc


def flat_series(duration=60.0, step=0.5, motion=0.05, luma=120.0):
    times = [i * step for i in range(int(duration / step))]
    return times, [motion] * len(times), [luma] * len(times)


class TestBinSeries:
    def test_bins_average(self):
        frames = [(0.1, 0.2, 100.0), (0.3, 0.4, 110.0), (0.9, 0.6, 90.0)]
        times, motion, luma = sc.bin_series(frames, duration=2.0, step=0.5)
        assert times[0] == 0.0
        assert motion[0] == (0.2 + 0.4) / 2
        assert luma[0] == 105.0
        assert motion[1] == 0.6

    def test_empty_bins_skipped(self):
        frames = [(1.6, 0.5, 100.0)]
        times, motion, _ = sc.bin_series(frames, duration=2.0, step=0.5)
        assert times == [1.5]
        assert motion == [0.5]


class TestPickWindows:
    def test_edge_damping_pushes_picks_inward(self):
        meta = make_meta(duration=60.0)
        picks = sc.pick_windows(meta, flat_series(), target_len=8,
                                min_len=4, cut_thr=0.4, max_picks=1)
        assert len(picks) == 1
        start, length, _ = picks[0]
        edge = max(3.0, 60.0 * 0.08)
        assert start >= edge
        assert start + length <= 60.0 - edge

    def test_dark_windows_lose(self):
        meta = make_meta(duration=60.0)
        times, motion, luma = flat_series()
        # a dark stretch 10..30 s, bright elsewhere
        luma = [10.0 if 10 <= t < 30 else 120.0 for t in times]
        picks = sc.pick_windows(meta, (times, motion, luma), target_len=8,
                                min_len=4, cut_thr=0.4, max_picks=1)
        start, length, _ = picks[0]
        assert not (10 <= start < 30)

    def test_high_motion_wins(self):
        meta = make_meta(duration=60.0)
        times, motion, luma = flat_series()
        motion = [0.3 if 40 <= t < 48 else 0.05 for t in times]
        picks = sc.pick_windows(meta, (times, motion, luma), target_len=8,
                                min_len=4, cut_thr=0.9, max_picks=1)
        start, _, _ = picks[0]
        assert 38 <= start <= 42

    def test_picks_never_overlap(self):
        meta = make_meta(duration=120.0)
        picks = sc.pick_windows(meta, flat_series(duration=120.0),
                                target_len=10, min_len=4, cut_thr=0.4,
                                max_picks=4)
        picks = sorted(picks)
        for (s1, l1, _), (s2, _, _) in zip(picks, picks[1:]):
            assert s2 >= s1 + l1 + 2.0

    def test_start_spike_clamped(self):
        """Decoder start noise must not hand the first seconds the win."""
        meta = make_meta(duration=60.0)
        times, motion, luma = flat_series(motion=0.1)
        motion[0] = motion[1] = 50.0  # garbage-high scene scores
        picks = sc.pick_windows(meta, (times, motion, luma), target_len=8,
                                min_len=4, cut_thr=60.0, max_picks=1)
        start, _, _ = picks[0]
        assert start >= 3.0

    def test_windows_respect_cut_boundaries(self):
        meta = make_meta(duration=60.0)
        times, motion, luma = flat_series()
        cut_at = 30.0
        motion = [0.8 if t == cut_at else m for t, m in zip(times, motion)]
        picks = sc.pick_windows(meta, (times, motion, luma), target_len=12,
                                min_len=4, cut_thr=0.5, max_picks=4)
        assert picks
        for start, length, _ in picks:
            assert start + length <= cut_at + 0.51 or start >= cut_at

    def test_empty_series_no_picks(self):
        meta = make_meta(duration=60.0)
        assert sc.pick_windows(meta, ([], [], []), 8, 4, 0.4, 3) == []


class TestPickCounts:
    def test_ladder(self):
        assert sc.n_picks_for(74.9) == 1
        assert sc.n_picks_for(75) == 2
        assert sc.n_picks_for(179.9) == 2
        assert sc.n_picks_for(180) == 3
        assert sc.n_picks_for(359.9) == 3
        assert sc.n_picks_for(360) == 4


class TestNaming:
    def test_plain_english_name(self):
        meta = make_meta(rel="baja trip/DJI_0042.MP4")
        name = sc.nice_pick_name(meta, 2, 3, 75.0, 20.0, "h")
        assert name == "baja trip - pick 2 of 3 - 20s - DJI_0042 at 1m15s - horizontal.mp4"

    def test_vertical_label(self):
        meta = make_meta(rel="trip/IMG_0007.MOV")
        assert "vertical" in sc.nice_pick_name(meta, 1, 1, 5.0, 8.0, "v")

    def test_exfat_unsafe_chars_stripped(self):
        meta = make_meta(rel='we?ird:take/DJI_0001.MP4')
        name = sc.nice_pick_name(meta, 1, 1, 0.0, 5.0, "h")
        for ch in ':*?"<>|/\\':
            assert ch not in name


class TestContentBuckets:
    def _bucket(self, rel):
        return sc.content_bucket(make_meta(rel=rel))

    def test_filename_patterns(self):
        assert self._bucket("x/DJI_0001.MP4") == "drone aerials"
        assert self._bucket("x/DJI-0230.mp4") == "drone aerials"
        assert self._bucket("x/DSCF1001.MOV") == "camera footage"
        assert self._bucket("x/C0042.MP4") == "camera footage"
        assert self._bucket("x/IMG_1234.MOV") == "phone clips"

    def test_folder_fallbacks(self):
        assert self._bucket("Drone Footage/clip.mp4") == "drone aerials"
        assert self._bucket("iphone dump/clip.mp4") == "phone clips"
        assert self._bucket("misc/clip.mp4") == "camera footage"


class TestSegmentsAndSampling:
    def test_short_regions_dropped(self):
        assert sc.build_segments(2.0, [], min_len=3, max_len=5) == []

    def test_in_band_kept_whole(self):
        assert sc.build_segments(4.0, [], 3, 5) == [(0.0, 4.0)]

    def test_long_regions_split_evenly(self):
        segments = sc.build_segments(10.0, [], 3, 5)
        assert len(segments) == 2
        assert all(abs(length - 5.0) < 0.01 for _, length in segments)

    def test_edge_cuts_ignored(self):
        segments = sc.build_segments(10.0, [0.2, 9.9], 3, 5)
        assert len(segments) == 2

    def test_interior_cut_splits(self):
        # 0..4 fits the band whole; 4..10 exceeds max 5 and splits in two
        segments = sc.build_segments(10.0, [4.0], 3, 5)
        assert [round(s, 1) for s, _ in segments] == [0.0, 4.0, 7.0]

    def test_sample_evenly_caps_and_keeps_ends(self):
        items = list(range(10))
        sampled = sc.sample_evenly(items, 3)
        assert sampled[0] == 0 and sampled[-1] == 9
        assert len(sampled) == 3
        assert sc.sample_evenly(items, 20) == items
