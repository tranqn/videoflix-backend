"""FFmpeg helpers and shared constants for the video app."""

import hashlib
import os
import subprocess
import uuid
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

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

# Cache for the video dashboard list. The payload is the same for every *user*,
# but NOT for every *origin*: the serializer embeds absolute thumbnail URLs built
# from the requesting Host (the frontend runs on another origin than the API, so
# a relative URL would resolve against the frontend and 404). A single global key
# would freeze whichever Host happened to miss the cache into the payload every
# other caller gets for the whole TTL, so the key is scoped per origin
# (scheme + host). Django validates Host against ALLOWED_HOSTS before we ever see
# it, so the key space stays bounded and cannot be inflated by a forged header.
#
# Per-origin keys cannot be dropped with a single cache.delete(), and the Django
# cache API has no key scan (LocMemCache least of all), so every key carries a
# generation token: replacing the token makes every origin's entry unreachable at
# once, and the orphans expire on their own TTL. The token is a random UUID, not
# a counter — if it is ever lost (Redis eviction/flush/restart) the replacement
# is a value that has never been used, so no previously cached payload can be
# resurrected.
VIDEO_LIST_CACHE_KEY = 'video_list'
VIDEO_LIST_CACHE_GENERATION_KEY = 'video_list_generation'
VIDEO_LIST_CACHE_TTL = 300

# Queue names. Transcoding is minutes-to-hours of ffmpeg; activation and
# password-reset e-mails must not queue behind it, so they stay on 'default'
# and media jobs get their own queue with its own worker.
MEDIA_QUEUE_NAME = 'media'
# 24h ceiling: long HLS batches can run overnight on modest hardware. Large
# finite, not -1/infinite — a hung job must eventually fail and be swept.
VIDEO_JOB_TIMEOUT = 86400
# A video that has been PROCESSING for longer than this is assumed dead (worker
# OOM-killed or container restarted mid-transcode); see sweep_stuck_videos.
STUCK_PROCESSING_HOURS = 24


def video_list_cache_generation():
    """Return the current video list cache generation, seeding one if unset."""
    generation = cache.get(VIDEO_LIST_CACHE_GENERATION_KEY)
    if generation is None:
        generation = uuid.uuid4().hex
        cache.set(VIDEO_LIST_CACHE_GENERATION_KEY, generation, None)
    return generation


def video_list_cache_key(request):
    """Return the video list cache key for the origin (scheme + host) of ``request``."""
    origin = f'{request.scheme}://{request.get_host()}'
    digest = hashlib.sha256(origin.encode()).hexdigest()[:32]
    return f'{VIDEO_LIST_CACHE_KEY}:{video_list_cache_generation()}:{digest}'


def invalidate_video_list_cache():
    """Drop every origin's cached video list by rotating the generation token."""
    cache.set(VIDEO_LIST_CACHE_GENERATION_KEY, uuid.uuid4().hex, None)


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
