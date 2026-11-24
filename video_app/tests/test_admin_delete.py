"""Deleting a video while it is transcoding must be refused, not raced.

post_delete rmtree()s hls_root(video.id) — the directory a running ffmpeg job is
writing into. Rather than cancelling the job (which would need a job-id column
and still could not stop a running ffmpeg synchronously), the admin refuses the
delete while status is PROCESSING. Nothing raises, so no admin view can 500.
"""

import shutil
import tempfile
from pathlib import Path

from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from video_app.models import Video

# A Path, not the str mkdtemp() returns: core/urls.py does MEDIA_ROOT / '...'
# at import time and blows up if the setting is a plain string.
MEDIA = Path(tempfile.mkdtemp())

# The admin templates resolve {% static %}; the project's manifest storage needs a
# collectstatic run the test suite does not do.
PLAIN_STATICFILES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


@override_settings(MEDIA_ROOT=MEDIA, STORAGES=PLAIN_STATICFILES)
class AdminDeleteGuardTests(TestCase):
    """Cover both admin delete paths: the single object view and the bulk action."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='root@example.com', email='root@example.com', password='StrongPass123!'
        )
        self.client.force_login(self.admin)
        self.model_admin = site._registry[Video]

    def make_video(self, status):
        return Video.objects.create(title='m', category='Drama', status=status)

    def request(self):
        request = RequestFactory().get('/admin/')
        request.user = self.admin
        return request

    def bulk_delete(self, video, confirm):
        data = {'action': 'delete_selected', '_selected_action': [str(video.pk)]}
        if confirm:
            data['post'] = 'yes'
        return self.client.post(reverse('admin:video_app_video_changelist'), data, follow=False)

    def test_permission_is_denied_while_processing(self):
        video = self.make_video(Video.Status.PROCESSING)
        self.assertFalse(self.model_admin.has_delete_permission(self.request(), video))

    def test_permission_is_granted_for_every_other_status(self):
        for status in (Video.Status.PENDING, Video.Status.READY, Video.Status.FAILED):
            video = self.make_video(status)
            self.assertTrue(self.model_admin.has_delete_permission(self.request(), video), status)

    def test_delete_view_refuses_a_processing_video_without_a_500(self):
        video = self.make_video(Video.Status.PROCESSING)
        url = reverse('admin:video_app_video_delete', args=[video.pk])
        response = self.client.post(url, {'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Video.objects.filter(pk=video.pk).exists())

    def test_delete_view_still_works_for_a_ready_video(self):
        video = self.make_video(Video.Status.READY)
        url = reverse('admin:video_app_video_delete', args=[video.pk])
        response = self.client.post(url, {'post': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Video.objects.filter(pk=video.pk).exists())

    def test_bulk_delete_confirmation_reports_the_missing_permission(self):
        video = self.make_video(Video.Status.PROCESSING)
        response = self.bulk_delete(video, confirm=False)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'permission')
        self.assertTrue(Video.objects.filter(pk=video.pk).exists())

    def test_bulk_delete_refuses_a_processing_video_without_a_500(self):
        video = self.make_video(Video.Status.PROCESSING)
        response = self.bulk_delete(video, confirm=True)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Video.objects.filter(pk=video.pk).exists())

    def test_bulk_delete_still_works_for_a_ready_video(self):
        video = self.make_video(Video.Status.READY)
        response = self.bulk_delete(video, confirm=True)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Video.objects.filter(pk=video.pk).exists())

    def test_delete_queryset_never_deletes_a_processing_video(self):
        """Defence in depth for any code path that reaches delete_queryset directly."""
        processing = self.make_video(Video.Status.PROCESSING)
        ready = self.make_video(Video.Status.READY)
        request = self.request()
        request._messages = FakeMessages()
        self.model_admin.delete_queryset(request, Video.objects.all())
        self.assertTrue(Video.objects.filter(pk=processing.pk).exists())
        self.assertFalse(Video.objects.filter(pk=ready.pk).exists())


class FakeMessages(list):
    """Message store stand-in for a RequestFactory request (no middleware)."""

    def add(self, level, message, extra_tags):
        """Record a message the way django.contrib.messages would."""
        self.append((level, message))
