"""Tests for the video list endpoint."""

import datetime

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from video_app.api.utils import VIDEO_LIST_CACHE_KEY
from video_app.models import Video

VALID_PASSWORD = 'StrongPass123!'


class VideoListTests(APITestCase):
    """Cover the /api/video/ endpoint."""

    def setUp(self):
        cache.clear()
        self.url = reverse('video-list')
        self.user = User.objects.create_user(
            username='v@example.com', email='v@example.com', password=VALID_PASSWORD
        )

    def make_video(self, title, status, created):
        """Create a video and force a deterministic creation timestamp."""
        video = Video.objects.create(
            title=title,
            category='Drama',
            status=status,
            thumbnail=f'thumbnails/{title}.jpg',
        )
        Video.objects.filter(pk=video.pk).update(created_at=created)
        return video

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_lists_ready_videos_newest_first(self):
        now = timezone.now()
        self.make_video('old', Video.Status.READY, now - datetime.timedelta(days=2))
        self.make_video('new', Video.Status.READY, now)
        self.make_video('pending', Video.Status.PENDING, now)
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([video['title'] for video in response.data], ['new', 'old'])

    def test_thumbnail_url_is_absolute(self):
        self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertTrue(
            response.data[0]['thumbnail_url'].startswith('http://testserver/media/thumbnails/')
        )

    def test_list_is_cached(self):
        self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        self.client.get(self.url)
        self.assertIsNotNone(cache.get(VIDEO_LIST_CACHE_KEY))
