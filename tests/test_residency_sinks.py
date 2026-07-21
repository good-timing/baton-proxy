"""Tests for the S3 event sink (sinks.py).

No AWS: S3Sink takes an injected fake client, so the key layout is exercised
without boto3 or network.
"""

from __future__ import annotations

import json

from baton_proxy.sinks import S3Sink, make_sink


class FakeS3:
    """Records put_object calls."""

    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 — boto3 kwarg names
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType})


def _event(**over):
    base = {
        "event_id": "ev1",
        "event_type": "tool_call_start",
        "session_id": "sess1",
        "tenant_id": "customer:acme",
        "vendor_id": "acme-mcp",
        "payload": {"tool_name": "echo", "params": {"message": "hi"}},
    }
    base.update(over)
    return base


# ---- S3Sink -----------------------------------------------------------------


def test_s3_key_layout_matches_residency_design():
    s3 = FakeS3()
    sink = S3Sink("cust-bucket", "baton", client=s3)
    sink.write(_event())
    put = s3.puts[0]
    assert put["Bucket"] == "cust-bucket"
    assert put["Key"] == "baton/customer:acme/sess1/ev1.json"
    assert json.loads(put["Body"])["payload"]["params"]["message"] == "hi"


def test_s3_ref_for():
    sink = S3Sink("cust-bucket", client=FakeS3())
    assert sink.ref_for(_event()) == "s3://cust-bucket/customer:acme/sess1/ev1.json"


def test_s3_no_prefix():
    s3 = FakeS3()
    S3Sink("b", client=s3).write(_event())
    assert s3.puts[0]["Key"] == "customer:acme/sess1/ev1.json"


def test_make_sink_s3_scheme_requires_boto3_or_builds():
    # Without boto3 installed, constructing a real s3:// sink raises a helpful
    # error; with it installed it builds. Either way the scheme is recognized
    # (not "unsupported scheme").
    try:
        sink = make_sink("s3://bucket/prefix", api_key=None)
        assert isinstance(sink, S3Sink)
    except ValueError as e:
        assert "boto3" in str(e)
