"""Overlays: changes to what the agent sees, applied on the way out of the proxy.

The file holds one overlay in the settings document's shape (SPEC §2.6), so a
change written here reads the same as one the Console stores::

    {"actions": [
      {"target": {"tool": "create_fields"}, "append": "Get the record type id from get_workspace first."},
      {"target": {"tool": "create_fields", "param": "fields"}, "replace": "..."},
      {"target": {"tool": "create_fields", "param": "legacy_mode"}, "remove": true},
      {"target": {"tool": "create_fields", "param": "record_type"}, "required": true},
      {"target": {"tool": "debug_dump"}, "remove": true},
      {"target": {"instructions": true}, "append": "..."}
    ]}

Actions apply in order. The proxy serves every caller the same, so tool and
instruction targets share the one file. A malformed file is refused at startup;
an action the tool list cannot take (no such tool or param, hiding a param the
tool requires) is skipped and reported as unmatched.

JSON is always accepted. YAML needs PyYAML (``pip install baton-proxy[overlay]``)
because the base package has no dependencies.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("baton_proxy")

# SPEC §11.4's blank: Unicode White_Space exactly, which str.strip() is not.
WHITE_SPACE = "\t\n\v\f\r \x85\xa0                　"
_TEXT_CHANGES = ("append", "replace")
_FLAG_CHANGES = ("remove", "required")

Place = Literal["audience", "server"]
INSTRUCTION_PIECES = ("instructions.subagent", "instructions.report_pointer")
AUDIENCE_PIECES = ("goal_param.description", "report_tool.description", "report_tool.refusal")
_HEADER_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class OverlayError(ValueError):
    """The overlay file cannot be used. Raised at startup, never mid-traffic."""


@dataclass(frozen=True)
class _Action:
    index: int
    change: str
    text: str | None = None
    tool: str | None = None
    param: str | None = None
    piece: str | None = None

    @property
    def hides_tool(self) -> bool:
        return self.tool is not None and self.param is None and self.change == "remove"

    @property
    def label(self) -> str:
        where = self.piece or ("instructions" if self.tool is None else self.tool)
        if self.param is not None:
            where += f".{self.param}"
        return f"actions[{self.index}] {self.change} {where}"


def overlaid(text: str, overlay: Overlay | None, piece: str) -> str:
    """``text``, one piece of Baton's text, after ``overlay``, without white
    space at its end (SPEC §2.6)."""
    actions = overlay.piece_actions(piece) if overlay is not None else []
    if not actions:
        return text
    return changed_text(text, actions).rstrip(WHITE_SPACE)


def changed_text(text: str, actions: list[_Action]) -> str:
    """``text`` after ``actions``: an append is a new paragraph, a replace the whole text."""
    for action in actions:
        if action.change == "replace":
            text = action.text or ""
        elif action.change == "append":
            kept = text.rstrip(WHITE_SPACE)
            text = f"{kept}\n\n{action.text}" if kept else action.text or ""
    return text


@dataclass
class Overlay:
    actions: list[_Action]
    hash: str
    id: str | None = None
    # SPEC §2.6's paragraph rule, for a Console overlay only:
    # test_an_overlay_file_keeps_the_white_space_its_instructions_end_with
    trims_paragraphs: bool = False

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._applied: set[int] = set()
        self._by_tool: dict[str, list[_Action]] = {}
        hidden: dict[str, set[str]] = {}
        for a in self.actions:
            if a.tool is not None:
                self._by_tool.setdefault(a.tool, []).append(a)
                if a.param is not None and a.change == "remove":
                    hidden.setdefault(a.tool, set()).add(a.param)
        self._hidden_params = {t: frozenset(p) for t, p in hidden.items()}
        self.hidden_tools = frozenset(a.tool for a in self.actions if a.hides_tool)
        self._instruction_actions = [a for a in self.actions if a.tool is None and a.piece is None]
        self._reported = False

    @property
    def label(self) -> str:
        """The id, or the hash prefix when there is none or it cannot be a header
        value, as logs and the Baton-Overlay header show it. Events keep the id.
        test_an_id_that_cannot_be_a_header_is_shown_as_its_hash"""
        if self.id is not None and _HEADER_SAFE_ID.fullmatch(self.id):
            return self.id
        return self.hash[:15]

    def piece_actions(self, piece: str) -> list[_Action]:
        return [a for a in self.actions if a.piece == piece]

    def hidden_params(self, tool_name: str) -> frozenset[str]:
        """Params a call to ``tool_name`` drops, because the agent was never shown them."""
        return self._hidden_params.get(tool_name, frozenset())

    def _keep(self, tool_name: str, param: str) -> None:
        """A listing showed ``param`` required, so it is never hidden and never dropped."""
        with self._lock:
            self._hidden_params[tool_name] = self.hidden_params(tool_name) - {param}

    def report_once(self) -> tuple[int, list[str]] | None:
        """Tools changed so far and ``unmatched()``, the first time across every session."""
        with self._lock:
            if self._reported:
                return None
            self._reported = True
            changed = len({a.tool for a in self.actions if a.index in self._applied})
        return changed, self.unmatched()

    def apply_to_instructions(self, original: str) -> str:
        changed = changed_text(original, self._instruction_actions)
        if self._instruction_actions and self.trims_paragraphs:
            return changed.rstrip(WHITE_SPACE)
        return changed

    def apply_to_tools(self, tools: list[Any]) -> list[Any]:
        """Return the tool list as the agent should see it. Never mutates ``tools``."""
        out: list[Any] = []
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else None
            actions = self._by_tool.get(name) if isinstance(name, str) else None
            if not actions:
                out.append(tool)
                continue
            if name in self.hidden_tools:
                self._mark(a.index for a in actions if a.hides_tool)
                continue
            tool = copy.deepcopy(tool)
            for a in actions:
                skipped = _apply_one(tool, a)
                if skipped is None:
                    self._mark([a.index])
                elif skipped == _REQUIRED and a.param is not None:
                    self._keep(name, a.param)
            out.append(tool)
        return out

    def unmatched(self) -> list[str]:
        """Tool actions no listing has taken yet. Instruction appends always apply."""
        with self._lock:
            return [
                a.label for a in self.actions if a.tool is not None and a.index not in self._applied
            ]

    def _mark(self, indexes: Iterable[int]) -> None:
        with self._lock:
            self._applied.update(indexes)


def _required(tool: dict[str, Any]) -> list[Any]:
    schema = tool.get("inputSchema")
    required = schema.get("required") if isinstance(schema, dict) else None
    return required if isinstance(required, list) else []


def overlay_label(overlay: Overlay | None) -> str:
    return overlay.label if overlay is not None else "none"


_REQUIRED = "the tool requires it"


def _apply_one(tool: dict[str, Any], a: _Action) -> str | None:
    """Apply ``a`` to ``tool``; why it was skipped, else None."""
    if a.param is None:
        return _change_description(tool, a)
    if a.change == "remove" and a.param in _required(tool):
        return _REQUIRED
    schema = tool.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or a.param not in props:
        return "no such param"
    if a.change == "remove":
        del props[a.param]
    elif a.change == "required":
        required = _required(tool)
        if a.param not in required:
            schema["required"] = [*required, a.param]
    elif isinstance(props[a.param], dict):
        return _change_description(props[a.param], a)
    else:
        return "its schema is not an object"
    return None


def _change_description(target: dict[str, Any], a: _Action) -> str | None:
    description = target.get("description")
    if not isinstance(description, str):
        if a.change == "append" and description is not None:
            return "its description is not text"
        description = ""
    target["description"] = changed_text(description, [a])
    return None


def _parse_action(index: int, raw: Any, place: Place | None = None) -> _Action:
    """One action. ``place`` is where a settings document holds it; a file's
    overlay has none, and refuses empty names and ``agent_text``."""
    where = f"actions[{index}]"
    if not isinstance(raw, dict) or not isinstance(target := raw.get("target"), dict):
        raise OverlayError(f"{where}: needs a `target` object")
    changes = [c for c in (*_TEXT_CHANGES, *_FLAG_CHANGES) if c in raw]
    if len(changes) != 1 or set(raw) != {"target", changes[0]}:
        raise OverlayError(
            f"{where}: needs `target` and exactly one of append, replace, remove, required"
        )
    change = changes[0]
    value = raw[change]
    if change in _TEXT_CHANGES and not isinstance(value, str):
        raise OverlayError(f"{where}: `{change}` needs text")
    if change in _FLAG_CHANGES and value is not True:
        raise OverlayError(f"{where}: `{change}` must be true")
    text = value if change in _TEXT_CHANGES else None
    keys = set(target)
    if place is None and any(
        isinstance(target.get(k), str) and not target[k] for k in ("tool", "param")
    ):
        raise OverlayError(f"{where}: a tool or param name cannot be empty")
    if keys == {"tool"} and isinstance(target["tool"], str):
        if change == "required":
            raise OverlayError(f"{where}: only a param can be required")
        return _placed(_Action(index, change, text, tool=target["tool"]), place, "audience")
    if keys == {"tool", "param"} and all(isinstance(target[k], str) for k in keys):
        action = _Action(index, change, text, tool=target["tool"], param=target["param"])
        return _placed(action, place, "audience")
    if keys == {"instructions"} and target["instructions"] is True:
        if change != "append":
            raise OverlayError(f"{where}: the instructions take only `append`")
        return _placed(_Action(index, change, text), place, "server")
    if keys == {"agent_text"}:
        if place is None:
            raise OverlayError(f"{where}: baton-proxy does not apply agent_text targets yet")
        if isinstance(piece := target["agent_text"], str):
            if change not in _TEXT_CHANGES:
                raise OverlayError(f"{where}: agent_text takes only append or replace")
            if piece not in (*INSTRUCTION_PIECES, *AUDIENCE_PIECES):
                raise OverlayError(f"{where}: unknown agent_text {piece!r}")
            allowed: Place = "server" if piece in INSTRUCTION_PIECES else "audience"
            return _placed(_Action(index, change, text, piece=piece), place, allowed)
    raise OverlayError(f"{where}: unknown target {json.dumps(target)}")


def _placed(action: _Action, place: Place | None, allowed: Place) -> _Action:
    if place is not None and place != allowed:
        where = "server_overlay" if allowed == "server" else "an audience's overlay"
        raise OverlayError(f"actions[{action.index}]: this target goes only in {where}")
    return action


def _digest(raw_actions: Any) -> str:
    canonical = json.dumps(raw_actions, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# One warning per overlay id and action (None: the whole overlay), because a
# saved overlay never changes under its id. Bounded, for a long-lived process.
_warned: set[tuple[str, int | None]] = set()
_WARNED_HELD = 4096

OverlayKey = tuple[str, str, str]


def _warn_once(key: tuple[str, int | None], message: str, *args: Any) -> None:
    if key in _warned:
        return
    if len(_warned) >= _WARNED_HELD:
        _warned.clear()
    _warned.add(key)
    logger.warning(message, *args)


def document_overlay(
    overlays: Mapping[str, Any],
    overlay_id: Any,
    place: Place,
    reuse: Mapping[OverlayKey, Overlay],
    parsed: dict[OverlayKey, Overlay],
) -> Overlay | None:
    """The overlay ``overlay_id`` names in a settings document, holding the actions
    allowed in ``place`` (SPEC §2.6). A bad action is skipped alone; an overlay
    that cannot be read is ``None``. Each warns once.

    An Overlay learns from listings which hidden params a tool requires, so one
    the previous document held (``reuse``) is returned as the same object, and
    every overlay returned is recorded in ``parsed`` for the next refresh.
    test_a_refreshed_document_keeps_what_its_overlay_learned
    """
    if overlay_id is None:
        return None
    body = overlays.get(overlay_id) if isinstance(overlay_id, str) else None
    raw_actions = body.get("actions") if isinstance(body, dict) else None
    if not isinstance(overlay_id, str) or not isinstance(raw_actions, list):
        shown = overlay_id if isinstance(overlay_id, str) else repr(overlay_id)
        _warn_once(
            (shown, None), "baton-proxy: overlay %r cannot be read; none is applied there", shown
        )
        return None
    key = (overlay_id, place, _digest(raw_actions))
    overlay = parsed.get(key) or reuse.get(key)
    if overlay is None:
        kept: list[_Action] = []
        for index, raw in enumerate(raw_actions):
            try:
                kept.append(_parse_action(index, raw, place))
            except OverlayError as exc:
                _warn_once(
                    (overlay_id, index),
                    "baton-proxy: overlay %r action %d is skipped: %s",
                    overlay_id,
                    index,
                    exc,
                )
        overlay = Overlay(actions=kept, hash=key[2], id=overlay_id, trims_paragraphs=True)
    parsed[key] = overlay
    return overlay


def parse_overlay(doc: Any) -> Overlay:
    if not isinstance(doc, dict) or set(doc) != {"actions"}:
        raise OverlayError('an overlay is an object with one key, "actions"')
    raw_actions = doc["actions"]
    if not isinstance(raw_actions, list):
        raise OverlayError("`actions` must be a list")
    actions = [_parse_action(i, raw) for i, raw in enumerate(raw_actions)]
    return Overlay(actions=actions, hash=_digest(raw_actions))


def load_overlay(path: str) -> Overlay:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise OverlayError(f"cannot read overlay {path}: {exc}") from exc
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise OverlayError(
                f"{path} is YAML, which needs PyYAML: pip install 'baton-proxy[overlay]', "
                "or write the overlay as JSON"
            ) from exc
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise OverlayError(f"{path}: {exc}") from exc
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OverlayError(f"{path}: {exc}") from exc
    return parse_overlay(doc)
