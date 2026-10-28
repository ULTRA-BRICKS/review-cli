"""Public backend contracts retained when transport implementation is split."""

from __future__ import annotations

import inspect
import subprocess
import sys
import typing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reviewlib import backends  # noqa: E402


def test_public_signature_has_no_internal_dependency_argument():
    params = inspect.signature(backends.review_claude).parameters
    assert list(params) == [
        "model",
        "prompt",
        "diff",
        "cwd",
        "timeout",
        "round_no",
        "effort",
    ]
    assert params["round_no"].default == 0
    assert params["effort"].default is None
    assert (
        inspect.signature(backends.configure_omp_bin).parameters["raw"].annotation
        == "object"
    )


def test_public_dependency_patch_reaches_api_command():
    with patch.object(
        backends, "_claude_api_model", return_value="sentinel-model"
    ) as resolve:
        assert (
            backends._claude_api_command("claude:original")
            == "Anthropic API sentinel-model"
        )
        resolve.assert_called_once_with("claude:original")


def test_public_dependency_patch_reaches_availability():
    with patch.object(
        backends, "backend_unavailable_reason", return_value="blocked"
    ) as reason:
        assert not backends.backend_available("fixture:seat")
        reason.assert_called_once_with("fixture:seat")
    with patch.object(backends, "backend_unavailable_reason", return_value=None):
        assert backends.backend_available("fixture:seat")


def test_configuration_writes_the_public_module_state():
    with patch.object(backends, "_CONFIG_OMP_BIN", None):
        backends.configure_omp_bin("  /fixture/bin/omp  ")
        assert backends._CONFIG_OMP_BIN == "/fixture/bin/omp"
        backends.configure_omp_bin(None)
        assert backends._CONFIG_OMP_BIN is None


def test_original_source_remains_inspectable():
    source = inspect.getsource(backends._review_claude_cli_with_account_fallback)
    assert "spawn_refuse_reason" in source
    assert "_usage_limit_skip_result" in source


def test_effort_capability_keeps_cache_management_surface():
    assert callable(backends._claude_cli_supports_effort.cache_clear)
    assert callable(backends._claude_cli_supports_effort.cache_info)


def test_context_manager_keeps_public_cleanup_dependency():
    fixture = Path("/fixture/private-sandbox")
    with patch.object(backends.tempfile, "mkdtemp", return_value=str(fixture)):
        with patch.object(backends, "_remove_sandbox_best_effort") as cleanup:
            with backends._seat_sandbox("fixture-") as actual:
                assert actual == fixture
            cleanup.assert_called_once_with(fixture)


def test_public_type_hints_still_resolve():
    assert (
        typing.get_type_hints(backends.review_claude)["return"] is backends.ReviewResult
    )


def test_implementation_modules_import_without_circular_initialization():
    root = Path(__file__).resolve().parent.parent
    for module in sorted((root / "reviewlib").glob("backend_*.py")):
        result = subprocess.run(
            [sys.executable, "-c", f"import reviewlib.{module.stem}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, (module.name, result.stderr)


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"PASS {name}")
