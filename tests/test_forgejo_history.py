"""Stored pre-migration identities match their canonical repository; standalone CI suite."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reviewlib import stats  # noqa: E402

OLD = "github.com/alex-mextner/review-cli"
NEW = "git.hyperide.ai/ultrabricks/review-cli"


def test_historical_repo_identity_matches_without_rewriting_record():
    for stored, current in [(OLD, NEW), (NEW, OLD)]:
        record = {"repo_id": stored, "diff_files": ["reviewlib/stats.py"]}
        assert stats._classify_iteration_identity(record, current, frozenset(["reviewlib/stats.py"])) == ("verified", None)
        assert record["repo_id"] == stored


def test_other_repo_cannot_borrow_historical_review():
    for stored in ["github.com/other/review-cli", "github.com/alex-mextner/task-cli", "git.hyperide.ai/other/review-cli", "arbitrary-repo"]:
        record = {"repo_id": stored, "diff_files": ["reviewlib/stats.py"]}
        assert stats._classify_iteration_identity(record, NEW, frozenset(["reviewlib/stats.py"])) == ("mismatched", "repo_mismatch")


def test_alias_does_not_erase_diff_identity_check():
    record = {"repo_id": OLD, "diff_files": ["old.py"]}
    bucket, reason = stats._classify_iteration_identity(record, NEW, frozenset(["new.py"]))
    assert bucket == "mismatched"
    assert reason != "repo_mismatch"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("OK: historical Forgejo identity scenarios")
