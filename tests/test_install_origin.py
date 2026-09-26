"""install.sh pulls and installs whatever its clone's origin serves, so it must recognise the real
repo by HOST and PATH — never by substring — before trusting a clone. These cases run the origin
functions of the real install.sh in bash, isolated from the rest of the installer. Same
plain-test_*-with-__main__ harness as the other unit files (tests/smoke.py runs it)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
_BASH = shutil.which("bash")


def _origin_functions() -> tuple[str, str]:
    text = INSTALL_SH.read_text(encoding="utf-8")
    repo = re.search(r'^REPO="([^"]+)"$', text, re.M)
    block = re.search(
        r"^_url_host_path\(\) \{.*?^_origin_is_legacy\(\) \{.*?^\}$", text, re.M | re.S
    )
    assert repo and block, "install.sh: REPO= or the _url_host_path.._origin_is_legacy block moved"
    return repo.group(1), block.group(0)


def _classify(url: str) -> tuple[bool, bool]:
    repo, block = _origin_functions()
    script = (
        f"REPO={repo}\n{block}\n"
        'c=no; l=no; _origin_is_canonical "$1" && c=yes; _origin_is_legacy "$1" && l=yes\n'
        'echo "$c $l"\n'
    )
    out = subprocess.run(
        [_BASH, "-c", script, "_", url], capture_output=True, text=True, timeout=30, check=True
    )
    canonical, legacy = out.stdout.split()
    return canonical == "yes", legacy == "yes"


CANONICAL = [
    "https://git.hyperide.ai/ultrabricks/{r}.git",
    "https://git.hyperide.ai/ultrabricks/{r}",
    "https://git.hyperide.ai/ultrabricks/{r}/",
    "https://alex@git.hyperide.ai/ultrabricks/{r}.git",
    "https://git.hyperide.ai:443/ultrabricks/{r}.git",
    "git@git.hyperide.ai:ultrabricks/{r}.git",
    "ssh://git@git.hyperide.ai:2222/ultrabricks/{r}.git",
]
LEGACY = [
    "https://github.com/alex-mextner/{r}.git",
    "git@github.com:alex-mextner/{r}.git",
    "https://github.com/ultrabricks/{r}",
]
FOREIGN = [
    "https://git.hyperide.ai.evil.example/ultrabricks/{r}.git",
    "https://hyperide.ai.evil.example/ultrabricks/{r}.git",
    "https://git.hyperide.ai@evil.example/ultrabricks/{r}.git",
    "https://evil.example\\@git.hyperide.ai/ultrabricks/{r}.git",
    "https://evil.example/git.hyperide.ai/ultrabricks/{r}.git",
    "https://evil.example/x@git.hyperide.ai/ultrabricks/{r}.git",
    "https://git.hyperide.ai/ultrabricks/{r}-evil.git",
    "https://git.hyperide.ai/other/{r}.git",
    "https://github.com.evil.example/alex-mextner/{r}.git",
    "https://evil.example/github.com/alex-mextner/{r}.git",
    "/home/u/src/{r}",
]
CASES = (
    [(u, (True, False)) for u in CANONICAL]
    + [(u, (False, True)) for u in LEGACY]
    + [(u, (False, False)) for u in FOREIGN]
)


def test_install_sh_recognises_the_repo_by_host_and_path():
    if _BASH is None:
        print("SKIP test_install_sh_recognises_the_repo_by_host_and_path: needs bash")
        return
    repo, _ = _origin_functions()
    wrong = []
    for url, want in CASES:
        got = _classify(url.format(r=repo))
        if got != want:
            wrong.append((url, got, want))
    assert not wrong, wrong


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
