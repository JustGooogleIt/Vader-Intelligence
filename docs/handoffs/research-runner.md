# Research runner, reports and comparison (release milestones 3 and 4)

Branch `feat/research-runner`, stacked on `feat/f35-evaluation` (PR #14). Implemented
2026-10-14 on this Mac in an isolated worktree (`uv sync --locked`, Python 3.11.6).
Every archive is disposable and every record synthetic. Nothing merged or deployed.

No authoritative experiment-runner plan existed, so this is the compact design and its
conservative offline implementation over the existing interfaces. It adds no provider
request, no model, no tuning, no LLM, no orchestration platform and no new table.

## Design

A **research run** = one archive + one research protocol + one evaluation scope →
immutable, reproducible results. It is a `runs` row of kind `research-run`:

| Provenance (`runs.provenance_json.configuration`) | Output (`runs.summary_json`) |
|---|---|
| research protocol hash, forward dataset, study dataset, view, `outcomes_as_of`, source ceiling, date range, split boundaries, archive identity (schema version, fetch ceiling, row counts of fetches/passes/bindings/decisions/settlement versions, digest of the latest fetch row) and the code hash in the run provenance | per segment (`whole`, or `development` then `holdout`) and per collector configuration cohort: the evaluation id, status, `existing` flag, supersedes id, mode, population counts and paired summary; the reconstruction chunk summaries; the holdout history; archive identity after the run; runtime |

Steps, in temporal order:

1. **Reconstruction** (cutoff-reconstruction view only): `forecast reconstruct` over
   the range in ≤31-day chunks, bindings in cutoff order, into the explicit study
   namespace. The forward view evaluates the forward dataset as actually published.
2. **Evaluation**: one `evaluate` per segment and per collector cohort found in the
   dataset's decisions (`collector_config_hash` pin). Cohorts and record modes are never
   pooled; each result is keyed by cohort hash.
3. **Resume**: decisions and evaluations have content keys, so a repeated or resumed run
   reports `existing: true` and writes nothing new. A budget exhaustion is `partial` with
   nothing written by the interrupted step.

**Split and held-out observations.** `--development-through D --holdout-from H` with
`start ≤ D < H ≤ through`; the two ranges never overlap; development is evaluated before
holdout; the summary's `holdout_history` lists every earlier evaluation in the dataset
whose period overlaps the holdout range, in any view (an open-ended period overlaps),
with its view, period and the protocol and code hash it used. The protocol is
hashed and recorded before any evaluation. v1 has no tunable parameter, so there is
nothing to fit; the framework records enough provenance to audit the experiment and
does not claim to prevent every form of researcher leakage (a reviewer changing the
protocol after reading a holdout report would be visible in `holdout_history`).

**Separation.** Synthetic, historical-reconstruction and forward-shadow results are
separate evaluations with their own `mode`; `evaluation compare` refuses different
views, modes or protocols and labels different collector cohorts.

## Reports (milestone 4)

- `vader evaluation report --id N` prints the concise text report; `--format json`
  (optionally `--items`, `--export FILE` to a new file) prints the machine-readable
  document: the stored report, identities, manifest digests. Text display rounds to six
  decimals; stored scores stay exact decimal strings.
- `vader evaluation compare --ids A,B` compares two evaluations on the opportunities
  both scored (paired metrics restricted to the intersection) and lists the coverage
  difference (opportunities and scored items only in one of them). Unequal samples are
  never presented as directly comparable.
- `vader research run … --out FILE` exports the run summary; `vader research inspect
  [--run-id ID]` reads runs back.

Every report carries: U/E/K/M/P (and FK/FM/FP in the forward view), decisions,
abstentions, unattempted, no-binding and unresolved-candidate counts; coverage ratios
with numerators and denominators; primary cutoff reasons, publication status and
reasons, outcome status and reasons; collector and research configuration hashes, code
hash, evaluator and runtime versions, policy identities; evidence mode and
`simulated_view`; the period (dates, cutoffs, `outcomes_as_of`, ceiling); provenance
limitations. No profitability, PnL or execution claim appears anywhere.

## Deterministic demonstration

`tests/integration/research_demo.py` builds a disposable archive from the shipped
fixture (injected transport and clocks), runs collection → ticks → settlement refresh →
forward and reconstruction research runs → text reports → replay, and checks 25
hand-verifiable expectations (midpoint 0.6450 from best YES bid 0.6400 and NO bid
0.3500; Brier 0.25 / 0.12602500, delta −0.12397500; log loss −ln 0.5 / −ln 0.6450;
U=E=K=M=P=FK=FM=FP=1). `tests/research/test_demo.py` runs it. Output at `313b115`:

```
Paired scores (n=1): Brier constant 0.250000 midpoint 0.126025 delta -0.123975 | log loss constant 0.693147 midpoint 0.438505 delta -0.254642 | clipped 0
```

## Tests

`tests/research/test_runner_and_reports.py` (8): forward run per cohort and
idempotent resume; reconstruction run in its own namespace with separate evaluations;
split order and holdout history; budget exhaustion partial with nothing written; text
and machine reports; comparison restricted to common scored opportunities with coverage
differences and view refusal; export never overwrites; CLI round trip.
`tests/research/test_demo.py` (1). Full suite at `313b115`: 798 passed, 2
pre-existing `tests/worker_observer` failures. Ops 31 OK.
