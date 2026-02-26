#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAPTURE_BIN_DEFAULT="${ROOT}/scripts/tce-capture"
CAPTURE_BIN="$CAPTURE_BIN_DEFAULT"
PERSONA_MODE="${TCE_ADVISOR_PERSONA_MODE:-normal}"
SESSION_ID="chat-$(date +%s)"
SHOW_JSON="false"
ENSURE_CLONE_MODE="true"
EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: ./scripts/chat_with_advisor.sh [options]

Options:
  --session-id <id>      Session id to persist advisor state
  --persona-mode <mode>  normal|naruto|shadow
  --capture-bin <path>   Path to tce-capture launcher (default: scripts/tce-capture)
  --show-json            Print raw advisor JSON per turn
  --no-ensure-clone-mode Do not force runtime mode to clone_advisor on start
  --help                 Show help

Chat commands:
  /state                 Show advisor state for this session
  /stop                  Stop advisor for this session
  /reset                 Reset advisor state file for this session
  /exit                  Exit chat loop
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --session-id)
      SESSION_ID="${2:-}"
      shift
      ;;
    --persona-mode)
      PERSONA_MODE="${2:-normal}"
      shift
      ;;
    --capture-bin)
      CAPTURE_BIN="${2:-$CAPTURE_BIN_DEFAULT}"
      shift
      ;;
    --show-json)
      SHOW_JSON="true"
      ;;
    --no-ensure-clone-mode)
      ENSURE_CLONE_MODE="false"
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      EXTRA_ARGS=("$@")
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [ -z "$SESSION_ID" ]; then
  echo "session id cannot be empty" >&2
  exit 1
fi

if [ ! -x "$CAPTURE_BIN" ]; then
  if command -v tce-capture >/dev/null 2>&1; then
    CAPTURE_BIN="tce-capture"
  else
    echo "tce-capture launcher not found. Run ./scripts/install.sh first." >&2
    exit 1
  fi
fi

echo "advisor chat session: ${SESSION_ID}"
echo "persona mode: ${PERSONA_MODE}"
if [ "$ENSURE_CLONE_MODE" = "true" ] && [ -x "${ROOT}/scripts/set_mode.sh" ]; then
  if "${ROOT}/scripts/set_mode.sh" clone_advisor >/dev/null 2>&1; then
    echo "runtime mode: clone_advisor"
  else
    echo "runtime mode: unchanged (could not switch to clone_advisor)"
  fi
fi
echo "type '/exit' to quit"
echo

while true; do
  printf "you> "
  if ! IFS= read -r line; then
    echo
    break
  fi

  case "$line" in
    "")
      continue
      ;;
    /exit|/quit)
      break
      ;;
    /state)
      "$CAPTURE_BIN" advisor-state --session-id "$SESSION_ID" --persona-mode "$PERSONA_MODE"
      continue
      ;;
    /stop)
      "$CAPTURE_BIN" advisor-stop --session-id "$SESSION_ID" --persona-mode "$PERSONA_MODE"
      continue
      ;;
    /reset)
      "$CAPTURE_BIN" advisor-reset --session-id "$SESSION_ID"
      continue
      ;;
  esac

  err_file="$(mktemp)"
  if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
    if ! response_json="$("$CAPTURE_BIN" advisor-chat "$line" --session-id "$SESSION_ID" --persona-mode "$PERSONA_MODE" "${EXTRA_ARGS[@]}" 2>"$err_file")"; then
      echo "advisor> command failed (API unavailable or config issue)"
      rm -f "$err_file"
      continue
    fi
  else
    if ! response_json="$("$CAPTURE_BIN" advisor-chat "$line" --session-id "$SESSION_ID" --persona-mode "$PERSONA_MODE" 2>"$err_file")"; then
      echo "advisor> command failed (API unavailable or config issue)"
      rm -f "$err_file"
      continue
    fi
  fi
  rm -f "$err_file"

  if [ "$SHOW_JSON" = "true" ]; then
    printf '%s\n' "$response_json"
    continue
  fi

  RESPONSE_JSON="$response_json" python3 - <<'PY'
import json
import os

raw = os.environ.get("RESPONSE_JSON", "")
try:
    payload = json.loads(raw)
except Exception:
    print("advisor> invalid response payload")
    raise SystemExit(0)

activation = payload.get("activation") or {}
ack = activation.get("persona_ack")
if isinstance(ack, str) and ack.strip():
    print(f"advisor> {ack.strip()}")

auto_handoff = payload.get("auto_handoff") or {}
if isinstance(auto_handoff, dict) and auto_handoff:
    note = auto_handoff.get("note")
    if isinstance(note, str) and note.strip():
        print(f"advisor> {note.strip()}")

final_response = payload.get("final_response")
advisor_suggestion = payload.get("advisor_suggestion")
clone_advice = payload.get("clone_advice") or {}
guidance_summary = clone_advice.get("guidance_summary") if isinstance(clone_advice, dict) else None
note = payload.get("note")
action = payload.get("action", "unknown")

text = None
for candidate in (final_response, advisor_suggestion, guidance_summary, note):
    if isinstance(candidate, str) and candidate.strip():
        text = candidate.strip()
        break

if text:
    print(f"advisor> {text}")
else:
    print(f"advisor> ({action})")
PY
done

echo "chat session ended: ${SESSION_ID}"
