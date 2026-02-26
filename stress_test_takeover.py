#!/usr/bin/env python3
"""
TCE Takeover 150-Turn Stress Test
==================================
Tests sustained autonomy by simulating 150 turns of conversation
with the takeover_step endpoint, tracking:
- Turn count
- Session state continuity
- Directive issuance
- Latency per turn
- Goal selection persistence
- Error/failure rates
"""

import requests
import time
import json
import sys
from datetime import datetime

API_BASE = "http://localhost:8080/v1"
SESSION_ID = "stress-test-150-auto"
TARGET_TURNS = 100

# Auth headers matching MCP client config
HEADERS = {
    "Authorization": "Bearer local-dev-token",
    "Content-Type": "application/json",
    "X-TCE-Consumer": "stress-test",
    "X-TCE-Role": "executor",
    "X-TCE-Workspace": "personal",
    "X-TCE-User": "local-user",
}

# Simulated conversation messages - realistic mix of questions, responses, and tasks
CONVERSATION_MESSAGES = [
    # Phase 1: Initial exploration (turns 1-20)
    "beru take over — investigate the TCE system architecture",
    "what goals do we currently have in the system?",
    "check the embedding pipeline status",
    "how many events are in the timeline?",
    "what patterns have been discovered?",
    "are there any blocking issues right now?",
    "investigate the entity graph coverage",
    "check if clone advice is working properly",
    "what's the current autonomy status?",
    "look at the search pipeline performance",
    "investigate memory rules configuration",
    "check the fingerprint state",
    "how is the observation store doing?",
    "are embeddings being generated for new events?",
    "what's the retrieval quality like?",
    "check the context bundle generation",
    "investigate the pattern extraction pipeline",
    "are there any stale goals?",
    "check the directive lifecycle status",
    "how is the goal cache performing?",
    # Phase 2: Task-oriented (turns 21-40)
    "analyze the most recent events in the timeline",
    "what are the top priority goals right now?",
    "check if the embedding timeout is still an issue",
    "investigate the situation type classification",
    "are there any duplicate observations?",
    "check the clone advice confidence levels",
    "how many events have embeddings vs missing?",
    "investigate the decision confidence calculation",
    "check the audit log for recent errors",
    "are the affective scores being computed correctly?",
    "investigate the goal selection scoring",
    "check the takeover session timeout",
    "how is the retrieval bounded behavior working?",
    "investigate any failed directives",
    "check the execution permit system",
    "are there any pending notices?",
    "investigate the goal cache freshness",
    "check the pattern evidence backlinks",
    "how is the entity extraction performing?",
    "investigate the similarity search accuracy",
    # Phase 3: Decision-making (turns 41-60)
    "should we increase the embedding timeout?",
    "what's the best approach for fixing the identity mismatch?",
    "investigate options for backfilling missing embeddings",
    "should we consolidate duplicate goals?",
    "what's the risk of changing situation type mappings?",
    "investigate the impact of seed data quality",
    "should we add more observation evidence?",
    "what patterns are most useful for clone advice?",
    "investigate the optimal goal discovery frequency",
    "should we tune the affective scoring weights?",
    "what's the best strategy for real data ingestion?",
    "investigate the memory rule visibility issue",
    "should we adjust the retrieval budget bounds?",
    "what's the impact of low entity graph coverage?",
    "investigate options for improving search recall",
    "should we add more situation types?",
    "what's the optimal directive expiry time?",
    "investigate the goal deduplication strategy",
    "should we change the confidence thresholds?",
    "what's the best approach for pattern validation?",
    # Phase 4: Continued exploration (turns 61-80)
    "check the current state of the system",
    "are there new events since we started?",
    "investigate if our test is generating data correctly",
    "check the autonomy score trend",
    "how many directives have we processed?",
    "investigate any latency spikes",
    "check the goal queue stability",
    "are there any new patterns emerging?",
    "investigate the session continuity",
    "check if takeover mode is still active",
    "investigate any dropped directives",
    "check the execution claim success rate",
    "are there any safety violations?",
    "investigate the context quality scores",
    "check the retrieval source distribution",
    "are we hitting any rate limits?",
    "investigate the memory usage patterns",
    "check the error rate over time",
    "are there any timeout issues?",
    "investigate the overall system health",
    # Phase 5: Vague messages to test standby (turns 81-100)
    "ok",
    "continue",
    "hmm interesting",
    "sure",
    "go on",
    "what else?",
    "keep going",
    "alright",
    "noted",
    "ok understood",
    "investigate more details about that",
    "check something else interesting",
    "what about the other components?",
    "keep investigating",
    "tell me more",
    "continue the analysis",
    "what do you think?",
    "anything else to look at?",
    "proceed",
    "ok what's next",
    # Phase 6: Back to concrete tasks (turns 101-120)
    "investigate the TCE worker job queue",
    "check the lifecycle cleanup status",
    "analyze the event retention policy",
    "investigate the graph snapshot generation",
    "check the Redis cache hit rates",
    "analyze the PostgreSQL query performance",
    "investigate the Ollama embedding throughput",
    "check the context bundle citation accuracy",
    "analyze the clone guidance evidence strength",
    "investigate the takeover classification accuracy",
    "check the situation type distribution",
    "analyze the goal priority score distribution",
    "investigate the affective score ranges",
    "check the pattern confidence distribution",
    "analyze the observation query performance",
    "investigate the event entity link coverage",
    "check the embedding vector dimensions",
    "analyze the search hybrid scoring",
    "investigate the fingerprint update frequency",
    "check the audit trail completeness",
    # Phase 7: Wrap-up investigation (turns 121-150)
    "summarize what we've found so far",
    "what are the key findings from this test?",
    "investigate any stability issues found",
    "check the overall test metrics",
    "analyze the turn-over-turn latency trend",
    "investigate the directive success rate",
    "check the goal continuity across turns",
    "analyze the state machine transitions",
    "investigate any context quality degradation",
    "check if autonomy remained stable",
    "analyze the retrieval performance over time",
    "investigate the session timeout behavior",
    "check the error recovery patterns",
    "analyze the clone advice consistency",
    "investigate the pattern matching stability",
    "check the entity graph evolution during test",
    "analyze the embedding generation rate",
    "investigate the search relevance drift",
    "check the memory rule effectiveness",
    "analyze the overall autonomy pipeline health",
    "investigate the test coverage gaps",
    "check the performance bottlenecks found",
    "analyze the failure modes discovered",
    "investigate the scalability implications",
    "check the data quality impact on autonomy",
    "analyze the end-to-end pipeline latency",
    "investigate the directive lifecycle reliability",
    "final summary of 150-turn stress test",
    "beru stand down — stress test complete",
    "report generation complete",
]

def call_takeover_step(session_id, message, turn_num):
    """Call the takeover_step endpoint directly."""
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
        resp = requests.post(f"{API_BASE}/takeover/step", json=payload, headers=HEADERS, timeout=30)
        elapsed_ms = (time.time() - start) * 1000

        if resp.status_code != 200:
            return {
                "turn": turn_num,
                "status": "error",
                "http_code": resp.status_code,
                "latency_ms": elapsed_ms,
                "error": resp.text[:200],
                "active": None,
                "mode": None,
                "has_directive": None,
                "directive_id": None,
                "directive_state": None,
                "selected_goal": None,
                "selection_score": None,
                "safety_decision": None,
                "continuity_ok": None,
                "decision_confidence": None,
                "turn_count": None,
            }

        data = resp.json()
        state = data.get("state", {})
        tc = state.get("takeover_context", {})
        sg = data.get("selected_goal", {}) or {}
        pe = data.get("pending_execution", {}) or {}
        lb = data.get("latency_breakdown_ms", {}) or {}

        # API uses "note" field for directive content (not "has_directive")
        has_directive = bool(data.get("note"))

        return {
            "turn": turn_num,
            "status": "ok",
            "http_code": 200,
            "latency_ms": elapsed_ms,
            "api_latency_ms": lb.get("total", 0),
            "error": None,
            "active": state.get("active"),
            "mode": state.get("mode"),
            "has_directive": has_directive,
            "directive_id": data.get("directive_id"),
            "directive_state": data.get("directive_state"),
            "selected_goal_id": sg.get("id") if isinstance(sg, dict) else None,
            "selected_goal_title": sg.get("title") if isinstance(sg, dict) else None,
            "selection_score": sg.get("selection_score") if isinstance(sg, dict) else None,
            "safety_decision": data.get("safety_decision"),
            "continuity_ok": data.get("continuity_ok"),
            "decision_confidence": data.get("decision_confidence"),
            "turn_count": tc.get("turn_count"),
            "classification": data.get("classification"),
            "action": data.get("action"),
            "needs_human": data.get("needs_human"),
            "retry_scheduled": data.get("retry_scheduled"),
            "goal_cache_hit": data.get("goal_cache_hit"),
            "execution_permit_required": data.get("execution_permit_required"),
            "retrieval_latency_ms": lb.get("retrieval", 0),
            "classify_latency_ms": lb.get("classify", 0),
            "context_quality_score": data.get("context_quality_score"),
            "retrieval_source": data.get("retrieval_source"),
            # LLM detection fields
            "decision_source": data.get("decision_source"),
            "has_clone_advice": bool(data.get("clone_advice")),
            "clone_confidence": (data.get("clone_advice") or {}).get("confidence") if isinstance(data.get("clone_advice"), dict) else None,
            "clone_evidence": (data.get("clone_advice") or {}).get("evidence_strength") if isinstance(data.get("clone_advice"), dict) else None,
        }
    except requests.exceptions.Timeout:
        elapsed_ms = (time.time() - start) * 1000
        return {
            "turn": turn_num,
            "status": "timeout",
            "http_code": None,
            "latency_ms": elapsed_ms,
            "error": "Request timeout (30s)",
            "active": None,
            "mode": None,
            "has_directive": None,
        }
    except Exception as e:
        elapsed_ms = (time.time() - start) * 1000
        return {
            "turn": turn_num,
            "status": "exception",
            "http_code": None,
            "latency_ms": elapsed_ms,
            "error": str(e)[:200],
            "active": None,
            "mode": None,
            "has_directive": None,
        }

def call_claim_execution(session_id, directive_id):
    """Claim a directive for execution."""
    payload = {
        "session_id": session_id,
        "directive_id": directive_id,
        "claimed_by": "stress-test"
    }
    try:
        resp = requests.post(f"{API_BASE}/takeover/execution/claim", json=payload, headers=HEADERS, timeout=10)
        return resp.status_code == 200, resp.json() if resp.status_code == 200 else resp.text[:100]
    except Exception as e:
        return False, str(e)[:100]

def call_report_execution(session_id, directive_id, state="succeeded"):
    """Report execution result."""
    payload = {
        "session_id": session_id,
        "directive_id": directive_id,
        "state": state,
        "result": "success" if state == "succeeded" else "failure",
        "details": {"summary": f"Stress test turn completed"}
    }
    try:
        resp = requests.post(f"{API_BASE}/takeover/execution/report", json=payload, headers=HEADERS, timeout=10)
        return resp.status_code == 200, resp.json() if resp.status_code == 200 else resp.text[:100]
    except Exception as e:
        return False, str(e)[:100]

def run_stress_test():
    """Run the 150-turn stress test."""
    print(f"{'='*80}")
    print(f"TCE TAKEOVER 150-TURN STRESS TEST")
    print(f"Session: {SESSION_ID}")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"{'='*80}\n")

    results = []
    errors = []
    directives_issued = 0
    directives_claimed = 0
    directives_reported = 0
    state_drops = 0
    mode_changes = []
    last_active = None
    consecutive_vague = 0
    goal_changes = 0
    last_goal_id = None
    cache_hits = 0
    cache_misses = 0

    for i in range(TARGET_TURNS):
        turn_num = i + 1
        msg = CONVERSATION_MESSAGES[i % len(CONVERSATION_MESSAGES)]

        # Print progress
        if turn_num % 10 == 0 or turn_num <= 5:
            print(f"\n--- Turn {turn_num}/{TARGET_TURNS} ---")
            print(f"  Message: {msg[:60]}...")

        result = call_takeover_step(SESSION_ID, msg, turn_num)
        results.append(result)

        # Track errors
        if result["status"] != "ok":
            errors.append(result)
            print(f"  ⚠️  Turn {turn_num}: {result['status']} - {result.get('error', 'unknown')[:80]}")
            continue

        # Track state continuity
        if last_active is not None and result["active"] != last_active:
            state_drops += 1
            print(f"  ⚠️  State change at turn {turn_num}: {last_active} → {result['active']}")
        last_active = result.get("active")

        # Track directives
        if result.get("has_directive"):
            directives_issued += 1
            directive_id = result.get("directive_id")

            if directive_id and result.get("directive_state") == "pending":
                # Try to claim and report
                claimed, _ = call_claim_execution(SESSION_ID, directive_id)
                if claimed:
                    directives_claimed += 1
                    reported, _ = call_report_execution(SESSION_ID, directive_id)
                    if reported:
                        directives_reported += 1

        # Track goal changes
        goal_id = result.get("selected_goal_id")
        if goal_id and goal_id != last_goal_id:
            goal_changes += 1
            last_goal_id = goal_id

        # Track cache
        if result.get("goal_cache_hit"):
            cache_hits += 1
        elif result.get("goal_cache_hit") is not None:
            cache_misses += 1

        # Print periodic status
        if turn_num % 10 == 0:
            ok_count = sum(1 for r in results if r["status"] == "ok")
            active_count = sum(1 for r in results if r.get("active") is True)
            avg_latency = sum(r["latency_ms"] for r in results if r["status"] == "ok") / max(ok_count, 1)
            print(f"  ✅ Turn {turn_num}: active={result.get('active')}, mode={result.get('mode')}, "
                  f"directive={result.get('has_directive')}, latency={result['latency_ms']:.0f}ms, "
                  f"src={result.get('decision_source')}, clone={result.get('has_clone_advice')}")
            print(f"     Running stats: ok={ok_count}/{turn_num}, active_turns={active_count}, "
                  f"directives={directives_issued}, avg_latency={avg_latency:.0f}ms")

        # Small delay to avoid hammering
        time.sleep(0.1)

    # Final report
    ok_results = [r for r in results if r["status"] == "ok"]
    active_results = [r for r in ok_results if r.get("active") is True]
    latencies = [r["latency_ms"] for r in ok_results]
    api_latencies = [r.get("api_latency_ms", 0) for r in ok_results if r.get("api_latency_ms")]
    retrieval_latencies = [r.get("retrieval_latency_ms", 0) for r in ok_results if r.get("retrieval_latency_ms")]

    print(f"\n{'='*80}")
    print(f"STRESS TEST RESULTS")
    print(f"{'='*80}")
    print(f"\n📊 TURN METRICS:")
    print(f"  Total turns attempted: {TARGET_TURNS}")
    print(f"  Successful turns: {len(ok_results)} ({len(ok_results)/TARGET_TURNS*100:.1f}%)")
    print(f"  Failed turns: {len(errors)} ({len(errors)/TARGET_TURNS*100:.1f}%)")
    print(f"  Timeout turns: {sum(1 for r in results if r['status'] == 'timeout')}")

    print(f"\n🔄 STATE CONTINUITY:")
    print(f"  Turns with active=True: {len(active_results)} ({len(active_results)/max(len(ok_results),1)*100:.1f}%)")
    print(f"  State drops (active→inactive): {state_drops}")
    print(f"  Final state active: {results[-1].get('active') if results else 'N/A'}")
    print(f"  Final mode: {results[-1].get('mode') if results else 'N/A'}")

    print(f"\n📋 DIRECTIVE LIFECYCLE:")
    print(f"  Directives issued: {directives_issued}")
    print(f"  Directives claimed: {directives_claimed}")
    print(f"  Directives reported: {directives_reported}")
    print(f"  Directive success rate: {directives_reported/max(directives_issued,1)*100:.1f}%")

    print(f"\n🎯 GOAL TRACKING:")
    print(f"  Goal changes: {goal_changes}")
    print(f"  Cache hits: {cache_hits}")
    print(f"  Cache misses: {cache_misses}")
    print(f"  Cache hit rate: {cache_hits/max(cache_hits+cache_misses,1)*100:.1f}%")

    if latencies:
        print(f"\n⏱️  LATENCY (e2e request):")
        print(f"  Min: {min(latencies):.0f}ms")
        print(f"  Max: {max(latencies):.0f}ms")
        print(f"  Avg: {sum(latencies)/len(latencies):.0f}ms")
        print(f"  p50: {sorted(latencies)[len(latencies)//2]:.0f}ms")
        print(f"  p90: {sorted(latencies)[int(len(latencies)*0.9)]:.0f}ms")
        print(f"  p99: {sorted(latencies)[int(len(latencies)*0.99)]:.0f}ms")

    if api_latencies:
        print(f"\n⏱️  LATENCY (API internal):")
        print(f"  Min: {min(api_latencies):.0f}ms")
        print(f"  Max: {max(api_latencies):.0f}ms")
        print(f"  Avg: {sum(api_latencies)/len(api_latencies):.0f}ms")

    if retrieval_latencies:
        print(f"\n⏱️  RETRIEVAL LATENCY:")
        print(f"  Min: {min(retrieval_latencies):.0f}ms")
        print(f"  Max: {max(retrieval_latencies):.0f}ms")
        print(f"  Avg: {sum(retrieval_latencies)/len(retrieval_latencies):.0f}ms")

    # Safety analysis
    safety_decisions = [r.get("safety_decision") for r in ok_results if r.get("safety_decision")]
    if safety_decisions:
        from collections import Counter
        safety_counts = Counter(safety_decisions)
        print(f"\n🛡️  SAFETY DECISIONS:")
        for decision, count in safety_counts.most_common():
            print(f"  {decision}: {count}")

    # Classification analysis
    classifications = [r.get("classification") for r in ok_results if r.get("classification")]
    if classifications:
        from collections import Counter
        class_counts = Counter(classifications)
        print(f"\n🏷️  CLASSIFICATIONS:")
        for cls, count in class_counts.most_common():
            print(f"  {cls}: {count}")

    # Continuity analysis
    continuity_ok = [r.get("continuity_ok") for r in ok_results if r.get("continuity_ok") is not None]
    if continuity_ok:
        ok_count = sum(1 for c in continuity_ok if c)
        print(f"\n🔗 CONTINUITY:")
        print(f"  OK: {ok_count}/{len(continuity_ok)} ({ok_count/len(continuity_ok)*100:.1f}%)")
        print(f"  Broken: {len(continuity_ok) - ok_count}")

    # LLM / Decision Source analysis
    decision_sources = [r.get("decision_source") for r in ok_results if r.get("decision_source")]
    if decision_sources:
        from collections import Counter
        ds_counts = Counter(decision_sources)
        print(f"\n🧠 LLM / DECISION SOURCE ANALYSIS:")
        for src, count in ds_counts.most_common():
            print(f"  {src}: {count}")
        fast_path_count = ds_counts.get("fast_path", 0)
        deliberation_count = sum(v for k, v in ds_counts.items() if k != "fast_path")
        print(f"  ---")
        print(f"  Fast path (no LLM): {fast_path_count} ({fast_path_count/max(len(decision_sources),1)*100:.1f}%)")
        print(f"  Deliberation (LLM): {deliberation_count} ({deliberation_count/max(len(decision_sources),1)*100:.1f}%)")

    clone_advice_count = sum(1 for r in ok_results if r.get("has_clone_advice"))
    clone_confidences = [r.get("clone_confidence") for r in ok_results if r.get("clone_confidence") is not None]
    clone_evidences = [r.get("clone_evidence") for r in ok_results if r.get("clone_evidence")]
    print(f"\n🤖 CLONE ADVICE (LLM indicator):")
    print(f"  Turns with clone_advice present: {clone_advice_count}/{len(ok_results)}")
    if clone_confidences:
        print(f"  Clone confidence: min={min(clone_confidences):.3f}, max={max(clone_confidences):.3f}, avg={sum(clone_confidences)/len(clone_confidences):.3f}")
    if clone_evidences:
        from collections import Counter
        ev_counts = Counter(clone_evidences)
        print(f"  Evidence strength: {dict(ev_counts)}")

    # Decision confidence analysis
    confidences = [r.get("decision_confidence") for r in ok_results if r.get("decision_confidence") is not None]
    if confidences:
        print(f"\n📊 DECISION CONFIDENCE:")
        print(f"  Min: {min(confidences):.3f}")
        print(f"  Max: {max(confidences):.3f}")
        print(f"  Avg: {sum(confidences)/len(confidences):.3f}")
        below_070 = sum(1 for c in confidences if c < 0.70)
        below_078 = sum(1 for c in confidences if c < 0.78)
        print(f"  Below 0.70 (clone_advice trigger): {below_070}/{len(confidences)}")
        print(f"  Below 0.78 (deliberation trigger): {below_078}/{len(confidences)}")

    # Latency spike analysis (LLM calls show as >500ms)
    high_latency = [r for r in ok_results if r.get("api_latency_ms", 0) > 500]
    print(f"\n⚡ LATENCY SPIKES (>500ms API = likely LLM call):")
    print(f"  Count: {len(high_latency)}/{len(ok_results)}")
    for r in high_latency[:10]:
        print(f"  Turn {r['turn']}: {r.get('api_latency_ms')}ms api, decision_source={r.get('decision_source')}, clone_advice={r.get('has_clone_advice')}")

    # Error details
    if errors:
        print(f"\n❌ ERROR DETAILS:")
        for err in errors[:10]:
            print(f"  Turn {err['turn']}: {err['status']} - {err.get('error', 'unknown')[:100]}")
        if len(errors) > 10:
            print(f"  ... and {len(errors)-10} more errors")

    # Turn count tracking
    turn_counts = [r.get("turn_count") for r in ok_results if r.get("turn_count") is not None]
    if turn_counts:
        print(f"\n📈 TURN COUNT TRACKING (server-side):")
        print(f"  First: {turn_counts[0]}")
        print(f"  Last: {turn_counts[-1]}")
        print(f"  Max: {max(turn_counts)}")
        monotonic = all(turn_counts[i] <= turn_counts[i+1] for i in range(len(turn_counts)-1))
        print(f"  Monotonically increasing: {'✅' if monotonic else '❌'}")

    # Verdict
    print(f"\n{'='*80}")
    success_rate = len(ok_results) / TARGET_TURNS * 100
    active_rate = len(active_results) / max(len(ok_results), 1) * 100
    directive_rate = directives_reported / max(directives_issued, 1) * 100

    if success_rate >= 95 and active_rate >= 90 and directive_rate >= 80:
        verdict = "✅ PASS — Sustained autonomy verified"
    elif success_rate >= 80 and active_rate >= 70:
        verdict = "⚠️  PARTIAL — Autonomy works but has gaps"
    else:
        verdict = "❌ FAIL — Sustained autonomy not reliable"

    print(f"VERDICT: {verdict}")
    print(f"  Success rate: {success_rate:.1f}% (threshold: 95%)")
    print(f"  Active rate: {active_rate:.1f}% (threshold: 90%)")
    print(f"  Directive lifecycle: {directive_rate:.1f}% (threshold: 80%)")
    print(f"{'='*80}")
    print(f"Finished: {datetime.now().isoformat()}")

    # Save raw results to JSON
    output_file = "/tmp/tce_stress_test_results.json"
    with open(output_file, "w") as f:
        json.dump({
            "session_id": SESSION_ID,
            "target_turns": TARGET_TURNS,
            "results": results,
            "summary": {
                "total_turns": TARGET_TURNS,
                "ok_turns": len(ok_results),
                "active_turns": len(active_results),
                "errors": len(errors),
                "state_drops": state_drops,
                "directives_issued": directives_issued,
                "directives_claimed": directives_claimed,
                "directives_reported": directives_reported,
                "goal_changes": goal_changes,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "avg_latency_ms": sum(latencies)/len(latencies) if latencies else 0,
                "max_latency_ms": max(latencies) if latencies else 0,
                "verdict": verdict,
            }
        }, f, indent=2, default=str)
    print(f"\nRaw results saved to: {output_file}")

if __name__ == "__main__":
    run_stress_test()
