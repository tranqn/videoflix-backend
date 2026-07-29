"""The single rule that decides who may read HLS bytes.

Two components now answer that question for the same URLs:

* Caddy, which authorizes the request with a ``forward_auth`` subrequest to
  ``/internal/hls-auth/`` and then serves the file itself (production), and
* the Django views in ``video_app.api.views`` (the dev stack has no Caddy, and
  they stay the fallback if the offload is ever turned off).

Two code paths deciding the same thing is exactly how an entitlement check ends
up enforced on one of them only. So the rule lives here, once, and both paths
import it -- ``video_app.api.views`` for the direct path and ``core.views``
for the proxy hook. Tightening this list (per-video entitlement, subscription
state, ...) must therefore change both paths at the same time, which is the
whole point.
"""

from rest_framework.permissions import IsAuthenticated


class CanStreamHLS(IsAuthenticated):
    """Allow only callers that may read HLS playlists and segments.

    Today this is plain "is authenticated" -- the same rule the streaming views
    have always enforced. It is a named subclass rather than ``IsAuthenticated``
    itself so the streaming rule can be tightened in one place without touching
    every unrelated endpoint that also happens to require a logged-in user.
    """


# Import this, not the class, so the two call sites cannot drift apart by one
# of them adding a second permission to its own list.
HLS_STREAM_PERMISSIONS = [CanStreamHLS]
