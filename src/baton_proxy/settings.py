"""Capture settings read from the Console (SPEC §2.6): fetch, match, apply.

The proxy reads the same document the SDKs read, with the same rules. A caller
here has a client name and nothing else, so ``label`` and ``share`` audiences
never match it, which is the spec's rule for a caller with no principal.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from baton_proxy._no_redirect import no_redirect_urlopen
from baton_proxy.config import INTENT_PARAM_MODES, REPORT_MODES
from baton_proxy.overlay import Overlay, OverlayKey, document_overlay

logger = logging.getLogger("baton_proxy")

SETTINGS_PATH = "/v0/settings"
TENANT_HEADER = "X-Baton-Tenant-Id"
DEFAULT_REFRESH_S = 60.0
MIN_REFRESH_S = 15.0
REQUEST_TIMEOUT_S = 10.0
# A settings document is small; a larger answer is not one.
MAX_DOCUMENT_BYTES = 1024 * 1024

_MATCH_KEYS = frozenset({"label", "client", "share"})
RESULT_CAPTURE_MODES = frozenset({"full", "off"})
# SPEC §11.4.4: the error legs whose messages survive withholding.
RESULTS_OFF_WARNING = (
    "baton-proxy: the Console withholds results for some callers. The messages of "
    "tool_list_error, resource_read_error, resource_list_error, prompt_get_error "
    "and prompt_list_error are still sent"
)


def share_bucket(vendor_id: str, key: str) -> int:
    digest = hashlib.sha256(f"{vendor_id}\n{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 10000


@dataclass(frozen=True)
class Caller:
    """What a ``match`` rule can see about one session."""

    labels: tuple[str, ...] = ()
    client_name: str | None = None
    share_key: str | None = None


@dataclass(frozen=True)
class CallSettings:
    """The settings one session's listing or call runs under."""

    # The event's ``capture`` member (SPEC §11.4), unset members left out.
    capture: Mapping[str, Any]
    # ``None`` in any of these means the code value.
    result_capture_mode: str | None = None
    intent_param_mode: str | None = None
    report_mode: str | None = None
    overlay: Overlay | None = None


@dataclass(frozen=True)
class Audience:
    settings: CallSettings
    labels: frozenset[str] | None = None
    clients: frozenset[str] | None = None
    share_threshold: int | None = None

    @property
    def matches_everyone(self) -> bool:
        return self.labels is None and self.clients is None and self.share_threshold is None

    def matches(self, caller: Caller, bucket: int | None) -> bool:
        if self.labels is not None and self.labels.isdisjoint(caller.labels):
            return False
        if self.clients is not None and caller.client_name not in self.clients:
            return False
        if self.share_threshold is not None:
            if bucket is None or bucket >= self.share_threshold:
                return False
        return True


@dataclass(frozen=True)
class SettingsDocument:
    version: int
    audiences: tuple[Audience, ...]
    unmatched: CallSettings
    server_overlay: Overlay | None = None
    # Every overlay this document holds, for the next refresh to reuse.
    overlays: Mapping[OverlayKey, Overlay] = field(default_factory=dict)

    @functools.cached_property
    def uses_share(self) -> bool:
        return any(a.share_threshold is not None for a in self.audiences)

    def settings_for(self, caller: Caller, vendor_id: str) -> CallSettings:
        bucket = (
            share_bucket(vendor_id, caller.share_key)
            if self.uses_share and caller.share_key is not None
            else None
        )
        for audience in self.audiences:
            if audience.matches(caller, bucket):
                return audience.settings
        return self.unmatched

    @functools.cached_property
    def _reachable(self) -> tuple[Audience, ...]:
        """The audiences a proxy caller can match: none by label or share."""
        return tuple(a for a in self.audiences if a.labels is None and a.share_threshold is None)

    @functools.cached_property
    def hidden_somewhere(self) -> frozenset[str]:
        return frozenset(
            name
            for a in self._reachable
            if a.settings.overlay is not None
            for name in a.settings.overlay.hidden_tools
        )

    @functools.cached_property
    def withholds_results(self) -> bool:
        return any(a.settings.result_capture_mode == "off" for a in self._reachable)

    def reports_on_for_anyone(self, code_value: str) -> bool:
        """Whether some caller can get reports, so the instructions point to the tool."""
        reachable = [a.settings for a in self._reachable]
        if not any(a.matches_everyone for a in self._reachable):
            reachable.append(self.unmatched)
        return any((s.report_mode or code_value) == "on" for s in reachable)


def _capture(
    version: int, audience: str | None = None, overlay: str | None = None
) -> Mapping[str, Any]:
    captured: dict[str, Any] = {"settings_version": version}
    if audience is not None:
        captured["audience"] = audience
    if overlay is not None:
        captured["overlay"] = overlay
    # Every call an audience serves shares this one value.
    return MappingProxyType(captured)


def _string_set(value: Any) -> frozenset[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return frozenset(value)


def _known(value: Any, values: frozenset[str]) -> bool:
    # A str check first: an unhashable value must not raise here.
    return isinstance(value, str) and value in values


def _audience(
    version: int,
    audience_id: str,
    match: dict[str, Any],
    capture: dict[str, Any],
    overlay: Overlay | None,
) -> Audience | None:
    """``None`` for an audience that can match nobody (SPEC §2.6)."""
    if not set(match) <= _MATCH_KEYS:
        return None
    labels = clients = threshold = None
    if "label" in match and (labels := _string_set(match["label"])) is None:
        return None
    if "client" in match and (clients := _string_set(match["client"])) is None:
        return None
    if "share" in match:
        share = match["share"]
        # The range test alone also refuses NaN, inf and integers too big for a float.
        if isinstance(share, bool) or not isinstance(share, int | float) or not 0 < share <= 1:
            return None
        # Half up, not round(): SPEC §2.6, pinned by test_a_half_rounds_up.
        threshold = math.floor(share * 10000 + 0.5)
    results, goal, reports = (capture.get(k) for k in ("results", "goal", "reports"))
    settings = CallSettings(
        _capture(version, audience_id, overlay.id if overlay is not None else None),
        result_capture_mode=results if _known(results, RESULT_CAPTURE_MODES) else None,
        intent_param_mode=goal if _known(goal, INTENT_PARAM_MODES) else None,
        report_mode=reports if _known(reports, REPORT_MODES) else None,
        overlay=overlay,
    )
    return Audience(settings, labels, clients, threshold)


def parse_document(
    body: bytes, previous: SettingsDocument | None = None
) -> SettingsDocument | None:
    """The document in ``body``, or ``None`` when it is not a valid one (SPEC §2.6).
    An overlay ``previous`` held unchanged is kept as the same object."""
    try:
        return _parse_document(json.loads(body), previous)
    except (ValueError, RecursionError):
        return None


def _parse_document(data: Any, previous: SettingsDocument | None) -> SettingsDocument | None:
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    audiences = data.get("audiences")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    if not isinstance(audiences, list) or not audiences:
        return None
    overlays = data.get("overlays")
    if not isinstance(overlays, dict):
        overlays = {}
    reuse = previous.overlays if previous is not None else {}
    held: dict[OverlayKey, Overlay] = {}
    parsed: list[Audience] = []
    for audience in audiences:
        if not isinstance(audience, dict):
            return None
        audience_id, match, capture = (audience.get(k) for k in ("id", "match", "capture"))
        if not isinstance(audience_id, str):
            return None
        if not isinstance(match, dict) or not isinstance(capture, dict):
            return None
        overlay = document_overlay(overlays, audience.get("overlay"), "audience", reuse, held)
        readable = _audience(version, audience_id, match, capture, overlay)
        if readable is not None:
            parsed.append(readable)
    server_overlay = document_overlay(overlays, data.get("server_overlay"), "server", reuse, held)
    return SettingsDocument(
        version,
        tuple(parsed),
        unmatched=CallSettings(_capture(version)),
        server_overlay=server_overlay,
        overlays=held,
    )


class HeldSettings(Protocol):
    """Where a session reads the document held now."""

    vendor_id: str

    @property
    def document(self) -> SettingsDocument | None: ...


@dataclass(frozen=True)
class _Answer:
    status: int
    etag: str | None = None
    body: bytes = b""


def _get(url: str, headers: dict[str, str], timeout_s: float) -> _Answer:
    """One GET. urllib's timeout is per socket operation, so the body is also
    bounded by ``timeout_s`` as a whole, against a server that trickles it."""
    deadline = time.monotonic() + timeout_s
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with no_redirect_urlopen(req, timeout=timeout_s) as resp:
            chunks: list[bytes] = []
            size = 0
            while chunk := resp.read1(65536):
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise ValueError("the settings answer is larger than a document can be")
                if time.monotonic() > deadline:
                    raise TimeoutError("the settings answer took longer than its timeout")
                chunks.append(chunk)
            return _Answer(resp.status, resp.headers.get("ETag"), b"".join(chunks))
    except urllib.error.HTTPError as exc:
        # urllib raises for every non-2xx, the 304 the protocol expects among them.
        return _Answer(exc.code)


class SettingsFetcher:
    """Reads the document on a background thread and holds the last good one."""

    def __init__(
        self,
        *,
        origin: str,
        api_key: str,
        tenant_id: str,
        vendor_id: str,
        refresh_s: float = DEFAULT_REFRESH_S,
        get: Callable[[str, dict[str, str], float], _Answer] = _get,
    ) -> None:
        self.vendor_id = vendor_id
        query = urllib.parse.urlencode({"server": vendor_id})
        self._url = f"{origin.rstrip('/')}{SETTINGS_PATH}?{query}"
        self._headers = {"Authorization": f"Bearer {api_key}", TENANT_HEADER: tenant_id}
        if not math.isfinite(refresh_s):
            refresh_s = DEFAULT_REFRESH_S
        self._refresh_s = max(refresh_s, MIN_REFRESH_S)
        self._get = get
        self._etag: str | None = None
        self._document: SettingsDocument | None = None
        # The last 200's body, so an unchanged answer is not parsed again.
        self._body_digest: bytes | None = None
        self._stop = threading.Event()
        self._warned = False

    @property
    def document(self) -> SettingsDocument | None:
        return self._document

    def start(self) -> None:
        threading.Thread(target=self._run, name="baton-settings", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.read_once()
            self._stop.wait(self._refresh_s)

    def read_once(self) -> None:
        """One request. Never raises: a failure keeps what is held."""
        try:
            self._read()
        except Exception:
            logger.debug("baton-proxy: reading capture settings failed", exc_info=True)

    def _read(self) -> None:
        headers = dict(self._headers)
        if self._etag is not None:
            headers["If-None-Match"] = self._etag
        answer = self._get(self._url, headers, REQUEST_TIMEOUT_S)
        if answer.status == 204:
            self._document, self._etag, self._body_digest = None, None, None
        elif answer.status == 200:
            digest = hashlib.sha256(answer.body).digest()
            if digest == self._body_digest:
                self._etag = answer.etag or self._etag
                return
            document = parse_document(answer.body, self._document)
            self._body_digest = digest
            if document is None:
                if not self._warned:
                    self._warned = True
                    logger.warning(
                        "baton-proxy: the Console sent capture settings this proxy cannot read"
                    )
                return
            # Once each time results go off (SPEC §11.4.4).
            was_off = self._document is not None and self._document.withholds_results
            if document.withholds_results and not was_off:
                logger.warning(RESULTS_OFF_WARNING)
            self._document = document
            self._etag = answer.etag
