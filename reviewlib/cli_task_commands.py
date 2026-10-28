"""Review CLI command implementation extracted from the compatibility facade.

Dependencies are supplied explicitly by the facade so existing library/test
patch seams remain effective. No model, quota or admission policy changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import argparse

def _task_transfer(_api, /, ns: argparse.Namespace) -> int:
    """`review task CODE --export` / `--import FILE` / `--import-from HOST` (review-cli#659)."""
    invalid = _validate_transfer(_api, ns)
    if invalid is not None:
        return invalid
    code = None
    if ns.code:
        try:
            code = _api.normalize_task_code(ns.code)
        except ValueError as exc:
            print(f"[review task] invalid task code: {exc}", file=_api.sys.stderr)
            return 2
    if ns.export:
        return _export_task_records(_api, code)
    status, lines, origin = _read_transfer_input(_api, ns, code)
    if status:
        return status
    try:
        result = _api.stats_import_records(
            lines, remote_host=origin, task_code=code if ns.import_from else None
        )
    except OSError as exc:
        print(f"[review task] cannot write the run-stats store: {exc}", file=_api.sys.stderr)
        return 1
    print(
        f"[review task] imported {result['imported']}, skipped {result['skipped']} "
        f"already recorded, rejected {result['rejected']} line(s) "
        "(not a record, or another task's)"
    )
    return 0


def _task_subcommand(_api, /, rest: list[str]) -> int:
    """`review task CODE [--detail N|SESSION] [--json]` — task-scoped review history.

    The run-stats JSONL is the authoritative iteration list; dashboard logs provide
    transcript detail when they are still present. Both stores are private local files.
    """
    if "--task" in rest or any(arg.startswith("--task=") for arg in rest):
        print(
            "[review task] use positional CODE for history lookup: review task CODE. "
            "--task is for recorded review modes.",
            file=_api.sys.stderr,
        )
        return 2
    parser = _api.argparse.ArgumentParser(
        prog="review task",
        description="Show review iterations, models, and transcripts for one task code.",
    )
    parser.add_argument("code", nargs="?", help="task/issue code, e.g. HYP-742")
    parser.add_argument(
        "--detail",
        metavar="N|SESSION",
        help="print the full transcript for an iteration number or session id",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 iff the task has enough PASSED recorded iterations across enough "
        "distinct board roles (self-merge-authority gate, default) or distinct "
        "models (when --min-models is explicitly given); fail-closed on "
        "missing/unreadable history or pre-verdict-field records; see "
        "--min-iter/--min-models/--min-roles and `review help task-check` for "
        "the full pass/fail logic (incl. the role-tracking fallback) and the "
        "complete --json field-by-field contract",
    )
    parser.add_argument(
        "--min-iter",
        type=int,
        default=3,
        help="review-bar floor: PASSED recorded iterations (default 3)",
    )
    parser.add_argument(
        "--min-models",
        type=int,
        default=None,
        help="review-bar floor: distinct models among the PASSED iterations. Not "
        "enforced by default (review-cli#246: role-based coverage is the "
        "default instead — see --min-roles). When explicitly given, this floor "
        "is ALWAYS enforced — including alongside --min-roles, where BOTH "
        "floors must then be met (an explicit request is never silently "
        "dropped) — and, when there's real review history to check it against, "
        "a non-blocking advisory is printed noting that role-based coverage is "
        "usually sufficient on its own.",
    )
    parser.add_argument(
        "--min-roles",
        type=int,
        default=None,
        help="review-cli#221: review-bar floor: distinct BOARD ROLES (architect/"
        "correctness/security/…) covered among the PASSED iterations, instead of "
        "distinct model NAMES. This is the DEFAULT check (review-cli#246) when "
        "neither --min-models nor --min-roles is given, at the same numeric "
        "floor --min-models used to default to (3) — pass this flag explicitly "
        "only to override that number. A role filled by the board's "
        "shortage-resilience duplicate-model pad (PR #207: one model reused "
        "onto an otherwise-empty role when too few distinct models are "
        "available) counts once per role, same as any other role, even though "
        "it repeats a model --min-models already counted. Only iterations from "
        "a mode that records per-seat roles (currently: the review-diff/visual "
        "board dispatch) contribute.",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        default=".",
        help="--check only: repo to verify recorded iterations against "
        "(diff-identity binding — see reviewlib.stats module docstring)",
    )
    identity_flags = parser.add_mutually_exclusive_group()
    identity_flags.add_argument(
        "--no-verify-identity",
        action="store_true",
        help="--check only: skip repo/diff mismatch detection entirely (legacy "
        "behavior — every PASSED iteration counts, exactly as before this gate "
        "existed). Escape hatch for a cwd that can't resolve a repo; NOT a way "
        "around a genuine mismatch finding.",
    )
    identity_flags.add_argument(
        "--verify-repo-only",
        action="store_true",
        help="--check only: verify recorded iterations by repository alone. "
        "Iterations recorded in another repository (even under the '#'-twin "
        "code) are excluded; same-repo iterations count whatever files they "
        "touched. For a caller like `gh ship` that checks a PR from a checkout "
        "whose own diff is unrelated to it (review-cli#589).",
    )
    transfer = parser.add_argument_group(
        "moving iterations between machines (review-cli#659)",
        "A review recorded on another machine (the build box) counts for --check here "
        "once its records are imported; each record keeps its repo/diff identity, so "
        "--check verifies it against THIS checkout like any local one.",
    )
    transfer.add_argument(
        "--export",
        action="store_true",
        help="print the task's run-stats records as JSONL (needs CODE)",
    )
    transfer.add_argument(
        "--import",
        dest="import_file",
        metavar="FILE|-",
        help="append records printed by --export (a file, or - for stdin) to the local "
        "store; records already there are skipped, so importing twice is safe",
    )
    transfer.add_argument(
        "--import-from",
        dest="import_from",
        metavar="USER@HOST",
        help="ssh to HOST, run `review task CODE --export` there and import the result "
        "(needs CODE)",
    )
    transfer.add_argument(
        "--from-host",
        metavar="NAME",
        help="--import only: tag imported records with this origin (remote_host)",
    )
    ns = parser.parse_args(rest)

    if ns.export or ns.import_file is not None or ns.import_from is not None or ns.from_host is not None:
        return _transfer_request(_api, ns)
    if ns.check:
        return _check_task_request(_api, ns)
    if not ns.code:
        return _task_summary_list(_api, ns.json)
    return _task_history(_api, ns)


def _transfer_request(_api, ns):
    # A transfer may never swallow --check and return a misleading successful gate.
    flags = (("--check", ns.check), ("--json", ns.json), ("--detail", ns.detail),
             ("--no-verify-identity", ns.no_verify_identity), ("--verify-repo-only", ns.verify_repo_only))
    clash = [flag for flag, given in flags if given]
    if clash:
        print(f"[review task] {', '.join(clash)} cannot be combined with --export/--import/--import-from; transfer first, then run the check", file=_api.sys.stderr)
        return 2
    return _api._task_transfer(ns)


def _check_task_request(_api, ns):
    if not ns.code:
        print("[review task] --check requires a task CODE", file=_api.sys.stderr)
        return 2
    limits = (("--min-iter", ns.min_iter), ("--min-models", ns.min_models), ("--min-roles", ns.min_roles))
    supplied = [(flag, value) for flag, value in limits if value is not None]
    if any(value < 1 for _, value in supplied):
        clauses = [f"{flag} {value}" for flag, value in supplied]
        print("[review task] --min-iter must be >= 1, and any explicitly given --min-models/--min-roles must also be >= 1 "
              f"(got {' '.join(clauses)})", file=_api.sys.stderr)
        return 2
    identity_mode = "full"
    if ns.no_verify_identity:
        identity_mode = "disabled"
    elif ns.verify_repo_only:
        identity_mode = "repo_only"
    return _api._quorum_check_subcommand(ns.code, ns.min_iter, ns.min_models,
        min_roles=ns.min_roles, as_json=ns.json, cwd_raw=ns.cwd, identity_mode=identity_mode)


def _task_summary_list(_api, as_json):
    summaries = _api.task_summaries()
    if as_json:
        import json

        print(json.dumps({"tasks": summaries}, indent=2))
        return 0
    if not summaries:
        print("No task-coded review iterations found.")
        return 0
    print("Review tasks:")
    for item in summaries:
        models = ", ".join(item["models"]) or "-"
        suffix = "" if item["iterations"] == 1 else "s"
        print(f"  {item['task_code']}: {item['iterations']} iteration{suffix} · models: {models}")
    return 0


def _task_history(_api, ns):
    # Shape-only lookup remains available for historical records predating taskref.
    try:
        code = _api.normalize_task_code(ns.code)
    except ValueError as exc:
        print(f"[review task] invalid task code: {exc}", file=_api.sys.stderr)
        return 2
    assert code is not None
    iterations = _api.iterations_for_task(code)
    sessions = _api._task_sessions(code)
    if ns.detail:
        return _api._task_detail(code, ns.detail, iterations, sessions, as_json=ns.json)
    if ns.json:
        import json

        print(json.dumps({"task_code": code, "iterations": iterations,
                          "sessions": [session.to_summary() for session in sessions]}, indent=2))
        return 0
    if not iterations and not sessions:
        print(f"No review iterations found for task {code}.")
        return 1
    count = len(iterations) if iterations else len(sessions)
    print(f"Task {code}: {count} iteration{'' if count == 1 else 's'}")
    if iterations:
        _render_iteration_history(_api, iterations, sessions)
    else:
        for index, session in enumerate(sessions, start=1):
            print(f"  iteration {index}: {session.started.isoformat()} · {session.mode} "
                  f"· models: {', '.join(session.models) or '-'} · session {session.session_id}")
    print(f"\nDetail: review task {code} --detail <iteration|session_id>")
    return 0


def _render_iteration_history(_api, iterations, sessions):
    used = set()
    for item in iterations:
        session = _api._session_for_task_iteration(item, sessions, iterations, used)
        if session is not None:
            used.add(session.session_id)
        models = ", ".join(item.get("models") or []) or "-"
        duration = _api.fmt_duration(float(item.get("duration_seconds") or 0))
        suffix = f" · session {session.session_id}" if session else " · logs not found"
        print(f"  iteration {int(item['iteration'])}: {item.get('ts', '?')} · {item.get('mode', '?')} "
              f"· pool={item.get('pool_size', 0)} · models: {models} "
              f"· ok={item.get('ok_count', 0)} fail={item.get('fail_count', 0)} · {duration}{suffix}")


def _validate_transfer(_api, ns):
    choices = (bool(ns.export), ns.import_file is not None, ns.import_from is not None)
    if sum(choices) != 1:
        print("[review task] use exactly one of --export, --import, --import-from", file=_api.sys.stderr)
        return 2
    if ns.from_host is not None and ns.import_file is None:
        print("[review task] --from-host tags records of --import FILE; --import-from tags them with its own host", file=_api.sys.stderr)
        return 2
    if ns.import_file is not None and ns.code:
        print("[review task] --import takes no task CODE: it imports every record in the file (to pull one task: review task CODE --import-from HOST)", file=_api.sys.stderr)
        return 2
    if (ns.export or ns.import_from) and not ns.code:
        flag = "--export" if ns.export else "--import-from"
        print(f"[review task] {flag} requires a task CODE", file=_api.sys.stderr)
        return 2
    return None


def _export_task_records(_api, code):
    import json

    try:
        records = _api.stats_export_records(code)
    except OSError as exc:
        print(f"[review task] cannot read the run-stats store: {exc}", file=_api.sys.stderr)
        return 1
    for record in records:
        print(json.dumps(record, separators=(",", ":")))
    return 0



def _read_remote_transfer(_api, ns, code):
    if not ns.import_from or ns.import_from.startswith("-"):
        print("[review task] --import-from takes a host, not an option", file=_api.sys.stderr)
        return 2, [], None
    rc, out, err = _api._ssh_review(ns.import_from, ["task", code, "--export"])
    if rc != 0:
        print(f"[review task] `review task {code} --export` failed on {ns.import_from} "
              f"(rc={rc}): {err.strip() or 'no output'}", file=_api.sys.stderr)
        return rc if rc > 0 else 1, [], None
    return 0, out.splitlines(), ns.from_host or ns.import_from


def _read_transfer_input(_api, ns, code):
    if ns.import_from is not None:
        return _read_remote_transfer(_api, ns, code)
    if ns.import_file == "-":
        try:
            lines = _api.sys.stdin.buffer.read().decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            print(f"[review task] cannot read stdin: {exc}", file=_api.sys.stderr)
            return 1, [], None
    else:
        try:
            lines = _api.Path(ns.import_file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[review task] cannot read {ns.import_file}: {exc}", file=_api.sys.stderr)
            return 1, [], None
    return 0, lines, ns.from_host
