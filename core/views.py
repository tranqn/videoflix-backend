"""Health check endpoint for container orchestration and monitoring."""

import django_rq
from django.core.cache import cache
from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rq.worker import Worker

from video_app.api.utils import MEDIA_QUEUE_NAME

_CACHE_HEALTH_KEY = 'health:roundtrip'


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
