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
# VBV cap (bit/s) enforced per rendition via -maxrate/-bufsize, so the number
# hls.js is told to expect (baked into the ffmpeg-generated master playlist)
# is a hard ceiling the encode cannot exceed, not just whatever -crf produces
# for a given source.
RENDITION_VARIANTS = {
    '480p': 1_400_000,
    '720p': 2_800_000,
    '1080p': 5_000_000,
}
SEGMENT_DURATION = 6
# Preferred thumbnail timestamp (seconds). Clips shorter than this fall back
# to their midpoint -- see thumbnail_timestamp().
THUMBNAIL_TIMESTAMP_SECONDS = 5.0
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
    """Run ffmpeg with the given arguments, raising with its stderr tail on failure.

    ``CalledProcessError.__str__`` is just the exit code -- exactly what RQ's
    failure callback and dashboard show -- while the actual reason (bad filter
    graph, unsupported codec, a seek past EOF) sits unread on ``exc.stderr``.
    Re-raise with the last lines of stderr folded into the message so a failed
    transcode is diagnosable without reproducing it locally.
    """
    try:
        subprocess.run(['ffmpeg', '-y', *arguments], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b'').decode(errors='replace').strip()
        tail = '\n'.join(stderr.splitlines()[-20:])
        raise RuntimeError(f'ffmpeg exited with status {exc.returncode}:\n{tail}') from exc


def probe_video_duration(source_path):
    """Return the source video's duration in seconds via ffprobe."""
    output = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-show_entries',
            'format=duration',
            '-of',
            'default=noprint_wrappers=1:nokey=1',
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(output)


def probe_video_frame_rate(source_path):
    """Return the source video's frame rate (frames/second) via ffprobe."""
    output = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-show_entries',
            'stream=r_frame_rate',
            '-of',
            'default=noprint_wrappers=1:nokey=1',
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    numerator, _, denominator = output.partition('/')
    return float(numerator) / float(denominator) if denominator else float(numerator)


def thumbnail_timestamp(duration):
    """Pick an in-bounds seek target (seconds) for the still-frame thumbnail.

    Historically fixed at THUMBNAIL_TIMESTAMP_SECONDS; a clip shorter than
    that seeks past EOF, so ffmpeg writes no frame and exits non-zero, failing
    the whole processing job even though the transcode itself would have
    succeeded. Keep the original mark for longer videos, but fall back to the
    midpoint of short ones so a valid frame always exists to grab.
    """
    if duration <= 0:
        return 0.0
    return min(THUMBNAIL_TIMESTAMP_SECONDS, duration / 2)


def generate_thumbnail(source_path, destination_path):
    """Grab a single still frame to use as the video thumbnail."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_video_duration(source_path)
    timestamp = thumbnail_timestamp(duration)
    run_ffmpeg(
        [
            '-ss',
            f'{timestamp:.3f}',
            '-i',
            str(source_path),
            '-frames:v',
            '1',
            '-q:v',
            '2',
            str(destination_path),
        ]
    )


def build_hls_pipeline_arguments(source_path, frame_rate, output_root):
    """Build the ffmpeg arguments for one single-pass, multi-rendition HLS encode.

    Decodes ``source_path`` once and fans it out with a ``split`` filter into
    one scaled libx264 encode per rendition, instead of the old approach of
    invoking ffmpeg once per resolution (three decodes of the same source).
    ``-var_stream_map`` also drives ffmpeg's own HLS muxer into writing the
    master playlist (``master_pl_name``), so a hand-rolled one is no longer
    needed -- and its BANDWIDTH/RESOLUTION values reflect what was actually
    encoded, rather than a static estimate.

    Every rendition shares a GOP of exactly SEGMENT_DURATION seconds
    (``-g``/``-keyint_min`` derived from ``frame_rate``) with scene-cut
    detection disabled (``-sc_threshold 0``), so segment N starts on the same
    source frame in every rendition -- required for a clean quality switch
    mid-playback. ``-maxrate``/``-bufsize`` per rendition cap the encode at
    the bitrate advertised in RENDITION_VARIANTS, since without a VBV cap
    ``-crf`` alone can produce a stream well past what the player was told to
    expect.
    """
    resolutions = list(RESOLUTION_HEIGHTS)
    frames_per_gop = max(1, round(frame_rate * SEGMENT_DURATION))

    split_labels = ''.join(f'[v{i}]' for i in range(len(resolutions)))
    filter_stages = [f'[0:v]split={len(resolutions)}{split_labels}']
    for i, resolution in enumerate(resolutions):
        height = RESOLUTION_HEIGHTS[resolution]
        filter_stages.append(f'[v{i}]scale=-2:{height},format=yuv420p[scaled{i}]')
    filter_complex = '; '.join(filter_stages)

    arguments = ['-i', str(source_path), '-filter_complex', filter_complex]
    for i, resolution in enumerate(resolutions):
        maxrate = RENDITION_VARIANTS[resolution]
        arguments += [
            '-map',
            f'[scaled{i}]',
            f'-c:v:{i}',
            'libx264',
            f'-profile:v:{i}',
            'main',
            f'-crf:v:{i}',
            '20',
            f'-preset:{i}',
            FFMPEG_PRESET,
            f'-maxrate:v:{i}',
            str(maxrate),
            f'-bufsize:v:{i}',
            str(maxrate * 2),
            f'-g:{i}',
            str(frames_per_gop),
            f'-keyint_min:{i}',
            str(frames_per_gop),
            f'-sc_threshold:{i}',
            '0',
        ]
    for _ in resolutions:
        arguments += ['-map', 'a:0']
    arguments += ['-c:a', 'aac', '-ar', '48000']

    var_stream_map = ' '.join(
        f'v:{i},a:{i},name:{resolution}' for i, resolution in enumerate(resolutions)
    )
    arguments += [
        '-var_stream_map',
        var_stream_map,
        '-hls_time',
        str(SEGMENT_DURATION),
        '-hls_playlist_type',
        'vod',
        '-master_pl_name',
        'master.m3u8',
        '-hls_segment_filename',
        str(output_root / '%v' / '%03d.ts'),
        str(output_root / '%v' / 'index.m3u8'),
    ]
    return arguments
