# Setup and source audit — 2026-09-27

Authenticated repository access and the remote were verified before creating this
checkout from the empty `JustGooogleIt/Vader-Intelligence` repository. Correction:
Git access alone did not establish privacy. On 2026-09-28 the authenticated GitHub
API reported public visibility; the checkpoint task changed it to private and
verified that result in a separate request before any push.
No project-mirror or synced reference files were edited.

Engineering skills source: [version 1.2.0, commit e46e798](https://github.com/Cool-Coder174/engineering-skills/tree/e46e79805be0ee5877fa9bc993492064bbb40aa5).
All 52 files in the ten existing engineering skills matched upstream. The built-in
installer added only eight missing folders at the exact commit, using Git transport:
capacity-engineering, engineering-consultant, incident-response, observability,
production-troubleshooting, reverse-branching, self-healing-apis, slo-engineering.
Existing skills were not overwritten. Installation root: `/Users/ashwin/.codex/skills`.

```sh
python3 /Users/ashwin/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo Cool-Coder174/engineering-skills \
  --ref e46e79805be0ee5877fa9bc993492064bbb40aa5 --method git \
  --dest /Users/ashwin/.codex/skills \
  --path skills/capacity-engineering skills/engineering-consultant \
  skills/incident-response skills/observability skills/production-troubleshooting \
  skills/reverse-branching skills/self-healing-apis skills/slo-engineering
```

This is the completed installation command, not an instruction to reinstall over
existing folders. Planning consulted planner, system-design, data-systems-design,
systems-programming, security-engineering; execution consulted detail-planning,
implement and verify. Setup also consulted skill-installer and OpenAI Docs.

Primary sources were rechecked with Python HTTPX at 2026-09-27 15:17:34 UTC, all
HTTP 200. SHA-256 fingerprints of the retrieved bytes:

| Source | Bytes | SHA-256 |
|---|---:|---|
| [Fixed point](https://docs.kalshi.com/getting_started/fixed_point_migration.md) | 5730 | `f2669602e88a6a42f92a1608775e90a6881de115d6539686d12b0e8f07d94a76` |
| [Order books](https://docs.kalshi.com/getting_started/orderbook_responses.md) | 8577 | `bb40a4b3df75841b1779058081fdbb1d7b830f8d170195fba4165ecbc606e43b` |
| [Market schema](https://docs.kalshi.com/api-reference/market/get-markets.md) | 21632 | `1e692342bea2dfa20d2df870beda954a507d011513ecf903f96e3b5b05f83dc4` |
| [Rate limits](https://docs.kalshi.com/getting_started/rate_limits.md) | 12742 | `fbb87855470a6cf3f57758dc0ae489d9e68fbbdc69e1f88915fb4349500ef6e3` |
| [Baseball terms](https://assets.kalshi.com/contract_terms/BASEBALLGAMEWIN.pdf) | 49979 | `46b02443153f4692acb3bac3d3aedabe93e837b08c80323013c8dce117ebb6e7` |

Production data sources are `external-api.kalshi.com/trade-api/v2`,
`assets.kalshi.com`, and the official MLB `statsapi.mlb.com/api/v1/schedule` feed.
Subsequent live-check requests archive their own bytes and retrieval fingerprints
in SQLite. Documentation fetches above were a setup audit, not observations.

Alias review: the live contract `KXMLBGAME-26SEP271510COLCWS-CWS` uses “Chicago WS”.
Official MLB game 824542 names home team 145 “Chicago White Sox”, away team 115
“Colorado Rockies”, with the same 2026-09-27 19:10 UTC start. That exact alias was
added to the reviewed table; ambiguous “Chicago” remains unsupported.
