"""An HTTP opener that never follows a redirect."""

from __future__ import annotations

import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# urllib re-sends Authorization to whatever host a redirect names, and turns a
# POST into a GET, so a 3xx is answered as the error it is.
no_redirect_urlopen = urllib.request.build_opener(_NoRedirect).open
