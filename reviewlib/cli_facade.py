"""Introspection metadata for explicit compatibility forwarding functions."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TypeVar

F = TypeVar("F", bound=Callable)


def forwarding(implementation: Callable) -> Callable[[F], F]:
    """Expose real source/docstrings while retaining the public call signature.

    Standard inspect.unwrap/getsource follow __wrapped__; inspect.signature uses
    the facade's signature so the internal dependency argument never leaks out.
    The facade function still executes normally and retains its own globals.
    """
    def decorate(public: F) -> F:
        public.__signature__ = inspect.signature(public, follow_wrapped=False)
        public.__wrapped__ = implementation
        public.__doc__ = implementation.__doc__
        return public

    return decorate
