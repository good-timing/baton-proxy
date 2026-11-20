"""Capture settings from the Console (SPEC §2.6), ported by name from
baton-sdk's ``test_capture_settings.py`` and ``test_overlays.py``."""

from __future__ import annotations

import http.server
import itertools
import json
import logging
import threading
import time
from typing import Any

import pytest

from baton_proxy import overlay as overlay_module
from baton_proxy.settings import (
    DEFAULT_REFRESH_S,
    Caller,
    SettingsDocument,
    SettingsFetcher,
    _Answer,
    _get,
    parse_document,
    share_bucket,
)


@pytest.fixture(autouse=True)
def _fresh_overlay_memory() -> None:
    """The warning dedup is per process."""
    overlay_module._warned.clear()


CONSOLE_SAMPLE = {
    "version": 9,
    "audiences": [
        {
            "id": "beta",
            "match": {"label": ["beta"], "share": 0.25},
            "capture": {"goal": "required", "reports": "on", "results": "full"},
            "overlay": "ov_beta_1",
        },
        {
            "id": "everyone",
            "match": {},
            "capture": {"goal": "required", "reports": "on", "results": "off"},
        },
    ],
    "overlays": {
        "ov_beta_1": {
            "actions": [
                {
                    "target": {"tool": "book_slot"},
                    "replace": "Books one slot. Check availability first.",
                },
                {"target": {"tool": "find_slots"}, "append": "Returns at most 20 slots."},
                {"target": {"tool": "legacy_book"}, "remove": True},
                {
                    "target": {"tool": "book_slot", "param": "timezone"},
                    "append": "Use an IANA name, such as Europe/Paris.",
                },
                {"target": {"tool": "book_slot", "param": "notes"}, "remove": True},
                {"target": {"tool": "find_slots", "param": "duration"}, "required": True},
                {
                    "target": {"tool": "find_slots", "param": "day"},
                    "replace": "The day, as YYYY-MM-DD.",
                },
                {
                    "target": {"agent_text": "goal_param.description"},
                    "replace": "What the user wants, in their words.",
                },
                {
                    "target": {"agent_text": "report_tool.description"},
                    "append": "Use it when a booking fails twice.",
                },
                {
                    "target": {"agent_text": "report_tool.refusal"},
                    "replace": "Reports are off for now.",
                },
            ]
        },
        "ov_server_1": {
            "actions": [
                {
                    "target": {"instructions": True},
                    "append": "Times are in the user's timezone.",
                },
                {
                    "target": {"agent_text": "instructions.report_pointer"},
                    "replace": "If a booking keeps failing, say so with the report tool.",
                },
                {
                    "target": {"agent_text": "instructions.subagent"},
                    "append": "Pass the user's timezone to subagents.",
                },
            ]
        },
    },
    "server_overlay": "ov_server_1",
}


def _document(*audiences: dict[str, Any], version: int = 3) -> bytes:
    return json.dumps({"version": version, "audiences": list(audiences)}).encode()


def _audience(audience_id: str, match: dict[str, Any], results: Any = "full") -> dict[str, Any]:
    return {
        "id": audience_id,
        "match": match,
        "capture": {"goal": "required", "reports": "off", "results": results},
    }


EVERYONE = _audience("everyone", {}, results="off")


def _parsed(body: bytes) -> SettingsDocument:
    document = parse_document(body)
    assert document is not None
    return document


def _matched(body: bytes, caller: Caller) -> str | None:
    return _parsed(body).settings_for(caller, "srv").capture.get("audience")


def _sample(**changes: Any) -> dict[str, Any]:
    document = json.loads(json.dumps(CONSOLE_SAMPLE))
    document["audiences"][0]["match"] = {"client": ["claude-code"]}
    document.update(changes)
    return document


class TestShare:
    def test_the_bucket_is_pinned(self) -> None:
        # SPEC §2.6's formula; every producer must give the same buckets.
        assert [share_bucket("acme", key) for key in ("alice", "bob", "carol")] == [
            1556,
            3902,
            7404,
        ]

    def test_a_half_rounds_up(self) -> None:
        # 0.00025 * 10000 is exactly 2.5, so buckets 0, 1 and 2 are in.
        key = next(k for i in itertools.count() if share_bucket("srv", k := f"u{i}") == 2)
        body = _document(_audience("tiny", {"share": 0.00025}), EVERYONE)
        assert _matched(body, Caller(share_key=key)) == "tiny"

    def test_raising_the_share_keeps_everyone_already_in(self) -> None:
        small = _document(_audience("in", {"share": 0.1}), EVERYONE)
        large = _document(_audience("in", {"share": 0.5}), EVERYONE)
        keys = [f"user-{i}" for i in range(500)]
        in_small = {k for k in keys if _matched(small, Caller(share_key=k)) == "in"}
        in_large = {k for k in keys if _matched(large, Caller(share_key=k)) == "in"}
        assert 25 < len(in_small) < 75
        assert in_small < in_large

    def test_share_needs_an_identity(self) -> None:
        body = _document(_audience("all", {"share": 1}), EVERYONE)
        assert _matched(body, Caller()) == "everyone"
        assert _matched(body, Caller(share_key="user-1")) == "all"


class TestDocument:
    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b"[" * 100000,
            _document(version=0),
            json.dumps({"version": True, "audiences": [EVERYONE]}).encode(),
            json.dumps({"version": 1, "audiences": []}).encode(),
            json.dumps(
                {"version": 1, "audiences": [{"id": 1, "match": {}, "capture": {}}]}
            ).encode(),
            json.dumps(
                {"version": 1, "audiences": [{"id": "a", "match": [], "capture": {}}]}
            ).encode(),
        ],
    )
    def test_an_invalid_document_is_refused(self, body: bytes) -> None:
        assert parse_document(body) is None

    def test_the_first_matching_audience_wins(self) -> None:
        body = _document(
            _audience("beta", {"label": ["beta", "alpha"]}),
            _audience("claude", {"client": ["claude-code"]}),
            EVERYONE,
        )
        assert _matched(body, Caller(labels=("alpha",), client_name="claude-code")) == "beta"
        assert _matched(body, Caller(client_name="claude-code")) == "claude"
        assert _matched(body, Caller(labels=("gamma",))) == "everyone"

    def test_match_keys_combine_with_and(self) -> None:
        body = _document(_audience("both", {"label": ["beta"], "client": ["cursor"]}), EVERYONE)
        assert _matched(body, Caller(labels=("beta",))) == "everyone"
        assert _matched(body, Caller(labels=("beta",), client_name="cursor")) == "both"

    @pytest.mark.parametrize(
        "match",
        [
            {"organization": ["org_1"]},
            {"label": "beta"},
            {"label": [1]},
            {"client": "cursor"},
            {"share": "0.5"},
            {"share": True},
            {"share": 5},
            {"share": -1},
            {"share": 1e305},
            {"share": float("inf")},
            {"share": float("nan")},
            {"share": 10**400},
        ],
    )
    def test_an_audience_it_cannot_read_matches_nobody(self, match: dict[str, Any]) -> None:
        body = _document(_audience("odd", match), EVERYONE)
        assert _matched(body, Caller(labels=("beta",), client_name="cursor", share_key="u")) == (
            "everyone"
        )

    def test_the_matched_audience_sets_results_and_is_recorded(self) -> None:
        document = _parsed(_document(_audience("beta", {"label": ["beta"]}), EVERYONE, version=7))
        beta = document.settings_for(Caller(labels=("beta",)), "srv")
        assert beta.result_capture_mode == "full"
        assert beta.capture == {"settings_version": 7, "audience": "beta"}
        rest = document.settings_for(Caller(), "srv")
        assert rest.result_capture_mode == "off"
        assert rest.capture == {"settings_version": 7, "audience": "everyone"}

    @pytest.mark.parametrize("results", ["metadata", ["full"], {"a": 1}, None])
    def test_an_unknown_results_value_leaves_the_code_value(self, results: Any) -> None:
        document = _parsed(_document(_audience("everyone", {}, results=results)))
        assert document.settings_for(Caller(), "srv").result_capture_mode is None

    def test_no_matching_audience_records_the_version_alone(self) -> None:
        document = _parsed(_document(_audience("beta", {"label": ["beta"]})))
        settings = document.settings_for(Caller(), "srv")
        assert settings.result_capture_mode is None
        assert settings.capture == {"settings_version": 3}


class TestReadingOverlays:
    def test_the_console_sample_reads_whole(self) -> None:
        document = parse_document(json.dumps(CONSOLE_SAMPLE).encode())
        assert document is not None
        beta = document.audiences[0].settings
        assert beta.capture["overlay"] == "ov_beta_1"
        assert beta.overlay is not None
        assert len(beta.overlay.actions) == 10
        assert beta.overlay.hidden_tools == {"legacy_book"}
        assert document.server_overlay is not None
        assert len(document.server_overlay.actions) == 3
        assert document.audiences[1].settings.overlay is None

    @pytest.mark.parametrize("overlays", [{}, None, "not an object"])
    def test_an_unknown_overlay_leaves_the_document_valid(self, overlays: Any) -> None:
        document = parse_document(json.dumps(_sample(overlays=overlays)).encode())
        assert document is not None
        assert document.audiences[0].settings.overlay is None
        assert "overlay" not in document.audiences[0].settings.capture
        assert document.server_overlay is None

    def test_overlays_absent_reads_as_none(self) -> None:
        body = _sample()
        del body["overlays"], body["server_overlay"], body["audiences"][0]["overlay"]
        document = parse_document(json.dumps(body).encode())
        assert document is not None
        assert document.server_overlay is None
        assert "overlay" not in document.audiences[0].settings.capture

    def test_an_action_in_the_wrong_place_is_skipped_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        body = _sample()
        body["overlays"]["ov_beta_1"]["actions"][:0] = [
            {"target": {"instructions": True}, "append": "server only"},
            {"target": {"agent_text": "nope"}, "append": "x"},
        ]
        body["overlays"]["ov_beta_1"]["actions"].append({"target": {"tool": "x"}, "remove": 1})
        with caplog.at_level(logging.WARNING, logger="baton_proxy"):
            document = parse_document(json.dumps(body).encode())
        assert document is not None
        overlay = document.audiences[0].settings.overlay
        assert overlay is not None and len(overlay.actions) == 10
        skipped = [r.getMessage() for r in caplog.records if "is skipped" in r.getMessage()]
        assert any("goes only in server_overlay" in m for m in skipped)
        assert any("unknown agent_text 'nope'" in m for m in skipped)
        assert any("must be true" in m for m in skipped)

    def test_an_empty_name_is_kept_and_a_non_text_piece_is_an_unknown_target(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        body = _sample()
        body["overlays"]["ov_beta_1"]["actions"] = [
            {"target": {"tool": ""}, "append": "x"},
            {"target": {"agent_text": 1}, "append": "x"},
        ]
        with caplog.at_level(logging.WARNING, logger="baton_proxy"):
            document = parse_document(json.dumps(body).encode())
        assert document is not None
        overlay = document.audiences[0].settings.overlay
        assert overlay is not None and [a.tool for a in overlay.actions] == [""]
        skipped = [r.getMessage() for r in caplog.records if "is skipped" in r.getMessage()]
        assert len(skipped) == 1 and "unknown target" in skipped[0]

    def test_an_id_that_cannot_be_a_header_is_shown_as_its_hash(self) -> None:
        body = _sample()
        body["overlays"]["ov_1\r\nSet-Cookie: x"] = body["overlays"].pop("ov_beta_1")
        body["audiences"][0]["overlay"] = "ov_1\r\nSet-Cookie: x"
        document = parse_document(json.dumps(body).encode())
        assert document is not None
        overlay = document.audiences[0].settings.overlay
        assert overlay is not None and overlay.label == overlay.hash[:15]
        assert document.audiences[0].settings.capture["overlay"] == "ov_1\r\nSet-Cookie: x"
        assert _parsed(json.dumps(_sample()).encode()).audiences[0].settings.overlay.label == (
            "ov_beta_1"
        )

    def test_a_refreshed_document_keeps_what_its_overlay_learned(self) -> None:
        body = _sample()
        body["overlays"]["ov_beta_1"]["actions"] = [
            {"target": {"tool": "book_slot", "param": "day"}, "remove": True}
        ]
        held = _parsed(json.dumps(body).encode())
        first = held.audiences[0].settings.overlay
        assert first is not None
        tool = {
            "name": "book_slot",
            "inputSchema": {"properties": {"day": {}}, "required": ["day"]},
        }
        first.apply_to_tools([tool])
        assert first.hidden_params("book_slot") == frozenset()
        body["version"] = 10
        refreshed = parse_document(json.dumps(body).encode(), held)
        assert refreshed is not None
        again = refreshed.audiences[0].settings.overlay
        assert again is first
        assert again.hidden_params("book_slot") == frozenset()


def _fetcher(*answers: _Answer | Exception) -> tuple[SettingsFetcher, list[dict[str, str]]]:
    queue = list(answers)
    sent: list[dict[str, str]] = []

    def get(url: str, headers: dict[str, str], timeout_s: float) -> _Answer:
        sent.append(headers)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    fetcher = SettingsFetcher(
        origin="https://h.example.com",
        api_key="baton_pk_x",
        tenant_id="ten_1",
        vendor_id="srv",
        get=get,
    )
    return fetcher, sent


def test_audiences_a_proxy_caller_cannot_match_do_not_count() -> None:
    body = _sample()
    body["audiences"] = [
        {
            "id": "beta",
            "match": {"label": ["beta"]},
            "capture": {"reports": "on"},
            "overlay": "ov_beta_1",
        },
        {"id": "some", "match": {"share": 0.5}, "capture": {"reports": "on"}},
        {"id": "everyone", "match": {}, "capture": {"reports": "off"}},
    ]
    document = _parsed(json.dumps(body).encode())
    assert not document.reports_on_for_anyone("off")
    assert document.hidden_somewhere == frozenset()


def test_an_overlay_too_deep_to_hash_makes_the_document_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_deep(_: Any) -> str:
        raise RecursionError

    monkeypatch.setattr(overlay_module, "_digest", too_deep)
    assert parse_document(json.dumps(_sample()).encode()) is None


class TestFetcher:
    def test_a_repeated_answer_still_takes_its_new_etag(self) -> None:
        body = _document(EVERYONE)
        fetcher, sent = _fetcher(_Answer(200, None, body), _Answer(200, '"v3"', body), _Answer(304))
        fetcher.read_once()
        fetcher.read_once()
        fetcher.read_once()
        assert sent[2]["If-None-Match"] == '"v3"'

    def test_an_unchanged_answer_is_not_parsed_again(self) -> None:
        body = _document(EVERYONE)
        fetcher, _ = _fetcher(_Answer(200, body=body), _Answer(200, body=body))
        fetcher.read_once()
        first = fetcher.document
        fetcher.read_once()
        assert fetcher.document is first

    def test_a_200_is_used_and_its_etag_sent_back(self) -> None:
        fetcher, sent = _fetcher(_Answer(200, '"v3-w1"', _document(EVERYONE)), _Answer(304))
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is not None and fetcher.document.version == 3
        assert sent[1]["If-None-Match"] == '"v3-w1"'

    def test_a_204_drops_the_document(self) -> None:
        fetcher, _ = _fetcher(_Answer(200, body=_document(EVERYONE)), _Answer(204))
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is None

    @pytest.mark.parametrize(
        "failure",
        [_Answer(500), _Answer(401), _Answer(200, body=b"{}"), OSError("down")],
    )
    def test_a_failure_keeps_the_document_held(self, failure: Any) -> None:
        fetcher, _ = _fetcher(_Answer(200, body=_document(EVERYONE)), failure)
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is not None


@pytest.mark.parametrize("refresh_s", [float("nan"), float("inf"), float("-inf")])
def test_a_refresh_that_is_not_a_number_uses_the_default(refresh_s: float) -> None:
    fetcher = SettingsFetcher(
        origin="https://h.example.com",
        api_key="k",
        tenant_id="t",
        vendor_id="srv",
        refresh_s=refresh_s,
    )
    assert fetcher._refresh_s == DEFAULT_REFRESH_S


def _serve(handler: type) -> http.server.HTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_server_that_trickles_bytes_is_cut_off_at_the_timeout() -> None:
    class Trickle(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.1)

        def log_message(self, *args: Any) -> None:
            pass

    server = _serve(Trickle)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            _get(f"http://127.0.0.1:{server.server_port}/v0/settings", {}, 0.5)
        assert time.monotonic() - started < 2
    finally:
        server.shutdown()


def test_a_real_request_reads_the_document_and_its_etag() -> None:
    seen: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({"path": self.path, **dict(self.headers.items())})
            if self.headers.get("If-None-Match") == '"v3-w1"':
                self.send_response(304)
                self.end_headers()
                return
            body = _document(EVERYONE)
            self.send_response(200)
            self.send_header("ETag", '"v3-w1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fetcher = SettingsFetcher(
            origin=f"http://127.0.0.1:{server.server_port}",
            api_key="baton_pk_x",
            tenant_id="ten_1",
            vendor_id="srv",
        )
        fetcher.read_once()
        fetcher.read_once()
    finally:
        server.shutdown()
    assert fetcher.document is not None and fetcher.document.version == 3
    assert [r["path"] for r in seen] == ["/v0/settings?server=srv"] * 2
    assert seen[0]["Authorization"] == "Bearer baton_pk_x"
    assert seen[0]["X-Baton-Tenant-Id"] == "ten_1"
    assert seen[1]["If-None-Match"] == '"v3-w1"'


def test_startup_waits_for_the_first_read() -> None:
    import time as _time

    def slow(*_: Any) -> _Answer:
        _time.sleep(0.2)
        return _Answer(200, "e", json.dumps(CONSOLE_SAMPLE).encode())

    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=slow
    )
    fetcher.start(wait_s=5)
    try:
        assert fetcher.document is not None
    finally:
        fetcher.stop()


def test_a_first_read_that_does_not_come_is_warned_and_not_waited_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import threading as _threading

    gate = _threading.Event()

    def stuck(*_: Any) -> _Answer:
        gate.wait(5)
        return _Answer(204)

    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=stuck
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        fetcher.start(wait_s=0.1)
    gate.set()
    fetcher.stop()
    assert "did not arrive" in caplog.text


def test_a_refused_read_is_logged_once_per_status(caplog: pytest.LogCaptureFixture) -> None:
    answers = iter([_Answer(401), _Answer(401), _Answer(404)])
    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=lambda *_: next(answers)
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        for _ in range(3):
            fetcher.read_once()
    assert [r.getMessage().split("HTTP ")[1][:3] for r in caplog.records] == ["401", "404"]


def test_a_refusal_that_comes_back_after_a_good_read_is_logged_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    answers = iter([_Answer(401), _Answer(204), _Answer(401)])
    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=lambda *_: next(answers)
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        for _ in range(3):
            fetcher.read_once()
    assert len(caplog.records) == 2


def test_a_console_that_cannot_be_reached_is_warned_once_per_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def down(*_: Any) -> _Answer:
        raise OSError("refused")

    answers = iter([down, down, lambda *_: _Answer(204), down])

    def get(*args: Any) -> _Answer:
        return next(answers)(*args)

    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=get
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        for _ in range(4):
            fetcher.read_once()
    assert [r.getMessage().count("could not read") for r in caplog.records] == [1, 1]


@pytest.mark.parametrize(("status", "hint"), [(401, "Check the key"), (503, "keeps the settings")])
def test_a_refusal_says_what_to_look_at(
    caplog: pytest.LogCaptureFixture, status: int, hint: str
) -> None:
    fetcher = SettingsFetcher(
        origin="https://c",
        api_key="k",
        tenant_id="t",
        vendor_id="v",
        get=lambda *_: _Answer(status),
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        fetcher.read_once()
    assert hint in caplog.text
