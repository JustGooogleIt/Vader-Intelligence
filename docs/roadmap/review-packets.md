# Review packets for the roadmap PR chain (Codex Prime and Codex Secondary)

Two independent reviewers. Each packet lists what to read first, the invariants to
attack, and commands that reproduce the evidence. Both reviewers inspect the interfaces
listed last. Exact SHAs: `docs/roadmap/continuation.md`. All PRs are drafts; nothing is
merged, deployed or activated; no credentials exist in this system.

| PR | Branch | Scope | Prime | Secondary |
|---|---|---|---|---|
| #17 | `fix/record-contract-3` | D-001 contract versioning (foundation repair) | lead | wheel/ops |
| #18 | `feat/eligibility-rule-v2` | D-002 rule v2 + mapping policy 2 | lead | live smoke |
| #19 | `feat/schedule-revisions` | D-003 post-C schedule change disposition | lead | — |
| #20 | `feat/historical-acquisition` | A4 public historical source | evidence kind | transport/CLI/resume |
| next | `feat/datasets-models` | B features, datasets, registry, models | leakage, holdout | CLI, determinism |
| next | symbolic | C bounded symbolic search | selection bias, holdout | budgets |
| next | execution-risk | D/E ledger, simulator, risk, Kelly | accounting, risk math | crash/restart |
| next | live adapter | F disabled-by-default Kalshi adapter | duplicate exposure | secrets, activation gates |
| next | pods | G orchestration and candidate lifecycle | protected state | process bounds |
| next | integration | H end-to-end, demos | claims | reproduction |

## Codex Prime — evidence meaning, versioning, leakage, scoring, money correctness

Read first: `docs/roadmap/decisions.md` (D-001..), `docs/roadmap/evidence-index.md`.

1. **D-001 (#17).** Is every rule difference between `rules="draft"` and `rules="current"`
   exactly a PR #12 hunk (`git diff 5811991 27a3473 -- src/vader_intelligence/forecast`)?
   Can any `legacy_draft` row become operational proof (`evidence.PROOF`, witness dedup,
   tick binding, evaluator publication)? Can a contract-3 row ever be validated under the
   draft rules? Does migration 005 touch any row? Do the legacy archives in
   `tests/fixtures/legacy_archives` truly come from the named revisions (regenerate with
   `generate.py`; structure and classification, not bytes, must match)?
2. **D-002 (#18).** Can rule v2 admit a game whose official start differs from the ticker
   time? Can close/expiration/occurrence times influence eligibility? Is the default
   cohort hash unchanged (`Config.provenance()`)? Does settlement replay re-derive mappings
   from the recorded rule set, not the caller's?
3. **D-003 (#19).** Can a decision for a game later played at another start ever be scored?
4. **A4 (#20).** Can historical-retrieval data reach the forecast tick or any forward
   cohort? (Stages are distinct; selection reads `book` only.)
5. **B.** Leakage: can a feature see a quote after C (`FeatureContext`, `until`), a result
   from the game date or later, a label, or post-cutoff execution candles
   (`hist-candles-post` is read only by `interpret.execution_candles`)? Preprocessing,
   calibration and search fit only on training folds; OOF predictions never from a model
   that saw the row; holdout read only through `splits.holdout_rows`; the holdout ledger
   marks every evaluation after the first as reuse.

Commands:

```sh
cd <worktree>; .venv/bin/python -m pytest -q tests/forecast_storage tests/forecast \
  tests/evaluation tests/test_eligibility_rule_v2.py tests/history tests/datasets
.venv/bin/python tests/integration/rule_v2_archive_recheck.py --db <any collection archive>
```

## Codex Secondary — CLI, packaging, operations, recovery, bounded work

1. Wheel: `uv build`; install outside the checkout; `tests/integration/wheel_smoke.py
   --python <venv>/bin/python` (schemas 1–5, contracts 1–3, draft legacy archive migrated).
2. Ops: schema-5 allowlist; shadow procedure (`docs/forward-shadow.md`) with
   `--record-contract 3`; the live rule-v2 shadow evidence (forward-shadow handoff).
3. History acquisition: `vader history acquire` bounds and resume; dedicated-archive
   refusal; history profile allowlist vs collector profile.
4. Datasets: `vader dataset build|inspect` determinism (same inputs → same dataset id);
   registry append-only triggers; artifact write-once.

## Interfaces (both reviewers)

- `collector → discovery → witness → evidence.PROOF → tick → evaluation` (D-001/D-002).
- `history.acquire → history.interpret → datasets.history → research.registry`.
- `collection archive → datasets.collection` (lineage filter) ↔ `history archive` labels.
- (later) `models` ↔ `registry` ↔ `pods`; `forecast → trading.risk → trading.executor →
  simulator | live gateway`.
