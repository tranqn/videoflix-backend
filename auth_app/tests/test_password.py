"""Tests for the password-reset flow."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APITestCase

VALID_PASSWORD = 'StrongPass123!'
NEW_PASSWORD = 'BrandNew456!'


def run_inline(func, *args, **kwargs):
    """Execute an enqueued job synchronously (stand-in for RQ in tests)."""
    return func(*args, **kwargs)


class PasswordResetRequestTests(APITestCase):
    """Cover the /api/password_reset/ endpoint."""

    def setUp(self):
        self.url = reverse('password_reset')
        self.user = User.objects.create_user(
            username='r@example.com', email='r@example.com', password=VALID_PASSWORD
        )

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_reset_sends_email_for_existing_user(self, _enqueue):
        response = self.client.post(self.url, {'email': 'r@example.com'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_reset_is_generic_for_unknown_email(self, _enqueue):
        response = self.client.post(self.url, {'email': 'nobody@example.com'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


class PasswordConfirmTests(APITestCase):
    """Cover the /api/password_confirm/<uidb64>/<token>/ endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='c@example.com', email='c@example.com', password=VALID_PASSWORD
        )
        self.uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        self.token = default_token_generator.make_token(self.user)

    def test_confirm_sets_new_password(self):
        url = reverse('password_confirm', args=[self.uidb64, self.token])
        response = self.client.post(
            url, {'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    def test_confirm_rejects_mismatch(self):
        url = reverse('password_confirm', args=[self.uidb64, self.token])
        response = self.client.post(
            url, {'new_password': NEW_PASSWORD, 'confirm_password': 'Other123!'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_confirm_rejects_invalid_token(self):
        url = reverse('password_confirm', args=[self.uidb64, 'bad-token'])
        response = self.client.post(
            url, {'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(NEW_PASSWORD))
