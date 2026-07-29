"""Background jobs for video post-processing (run by the RQ worker)."""

import shutil
from pathlib import Path

import django_rq
from django.conf import settings
from django.db import transaction
from rq import Callback

from .api.utils import (
    MEDIA_QUEUE_NAME,
    RESOLUTION_HEIGHTS,
    VIDEO_JOB_TIMEOUT,
    build_master_playlist,
    generate_thumbnail,
    hls_root,
    invalidate_video_list_cache,
    transcode_to_hls,
)
from .models import Video


def enqueue_processing(video_id):
    """Queue the HLS transcoding of a video on the media queue.

    Single entry point for signals and admin: transcoding must never land on
    'default', which carries the fast e-mail jobs. ``on_failure`` records FAILED
    when the job dies in a way ``process_video`` cannot catch itself (job
    timeout, unpickling error, worker exception outside the try block). A worker
    that is OOM-killed runs no callback at all — that case is covered by the
    ``sweep_stuck_videos`` management command.
    """
    queue = django_rq.get_queue(MEDIA_QUEUE_NAME)
    return queue.enqueue(
        process_video,
        video_id,
        job_timeout=VIDEO_JOB_TIMEOUT,
        on_failure=Callback(mark_video_failed),
    )


def mark_video_failed(job, connection, exc_type, exc_value, traceback):
    """RQ failure callback: flag the video of a dead job as FAILED.

    Uses a queryset update so a video deleted while its job was running does not
    raise DoesNotExist inside the callback. READY rows are left alone: a later
    successful run must not be overwritten by a late failure callback.
    """
    if not job.args:
        return
    Video.objects.filter(pk=job.args[0]).exclude(status=Video.Status.READY).update(
        status=Video.Status.FAILED
    )


def process_video(video_id):
    """Generate the thumbnail and every HLS rendition, then mark READY."""
    video = Video.objects.filter(pk=video_id).first()
    if video is None:
        # Deleted between enqueue and pickup. Drop any output that may already
        # exist instead of raising DoesNotExist out of the worker.
        shutil.rmtree(hls_root(video_id), ignore_errors=True)
        return
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
    """Persist a new processing status, tolerating a video deleted mid-transcode.

    A queryset UPDATE, not ``save(update_fields=...)``: saving a row that no
    longer exists raises DatabaseError *and* leaves the surrounding transaction
    marked for rollback, while an UPDATE simply affects zero rows. An UPDATE
    fires no post_save, so the cached list — which contains exactly the READY
    videos — is invalidated here instead.
    """
    if not Video.objects.filter(pk=video.pk).update(status=status):
        return
    video.status = status
    transaction.on_commit(invalidate_video_list_cache)
