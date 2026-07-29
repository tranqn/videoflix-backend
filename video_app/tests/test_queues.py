"""Queue split: transcoding must never share a queue (or worker) with e-mails.

The dangerous half of this change is not the enqueue side, it is the consumer
side: an enqueue to a queue no worker pops from raises nothing at all, the job
just never runs. These tests read the two hardcoded consumers (the dev entrypoint
and the production compose file) and assert they cover every configured queue.
"""

import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from video_app.admin import VideoAdmin
from video_app.api.utils import MEDIA_QUEUE_NAME, VIDEO_JOB_TIMEOUT
from video_app.models import Video
from video_app.tasks import enqueue_processing, mark_video_failed, process_video

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEV_ENTRYPOINT = BACKEND_DIR / 'backend.entrypoint.sh'
PROD_COMPOSE = BACKEND_DIR / 'compose.prod.yml'
RQWORKER_LINE = re.compile(r'manage\.py rqworker ([\w\s-]+)')
SERVICE_LINE = re.compile(r'^  (?P<name>[\w-]+):\s*$')
ENTRYPOINT_LINE = re.compile(r'^\s*entrypoint:\s*\[(?P<items>.*)\]\s*$')


def dev_worker_queues():
    """Return the queue names the dev entrypoint's rqworker consumes."""
    match = RQWORKER_LINE.search(DEV_ENTRYPOINT.read_text())
    assert match, 'no `manage.py rqworker` line in backend.entrypoint.sh'
    return [word for word in match.group(1).split() if not word.startswith('-')]


def prod_worker_queues():
    """Return {service name: [queues]} for every rqworker service in production.

    Read line by line instead of with a YAML parser: PyYAML is not a declared
    dependency of this project, it only happens to come in with bandit.
    """
    workers = {}
    service = None
    for line in PROD_COMPOSE.read_text().splitlines():
        header = SERVICE_LINE.match(line)
        if header:
            service = header.group('name')
            continue
        entrypoint = ENTRYPOINT_LINE.match(line)
        if not entrypoint:
            continue
        words = [word.strip(' "\'') for word in entrypoint.group('items').split(',')]
        if 'rqworker' in words:
            rest = words[words.index('rqworker') + 1 :]
            workers[service] = [word for word in rest if word and not word.startswith('-')]
    return workers


class QueueConfigurationTests(TestCase):
    """The queues themselves."""

    def test_default_and_media_queues_exist(self):
        self.assertEqual(set(settings.RQ_QUEUES), {'default', 'media'})

    def test_media_queue_allows_a_long_transcode(self):
        self.assertGreaterEqual(settings.RQ_QUEUES['media']['DEFAULT_TIMEOUT'], VIDEO_JOB_TIMEOUT)


class EnqueueTargetTests(TestCase):
    """Transcoding is enqueued on 'media', never on the e-mail queue."""

    @patch('django_rq.get_queue')
    def test_enqueue_processing_uses_the_media_queue(self, get_queue):
        enqueue_processing(42)
        get_queue.assert_called_once_with(MEDIA_QUEUE_NAME)
        _, kwargs = get_queue.return_value.enqueue.call_args
        self.assertEqual(get_queue.return_value.enqueue.call_args[0], (process_video, 42))
        self.assertEqual(kwargs['job_timeout'], VIDEO_JOB_TIMEOUT)
        self.assertEqual(kwargs['on_failure'].func, mark_video_failed)

    @patch('video_app.signals.enqueue_processing')
    def test_upload_enqueues_transcoding_on_commit(self, enqueue):
        with self.captureOnCommitCallbacks(execute=True):
            video = Video.objects.create(title='m', category='Drama')
        enqueue.assert_called_once_with(video.id)

    @patch('django_rq.get_queue')
    def test_admin_reprocess_uses_the_media_queue(self, get_queue):
        video = Video.objects.create(title='m', category='Drama', status=Video.Status.FAILED)
        request = RequestFactory().post('/admin/')
        request.user = User.objects.create_superuser('a@example.com', 'a@example.com', 'pw')
        with patch.object(VideoAdmin, 'message_user'):
            VideoAdmin(Video, None).reprocess_videos(request, Video.objects.filter(pk=video.pk))
        get_queue.assert_called_with(MEDIA_QUEUE_NAME)


class WorkerCoverageTests(TestCase):
    """Both hardcoded consumers must pop from every queue that gets written to."""

    def test_dev_entrypoint_consumes_every_queue(self):
        self.assertEqual(set(dev_worker_queues()), set(settings.RQ_QUEUES))

    def test_production_workers_consume_every_queue(self):
        consumed = {queue for queues in prod_worker_queues().values() for queue in queues}
        self.assertEqual(consumed, set(settings.RQ_QUEUES))

    def test_production_isolates_media_from_the_email_queue(self):
        """A 90-minute transcode must not be able to hold up an activation mail."""
        for service, queues in prod_worker_queues().items():
            self.assertNotEqual(
                {'default', MEDIA_QUEUE_NAME},
                set(queues) & {'default', MEDIA_QUEUE_NAME},
                f'{service} consumes both queues',
            )
