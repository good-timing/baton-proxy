"""Which JSON-RPC messages are owed an answer, and which message answers one."""

from __future__ import annotations

from typing import Any


def owed_answer_id(msg: dict[str, Any]) -> Any:
    """The id a client message is owed an answer on, or None.

    A message without a method is the client's answer to a server request; its id
    is the server's, so nothing here may answer or close it.
    """
    return msg.get("id") if "method" in msg else None


def answers(msg: Any, req_id: Any) -> bool:
    """Whether ``msg`` answers ``req_id``; a server request reusing the id does not."""
    return (
        req_id is not None
        and isinstance(msg, dict)
        and msg.get("id") == req_id
        and "method" not in msg
    )
