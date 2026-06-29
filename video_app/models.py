"""Database models for the video app."""
from django.db import models


class Video(models.Model):
    """A movie uploaded by staff and streamed to users as HLS."""

    class Status(models.TextChoices):
        """Lifecycle of a video while it is being transcoded."""

        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Failed'

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100)
    video_file = models.FileField(upload_to='uploads/')
    thumbnail = models.FileField(upload_to='thumbnails/', blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Return the video title for admin and shell displays."""
        return self.title
