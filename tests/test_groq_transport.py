from pathlib import Path
from unittest.mock import patch
from reviewlib import backends as b

def test_omp_groq_env_key_is_valid_auth_without_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-key")
    monkeypatch.setenv("OMP_AUTH_DB", str(tmp_path / "missing.db"))
    assert b._omp_auth_available("groq")
    assert not b._omp_auth_available("openai")

def test_groq_named_route():
    assert b.resolve_backend("groq:llama-3.3-70b-versatile") is b.review_groq
    assert b.provider_route_name("groq:llama-3.3-70b-versatile") == "groq"

def test_groq_http_uses_only_own_key_and_fixed_endpoint(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-groq-key")
    with patch.object(b, "_openai_compatible_request") as send, patch.object(b, "unpaid_provider_result", return_value=None):
        b.review_groq("groq:llama-3.3-70b-versatile", "review", "diff", Path.cwd(), 20)
    args = send.call_args.kwargs
    assert args["base_url"] == "https://api.groq.com/openai/v1"
    assert args["key"] == "synthetic-groq-key"
    assert args["extra_body"]["max_tokens"] == 2000
    assert args["api_model"] == "llama-3.3-70b-versatile"

def test_groq_missing_key_cannot_call_http(monkeypatch):
    with patch.object(b, "_groq_key", side_effect=RuntimeError("GROQ_API_KEY unavailable")), patch.object(b, "_openai_compatible_request") as send, patch.object(b, "unpaid_provider_result", return_value=None):
        result = b.review_groq("groq", "review", "diff", Path.cwd(), 20)
    assert result.returncode != 0
    assert not send.called

def test_disabled_provider_stays_disabled():
    denied = b.ReviewResult(model="groq", command="disabled", returncode=1, stdout="", stderr="disabled")
    with patch.object(b, "unpaid_provider_result", return_value=denied), patch.object(b, "_openai_compatible_request") as send:
        assert b.review_groq("groq", "review", "diff", Path.cwd(), 20) is denied
    assert not send.called
