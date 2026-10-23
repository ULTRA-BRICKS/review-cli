# `scripts/review-remote` — run a review on another box, keep every local gate

The host-wide admission queue (`review jobs`) serialises every local agent's review behind one
machine's CPU/RAM and provider seats. `review-remote` runs the SAME `review` on a second box that
has its own logins (`REVIEW_REMOTE=user@host`, key-only ssh, `review`/`omp`/`git` on the box's
non-interactive PATH) and brings the evidence home:

```bash
export REVIEW_REMOTE=ultra@65.21.40.51
review-remote diff --staged --task CODE -C <repo>           # same args as `review`
review-remote diff --task CODE -C <repo> --detach           # job-id now; `review-remote wait|status <job>`
git diff origin/main...HEAD | review-remote diff --task CODE -C <repo>   # review a committed branch
```

Supported modes: `diff`, `just-ask`/`ask`, `quorum`, `brainstorm`. `-o FILE` and a piped diff on
stdin work as locally (with `--detach` the piped diff is spooled first); `task`/`stat`/`visual`
stay local (the imported records are already in the local store). Limits, all refused loudly: the
repo needs an `origin` remote (records are keyed by it), and `--commit` is not supported (the
checkpoint commit would land in the box's throwaway checkout, not yours). Install: `ln -sfn
<checkout>/scripts/review-remote ~/.local/bin/review-remote`.

How it stays honest: the repo state is shipped (HEAD to a bare mirror on the box, then the staged
patch, the unstaged patch and untracked files re-applied) and the run is REFUSED unless the box's
`git diff --cached` / `git diff HEAD` hashes equal the local ones. The box runs with a per-job
`REVIEW_STATS_FILE`/`REVIEW_MARKER`; afterwards the wrapper (a) appends the box's own run-stats
records unmodified (plus `remote_host`) to the local run-stats, which is what `review task CODE
--check` / `gh ship` count, (b) touches the commit-gate marker only if the box's staged review
wrote it, and (c) installs the box's `review-stamp` only if its digest equals the sha256 of the
local `git diff --no-ext-diff --cached` and its HEAD equals the local HEAD. No box record, no
import. Reviewer seats and model auth are the box's own (`review --show-board` there); the box
needs no secrets from this repo.

Admission: review-cli's host-wide admission is strict FIFO and gates on the box's load average, so a
build box at load 30+ parks every review for hours even though a review is ~0.05 cpu of API
waiting. `review-remote` therefore runs its jobs in their OWN admission lane (a separate
`REVIEW_ADMISSION_DIR` on the box, so they neither queue behind nor block the box's other reviews)
with `REVIEW_ADMISSION_MIN_SLOTS` (default 12, override with `REVIEW_REMOTE_MIN_SLOTS`) so up to
that many runs start immediately regardless of load. Provider seat limits and 429 backoff apply
WITHIN the lane only (that state lives in the admission dir), so reviews run on the box outside
`review-remote` do not share them — keep the box's other review traffic off the same providers, or
point both at one lane. Throughput is then bounded by the providers (a throttled `zai` seat pool
is the usual limit), not by the box. `review-remote queue` shows the lane (`REVIEW_REMOTE` must be
set). `--commit`, `--visual` and `--before` are refused (they act on local state the box lacks).

---
