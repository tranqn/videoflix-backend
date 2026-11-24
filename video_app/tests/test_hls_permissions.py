"""One rule, two enforcement points: the streaming views and Caddy's auth hook.

Production serves HLS bytes from Caddy after a forward_auth call to Django; the
dev stack has no Caddy and serves them from the views below. Both must apply the
same rule, or a future entitlement check lands on one path only -- and the path
it misses is the one that actually ships the bytes.
"""

from unittest import TestCase

from core.views import HLSAuthView
from video_app.api.permissions import HLS_STREAM_PERMISSIONS, CanStreamHLS
from video_app.api.views import (
    HLSManifestView,
    HLSMasterView,
    HLSSegmentView,
    VideoListView,
)

STREAMING_VIEWS = [HLSMasterView, HLSManifestView, HLSSegmentView, HLSAuthView]


class SharedStreamingPermissionTests(TestCase):
    def test_every_streaming_entry_point_uses_the_same_object(self):
        for view in STREAMING_VIEWS:
            with self.subTest(view=view.__name__):
                # `is`, not `==`: a second list with the same contents is
                # exactly the copy this is meant to prevent.
                self.assertIs(view.permission_classes, HLS_STREAM_PERMISSIONS)

    def test_the_rule_is_a_named_class_not_bare_is_authenticated(self):
        # Tightening streaming access must not require touching every endpoint
        # that merely wants a logged-in user.
        self.assertEqual(HLS_STREAM_PERMISSIONS, [CanStreamHLS])

    def test_the_json_dashboard_is_not_governed_by_the_streaming_rule(self):
        # /api/video/ is not offloaded and is not a streaming entry point.
        self.assertIsNot(VideoListView.permission_classes, HLS_STREAM_PERMISSIONS)
