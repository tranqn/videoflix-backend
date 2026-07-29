"""FFmpeg helpers and shared constants for the video app."""

import os
import subprocess
from pathlib import Path

from django.conf import settings

# Offered resolutions mapped to their target height (width is auto, kept even).
RESOLUTION_HEIGHTS = {'480p': 480, '720p': 720, '1080p': 1080}
# Average bitrate (bit/s) + 16:9 frame size advertised per rendition in the
# master playlist, so an HLS player can pick the variant that fits its bandwidth.
RENDITION_VARIANTS = {
    '480p': (1_400_000, '854x480'),
    '720p': (2_800_000, '1280x720'),
    '1080p': (5_000_000, '1920x1080'),
}
SEGMENT_DURATION = 6
THUMBNAIL_TIMESTAMP = '00:00:05'
# On small hosts set FFMPEG_PRESET=ultrafast in .env to halve transcode
# time at the cost of slightly larger segment files.
FFMPEG_PRESET = os.environ.get('FFMPEG_PRESET', 'veryfast')

# Cache key for the shared (same for every user) video dashboard list.
VIDEO_LIST_CACHE_KEY = 'video_list'
VIDEO_LIST_CACHE_TTL = 300


def hls_root(video_id):
    """Return the base directory holding a video's HLS renditions."""
    return Path(settings.MEDIA_ROOT) / 'hls' / str(video_id)


def run_ffmpeg(arguments):
    """Run ffmpeg with the given arguments, raising on a non-zero exit."""
    subprocess.run(['ffmpeg', '-y', *arguments], check=True, capture_output=True)


def generate_thumbnail(source_path, destination_path):
    """Grab a single still frame to use as the video thumbnail."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            '-ss',
            THUMBNAIL_TIMESTAMP,
            '-i',
            str(source_path),
            '-frames:v',
            '1',
            '-q:v',
            '2',
            str(destination_path),
        ]
    )


def transcode_to_hls(source_path, resolution, output_dir):
    """Transcode the source into an HLS rendition at the given resolution."""
    output_dir.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(build_hls_arguments(source_path, RESOLUTION_HEIGHTS[resolution], output_dir))


def build_master_playlist(video_id):
    """Write the master playlist that references every HLS rendition."""
    lines = ['#EXTM3U', '#EXT-X-VERSION:3']
    for resolution in RESOLUTION_HEIGHTS:
        bandwidth, frame_size = RENDITION_VARIANTS[resolution]
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={frame_size}')
        lines.append(f'{resolution}/index.m3u8')
    master_path = hls_root(video_id) / 'master.m3u8'
    master_path.parent.mkdir(parents=True, exist_ok=True)
    master_path.write_text('\n'.join(lines) + '\n')


def build_hls_arguments(source_path, height, output_dir):
    """Build the ffmpeg argument list for one HLS rendition."""
    return [
        '-i',
        str(source_path),
        '-vf',
        f'scale=-2:{height}',
        '-c:v',
        'libx264',
        '-profile:v',
        'main',
        '-crf',
        '20',
        '-preset',
        FFMPEG_PRESET,
        '-c:a',
        'aac',
        '-ar',
        '48000',
        '-hls_time',
        str(SEGMENT_DURATION),
        '-hls_playlist_type',
        'vod',
        '-hls_segment_filename',
        str(output_dir / '%03d.ts'),
        str(output_dir / 'index.m3u8'),
    ]
