"""HealthView: per-dependency status, and the SSL-redirect exemption it needs.

The suite runs with sqlite, locmem cache and synchronous RQ (see
core/settings.py TESTING block), so these tests exercise the view in exactly
that environment as well as mocking the failure paths that only show up
against real Postgres/Redis.
"""

import re
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse


class HealthViewTests(TestCase):
    """Happy path: every dependency reports healthy in the test environment."""

    def test_returns_200_and_ok_when_all_dependencies_are_healthy(self):
        response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['checks']['database']['healthy'])
        self.assertTrue(body['checks']['cache']['healthy'])
        self.assertTrue(body['checks']['media_queue_worker']['healthy'])

    def test_media_queue_is_synchronous_in_the_test_suite(self):
        """RQ_QUEUES['media']['ASYNC'] = False in tests, so no worker is required."""
        response = self.client.get(reverse('health'))
        detail = response.json()['checks']['media_queue_worker']['detail']
        self.assertIn('synchronous', detail)

    def test_database_failure_returns_503_naming_the_dependency(self):
        with patch('core.views.connection') as mock_connection:
            mock_connection.ensure_connection.side_effect = Exception('boom')
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body['status'], 'error')
        self.assertFalse(body['checks']['database']['healthy'])
        self.assertIn('boom', body['checks']['database']['detail'])
        # An unrelated dependency failing does not hide the others' status.
        self.assertTrue(body['checks']['cache']['healthy'])

    def test_cache_failure_returns_503_naming_the_dependency(self):
        with patch('core.views.cache') as mock_cache:
            mock_cache.set.side_effect = Exception('cache down')
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['checks']['cache']['healthy'])

    def test_missing_worker_on_an_async_queue_returns_503(self):
        """The failure this endpoint exists to catch: a queue nobody drains."""
        fake_queue = MagicMock(is_async=True)
        with (
            patch('core.views.django_rq.get_queue', return_value=fake_queue),
            patch('core.views.Worker.all', return_value=[]),
        ):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        detail = response.json()['checks']['media_queue_worker']
        self.assertFalse(detail['healthy'])
        self.assertIn('no worker registered', detail['detail'])

    def test_worker_lookup_raising_is_a_503_not_a_500(self):
        with patch('core.views.django_rq.get_queue', side_effect=Exception('redis down')):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertIn('redis down', response.json()['checks']['media_queue_worker']['detail'])


@override_settings(DEBUG=False, SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=['testserver'])
class HealthEndpointSslRedirectExemptionTests(TestCase):
    """The container healthcheck speaks plain HTTP; it must not be redirected."""

    def test_health_endpoint_is_exempt_from_the_ssl_redirect(self):
        response = self.client.get('/api/health/')
        self.assertNotIn(response.status_code, (301, 308))

    def test_the_exempt_pattern_matches_the_path_without_its_leading_slash(self):
        """SecurityMiddleware strips the leading '/' before matching.

        A pattern anchored on '/api/health/' would silently never match and
        the exemption above would be dead code, so this pins the actual
        stripped form the pattern has to match.
        """
        path = '/api/health/'.lstrip('/')
        patterns = [re.compile(p) for p in settings.SECURE_REDIRECT_EXEMPT]
        self.assertTrue(any(p.search(path) for p in patterns))
