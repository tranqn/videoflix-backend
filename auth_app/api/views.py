"""Authentication endpoints: register, activate, login, refresh, logout, reset."""

import django_rq
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.views import TokenRefreshView

from .serializers import (
    LoginSerializer,
    PasswordConfirmSerializer,
    PasswordResetSerializer,
    RegistrationSerializer,
)
from .utils import (
    blacklist_refresh_token,
    build_login_response,
    get_user_from_uidb64,
    send_activation_email,
    send_password_reset_email,
    set_token_cookie,
)


class RegistrationView(APIView):
    """Create a new (inactive) user account and trigger the activation mail."""

    permission_classes = [AllowAny]

    def post(self, request):
        """Validate the form, persist the user and enqueue the e-mail."""
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        django_rq.enqueue(send_activation_email, user.id)
        return Response(self.build_payload(user), status=201)

    def build_payload(self, user):
        """Build the registration response (token is informational only)."""
        token = default_token_generator.make_token(user)
        return {'user': {'id': user.id, 'email': user.email}, 'token': token}


class ActivateView(APIView):
    """Activate an account from the uid + token in the activation link."""

    permission_classes = [AllowAny]

    def get(self, request, uidb64, token):
        """Validate the token and flip the account to active."""
        user = get_user_from_uidb64(uidb64)
        if user is None or not default_token_generator.check_token(user, token):
            return Response({'message': 'Activation failed.'}, status=400)
        user.is_active = True
        user.save(update_fields=['is_active'])
        return Response({'message': 'Account successfully activated.'}, status=200)


class LoginView(APIView):
    """Authenticate a user and deliver the JWTs as HttpOnly cookies."""

    permission_classes = [AllowAny]

    def post(self, request):
        """Validate credentials and start an authenticated session."""
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.authenticate_user(serializer.validated_data)

    def authenticate_user(self, credentials):
        """Return a login response or the appropriate error response."""
        user = authenticate(username=credentials['email'], password=credentials['password'])
        if user is not None:
            return build_login_response(user)
        return self.handle_failed_login(credentials)

    def handle_failed_login(self, credentials):
        """Distinguish an inactive account from bad credentials (generic)."""
        candidate = User.objects.filter(email__iexact=credentials['email']).first()
        if self.is_inactive_match(candidate, credentials['password']):
            return Response({'detail': 'Please activate your account first.'}, status=403)
        return Response({'detail': 'Invalid credentials.'}, status=401)

    def is_inactive_match(self, candidate, password):
        """Return True if the account exists, is inactive and the password is correct."""
        return bool(candidate and not candidate.is_active and candidate.check_password(password))


class CookieTokenRefreshView(TokenRefreshView):
    """Refresh the access token using the refresh-token cookie."""

    # simplejwt ships no type information, so mypy infers TokenViewBase's
    # `permission_classes = ()` as the empty-tuple type and rejects any
    # override. The narrow base annotation is the defect, not this line.
    permission_classes = [AllowAny]  # type: ignore[assignment]

    def post(self, request, *args, **kwargs):
        """Read the refresh cookie and rotate the tokens."""
        refresh_token = request.COOKIES.get('refresh_token')
        if refresh_token is None:
            return Response({'detail': 'Refresh token not found.'}, status=400)
        return self.refresh_tokens(refresh_token)

    def refresh_tokens(self, refresh_token):
        """Validate the refresh token and build the rotated-cookie response."""
        serializer = self.get_serializer(data={'refresh': refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except (InvalidToken, TokenError):
            return Response({'detail': 'Refresh token invalid.'}, status=401)
        return self.build_refresh_response(serializer.validated_data)

    def build_refresh_response(self, data):
        """Set the new access (and rotated refresh) cookie."""
        response = Response(
            {'detail': 'Token refreshed', 'access': str(data['access'])}, status=200
        )
        set_token_cookie(response, 'access_token', data['access'])
        if data.get('refresh'):
            set_token_cookie(response, 'refresh_token', data['refresh'])
        return response


class LogoutView(APIView):
    """Log the user out and invalidate the refresh token."""

    permission_classes = [AllowAny]

    def post(self, request):
        """Blacklist the refresh token and clear both auth cookies."""
        token = request.COOKIES.get('refresh_token')
        if token is None:
            return Response({'detail': 'Refresh token not found.'}, status=400)
        blacklist_refresh_token(token)
        return self.build_logout_response()

    def build_logout_response(self):
        """Return the logout response with both auth cookies deleted."""
        response = Response(
            {
                'detail': 'Logout successful! All tokens will be deleted. '
                'Refresh token is now invalid.',
            },
            status=200,
        )
        response.delete_cookie('access_token')
        response.delete_cookie('refresh_token')
        return response


class PasswordResetView(APIView):
    """Send a reset link without revealing whether the account exists."""

    permission_classes = [AllowAny]

    def post(self, request):
        """Accept an e-mail and (if it matches) enqueue the reset mail."""
        serializer = PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.dispatch_reset_email(serializer.validated_data['email'])
        return Response({'detail': 'An email has been sent to reset your password.'}, status=200)

    def dispatch_reset_email(self, email):
        """Enqueue the reset mail only for an existing active account."""
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            django_rq.enqueue(send_password_reset_email, user.id)


class PasswordConfirmView(APIView):
    """Set a new password from the uid + token in the reset link."""

    permission_classes = [AllowAny]

    def post(self, request, uidb64, token):
        """Validate the token and store the new password."""
        serializer = PasswordConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_user_from_uidb64(uidb64)
        if user is None or not default_token_generator.check_token(user, token):
            return Response({'detail': 'Invalid or expired reset link.'}, status=400)
        self.store_password(user, serializer.validated_data['new_password'])
        return Response({'detail': 'Your Password has been successfully reset.'}, status=200)

    def store_password(self, user, new_password):
        """Persist the new password hash."""
        user.set_password(new_password)
        user.save(update_fields=['password'])
