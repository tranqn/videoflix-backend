"""Tests for the video processing task (ffmpeg is mocked)."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from video_app import tasks
from video_app.models import Video

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class ProcessVideoTests(TestCase):
    """Cover process_video orchestration with ffmpeg stubbed out."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def create_video(self):
        """Create a video with a real (tiny) source file on disk."""
        upload = SimpleUploadedFile('movie.mp4', b'fake-bytes', content_type='video/mp4')
        return Video.objects.create(title='m', category='Drama', video_file=upload)

    @patch('video_app.api.utils.run_ffmpeg')
    def test_process_video_marks_ready_and_sets_thumbnail(self, _ffmpeg):
        video = self.create_video()
        tasks.process_video(video.id)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.READY)
        self.assertEqual(video.thumbnail.name, f'thumbnails/{video.id}.jpg')

    @patch('video_app.api.utils.run_ffmpeg', side_effect=OSError('ffmpeg failed'))
    def test_process_video_marks_failed_on_error(self, _ffmpeg):
        video = self.create_video()
        with self.assertRaises(OSError):
            tasks.process_video(video.id)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.FAILED)

    @patch('video_app.api.utils.run_ffmpeg')
    def test_process_video_writes_master_playlist(self, _ffmpeg):
        video = self.create_video()
        tasks.process_video(video.id)
        master = Path(MEDIA) / 'hls' / str(video.id) / 'master.m3u8'
        self.assertTrue(master.is_file())
        content = master.read_text()
        self.assertIn('#EXT-X-STREAM-INF', content)
        self.assertIn('480p/index.m3u8', content)
