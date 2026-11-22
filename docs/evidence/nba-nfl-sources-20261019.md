# NBA / NFL extension research for Vader-Intelligence

Retrieved 2026-10-19, between 05:26 and 05:31 UTC. This was read-only work: no credentials were used, no orders were placed, and no repository was touched.
Labels: **VERIFIED** means I saw it in a response body, a response header, or an official document. **INFERENCE** means I reasoned it out or it comes from a secondary source.
Kalshi base URL: `https://external-api.kalshi.com/trade-api/v2` (shortened to `$K` below).

## 0. GET ledger (16 public, unauthenticated data GETs; one more than the ~15 budget)
| # | URL | Status |
|---|-----|--------|
| 1 | `$K/series/KXNBAGAME` | 200 |
| 2 | `$K/series/KXNFLGAME` | 200 |
| 3 | `$K/markets?series_ticker=KXNFLGAME&limit=5` | 200 |
| 4 | `$K/historical/markets?series_ticker=KXNFLGAME&limit=5` | 200 |
| 5 | `$K/markets?series_ticker=KXNBAGAME&limit=5` | 200 |
| 6 | `$K/historical/markets?series_ticker=KXNBAGAME&limit=5` | 200 |
| 7 | `$K/historical/markets?series_ticker=KXNFLGAME&limit=1000` (1 page, no cursor) | 200 |
| 8 | `$K/markets?series_ticker=KXNFLGAME&status=settled&limit=1000` (1 page) | 200 |
| 9 | `$K/events?series_ticker=KXNFLGAME&limit=2` | 200 |
| 10-12 | `$K/historical/markets?series_ticker=KXNBAGAME&limit=1000` (3 pages, cursor exhausted) | 200 |
| 13 | `$K/markets?series_ticker=KXNBAGAME&status=settled&limit=1000` (1 page) | 200 |
| 14-15 | `https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json` (plain, then full browser headers) | **403** |
| 16 | `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2025&seasontype=2&week=4` | 200 |

Docs consulted with WebFetch/WebSearch on 2026-10-19 (these were not data GETs): Kalshi API docs, Kalshi contract PDFs, the NBA and NFL terms of use, the Disney (ESPN) terms of use, the nba_api GitHub issue, nflverse, and NBA.com key dates.

---
## 1. Kalshi game-winner series

### 1.1 Series metadata (VERIFIED, GETs #1 and #2)
| Field | KXNBAGAME | KXNFLGAME |
|---|---|---|
| title | "NBA Game" | "NFL Game" |
| exchange_index (series) | **3** | **0** |
| product_metadata (series) | `{"scope":"Game"}` | `{"scope":"Game"}` |
| settlement_sources | the Governing League `https://www.nba.com`, then ESPN `https://www.espn.com` | the Governing League `https://www.nfl.com/` (only) |
| contract_terms_url | `https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf` | `https://assets.kalshi.com/contract_terms/FOOTBALLGAMEWIN.pdf` |
| contract_url | `.../regulatory/product-certifications/BASKETBALLGAMEWIN.pdf` | `.../FOOTBALLGAMEWIN.pdf` |
| fee_type / frequency | quadratic_with_maker_fees / custom | same |
| last_updated_ts | 2026-10-14T17:19:35Z | 2026-10-06T14:58:29Z |

**product_metadata at the event level** (VERIFIED for NFL, GET #9): `{"competition":"Pro Football","competition_scope":"Game"}`. The same events also showed `mutually_exclusive: true`, `collateral_return_type: "MECNET"`, `sub_title: "DAL vs PHI (Oct 26)"` and `title: "Dallas vs Philadelphia"`.
NBA event product_metadata was not fetched (INFERENCE: it likely reads `"Pro Basketball"`/`"Game"`, but this is unverified).
The market objects have **no product_metadata key at all** (VERIFIED from the key list of a live NBA market).

### 1.2 Ticker format (VERIFIED)
- Event ticker: `KX{NBA|NFL}GAME-{YY}{MON}{DD}{AWAY}{HOME}`. Market ticker: `{event}-{TEAMCODE}`.
  Examples: `KXNFLGAME-26OCT26DALPHI-PHI`, `KXNBAGAME-26OCT25MEMSAC-SAC`, `KXNBAGAME-26JUN13NYKSAS-NYK`.
- **No start time is encoded.** The ticker carries only a date. MLB's `KXMLBGAME-26OCT192008CLETB` carries HHMM; NBA and NFL tickers do not. Across all 1,516 NBA and 474 NFL settled events, every ticker matched `\d{2}[A-Z]{3}\d{2}` followed by the team codes.
- **The date is the US Eastern calendar date, not the UTC date and not the venue-local date.**
  - VERIFIED by matching against ESPN (GET #16): SEA@ARI kicked off at `2025-09-26T00:15Z` and its ticker is `25SEP25SEAARI`. CIN@DEN kicked off at `2025-09-30T00:15Z` and its ticker is `25SEP29CINDEN`.
  - The contract PDFs say "All times are interpreted in Eastern Time (ET)."
  - The Melbourne game is `26SEP10SFLAR`. INFERENCE: it was played on Sep 11 local time in Melbourne.
- **Team order is away team, then home team, even at neutral sites.**
  - VERIFIED: the title "Game 5: New York at San Antonio" goes with `26JUN13NYKSAS`.
  - VERIFIED: 2025 NFL week 4 lines up 16 for 16 with ESPN's home/away flags, including Dublin (`25SEP28MINPIT`, which ESPN shows as home=PIT with neutralSite=true).
  - Other international games follow the same rule: São Paulo `25SEP05KCLAC`, London `25OCT05MINCLE`, Berlin (NBA) `26JAN15MEMORL`, London (NBA) `26JAN18ORLMEM`.
- **Titles say "vs" or "at" inconsistently over time**, so they cannot be used to detect neutral sites (VERIFIED).
  - NBA: "vs" for every 2025 event; "at" for 2026 Jan–Jun; "<Team> wins" from Oct 2026.
  - NFL: "at" for the 2025 regular season and preseason; "vs" for the HOF game and some others; "wins" in 2026.
- **There is no scheduled-start field. Start time can only be backed out, and the semantics drift** (VERIFIED from observed fields; the formulas are INFERENCE).
  - Live markets (Oct 2026): `close_time = latest_expiration_time = kickoff + 48h`, and `expected_expiration_time = occurrence_datetime = kickoff + 3h`.
    - DAL@PHI MNF: close `2026-10-29T00:15Z`, occurrence `2026-10-27T03:15Z`, which implies an 8:15pm ET kickoff.
  - Late Sep 2026 NFL: `occurrence = kickoff + 3h` but `expected_expiration = kickoff + 6h`.
  - Some 2025–Jan 2026 NBA markets have `occurrence_datetime: null`.
  - Historical NBA markets have `expiration_time = start + 14d`.
  - After settlement, `close_time` becomes the actual close time.
  - **Conclusion: an external schedule source is required for scheduled start time.**
- **Team codes:**
  - NFL: 33 codes for 32 teams. The Rams were `LA` through Jan 2026 and `LAR` from Aug 2026. Both use the same `custom_strike.football_team` UUID `422d5fd7-…`.
  - Kalshi NFL codes are `JAC` and `WAS`; ESPN uses `JAX` and `WSH` (VERIFIED in GET #16).
  - NBA: 30 NBA tricodes plus exhibition opponents `GUA` (Guangzhou) and `LIO` (London Lions).
  - Display labels drift: "LA" or "Los Angeles C" for LAC, "Los Angeles" or "Los Angeles L" for LAL, "NOLA Saints", "DAL Cowboys".
  - **Key on `custom_strike.{basketball|football}_team` UUIDs, not on codes or labels.** Each code maps to exactly one UUID (VERIFIED).

### 1.3 Contract structure
- **There are exactly two complementary binary markets per game, one YES market per team** (VERIFIED: every one of the 1,516 NBA and 474 NFL settled events has exactly 2 markets).
- No "Tie" strike is listed for NFL. In a tie both markets settle `result:"scalar"` with `settlement_value_dollars:"0.5000"` (VERIFIED, 7 unique NFL tie events):
  - 25AUG07LVSEA, 25AUG10MIACHI, 25AUG17JACNO, 25SEP28GBDAL (the 40-40 game; ESPN shows "Final/OT" with neither team marked winner), 26AUG13INDNE, 26AUG28SEAKC, 26OCT11NYGWAS.
  - Five of the seven are preseason games.
- Fair-price voids sum to $1.00 across the pair (VERIFIED, NBA):
  - 26JAN25DALMIL: 0.56/0.44, settled 2026-02-02.
  - 26JAN25DENMEM: 0.62/0.38.
  - 26JAN08MIACHI: 0.31/0.69, settled 2026-01-14.
- **Makeup games get a new event ticker** (VERIFIED): `26MAR31DALMIL`, `26MAR18DENMEM`, `26JAN29MIACHI`. So 2025-26 has 1,233 NBA regular-season events against 1,230 games. The six teams in those three games each have 83 events; every other team has 82. A fixture key of (date, away, home) therefore keeps both the voided original and the makeup game.
- exchange_index (VERIFIED): all NFL markets are 0. NBA markets were 0 up to and including the June 2026 Finals, and 3 from the Oct 2026 preseason onward. Store exchange_index per market.

### 1.4 Rules wording (VERIFIED quotes)
**NFL, live (KXNFLGAME-26OCT26DALPHI-PHI):**
- rules_primary: "If Philadelphia wins the Dallas vs Philadelphia Pro Football game originally scheduled for Oct 26, 2026, then the market resolves to Yes."
- rules_secondary: "…If the game ends in a tie, the market will resolve to $0.50 for each team. If the game is postponed but begins within 48 hours from its originally scheduled start time, the market will remain open and resolve based on the official final result. If the game is not started within 48 hours, the market will resolve to a fair price."

**NFL, 2025 season (KXNFLGAME-25DEC27BALGB-GB):**
- rules_secondary: "The following market refers to the team who is the winner (or participant awarded a tie) in the Baltimore at Green Bay professional football game originally scheduled for Dec 27, 2025. If this game is postponed or delayed, the market will remain open and close after the rescheduled game has finished (within two weeks). If the cancelled game is not played or is rescheduled further than two weeks out, the market will resolve to a fair price … If the game is declared a tie, the market will resolve to 50/50 for both teams."
- The 2025 HOF game said "(within two years)… declared a tie … 50/50".
- Playoff text has no date. `26JAN25LASEA` reads "…Los Angeles R at Seattle professional football **divisional round** game". A separate `26JAN18LACHI` event also exists, so INFERENCE: the Jan 25 game was really the conference championship and Kalshi's round label is wrong. **Do not parse round labels.**

**NBA, live and Oct 2026 preseason (KXNBAGAME-26OCT25MEMSAC-SAC):**
- rules_primary: "If Sacramento wins the Memphis vs Sacramento Pro Basketball game originally scheduled for Oct 25, 2026, then the market resolves to Yes."
- rules_secondary: "If the game is postponed but begins within 48 hours of its originally scheduled start time, the market will remain open and resolve based on the official final result. If the game is cancelled or not started within 48 hours of its originally scheduled start, all markets will resolve to a fair price."

**NBA, 2025-26 and 2026 playoffs:** rules_secondary is the empty string in 2,726 of 3,032 markets. Example primary text: "If San Antonio wins the Game 5: New York at San Antonio professional basketball game originally scheduled for Jun 13, 2026…".
**NBA, Apr–May 2025:** "…If this game/series is postponed or delayed, the market will remain open and close after the rescheduled game/series has finished (within two years). If the cancelled game/series is not played, rescheduled, or declared a tie, the market will resolve to 50/50 for all teams."

### 1.5 Contract PDFs (VERIFIED; extracted text; the PDFs are living documents)
**BASKETBALLGAMEWIN.pdf** (created 2026-09-29):
- Source Agencies, in order: the governing league, ESPN, AP, Reuters, Fox Sports, the official broadcaster.
- The default time period includes overtime.
- Tie with no Tie strike: $0.50 each.
- Cancelled before start and not rescheduled within 48h: "last fair price".
- Suspended after start: counts as complete only if 48 minutes of NBA play are reached or the league declares the result official.
- Forfeit before start: fair price.
- Latest expiration is one week after the game, at 10:00 AM ET.

**FOOTBALLGAMEWIN.pdf** (created 2026-10-06):
- Source Agencies, in order: the league, ESPN, CBS, Fox, NBC, AP, WSJ, the broadcaster.
- Overtime is included.
- Tie: $0.50 each.
- Suspended before 55 minutes of play without resumption within 48h: fair price.
- Postponed but started within 48h of the original date: stays open.
- **Venue change:** the market stays open only if the home/away designation is unchanged and the game is played within 48h. A reversed designation means fair price.
- **Neutral site:** "home and away designations follow official league or organizer designations."
- Historical markets were governed by earlier text (for example, the "within two weeks" wording), so record the rules text per market.

### 1.6 Settled-market counts (VERIFIED; counted on 2026-10-19, deduplicated by ticker)
**KXNBAGAME: 3,032 settled markets, 1,516 games.**
- Historical endpoint: 2,898 markets, 3 pages at limit=1000.
  - Ticker dates 2025-04-15 to 2026-06-13.
  - 2025 play-in and playoffs: 86 games.
  - 2025 preseason (Oct 10–17): 43 games.
  - 2025-26 regular season: 1,233 events.
  - 2026 play-in and playoffs: 87 games.
- Live `status=settled`: 134 markets, all 2026 preseason (Oct 3–16, 2026), including `26OCT12LIOPOR`. No overlap with the historical set.
- Absent: the 2025 NBA Cup final on Dec 16 (no `25DEC16` event). The Dec 13 semifinals are present.

**KXNFLGAME: 948 settled markets, 474 games.**
- Historical: 700 markets, settlement range 2025-08-01 to 2026-08-16.
  - 2025 preseason: 49 games, including the HOF game.
  - 2025 regular season plus the WC, DIV and CONF rounds: 284 games (272 + 12).
  - Early Aug 2026 preseason.
- Live settled: 280 markets.
- **32 tickers (16 events from Aug 2026) appear in both endpoints**, so deduplicate.
- Absent: Super Bowl LX, 2026-02-08 (no `26FEB` events). INFERENCE: it sits in a separate series.

Kalshi docs: `/historical/markets` takes limit ≤1000. Markets that settled before `market_settled_ts` (from `GET /historical/cutoff`) are served from the historical endpoints.
Sources: `https://docs.kalshi.com/api-reference/historical/get-historical-markets` and `https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps`.

**Upcoming markets** (VERIFIED): NFL games are listed about 12–13 days ahead (open_time 2026-10-13 for Oct 25–26). NBA 2026-27 regular-season markets opened 2026-10-18 and 2026-10-19 for Oct 25 games.

---
## 2. Schedule and result sources

### 2.1 NBA
**A. `https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json` (official NBA CDN)**
- **VERIFIED 403 "Access Denied" (Akamai) on 2026-10-19**: two attempts, the second with a Chrome User-Agent, `Referer`/`Origin: https://www.nba.com` and Accept headers.
- The 403 response carries `access-control-allow-origin: https://www.nba.com`.
- Corroboration (secondary): nba_api issue #665 (`https://github.com/swar/nba_api/issues/665`, opened 2026-04-17) cites `.../staticData/scheduleLeagueV2_1.json`. Comments in that issue:
  - 2026-05-04: it "stopped working".
  - 2026-05-14: "Access Denied" from many VPNs and devices.
  - 2026-05-22: a header workaround fixed it.
  - 2026-07-21: the "header fix no longer works".
- Authority: highest (league-official, with NBA gameIds). INFERENCE: the file covers the current season only.
- **Assessment: not reliably reachable by a scripted client today.**

**B. `stats.nba.com` (for example `/stats/scheduleleaguev2`)** — not tested (budget).
- Secondary reports: it requires browser headers and blocks cloud IPs and non-browser TLS fingerprints (Akamai). No published rate limit; the community uses about 600ms between calls.
- Undocumented and unsupported. INFERENCE: historical seasons are available via the `Season=` parameter.

**C. Legacy `https://data.nba.com/data/10s/v2015/json/mobile_teams/nba/{YYYY}/league/00_full_schedule.json`**
- Source: pbpstats code, `https://pbpstats.readthedocs.io/en/latest/_modules/pbpstats/data_loader/data_nba/schedule/web.html`.
- Not tested. INFERENCE: probably retired or stale.

**D. NBA terms of use** (`https://www.nba.com/termsofuse`, "LAST UPDATED: July 13, 2026", VERIFIED via fetch). Section 9 says NBA Statistics:
- "may only be used, displayed, or published for legitimate news reporting or private, non-commercial purposes";
- may not be used with "any website, product, or service that features a database … of regularly updated statistics without … express prior consent";
- require attribution to NBA.com.
- A schedule/results database feeding a market-research system is at least arguably covered by this.

**E. ESPN NBA scoreboard** (`site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`) — not tested.
- Same caveats as 2.2-B below.
- Notable: Kalshi's KXNBAGAME series metadata names ESPN as a settlement source.

**NBA verdict: there is no authoritative, free, reachable, ToU-clean source today → BLOCKED.**

### 2.2 NFL
**A. Official NFL** — no free public schedule or results API exists (INFERENCE from searches; no official developer program found).
- `api.nfl.com` requires OAuth tokens, which is out of bounds (credentials).
- The legacy scorestrip XML feed is reported retired.
- Licensed alternatives: Sportradar, SportsData.io (API keys, paid or trial).
- NFL terms of use (`https://www.nfl.com/legal/terms/`, "Updated: May 16, 2024", VERIFIED via fetch):
  - "Systematic retrieval of data or other content from the Services … is prohibited absent our express prior written consent"
  - "solely for your own individual non-commercial and informational purposes".

**B. ESPN `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`** — unofficial and undocumented. VERIFIED working on 2026-10-19 (GET #16).
- **Shape (VERIFIED):** top level has `leagues`, `season{type:2,year:2025}`, `week{number:4}`, `events[]`.
- Each event has `id` (e.g. `401772632`), `date` (UTC, minute precision, e.g. `2025-09-28T13:30Z`), `shortName` ("SEA @ ARI"; "MIN VS PIT" when neutral), and `status.type{name:"STATUS_FINAL",state:"post",completed:true,detail:"Final"|"Final/OT"}`.
- Each event's `competitions[0]` has `neutralSite` (true for Croke Park, Ireland), `venue{fullName,address.country}`, and `competitors[]{homeAway, team.abbreviation, score, winner}`. In a tie, both teams have `winner:false` (GB 40 – DAL 40).
- History (VERIFIED): `?dates=2025&seasontype=2&week=4` returned the full 2025 week 4. INFERENCE: `?dates=YYYYMMDD` and `seasontype=1/2/3` work for preseason, regular season and postseason.
- Headers: `cache-control: max-age=1`, `access-control-allow-origin: *`, no rate-limit headers. No published rate limit.
- Postponed or canceled statuses would appear as values such as `STATUS_POSTPONED` or `STATUS_CANCELED` (INFERENCE; not observed).
- Terms: ESPN falls under the Disney terms of use (`https://disneytermsofuse.com/english/`, "Last Updated: May 24, 2024", VERIFIED). Section 2.B.x prohibits accessing or extracting "using a robot, spider, script, or other automated means", and 2.B.viii prohibits commercial use. Stability: no SLA or documentation; it can change without notice.
- Authority: ESPN is the #2 Source Agency in Kalshi's FOOTBALLGAMEWIN and BASKETBALLGAMEWIN contract terms, so it is contract-recognized but not league-official.

**C. nflverse / nfldata `games.csv`** — community (Lee Sharpe), free, on GitHub. Docs: `https://github.com/nflverse/nfldata/blob/master/DATASETS.md`; releases at `https://github.com/nflverse/nflverse-data/releases`.
- Fields (per docs):
  - game_id; alt_game_id (`season_week_away_home`);
  - game_type REG, WC, DIV, CON or SB;
  - gameday; gametime in **Eastern time**;
  - away_team and home_team (prefers `JAX` and `LAR`);
  - scores; `location` Home or **Neutral**;
  - cross-reference IDs **gsis, pfr, pff, espn**.
- **Excludes preseason**, but Kalshi lists preseason.
- Coverage: about 1999 onward per secondary sources; the DATASETS.md text says 2006 and links an old `habitatring.com` URL.
- Not authoritative and update latency is unknown. INFERENCE: the current file lives at `nflverse/nfldata/master/data/games.csv` (not fetched).

**NFL verdict: no official free source exists → BLOCKED for "authoritative".** Unofficial fallbacks work technically (ESPN, verified) but carry terms-of-use risk.

---
## 3. League-specific identity hazards

### NBA
- **Preseason is listed on Kalshi** (VERIFIED): 43 games in 2025 and 67 in 2026, including non-NBA clubs (GUA, LIO). They need a season-type flag and must be excluded from or segregated in model training.
- **International and neutral sites** (VERIFIED via NBA.com key dates, `https://www.nba.com/news/key-dates`, "Updated on October 18, 2026"):
  - 2026-27: Mexico City on Nov 7 (Nuggets vs Pacers), Paris on Jan 14, 2027 and Manchester on Jan 17, 2027 (Spurs vs Pelicans).
  - Preseason: Quebec City Oct 3, Vancouver Oct 10, Macao Oct 9 and 11.
  - Kalshi always uses the designated home team, so venue must come from the schedule source.
- **NBA Cup** (VERIFIED dates): group play Oct 30 – Nov 27, quarterfinals Dec 4–5, semifinals Dec 8 and/or 9, championship Dec 11 at Hinkle Fieldhouse, Indianapolis. This is the first final outside Las Vegas, and INFERENCE (secondary sources): the knockout rounds are in team markets.
  - The final does not count in the standings (secondary: ESPN and Wikipedia).
  - INFERENCE: only 80 games per team are published up front, and the remaining two are slotted in December after group play, so the schedule gains games mid-season.
  - VERIFIED: the 2025 Cup final is absent from KXNBAGAME.
- **Postponements** (VERIFIED): voided original plus a new makeup ticker (see 1.3). A schedule join must map both to the same NBA gameId, or the makeup to a new one, and label the void.
- **Playoffs**: game-number prefixes appear in titles ("Game 5: …"). If-necessary games may be listed and later voided (INFERENCE).
- **Abbreviations**: Kalshi uses NBA tricodes. INFERENCE: ESPN uses GS, NY, NO, SA, UTAH, WSH, which needs a mapping table. Labels drift ("LA", "New York K").

### NFL
- **Ties are real** (VERIFIED): 7 tie events in 474 games, 5 of them preseason; settled at 0.5. INFERENCE: preseason has no overtime and the regular season has a 10-minute overtime. **Models must allow a third outcome.**
- **International games in 2026** (secondary: `https://www.profootballhof.com/news/nfl-releases-schedule-of-international-games-for-2026`, CBS):
  - Melbourne Sep 10 (SF–LAR), Rio Sep 27 (BAL–DAL), London Oct 4 (IND–WAS), Oct 11 (PHI–JAC) and Oct 18 (HOU–JAC, Wembley).
  - Paris Oct 25 (PIT at NO), Madrid Nov 8 (CIN at ATL), Munich Nov 15 (NE at DET), Mexico City Nov 22 (MIN–SF).
  - The matching Kalshi tickers `26SEP10SFLAR`, `26SEP27BALDAL`, `26OCT04INDWAS`, `26OCT11PHIJAC` and `26OCT18HOUJAC` were VERIFIED to exist.
  - The ET date can differ from the venue date (Melbourne). Early ET kickoffs exist: INDWAS close 16:41Z.
- **Flex and rescheduled kickoffs** (secondary: NBC syndicated `https://www.nbcwashington.com/news/sports/nfl/nfl-flex-scheduling-games-how-change-works/`):
  - SNF: weeks 5–10 (twice) and 11–17, with 12 days' notice (6 days from week 14).
  - MNF: weeks 12–17, 12 days' notice.
  - TNF: twice per season in weeks 13–17, 21 days' notice.
  - Kalshi identity is anchored to the "originally scheduled" ET date, so a Sunday→Monday flex can happen after listing (markets open about 12–13 days ahead). Store both the original and the actual kickoff.
- **Bye weeks**: no game, so no ticker; harmless. Teams play at most one game per date, so (ET date, away, home) is unique apart from makeups.
- **Season-type mixing**: KXNFLGAME holds preseason, REG, WC, DIV and CONF, but not the Super Bowl. Round labels in the rules can be wrong (see 1.4).
- **Abbreviations** (VERIFIED):
  - Kalshi `JAC`, `WAS`, `LA`→`LAR` versus ESPN `JAX`, `WSH`, `LAR`; nflverse prefers `JAX` and `LAR`.
  - Kalshi labels drift between "Los Angeles R", "LA Rams" and "NOLA Saints".
  - A 3-way mapping table (Kalshi code, Kalshi UUID, schedule-source code) is required.

---
## 4. Recommendation
1. **Neither league can be supported defensibly today under the MLB-equivalent bar** (official league source, free, publicly reachable by a script, with no credential use).
   - **NBA → mark BLOCKED.**
     - The official schedule JSON returned **403 Access Denied** to scripted requests on 2026-10-19 (VERIFIED, including with browser headers), and community reports show this since May–July 2026.
     - stats.nba.com is protected the same way (secondary).
     - The NBA terms of use restrict statistics databases without consent.
   - **NFL → mark BLOCKED.**
     - No official free NFL schedule or results endpoint exists.
     - The nfl.com terms prohibit systematic retrieval.
     - The only working free feeds are unofficial: ESPN (Disney terms prohibit automated access) and nflverse (community-maintained, no preseason).
2. **Kalshi-side data is ready for both leagues** (VERIFIED):
   - Public series, markets and historical endpoints work without auth.
   - NBA: 1,516 settled games. NFL: 474 settled games.
   - Two complementary markets per game.
   - Stable team UUIDs.
   - The Kalshi side is not the blocker; the schedule-source policy is.
3. **If the project accepts a "contract-named secondary source" policy** (ESPN is Source Agency #2 in both Kalshi contracts, and KXNBAGAME series metadata names ESPN):
   - **Pilot NFL first, research-only.**
     - Small calendar: about 285 regular-season and playoff games plus about 49 preseason games per year.
     - ESPN's NFL shape is VERIFIED, including neutralSite, ties and history.
     - nflverse offers an independent cross-check, with ESPN, GSIS and PFR IDs.
   - **NBA second**, after handling preseason or exhibition opponents, makeup-game duplicates, Cup-final exclusion and mid-season TBD games.
   - Either way, record the ToU risk, cache aggressively at low request rates, and never treat ESPN as league-official.
4. **Required before any build:**
   - A per-league team map keyed by Kalshi UUID.
   - An ET-date fixture key: (league, season_type, ET date, away, home), plus a makeup/void flag.
   - Scheduled start taken from the schedule source, never from Kalshi time fields.
   - A tie (0.5) outcome class for NFL and a fair-price-void class for both leagues.
   - Per-market storage of exchange_index and rules text.
