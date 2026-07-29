"""Pins the setting the API's CSRF protection actually rests on.

DRF's ``APIView.as_view()`` is csrf_exempt and ``CsrfViewMiddleware`` only
enforces a token for ``SessionAuthentication``. Authentication here is a JWT in
a cookie, so no endpoint ever validates a CSRF token: the only thing keeping a
third-party site from POSTing with the victim's cookies is SameSite=Lax on the
auth cookies. That makes ``AUTH_COOKIE_SAMESITE`` a security control, and this
test exists so relaxing it to 'None' fails CI instead of passing silently.
"""

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

from auth_app.tests.helpers import ThrottledAPITestCase

VALID_PASSWORD = 'StrongPass123!'


class CsrfPostureTests(ThrottledAPITestCase):
    """Guard the SameSite setting and record why it matters."""

    def test_auth_cookie_samesite_is_lax(self):
        self.assertEqual(settings.AUTH_COOKIE_SAMESITE, 'Lax')

    def test_samesite_reaches_the_cookies_that_carry_the_jwts(self):
        User.objects.create_user(
            username='cs@example.com', email='cs@example.com', password=VALID_PASSWORD
        )
        response = self.client.post(
            reverse('login'),
            {'email': 'cs@example.com', 'password': VALID_PASSWORD},
            format='json',
        )
        for name in ['access_token', 'refresh_token']:
            self.assertEqual(response.cookies[name]['samesite'], 'Lax')

    def test_session_authentication_is_not_enabled(self):
        """SessionAuthentication would be the one path that enforces CSRF.

        If it is ever added, the reasoning in this module (and the comment in
        core/settings.py) has to be revisited rather than silently inherited.
        """
        configured = settings.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']
        self.assertNotIn('rest_framework.authentication.SessionAuthentication', configured)
