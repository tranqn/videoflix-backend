"""Background jobs for video post-processing (run by the RQ worker)."""

from pathlib import Path

from django.conf import settings

from .api.utils import (
    RESOLUTION_HEIGHTS,
    build_master_playlist,
    generate_thumbnail,
    hls_root,
    transcode_to_hls,
)
from .models import Video


def process_video(video_id):
    """Generate the thumbnail and every HLS rendition, then mark READY."""
    video = Video.objects.get(pk=video_id)
    set_status(video, Video.Status.PROCESSING)
    try:
        build_thumbnail(video)
        build_renditions(video)
        build_master_playlist(video.id)
    except Exception:
        set_status(video, Video.Status.FAILED)
        raise
    set_status(video, Video.Status.READY)


def build_thumbnail(video):
    """Create the thumbnail file and attach it to the video."""
    source = Path(video.video_file.path)
    destination = Path(settings.MEDIA_ROOT) / 'thumbnails' / f'{video.id}.jpg'
    generate_thumbnail(source, destination)
    video.thumbnail.name = f'thumbnails/{video.id}.jpg'
    video.save(update_fields=['thumbnail'])


def build_renditions(video):
    """Transcode the source video into every offered resolution."""
    source = Path(video.video_file.path)
    for resolution in RESOLUTION_HEIGHTS:
        transcode_to_hls(source, resolution, hls_root(video.id) / resolution)


def set_status(video, status):
    """Persist a new processing status on the video."""
    video.status = status
    video.save(update_fields=['status'])
