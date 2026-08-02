# TCE 10,000-Scenario Evaluation

Date: 2026-07-30
Branch: codex/tce-proof-simplification-v0.4

## Scope

This evaluation measures historical-memory retrieval and serving behavior. It does not establish human-behavior cloning fidelity.

- 10,000 deterministic scenarios
- all 4,649 embedded imported user inputs covered at least twice
- 2,500 exact-recall queries
- 2,500 rare-keyword queries
- 2,500 noisy/partial queries
- 2,500 explicit Claude-to-Codex continuity queries
- k=10, eight concurrent clients
- isolated Postgres, Redis DB, and API runtime
- no production data mutations

## Read-Path Results

| Query class | Top-1 equivalent | Top-5 equivalent | Top-10 equivalent | p95 latency |
|---|---:|---:|---:|---:|
| Exact | 43.96% | 70.60% | 86.56% | 443.55 ms |
| Rare keywords | 38.72% | 68.52% | 80.48% | 438.61 ms |
| Noisy/partial | 22.04% | 42.60% | 52.52% | 446.82 ms |
| Explicit cross-user | 22.80% | 44.48% | 59.40% | 474.42 ms |
| Overall | 31.88% | 56.55% | 69.74% | 450.84 ms |

- HTTP success: 10,000/10,000
- Wall time: 302.83 seconds
- Throughput: 33.02 requests/second
- Latency: p50 216.75 ms, p95 450.84 ms, p99 609.18 ms, max 1086.11 ms
- Strict event-ID recall: top-1 25.91%, top-5 50.23%, top-10 63.84%
- Explicit cross-user scope activation: 100%
- Intent planner usage: 0%; the runtime uses the default disabled setting

## Capture-On Stress Finding

The production-like capture-on path was stopped after 2,500 requests because it became invalid as a quality benchmark:

- 7 HTTP failures
- throughput degraded from 26.05 to 17.71 requests/second
- 128 Postgres connections observed
- 94 tuple-lock waiters observed
- lock hotspot: team_memberships INSERT with ON CONFLICT DO UPDATE SET active=true during auto-captured graph indexing

The same workload with auto-capture disabled completed all 10,000 requests with zero failures and no lock waiters. This isolates the defect to the write/capture path, not vector retrieval.

## Verdict

TCE reliably serves and scopes shared memory, but fuzzy continuation quality is not yet strong enough to claim deterministic autonomous handoff. Exact and rare-keyword top-10 recall are useful; noisy and cross-user top-10 recall remain below a production-quality target. The capture-on concurrency bottleneck must also be fixed before sustained multi-agent traffic.

## Artifacts

- Aggregate result: /Users/joeljoseph/.cache/open-timeline-engine/tce-10k-eval-readpath-20260730T213629Z.json
- Capture-on stress finding: /Users/joeljoseph/.cache/open-timeline-engine/tce-10k-capture-on-stress-20260730.json
- No raw prompts are stored in either artifact.
