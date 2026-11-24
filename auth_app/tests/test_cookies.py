"""Tests for the auth-cookie attributes on login and logout."""

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

from auth_app.api.utils import AUTH_COOKIE_PATH
from auth_app.tests.helpers import ThrottledAPITestCase

VALID_PASSWORD = 'StrongPass123!'
COOKIE_NAMES = ['access_token', 'refresh_token']
# Attributes a browser compares when deciding whether a Set-Cookie replaces an
# existing cookie; a mismatch leaves the original one in place.
MATCHED_ATTRIBUTES = ['path', 'samesite', 'secure', 'httponly', 'domain']


class LogoutCookieTests(ThrottledAPITestCase):
    """Deleting the auth cookies must mirror how they were set."""

    def setUp(self):
        super().setUp()
        User.objects.create_user(
            username='ck@example.com', email='ck@example.com', password=VALID_PASSWORD
        )
        self.login_response = self.client.post(
            reverse('login'),
            {'email': 'ck@example.com', 'password': VALID_PASSWORD},
            format='json',
        )
        self.logout_response = self.client.post(reverse('logout'))

    def test_logout_expires_both_cookies(self):
        self.assertEqual(self.logout_response.status_code, 200)
        for name in COOKIE_NAMES:
            morsel = self.logout_response.cookies[name]
            self.assertEqual(morsel.value, '')
            self.assertEqual(morsel['max-age'], 0)
            self.assertEqual(morsel['expires'], 'Thu, 01 Jan 1970 00:00:00 GMT')

    def test_deletion_attributes_match_creation(self):
        for name in COOKIE_NAMES:
            created = self.login_response.cookies[name]
            deleted = self.logout_response.cookies[name]
            for attribute in MATCHED_ATTRIBUTES:
                self.assertEqual(
                    deleted[attribute],
                    created[attribute],
                    f'{name}: {attribute} differs between set and delete',
                )

    def test_attributes_are_the_configured_ones(self):
        for name in COOKIE_NAMES:
            morsel = self.logout_response.cookies[name]
            self.assertEqual(morsel['path'], AUTH_COOKIE_PATH)
            self.assertEqual(morsel['samesite'], settings.AUTH_COOKIE_SAMESITE)
            self.assertEqual(bool(morsel['secure']), settings.AUTH_COOKIE_SECURE)
            self.assertTrue(morsel['httponly'])
