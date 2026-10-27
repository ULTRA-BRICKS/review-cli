"""State shared only by the ordered stages of a single CLI invocation."""
from __future__ import annotations
from types import SimpleNamespace

CONTINUE = object()

class DispatchState(SimpleNamespace):
    """A fresh invocation frame; populated before each downstream phase reads it."""
