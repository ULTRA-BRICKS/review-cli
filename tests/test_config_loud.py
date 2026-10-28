"""An unreadable config.yaml is a loud error, never a silent fall back to defaults
(review-cli#705).

WHY: `load_config()` used to return {} on ANY exception. Under an interpreter without
PyYAML (Homebrew python3 here; /usr/bin/python3 had it) the whole config -- the cheap
roster, the omp account policy and its usage ceiling -- vanished and every status screen
looked normal. An ABSENT file still means defaults; a file that exists but cannot be
loaded now raises `ConfigError` and the CLI stops with exit 2 and one message naming the
file, the cause and the fix.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli, config  # noqa: E402


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def _block_yaml_import(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_absent_config_means_defaults(config_file):
    assert not config_file.exists()
    assert config.load_config() == {}


def test_empty_config_means_defaults(config_file):
    config_file.write_text("# only a comment\n")
    assert config.load_config() == {}


def test_valid_config_is_returned(config_file):
    config_file.write_text("pool: 3\nomp_accounts:\n  max_usage_percent: 80\n")
    assert config.load_config() == {
        "pool": 3,
        "omp_accounts": {"max_usage_percent": 80},
    }


def test_missing_pyyaml_raises_naming_path_cause_and_fix(config_file, monkeypatch):
    config_file.write_text("pool: 3\n")
    _block_yaml_import(monkeypatch)
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    text = str(exc.value)
    assert str(config_file) in text
    assert "PyYAML" in text
    assert sys.executable in text
    assert "pip install" in text


def test_missing_pyyaml_without_a_config_file_is_not_an_error(config_file, monkeypatch):
    _block_yaml_import(monkeypatch)
    assert config.load_config() == {}


def test_syntax_error_raises_with_the_location(config_file):
    config_file.write_text("models: [unclosed\npool: 3\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    text = str(exc.value)
    assert str(config_file) in text
    assert "line" in text


def test_non_mapping_top_level_raises(config_file):
    config_file.write_text("- a\n- b\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    assert "mapping" in str(exc.value)


def test_unreadable_file_raises(config_file):
    config_file.mkdir()  # a directory where the file should be
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    assert str(config_file) in str(exc.value)


def test_cli_stops_with_exit_2_and_one_message(config_file, monkeypatch, capsys):
    config_file.write_text("pool: 3\n")
    _block_yaml_import(monkeypatch)
    rc = cli.main(["seats"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert str(config_file) in captured.err
    assert "PyYAML" in captured.err
    assert "Traceback" not in captured.err


def test_cli_help_still_works_with_a_broken_config(config_file, capsys):
    config_file.write_text("models: [unclosed\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "usage: review" in capsys.readouterr().out


def test_yaml_error_without_message_or_problem_still_raises_config_error(
    config_file, monkeypatch
):
    import yaml

    def boom(_text):
        raise yaml.YAMLError()

    config_file.write_text("pool: 3\n")
    monkeypatch.setattr(yaml, "safe_load", boom)
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    assert "YAMLError" in str(exc.value)


def test_config_is_read_as_utf_8_whatever_the_locale(config_file, monkeypatch):
    config_file.write_bytes("# обзор — ревью\npool: 3\n".encode())
    real_read_text = Path.read_text

    def ascii_locale_read_text(self, encoding=None, errors=None):
        return real_read_text(self, encoding=encoding or "ascii", errors=errors)

    monkeypatch.setattr(Path, "read_text", ascii_locale_read_text)
    assert config.load_config() == {"pool": 3}


def test_config_that_is_not_utf_8_raises(config_file):
    config_file.write_bytes(b"pool: 3\n# \xff\xfe\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    assert str(config_file) in str(exc.value)


def test_config_error_is_a_package_export():
    import reviewlib

    assert reviewlib.ConfigError is config.ConfigError


if __name__ == "__main__":
    # smoke.py runs each unit file as a script; this file needs pytest fixtures, so delegate
    # to pytest (tests/conftest.py's isolation still applies).
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
