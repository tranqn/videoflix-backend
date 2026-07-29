"""Signal handlers: trigger HLS processing and clean up media files."""

import shutil

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .api.utils import hls_root, invalidate_video_list_cache
from .models import Video
from .tasks import enqueue_processing


@receiver(post_save, sender=Video)
def enqueue_video_processing(sender, instance, created, **kwargs):
    """Invalidate the cache and process newly uploaded videos after commit.

    Both actions run on commit, never inside the transaction: invalidating early
    lets a concurrent request repopulate the cache from pre-commit state, and
    that stale payload would then survive for the full TTL.
    """
    transaction.on_commit(invalidate_video_list_cache)
    if created:
        transaction.on_commit(lambda: enqueue_processing(instance.id))


@receiver(post_delete, sender=Video)
def cleanup_video_files(sender, instance, **kwargs):
    """Remove the source file, thumbnail and HLS output of a deleted video.

    Deleting a video that is still PROCESSING would race the ffmpeg worker
    writing into exactly this directory; the admin refuses that delete (see
    VideoAdmin.has_delete_permission), so by the time this runs no job is
    supposed to own the directory any more.
    """
    transaction.on_commit(invalidate_video_list_cache)
    instance.video_file.delete(save=False)
    instance.thumbnail.delete(save=False)
    shutil.rmtree(hls_root(instance.id), ignore_errors=True)
