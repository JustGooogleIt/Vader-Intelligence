# Forecast/evaluation specification handoff

F3.1 reconciliation (2026-09-30): imported this handoff and the specification from
`9e2ab907098fef763ebf7e4acbaacf8ebf8fa5b7` onto reviewed code
`9a34420947f5722109186ff4897f0b1f6cb18f09`. Independent re-review resolved the P2;
the correction changes inspection evidence association only, with no schema or
collector-interface change. The three spec findings are resolved in this revision.
The historical planning record follows unchanged. Current F3.1 contracts, actual
tests and deferred obligations are in [forecast-policies.md](forecast-policies.md).

Date: 2026-09-30. Documentation-only branch `docs/forecast-evaluation-spec`, created
in `/Users/ashwin/Documents/github-repos/Vader-Intelligence-forecast-spec` from exact
integration reference `9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7`.
This review revision starts from documentation commit
`3c49998ac8356ec9c4d974292e2c4f8e03b92d82`. Integration head
`9a34420947f5722109186ff4897f0b1f6cb18f09` remains with Secondary for independent
re-review; this revision does not change or revalidate that code.
Repository intentionally public; visibility unchanged. PR #4 and its worker branch
are untouched. No application code, dependency, database or running service changed.

## Deliverable and decisions

[Detailed F3 specification](../specs/forecast-evaluation-v1.md) defines the full
implementation contract, examples, new records, bounded CLI, acceptance tests and
small implementation increments. It is **not an implementation or a deployment**.

- T−60 follows witnessed schedule history; the first reached cutoff is sticky.
- Inputs must be retrieved and provably visible by cutoff. New conservative visibility
  receipts address gaps in existing HTTP/sequence timestamps and mapping history.
- Book/schedule/market freshness stays fixed at 300 seconds for v1. It does not
  generally tolerate one missed cycle. Keep tick-before-collection ordering and
  accept stale-input abstention; no extra witnessing phase or coverage-driven increase.
  Publication grace remains a separate 150 seconds, with actual delay recorded.
- Separate cutoff-information eligibility/probabilities, operational publication
  status/veto, and later settlement eligibility. Post-cutoff warnings may veto a
  publication but cannot rewrite the cutoff decision or its research population.
- Never pool the `cutoff-reconstruction` and `forward-shadow` evaluation views.
  Reconstruction uses qualifying pre-cutoff evidence regardless of later invocation
  time; forward scoring also requires actual timely publication. Report failed,
  vetoed, late, unconfirmed and unattempted opportunities in forward coverage.
  Synthetic evidence remains labeled and separate in either view.
- A future bounded collector phase must finish discovery/identity/eligibility and
  commit an immutable per-pass summary before requesting books. Preserve page/source
  references, candidate counts, eligibility coverage, completeness and its own
  availability receipt in the existing next tick. Book failure cannot change that
  phase's completeness. Incomplete/ambiguous discovery fails closed. This requires
  a future collector/interface change; none is implemented here or added to PR #4.
- One deterministic event/contract selection per game: lexical event ticker, lower
  numerical MLB team ID's YES contract. No price/outcome selection or opposing double count.
- Constant and same-book Decimal midpoint baselines; paired comparisons, complete
  observed universe/exclusion ledger, broader constant coverage separately. Constant
  eligibility is independent of book success; midpoint additionally needs a valid
  book. Paired samples are their shared valid games within each evaluation view.
- Only finalized consistent Kalshi binary settlement scores; MLB results remain separate.
  Exceptional payouts and later corrections retain their evidence and prior reports.
- Explicit proposed schema 3 with seven additive tables, including independent
  `discovery_passes` and `forecast_publications`. Cutoff decisions, publication
  records and settlement joins have separate evidence and reason lists. Current
  code still supports only schemas 1/2. No migration was created or run.
- Finite offline research commands first; no scheduler installation/hook is authorized
  by this planning task. Future schedule integration must be separately reviewed.

## Grounding and skill provenance

Inspected AGENTS.md; baseline, settlement and operations handoffs; plan/executor;
integration evidence; storage and both migrations; collector/eligibility/book parser;
mapping and settlement journal/models/service/schema; provenance/replay/CLI/config;
operations runner/schema guards; relevant price, identity, correction, replay, lock
and integration tests. Historical docs have stale milestone/privacy statements;
the reference integration commit and current user scope take precedence.

Applied Cool Coder `planner`, `system-design`, `data-systems-design`,
`detail-planning` at version **1.2.0**, commit
`e46e79805be0ee5877fa9bc993492064bbb40aa5`. Read-only upstream comparison matched
all **24 files** (1 planner, 8 system-design, 14 data-systems-design, 1 detail-planning).
Shared installations and local modifications were preserved; no tooling upgraded.

This review revision applies **planner** and **data-systems-design** at the same
pinned version, using the existing installation read-only. The larger skill set
and 24-file comparison above describe the original specification pass.

The first upstream comparison using system Python HTTPS failed certificate validation.
Retried with the existing system curl and verified TLS, comparing the archive in memory;
no TLS bypass, downloads in Git or certificate/tooling changes were made.

## Verification and publication

This task verifies documentation, not F3 functionality. The acceptance matrix and
commands in the specification are future implementation checks. Existing integration
test counts are historical evidence, not rerun or claimed as forecast verification.
No live market calls, test databases or service smoke jobs are needed for this task.

The original documentation pass checked its two-file allowlist, relative Markdown
targets, fenced blocks, whitespace and three synthetic Decimal Brier values.
Those checks are historical, not verification of this revision.

Revision checks: exact two-file allowlist against `3c49998`, relative Markdown
targets, balanced fenced blocks, whitespace, Decimal example arithmetic and the
timeline/350-second/coverage examples. Read through the changed clock, population,
mode, scoring, schema, scheduler and acceptance contracts for contradictions.
These are documentation consistency checks, not executed application regressions.
The future timing acceptance sweep covers cutoff offsets, collection durations,
witness delays, publication latency and one missed cycle; it has not been run
against an implementation. Discovery tests must hold sources/receipts identical
while varying only book success, preserving common eligibility and constant coverage.

The timeline example fixes T=20:00, C=19:00, postponement observed 19:00:30:
first attempts at 19:00:10 versus 19:01 have identical cutoff eligibility and
reconstructed probabilities. The earlier attempt can publish; the later one vetoes.
Both remain in cutoff coverage; forward publication counts differ. The 350-second-old
qualifying book abstains despite a missed cycle; it does not relax the 300-second rule.

Publication uses a normal commit/push without force-push, merge,
PR #4 edits or deployment. The SHA is returned to the operator rather than
self-embedded in this commit.

Reproducible documentation checks:

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence-forecast-spec
git diff --check
git diff --stat 3c49998ac8356ec9c4d974292e2c4f8e03b92d82
git diff --name-only 3c49998ac8356ec9c4d974292e2c4f8e03b92d82
# Expected: only this handoff and docs/specs/forecast-evaluation-v1.md.
git status --short
```

## Next engineer's exact sequence

1. Read the spec and obtain the independently reviewed integration commit. Compare
   changes to schema guards, health kinds, mapping, replay and CLI against the pinned
   reference. Resolve the explicitly provisional interfaces before implementing.
2. Start a new isolated implementation branch/worktree from that reviewed code;
   bring in these two documents normally. Do not switch branches in another task's
   checkout or start services/migrate a production archive.
3. Set up that worktree's environment with the existing pinned lock (`python3 -m venv
   .venv`, install the recorded uv 0.12.19 if needed, `.venv/bin/uv sync --locked`).
   No dependency or tooling upgrade is needed by the proposed design.
4. Implement F3.1 pure policies first, then F3.2 explicit schema expansion and
   compatibility tests, F3.3a independent discovery-phase persistence, F3.3b
   evidence/selection, F3.4 CLI, F3.5 separate-view evaluation, F3.6 full isolated
   verification. Commit coherent increments. Preserve collector predicates, per-book
   freshness guards and legacy-schema behavior while adding the schema-3 phase boundary.
5. Run planned focused/full tests, operations suite, package build, installed-wheel
   smoke and populated backup/restore/replay. Distinguish fixtures from any later
   real forward demonstration; absence of a qualifying opportunity is inconclusive.
6. Publish implementation for review. Do not infer approval to deploy, change visibility,
   implement later milestones or promote a strategy from this documentation task.

No known unresolved contradiction remains among the three review decisions.
Remaining implementation prerequisites are integration review reconciliation/migration number,
legacy availability limitations, measured coverage under the proposed freshness/grace,
and any future production scheduling authorization. Defaults are recommended in
spec §10; none prevents building and testing the pure logic and isolated evidence ledger.
