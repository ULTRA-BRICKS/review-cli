"""Task identity must not select a different plan or stale help contract."""
import io
import re
import sys

from reviewlib import backends, cli


def test_light_plan_is_identical_for_absent_flag_and_environment_task(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(backends, "backend_unavailable_reason", lambda _model: None)
    outputs = []
    for source in ("absent", "flag", "environment"):
        monkeypatch.delenv("REVIEW_TASK_CODE", raising=False)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        args = ["diff", "--show-board", "--preset", "light"]
        if source == "flag":
            args.extend(["--task", "518"])
        elif source == "environment":
            monkeypatch.setenv("REVIEW_TASK_CODE", "518")
        assert cli.main(args) == 0
        captured = capsys.readouterr()
        outputs.append(captured.out)
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0].strip(), "An empty display cannot establish plan parity"


def test_help_does_not_tie_role_coverage_to_task_identity(capsys):
    import pytest

    with pytest.raises(SystemExit) as stopped:
        cli.main(["diff", "--help"])
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.replace("`", "").split())
    assert "gh ship" not in help_text, "Current workflow is Forgejo, not archived GitHub"
    assert not re.search(r"recorded\s+--task", help_text)
    assert "optional task/issue code" in help_text
