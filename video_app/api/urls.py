"""URL routes for the video endpoints."""
from django.urls import path

from .views import HLSManifestView, HLSMasterView, HLSSegmentView, VideoListView

# The master route comes first (entry point for adaptive playback), then the
# manifest route before the segment route so 'index.m3u8' is never captured by
# the generic <segment> pattern.
urlpatterns = [
    path('video/', VideoListView.as_view(), name='video-list'),
    path(
        'video/<int:movie_id>/master.m3u8',
        HLSMasterView.as_view(),
        name='video-master',
    ),
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
