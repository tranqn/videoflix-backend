"""Admin configuration for the video app — videos are uploaded here."""

from django.contrib import admin, messages

from .models import Video
from .tasks import enqueue_processing


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
            enqueue_processing(video.id)
        self.message_user(request, f'{queryset.count()} video(s) queued for reprocessing.')

    def has_delete_permission(self, request, obj=None):
        """Refuse deleting a video while its transcoding job is still running.

        post_delete rmtree()s the HLS directory the worker is writing into, so a
        delete during PROCESSING races ffmpeg and leaves orphaned files behind.
        Denying the permission (rather than raising) is what keeps the admin free
        of 500s: the change view hides the delete button and answers 403, and the
        bulk 'delete selected' action reports the missing permission on its
        confirmation page instead of blowing up.
        """
        if obj is not None and obj.status == Video.Status.PROCESSING:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        """Delete in bulk, never touching a video that is still PROCESSING."""
        blocked = queryset.filter(status=Video.Status.PROCESSING).count()
        if blocked:
            self.message_user(
                request,
                f'{blocked} video(s) skipped: still processing, delete them once done.',
                level=messages.WARNING,
            )
        queryset.exclude(status=Video.Status.PROCESSING).delete()
