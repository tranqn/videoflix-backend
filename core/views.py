"""Infrastructure endpoints: the health check and the Caddy authorization hook."""

import django_rq
from django.core.cache import cache
from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rq.worker import Worker

from auth_app.api.authentication import CookieJWTAuthentication
from video_app.api.permissions import HLS_STREAM_PERMISSIONS
from video_app.api.utils import MEDIA_QUEUE_NAME

_CACHE_HEALTH_KEY = 'health:roundtrip'


class HLSAuthView(APIView):
    """Authorize one HLS request on Caddy's behalf; serve no bytes.

    In production Caddy delivers playlists and segments straight off the media
    volume and never wakes Gunicorn for the payload. It still asks Django
    whether the caller may have them: a ``forward_auth`` subrequest to this
    endpoint, carrying the original request's cookies. 2xx lets Caddy serve the
    file, anything else is returned to the client verbatim.

    Deliberate choices:

    * **Not under /api/.** Caddy's ``@backend`` matcher proxies all of /api/* to
      Gunicorn, so an endpoint there would be reachable from the internet. Under
      /internal/ the fallback handler serves the frontend instead, and this view
      is only reachable in-cluster as ``web:8000``. It is not a security
      boundary by itself -- it leaks nothing but "your cookie is valid" -- but
      an unauthenticated endpoint should not be published for no reason.
    * **204, not 200.** ``forward_auth`` discards the body of a 2xx response.
      A serialized payload here would be bytes rendered and thrown away on
      *every single segment* of every stream.
    * **The permission comes from video_app.api.permissions**, shared with the
      streaming views, so the proxy path and the direct path cannot drift.

    Failure modes are closed by construction: an expired cookie authenticates as
    anonymous and gets a 401, and if this process is down or slow Caddy has no
    2xx to act on and answers 502/504 -- it never falls through to the file.
    """

    authentication_classes = [CookieJWTAuthentication]
    permission_classes = HLS_STREAM_PERMISSIONS

    def get(self, request):
        """Return 204 if the caller may stream; DRF returns 401/403 otherwise."""
        return Response(status=204)


class HealthView(APIView):
    """Report per-dependency health: database, cache, and the media worker.

    200 only if every check below passes; otherwise 503 naming the dependency
    that failed. The worker check is the one that matters most: database and
    cache can be green while an enqueue to a mis-named queue silently never
    runs, so "a worker is registered for the media queue" is checked
    explicitly rather than inferred from the queue merely existing.

    No check is allowed to raise — an unreachable dependency is a 503, not a
    500 crashing this endpoint itself.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        """Run all checks and return the aggregate status."""
        checks = {
            'database': self._check_database(),
            'cache': self._check_cache(),
            'media_queue_worker': self._check_media_worker(),
        }
        healthy = all(ok for ok, _detail in checks.values())
        body = {
            'status': 'ok' if healthy else 'error',
            'checks': {
                name: {'healthy': ok, 'detail': detail} for name, (ok, detail) in checks.items()
            },
        }
        return Response(body, status=200 if healthy else 503)

    @staticmethod
    def _check_database():
        """Confirm the database connection is reachable."""
        try:
            connection.ensure_connection()
        except Exception as exc:  # any failure here is a 503, not a 500
            return False, str(exc)
        return True, 'connected'

    @staticmethod
    def _check_cache():
        """Round-trip a value through the configured cache backend."""
        value = 'ok'
        try:
            cache.set(_CACHE_HEALTH_KEY, value, timeout=5)
            if cache.get(_CACHE_HEALTH_KEY) != value:
                return False, 'set/get round-trip mismatch'
        except Exception as exc:  # any failure here is a 503, not a 500
            return False, str(exc)
        return True, 'round-trip ok'

    @staticmethod
    def _check_media_worker():
        """Confirm a worker is registered for the media queue.

        Synchronous queues (test suite, or ASYNC=False locally) execute jobs
        inline on enqueue and have no worker process to register, so they are
        reported healthy without a Worker.all() lookup — which would
        otherwise need a real Redis connection that does not exist there.
        """
        try:
            queue = django_rq.get_queue(MEDIA_QUEUE_NAME)
            if not queue.is_async:
                return True, 'synchronous queue, no worker required'
            workers = Worker.all(queue=queue)
        except Exception as exc:  # any failure here is a 503, not a 500
            return False, str(exc)
        if not workers:
            return False, f"no worker registered for queue '{MEDIA_QUEUE_NAME}'"
        return True, f'{len(workers)} worker(s) registered'
