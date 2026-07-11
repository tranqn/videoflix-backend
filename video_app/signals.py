"""Signal handlers: trigger HLS processing and clean up media files."""
import shutil

import django_rq
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .api.utils import VIDEO_LIST_CACHE_KEY, hls_root
from .models import Video
from .tasks import process_video


@receiver(post_save, sender=Video)
def enqueue_video_processing(sender, instance, created, **kwargs):
    """Invalidate the cache and process newly uploaded videos after commit."""
    cache.delete(VIDEO_LIST_CACHE_KEY)
    if created:
        transaction.on_commit(
            # 24h ceiling: long HLS batches can run overnight on modest hardware.
            # Large finite, not -1/infinite — the single worker must self-heal if
            # a job ever hangs.
            lambda: django_rq.enqueue(process_video, instance.id, job_timeout=86400)
        )


@receiver(post_delete, sender=Video)
def cleanup_video_files(sender, instance, **kwargs):
    """Remove the source file, thumbnail and HLS output of a deleted video."""
    cache.delete(VIDEO_LIST_CACHE_KEY)
    instance.video_file.delete(save=False)
    instance.thumbnail.delete(save=False)
    shutil.rmtree(hls_root(instance.id), ignore_errors=True)
