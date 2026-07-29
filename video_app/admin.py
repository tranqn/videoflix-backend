"""Admin configuration for the video app — videos are uploaded here."""

import django_rq
from django.contrib import admin

from .models import Video
from .tasks import process_video


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    """Upload videos and follow their processing status."""

    list_display = ('id', 'title', 'category', 'status', 'created_at')
    list_filter = ('status', 'category')
    search_fields = ('title', 'description')
    readonly_fields = ('status', 'thumbnail', 'created_at')
    actions = ('reprocess_videos',)

    @admin.action(description='Reprocess selected videos (re-run HLS transcoding)')
    def reprocess_videos(self, request, queryset):
        """Re-enqueue the transcoding job for each selected video (e.g. FAILED)."""
        for video in queryset:
            django_rq.enqueue(process_video, video.id, job_timeout=86400)
        self.message_user(request, f'{queryset.count()} video(s) queued for reprocessing.')
