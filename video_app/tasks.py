"""Background jobs for video post-processing (run by the RQ worker)."""

import shutil
from pathlib import Path

import django_rq
from django.conf import settings
from django.db import transaction
from rq import Callback

from .api.utils import (
    MEDIA_QUEUE_NAME,
    VIDEO_JOB_TIMEOUT,
    build_hls_pipeline_arguments,
    generate_thumbnail,
    hls_root,
    invalidate_video_list_cache,
    probe_video_frame_rate,
    run_ffmpeg,
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
    """Transcode the source into every offered resolution in a single pass.

    One ffmpeg invocation decodes the source once and fans it out into all
    three renditions (see build_hls_pipeline_arguments), instead of the old
    per-resolution loop that decoded the same source three times.
    """
    source = Path(video.video_file.path)
    root = hls_root(video.id)
    root.mkdir(parents=True, exist_ok=True)
    frame_rate = probe_video_frame_rate(source)
    run_ffmpeg(build_hls_pipeline_arguments(source, frame_rate, root))


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
