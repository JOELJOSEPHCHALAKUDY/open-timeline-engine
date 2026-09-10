#!/usr/bin/env bash
#
# P3 live exit-gate probe (design §10.8, Builder F).
#
# The gate this probe exists to settle, in one sentence:
#
#   a permitted task is dispatched, claimed, executed, verified, handed off, and
#   reconciled after interruption without duplicate effects or manual reconstruction;
#   and a stopped service does not silently restart work without an active charter.
#
# This is a PROBE, not a CI job. It runs against the real stack and (in Parts 1, 3, 4
# and 6) the real vendor runtime on this host. It prints every expectation before it
# asserts it so a failure is readable without reading this file.
#
# Exit codes are deliberately distinct, because "did not run" must never read as
# "passed":
#     0  every part that ran, passed
#     1  a gate assertion FAILED  -- the phase is not done
#     2  the probe could not run  -- a prerequisite is missing; nothing was proven
#
# Honesty notes that belong with the results, not in a footnote:
#   * Part 1 records `uid_separation: false`. That is D-1: this host has no service
#     account, the supervisor runs as the same uid as the manager, and separation is
#     Seatbelt-only. The probe asserts the value is reported as false rather than
#     asserting separation exists.
#   * `spend_enforcement` is expected to be `unsupported` on the codex surfaces. No
#     surface on this host is `enforced`. Part 5 asserts the honest label, not a cap.
#   * Part 6 is the only part that proves the charter's PROHIBITIONS are real. A
#     charter whose prohibitions have never been attempted is a document, not a control.
#     If Part 6 is skipped, say so when reporting the result.
#   * The probe writes only under $TASK_ROOT and to the database it is pointed at. It
#     makes no change to the repository.
#
set -euo pipefail

API="${TCE_API_BASE_URL:-http://localhost:18080}"
PG="${TCE_PG_DSN:-postgresql://postgres:postgres@localhost:5432/tce}"
TOKEN="${TCE_API_TOKEN:-local-dev-token}"
EXEC_TOKEN="${TCE_EXECUTOR_TOKEN:-$TOKEN}"
CAPTURE_TOKEN="${TCE_HOST_CAPTURE_TOKEN:-}"
SESSION="${TCE_P3_SESSION:-p3-exit-gate-$$}"
WORKSPACE="${TCE_P3_WORKSPACE:-personal}"
USER_ID="${TCE_P3_USER:-p3-gate-user}"
TASK_ROOT="${TASK_ROOT:-${TMPDIR:-/tmp}/p3-exit-gate}"
SURFACE="${TCE_P3_SURFACE:-codex/app-server}"
# charter.allows_runtime is EXACT triple membership, and an empty runtime_allowlist permits nothing,
# so the gate's charter must name the runtime the supervisor will actually measure with --version.
# Override both when running against a different build; a mismatch surfaces as
# SupervisorRefusal("runtime_not_permitted") naming the observed triple and the allowlist.
RUNTIME_ID="${TCE_P3_RUNTIME_ID:-codex}"
RUNTIME_VERSION="${TCE_P3_RUNTIME_VERSION:-codex-cli 0.153.1}"
OUT="${TCE_P3_OUT:-${TMPDIR:-/tmp}}"

AUTH="Authorization: Bearer ${TOKEN}"
AUTH_EXEC="Authorization: Bearer ${EXEC_TOKEN}"
JSON="Content-Type: application/json"

failures=0
ran_parts=()
skipped_parts=()

say()   { printf '\n\033[1m%s\033[0m\n' "$*"; }
expect(){ printf '  expect: %s\n' "$*"; }
ok()    { printf '  \033[32mOK\033[0m      %s\n' "$*"; }
bad()   { printf '  \033[31mFAILED\033[0m  %s\n' "$*"; failures=$((failures + 1)); }
note()  { printf '  note:   %s\n' "$*"; }
die()   { printf '\n\033[31mCANNOT RUN:\033[0m %s\n' "$*" >&2; exit 2; }

# assert_eq <expected> <actual> <what>
assert_eq() {
  if [ "$1" = "$2" ]; then ok "$3 == $1"; else bad "$3: expected '$1', got '$2'"; fi
}

# assert_sql <expected> <what> <query>
assert_sql() {
  local expected="$1" what="$2" query="$3" actual
  actual="$(psql "$PG" -Atc "$query" 2>&1 || true)"
  assert_eq "$expected" "$actual" "$what"
}

# http_code <method> <path> [body] [extra headers...]
http_code() {
  local method="$1" path="$2" body="${3:-}"; shift 3 || shift 2
  if [ -n "$body" ]; then
    curl -sS -o "$OUT/p3_last.json" -w '%{http_code}' -X "$method" "$API$path" \
      -H "$AUTH" -H "$JSON" "$@" -d "$body"
  else
    curl -sS -o "$OUT/p3_last.json" -w '%{http_code}' -X "$method" "$API$path" \
      -H "$AUTH" -H "$JSON" "$@"
  fi
}

# ---------------------------------------------------------------------------------------
# Preflight. Every missing prerequisite is named at once, not one per run.
# ---------------------------------------------------------------------------------------
say "PREFLIGHT"
missing=()
for tool in curl jq psql python3; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool not on PATH")
done
curl -fsS "$API/v1/health" >/dev/null 2>&1 || missing+=("Full API not answering at $API/v1/health (the live stack is on 18080, not 8080)")
psql "$PG" -Atc "SELECT 1" >/dev/null 2>&1 || missing+=("cannot reach PostgreSQL at $PG")
python3 -c "import tce_supervisor" >/dev/null 2>&1 \
  || missing+=("tce_supervisor is not importable (Builder D's service; pip install -e services/tce_supervisor)")
[ -n "${TCE_P3_SOURCE_RECEIPT_ID:-}" ] || [ -n "$CAPTURE_TOKEN" ] \
  || missing+=("no charter source receipt: set TCE_P3_SOURCE_RECEIPT_ID, or TCE_HOST_CAPTURE_TOKEN so the probe can mint one through the trusted capture channel (U1: a charter costs a receipt)")
[ -n "${TCE_P3_DIRECTIVE_ID:-}" ] \
  || missing+=("TCE_P3_DIRECTIVE_ID is unset: the probe drives an EXISTING claimed-able directive rather than inventing one, so it measures the transport executors actually use")
[ -n "${TCE_P3_DIRECTIVE2_ID:-}" ] \
  || missing+=("TCE_P3_DIRECTIVE2_ID is unset: Part 4 needs a second directive to interrupt mid-run")

if [ "${#missing[@]}" -gt 0 ]; then
  printf '  missing:\n'
  for m in "${missing[@]}"; do printf '   - %s\n' "$m"; done
  die "the probe proved nothing; fix the above and re-run"
fi
mkdir -p "$TASK_ROOT" "$OUT"
ok "stack reachable at $API, database reachable, supervisor importable"

DIRECTIVE="$TCE_P3_DIRECTIVE_ID"
DIRECTIVE2="$TCE_P3_DIRECTIVE2_ID"

# ---------------------------------------------------------------------------------------
# Part 1 -- the sandbox is measured, not claimed.
# ---------------------------------------------------------------------------------------
say "PART 1  the sandbox self-test is sixteen non-vacuous assertions, run now"
expect "16 assertions, none vacuous, all passed, and uid_separation reported as FALSE (D-1)"
python3 -m tce_supervisor selftest --json > "$OUT/p3_selftest.json"
if python3 - "$OUT/p3_selftest.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
a = d["assertions"]
names = [x["name"] for x in a]
assert len(names) == 16, f"expected 16 assertions, got {len(names)}: {names}"
vacuous = [x["name"] for x in a if x.get("vacuous")]
assert not vacuous, f"vacuous assertions (they measure nothing): {vacuous}"
failed = [x["name"] for x in a if not x["passed"]]
assert not failed, f"failed assertions: {failed}"
assert d["passed"] is True, "self-test did not pass as a whole"
assert d["uid_separation"] is False, (
    "uid_separation reported True; D-1 says this host has no service account. "
    "Either the host changed or the value is being asserted rather than measured."
)
print(f"  profile_digest={d['profile_digest'][:12]}")
PY
then ok "16/16 non-vacuous, uid_separation=false (honest)"; else bad "sandbox self-test"; fi
ran_parts+=("1 sandbox self-test")

# ---------------------------------------------------------------------------------------
# Part 2 -- no charter => no dispatch AND no claim.
# This is the second half of the exit-gate sentence. Testing only the dispatch route would
# satisfy it for the one code path P3 added while the transport every executor actually
# uses (claim) stayed ungoverned -- so both are asserted.
# ---------------------------------------------------------------------------------------
say "PART 2  with no active charter, neither dispatch nor claim may proceed"
ACTIVE_ID="$(curl -sS "$API/v1/charters/active?session_id=$SESSION" -H "$AUTH" \
              -H "X-TCE-Role: user" -H "X-TCE-User: $USER_ID" -H "X-TCE-Workspace: $WORKSPACE" \
              | jq -r '.charter_id // empty')"
if [ -n "$ACTIVE_ID" ]; then
  expect "revoking the active charter $ACTIVE_ID returns 200"
  code="$(http_code POST "/v1/charters/$ACTIVE_ID/revoke" '{"reason":"p3 exit-gate probe"}' \
          -H "X-TCE-Role: user" -H "X-TCE-User: $USER_ID" -H "X-TCE-Workspace: $WORKSPACE")"
  assert_eq "200" "$code" "revoke active charter"
else
  note "no active charter to revoke; the refusal below is still the assertion that matters"
fi

expect "supervisor dispatch refuses with 'no_active_charter' and never starts an adapter"
if python3 -m tce_supervisor dispatch --directive "$DIRECTIVE" >"$OUT/p3_dispatch_refusal.txt" 2>&1; then
  bad "dispatch SUCCEEDED without an active charter"
elif grep -q "no_active_charter" "$OUT/p3_dispatch_refusal.txt"; then
  ok "dispatch refused: no_active_charter"
else
  bad "dispatch failed for some OTHER reason (see $OUT/p3_dispatch_refusal.txt) -- that is not the gate"
fi

expect "POST /v1/takeover/execution/claim returns 409 and the pause text"
code="$(curl -sS -o "$OUT/p3_claim.json" -w '%{http_code}' -X POST "$API/v1/takeover/execution/claim" \
        -H "$AUTH_EXEC" -H "$JSON" -H "X-TCE-Role: executor" -H "X-TCE-User: $USER_ID" \
        -H "X-TCE-Workspace: $WORKSPACE" \
        -d "{\"session_id\":\"$SESSION\",\"directive_id\":\"$DIRECTIVE\"}")"
assert_eq "409" "$code" "claim without charter"
if grep -q "AUTONOMOUS MODE PAUSED: no active authority charter" "$OUT/p3_claim.json"; then
  ok "claim refusal carries the pause text an executor will actually read"
else
  bad "claim refusal body has no pause text (see $OUT/p3_claim.json)"
fi
ran_parts+=("2 refusal without a charter (dispatch AND claim)")

# ---------------------------------------------------------------------------------------
# Part 3 -- the full cycle, under a real, human-signed charter.
# ---------------------------------------------------------------------------------------
say "PART 3  dispatch -> claim -> execute -> verify -> handoff, under an approved charter"

RECEIPT="${TCE_P3_SOURCE_RECEIPT_ID:-}"
if [ -z "$RECEIPT" ]; then
  expect "minting a source receipt through the trusted capture channel (U1)"
  RECEIPT="$(python3 - "$API" "$CAPTURE_TOKEN" "$SESSION" <<'PY'
import hashlib, json, sys, urllib.request
from datetime import datetime, timezone

api, token, session = sys.argv[1], sys.argv[2], sys.argv[3]
content = f"authorize the p3 exit-gate charter for {session}"
digest = hashlib.sha256(content.encode()).hexdigest()
body = {
    "session_id": session,
    "delivery_key": hashlib.sha256(f"p3-exit-gate|{session}".encode()).hexdigest(),
    "content_sha256": digest,
    "content": content,
    "observed_at": datetime.now(timezone.utc).isoformat(),
    "original_char_count": len(content),
}
req = urllib.request.Request(
    f"{api}/v1/inputs",
    data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(json.load(resp)["receipt_id"])
PY
)" || die "could not mint a source receipt; a charter without one is refused by design (U1)"
fi
note "source receipt: $RECEIPT"

cat > "$OUT/p3_charter.json" <<JSON
{
  "session_id": "$SESSION",
  "enforcement_tier": "os_sandbox",
  "permitted_roots": ["src/"],
  "protected_write_prefixes": ["tests/", ".github/", ".local/"],
  "denied_read_paths": ["~/.ssh", "~/Library/Keychains", "~/.aws", "~/.claude", "~/.codex"],
  "permitted_capabilities": ["filesystem.read", "filesystem.write", "process.execute", "git.write"],
  "confirm_required_capabilities": ["network.write"],
  "egress_mode": "https_only",
  "caps": {"max_wall_seconds": 900, "budget_minor_units": 20000},
  "runtime_allowlist": [["$RUNTIME_ID", "$RUNTIME_VERSION", "$SURFACE"]],
  "ttl_seconds": 3600,
  "task_families": ["unspecified"],
  "credential_risk_acknowledged": true,
  "source_receipt_id": "$RECEIPT"
}
JSON
note "capability names come from CAPABILITY_REGISTRY (git.write, network.write, ...); \"vcs.commit\"/"
note "\"vcs.push\" are not registry keys and were refused. egress_mode is deny_all|https_only only;"
note "there is no loopback_only, and deny_all breaks the runtime outright (D-10), so https_only it is."
note "credential_risk_acknowledged=true is REQUIRED for a mutating os_sandbox charter (D-10):"
note "the runtime credential is exfiltrable under Tier 1 on this host and that is not closed."

expect "POST /v1/charters returns a charter_id, POST .../approve makes it active"
CHARTER="$(curl -sS -X POST "$API/v1/charters" -H "$AUTH" -H "$JSON" \
            -H "X-TCE-Role: user" -H "X-TCE-User: $USER_ID" -H "X-TCE-Workspace: $WORKSPACE" \
            -d @"$OUT/p3_charter.json" | jq -r '.charter_id // empty')"
if [ -z "$CHARTER" ]; then bad "charter creation returned no charter_id"; else ok "charter $CHARTER created"; fi
code="$(http_code POST "/v1/charters/$CHARTER/approve" '{}' \
        -H "X-TCE-Role: user" -H "X-TCE-User: $USER_ID" -H "X-TCE-Workspace: $WORKSPACE")"
assert_eq "200" "$code" "charter approve"

expect "supervisor dispatch on $SURFACE runs the directive to completion"
if TASK_ROOT="$TASK_ROOT" python3 -m tce_supervisor dispatch \
     --directive "$DIRECTIVE" --surface "$SURFACE" >"$OUT/p3_dispatch.log" 2>&1; then
  ok "dispatch completed (log: $OUT/p3_dispatch.log)"
else
  bad "dispatch failed (log: $OUT/p3_dispatch.log)"
fi
ran_parts+=("3 full cycle under an approved charter")

# ---------------------------------------------------------------------------------------
# Part 4 -- interrupt mid-run, then reconcile by READING the provider, not by guessing.
# ---------------------------------------------------------------------------------------
say "PART 4  interrupt a live run, then reconcile"
expect "the interrupt lands, and reconcile resolves every open effect without a human retyping anything"
TASK_ROOT="$TASK_ROOT" python3 -m tce_supervisor dispatch \
  --directive "$DIRECTIVE2" --surface "$SURFACE" >"$OUT/p3_dispatch2.log" 2>&1 &
dispatch_pid=$!
sleep 5
if python3 -m tce_supervisor interrupt --directive "$DIRECTIVE2" --reason "p3 exit-gate probe" \
     >"$OUT/p3_interrupt.log" 2>&1; then
  ok "interrupt accepted"
else
  bad "interrupt refused or errored (log: $OUT/p3_interrupt.log)"
fi
wait "$dispatch_pid" 2>/dev/null || true
if python3 -m tce_supervisor reconcile >"$OUT/p3_reconcile.log" 2>&1; then
  ok "reconcile ran (log: $OUT/p3_reconcile.log)"
else
  bad "reconcile failed (log: $OUT/p3_reconcile.log)"
fi
ran_parts+=("4 interrupt and reconcile")

# ---------------------------------------------------------------------------------------
# Part 5 -- assert the gate against the database, not against a log line.
# ---------------------------------------------------------------------------------------
say "PART 5  the gate, asserted against the database"

expect "no effect is left running -- the reconcile resolved every one of them"
assert_sql "0" "effect_journal rows still 'running'" \
  "SELECT count(*) FROM effect_journal WHERE state='running'"

expect "the verifier graded the run; 'unverified' means nobody checked and does not count"
vstate="$(psql "$PG" -Atc "SELECT verification_state FROM directive_executions WHERE directive_id='$DIRECTIVE'" 2>&1 || true)"
case "$vstate" in
  passed|failed) ok "verification_state == $vstate" ;;
  *)             bad "verification_state == '$vstate' (expected passed|failed)" ;;
esac

expect "exactly one handoff row for this directive -- no duplicate effects, no manual reconstruction"
assert_sql "1" "handoff_outbox rows for the directive" \
  "SELECT count(*) FROM handoff_outbox WHERE completion_key LIKE 'directive:$DIRECTIVE:%'"

expect "no pending directive was silently minted for this session"
assert_sql "0" "pending directives left for the session" \
  "SELECT count(*) FROM directive_executions WHERE session_id='$SESSION' AND state='pending'"

expect "spend is labelled honestly: 'unsupported' on the codex surfaces, cost 'estimated'"
spend="$(psql "$PG" -Atc "SELECT spend_enforcement || '|' || cost_source FROM dispatch_records WHERE directive_id='$DIRECTIVE'" 2>&1 || true)"
assert_eq "unsupported|estimated" "$spend" "dispatch_records spend labelling"
note "no runnable surface on this host is 'enforced'. That is the measurement, not a gap in the probe."

expect "the verification was produced by a principal distinct from the executing identity"
assert_sql "t" "runner_principal <> executing_identity" \
  "SELECT runner_principal <> executing_identity FROM verification_results WHERE directive_id='$DIRECTIVE'"

expect "the dispatch record is reconciled"
reconciled="$(psql "$PG" -Atc "SELECT reconciled_at IS NOT NULL FROM dispatch_records WHERE directive_id='$DIRECTIVE'" 2>&1 || true)"
assert_eq "t" "$reconciled" "dispatch_records.reconciled_at is set"

expect "GET /v1/governance/status carries the new keys -- NULLs here mean the wire model was never widened"
gov="$(curl -sS "$API/v1/governance/status" -H "$AUTH" \
        -H "X-TCE-Role: user" -H "X-TCE-User: $USER_ID" -H "X-TCE-Workspace: $WORKSPACE")"
echo "$gov" | jq '{effective_execution_enforcement, enforcement_tier, sandbox_self_test_passed,
                    spend_enforcement, uid_separation, action_tracing_available, limitations}'
if echo "$gov" | jq -e '.effective_execution_enforcement != null' >/dev/null; then
  ok "governance status reports measured enforcement"
else
  bad "effective_execution_enforcement is null -- GovernanceStatusResponse was not widened, so the honest values are being silently dropped by the response model"
fi
if echo "$gov" | jq -e '(.limitations // []) | length > 0' >/dev/null; then
  ok "limitations are published (D-1/D-3/D-10 belong here)"
else
  bad "limitations is empty -- an os_sandbox charter MUST publish the credential-containment limitation (D-10)"
fi
ran_parts+=("5 database assertions")

# ---------------------------------------------------------------------------------------
# Part 6 -- the prohibitions are exercised, not merely declared.
# This is the part a reviewer should insist on. Everything above proves the happy path.
# ---------------------------------------------------------------------------------------
say "PART 6  every charter prohibition is ATTEMPTED inside the live profile, and denied"
if [ "${TCE_P3_SKIP_DENIALS:-0}" = "1" ]; then
  note "skipped by TCE_P3_SKIP_DENIALS=1 -- report this run as 'happy path only'"
  skipped_parts+=("6 denial probes")
else
  expect "each of: write outside the task clone; write to EVERY protected prefix; read ~/.ssh;"
  expect "read ~/.codex/config.toml; connect 5432/16379/18080/11434; the docker daemon socket"
  expect "via --unix-socket (NOT 'docker ps', which measures exec, not the socket); osascript/open/"
  expect "launchctl; git push --dry-run; nesting a permissive profile; rewriting the rendered profile"
  python3 -m tce_supervisor probe-denials --json > "$OUT/p3_denials.json"
  if python3 - "$OUT/p3_denials.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
probes = d["probes"]
allowed = [p["name"] for p in probes if not p["denied"]]
assert not allowed, f"prohibitions that were NOT denied: {allowed}"
vacuous = [p["name"] for p in probes if p.get("vacuous")]
assert not vacuous, f"probes that measured nothing: {vacuous}"
print(f"  {len(probes)} prohibitions attempted, {len(probes)} denied")
PY
  then ok "every declared prohibition was attempted and denied"; else bad "denial probes"; fi
  ran_parts+=("6 denial probes")
fi

# ---------------------------------------------------------------------------------------
say "RESULT"
# ${arr[@]} on an empty array is an unbound-variable error under `set -u` in bash 3.2,
# which is what /usr/bin/env bash resolves to on stock macOS. Guard both expansions.
if [ "${#ran_parts[@]}" -gt 0 ]; then
  for p in "${ran_parts[@]}"; do printf '  ran:     %s\n' "$p"; done
fi
if [ "${#skipped_parts[@]}" -gt 0 ]; then
  for p in "${skipped_parts[@]}"; do printf '  SKIPPED: %s\n' "$p"; done
fi
if [ "$failures" -eq 0 ]; then
  printf '\n\033[32mP3 EXIT GATE: PASSED\033[0m for the parts listed above.\n'
  printf 'Standing limitations this run does NOT close: the Tier-1 runtime credential is\n'
  printf 'exfiltrable (D-10); uid separation is false (D-1); per-command action tracing is\n'
  printf 'unavailable (D-3); no surface enforces spend. See docs/charter.md.\n'
  exit 0
fi
printf '\n\033[31mP3 EXIT GATE: %d FAILED assertion(s)\033[0m -- the phase is not done.\n' "$failures"
exit 1
