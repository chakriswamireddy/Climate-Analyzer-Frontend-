"""Request trace ids (docs/02 §3, §7.2 — every response carries ``X-Trace-Id``).

The id lives in two places so it survives every failure mode:

* ``scope["state"]["trace_id"]`` — reachable from the ``Request`` object the
  exception handlers receive, including the outermost failure path where the
  contextvar has already been reset;
* a contextvar — convenient for code that has no ``Request`` in hand.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Mapping

_trace: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return "tr_" + uuid.uuid4().hex


def begin(scope: dict, trace_id: str | None = None) -> tuple[str, contextvars.Token]:
    """Install the id on ``scope`` + the contextvar; returns a reset token."""
    value = trace_id or new_trace_id()
    state = scope.setdefault("state", {})
    state["trace_id"] = value
    return value, _trace.set(value)


def end(token: contextvars.Token) -> None:
    _trace.reset(token)


def get_trace_id(request: object | None = None) -> str:
    """Trace id of the current request, from ``request.scope`` when available."""
    if request is not None:
        scope = getattr(request, "scope", None)
        if isinstance(scope, Mapping):
            state = scope.get("state") or {}
            found = state.get("trace_id")
            if found:
                return str(found)
    return _trace.get()


__all__ = ["begin", "end", "get_trace_id", "new_trace_id"]
