"""Tests for /internal/hls-auth/, the forward_auth hook Caddy calls per request."""

from django.contrib.auth.models import User
from django.urls import resolve, reverse
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from core.views import HLSAuthView
from video_app.api.permissions import HLS_STREAM_PERMISSIONS

URL = '/internal/hls-auth/'


class HLSAuthEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='v@example.com', email='v@example.com', password='StrongPass123!'
        )

    def authenticate(self, user=None):
        token = RefreshToken.for_user(user or self.user).access_token
        self.client.cookies['access_token'] = str(token)

    def test_url_is_reachable_at_the_path_caddy_calls(self):
        # The Caddyfile hard-codes this string; reverse() would hide a rename.
        self.assertEqual(reverse('hls-auth'), URL)
        self.assertIs(resolve(URL).func.view_class, HLSAuthView)

    def test_not_published_under_api(self):
        # /api/* is proxied to Gunicorn by the Caddyfile, so an endpoint there
        # would be reachable from the internet.
        self.assertFalse(URL.startswith('/api/'))

    def test_anonymous_is_denied(self):
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_invalid_cookie_is_denied(self):
        self.client.cookies['access_token'] = 'not-a-jwt'
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_authenticated_gets_204_with_no_body(self):
        self.authenticate()
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 204)
        # forward_auth throws the body away on 2xx; rendering one would cost
        # bytes on every segment of every stream.
        self.assertEqual(response.content, b'')

    def test_inactive_user_is_denied(self):
        self.authenticate()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_only_get_is_allowed(self):
        self.authenticate()
        self.assertEqual(self.client.post(URL).status_code, 405)

    def test_shares_the_streaming_permission(self):
        # The whole point of the extraction: tightening the streaming rule must
        # reach the proxy path too, not just the Django views.
        self.assertIs(HLSAuthView.permission_classes, HLS_STREAM_PERMISSIONS)
