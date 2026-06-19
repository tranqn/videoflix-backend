"""WSGI config for the core project.

Exposes the WSGI callable as a module-level variable named ``application``.
Gunicorn serves this in the Docker setup (``gunicorn core.wsgi:application``).
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

application = get_wsgi_application()
