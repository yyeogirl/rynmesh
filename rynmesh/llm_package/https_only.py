"""One HTTPS-only opener shared by every download this package performs.

Both downloads — the pinned runtime archive (`runtime_native_install`) and the
verified model (`model_download`) — must keep the *whole* redirect chain on
HTTPS, not just the first request. A single implementation lives here so a
future download cannot quietly get the default opener instead.

Nothing raised from here may contain a URL or a filesystem path: these
messages reach the owner through setup progress and node logs.
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

from .errors import LifecycleError

REDIRECT_OFF_HTTPS = "download redirected to a non-HTTPS URL"


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the whole redirect chain on HTTPS, not just the first request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise LifecycleError(REDIRECT_OFF_HTTPS)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_https_only_opener() -> urllib.request.OpenerDirector:
    """An opener that refuses to follow a redirect off HTTPS."""
    return urllib.request.build_opener(HttpsOnlyRedirect)
