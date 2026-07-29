"""Tests for the separation of the activation and password-reset tokens."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from auth_app.api.utils import activation_token_generator
from auth_app.tests.helpers import ThrottledAPITestCase

VALID_PASSWORD = 'StrongPass123!'
NEW_PASSWORD = 'BrandNew456!'


def run_inline(func, *args, **kwargs):
    """Execute an enqueued job synchronously (stand-in for RQ in tests)."""
    return func(*args, **kwargs)


class TokenNamespaceTests(ThrottledAPITestCase):
    """The two flows must not accept each other's tokens."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='t@example.com',
            email='t@example.com',
            password=VALID_PASSWORD,
            is_active=False,
        )
        self.uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))

    def post_confirm(self, token):
        """POST a new password to /api/password_confirm/ with the given token."""
        return self.client.post(
            reverse('password_confirm', args=[self.uidb64, token]),
            {'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD},
            format='json',
        )

    def test_activation_token_cannot_reset_the_password(self):
        token = activation_token_generator.make_token(self.user)
        response = self.post_confirm(token)
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(NEW_PASSWORD))

    def test_reset_token_cannot_activate_the_account(self):
        token = default_token_generator.make_token(self.user)
        response = self.client.get(reverse('activate', args=[self.uidb64, token]))
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_activation_token_is_single_use(self):
        token = activation_token_generator.make_token(self.user)
        url = reverse('activate', args=[self.uidb64, token])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        # is_active is part of the hash, so the very same link is now dead.
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_used_activation_token_cannot_reset_the_password_later(self):
        token = activation_token_generator.make_token(self.user)
        self.client.get(reverse('activate', args=[self.uidb64, token]))
        response = self.post_confirm(token)
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(NEW_PASSWORD))


class RegistrationPayloadTests(ThrottledAPITestCase):
    """The registration response must not leak the activation token."""

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_response_contains_no_token(self, _enqueue):
        response = self.client.post(
            reverse('register'),
            {
                'email': 'quiet@example.com',
                'password': VALID_PASSWORD,
                'confirmed_password': VALID_PASSWORD,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn('token', response.data)
        self.assertEqual(set(response.data), {'user'})
        self.assertEqual(set(response.data['user']), {'id', 'email'})

    @patch('django_rq.enqueue', side_effect=run_inline)
    def test_caller_cannot_self_activate_from_the_response(self, _enqueue):
        response = self.client.post(
            reverse('register'),
            {
                'email': 'hijack@example.com',
                'password': VALID_PASSWORD,
                'confirmed_password': VALID_PASSWORD,
            },
            format='json',
        )
        user = User.objects.get(email='hijack@example.com')
        uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
        # Everything the caller was told, tried as an activation token.
        for value in [str(response.data['user']['id']), response.data['user']['email']]:
            self.assertEqual(
                self.client.get(reverse('activate', args=[uidb64, value])).status_code, 400
            )
        user.refresh_from_db()
        self.assertFalse(user.is_active)
