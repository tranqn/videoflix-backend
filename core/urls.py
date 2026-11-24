"""Root URL configuration for the Videoflix backend."""

from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path, re_path
from django.views.static import serve

# Annotated because the list starts out holding only URLResolvers (every entry
# is an include()); without it mypy locks the element type in and rejects the
# plain URLPattern appended below.
urlpatterns: list[URLPattern | URLResolver] = [
    path('admin/', admin.site.urls),
    path('django-rq/', include('django_rq.urls')),
    path('api/', include('auth_app.api.urls')),
    path('api/', include('video_app.api.urls')),
]

# Serve thumbnails directly (public images used as <img> sources). HLS playlists
# and segments are NOT exposed here — they are delivered only through the
# authenticated API views, so streaming stays behind JWT auth.
urlpatterns += [
    re_path(
        r'^media/thumbnails/(?P<path>.*)$',
        serve,
        # Path() rather than `settings.MEDIA_ROOT / ...`: this is evaluated at
        # import time, and a test that overrides MEDIA_ROOT with a plain string
        # (tempfile.mkdtemp() returns one) made importing this module raise
        # TypeError. The suite only passed because another test imported it
        # first while the setting was still a Path.
        {'document_root': Path(settings.MEDIA_ROOT) / 'thumbnails'},
    ),
]
