"""Tests for registration and account activation."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from auth_app.api.utils import activation_token_generator
from auth_app.tests.helpers import ThrottledAPITestCase

VALID_PASSWORD = 'StrongPass123!'


def run_inline(func, *args, **kwargs):
    """Execute an enqueued job synchronously (stand-in for RQ in tests)."""
    return func(*args, **kwargs)


class RegistrationTests(ThrottledAPITestCase):
    """Cover the /api/register/ endpoint."""

    def setUp(self):
        super().setUp()
        self.url = reverse('register')

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_register_creates_inactive_user_and_sends_email(self, _enqueue):
        payload = {
            'email': 'new@example.com',
            'password': VALID_PASSWORD,
            'confirmed_password': VALID_PASSWORD,
        }
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='new@example.com')
        self.assertFalse(user.is_active)
        self.assertEqual(user.username, 'new@example.com')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('uid=', mail.outbox[0].body)

    def test_register_rejects_duplicate_email_generically(self):
        User.objects.create_user(
            username='dup@example.com', email='dup@example.com', password=VALID_PASSWORD
        )
        payload = {
            'email': 'dup@example.com',
            'password': VALID_PASSWORD,
            'confirmed_password': VALID_PASSWORD,
        }
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('already', str(response.data).lower())

    def test_register_rejects_password_mismatch(self):
        payload = {
            'email': 'mismatch@example.com',
            'password': VALID_PASSWORD,
            'confirmed_password': 'Different123!',
        }
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email='mismatch@example.com').exists())


class ActivationTests(ThrottledAPITestCase):
    """Cover the /api/activate/<uidb64>/<token>/ endpoint."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='a@example.com',
            email='a@example.com',
            password=VALID_PASSWORD,
            is_active=False,
        )
        self.uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))

    def test_activation_with_valid_token_activates_account(self):
        token = activation_token_generator.make_token(self.user)
        response = self.client.get(reverse('activate', args=[self.uidb64, token]))
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_activation_with_invalid_token_fails(self):
        response = self.client.get(reverse('activate', args=[self.uidb64, 'invalid']))
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
