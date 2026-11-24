"""Mark videos as FAILED that have been stuck in PROCESSING for too long."""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from video_app.api.utils import STUCK_PROCESSING_HOURS
from video_app.models import Video


class Command(BaseCommand):
    """Recover videos whose transcoding job died without ever reporting back.

    ``process_video`` leaves PROCESSING through its own try/except, and the RQ
    ``on_failure`` callback covers job timeouts and exceptions. Neither runs when
    the worker is killed outright (OOM kill, container restart mid-transcode):
    the row then stays PROCESSING forever, never shows up in the video list and
    is never retried. Run this periodically (cron / systemd timer) to flip those
    rows to FAILED so the admin's 'Reprocess selected videos' action can pick
    them up.

    The age is measured from ``created_at`` — the model has no timestamp for the
    last status change — so a video re-queued via the admin long after upload
    counts as old immediately. Keep the threshold at or above the job timeout,
    and re-run the reprocess action if a sweep flags one of those; nothing but
    the status column is touched.
    """

    help = 'Flag videos stuck in PROCESSING beyond the threshold as FAILED.'

    def add_arguments(self, parser):
        """Register the age threshold and the dry-run switch."""
        parser.add_argument(
            '--hours',
            type=float,
            default=STUCK_PROCESSING_HOURS,
            help=(
                'Age in hours after which PROCESSING counts as stuck '
                f'(default: {STUCK_PROCESSING_HOURS}).'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Only report what would be flagged, change nothing.',
        )

    def handle(self, *args, **options):
        """Flag (or, with --dry-run, only report) every stuck video."""
        cutoff = timezone.now() - timedelta(hours=options['hours'])
        stuck = Video.objects.filter(status=Video.Status.PROCESSING, created_at__lt=cutoff)
        titles = list(stuck.values_list('id', 'title'))
        if not titles:
            self.stdout.write('No videos stuck in PROCESSING.')
            return
        for video_id, title in titles:
            self.stdout.write(f'stuck: #{video_id} {title}')
        if options['dry_run']:
            self.stdout.write(f'Would flag {len(titles)} video(s) as FAILED (dry run).')
            return
        stuck.update(status=Video.Status.FAILED)
        self.stdout.write(f'Flagged {len(titles)} video(s) as FAILED.')
