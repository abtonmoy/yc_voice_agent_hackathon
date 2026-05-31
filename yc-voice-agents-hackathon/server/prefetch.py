"""Speculative tool prefetch (optimization-plan.md §4 E).

:class:`PrefetchExecutor` is a cache-aware wrapper around the pure diagnostic
tools. :meth:`cached_or_run` returns a cached result instantly when present,
otherwise runs the tool and stores it. :meth:`prefetch` schedules the tool to run
in the background (``asyncio.create_task``) and populate the cache, so a later
``cached_or_run`` for the same key is an instant hit.

Cache key is ``(tool_name, frozenset(args))`` and lives on
``state.prefetch_cache`` for the whole call (TTL = the call). stdlib/asyncio only
— no pipecat.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


def make_key(tool_fn: Callable[..., Any], *args: Any) -> tuple[str, frozenset]:
    """Build the cache key ``(tool_name, frozenset(args))`` for a prefetch (§4 E).

    Positional ``args`` are keyed as a ``frozenset`` so call-order does not
    produce distinct keys for the same argument set.
    """
    name = getattr(tool_fn, "__name__", repr(tool_fn))
    return (name, frozenset(args))


class PrefetchExecutor:
    """Cache-aware executor over ``state.prefetch_cache``.

    Stores every result keyed by ``(tool_name, frozenset(args))`` so a value
    computed (or speculatively prefetched) once is reused for the rest of the
    call. Supports both sync and async tool functions.
    """

    def __init__(self, state) -> None:
        self._state = state
        # Track scheduled background tasks so callers can await them in tests
        # and so we hold a reference (tasks would otherwise be GC'd).
        self._tasks: dict[tuple, asyncio.Task] = {}

    def _cache(self) -> dict:
        return self._state.prefetch_cache

    async def _invoke(self, tool_fn: Callable[..., Any], *args: Any) -> Any:
        """Call ``tool_fn`` whether it is sync or a coroutine function."""
        result = tool_fn(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def cached_or_run(
        self, tool_fn: Callable[..., Any], *args: Any, key: tuple | None = None
    ) -> Any:
        """Return the cached result for this key, else run the tool and cache it.

        If a background :meth:`prefetch` for the same key is in flight, await it
        rather than running the tool a second time.
        """
        cache_key = key if key is not None else make_key(tool_fn, *args)
        if cache_key in self._cache():
            return self._cache()[cache_key]

        # A prefetch for this key may already be running — await it instead of
        # duplicating the work.
        task = self._tasks.get(cache_key)
        if task is not None:
            result = await task
            return result

        result = await self._invoke(tool_fn, *args)
        self._cache()[cache_key] = result
        return result

    def prefetch(
        self, tool_fn: Callable[..., Any], *args: Any, key: tuple | None = None
    ) -> asyncio.Task:
        """Schedule ``tool_fn(*args)`` in the background; cache the result.

        Returns the created :class:`asyncio.Task` (so tests can await it). A
        no-op (returns the existing task) if the key is already cached or a
        prefetch for it is already in flight.
        """
        cache_key = key if key is not None else make_key(tool_fn, *args)

        if cache_key in self._cache():
            return _completed_task(self._cache()[cache_key])

        existing = self._tasks.get(cache_key)
        if existing is not None and not existing.done():
            return existing

        async def _run() -> Any:
            result = await self._invoke(tool_fn, *args)
            self._cache()[cache_key] = result
            return result

        task = asyncio.ensure_future(_run())

        def _cleanup(_t: asyncio.Task, _k: tuple = cache_key) -> None:
            # Drop the finished task so a later prefetch can reschedule if needed.
            if self._tasks.get(_k) is _t:
                self._tasks.pop(_k, None)

        task.add_done_callback(_cleanup)
        self._tasks[cache_key] = task
        return task


def _completed_task(value: Any) -> asyncio.Task:
    """Wrap an already-known value in a finished Task for a uniform return type."""
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut  # type: ignore[return-value]


__all__ = ["PrefetchExecutor", "make_key"]
