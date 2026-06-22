"""auth_app relies on Django's built-in ``User`` model.

Registration stores the e-mail address as both ``email`` and ``username`` so the
account works with the course Docker entrypoint, which creates the superuser via
``create_superuser(username=...)``. No additional models are required here.
"""
