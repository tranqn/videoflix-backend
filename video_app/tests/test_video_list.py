"""Tests for the video list endpoint."""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from video_app.api.utils import (
    VIDEO_LIST_CACHE_GENERATION_KEY,
    invalidate_video_list_cache,
    video_list_cache_key,
)
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

    def cache_key_for(self, host):
        """Return the cache key the view uses for a request to ``host``."""
        request = RequestFactory().get(self.url, headers={'host': host})
        with self.settings(ALLOWED_HOSTS=[host]):
            return video_list_cache_key(request)

    def test_list_is_cached(self):
        self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        self.client.get(self.url)
        self.assertIsNotNone(cache.get(self.cache_key_for('testserver')))

    def test_cache_key_differs_per_host(self):
        self.assertNotEqual(self.cache_key_for('testserver'), self.cache_key_for('other.test'))

    def test_hosts_do_not_see_each_others_payload(self):
        """The cached payload embeds absolute URLs, so it must not cross hosts."""
        self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        with self.settings(ALLOWED_HOSTS=['testserver', 'videoflix.example']):
            first = self.client.get(self.url)
            second = self.client.get(self.url, headers={'host': 'videoflix.example'})
        self.assertTrue(
            first.data[0]['thumbnail_url'].startswith('http://testserver/media/thumbnails/'),
            first.data[0]['thumbnail_url'],
        )
        self.assertTrue(
            second.data[0]['thumbnail_url'].startswith('http://videoflix.example/media/'),
            second.data[0]['thumbnail_url'],
        )

    def test_unknown_host_never_reaches_the_cache(self):
        """A forged Host is rejected by Django, so it cannot create cache keys."""
        self.client.force_authenticate(user=self.user)
        with self.settings(ALLOWED_HOSTS=['testserver'], DEBUG=False):
            response = self.client.get(self.url, headers={'host': 'evil.example'})
        self.assertEqual(response.status_code, 400)

    def test_invalidation_drops_every_hosts_entry(self):
        self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        with self.settings(ALLOWED_HOSTS=['testserver', 'videoflix.example']):
            self.client.get(self.url)
            self.client.get(self.url, headers={'host': 'videoflix.example'})
        hosts = ['testserver', 'videoflix.example']
        self.assertTrue(all(cache.get(self.cache_key_for(host)) for host in hosts))
        invalidate_video_list_cache()
        # The old entries are not deleted, they become unreachable: the current
        # key of every host is a miss again.
        self.assertTrue(all(cache.get(self.cache_key_for(host)) is None for host in hosts))

    def test_a_lost_generation_token_cannot_resurrect_old_entries(self):
        """After an eviction the reseeded generation must be a brand new one."""
        stale = self.cache_key_for('testserver')
        cache.delete(VIDEO_LIST_CACHE_GENERATION_KEY)
        self.assertNotEqual(stale, self.cache_key_for('testserver'))

    @patch('video_app.signals.enqueue_processing')
    def test_save_invalidates_the_cache_only_after_commit(self, _enqueue):
        self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        self.client.get(self.url)
        with self.captureOnCommitCallbacks(execute=False):
            self.make_video('second', Video.Status.READY, timezone.now())
            key = self.cache_key_for('testserver')
            self.assertIsNotNone(cache.get(key), 'cache was invalidated before commit')
        with self.captureOnCommitCallbacks(execute=True):
            self.make_video('third', Video.Status.READY, timezone.now())
        self.assertIsNone(cache.get(self.cache_key_for('testserver')))

    @patch('video_app.signals.enqueue_processing')
    def test_delete_invalidates_the_cache_only_after_commit(self, _enqueue):
        video = self.make_video('movie', Video.Status.READY, timezone.now())
        self.client.force_authenticate(user=self.user)
        self.client.get(self.url)
        with self.captureOnCommitCallbacks(execute=False):
            video.delete()
            key = self.cache_key_for('testserver')
            self.assertIsNotNone(cache.get(key), 'cache was invalidated before commit')
        with self.captureOnCommitCallbacks(execute=True):
            self.make_video('second', Video.Status.READY, timezone.now()).delete()
        self.assertIsNone(cache.get(self.cache_key_for('testserver')))
