"""End-user identity capture — the Emitter sends a resolver's principal as stated.

Also guards the `user_name` scrub rule against over-broad `name` redaction.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from baton_proxy.config import Config
from baton_proxy.emitter import Emitter
from baton_proxy.identity import Principal
from baton_proxy.scrub import Scrubber


def _config(path: str) -> Config:
    return Config(
        session_id="s",
        event_sink=f"file://{path}",
        tenant_id="acme",
        api_key=None,
        consent_token="c",
        vendor_id="v",
        log_file=None,
    )


def _emit_one(tmp_path, *, principal: Principal | None) -> dict:
    p = tmp_path / "events.jsonl"
    e = Emitter(_config(str(p)))
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={"x": 1}, principal=principal, call_id="c1")
    e.stop()
    lines = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    return lines[-1]


def test_the_id_is_sent_as_the_resolver_returned_it(tmp_path) -> None:
    ev = _emit_one(tmp_path, principal=Principal(principal_id="Alice@Example.COM"))
    assert ev["principal"] == {"id": "Alice@Example.COM", "source": "asserted", "form": "raw"}


def test_a_form_the_resolver_states_reaches_the_wire_with_the_id_untouched(tmp_path) -> None:
    digest = "9F2C" + "ab" * 30
    ev = _emit_one(tmp_path, principal=Principal(principal_id=digest, form="hashed"))
    assert ev["principal"] == {"id": digest, "source": "asserted", "form": "hashed"}


@pytest.mark.parametrize("form", ["encrypted", "HASHED", None, ["hashed"]])
def test_an_unregistered_form_is_sent_as_raw(
    tmp_path, caplog: pytest.LogCaptureFixture, form: Any
) -> None:
    """SPEC §11.4: anything not ``"hashed"`` is personal data."""
    with caplog.at_level(logging.WARNING):
        ev = _emit_one(tmp_path, principal=Principal(principal_id="u123", form=form))
    assert ev["principal"] == {"id": "u123", "source": "asserted", "form": "raw"}
    assert "form" in caplog.text


@pytest.mark.parametrize("blank", ["", "   ", "\t\n", None, 7, "a\ud800b", "jane\x00"])
def test_a_blank_unsendable_or_non_string_id_emits_no_principal(tmp_path, blank: Any) -> None:
    ev = _emit_one(tmp_path, principal=Principal(principal_id=blank))
    assert "principal" not in ev
    assert ev["event_type"] == "tool_call_start"


@pytest.mark.parametrize("returned", [{"principal_id": "u123"}, "u123", 7])
def test_a_resolver_return_that_is_not_a_principal_cannot_fail_the_emit(
    tmp_path, returned: Any
) -> None:
    ev = _emit_one(tmp_path, principal=returned)
    assert "principal" not in ev
    assert ev["event_type"] == "tool_call_start"


def test_the_id_is_capped(tmp_path) -> None:
    ev = _emit_one(tmp_path, principal=Principal(principal_id="x" * 500))
    assert ev["principal"]["id"] == "x" * 128


def test_principal_rides_as_one_object_with_all_three_members(tmp_path) -> None:
    """SPEC §11.4: all three members or none — a partial object is malformed.

    The schema in ``test_spec_conformance.py`` enforces ``required`` and
    ``additionalProperties`` structurally. This pins the same rule against the
    emitter directly, so the guarantee survives a session where the submodule
    is absent and that test skips.
    """
    ev = _emit_one(tmp_path, principal=Principal(principal_id="u123"))
    assert set(ev["principal"]) == {"id", "source", "form"}
    assert "principal_id" not in ev, "the flat field is retired (SPEC §13)"


def test_the_proxy_never_claims_an_attestation(tmp_path) -> None:
    """A consumer trusts only exactly ``"attested"``, and nothing in the
    producing stack verifies a principal."""
    ev = _emit_one(tmp_path, principal=Principal(principal_id="u123", form="hashed"))
    assert ev["principal"]["source"] == "asserted"


def test_no_principal_omits_the_member(tmp_path) -> None:
    ev = _emit_one(tmp_path, principal=None)
    assert "principal" not in ev
    assert "principal_id" not in ev


# ---- scrub: user_name redacted, name-ish keys untouched --------------------


def test_user_name_field_scrubbed_but_not_name() -> None:
    out = Scrubber()({"user_name": "Alice Smith", "name": "get_thing", "tool_name": "echo"})
    assert out["user_name"].startswith("[REDACTED")
    assert out["name"] == "get_thing"  # prompt/tool names must survive
    assert out["tool_name"] == "echo"
