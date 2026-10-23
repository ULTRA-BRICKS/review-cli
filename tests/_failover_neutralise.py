"""Shared test helper: temporarily neutralise seat-level PROVIDER-failover.

WHY THIS EXISTS: `test_failover_pool.py` and `test_inseat_retry.py` exercise the
BOARD-level reserve-replace and the in-seat RETRY layers IN ISOLATION. Seat-level
provider-failover (reviewlib.provider_failover.provider_chain — the same model across
several providers) is a DISTINCT layer with its own suite (test_provider_failover.py); if
left live it would change what "a seat fails" means here (a chained seat would fall over to
its alternate provider instead of failing, so no reserve backfill / retry fires). So those
two files replace `provider_chain` with an IDENTITY chain (`[model]`) for their own runs.

The patch MUST be scoped, not module-level: a bare module-level `_pf.provider_chain = ...`
runs at pytest COLLECTION time and permanently poisons the global for EVERY test collected
afterwards — it silently disabled failover in test_provider_failover.py's integration tests
(the seat never tried its alternate provider). This context manager saves and restores the
real function, and both files apply it per-test (an autouse fixture under pytest, and around
each `fn()` call in their `__main__` script harness), so the neutralisation never leaks.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import reviewlib.provider_failover as _pf


@contextlib.contextmanager
def identity_provider_chain():
    """Within the block, `provider_failover.provider_chain(model, ...)` returns `[model]`
    (no failover alternates). The panel imports `provider_chain` lazily from the module, so
    patching the module attribute is what the seat loop actually reads. Restored on exit."""
    saved = _pf.provider_chain
    _pf.provider_chain = lambda model, **_kw: [model]
    # Also a fresh seat-cooldown store for the block: a failed seat call records an
    # availability window (review-cli#663), which the plain-script runners would otherwise
    # carry into the next test and exclude the same stub seat there (pytest isolates it
    # through tests/conftest.py).
    saved_store = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
    with tempfile.TemporaryDirectory() as store_dir:
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(
            Path(store_dir) / "seat-cooldown.json"
        )
        try:
            yield
        finally:
            _pf.provider_chain = saved
            if saved_store is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_store
