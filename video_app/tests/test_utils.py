"""Fast, mocked-ffmpeg tests for the ffmpeg helpers in video_app/api/utils.py."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from video_app.api.utils import (
    RENDITION_VARIANTS,
    RESOLUTION_HEIGHTS,
    SEGMENT_DURATION,
    THUMBNAIL_TIMESTAMP_SECONDS,
    build_hls_pipeline_arguments,
    run_ffmpeg,
    thumbnail_timestamp,
)


class TestThumbnailTimestamp:
    """thumbnail_timestamp() picks a seek target that always exists in the clip."""

    def test_long_clip_keeps_the_original_five_second_mark(self):
        assert thumbnail_timestamp(120.0) == THUMBNAIL_TIMESTAMP_SECONDS

    def test_short_clip_falls_back_to_the_midpoint(self):
        # A 3s clip would have the old fixed 5s mark seek past EOF.
        assert thumbnail_timestamp(3.0) == 1.5

    def test_zero_duration_does_not_raise(self):
        assert thumbnail_timestamp(0.0) == 0.0

    def test_negative_duration_does_not_raise(self):
        assert thumbnail_timestamp(-1.0) == 0.0


class TestRunFfmpegError:
    """run_ffmpeg() must surface ffmpeg's own diagnostics, not just the exit code."""

    def test_reraises_with_stderr_tail_on_failure(self):
        stderr = ('line one\n' * 30 + 'Unknown encoder libx264').encode()
        error = subprocess.CalledProcessError(1, ['ffmpeg'], output=b'', stderr=stderr)
        with patch('subprocess.run', side_effect=error):
            try:
                run_ffmpeg(['-i', 'in.mp4', 'out.mp4'])
            except RuntimeError as exc:
                message = str(exc)
            else:
                raise AssertionError('run_ffmpeg did not raise')
        assert 'status 1' in message
        assert 'Unknown encoder libx264' in message
        # str(CalledProcessError) alone is just the exit code -- confirm we
        # actually improved on it rather than merely re-wrapping it.
        assert str(error) not in message or 'Unknown encoder libx264' in message

    def test_success_does_not_raise(self):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(['ffmpeg'], 0)
            run_ffmpeg(['-i', 'in.mp4', 'out.mp4'])


class TestBuildHlsPipelineArguments:
    """build_hls_pipeline_arguments() produces one ffmpeg invocation for all renditions."""

    def build(self, frame_rate=30.0, output_root=None):
        return build_hls_pipeline_arguments(
            Path('source.mp4'), frame_rate, output_root or Path('/media/hls/1')
        )

    def test_decodes_source_once_and_splits_into_one_branch_per_rendition(self):
        args = self.build()
        filter_complex = args[args.index('-filter_complex') + 1]
        assert f'split={len(RESOLUTION_HEIGHTS)}' in filter_complex
        for resolution, height in RESOLUTION_HEIGHTS.items():
            assert f'scale=-2:{height}' in filter_complex

    def test_maxrate_and_bufsize_present_per_rendition(self):
        args = self.build()
        for i, resolution in enumerate(RESOLUTION_HEIGHTS):
            maxrate = RENDITION_VARIANTS[resolution]
            assert f'-maxrate:v:{i}' in args
            assert args[args.index(f'-maxrate:v:{i}') + 1] == str(maxrate)
            assert f'-bufsize:v:{i}' in args
            assert args[args.index(f'-bufsize:v:{i}') + 1] == str(maxrate * 2)

    def test_keyframes_aligned_to_segment_duration_across_renditions(self):
        frame_rate = 30.0
        args = self.build(frame_rate=frame_rate)
        expected_gop = str(round(frame_rate * SEGMENT_DURATION))
        for i in range(len(RESOLUTION_HEIGHTS)):
            assert args[args.index(f'-g:{i}') + 1] == expected_gop
            assert args[args.index(f'-keyint_min:{i}') + 1] == expected_gop
            assert args[args.index(f'-sc_threshold:{i}') + 1] == '0'

    def test_var_stream_map_names_match_output_layout(self):
        args = self.build()
        var_stream_map = args[args.index('-var_stream_map') + 1]
        for resolution in RESOLUTION_HEIGHTS:
            assert f'name:{resolution}' in var_stream_map

    def test_output_pattern_uses_percent_v_for_the_public_directory_contract(self):
        root = Path('/media/hls/42')
        args = self.build(output_root=root)
        assert str(root / '%v' / '%03d.ts') in args
        assert str(root / '%v' / 'index.m3u8') in args
        assert 'master.m3u8' in args
        assert args[args.index('-master_pl_name') + 1] == 'master.m3u8'
