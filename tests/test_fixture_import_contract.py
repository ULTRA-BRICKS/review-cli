"""Importing a test module must not replace the product's live functions."""
from pathlib import Path
import subprocess
import sys


def test_run_stats_module_import_preserves_product_bindings():
    source = Path(__file__).resolve().parents[1]
    probe = r'''
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path.cwd() / "tests"))
from reviewlib import backends, cli, panel
modules = (backends, cli, panel)
before = {(module.__name__, name): value for module in modules
          for name, value in vars(module).items() if callable(value)}
spec = importlib.util.spec_from_file_location("isolated_stats_fixture_probe", "tests/test_run_stats.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
after = {(module.__name__, name): value for module in modules
         for name, value in vars(module).items() if callable(value)}
changed = sum(before[key] is not after.get(key) for key in before)
print("changed_product_bindings=" + str(changed))
raise SystemExit(1 if changed else 0)
'''
    result = subprocess.run([sys.executable, '-I', '-c', probe], cwd=source,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout
