"""Tracked background tasks (docs/02 §14: backfills never run inline in a
request handler).

A module-level registry keeps every spawned task alive until it finishes
(membership in the set is the strong reference) and swallows — but logs —
terminal exceptions, so a failed backfill never surfaces as an unhandled task
warning or a broken response.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        _tasks.discard(done)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            # Also marks the exception as retrieved (no GC-time warnings).
            log.warning("background task %s failed: %s", done.get_name(), exc)

    task.add_done_callback(_done)
    return task


def pending() -> set[asyncio.Task]:
    return set(_tasks)


async def drain(timeout: float = 30.0) -> bool:
    """Wait for every tracked task (tests, CLI). ``True`` = all finished."""
    if not _tasks:
        return True
    done, _still = await asyncio.wait(set(_tasks), timeout=timeout)
    return not _still


__all__ = ["drain", "pending", "spawn"]
