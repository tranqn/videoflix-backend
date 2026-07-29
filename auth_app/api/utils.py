"""Helper functions for the authentication endpoints."""

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import PasswordResetTokenGenerator, default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.utils.html import strip_tags
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

# Frontend pages that read the uid + token query parameters from the link.
ACTIVATION_PATH = '/pages/auth/activate.html'
RESET_PATH = '/pages/auth/confirm_password.html'

# Both auth cookies are site-wide; deletion must repeat the exact same path.
AUTH_COOKIE_PATH = '/'


class ActivationTokenGenerator(PasswordResetTokenGenerator):
    """One-shot token for account activation, in its own namespace.

    Two properties matter here and neither is given by ``default_token_generator``:

    * ``key_salt`` differs from the password-reset generator's, so an activation
      token can never be replayed against ``/api/password_confirm/`` and a reset
      token can never activate an account.
    * ``is_active`` is part of the hash input, so the token stops verifying the
      moment activation flips the flag — the link is usable exactly once.
    """

    key_salt = 'auth_app.api.utils.ActivationTokenGenerator'

    def _make_hash_value(self, user, timestamp):
        """Bind the token to the account's activation state as well."""
        return f'{super()._make_hash_value(user, timestamp)}{user.is_active}'


activation_token_generator = ActivationTokenGenerator()


def set_token_cookie(response, key, token):
    """Store a JWT as an HttpOnly cookie on the given response."""
    response.set_cookie(
        key=key,
        value=str(token),
        path=AUTH_COOKIE_PATH,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


def delete_token_cookie(response, key):
    """Expire an auth cookie with the exact attributes it was set with.

    ``HttpResponse.delete_cookie()`` is not usable here: it derives ``secure``
    from the cookie *name* and never re-sends ``httponly``, so the expiry would
    carry a different attribute set than the original cookie. A browser that
    stored a Secure/SameSite=Lax cookie may then simply keep it. Deleting is
    therefore re-setting the cookie empty and already expired.
    """
    response.set_cookie(
        key=key,
        value='',
        max_age=0,
        expires='Thu, 01 Jan 1970 00:00:00 GMT',
        path=AUTH_COOKIE_PATH,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


def build_login_response(user):
    """Issue tokens, set the auth cookies and return the user payload."""
    refresh = RefreshToken.for_user(user)
    response = Response(
        {
            'detail': 'Login successful',
            'user': {'id': user.id, 'username': user.username},
        },
        status=200,
    )
    set_token_cookie(response, 'access_token', refresh.access_token)
    set_token_cookie(response, 'refresh_token', refresh)
    return response


def blacklist_refresh_token(token):
    """Blacklist a refresh token, ignoring already-invalid ones."""
    try:
        RefreshToken(token).blacklist()
    except TokenError:
        pass


def encode_uid(user):
    """Return the URL-safe base64 encoding of the user's primary key."""
    return urlsafe_base64_encode(force_bytes(user.pk))


def get_user_from_uidb64(uidb64):
    """Return the user for a base64-encoded id, or None if it is invalid."""
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        return User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None


def build_frontend_link(path, uidb64, token):
    """Build a frontend URL carrying the uid and token query parameters."""
    return f'{settings.FRONTEND_BASE_URL}{path}?uid={uidb64}&token={token}'


def send_html_email(subject, recipient, template, context):
    """Render an HTML template and send it with a plain-text fallback."""
    html_body = render_to_string(template, context)
    message = EmailMultiAlternatives(
        subject, strip_tags(html_body), settings.DEFAULT_FROM_EMAIL, [recipient]
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()


def send_activation_email(user_id):
    """Send the account-activation e-mail (enqueued as an RQ job)."""
    user = User.objects.get(pk=user_id)
    link = build_frontend_link(
        ACTIVATION_PATH, encode_uid(user), activation_token_generator.make_token(user)
    )
    send_html_email(
        'Activate your Videoflix account',
        user.email,
        'emails/activation.html',
        {'user': user, 'activation_url': link},
    )


def send_password_reset_email(user_id):
    """Send the password-reset e-mail (enqueued as an RQ job)."""
    user = User.objects.get(pk=user_id)
    link = build_frontend_link(
        RESET_PATH, encode_uid(user), default_token_generator.make_token(user)
    )
    send_html_email(
        'Reset your Videoflix password',
        user.email,
        'emails/password_reset.html',
        {'user': user, 'reset_url': link},
    )
