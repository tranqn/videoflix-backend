"""Tests for the rate limits on the unauthenticated auth endpoints.

Every class here inherits the cache clearing from ThrottledAPITestCase: DRF
keeps its request history in the cache, the test cache is LocMem and therefore
process-wide, so without clearing, these tests would consume each other's
budgets (and everyone else's).
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse

from auth_app.tests.helpers import ThrottledAPITestCase

VALID_PASSWORD = 'StrongPass123!'

# Mirrors core.settings REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'].
LOGIN_LIMIT = 10
REGISTER_LIMIT = 5
PASSWORD_RESET_LIMIT = 3


def run_inline(func, *args, **kwargs):
    """Execute an enqueued job synchronously (stand-in for RQ in tests)."""
    return func(*args, **kwargs)


class LoginThrottleTests(ThrottledAPITestCase):
    """/api/login/ is limited to 10 attempts per minute and IP."""

    def setUp(self):
        super().setUp()
        self.url = reverse('login')
        self.user = User.objects.create_user(
            username='th@example.com', email='th@example.com', password=VALID_PASSWORD
        )

    def attempt(self, **extra):
        """Send one wrong-password login attempt."""
        return self.client.post(
            self.url,
            {'email': 'th@example.com', 'password': 'WrongPass123!'},
            format='json',
            **extra,
        )

    def test_login_is_throttled_after_the_limit(self):
        for _ in range(LOGIN_LIMIT):
            self.assertEqual(self.attempt().status_code, 401)
        self.assertEqual(self.attempt().status_code, 429)

    def test_throttled_login_rejects_even_correct_credentials(self):
        for _ in range(LOGIN_LIMIT):
            self.attempt()
        response = self.client.post(
            self.url, {'email': 'th@example.com', 'password': VALID_PASSWORD}, format='json'
        )
        self.assertEqual(response.status_code, 429)

    def test_forged_x_forwarded_for_does_not_buy_a_new_bucket(self):
        """NUM_PROXIES=1 means only the proxy's own XFF entry counts.

        The header below is what Caddy produces when a client sends
        ``X-Forwarded-For: 9.9.9.9``: the real peer is appended last. A caller
        rotating the spoofed part must stay in the 127.0.0.1 bucket.
        """
        for index in range(LOGIN_LIMIT):
            forged = f'9.9.9.{index}, 127.0.0.1'
            self.assertEqual(self.attempt(HTTP_X_FORWARDED_FOR=forged).status_code, 401)
        self.assertEqual(self.attempt(HTTP_X_FORWARDED_FOR='9.9.9.99, 127.0.0.1').status_code, 429)


class RegisterThrottleTests(ThrottledAPITestCase):
    """/api/register/ is limited to 5 sign-ups per hour and IP."""

    def register(self, index):
        """Register one fresh account."""
        return self.client.post(
            reverse('register'),
            {
                'email': f'flood{index}@example.com',
                'password': VALID_PASSWORD,
                'confirmed_password': VALID_PASSWORD,
            },
            format='json',
        )

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_register_is_throttled_after_the_limit(self, _enqueue):
        for index in range(REGISTER_LIMIT):
            self.assertEqual(self.register(index).status_code, 201)
        self.assertEqual(self.register(REGISTER_LIMIT).status_code, 429)
        self.assertEqual(User.objects.count(), REGISTER_LIMIT)


class PasswordResetThrottleTests(ThrottledAPITestCase):
    """/api/password_reset/ is limited to 3 requests per hour and IP."""

    def setUp(self):
        super().setUp()
        self.url = reverse('password_reset')
        User.objects.create_user(
            username='pr@example.com', email='pr@example.com', password=VALID_PASSWORD
        )

    def request_reset(self):
        """Ask for a reset mail for the existing account."""
        return self.client.post(self.url, {'email': 'pr@example.com'}, format='json')

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_password_reset_is_throttled_after_the_limit(self, _enqueue):
        for _ in range(PASSWORD_RESET_LIMIT):
            self.assertEqual(self.request_reset().status_code, 200)
        self.assertEqual(self.request_reset().status_code, 429)
        # The point of the limit: no further mail, no further queued job.
        self.assertEqual(len(mail.outbox), PASSWORD_RESET_LIMIT)
