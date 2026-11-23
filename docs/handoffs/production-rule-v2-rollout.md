# Production rule-v2 rollout: prepared, not executed (2026-11-23)

The owner approved preparing this ("that's fine"; no rush). The deployed collector reads
public data but books nothing, because its rule parser rejects every current `KXMLBGAME`
candidate (date-only postseason rules; D-002, accepted 2026-11-23). The fix is on `main`
as the opt-in `eligibility_rules = ["rule-v1", "rule-v2"]`.

**Status: blocked on disk space, and pointless until MLB resumes.** On 2026-11-23 this Mac
had 2.5 GB free and the production operations directory used 2.1 GB. Step 2 copies the
archive to a new path, which would leave less than the collector's 512 MiB write floor.
The MLB season is over, so a live check would be inconclusive until spring training
(late February) and the regular season (late March).

## Preconditions

1. Free disk ≥ (production archive size + WAL) + 2 GiB, measured with `df -h ~` and
   `du -sh` of the archive directory immediately before step 2.
2. `main` contains the rule-v2 code (true since the 2026-11-22 source merge, #38).
3. An operator window: the scheduled collector can be stopped for about 15 minutes.
4. MLB games are listed on Kalshi (spring training or later), or the outcome of step 4
   is accepted as `inconclusive`.

## Steps

All `ops/manage.py` commands run from the stable production checkout with the
production configuration; paths in angle brackets are the operator's.

1. Record the current state: `ops/manage.py status`; note the deployed commit, the
   collector TOML and the launchd label.
2. Back up to a **new** path and verify the copy:
   `ops/manage.py backup --source <archive> --destination <new-backup-path>`, then
   `ops/manage.py inspect --database <new-backup-path>`.
3. Bounded shadow at the merged head on an **isolated** archive under
   `~/Documents/Vader-Intelligence-shadow/<date>-rule-v2/` with rule v1+v2 (the recipe in
   `docs/handoffs/claude-release-validation.md`, Task C). Confirm eligible > 0 or an
   honest `inconclusive`.
4. Operator window: `ops/manage.py stop`; update the stable checkout to the reviewed
   `main` commit; set `eligibility_rules = ["rule-v1", "rule-v2"]` in the production
   collector TOML (a new collector configuration hash, so a new cohort, spec §5); one
   manual `run`; confirm eligible > 0 or `inconclusive`; then `install`.
5. Health watch for a day: `ops/manage.py status` and `ops/manage.py health`.

No migration is needed: collection on schema 1 is unchanged. Forecasting on production
is a separate decision.

## Rollback

`ops/manage.py stop`; restore the previous checkout commit and TOML; `install`. The
archive is never overwritten or migrated by this rollout, so no data rollback is needed.
