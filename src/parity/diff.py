"""Semantic diff between a baseline output and a candidate output.

A character diff of model output is close to useless — every line differs, and
the reader learns nothing. What a reviewer actually needs to know is:

* which *fields* changed, appeared, or vanished
* which *tool calls* changed
* and only then, for prose, what the wording change was

So this diffs structure first and falls back to text. That ordering is the whole
point of the module: a dropped `invoice.tax_total` should be one line, not a
wall of red.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from parity.checks.base import try_parse_json
from parity.domain.models import InteractionOutput, ToolCall, canonical_json

#: Longest text rendered in a diff before it is elided. A reviewer scrolling
#: 40KB of prose is not reviewing.
MAX_TEXT_CHARS = 4000


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True)
class FieldChange:
    """One field-level difference between two JSON documents."""

    path: str
    kind: ChangeKind
    before: Any = None
    after: Any = None

    def render(self) -> str:
        if self.kind is ChangeKind.REMOVED:
            return f"- {self.path}: {_short(self.before)}"
        if self.kind is ChangeKind.ADDED:
            return f"+ {self.path}: {_short(self.after)}"
        return f"~ {self.path}: {_short(self.before)} → {_short(self.after)}"


@dataclass(frozen=True)
class ToolCallChange:
    kind: ChangeKind
    name: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    def render(self) -> str:
        if self.kind is ChangeKind.REMOVED:
            return f"- tool {self.name}({_short(self.before)}) no longer called"
        if self.kind is ChangeKind.ADDED:
            return f"+ tool {self.name}({_short(self.after)}) newly called"
        return f"~ tool {self.name}: {_short(self.before)} → {_short(self.after)}"


@dataclass
class OutputDiff:
    """Everything that changed between two outputs."""

    structured: bool = False
    """True when both sides parsed as JSON and a field-level diff was possible."""

    fields: list[FieldChange] = field(default_factory=list)
    tool_calls: list[ToolCallChange] = field(default_factory=list)
    text_before: str = ""
    text_after: str = ""
    finish_reason_before: str | None = None
    finish_reason_after: str | None = None

    @property
    def finish_reason_changed(self) -> bool:
        return self.finish_reason_before != self.finish_reason_after

    @property
    def empty(self) -> bool:
        return not (
            self.fields
            or self.tool_calls
            or self.text_before != self.text_after
            or self.finish_reason_changed
        )

    def text_unified(self, *, context: int = 2) -> list[str]:
        """Unified diff of the prose, word-wrapped into comparable lines.

        Only meaningful when the outputs are not structured; for JSON the field
        list says more in less space.
        """
        if self.text_before == self.text_after:
            return []
        before = _wrap(self.text_before)
        after = _wrap(self.text_after)
        return list(
            difflib.unified_diff(
                before, after, fromfile="baseline", tofile="candidate", n=context, lineterm=""
            )
        )

    def summary(self) -> str:
        """One line, for a table cell."""
        parts: list[str] = []
        removed = sum(1 for c in self.fields if c.kind is ChangeKind.REMOVED)
        added = sum(1 for c in self.fields if c.kind is ChangeKind.ADDED)
        changed = sum(1 for c in self.fields if c.kind is ChangeKind.CHANGED)
        if removed:
            parts.append(f"{removed} field(s) removed")
        if added:
            parts.append(f"{added} field(s) added")
        if changed:
            parts.append(f"{changed} value(s) changed")
        if self.tool_calls:
            parts.append(f"{len(self.tool_calls)} tool-call change(s)")
        if self.finish_reason_changed:
            parts.append(
                f"finish reason {self.finish_reason_before or 'none'}"
                f" → {self.finish_reason_after or 'none'}"
            )
        if not parts and self.text_before != self.text_after:
            delta = len(self.text_after) - len(self.text_before)
            parts.append(f"prose rewritten ({delta:+d} chars)")
        return "; ".join(parts) or "no difference"


def _short(value: Any, limit: int = 60) -> str:
    if value is None:
        return "∅"
    rendered = value if isinstance(value, str) else canonical_json(value)
    rendered = rendered.replace("\n", " ")
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _wrap(text: str, width: int = 88) -> list[str]:
    """Split prose into short lines so a unified diff localises the change.

    Without this, a paragraph is one line and any edit reports the whole
    paragraph as changed.
    """
    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        if len(paragraph) <= width:
            lines.append(paragraph)
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            lines.append(current)
    return lines


def _flatten(value: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Map dotted path to leaf value.

    Lists are indexed, unlike the `required_fields` check which collapses them.
    Here the reader wants to know *which* element changed.
    """
    if depth > 24:
        return {prefix or "<root>": value}
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        if not value and prefix:
            # A nested empty object is a real field. An empty *document* is not
            # a field, so it contributes nothing and does not show up as a
            # phantom change when the other side has content.
            flat[prefix] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(item, path, depth + 1))
    elif isinstance(value, list):
        if not value and prefix:
            flat[prefix] = []
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]", depth + 1))
    else:
        flat[prefix or "<root>"] = value
    return flat


def diff_json(before: Any, after: Any) -> list[FieldChange]:
    """Field-level difference between two parsed JSON documents."""
    flat_before = _flatten(before)
    flat_after = _flatten(after)
    changes: list[FieldChange] = []

    for path in sorted(set(flat_before) | set(flat_after)):
        in_before = path in flat_before
        in_after = path in flat_after
        if in_before and not in_after:
            changes.append(
                FieldChange(path=path, kind=ChangeKind.REMOVED, before=flat_before[path])
            )
        elif in_after and not in_before:
            changes.append(FieldChange(path=path, kind=ChangeKind.ADDED, after=flat_after[path]))
        elif flat_before[path] != flat_after[path]:
            changes.append(
                FieldChange(
                    path=path,
                    kind=ChangeKind.CHANGED,
                    before=flat_before[path],
                    after=flat_after[path],
                )
            )
    return changes


def diff_tool_calls(
    before: tuple[ToolCall, ...], after: tuple[ToolCall, ...]
) -> list[ToolCallChange]:
    """Compare tool calls by name, ignoring provider-assigned call ids."""
    changes: list[ToolCallChange] = []
    before_by_name: dict[str, list[ToolCall]] = {}
    after_by_name: dict[str, list[ToolCall]] = {}
    for call in before:
        before_by_name.setdefault(call.name, []).append(call)
    for call in after:
        after_by_name.setdefault(call.name, []).append(call)

    for name in sorted(set(before_by_name) | set(after_by_name)):
        old = before_by_name.get(name, [])
        new = after_by_name.get(name, [])
        for index in range(max(len(old), len(new))):
            old_call = old[index] if index < len(old) else None
            new_call = new[index] if index < len(new) else None
            if old_call is not None and new_call is None:
                changes.append(
                    ToolCallChange(kind=ChangeKind.REMOVED, name=name, before=old_call.arguments)
                )
            elif new_call is not None and old_call is None:
                changes.append(
                    ToolCallChange(kind=ChangeKind.ADDED, name=name, after=new_call.arguments)
                )
            elif (
                old_call is not None
                and new_call is not None
                and old_call.arguments != new_call.arguments
            ):
                changes.append(
                    ToolCallChange(
                        kind=ChangeKind.CHANGED,
                        name=name,
                        before=old_call.arguments,
                        after=new_call.arguments,
                    )
                )
    return changes


def diff_outputs(before: InteractionOutput, after: InteractionOutput) -> OutputDiff:
    """Structure-first diff of two model outputs."""
    result = OutputDiff(
        text_before=before.text[:MAX_TEXT_CHARS],
        text_after=after.text[:MAX_TEXT_CHARS],
        finish_reason_before=before.finish_reason,
        finish_reason_after=after.finish_reason,
    )
    result.tool_calls = diff_tool_calls(before.tool_calls, after.tool_calls)

    before_ok, before_json = try_parse_json(before.text)
    after_ok, after_json = try_parse_json(after.text)
    if before_ok and after_ok:
        result.structured = True
        result.fields = diff_json(before_json, after_json)

    return result
