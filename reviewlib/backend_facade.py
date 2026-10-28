"""Retain public backend signatures and resolvable types across transport modules."""

from __future__ import annotations

from typing import get_type_hints

from .cli_facade import forwarding as _forwarding


def forwarding(implementation):
    """Preserve public annotations before `__wrapped__` changes their lookup scope.

    `get_type_hints` follows the wrapped function's globals. Public annotations
    originally resolve in `backends`, not in its implementation module. Resolving
    them here retains their meaning while the existing forwarding helper retains
    the exact signature, source inspection, name and module metadata.
    """
    forward = _forwarding(implementation)

    def decorate(function):
        annotations = get_type_hints(function)
        wrapped = forward(function)
        wrapped.__annotations__ = annotations
        return wrapped

    return decorate
