"""Task-local response observation without changing GraphQL wire payloads."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone


class GQLResponseObserver:
    _current: ContextVar[GQLResponseObserver | None] = ContextVar("gql_response_observer", default=None)

    def __init__(self, callback: Callable[[object, datetime], None]):
        self._callback: Callable[[object, datetime], None] | None = callback
        self._entered = False

    @contextmanager
    def observe(self) -> Iterator[None]:
        if self._entered:
            raise RuntimeError("GraphQL response observer cannot be reused")
        self._entered = True
        token = self._current.set(self)
        try:
            yield
        finally:
            # Child tasks can retain a copied context after the request scope ends.
            self._callback = None
            self._current.reset(token)

    @classmethod
    def record(cls, response: object) -> None:
        observer = cls._current.get()
        if observer is not None and observer._callback is not None:
            observer._callback(response, datetime.now(timezone.utc))
