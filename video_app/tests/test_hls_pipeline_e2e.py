"""Real, non-mocked end-to-end tests for the single-pass HLS pipeline.

Every other test in this app mocks ``run_ffmpeg``/``subprocess.run`` and
therefore cannot catch a changed output layout, a broken filter graph, or a
malformed master playlist. These tests actually shell out to ffmpeg/ffprobe
against synthetic sources generated with ffmpeg itself (no binary fixtures
committed), and are marked ``ffmpeg`` so they can be deselected in
environments without those binaries on PATH, e.g. ``pytest -m "not ffmpeg"``.
"""

import re
import shutil
import subprocess

import pytest

from video_app.api.utils import (
    RENDITION_VARIANTS,
    RESOLUTION_HEIGHTS,
    build_hls_pipeline_arguments,
    generate_thumbnail,
    probe_video_frame_rate,
    run_ffmpeg,
)

FFMPEG_AVAILABLE = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not FFMPEG_AVAILABLE, reason='ffmpeg/ffprobe not found on PATH'),
]

SEGMENT_PATTERN = re.compile(r'^\d+\.ts$')
# VBV allows short-term bursts above -maxrate; a generous but still
# meaningful ceiling for a source deliberately chosen to be too busy for
# -crf 20 to stay anywhere near the cap on its own (see make_synthetic_source).
MAXRATE_TOLERANCE = 1.4


def make_synthetic_source(path, duration, fps=30, size='640x360'):
    """Generate a short, high-motion synthetic clip using ffmpeg's own sources.

    Uses ``life`` (a cellular automaton), not the flat ``testsrc``, because it
    is hard to compress: an unconstrained -crf 20 encode of it blows well past
    every rendition's declared maxrate, which is what makes the VBV-cap
    assertion below meaningful rather than incidental.
    """
    subprocess.run(
        [
            'ffmpeg',
            '-y',
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            f'life=size={size}:rate={fps}:ratio=0.4:mold=10',
            '-f',
            'lavfi',
            '-i',
            f'sine=frequency=440:duration={duration}',
            '-t',
            str(duration),
            '-c:v',
            'libx264',
            '-preset',
            'ultrafast',
            '-crf',
            '0',
            '-pix_fmt',
            'yuv420p',
            '-c:a',
            'aac',
            '-shortest',
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def playlist_total_duration(playlist_path):
    """Sum the #EXTINF durations in an HLS media playlist."""
    return sum(
        float(match.group(1))
        for match in re.finditer(r'#EXTINF:([\d.]+)', playlist_path.read_text())
    )


def segment_frame_counts(ts_path):
    """Return (total frame count, keyframe count) for a single .ts segment."""
    output = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-show_entries',
            'frame=pict_type',
            '-of',
            'csv=p=0',
            str(ts_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    # ffprobe's csv writer trails a stray comma on some rows (I-frames, at
    # least on ffmpeg 8.1) even with a single field selected -- strip it too.
    return len(output), sum(1 for pict_type in output if pict_type.strip().rstrip(',') == 'I')


def run_pipeline(tmp_path, duration):
    """Generate a synthetic source of the given duration and transcode it for real."""
    source = tmp_path / 'source.mp4'
    make_synthetic_source(source, duration)
    output_root = tmp_path / 'hls'
    output_root.mkdir()
    frame_rate = probe_video_frame_rate(source)
    run_ffmpeg(build_hls_pipeline_arguments(source, frame_rate, output_root))
    return source, output_root


class TestSinglePassHlsPipelineLayout:
    """The on-disk layout is a public contract shared by views.py, the frontend, and Caddy."""

    def test_directory_layout_and_segment_names_match_the_public_contract(self, tmp_path):
        _, output_root = run_pipeline(tmp_path, duration=13)

        assert (output_root / 'master.m3u8').is_file()
        for resolution in RESOLUTION_HEIGHTS:
            rendition_dir = output_root / resolution
            assert (rendition_dir / 'index.m3u8').is_file()
            segments = sorted(p.name for p in rendition_dir.glob('*.ts'))
            assert segments, f'{resolution} produced no segments'
            for name in segments:
                assert SEGMENT_PATTERN.match(name), f'{resolution}/{name} violates ^\\d+\\.ts$'

    def test_master_playlist_advertises_bandwidth_and_resolution_per_variant(self, tmp_path):
        _, output_root = run_pipeline(tmp_path, duration=13)
        content = (output_root / 'master.m3u8').read_text()
        stream_infs = [
            line for line in content.splitlines() if line.startswith('#EXT-X-STREAM-INF')
        ]
        assert len(stream_infs) == len(RESOLUTION_HEIGHTS)
        for line in stream_infs:
            assert 'BANDWIDTH=' in line
            assert 'RESOLUTION=' in line


class TestSinglePassHlsPipelineEncoding:
    """The single decode must still yield capped, cross-rendition-aligned output."""

    def test_maxrate_caps_the_encoded_bitrate(self, tmp_path):
        _, output_root = run_pipeline(tmp_path, duration=13)
        for resolution, maxrate in RENDITION_VARIANTS.items():
            rendition_dir = output_root / resolution
            total_bytes = sum(f.stat().st_size for f in rendition_dir.glob('*.ts'))
            total_duration = playlist_total_duration(rendition_dir / 'index.m3u8')
            measured_bitrate = total_bytes * 8 / total_duration
            assert measured_bitrate <= maxrate * MAXRATE_TOLERANCE, (
                f'{resolution}: measured {measured_bitrate:.0f} bit/s exceeds '
                f'{MAXRATE_TOLERANCE}x its {maxrate} bit/s -maxrate cap'
            )

    def test_keyframe_positions_agree_across_renditions(self, tmp_path):
        _, output_root = run_pipeline(tmp_path, duration=13)
        resolutions = list(RESOLUTION_HEIGHTS)
        segment_names = sorted(p.name for p in (output_root / resolutions[0]).glob('*.ts'))
        # The last segment can be a short, partial GOP; only full segments
        # are guaranteed to line up frame-for-frame across renditions.
        for name in segment_names[:-1]:
            counts = {
                resolution: segment_frame_counts(output_root / resolution / name)
                for resolution in resolutions
            }
            values = list(counts.values())
            assert all(value == values[0] for value in values), (
                f'{name}: keyframe/frame counts diverge across renditions: {counts}'
            )
            frame_count, keyframe_count = values[0]
            assert keyframe_count == 1, f'{name}: expected exactly one keyframe, got {counts}'


class TestShortClipThumbnail:
    """A clip shorter than the old fixed 5s mark used to fail the whole job."""

    def test_thumbnail_succeeds_for_a_clip_shorter_than_five_seconds(self, tmp_path):
        source = tmp_path / 'short.mp4'
        make_synthetic_source(source, duration=3)
        destination = tmp_path / 'thumbnail.jpg'

        generate_thumbnail(source, destination)

        assert destination.is_file()
        assert destination.stat().st_size > 0

    def test_full_pipeline_succeeds_for_a_clip_shorter_than_five_seconds(self, tmp_path):
        source, output_root = run_pipeline(tmp_path, duration=3)
        thumbnail = tmp_path / 'thumbnail.jpg'

        generate_thumbnail(source, thumbnail)

        assert thumbnail.is_file()
        assert (output_root / 'master.m3u8').is_file()
        for resolution in RESOLUTION_HEIGHTS:
            segments = list((output_root / resolution).glob('*.ts'))
            assert segments, f'{resolution} produced no segments for a sub-5s clip'
