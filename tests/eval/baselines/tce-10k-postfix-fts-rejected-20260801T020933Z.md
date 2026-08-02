# TCE FTS Retrieval Experiment: Rejected at Phase 4

Date: 2026-08-01
Branch: `codex/tce-proof-simplification-v0.4`
Baseline: `BASELINE_B.json`

## Verdict

The experiment confirms that the old lexical rank was suppressing retrieval
quality, but the implementation is not production-ready. It fails two locked
acceptance gates, including the hard latency ceiling. Phase 5 RRF and Phase 6
Lite rollout must not proceed from this result.

## Results

| Metric | Approved baseline | FTS experiment | Delta | Result |
|---|---:|---:|---:|---|
| Overall equivalent top-10 | 0.6817 | 0.9128 | +0.2311 | PASS |
| Noisy/partial equivalent top-10 | 0.4996 | 0.8932 | +0.3936 | PASS |
| Explicit cross-user equivalent top-10 | 0.5896 | 0.8712 | +0.2816 | PASS |
| Rare-keyword equivalent top-10 | 0.7816 | 0.9392 | +0.1576 | HALT: exceeds confound ceiling |
| Exact equivalent top-10 | 0.8560 | 0.9476 | +0.0916 | PASS |
| Overall equivalent MRR@10 | 0.421541 | 0.758741 | +0.337200 | PASS |
| p95 HTTP latency | 451.61 ms | 686.99 ms | +235.38 ms | HALT: exceeds 600 ms |
| Throughput | 32.84 req/s | 20.35 req/s | -38.0% | Regression |
| Cross-user scope rate | 0.2519 | 0.2519 | 0.0000 | PASS |
| HTTP success | 1.0 | 1.0 | 0.0 | PASS |
| Blocked mean | 0.0 | 0.0 | 0.0 | PASS |

Channel evidence:

- `fts_union_ilike`: 9,875/10,000 requests (98.75%)
- `ilike_only`: 125/10,000 requests
- `ilike_fallback_error`: 0 requests
- `pgvector_ann`: 10,000/10,000 requests
- planner usage: 0%

## Interpretation

The primary prediction passed by a wide margin, so the diagnosis was not wrong:
using merge/recency position as lexical relevance materially harmed recall.

The rare-keyword guard also fired. Corpus, seed, ANN pool, Redis reset, planner,
query expansion, capture mode, embedding model, and owner/workspace scope were
held constant. That makes an uncontrolled-variable explanation unlikely; the
plan's assumption that rare-keyword retrieval was already saturated appears to
be false. The locked gate still requires the result to be rejected until that
assumption is explicitly revised.

The latency failure is independent and decisive. The FTS arm plus unchanged
ILIKE arm and wider candidate scoring increased p95 by 52% and reduced
throughput by 38%. Recall gains cannot justify shipping above the hard latency
budget.

## Required Next Decision

Before implementation can resume, explicitly authorize both:

1. A latency-remediation experiment that preserves the accepted quality and
   privacy targets while restoring p95 to at most 600 ms.
2. Revision or removal of the rare-keyword confound ceiling based on the fully
   controlled result.

Artifacts:

- `tce-10k-postfix-fts-rejected-20260801T020933Z.json`
- `tce-10k-postfix-fts-rejected-20260801T020933Z.gate.txt`
- `BASELINE_B.json`
