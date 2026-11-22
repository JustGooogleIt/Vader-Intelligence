# Independent review packets for the research-pipeline release

Two independent reviewers audit the release. Each packet names the branches, the
files to read first, the invariants to attack, the commands that reproduce the
evidence, and the decisions the implementer took that a reviewer may overturn. Both
packets must inspect the interfaces between components (listed last).

Stacked draft PRs, in review order (bases are the previous PR's branch):

| PR | Branch | Scope |
|---|---|---|
| #12 | `fix/f33-foundation-repairs` | Six PR #10 review defects |
| #13 | `feat/f34-completion` | Decision path at C, vetoes, late publication, reconstruct |
| #14 | `feat/f35-evaluation` | F3.5 outcome join, two views, immutable evaluations |
| #15 | `feat/research-runner` | Runner, reports, comparison, deterministic demo |
| #16 | `feat/forward-shadow-ops` | Shadow operations, hardening, wheel smoke, perf |

Exact head SHAs are in `docs/handoffs/release-continuation.md`. Nothing is merged;
`main` is `ebb58df`. Production (schema 1, old code) is untouched.

## Codex Prime: evidence contracts, provenance, temporal correctness, scoring, cohorts, migrations, immutable evaluation semantics

Read first: `docs/handoffs/f34-completion.md`, `docs/handoffs/f35-evaluation.md`, then
`docs/handoffs/release-continuation.md` §"Design decisions".

Attack these invariants:

1. **Lineage** (`forecast/references.py:run_source_mode`, `collector.py`,
   `forecast/witness.py`, `journal.validate_links`): can any path produce a
   `forward_shadow` receipt, binding, decision or evaluation item from a run made with
   an injected transport/clock, or from a fixture? Includes the discovery-pass fallback
   inference for runs written before lineage was persisted (narrowing only).
2. **Operational witness boundary** (`journal.validate_links` receipt branch,
   `evidence._receipt_filter`): a contract-2 receipt not written by a `forecast-witness`
   run naming the same namespace and session must be refused; a row inserted around the
   journal must be ignored by projection and named by integrity. Decision flagged:
   contract 2 amended in place rather than contract 3 (no retained schema-4 archive).
3. **Renewal at C** (`forecast/service.py:renewal_at_cutoff`, `cutoff_facts`): can a
   decision use schedule/event evidence retrieved or witnessed after C, or a book from
   another schedule version? Are the reason categories (identity / pregame / contract)
   mapped correctly from `scope.eligible` reasons? An unknown reason must fail closed.
4. **Publication** (`forecast/tick.py`): later-tick publication at the actual time
   (decision Q-M1-1); the veto reads only already-committed evidence; a vetoed or missed
   record is never replaced.
5. **Outcome join** (`evaluation/outcomes.py`): latest completed attempt by sequence
   within `outcomes_as_of` and ceiling; disputed/pending/exceptional never score; the
   mapping must confirm the frozen gamePk (decision Q-M2-1); MLB is annotation only.
6. **Evaluation immutability and cohorts** (`evaluation/service.py`,
   `journal.append_evaluation`, `check_cohort`): identical manifests return the
   existing run; changed evidence supersedes with a reason; A→B→A keeps three; one
   collector hash per evaluation; scored items require the discovery pin; unpinned rows
   stay unscored; a game decided in one cohort is never "unattempted" in another.
7. **Scores** (`evaluation/scoring.py`): exact decimal strings, clipping counted not
   applied, means null at N=0, paired restricted to items with both scores.
8. **Migrations** (`forecast/schema.py:check_contracts`, `migrate`, `storage.py`): no
   mutation before validation at open (writable and read-only) and before 003/004; a
   contract-2 row on schema 3 or an unknown contract is never legitimized.
9. **Reconstruction** (`forecast/reconstruct.py`): H decisions cite live bindings and
   passes only (M9/M10); synthetic bindings yield synthetic decisions; the ceiling may not
   exclude pre-cutoff evidence; no publication.

Commands:

```sh
cd <worktree>; .venv/bin/python -m pytest -q tests/forecast_storage tests/forecast tests/evaluation tests/research
.venv/bin/python tests/integration/research_demo.py          # hand-verifiable numbers
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python -m pytest --noconftest -q \
  /Users/ashwin/Documents/Vader-Intelligence-reviews/20261012-pr10-evidence/test_independent.py  # the original reproductions
```

## Codex Secondary: collection/tick integration, concurrency, recovery, CLI behaviour, packaging, operations, performance, end-to-end reproduction

Read first: `docs/forward-shadow.md`, `docs/handoffs/forward-shadow-ops.md`,
`docs/handoffs/research-runner.md`.

Attack these:

1. **Tick orchestration** (`forecast/tick.py`): witness → bindings → decisions →
   publications → witness; budget partials; repeated/concurrent ticks; crash before and
   after each record; the per-invocation witness caps actually bound work.
2. **Shadow cycle** (`ops/runner.py:run_locked`, `run_tick`, `maintenance`;
   `ops/common.py:validate_shadow`): tick before collection in one lock hold; tick
   deadline; exit codes (tick failure → 1 without hiding collection); overlap → 75;
   forbidden-path refusal; `shadow-evaluate` under the ops lock; `shadow_health` states.
3. **CLI** (`cli.py`): `forecast tick|inspect|reconstruct`, `evaluate`, `evaluation
   inspect|report|compare`, `research run|inspect`; exit 0/3/1/130; text vs JSON output;
   `--export`/`--out` never overwrite.
4. **Packaging**: `uv build`, install into a separate environment, run
   `tests/integration/wheel_smoke.py --python <env>/bin/python` (41 checks; schemas 1–4
   and contracts 1–2 asserted from the archive; refusal before mutation).
5. **Operations**: `python -m unittest discover -s tests/ops -v` (42); optional native
   drills `tests/ops/mac_launchd_smoke.py`; the finite public smoke procedure in
   `docs/forward-shadow.md` and its recorded outcome in the ops handoff.
6. **Performance**: `tests/integration/perf_probe.py --games 100 --passes 200`
   (projection, selection, integrity, evaluation, open-time preflight); confirm the
   scalar-first projection and the linear integrity cost noted in the handoff.
7. **End-to-end reproduction**: `tests/integration/research_demo.py` and
   `tests/forecast/test_vertical_slice_f34.py` through real entry points; replay after
   evaluation preserves research rows.

## Interfaces between components (both reviewers)

- `collector.py` → `forecast/discovery.py` → `forecast/witness.py` → `forecast/evidence.py`
  (lineage, passes, receipts, projection).
- `forecast/service.py` ↔ `forecast/selection.py` ↔ `forecast/policy.py` (renewal,
  inputs, pure predicates).
- `forecast/tick.py` ↔ `forecast/publication.py` ↔ `forecast/witness.py` (publication
  record, receipt, timeliness).
- `evaluation/service.py` ↔ `evaluation/outcomes.py` ↔ `settlement/*` (outcome view) and
  ↔ `forecast/journal.py` (`append_evaluation`, `check_cohort`).
- `research/runner.py` ↔ `forecast/reconstruct.py` ↔ `evaluation/service.py`.
- `ops/runner.py` ↔ `cli.py` (`forecast tick`, `collect --once`, `settlement refresh`,
  `research run`).
