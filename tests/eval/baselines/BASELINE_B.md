# Approved TCE Retrieval Baseline B

The controlled 2026-08-01 run is the approved pre-change baseline for the
lexical retrieval implementation. The user approved this re-baseline after the
July 30 artifact failed the original reproducibility tolerance.

- JSON: `BASELINE_B.json`
- Source evidence: `tce-10k-rebaseline-failed-20260801T013202Z.json`
- Corpus: 4,649 embedded events
- Scenarios: 10,000, seed 20260730
- Equivalent-memory top-10: 0.6817 overall
- Noisy/partial top-10: 0.4996
- Explicit cross-user top-10: 0.5896
- HTTP success: 1.0
- p95 latency: 451.61 ms

The original absolute post-fix quality targets remain unchanged. Approval of a
new baseline does not weaken the acceptance gates.
