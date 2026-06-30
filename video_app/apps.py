"""App configuration for the video app."""
from django.apps import AppConfig


class VideoAppConfig(AppConfig):
    """Configuration for the ``video_app`` application."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'video_app'

    def ready(self):
        """Connect the model signals once the app registry is ready."""
        from . import signals  # noqa: F401
