"""Root URL configuration for the Videoflix backend."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
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
        {'document_root': settings.MEDIA_ROOT / 'thumbnails'},
    ),
]
