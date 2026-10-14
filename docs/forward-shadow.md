# Isolated forward-shadow workflow

A bounded research workflow that collects public data, witnesses it, generates
prospective forecasts or explicit abstentions, and later evaluates outcomes, on
**isolated storage only**. It reuses the existing serialized operations cycle
(`ops/manage.py` + launchd) and the finite CLI commands; it adds no scheduler, daemon,
credential, paid service or new provider request. Every scheduled cycle is one
invocation: an archive-only `forecast tick`, then one bounded `collect --once`.

`forward_shadow` is a cohort mode, not a success flag. Nothing here is a trading
system; forecast accuracy is not executable PnL, and no profitability claim is made.

## Isolation and protection against production paths

The shadow is an ordinary operations configuration with a `shadow` block:

```json
"shadow": {
  "role": "forward-shadow",
  "research_config": "/abs/path/research.toml",
  "tick_budget": 15,
  "forbidden_paths": ["/Users/ashwin/Documents/github-repos/Vader-Intelligence/data"]
}
```

`ops.common.validate` refuses the configuration unless: the role is `forward-shadow`;
`test_label` is true (the launchd label is `com.vader.test.collection.<hash>`, never the
production label); the research TOML exists; `tick_budget + deadline + 2 × grace <
cadence` (the combined wrapper budget, remeasured, not the collection deadline alone);
`forbidden_paths` names at least one absolute production path; and none of the
database, state directory, collector config or research config equals or lives under a
forbidden path. The archive and state directory are explicit absolute paths of the
operator's choosing; `configure` refuses to overwrite an existing config or state.

Record the actual production archive path(s) in `forbidden_paths`. The historical
production archive described in the operations doc is `…/Vader-Intelligence/data/live.sqlite3`;
verify the real one before configuring (`docs/operations.md`).

## Start

```sh
VADER_ROOT=/Users/ashwin/Documents/github-repos/Vader-Intelligence   # stable checkout for launchd
VADER_PY="$VADER_ROOT/.venv/bin/python"
VADER_OPS="$VADER_ROOT/ops/manage.py"
SHADOW_DIR="$HOME/vader-shadow-20261014"                              # isolated, outside production
SHADOW_DB="$SHADOW_DIR/shadow.sqlite3"
SHADOW_CONFIG="$SHADOW_DIR/shadow-ops.json"
SHADOW_STATE="$SHADOW_DIR/state"
mkdir -p "$SHADOW_DIR"
printf '[research]\ndataset = "forward-shadow-20261014"\n' > "$SHADOW_DIR/research.toml"

# Schema 4 archive, explicitly, before any cycle (no command migrates implicitly).
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" db-init
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" settlement migrate
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" forecast migrate
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" forecast migrate --record-contract 2

"$VADER_PY" "$VADER_OPS" configure \
  --checkout "$VADER_ROOT" --python "$VADER_PY" --database "$SHADOW_DB" \
  --collector-config "$VADER_ROOT/ops/collector.example.toml" \
  --state "$SHADOW_STATE" --output "$SHADOW_CONFIG" \
  --cadence 120 --deadline 90 --grace 5 --test-label \
  --shadow-research-config "$SHADOW_DIR/research.toml" --tick-budget 15 \
  --forbid "$VADER_ROOT/data"

"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" plist      # inspect; registers nothing
"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" run        # one manual cycle: tick, then collect
"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" install    # launchd, test label, same preflight as production
"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" status
```

The combined budget is explicit: with cadence 120, deadline 90 and grace 5 the tick budget
is at most 19 s; `configure` refuses more. The launchd install preflight is unchanged
(stable standalone checkout, clean tree, interpreter location, archive integrity).

## Each cycle (single writer, overlap)

1. `forecast tick --research-config … --budget T`: witnesses the previous pass's
   committed fetches and discovery summary, prepares and witnesses bindings, computes due
   decisions from evidence available at C, attempts publication for eligible decisions
   lacking a record, records missed, exits. Exit 0 complete, 3 inconclusive (no due
   decision), 1 partial/failed, 130 interrupted. Its deadline is `tick_budget + grace`.
2. `collect --once`: the unchanged two-phase collection (discovery, immutable summary,
   books), deadline as configured.

Both run inside one `.ops-run.lockfile` hold, and both take the archive's own
`.lockfile` writer lock in turn. A second scheduled or manual invocation during a cycle
exits 75 `overlap_skipped` and records `last-overlap.json`; it never runs two writers.
There is no in-process retry: the next scheduled cycle is the retry. A tick failure or
deadline makes the wrapper exit 1 (visible in `launchctl` and `status`) but never
prevents the collection of that cycle.

## Evaluate (maintenance, bounded, under the same lock)

```sh
"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" shadow-evaluate \
  --from 2026-10-14 --through 2026-10-15 --settlement-limit 20 --settlement-budget 120
```

Runs `settlement refresh` (public read-only, bounded by limit/budget) and then
`research run --view forward-shadow` for the date range with `outcomes_as_of` = now,
each as a supervised child under the ops run lock, and writes
`state/shadow-evaluate-<timestamp>.json`. A scheduled cycle that fires meanwhile skips
as an overlap. Use `--skip-settlement` to re-evaluate without a refresh. Reports:

```sh
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" research inspect
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" evaluation report --id N
"$VADER_PY" -m vader_intelligence.cli --db "$SHADOW_DB" forecast inspect --limit 20
```

## Health and status

`health`/`status` add a `shadow` section computed from the latest history entry (no
archive access, so it works while a cycle holds the lock):

| `shadow.status` | Meaning |
|---|---|
| `no_eligible_markets` | Discovery completed with no eligible contracts; the tick had nothing due. Inconclusive, not a failure. An empty eligible live universe proves nothing. |
| `waiting_for_cutoff` | Eligible games were collected; no decision cutoff was reached in the last tick. |
| `forecasting` | The last tick recorded at least one decision or explicit abstention. |
| `operational_failure` | The tick or the collection failed, timed out, produced invalid output or was interrupted. |
| `inconclusive` | Neither failure nor evidence of eligible markets. |
| `never_invoked` | No shadow cycle has run. |

The collection status, gaps, storage pressure and scheduler state are reported as
before and separately.

## Logs

Per slot: `stdout-N.log`/`stderr-N.log` (collection) and `tick-stdout-N.log`/
`tick-stderr-N.log` (tick), bounded by `log_bytes`, rotated with the history
(`retention` invocations). Maintenance steps write `shadow-settlement-<ts>` and
`shadow-research-<ts>` logs. The tick output is the tick summary JSON (run id, counts,
witness summaries, errors). There are no credentials anywhere in this system: the
providers are public read-only endpoints, and the configuration holds only paths and
integers, so nothing is redacted and nothing secret can leak into a log.

## Restart and recovery

- Crash inside a tick before a record commits: nothing half-written (each record is one
  `BEGIN IMMEDIATE` transaction); the next cycle re-derives.
- Crash after a decision commits but before its publication: the next tick publishes
  at its actual time (`allowed` inside the window, `missed` after), never backdated.
- Crash during collection: `Store.recover` on the next invocation marks pending
  attempts interrupted; a resumed run gets a new discovery pass.
- Mac sleep or a missed cycle: recorded as a gap; evidence older than 300 s abstains.
- `stop` / `restart` / `uninstall` are the unchanged lifecycle commands; the
  registration and lock inodes are never deleted blindly.

## Stop and clean up

```sh
"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" stop
"$VADER_PY" "$VADER_OPS" --config "$SHADOW_CONFIG" uninstall
# Preserve or remove the isolated directory explicitly; nothing else refers to it.
"$VADER_PY" "$VADER_OPS" inspect --database "$SHADOW_DB"   # integrity before archiving it
```

The shadow never touches a forbidden path, so cleanup is deleting `$SHADOW_DIR` after
the evidence has been copied where it is wanted.

## Finite public smoke (authorized shape)

Separate test storage, the existing rate limits, a finite number of manual `run`
cycles, then one `shadow-evaluate`. Report the actual duration, the number of cycles,
every interruption, and the `shadow.status` of each cycle. If no eligible MLB markets
exist (for example outside the season or between games), the honest result is
`no_eligible_markets`; do not expand to another league or relabel synthetic evidence.
A 24-hour gate is only passed by a 24-hour run.
