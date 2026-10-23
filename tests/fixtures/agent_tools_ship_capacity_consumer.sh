# Verbatim copy of agent-tools ci/ship/ship.sh's _review_capacity_all_exhausted (the
# consumer of `review capacity-check --json`, agent-tools#705) at agent-tools commit
# 0794222c91b929d8f2eb88f9836f67d66978b609. tests/test_capacity_check_contract.py runs it
# against this repo's real report and fails when the live agent-tools copy drifts from it.
_review_capacity_all_exhausted() {
  command -v review >/dev/null 2>&1 || { echo "0"; return 0; }
  command -v jq >/dev/null 2>&1 || { echo "0"; return 0; }
  local json rc=0
  json=$(review capacity-check --json 2>/dev/null) || rc=$?
  if [ "$rc" != "0" ] || [ -z "$json" ]; then echo "0"; return 0; fi
  local all_ex
  all_ex=$(printf '%s' "$json" | jq -r 'if .all_exhausted == true then "true" else "false" end' 2>/dev/null) || { echo "0"; return 0; }
  if [ "$all_ex" != "true" ]; then echo "0"; return 0; fi
  local evidence
  evidence=$(printf '%s' "$json" | jq -r '[.exhausted[]? | "\(.model): \(.evidence // .source // "exhausted")"] | join("; ")' 2>/dev/null) || evidence=""
  printf '1\t%s\n' "$evidence"
}
