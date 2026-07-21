"""Tests for the data-residency sinks: S3Sink + SplitSink (sinks.py).

No AWS: S3Sink takes an injected fake client, so the key layout and the
payload/metadata split are exercised without boto3 or network.
"""

from __future__ import annotations

import json

from baton_proxy.sinks import FileSink, S3Sink, Sink, SplitSink, make_sink


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


# ---- SplitSink --------------------------------------------------------------


def test_split_routes_payload_and_metadata():
    s3 = FakeS3()
    payload = S3Sink("cust-bucket", client=s3)
    meta_written: list[dict] = []

    class MetaSink(Sink):
        def write(self, event):
            meta_written.append(event)

        def close(self):
            return

    split = SplitSink(MetaSink(), payload)
    split.write(_event())

    # payload leg got the FULL event
    assert json.loads(s3.puts[0]["Body"])["payload"]["tool_name"] == "echo"
    # metadata leg got the envelope WITHOUT raw payload, plus offload markers
    meta = meta_written[0]
    assert "payload" not in meta
    assert meta["payload_offloaded"] is True
    assert meta["payload_ref"] == "s3://cust-bucket/customer:acme/sess1/ev1.json"
    assert meta["event_id"] == "ev1"  # metadata preserved


def test_split_payload_ref_omitted_when_sink_has_no_ref_for(tmp_path):
    # FileSink has no ref_for -> metadata is marked offloaded but carries no ref.
    pfile = tmp_path / "payload.jsonl"
    mfile = tmp_path / "meta.jsonl"
    split = SplitSink(FileSink(str(mfile)), FileSink(str(pfile)))
    split.write(_event())
    split.close()
    meta = json.loads(mfile.read_text().strip())
    assert meta["payload_offloaded"] is True
    assert "payload_ref" not in meta
    assert "payload" not in meta
    payload_line = json.loads(pfile.read_text().strip())
    assert payload_line["payload"]["tool_name"] == "echo"
