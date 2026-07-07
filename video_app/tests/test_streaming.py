"""Tests for HLS manifest and segment streaming."""
import shutil
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from video_app.models import Video

VALID_PASSWORD = 'StrongPass123!'
MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class StreamingTests(APITestCase):
    """Cover the HLS manifest and segment endpoints."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(
            username='w@example.com', email='w@example.com', password=VALID_PASSWORD
        )
        self.video = Video.objects.create(
            title='m', category='Drama', status=Video.Status.READY
        )
        self.hls_dir = Path(MEDIA) / 'hls' / str(self.video.id) / '480p'
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        (self.hls_dir / 'index.m3u8').write_text('#EXTM3U\n')
        (self.hls_dir / '000.ts').write_bytes(b'\x47\x40\x00')

    def manifest_url(self, resolution='480p'):
        return reverse('video-manifest', args=[self.video.id, resolution])

    def segment_url(self, segment='000.ts', resolution='480p'):
        return reverse('video-segment', args=[self.video.id, resolution, segment])

    def test_manifest_requires_auth(self):
        self.assertEqual(self.client.get(self.manifest_url()).status_code, 401)

    def test_manifest_served_with_content_type(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.manifest_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.apple.mpegurl')

    def test_manifest_unknown_resolution_returns_404(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get(self.manifest_url('999p')).status_code, 404)

    def test_segment_served_with_content_type(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.segment_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'video/MP2T')

    def test_segment_requires_auth(self):
        self.assertEqual(self.client.get(self.segment_url()).status_code, 401)

    def test_segment_rejects_non_numeric_name(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get(self.segment_url('evil.ts')).status_code, 404)

    def test_segment_missing_file_returns_404(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get(self.segment_url('999.ts')).status_code, 404)
