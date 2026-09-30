# Forecast/evaluation specification handoff

Date: 2026-09-30. Documentation-only branch `docs/forecast-evaluation-spec`, created
in `/Users/ashwin/Documents/github-repos/Vader-Intelligence-forecast-spec` from exact
integration reference `9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7`.
Repository intentionally public; visibility unchanged. PR #4 and its worker branch
are untouched. No application code, dependency, database or running service changed.

## Deliverable and decisions

[Detailed F3 specification](../specs/forecast-evaluation-v1.md) defines the full
implementation contract, examples, new records, bounded CLI, acceptance tests and
small implementation increments. It is **not an implementation or a deployment**.

- T−60 follows witnessed schedule history; the first reached cutoff is sticky.
- Inputs must be retrieved and provably visible by cutoff. New conservative visibility
  receipts address gaps in existing HTTP/sequence timestamps and mapping history.
- Default book/schedule/market age is 300 seconds; publication grace is 150 seconds,
  with actual delay recorded. Both are versioned, separately configurable research policy.
- Forward-shadow, historical reconstruction and synthetic runs remain separate;
  uncertain availability and late/unconfirmed publication cannot claim forward success.
- One deterministic event/contract selection per game: lexical event ticker, lower
  numerical MLB team ID's YES contract. No price/outcome selection or opposing double count.
- Constant and same-book Decimal midpoint baselines; paired comparisons, complete
  observed universe/exclusion ledger, broader constant coverage separately.
- Only finalized consistent Kalshi binary settlement scores; MLB results remain separate.
  Exceptional payouts and later corrections retain their evidence and prior reports.
- Explicit proposed schema 3 with five additive tables; current code still supports
  only schemas 1/2. No migration was created or run.
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

The first upstream comparison using system Python HTTPS failed certificate validation.
Retried with the existing system curl and verified TLS, comparing the archive in memory;
no TLS bypass, downloads in Git or certificate/tooling changes were made.

## Verification and publication

This task verifies documentation, not F3 functionality. The acceptance matrix and
commands in the specification are future implementation checks. Existing integration
test counts are historical evidence, not rerun or claimed as forecast verification.
No live market calls, test databases or service smoke jobs are needed for this task.

Documentation checks passed: exact two-file allowlist, valid relative Markdown
targets, balanced fenced blocks, no trailing whitespace, and independent Decimal
recalculation of the three synthetic Brier values. Reviewed the specification against
the request and actual source interfaces, including the mutable discovery summary
gap; visibility receipts explicitly preserve that summary rather than trusting a
later resumed run. Publication uses a normal commit/push without force-push, merge,
PR #4 edits or deployment. The SHA is returned to the operator rather than
self-embedded in this commit.

Reproducible documentation checks:

```sh
cd /Users/ashwin/Documents/github-repos/Vader-Intelligence-forecast-spec
git diff --check
git diff --stat 9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7
git diff --name-only 9d3d1ea2f167d5154dd6c67b3f47f2c7640fced7
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
   compatibility tests, F3.3 evidence/selection, F3.4 CLI, F3.5 evaluation, F3.6 full
   isolated verification. Commit each coherent increment; preserve collector behavior.
5. Run planned focused/full tests, operations suite, package build, installed-wheel
   smoke and populated backup/restore/replay. Distinguish fixtures from any later
   real forward demonstration; absence of a qualifying opportunity is inconclusive.
6. Publish implementation for review. Do not infer approval to deploy, change visibility,
   implement later milestones or promote a strategy from this documentation task.

Unresolved items are bounded: integration review reconciliation/migration number,
legacy availability limitations, measured coverage under the proposed freshness/grace,
and any future production scheduling authorization. Defaults are recommended in
spec §10; none prevents building and testing the pure logic and isolated evidence ledger.
