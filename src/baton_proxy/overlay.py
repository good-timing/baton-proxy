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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# SPEC §11.4's blank: Unicode White_Space exactly, which str.strip() is not.
_WHITE_SPACE = "\t\n\v\f\r \x85\xa0                　"
_TEXT_CHANGES = ("append", "replace")
_FLAG_CHANGES = ("remove", "required")


class OverlayError(ValueError):
    """The overlay file cannot be used. Raised at startup, never mid-traffic."""


@dataclass(frozen=True)
class _Action:
    index: int
    change: str
    text: str | None = None
    tool: str | None = None
    param: str | None = None

    @property
    def hides_tool(self) -> bool:
        return self.tool is not None and self.param is None and self.change == "remove"

    @property
    def label(self) -> str:
        where = "instructions" if self.tool is None else self.tool
        if self.param is not None:
            where += f".{self.param}"
        return f"actions[{self.index}] {self.change} {where}"


def changed_text(text: str, actions: list[_Action]) -> str:
    """``text`` after ``actions``: an append is a new paragraph, a replace the whole text."""
    for action in actions:
        if action.change == "replace":
            text = action.text or ""
        elif action.change == "append":
            kept = text.rstrip(_WHITE_SPACE)
            text = f"{kept}\n\n{action.text}" if kept else action.text or ""
    return text


@dataclass
class Overlay:
    actions: list[_Action]
    hash: str

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
        self._instruction_actions = [a for a in self.actions if a.tool is None]
        self._reported = False

    @property
    def label(self) -> str:
        """The hash as logs and the Baton-Overlay header show it: a prefix of the event's."""
        return self.hash[:15]

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
        return changed_text(original, self._instruction_actions)

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

    def _mark(self, indexes: Any) -> None:
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


def _parse_action(index: int, raw: Any) -> _Action:
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
    if any(isinstance(target.get(k), str) and not target[k] for k in ("tool", "param")):
        raise OverlayError(f"{where}: a tool or param name cannot be empty")
    if keys == {"tool"} and isinstance(target["tool"], str):
        if change == "required":
            raise OverlayError(f"{where}: only a param can be required")
        return _Action(index, change, text, tool=target["tool"])
    if keys == {"tool", "param"} and all(isinstance(target[k], str) for k in keys):
        return _Action(index, change, text, tool=target["tool"], param=target["param"])
    if keys == {"instructions"} and target["instructions"] is True:
        if change != "append":
            raise OverlayError(f"{where}: the instructions take only `append`")
        return _Action(index, change, text)
    if keys == {"agent_text"}:
        raise OverlayError(f"{where}: baton-proxy does not apply agent_text targets yet")
    raise OverlayError(f"{where}: unknown target {json.dumps(target)}")


def parse_overlay(doc: Any) -> Overlay:
    if not isinstance(doc, dict) or set(doc) != {"actions"}:
        raise OverlayError('an overlay is an object with one key, "actions"')
    raw_actions = doc["actions"]
    if not isinstance(raw_actions, list):
        raise OverlayError("`actions` must be a list")
    actions = [_parse_action(i, raw) for i, raw in enumerate(raw_actions)]
    canonical = json.dumps(raw_actions, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Overlay(actions=actions, hash=digest)


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
