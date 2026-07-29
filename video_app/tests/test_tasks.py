"""Tests for the video processing task (ffmpeg/ffprobe are mocked)."""

import shutil
import subprocess
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from video_app import tasks
from video_app.models import Video

MEDIA = tempfile.mkdtemp()


def fake_subprocess_run(args, **kwargs):
    """Stand in for subprocess.run: fake ffprobe answers, no-op ffmpeg calls.

    Patched at the ``subprocess`` module itself so it covers every call site
    (generate_thumbnail's duration probe, build_renditions' frame-rate probe,
    and run_ffmpeg) regardless of which module holds a reference to the
    wrapper function that invokes it.
    """
    text_mode = kwargs.get('text', False)
    if args[0] == 'ffprobe' and 'format=duration' in args:
        stdout = '10.0\n'
    elif args[0] == 'ffprobe':
        stdout = '30/1\n'
    else:
        stdout = ''
    return subprocess.CompletedProcess(
        args, 0, stdout=stdout if text_mode else stdout.encode(), stderr='' if text_mode else b''
    )


@override_settings(MEDIA_ROOT=MEDIA)
class ProcessVideoTests(TestCase):
    """Cover process_video orchestration with ffmpeg/ffprobe stubbed out."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def create_video(self):
        """Create a video with a real (tiny) source file on disk."""
        upload = SimpleUploadedFile('movie.mp4', b'fake-bytes', content_type='video/mp4')
        return Video.objects.create(title='m', category='Drama', video_file=upload)

    @patch('subprocess.run', side_effect=fake_subprocess_run)
    def test_process_video_marks_ready_and_sets_thumbnail(self, _run):
        video = self.create_video()
        tasks.process_video(video.id)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.READY)
        self.assertEqual(video.thumbnail.name, f'thumbnails/{video.id}.jpg')

    @patch('subprocess.run')
    def test_process_video_marks_failed_on_error(self, mock_run):
        error = subprocess.CalledProcessError(1, ['ffmpeg'], output=b'', stderr=b'boom')

        def run_side_effect(args, **kwargs):
            if args[0] == 'ffmpeg':
                raise error
            return fake_subprocess_run(args, **kwargs)

        mock_run.side_effect = run_side_effect
        video = self.create_video()
        with self.assertRaises(RuntimeError):
            tasks.process_video(video.id)
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.FAILED)

    @patch('subprocess.run', side_effect=fake_subprocess_run)
    def test_process_video_invokes_a_single_pass_ffmpeg_call_per_stage(self, mock_run):
        video = self.create_video()
        tasks.process_video(video.id)
        ffmpeg_calls = [call for call in mock_run.call_args_list if call.args[0][0] == 'ffmpeg']
        # One call for the thumbnail, one single-pass call for all renditions
        # (build_renditions used to loop and invoke ffmpeg once per resolution).
        self.assertEqual(len(ffmpeg_calls), 2)
        rendition_call_args = ffmpeg_calls[1].args[0]
        self.assertIn('-var_stream_map', rendition_call_args)
        self.assertIn('-master_pl_name', rendition_call_args)
