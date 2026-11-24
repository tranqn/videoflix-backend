"""Job lifecycle: nothing may stay in PROCESSING forever, no job may crash on a deleted row."""

import datetime
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from video_app import tasks
from video_app.api.utils import hls_root
from video_app.models import Video

# A Path, not the str mkdtemp() returns: core/urls.py does MEDIA_ROOT / '...'
# at import time and blows up if the setting is a plain string.
MEDIA = Path(tempfile.mkdtemp())


def fake_job(*args):
    """Minimal stand-in for an rq Job, which is all the callback touches."""
    return SimpleNamespace(args=args)


@override_settings(MEDIA_ROOT=MEDIA)
class FailureCallbackTests(TestCase):
    """The on_failure callback records FAILED when process_video cannot."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def make_video(self, status=Video.Status.PROCESSING):
        return Video.objects.create(title='m', category='Drama', status=status)

    def test_callback_marks_a_processing_video_as_failed(self):
        video = self.make_video()
        tasks.mark_video_failed(fake_job(video.id), None, OSError, OSError('boom'), None)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.FAILED)

    def test_callback_leaves_a_ready_video_alone(self):
        """A late failure callback must not undo a successful later run."""
        video = self.make_video(status=Video.Status.READY)
        tasks.mark_video_failed(fake_job(video.id), None, OSError, OSError('boom'), None)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.READY)

    def test_callback_survives_a_deleted_video(self):
        video = self.make_video()
        video_id = video.id
        video.delete()
        tasks.mark_video_failed(fake_job(video_id), None, OSError, OSError('boom'), None)

    def test_callback_without_arguments_is_a_noop(self):
        tasks.mark_video_failed(fake_job(), None, OSError, OSError('boom'), None)

    def test_process_video_ignores_a_video_deleted_before_pickup(self):
        """Deleting mid-flight must not raise DoesNotExist out of the worker."""
        video = self.make_video()
        video_id = video.id
        leftover = hls_root(video_id) / '480p'
        leftover.mkdir(parents=True, exist_ok=True)
        video.delete()
        tasks.process_video(video_id)
        self.assertFalse(hls_root(video_id).exists())

    def test_set_status_on_a_deleted_row_is_a_noop(self):
        video = self.make_video()
        Video.objects.filter(pk=video.pk).delete()
        tasks.set_status(video, Video.Status.FAILED)


class SweepStuckVideosTests(TestCase):
    """The sweeper recovers rows whose worker died without reporting back."""

    def make_video(self, status, age_hours):
        video = Video.objects.create(title=f'{status}-{age_hours}', category='Drama', status=status)
        Video.objects.filter(pk=video.pk).update(
            created_at=timezone.now() - datetime.timedelta(hours=age_hours)
        )
        return video

    def sweep(self, *arguments):
        out = StringIO()
        call_command('sweep_stuck_videos', *arguments, stdout=out)
        return out.getvalue()

    def test_flags_a_video_stuck_beyond_the_threshold(self):
        video = self.make_video(Video.Status.PROCESSING, age_hours=30)
        output = self.sweep()
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.FAILED)
        self.assertIn(f'#{video.id}', output)

    def test_leaves_a_recent_processing_video_alone(self):
        video = self.make_video(Video.Status.PROCESSING, age_hours=1)
        self.sweep()
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.PROCESSING)

    def test_leaves_other_statuses_alone(self):
        for status in (Video.Status.PENDING, Video.Status.READY, Video.Status.FAILED):
            video = self.make_video(status, age_hours=30)
            self.sweep()
            video.refresh_from_db()
            self.assertEqual(video.status, status)

    def test_threshold_is_configurable(self):
        video = self.make_video(Video.Status.PROCESSING, age_hours=3)
        self.sweep('--hours', '2')
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.FAILED)

    def test_dry_run_reports_without_changing_anything(self):
        video = self.make_video(Video.Status.PROCESSING, age_hours=30)
        output = self.sweep('--dry-run')
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.PROCESSING)
        self.assertIn('Would flag 1', output)

    def test_reports_when_nothing_is_stuck(self):
        self.assertIn('No videos stuck', self.sweep())
