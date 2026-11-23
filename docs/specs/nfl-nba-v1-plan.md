# NFL and NBA verticals (A5): source decision and implementation plan

Status: **plan**, 2026-11-23. Decision D-020 (`docs/roadmap/decisions.md`) records the
source choice. Nothing here is implemented yet. The owner delegated the source choice
("do due diligence and choose after stack ranking approaches"); D-005 had blocked A5 on
exactly that choice.

## 1. What changed since D-005

D-005 (2026-10-19) found Kalshi ready for both leagues and no league-official schedule
source that is free, reachable by a script and permitted by its terms. Rechecked on
2026-11-23 with a handful of public requests:

| Check | Result |
|---|---|
| `cdn.nba.com` schedule and live scoreboard JSON | Still **403 Access Denied** to scripts |
| nflverse `schedules` release (`nflverse/nflverse-data`, CC-BY-4.0) | Reachable; regenerated automatically every 10–30 min; `timestamp.json` gives the content time (e.g. `2026-11-23 05:06:22 EST`) |
| sportsdataverse `espn_nba_schedules` release (`sportsdataverse/sportsdataverse-data`, MIT) | Reachable; current-season file updated about daily (2026-11-22 12:34Z) |
| FixtureDownload NBA feed | Reachable, but the opener is listed at 19:00Z (3 pm ET), most likely a mislabelled local time |
| Kalshi `KXNFLGAME` / `KXNBAGAME` open markets | 34 / 62 markets (17 / 31 games); every team code maps to exactly one `custom_strike` team UUID |

## 2. Ranking

Criteria, in order: permitted automated access for this use; correctness of start time
and identity; freshness of status; coverage (season types, neutral sites, makeups);
cost and credentials.

**NFL**
1. **nflverse `schedules` release — chosen.** Open licence on GitHub, built for
   programmatic download (nflreadr/nflreadpy). ET `gameday` + `gametime` match Kalshi's
   ET-date tickers; `location` marks neutral sites; carries ESPN and GSIS ids;
   regenerated every 10–30 min. Gaps: no preseason (Kalshi lists preseason: those
   candidates stay ineligible), no explicit status field (pregame is derived from start
   time and absence of a result, bounded by content age).
2. ESPN site API: real-time status and contract-named (#2 source agency), but the Disney
   terms forbid automated access. Rejected.
3. nfl.com / api.nfl.com: terms forbid systematic retrieval; the API needs tokens. Rejected.

**NBA**
1. **sportsdataverse `espn_nba_schedules` release — chosen.** MIT licence on GitHub; ESPN
   game ids, UTC start, `time_valid`, `status_type_name`, `neutral_site`, `season_type`,
   team abbreviations. Gap: refreshed about daily, so same-day postponements can lag.
2. balldontlie API: permitted and near real time, but needs an account key. Upgrade path
   once the owner supplies a key (they said they would look for keys).
3. ESPN site API (terms forbid automation), `cdn.nba.com` / `stats.nba.com` (blocked and
   restrictive terms), FixtureDownload (times look wrong). Rejected.

Neither chosen source is league-official. Both are derived from ESPN or league feeds by
open-data projects. Settlement remains Kalshi's own result; the schedule source only
establishes identity, the scheduled start (and so C), and pregame status.

## 3. Constraints that shape the design

- **Size.** nflverse `games.csv` is 2.1 MB (`games.csv.gz` 0.5 MB); the NBA current
  season CSV is 3.4 MB and grows to about 9 MB. The transport caps JSON at 2 MiB.
- **Redirects.** GitHub release downloads answer 302 to `release-assets.githubusercontent.com`
  (or `objects.githubusercontent.com`); the transport forbids redirects.
- **Disk.** This Mac has about 2.8 GB free. Blobs deduplicate identical bytes, but a
  changed whole-season file is a new blob. Polling every 2 minutes is pointless for a
  source that changes every 10 minutes to a day, and would multiply stored versions.
- **Freshness.** MLB's guard re-fetches the live schedule within 120 s of a book
  request. These sources cannot say more than their own content time.

## 4. Design

**League registry.** `leagues/{nfl,nba}.py`: series ticker, rules wording, team table
keyed by Kalshi `custom_strike` UUID with the Kalshi code and the source abbreviation
(NFL: Kalshi `JAC`/`LAR` vs nflverse `JAX`/`LA`; NBA: ESPN short codes such as `GS`, `NY`,
`NO`, `SA`, `UTAH`, `WSH` to be confirmed from the file), and season-type handling.

**Identity.** A candidate is (league, ET date from the ticker, away, home) from the event
ticker, confirmed by the rule text ("… vs … game originally scheduled for Nov 30, 2026")
and both markets' team UUIDs. Exactly one schedule row must match (ET date, away, home);
zero is `unmapped_game`, more than one is `ambiguous_game`. Makeup games carry new
tickers and match their new date. Kalshi time fields are never used for the start.

**Schedule fetch.** A separate stage per league, at most every 10 minutes (NFL) or 60
minutes (NBA), with a per-stage byte cap (NFL `games.csv.gz` ≤ 2 MiB compressed and ≤ 8 MiB
inflated; NBA CSV ≤ 16 MiB). One bounded redirect hop is allowed only from an allowlisted
`github.com/<owner>/<repo>/releases/download/<tag>/<asset>` to the two GitHub asset hosts,
recorded in the fetch row. Conditional requests (`If-None-Match`) avoid re-downloading an
unchanged file. Decompression is bounded.

**Eligibility (per league, versioned `nfl-rule-v1` / `nba-rule-v1`).** Eligible only if:
unique match; start time known (NBA `time_valid`); status not final, live, postponed or
cancelled (NBA status; NFL: no result recorded); `now < start − pregame_buffer`; the
schedule's **content time** is no older than a per-league bound (NFL 2 h, NBA 36 h); the
schedule **retrieval** is no older than the existing per-book guard requires; the Kalshi
market is `active`; exactly two complementary markets. Preseason and exhibition
opponents are ineligible in v1.

**Protocols and cohorts.** New protocol ids `nfl-t60-v1` and `nba-t60-v1`; the collector
configuration hash differs per league, so cohorts never mix with MLB (spec §5). NFL ties
settle at $0.50 each and are already excluded from binary scoring as scalar payouts.

## 5. Increments

| # | Increment | Done when |
|---|---|---|
| 1 | Pure layer: league registry and team tables, ticker/rule identity, schedule parsers for both sources, eligibility predicates | Unit tests on synthetic rows shaped like the real files, including abbreviation mismatches, neutral sites, makeups, preseason, unknown times, stale content |
| 2 | Transport: allowlisted GitHub release download with one bounded redirect, per-stage caps, conditional GET, bounded gzip | Offline transport tests; no other host or redirect admitted |
| 3 | Collector: per-league discovery pass (series, events, schedule at its own cadence, eligibility, books) on schema-3+ archives | Collector tests per league; MLB behaviour unchanged |
| 4 | Forecast tick and evaluation per league protocol; settlement mapping using the schedule source for identity only | Synthetic end-to-end per league; outcome is Kalshi's |
| 5 | Bounded live forward shadow, NFL first, on an isolated archive | One real decision recorded or an honest `inconclusive` |

No production change is part of this plan. Any live shadow writes only to an isolated
archive under the shadow directory, and respects the disk floor.
