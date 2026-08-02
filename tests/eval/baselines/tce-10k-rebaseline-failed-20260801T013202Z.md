# TCE 10,000-Scenario Re-baseline: Comparability Halt

Date: 2026-08-01
Branch: `codex/tce-proof-simplification-v0.4`
Commit under test: `080276d959c04b3653dd050505449031ee1dfe47`

## Verdict

The fresh pre-change run does not reproduce the 2026-07-30 artifact within the
locked tolerance. The lexical retrieval implementation must not be changed on
the basis of this comparison.

The comparison command exited with status 1. Two gates failed:

| Metric | 2026-07-30 artifact | Fresh run | Delta | Allowed | Result |
|---|---:|---:|---:|---:|---|
| Overall equivalent top-10 | 0.6974 | 0.6817 | -0.0157 | +/-0.0150 | HALT |
| Noisy/partial equivalent top-10 | 0.5252 | 0.4996 | -0.0256 | +/-0.0250 | HALT |

The exact, rare-keyword, and explicit cross-user variant gates passed.

## Controlled Conditions

- Frozen embedded corpus: 4,649 events (Claude 1,629; Codex 3,020)
- Deterministic workload: 10,000 scenarios, seed 20260730, 2,500 per variant
- HTTP success: 10,000/10,000
- Retrieval source: `pgvector_ann` for 10,000/10,000 requests
- Planner usage: 0%
- Blocked mean: 0
- Fresh working database cloned from immutable `tce_eval_seed`
- Redis database 15 flushed before the run
- Auto-capture, query expansion, intent retrieval, and Qdrant disabled
- Embedding model: `mxbai-embed-large:latest`
- Embedding model digest: `468836162de7f81e041c43663fedbbba921dcea9b9fefea135685a39b2d83dd8`
- Query embedding output was byte-stable across five repeated probes

## Diagnosis

The failed run is not explained by corpus drift, request failures, planner flag
leakage, or an embedding fallback. The July 30 artifact does not record the Git
SHA, embedding model digest/runtime, or initial Redis activation state. Those
missing provenance fields prevent an exact reconstruction of its environment.

The evidence therefore supports only this conclusion: the July 30 result is not
a valid before/after baseline under the plan's own reproducibility gate. It does
not establish that the proposed FTS fix is ineffective.

## Next Decision

Choose one of these before retrieval implementation resumes:

1. Re-baseline from the fresh, controlled artifact and approve revised targets.
2. Recover enough provenance to reproduce the July 30 environment and rerun.
3. Abandon the before/after implementation plan.

Artifacts:

- `tce-10k-rebaseline-failed-20260801T013202Z.json`
- `tce-10k-rebaseline-failed-20260801T013202Z.gate.txt`
- `tce-10k-eval-readpath-20260730T213629Z.json`
