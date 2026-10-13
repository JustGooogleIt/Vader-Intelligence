# F3.3b-remainder and F3.4 implementation checklist: requirement → code → test

Status: living checklist kept with the implementation branch. Written 2026-10-12 by the
implementing session, stacked on PR #10 (`feat/f33-record-contract-2`, tip `841af2a`),
which is on PR #9 (`feat/f33a-discovery-boundary`, `8f9f2c0`), on PR #8
(`docs/f33-implementation-plan`, `deea149`), on `main` `ebb58df`.

Governing documents: [forecast/evaluation specification](forecast-evaluation-v1.md) §2
(decision clock, schedule selection, three decisions), §3 (population and contract
selection), §4 (probability and abstention), §6 (persistence), §7 (interfaces, tick,
CLI). The F3.3 plan (`f33-implementation-plan.md`) governs the already-delivered evidence
half; this block builds the decision/publication/tick path the plan deferred (§8.2 rows
"Later" and the spec §8 steps F3.3b and F3.4). PRs #9 and #10 are **provisional
dependencies under review**; this branch does not modify their reviewed files beyond
additive extension of the witness to new subject kinds.

Scope split (from the spec §8 step table and §7):

- **F3.3b remainder**: identity bindings, contract selection at the cutoff, book-input
  selection, decision generation, publication generation. Modules `forecast/selection.py`
  (`select_inputs`), `forecast/service.py`, `forecast/publication.py`.
- **F3.4**: the research-configuration parser, the `forecast tick` invocation path, and
  `forecast inspect`. Modules `forecast/research.py`, `forecast/tick.py`, `cli.py`.

Legend: ✅ implemented and tested on this Mac; ⬜ deferred with a documented handoff.

## Research configuration (F3.4)

| # | Requirement (source) | Code | Test | State |
|---|---|---|---|---|
| R-1 | One versioned research protocol, not tunable during a cohort; canonical config hash required; reject invalid/nonfinite settings (spec §2) | `forecast/research.py:Protocol`, `load`, `config_hash` | `test_research.py` | ✅ |
| R-2 | v1 defaults: protocol_id mlb-t60-v1, horizon 3600, book/schedule/market max age 300, grace 150, epsilon 0.000001, max_games 100, max_contracts 200, budget 15 (spec §2) | `research.py` defaults | `test_research.py` | ✅ |
| R-3 | A separate research-config argument/parser, not unknown fields in the collector `[collector]` parser (spec §1, §7) | `research.py` reads `[research]`; collector `Config` untouched | `test_research.py`, `test_cli` | ✅ |

## Decision and publication generation (F3.3b remainder)

| # | Requirement (source) | Code | Test | State |
|---|---|---|---|---|
| D-1 | Pre-C prepared, witnessed identity binding: gamePk, original rule date/start, home/away IDs, official game number, YES team, reviewed terms hash, evidence tuple (spec §3.1) | `service.py:prepare_binding` | `test_service.py` | ✅ |
| D-2 | Contract selection fixed before reading prices: smallest event_ticker, lower-team-ID YES contract, both teams required, no fallback (spec §3.2–3.4) | reuse `selection.select_contract`; `service.py` | `test_service.py` | ✅ |
| D-3 | Latest completed book retrieval received and witnessed by C, 0..300 s, both sides present, valid; else abstain, no backward search (spec §4) | `selection.py:select_inputs` | `test_selection_inputs.py` | ✅ |
| D-4 | Constant 0.5 when common cutoff checks pass; midpoint additionally needs the book; later fields never erase a stored probability (spec §4) | `service.py:decide_opportunity` using `baselines`, `policy.cutoff_eligibility` | `test_service.py` | ✅ |
| D-5 | Selection uses only evidence available at C; later arrivals cannot leak in; 300 s applied exactly; HTTP Date confers nothing (spec §2, §4) | `evidence` projection + `select_inputs`; no header read | `test_service.py`, `test_vertical_slice_f34.py` | ✅ |
| D-6 | Decision provenance reproduces inputs and result; immutable; distinct decision/publication/observation/persistence times (spec §2, §6) | decision/publication envelopes + manifests | `test_service.py`, replay | ✅ |
| D-7 | Collector vs research config hashes distinct; cohort isolation; contract-2 decision pins its pass (spec §5, §9.2) | `service.py` sets research hash; pin from pass | `test_service.py` | ✅ |
| D-8 | Three independent decisions; publication verdict/timeliness from `classify_publication`; publication is a separate immutable record (spec §2, §6) | `publication.py:decide`, `service.py:publish` | `test_service.py`, crash tests | ✅ |

## Tick and selection service (F3.4)

| # | Requirement (source) | Code | Test | State |
|---|---|---|---|---|
| T-1 | Tick is finite: witness prior evidence and summaries, prepare/witness bindings, compute due decisions, attempt publication, record missed, exit (spec §7, §2) | `forecast/tick.py:tick` | `test_tick.py` | ✅ |
| T-2 | Process receipts before choosing sources; a receipt made now never authorizes an input for an earlier C (spec §7) | tick order; `evidence` uses observed_at ≤ C | `test_tick.py` | ✅ |
| T-3 | Repeated invocation of the same logical tick is idempotent (decision key excludes time/ticker) (spec §6) | decision/binding/publication idempotency keys | `test_tick.py` | ✅ |
| T-4 | Concurrent invocation: one writer, the other fails `writer_busy`; no duplicate/retroactive forecast (spec §6, §7) | `storage.writer_lock`; keys | `test_tick.py` | ✅ |
| T-5 | Restart after interruption; failure before/during/after persistence leaves no partial forecast; unknown stays unknown (spec §6) | per-record transactions; tick re-derives | `test_tick.py` | ✅ |
| T-6 | No eligible markets/evidence → recorded abstention batch, not a fabricated error (spec §7 exit codes) | tick abstention path | `test_tick.py` | ✅ |
| T-7 | Multiple candidates and deterministic tie-breaking (spec §3) | `select_contract` determinism | `test_service.py` | ✅ |
| T-8 | Schedule boundaries and late execution → missed/late publication, cutoff facts intact (spec §2) | `classify_publication` | `test_tick.py` | ✅ |
| T-9 | Exposed through `vader forecast tick --research-config ... --budget N` and `forecast inspect --limit N`; no daemon/scheduler (spec §7) | `cli.py` | `test_cli` | ✅ |
| T-10 | Exit 0 complete, 3 inconclusive, 1 failed/partial, 130 interrupted (spec §7) | `cli.py:main` mapping | `test_cli` | ✅ |

## Inspection and replay (F3.4)

| # | Requirement (source) | Code | Test | State |
|---|---|---|---|---|
| I-1 | Inspect answers: what the tick considered, eligible-at-C evidence, why selected/rejected, policy/config, published vs abstained; read-only, keyset pagination, max 100 (spec §7) | `tick.py:inspect`, `cli.py` | `test_tick.py`, `test_cli` | ✅ |
| I-2 | Replay reproduces the same result from the same persisted inputs; no present-day fetch; diagnostic recompute distinct from new immutable records (spec §6, §7) | `replay` unchanged preserves; decisions reproducible via re-derive helper | `test_vertical_slice_f34.py` | ✅ |

## Deferred, with handoff

| # | Item | Why deferred | Where |
|---|---|---|---|
| X-1 | Schedule-revision state machine (`policy.decide_schedule`, moving C, multi-revision binding chains) (spec §2) | Large, orthogonal to the single-revision eligible/abstain path; the decision key already makes the first C sticky | handoff |
| X-2 | `forecast reconstruct` (archive-only historical campaign) (spec §7) | Its own command and dataset/source-ceiling semantics; the forward tick is the smallest complete path | handoff |
| X-3 | F3.5 evaluation materialization (`evaluate`, `evaluation inspect`, outcomes join) | Explicitly out of scope for this assignment; upstream contracts it needs are completed here | handoff |
