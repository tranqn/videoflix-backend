"""URL routes for the video endpoints."""
from django.urls import path

from .views import HLSManifestView, HLSSegmentView, VideoListView

# The manifest route is listed before the segment route so that 'index.m3u8'
# is never captured by the generic <segment> pattern.
urlpatterns = [
    path('video/', VideoListView.as_view(), name='video-list'),
    path(
        'video/<int:movie_id>/<str:resolution>/index.m3u8',
        HLSManifestView.as_view(),
        name='video-manifest',
    ),
    path(
        'video/<int:movie_id>/<str:resolution>/<str:segment>',
        HLSSegmentView.as_view(),
        name='video-segment',
    ),
]
