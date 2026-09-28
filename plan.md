# Vader Intelligence

Accepted 2026-09-27. Implementation authorization covers milestone 1 only.

## Requirements and design
- Sports only: MLB `KXMLBGAME`, full single-game team winner contracts, pregame only.
- One Python 3.11+ process, SQLite on the operator's Mac, public GET requests only.
- Archive immutable source bytes, SHA-256, retrieval identity/time, rules/documents,
  versioned entities, quotes, volume, open interest and full order books.
- Decimal strings / Decimal arithmetic; never conflate missing values and zero.
- Verified official game identity/status/start is required before book collection.
- Hash deduplication applies to bytes, not distinct retrievals. Replay is idempotent.
- SQLite WAL, FULL synchronous commits, foreign keys, process lock, atomic response +
  normalization + checkpoint. No network calls inside write transactions.
- Bounded memory, requests, retries and pagination; gaps and partial runs remain visible.
- No trades, paid services, forecasts, agent manager or scheduler in milestone 1.
- Provenance v1 reserves prompt/model/settings/input/output/evaluation/cost links;
  only collection metadata is populated now. Future research agents do not own scoring
  or promotion gates. Development and protected evaluation data remain separate.

## Phase F1: Discovery and collection
Status: Complete — verified 2026-09-27
- [x] Establish isolated authenticated checkout and update skills without overwrites.
- [x] Recheck API docs, exact price semantics and public connectivity.
- [x] Package, TOML config, CLI, lockfile, setup/runbook.
- [x] Transactional archive, normalization, replay, checkpoint recovery.
- [x] HTTP bounds, retries, pagination, errors and GET-only allowlist.
- [x] MLB eligibility and pregame snapshots.
- [x] Deterministic offline failure/recovery tests and independent live integration.
- [x] Verification evidence and documented limitations.

Acceptance: offline tests pass; live check archives eligible real contracts, documents,
schedule evidence and books, then reopens and verifies archive integrity. An empty
eligible universe is inconclusive, never replaced by a fixture pass.

Evidence: 93 offline tests passed; Ruff and distribution build passed. Final live
check at 16:30 UTC archived 22 books across 11 games, with verified archive hashes
after reopening. Replay of 86 responses across two live runs changed no row counts.
Details: [verification](docs/verification.md), [runbook](README.md).

## Phase F2: Mapping and official settlement — deferred
Versioned mappings and separate official MLB results / Kalshi settlement records.
Acceptance: resolve ordinary games; quarantine ambiguity; test doubleheaders,
postponements, cancellations and nonbinary payouts without inventing boolean outcomes.

## Phase F3: Timestamped baselines and evaluation — deferred
Freeze 50/50 and market-midpoint forecasts at T-60 minutes from already available inputs;
abstain on stale/missing observations. Acceptance: reproducible Brier/log loss and coverage,
one game per scoring unit, versioned evaluation. Accuracy, paper P&L, risk and compute cost
are separate; no combined reward score.

## Phase F4: Scheduled operation — deferred
macOS launchd without overlapping collectors; tmux is inspection, not durable state.
Acceptance: 24-hour run, forced restart, network/sleep gaps and restored backup; freshness,
incomplete-run and storage-pressure health checks. This Mac was selected by the user.

## Evidence and sizing
GitHub repository initially empty. Public Kalshi and MLB requests returned HTTP 200.
2026-09-27 14:49 UTC: 15 MLB open events; Houston and San Francisco sampled books
had $0.01 spreads. Displayed depth is not guaranteed executable liquidity.
MLB postseason starts September 29; future listings/liquidity are not guaranteed.
At 30 markets/minute: 43,200 books/day; estimated 8 KB/observation => 0.35 GB/day.
Single-node SQLite meets this load. At 10x, HTTP cadence and disk growth constrain us
before a multi-service architecture is justified. No paid infrastructure authorized.

## Skill provenance
Engineering skills upstream version 1.2.0, commit
`e46e79805be0ee5877fa9bc993492064bbb40aa5` (Cool-Coder174/engineering-skills).
All pre-existing 52 skill files matched upstream; eight missing directories were added
using the inspected built-in Codex installer with `--method git --ref <commit>`.
Planning: planner, system-design, data-systems-design, systems-programming,
security-engineering. Execution: detail-planning, implement, verify. Setup consulted
skill-installer and OpenAI Docs. No unrelated configuration was changed.
