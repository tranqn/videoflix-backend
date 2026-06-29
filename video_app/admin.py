"""Admin configuration for the video app — videos are uploaded here."""
from django.contrib import admin

from .models import Video


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    """Upload videos and follow their processing status."""

    list_display = ('id', 'title', 'category', 'status', 'created_at')
    list_filter = ('status', 'category')
    search_fields = ('title', 'description')
    readonly_fields = ('status', 'thumbnail', 'created_at')
