"""Guards the contract between caddy/Caddyfile and the Python HLS constants.

In production Caddy decides, from a regexp, which paths it may read off the
media volume without asking Django anything about the file. That regexp is a
second copy of what RESOLUTION_HEIGHTS and SEGMENT_PATTERN say. Copies drift:
adding a 1440p rendition in Python would produce URLs the dev stack serves and
production 404s, and loosening the segment pattern on the Caddy side would
widen what the proxy will hand out. These tests fail when they stop agreeing.

The regexp is read out of the shipped Caddyfile, not restated here -- a test
that restates the value it is checking guards nothing.
"""

import re
from pathlib import Path
from unittest import TestCase

from video_app.api.utils import RESOLUTION_HEIGHTS
from video_app.api.views import SEGMENT_PATTERN

BACKEND_DIR = Path(__file__).resolve().parents[2]
CADDYFILE = BACKEND_DIR / 'caddy' / 'Caddyfile'
CADDYFILE_TEXT = CADDYFILE.read_text()


def hls_pattern():
    """Return the @hls path_regexp from the Caddyfile, compiled with `re`.

    Caddy uses RE2; the pattern only uses constructs RE2 and `re` agree on
    (anchors, character classes, non-capturing groups, alternation), so
    compiling it here tests the real thing rather than an approximation.
    """
    match = re.search(r'^\s*@hls path_regexp hls (\S+)\s*$', CADDYFILE_TEXT, re.MULTILINE)
    assert match, 'the @hls path_regexp matcher is gone from caddy/Caddyfile'
    return re.compile(match.group(1))


class CaddyHLSMatcherTests(TestCase):
    def setUp(self):
        self.pattern = hls_pattern()

    def test_every_python_resolution_is_offloaded(self):
        for resolution in RESOLUTION_HEIGHTS:
            with self.subTest(resolution=resolution):
                self.assertRegex(f'/api/video/7/{resolution}/index.m3u8', self.pattern)
                self.assertRegex(f'/api/video/7/{resolution}/000.ts', self.pattern)

    def test_matcher_offers_no_resolution_python_does_not_know(self):
        # The Caddyfile lists the renditions literally; pull them back out and
        # compare as sets, so an addition on either side alone fails here.
        listed = set(re.findall(r'\d+p', self.pattern.pattern))
        self.assertEqual(listed, set(RESOLUTION_HEIGHTS))

    def test_master_playlist_is_offloaded(self):
        self.assertRegex('/api/video/7/master.m3u8', self.pattern)

    def test_segment_names_agree_with_segment_pattern(self):
        for name, allowed in [
            ('000.ts', True),
            ('12345.ts', True),
            ('evil.ts', False),
            ('..%2f000.ts', False),
            ('000.ts.mp4', False),
            ('.ts', False),
        ]:
            with self.subTest(name=name):
                self.assertEqual(bool(SEGMENT_PATTERN.match(name)), allowed)
                self.assertEqual(
                    bool(self.pattern.match(f'/api/video/7/480p/{name}')),
                    allowed,
                    'Caddy and SEGMENT_PATTERN disagree about this segment name',
                )

    def test_json_video_list_is_not_swallowed(self):
        # /api/video/ must keep reaching Django; it has no on-disk counterpart.
        for path in ['/api/video/', '/api/video', '/api/video/7/', '/api/health/']:
            with self.subTest(path=path):
                self.assertIsNone(self.pattern.match(path))

    def test_traversal_shapes_do_not_match(self):
        for path in [
            '/api/video/7/480p/../../uploads/source.mp4',
            '/api/video/7/480p/..%2f..%2fuploads%2fsource.mp4',
            '/api/video/7/../../../etc/passwd',
            '/api/video/7/480p/000.ts/../../../etc/passwd',
            '/api/video/7/480p%2f000.ts',
            '/api/video/7/480p/000.ts?x=/../',
        ]:
            with self.subTest(path=path):
                self.assertIsNone(self.pattern.match(path))


class CaddyfileStructureTests(TestCase):
    def test_hls_handler_is_ordered_before_the_api_proxy(self):
        # @backend matches /api/*, which covers every HLS URL. If it wins, the
        # offload is dead and every segment goes back through Gunicorn -- with
        # no visible symptom. CI re-checks this against `caddy adapt` output.
        self.assertLess(
            CADDYFILE_TEXT.index('handle @hls {'),
            CADDYFILE_TEXT.index('handle @backend {'),
        )

    def test_inner_route_preserves_forward_auth_before_the_rewrite(self):
        # Inside a handle block Caddy sorts `uri` before `forward_auth`; the
        # inner `route` is what stops the auth hook from being told about a
        # path the client never requested.
        block = CADDYFILE_TEXT[CADDYFILE_TEXT.index('handle @hls {') :]
        block = block[: block.index('handle @backend {')]
        self.assertIn('route {', block)
        self.assertLess(block.index('forward_auth web:8000'), block.index('uri strip_prefix'))

    def test_document_root_excludes_the_raw_uploads(self):
        # /srv/media would put <media>/uploads/ -- the original, untranscoded
        # source files -- inside a public document root.
        self.assertIn('root * /srv/media/hls', CADDYFILE_TEXT)
        self.assertNotIn('root * /srv/media\n', CADDYFILE_TEXT)

    def test_auth_endpoint_matches_the_django_route(self):
        self.assertIn('uri /internal/hls-auth/', CADDYFILE_TEXT)

    def test_content_types_are_set_explicitly(self):
        # Go's MIME table has neither extension and Caddy will not sniff.
        self.assertIn('Content-Type application/vnd.apple.mpegurl', CADDYFILE_TEXT)
        self.assertIn('Content-Type video/MP2T', CADDYFILE_TEXT)

    def test_segments_are_not_cached_by_shared_caches(self):
        self.assertIn('Cache-Control "private, max-age=86400, immutable"', CADDYFILE_TEXT)
