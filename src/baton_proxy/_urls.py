"""A URL as a log line may show it."""

from __future__ import annotations

import urllib.parse


def shown_url(url: str) -> str:
    """``url``'s scheme, host and port, or ``<not shown>`` if it holds an '@' or does not parse.

    Userinfo, path, query and fragment can each hold a credential, and an unencoded
    '/', '?' or '#' in a password moves where urlsplit thinks the host starts.
    """
    if "@" in url:
        return "<not shown>"
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "<not shown>"
    if not (parts.scheme and host):
        return "<not shown>"
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" if port is None else f"{parts.scheme}://{host}:{port}"
