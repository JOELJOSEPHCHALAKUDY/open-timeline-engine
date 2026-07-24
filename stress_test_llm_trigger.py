#!/usr/bin/env python3
"""
TCE Takeover LLM Trigger Test
===============================
Crafts scenarios designed to force LOW confidence so the LLM
deliberation path (advisor_reason via clone_advice) actually fires.

Confidence formula (from takeover.py):
  confidence = 0.40 * objective_clarity
             + 0.25 * evidence_strength
             + 0.20 * outcome_stability
             + 0.15 * classifier_certainty

LLM fires when ALL of:
  1. should_trigger_deliberation = True  (conf < 0.78, objective changed, turn%6==0, failures>=2, or deliberation hints)
  2. decision_confidence < 0.70
  3. (not refresh_working_set) OR retrieval_reason == "explicit_deep_intent"

Strategy to force low confidence:
  - Use vague/short objectives ("ok", "hmm") → objective_clarity = 0.35-0.45
  - Fresh session with no evidence → evidence_strength = 0.42
  - Inject failures into recent_outcomes → outcome_stability drops
  - Use QUESTION/SUGGESTION classification → classifier_certainty = 0.62

Also: Use "investigate"/"research"/"analyze" keywords to force deliberation hints.
"""

import json
import time
from datetime import datetime

import requests

API_BASE = "http://localhost:8080/v1"
HEADERS = {
    "Authorization": "Bearer local-dev-token",
    "Content-Type": "application/json",
    "X-TCE-Consumer": "llm-trigger-test",
    "X-TCE-Role": "executor",
    "X-TCE-Workspace": "personal",
    "X-TCE-User": "local-user",
}

def takeover_step(session_id, message, turn):
    payload = {
        "message": message,
        "session_id": session_id,
        "activation_mode_default": "takeover",
        "persona_mode": "normal",
        "activation_keywords": "beru take over,beru takeover",
        "stop_keywords": "beru stand down,shadow stand down",
    }
    start = time.time()
    try:
        resp = requests.post(f"{API_BASE}/takeover/step", json=payload, headers=HEADERS, timeout=60)
        elapsed = (time.time() - start) * 1000
        if resp.status_code != 200:
            return {"turn": turn, "status": "error", "code": resp.status_code, "latency_ms": elapsed,
                    "error": resp.text[:300]}
        data = resp.json()
        state = data.get("state", {})
        tc = state.get("takeover_context", {})
        lb = data.get("latency_breakdown_ms", {}) or {}
        ca = data.get("clone_advice") or {}
        sg = data.get("selected_goal") or {}
        return {
            "turn": turn,
            "status": "ok",
            "latency_ms": elapsed,
            "api_total_ms": lb.get("total", 0),
            "active": state.get("active"),
            "mode": state.get("mode"),
            "turn_count": tc.get("turn_count"),
            "objective": tc.get("objective", "")[:60],
            "has_note": bool(data.get("note")),
            "classification": data.get("classification"),
            "decision_source": data.get("decision_source"),
            "decision_confidence": data.get("decision_confidence"),
            "safety_decision": data.get("safety_decision"),
            "continuity_ok": data.get("continuity_ok"),
            "needs_human": data.get("needs_human"),
            "context_quality_score": data.get("context_quality_score"),
            "retrieval_triggered": data.get("retrieval_triggered"),
            "retrieval_source": data.get("retrieval_source"),
            "retrieval_reason": data.get("retrieval_reason"),
            "retrieval_ms": lb.get("retrieval", 0),
            # Clone advice details
            "clone_confidence": ca.get("confidence") if isinstance(ca, dict) else None,
            "clone_evidence": ca.get("evidence_strength") if isinstance(ca, dict) else None,
            "clone_has_guidance": bool(ca.get("guidance_summary")) if isinstance(ca, dict) else False,
            "clone_has_advisor": bool(ca.get("advisor_decision")) if isinstance(ca, dict) else False,
            # Confidence components (if present)
            "conf_components": ca.get("confidence_components") if isinstance(ca, dict) else None,
            # Goal info
            "goal_title": sg.get("title") if isinstance(sg, dict) else None,
            "selection_score": sg.get("selection_score") if isinstance(sg, dict) else None,
            # Directive
            "directive_id": data.get("directive_id"),
            "directive_state": data.get("directive_state"),
            "execution_permit_required": data.get("execution_permit_required"),
        }
    except Exception as e:
        return {"turn": turn, "status": "exception", "latency_ms": (time.time()-start)*1000,
                "error": str(e)[:200]}

def claim_and_report_failure(session_id, directive_id):
    """Claim and report FAILURE to poison outcome stability."""
    try:
        resp = requests.post(f"{API_BASE}/takeover/execution/claim",
            json={"session_id": session_id, "directive_id": directive_id, "claimed_by": "llm-test"},
            headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return False
        resp2 = requests.post(f"{API_BASE}/takeover/execution/report",
            json={"session_id": session_id, "directive_id": directive_id,
                  "state": "failed", "result": "failure",
                  "failure_reason": "intentional test failure",
                  "details": {"summary": "Stress test intentional failure to lower outcome stability"}},
            headers=HEADERS, timeout=10)
        return resp2.status_code == 200
    except requests.RequestException:
        return False

def run_test():
    print(f"{'='*90}")
    print("TCE TAKEOVER LLM TRIGGER TEST — Forcing Low Confidence")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"{'='*90}")

    session_id = "llm-trigger-test"
    results = []

    # ─── PHASE 1: Activate with a VAGUE objective ───
    print(f"\n{'─'*90}")
    print("PHASE 1: Activate with vague objective (should get low objective_clarity)")
    print(f"{'─'*90}")

    # Activate — using short vague activation
    r = takeover_step(session_id, "beru take over", 1)
    results.append(r)
    print(f"  T1  conf={r.get('decision_confidence'):.3f}  src={r.get('decision_source'):15s}  "
          f"obj=\"{r.get('objective')}\"  latency={r.get('latency_ms'):.0f}ms")

    # Report failure on the directive to start poisoning outcomes
    if r.get("directive_id"):
        claim_and_report_failure(session_id, r["directive_id"])
        print("       → Reported FAILURE on directive to lower outcome stability")

    time.sleep(0.2)

    # ─── PHASE 2: Send vague messages to keep confidence low ───
    print(f"\n{'─'*90}")
    print("PHASE 2: Vague messages + failures (lowering confidence)")
    print(f"{'─'*90}")

    vague_messages = [
        "ok",           # 1 word → specificity 0.35
        "hmm",          # 1 word
        "sure",         # 1 word
        "continue",     # 1 word, vague
        "go on",        # 2 words
        "what?",        # question, 1 word
        "ok then",      # 2 words
        "yeah",         # 1 word
        "mhm",          # 1 word
        "alright",      # 1 word
    ]

    for i, msg in enumerate(vague_messages):
        turn = i + 2
        r = takeover_step(session_id, msg, turn)
        results.append(r)
        print(f"  T{turn:<3d} conf={r.get('decision_confidence') or 0:.3f}  src={str(r.get('decision_source','???')):15s}  "
              f"cls={str(r.get('classification','???')):10s}  quality={r.get('context_quality_score') or 0:.3f}  "
              f"ret={str(r.get('retrieval_reason','none')):30s}  latency={r.get('latency_ms') or 0:.0f}ms")

        # Keep reporting failures
        if r.get("directive_id"):
            failed = claim_and_report_failure(session_id, r["directive_id"])
            if failed:
                print(f"       → Reported FAILURE #{i+2}")
        time.sleep(0.15)

    # ─── PHASE 3: Deliberation-hint messages with low confidence ───
    print(f"\n{'─'*90}")
    print("PHASE 3: Deliberation hints (research/investigate/analyze) — should trigger LLM")
    print(f"{'─'*90}")

    deliberation_messages = [
        "research this problem deeply",              # "research" + "deep" hints
        "investigate the root cause",                  # "investigate" hint
        "analyze what's going wrong",                  # "analyze" hint
        "do a deep analysis of the situation",         # "deep" + "analysis" hints
        "research why confidence is low",              # "research" hint
        "investigate",                                  # bare "investigate"
        "analyze",                                      # bare "analyze"
        "deep research into the architecture",         # "deep" + "research"
        "investigate the clone advice pipeline",       # "investigate"
        "research the deliberation trigger logic",     # "research"
    ]

    for i, msg in enumerate(deliberation_messages):
        turn = len(vague_messages) + i + 2
        r = takeover_step(session_id, msg, turn)
        results.append(r)

        is_llm = r.get("decision_source") not in ("fast_path", None)
        llm_marker = "🧠 LLM!" if is_llm else "      "
        latency_marker = "⚡SLOW" if (r.get("api_total_ms") or 0) > 500 else "      "

        print(f"  T{turn:<3d} conf={r.get('decision_confidence') or 0:.3f}  src={str(r.get('decision_source','???')):15s}  "
              f"cls={str(r.get('classification','???')):10s}  quality={r.get('context_quality_score') or 0:.3f}  "
              f"ret={str(r.get('retrieval_reason','none')):30s}  api={r.get('api_total_ms') or 0:5.0f}ms  "
              f"{llm_marker} {latency_marker}")

        if r.get("directive_id"):
            claim_and_report_failure(session_id, r["directive_id"])
        time.sleep(0.15)

    # ─── PHASE 4: Change objective mid-stream (forces deliberation) ───
    print(f"\n{'─'*90}")
    print("PHASE 4: Objective changes (should force deliberation)")
    print(f"{'─'*90}")

    objective_changes = [
        "beru take over — fix the embedding pipeline",
        "now switch to investigating the identity mismatch bug",
        "actually research the pattern extraction instead",
        "no wait, analyze the goal deduplication problem",
        "investigate something completely different: Redis caching",
    ]

    for i, msg in enumerate(objective_changes):
        turn = len(vague_messages) + len(deliberation_messages) + i + 2
        r = takeover_step(session_id, msg, turn)
        results.append(r)

        is_llm = r.get("decision_source") not in ("fast_path", None)
        llm_marker = "🧠 LLM!" if is_llm else "      "

        print(f"  T{turn:<3d} conf={r.get('decision_confidence') or 0:.3f}  src={str(r.get('decision_source','???')):15s}  "
              f"obj=\"{r.get('objective','')[:40]}\"  "
              f"ret={str(r.get('retrieval_reason','none')):30s}  api={r.get('api_total_ms') or 0:5.0f}ms  "
              f"{llm_marker}")

        if r.get("directive_id"):
            claim_and_report_failure(session_id, r["directive_id"])
        time.sleep(0.15)

    # ─── PHASE 5: Direct clone_advice endpoint call ───
    print(f"\n{'─'*90}")
    print("PHASE 5: Direct clone_advice endpoint call (bypasses gating)")
    print(f"{'─'*90}")

    try:
        start = time.time()
        resp = requests.post(f"{API_BASE}/clone/advice",
            json={"task": "investigate the takeover LLM integration", "app_context": "testing"},
            headers=HEADERS, timeout=60)
        elapsed = (time.time() - start) * 1000
        if resp.status_code == 200:
            data = resp.json()
            print(f"  Status: {resp.status_code}")
            print(f"  Latency: {elapsed:.0f}ms")
            print(f"  Confidence: {data.get('confidence')}")
            print(f"  Evidence: {data.get('evidence_strength')}")
            print(f"  Has advisor_decision: {bool(data.get('advisor_decision'))}")
            print(f"  Guidance (first 100 chars): {str(data.get('guidance_summary',''))[:100]}")
            if data.get('advisor_decision'):
                ad = data['advisor_decision']
                print(f"  Advisor reasoning: {str(ad.get('reasoning',''))[:100]}")
                print(f"  Advisor action: {ad.get('action')}")
        else:
            print(f"  Status: {resp.status_code}")
            print(f"  Error: {resp.text[:300]}")
    except Exception as e:
        print(f"  Exception: {e}")

    # ─── SUMMARY ───
    print(f"\n{'='*90}")
    print("SUMMARY")
    print(f"{'='*90}")

    ok_results = [r for r in results if r.get("status") == "ok"]
    from collections import Counter

    sources = Counter(r.get("decision_source") for r in ok_results)
    print(f"\nDecision sources: {dict(sources)}")

    fast_path = sources.get("fast_path", 0)
    non_fast = sum(v for k, v in sources.items() if k != "fast_path" and k is not None)
    print(f"  Fast path (no LLM): {fast_path}")
    print(f"  Non-fast-path (potential LLM): {non_fast}")

    confs = [r.get("decision_confidence") for r in ok_results if r.get("decision_confidence") is not None]
    if confs:
        print(f"\nConfidence range: {min(confs):.3f} - {max(confs):.3f}")
        print(f"  Below 0.70: {sum(1 for c in confs if c < 0.70)}/{len(confs)}")
        print(f"  Below 0.78: {sum(1 for c in confs if c < 0.78)}/{len(confs)}")

    ret_reasons = Counter(r.get("retrieval_reason") for r in ok_results if r.get("retrieval_reason"))
    if ret_reasons:
        print(f"\nRetrieval reasons: {dict(ret_reasons)}")

    high_lat = [r for r in ok_results if r.get("api_total_ms", 0) > 500]
    print(f"\nHigh-latency turns (>500ms API, likely LLM): {len(high_lat)}")
    for r in high_lat:
        print(f"  Turn {r['turn']}: {r.get('api_total_ms')}ms, src={r.get('decision_source')}")

    # Final answer
    print(f"\n{'='*90}")
    if non_fast > 0 or len(high_lat) > 0:
        print("✅ LLM WAS TRIGGERED in takeover flow")
    else:
        print("❌ LLM WAS NEVER TRIGGERED — entire flow is rules-only")
    print(f"{'='*90}")
    print(f"Finished: {datetime.now().isoformat()}")

    # Save
    with open("/tmp/tce_llm_trigger_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("Results saved to /tmp/tce_llm_trigger_results.json")

if __name__ == "__main__":
    run_test()
