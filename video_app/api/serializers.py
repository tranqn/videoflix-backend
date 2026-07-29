"""Serializers for the video endpoints."""

from rest_framework import serializers

from video_app.models import Video


class VideoListSerializer(serializers.ModelSerializer):
    """Serialize video metadata for the dashboard list."""

    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = [
            'id',
            'created_at',
            'title',
            'description',
            'thumbnail_url',
            'category',
        ]

    def get_thumbnail_url(self, obj):
        """Return the absolute thumbnail URL used as the frontend <img> src."""
        if not obj.thumbnail:
            return None
        request = self.context.get('request')
        if request is None:
            return obj.thumbnail.url
        return request.build_absolute_uri(obj.thumbnail.url)
