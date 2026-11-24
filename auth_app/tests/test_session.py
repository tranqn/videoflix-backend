"""Tests for login, logout and token refresh."""

from django.contrib.auth.models import User
from django.urls import reverse

from auth_app.tests.helpers import ThrottledAPITestCase

VALID_PASSWORD = 'StrongPass123!'


class LoginTests(ThrottledAPITestCase):
    """Cover the /api/login/ endpoint."""

    def setUp(self):
        super().setUp()
        self.url = reverse('login')
        self.user = User.objects.create_user(
            username='u@example.com', email='u@example.com', password=VALID_PASSWORD
        )

    def test_login_success_sets_cookies(self):
        response = self.client.post(
            self.url, {'email': 'u@example.com', 'password': VALID_PASSWORD}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)
        self.assertEqual(response.data['user']['username'], 'u@example.com')

    def test_login_wrong_password_is_generic_401(self):
        response = self.client.post(
            self.url, {'email': 'u@example.com', 'password': 'WrongPass123!'}, format='json'
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data['detail'], 'Invalid credentials.')

    def test_login_inactive_account_returns_403(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        response = self.client.post(
            self.url, {'email': 'u@example.com', 'password': VALID_PASSWORD}, format='json'
        )
        self.assertEqual(response.status_code, 403)


class SessionTokenTests(ThrottledAPITestCase):
    """Cover token refresh and logout, including blacklisting."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='s@example.com', email='s@example.com', password=VALID_PASSWORD
        )
        self.client.post(
            reverse('login'),
            {'email': 's@example.com', 'password': VALID_PASSWORD},
            format='json',
        )
        self.refresh_value = self.client.cookies['refresh_token'].value

    def test_refresh_with_cookie_succeeds(self):
        response = self.client.post(reverse('token_refresh'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)

    def test_refresh_without_cookie_returns_400(self):
        self.client.cookies.clear()
        response = self.client.post(reverse('token_refresh'))
        self.assertEqual(response.status_code, 400)

    def test_refresh_with_invalid_cookie_returns_401(self):
        self.client.cookies['refresh_token'] = 'not-a-token'
        response = self.client.post(reverse('token_refresh'))
        self.assertEqual(response.status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 200)
        self.client.cookies['refresh_token'] = self.refresh_value
        retry = self.client.post(reverse('token_refresh'))
        self.assertEqual(retry.status_code, 401)

    def test_logout_without_cookie_returns_400(self):
        self.client.cookies.clear()
        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 400)
