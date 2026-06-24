"""Serializers for registration, login and password reset."""
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

# Deliberately vague message (no account-existence hint) for the registration
# form, as required by the security note in the checklist.
GENERIC_INPUT_ERROR = 'Please check your entries and try again.'


def check_password_strength(password):
    """Run Django's password validators, re-raising as a DRF error."""
    try:
        validate_password(password)
    except DjangoValidationError as error:
        raise serializers.ValidationError(list(error.messages))


class RegistrationSerializer(serializers.ModelSerializer):
    """Validate the registration form and create an inactive user."""

    confirmed_password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'confirmed_password']
        extra_kwargs = {'password': {'write_only': True}}

    def validate_email(self, value):
        """Reject e-mails already in use, without confirming which one."""
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(GENERIC_INPUT_ERROR)
        return value

    def validate(self, data):
        """Ensure the passwords match and satisfy the strength rules."""
        if data['password'] != data['confirmed_password']:
            raise serializers.ValidationError(
                {'confirmed_password': 'The passwords do not match.'}
            )
        check_password_strength(data['password'])
        return data

    def create(self, validated_data):
        """Create the user (username = email) as inactive until activation."""
        user = User(
            username=validated_data['email'],
            email=validated_data['email'],
            is_active=False,
        )
        user.set_password(validated_data['password'])
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    """Validate login credentials (email + password)."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class PasswordResetSerializer(serializers.Serializer):
    """Validate the e-mail address for a password-reset request."""

    email = serializers.EmailField()


class PasswordConfirmSerializer(serializers.Serializer):
    """Validate the new password supplied with a reset token."""

    new_password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, data):
        """Ensure both passwords match and satisfy the strength rules."""
        if data['new_password'] != data['confirm_password']:
            raise serializers.ValidationError(
                {'confirm_password': 'The passwords do not match.'}
            )
        check_password_strength(data['new_password'])
        return data
