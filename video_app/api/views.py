"""Video dashboard and HLS streaming endpoints."""

import re

from django.core.cache import cache
from django.http import FileResponse, Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from video_app.models import Video

from .permissions import HLS_STREAM_PERMISSIONS
from .serializers import VideoListSerializer
from .utils import (
    RESOLUTION_HEIGHTS,
    VIDEO_LIST_CACHE_TTL,
    hls_root,
    video_list_cache_key,
)

# Segment file names ffmpeg produces (e.g. 000.ts); blocks path traversal.
#
# Mirrored in caddy/Caddyfile: the @hls matcher's regexp ends in `[0-9]+\.ts`
# and lists the RESOLUTION_HEIGHTS keys literally, because in production Caddy
# decides which paths it may serve from disk without asking Django. Loosening
# either pattern without the other creates a shape that one of the two paths
# serves and the other 404s. video_app/tests/test_caddy_hls_contract.py fails
# when they drift.
SEGMENT_PATTERN = re.compile(r'^\d+\.ts$')


class VideoListView(APIView):
    """Return the list of ready videos, newest first (cached in Redis)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Return the cached payload, building it on a cache miss.

        The key is scoped to the request origin because the payload contains
        absolute (Host-dependent) thumbnail URLs, and it carries the current
        invalidation generation, so an entry written from a generation that has
        meanwhile been bumped is simply never read again.
        """
        key = video_list_cache_key(request)
        payload = cache.get(key)
        if payload is None:
            payload = self.build_payload(request)
            cache.set(key, payload, VIDEO_LIST_CACHE_TTL)
        return Response(payload, status=200)

    def build_payload(self, request):
        """Serialize all READY videos ordered by newest first."""
        videos = Video.objects.filter(status=Video.Status.READY)
        serializer = VideoListSerializer(videos, many=True, context={'request': request})
        return serializer.data


class HLSMasterView(APIView):
    """Serve the master HLS playlist that lists every rendition of a video."""

    permission_classes = HLS_STREAM_PERMISSIONS

    def get(self, request, movie_id):
        """Return the master.m3u8, or 404 if it has not been built yet."""
        master = hls_root(movie_id) / 'master.m3u8'
        if not master.is_file():
            raise Http404
        return FileResponse(open(master, 'rb'), content_type='application/vnd.apple.mpegurl')


class HLSManifestView(APIView):
    """Serve the HLS playlist (index.m3u8) for a video and resolution."""

    permission_classes = HLS_STREAM_PERMISSIONS

    def get(self, request, movie_id, resolution):
        """Return the m3u8 manifest, or 404 if it does not exist."""
        if resolution not in RESOLUTION_HEIGHTS:
            raise Http404
        manifest = hls_root(movie_id) / resolution / 'index.m3u8'
        if not manifest.is_file():
            raise Http404
        return FileResponse(open(manifest, 'rb'), content_type='application/vnd.apple.mpegurl')


class HLSSegmentView(APIView):
    """Serve a single HLS segment (.ts) for a video and resolution."""

    permission_classes = HLS_STREAM_PERMISSIONS

    def get(self, request, movie_id, resolution, segment):
        """Return the requested .ts segment, or 404 if invalid/missing."""
        if resolution not in RESOLUTION_HEIGHTS or not SEGMENT_PATTERN.match(segment):
            raise Http404
        segment_path = hls_root(movie_id) / resolution / segment
        if not segment_path.is_file():
            raise Http404
        return FileResponse(open(segment_path, 'rb'), content_type='video/MP2T')
