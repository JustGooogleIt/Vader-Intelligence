"""Bounded read projections for the Command Center.

Every projection returns a JSON-safe dict with `state` (`ok`, `empty`, `stale`,
`unavailable`, `not_configured`), `source_time` (the newest timestamp the data itself
carries, or None) and `generated_at` (when this projection was computed). Nothing here
writes. Archive reads use short read-only connections with a small busy timeout, so a
busy archive yields `unavailable`, never a hang. Unknown usage or cost is reported as
`None` ("unknown"), never as zero.
"""

import json
import time
from datetime import datetime, timezone

from ..pods.store import open_store as open_pods_store
from ..pods.store import status as pods_status
from .config import CenterConfig
from .readers import Unavailable, archive, has_table, loads, now_iso, ops_state, schema_version
from .store import CenterStore

MODES = ("synthetic", "forward_shadow", "historical_reconstruction")
EVIDENCE_LIMIT = 100
DECISION_LIMIT = 100


def _ts(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, ValueError):
        return None


def _newest(*values):
    moments = [m for m in (_ts(v) for v in values if v) if m is not None]
    return max(moments).isoformat() if moments else None


def _wrap(state, data, source_time, now, **extra):
    return {
        "state": state,
        "source_time": source_time,
        "generated_at": now_iso(now),
        **extra,
        **data,
    }


def unavailable(reason, now, **extra):
    return _wrap("unavailable", {"reason": reason}, None, now, **extra)


# Archive ----------------------------------------------------------------------------------


def archive_summary(cfg: CenterConfig, now=None):
    """Schema, runs and the newest retrieval time; the whole archive's freshness."""
    try:
        with archive(cfg.archive) as db:
            version = schema_version(db)
            runs = db.execute(
                "SELECT id, kind, started_at, finished_at, status FROM runs ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
            latest = db.execute(
                "SELECT MAX(retrieved_at) FROM fetches WHERE state='ok'"
            ).fetchone()[0]
            counts = {
                t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("fetches", "entities", "observations", "eligibility")
            }
            data = {
                "schema_version": version,
                "label": cfg.labels.get("archive"),
                "runs": [dict(r) for r in runs],
                "counts": counts,
                "latest_retrieval": latest,
            }
    except Unavailable as exc:
        return unavailable(str(exc), now)
    state = "empty" if counts["fetches"] == 0 else "ok"
    return _wrap(state, data, latest, now)


def _latest_eligibility(db):
    """Latest eligibility row per ticker (by id), with its data."""
    rows = db.execute(
        "SELECT e.* FROM eligibility e JOIN (SELECT ticker, MAX(id) AS id FROM eligibility GROUP BY ticker) m"
        " ON m.id=e.id ORDER BY e.ticker"
    ).fetchall()
    return [dict(r) | {"data": loads(r["data_json"], {})} for r in rows]


def _entities(db, kind, keys=None):
    """Latest entity record per ticker of a kind (highest id)."""
    if keys is not None and not keys:
        return {}
    where = ""
    params = [kind]
    if keys is not None:
        where = f" AND ticker IN ({','.join('?' * len(keys))})"
        params.extend(keys)
    rows = db.execute(
        "SELECT e.ticker, e.parent_ticker, e.fetch_id, e.data_json FROM entities e JOIN"
        " (SELECT ticker, MAX(id) AS id FROM entities WHERE kind=?"
        + where
        + " GROUP BY ticker) m ON m.id=e.id",
        params,
    ).fetchall()
    return {r["ticker"]: dict(r) | {"data": loads(r["data_json"], {})} for r in rows}


def _games_index(db, now):
    """Games as the archive identifies them: latest eligibility per ticker grouped by the
    official game id, with the MLB schedule entity and the market/event entities."""
    rows = _latest_eligibility(db)
    tickers = [r["ticker"] for r in rows]
    markets = _entities(db, "market", tickers)
    games = {}
    unmapped = []
    for row in rows:
        market = markets.get(row["ticker"], {})
        event = market.get("parent_ticker") or (market.get("data") or {}).get("event_ticker")
        game_id = row["game_id"]
        if game_id is None:
            unmapped.append(
                {"ticker": row["ticker"], "reason": row["reason"], "checked_at": row["checked_at"]}
            )
            continue
        game = games.setdefault(
            game_id,
            {
                "game_id": game_id,
                "event_tickers": set(),
                "contracts": [],
                "cutoffs": set(),
                "checked_at": None,
            },
        )
        game["event_tickers"].add(event)
        game["cutoffs"].add(row["cutoff"])
        game["contracts"].append(
            {
                "ticker": row["ticker"],
                "event_ticker": event,
                "eligible": bool(row["eligible"]),
                "reason": row["reason"],
                "cutoff": row["cutoff"],
                "checked_at": row["checked_at"],
                "schedule_fetch_id": row["schedule_fetch_id"],
                "market_fetch_id": row["market_fetch_id"],
                "mapping_version": (row["data"] or {}).get("mapping_version"),
            }
        )
        game["checked_at"] = _newest(game["checked_at"], row["checked_at"])
    schedule = _entities(db, "mlb_game", [str(g) for g in games])
    # Tickers of the same event whose latest eligibility names another game: ambiguity.
    by_event = {}
    for g in games.values():
        for event in g["event_tickers"]:
            by_event.setdefault(event, set()).add(g["game_id"])
    out = []
    for game_id, g in sorted(games.items()):
        entity = schedule.get(str(game_id), {}).get("data") or {}
        teams = entity.get("teams") or {}
        start = entity.get("gameDate")
        cutoff = sorted(c for c in g["cutoffs"] if c)
        reasons = []
        if not entity:
            reasons.append("schedule_entity_missing")
        if len(g["event_tickers"]) != 1 or None in g["event_tickers"]:
            reasons.append("event_ticker_ambiguous")
        if any(len(by_event[e]) > 1 for e in g["event_tickers"] if e in by_event):
            reasons.append("event_maps_to_several_games")
        if len(cutoff) > 1:
            reasons.append("cutoffs_differ")
        countdown = None
        if cutoff and now is not None:
            moment = _ts(cutoff[0])
            if moment is not None:
                countdown = (moment - now).total_seconds()
        out.append(
            {
                "game_id": game_id,
                "event_ticker": next(iter(g["event_tickers"]))
                if len(g["event_tickers"]) == 1
                else None,
                "event_tickers": sorted(e for e in g["event_tickers"] if e),
                "away": (teams.get("away") or {}).get("team") or {},
                "home": (teams.get("home") or {}).get("team") or {},
                "scheduled_start": start,
                "official_date": entity.get("officialDate"),
                "status": (entity.get("status") or {}).get("detailedState"),
                "cutoff": cutoff[0] if cutoff else None,
                "countdown_seconds": countdown,
                "post_cutoff": countdown is not None and countdown < 0,
                "contracts": sorted(g["contracts"], key=lambda c: c["ticker"]),
                "eligible_contracts": sum(1 for c in g["contracts"] if c["eligible"]),
                "excluded_contracts": sum(1 for c in g["contracts"] if not c["eligible"]),
                "identity": "ambiguous" if reasons else "canonical",
                "identity_reasons": reasons,
                "can_launch": not reasons,
                "checked_at": g["checked_at"],
            }
        )
    return out, unmapped


def games(cfg: CenterConfig, now=None):
    moment = datetime.now(timezone.utc) if now is None else now
    try:
        with archive(cfg.archive) as db:
            index, unmapped = _games_index(db, moment)
            latest = db.execute("SELECT MAX(checked_at) FROM eligibility").fetchone()[0]
    except Unavailable as exc:
        return unavailable(str(exc), now)
    state = "empty" if not index and not unmapped else "ok"
    return _wrap(
        state,
        {"games": index, "unmapped": unmapped, "label": cfg.labels.get("archive")},
        latest,
        now,
    )


def game(cfg: CenterConfig, game_id, now=None, *, center=None):
    """One game's workspace: identity pins, evidence timeline, frozen decisions with
    publication status, settlement versions, evaluation items and this game's runs."""
    moment = datetime.now(timezone.utc) if now is None else now
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return unavailable("game id must be an integer", now)
    try:
        with archive(cfg.archive) as db:
            index, _ = _games_index(db, moment)
            found = [g for g in index if g["game_id"] == game_id]
            if not found:
                return _wrap("empty", {"game_id": game_id, "reason": "unknown game"}, None, now)
            head = found[0]
            tickers = [c["ticker"] for c in head["contracts"]]
            marks = ",".join("?" * len(tickers))
            eligibility = [
                dict(r) | {"data": loads(r["data_json"], {})}
                for r in db.execute(
                    f"SELECT * FROM eligibility WHERE ticker IN ({marks}) ORDER BY id DESC LIMIT ?",
                    (*tickers, EVIDENCE_LIMIT),
                )
            ]
            for row in eligibility:
                row.pop("data_json", None)
            observations = [
                dict(r) | {"data": loads(r["data_json"], {})}
                for r in db.execute(
                    f"SELECT o.id, o.fetch_id, o.ticker, o.kind, o.parser_version, o.data_json,"
                    f" f.retrieved_at FROM observations o JOIN fetches f ON f.id=o.fetch_id"
                    f" WHERE o.ticker IN ({marks}) ORDER BY o.id DESC LIMIT ?",
                    (*tickers, EVIDENCE_LIMIT),
                )
            ]
            for row in observations:
                row.pop("data_json", None)
            markets = _entities(db, "market", tickers)
            fetch_ids = sorted(
                {r["schedule_fetch_id"] for r in eligibility if r["schedule_fetch_id"]}
                | {r["market_fetch_id"] for r in eligibility if r["market_fetch_id"]}
            )
            fetches = []
            if fetch_ids:
                fetches = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT f.id, f.started_at, f.retrieved_at, f.status, f.state, r.stage, r.url, r.run_id"
                        f" FROM fetches f JOIN requests r ON r.id=f.request_id WHERE f.id IN ({','.join('?' * len(fetch_ids))})",
                        fetch_ids,
                    )
                ]
            decisions = _decisions(db, game_id, cfg)
            settlement = _settlement(db, tickers, game_id)
            evaluation = _evaluation(db, game_id)
            source = _newest(
                head["checked_at"],
                *(o["retrieved_at"] for o in observations),
                *(d["created_at"] for d in decisions),
            )
    except Unavailable as exc:
        return unavailable(str(exc), now)
    runs = _runs_for_game(cfg, game_id, center)
    data = {
        "game": head,
        "pins": {
            "game_id": game_id,
            "event_ticker": head["event_ticker"],
            "market_tickers": tickers,
            "archive_schema": None,
            "source_runs": sorted({r["run_id"] for r in eligibility}),
            "label": cfg.labels.get("archive"),
        },
        "markets": {
            t: {"fetch_id": m["fetch_id"], "parent_ticker": m["parent_ticker"], "data": m["data"]}
            for t, m in markets.items()
        },
        "eligibility": eligibility,
        "observations": observations,
        "fetches": fetches,
        "decisions": decisions,
        "settlement": settlement,
        "evaluation": evaluation,
        "runs": runs,
        "warnings": (
            ["post_cutoff: only frozen forecasts apply; later notes never rewrite them"]
            if head["post_cutoff"]
            else []
        )
        + (["identity ambiguous: launch disabled"] if head["identity"] == "ambiguous" else []),
    }
    return _wrap("ok", data, source, now)


def _decisions(db, game_id, cfg):
    """Frozen decisions for a game with their publication status (contract-level fields
    only), labelled by mode. Receipts are counted through the forecast journal's own
    reader when available."""
    if not has_table(db, "forecast_decisions"):
        return []
    rows = db.execute(
        "SELECT id, digest, run_id, mode, protocol, cutoff, binding_id, supersedes_id, payload_json"
        " FROM forecast_decisions WHERE json_extract(payload_json,'$.game_id')=?"
        " ORDER BY id DESC LIMIT ?",
        (game_id, DECISION_LIMIT),
    ).fetchall()
    out = []
    for r in rows:
        p = loads(r["payload_json"], {})
        pub = db.execute(
            "SELECT id, verdict, json_extract(payload_json,'$.manifest.policy_status') AS status,"
            " json_extract(payload_json,'$.attempted_at') AS attempted_at"
            " FROM forecast_publications WHERE decision_id=?",
            (r["id"],),
        ).fetchone()
        receipts = 0
        if pub is not None and has_table(db, "forecast_receipts"):
            receipts = db.execute(
                "SELECT COUNT(*) FROM forecast_receipts WHERE publication_id=?", (pub["id"],)
            ).fetchone()[0]
        out.append(
            {
                "decision_id": r["id"],
                "digest": r["digest"],
                "run_id": r["run_id"],
                "mode": r["mode"],
                "forward_evidence": r["mode"] == "forward_shadow",
                "protocol": r["protocol"],
                "dataset": p.get("dataset"),
                "cutoff": r["cutoff"],
                "created_at": p.get("created_at"),
                "scheduled_start": p.get("scheduled_start"),
                "supersedes_id": r["supersedes_id"],
                "frozen": {
                    k: p.get(k)
                    for k in (
                        "selected",
                        "forecasts",
                        "baselines",
                        "abstained",
                        "abstention_reasons",
                    )
                    if k in p
                },
                "publication": None
                if pub is None
                else {
                    "id": pub["id"],
                    "verdict": pub["verdict"],
                    "status": pub["status"],
                    "attempted_at": pub["attempted_at"],
                    "receipts": receipts,
                },
            }
        )
    return out


def _settlement(db, tickers, game_id):
    if not has_table(db, "settlement_versions"):
        return {"available": False, "versions": []}
    keys = [*tickers, str(game_id)]
    rows = db.execute(
        f"SELECT id, kind, entity_key, revision, change_kind, digest, first_fetch_id FROM settlement_versions"
        f" WHERE entity_key IN ({','.join('?' * len(keys))}) ORDER BY id DESC LIMIT ?",
        (*keys, EVIDENCE_LIMIT),
    ).fetchall()
    targets = []
    if has_table(db, "settlement_targets"):
        targets = [
            dict(r)
            for r in db.execute(
                f"SELECT id, run_id, ticker, state, mapping_version_id FROM settlement_targets"
                f" WHERE ticker IN ({','.join('?' * len(tickers))}) ORDER BY id DESC LIMIT ?",
                (*tickers, EVIDENCE_LIMIT),
            )
        ]
    return {"available": True, "versions": [dict(r) for r in rows], "targets": targets}


def _evaluation(db, game_id):
    if not has_table(db, "evaluation_items"):
        return {"available": False, "items": []}
    rows = db.execute(
        "SELECT i.evaluation_id, i.opportunity_key, i.decision_id, i.publication_id, i.receipt_id,"
        " r.mode, r.view, r.created_at, json_extract(i.payload_json,'$.scored') AS scored,"
        " json_extract(i.payload_json,'$.outcome') AS outcome"
        " FROM evaluation_items i JOIN evaluation_runs r ON r.id=i.evaluation_id"
        " WHERE i.game_id=? ORDER BY i.evaluation_id DESC LIMIT ?",
        (game_id, EVIDENCE_LIMIT),
    ).fetchall()
    return {"available": True, "items": [dict(r) for r in rows]}


def _runs_for_game(cfg, game_id, center):
    try:
        own = center is not None
        store = center or CenterStore(cfg.root, readonly=True)
    except FileNotFoundError:
        return []
    try:
        return [r for r in store.runs(kind="game_research") if r["pins"].get("game_id") == game_id]
    finally:
        if not own:
            store.close()


# Operations / collection ------------------------------------------------------------------


def collection(cfg: CenterConfig, now=None):
    """Collection and shadow health from the ops state (never opens the archive)."""
    if cfg.ops_config is None:
        return _wrap("not_configured", {"reason": "no operations configuration"}, None, now)
    try:
        config, history = ops_state(cfg.ops_config)
    except Unavailable as exc:
        return unavailable(str(exc), now)
    from ops.runner import shadow_health

    entries = history.get("history") or []
    latest = entries[-1] if entries else None
    moment = time.time() if now is None else now.timestamp()
    state = "empty"
    status = "never_invoked"
    age = None
    if latest:
        age = moment - latest.get("start_epoch", moment)
        status = latest.get("status")
        stale = age > config.get("cadence", 0) + config.get("grace", 0) + config.get("deadline", 0)
        state = "stale" if stale else "ok"
    shadow = shadow_health(config, latest) if "shadow" in config else None
    return _wrap(
        state,
        {
            "status": status,
            "label": config.get("test_label") and "test label" or None,
            "cadence": config.get("cadence"),
            "latest": latest,
            "age_seconds": age,
            "shadow": shadow,
            "retained": len(entries),
            "database_matches_archive": cfg.archive is not None
            and str(cfg.archive) == config.get("database"),
        },
        latest.get("ended_at") or latest.get("started_at") if latest else None,
        now,
    )


# Farms / pods ----------------------------------------------------------------------------


def farms(cfg: CenterConfig, now=None):
    """Runs from the Command Center store plus the orchestration store's status."""
    data = {
        "runs": [],
        "pods": None,
        "global_cap": cfg.global_agent_cap,
        "active_processes": None,
        "cost_usd": None,
    }
    source = None
    try:
        with CenterStore(cfg.root, readonly=True) as center:
            data["runs"] = center.runs(limit=100)
    except FileNotFoundError:
        pass
    except Exception as exc:  # a locked store is unavailable, not an error page
        return unavailable(f"command center store: {type(exc).__name__}", now)
    pods_root = cfg.root
    if (pods_root / "orchestration.sqlite3").is_file():
        try:
            store = open_pods_store(pods_root, readonly=True)
            try:
                summary = pods_status(store, now)
            finally:
                store.close()
            data["pods"] = summary
            data["active_processes"] = len(summary["active_attempts"])
            source = summary["generated_at"]
        except Exception as exc:
            return unavailable(f"orchestration store: {type(exc).__name__}", now)
    state = "ok" if data["runs"] or data["pods"] else "empty"
    return _wrap(state, data, source, now)


# Overview ---------------------------------------------------------------------------------


def overview(cfg: CenterConfig, now=None):
    moment = datetime.now(timezone.utc) if now is None else now
    arch = archive_summary(cfg, moment)
    g = games(cfg, moment)
    coll = collection(cfg, moment)
    f = farms(cfg, moment)
    upcoming = []
    eligible = excluded = 0
    coverage = None
    if g["state"] == "ok":
        for item in g["games"]:
            eligible += item["eligible_contracts"]
            excluded += item["excluded_contracts"]
        upcoming = sorted(
            (gm for gm in g["games"]),
            key=lambda gm: (gm["cutoff"] or "", gm["game_id"]),
        )[:50]
        coverage = _coverage(cfg)
    failures = []
    if coll["state"] not in ("ok", "empty", "not_configured"):
        failures.append({"source": "collection", "detail": coll.get("reason") or coll["state"]})
    if coll["state"] == "ok" and (coll.get("shadow") or {}).get("status") == "operational_failure":
        failures.append({"source": "shadow", "detail": coll["shadow"]["tick"]})
    if f["state"] == "ok" and f["pods"]:
        failures.extend({"source": "pods", "detail": x} for x in f["pods"]["recent_failures"][:10])
    budgets = {
        "global_agent_cap": cfg.global_agent_cap,
        "active_processes": f.get("active_processes"),
        "pods": (f.get("pods") or {}).get("pods"),
        "cost_usd": None,  # unknown: no provider usage is metered in this pilot
        "note": "cost and provider usage are unknown, not zero",
    }
    states = {
        "archive": arch["state"],
        "games": g["state"],
        "collection": coll["state"],
        "farms": f["state"],
    }
    return _wrap(
        "ok",
        {
            "sources": states,
            "archive": arch,
            "collection": coll,
            "games": upcoming,
            "unmapped": g.get("unmapped", []),
            "contracts": {"eligible": eligible, "excluded": excluded},
            "coverage": coverage,
            "farms": {
                "runs": f.get("runs", []),
                "active_processes": f.get("active_processes"),
                "pods": f.get("pods"),
            },
            "failures": failures,
            "budgets": budgets,
            "label": cfg.labels,
        },
        _newest(arch.get("source_time"), g.get("source_time"), coll.get("source_time")),
        moment,
    )


def _coverage(cfg):
    """Markets with a book observation among the markets named by eligibility."""
    try:
        with archive(cfg.archive) as db:
            total = db.execute("SELECT COUNT(DISTINCT ticker) FROM eligibility").fetchone()[0]
            with_book = db.execute(
                "SELECT COUNT(DISTINCT o.ticker) FROM observations o WHERE o.kind='book'"
                " AND o.ticker IN (SELECT ticker FROM eligibility)"
            ).fetchone()[0]
            newest = db.execute(
                "SELECT MAX(f.retrieved_at) FROM observations o JOIN fetches f ON f.id=o.fetch_id WHERE o.kind='book'"
            ).fetchone()[0]
    except Unavailable:
        return None
    return {"markets": total, "with_book": with_book, "newest_book": newest}


def engineering(cfg: CenterConfig, now=None):
    """Maintenance runs: task, patch/commit, checks, review, recommendation, integration state."""
    try:
        with CenterStore(cfg.root, readonly=True) as center:
            runs = center.runs(kind="maintenance", limit=100)
    except FileNotFoundError:
        return _wrap("empty", {"runs": []}, None, now)
    except Exception as exc:
        return unavailable(f"command center store: {type(exc).__name__}", now)
    return _wrap(
        "ok" if runs else "empty", {"runs": runs}, runs[0]["created_at"] if runs else None, now
    )


def activity(cfg: CenterConfig, run_id, page=1, now=None):
    """Task events of a run's tasks, newest first, `activity_page` per page."""
    try:
        with CenterStore(cfg.root, readonly=True) as center:
            run = center.run(run_id)
    except FileNotFoundError:
        run = None
    except Exception as exc:
        return unavailable(f"command center store: {type(exc).__name__}", now)
    if run is None:
        return _wrap("empty", {"run_id": run_id, "events": [], "page": page}, None, now)
    task_ids = [t["task_id"] for t in run["tasks"]]
    events = []
    if task_ids and (cfg.root / "orchestration.sqlite3").is_file():
        try:
            store = open_pods_store(cfg.root, readonly=True)
        except Exception as exc:
            return unavailable(f"orchestration store: {type(exc).__name__}", now)
        try:
            size = cfg.activity_page
            offset = (max(int(page), 1) - 1) * size
            rows = store.db.execute(
                f"SELECT id, task_id, attempt_id, kind, at, detail_json FROM task_events"
                f" WHERE task_id IN ({','.join('?' * len(task_ids))}) ORDER BY id DESC LIMIT ? OFFSET ?",
                (*task_ids, size, offset),
            ).fetchall()
            from ..pods.store import iso as pods_iso

            events = [
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "attempt_id": r["attempt_id"],
                    "kind": r["kind"],
                    "at": pods_iso(r["at"]),
                    "detail": loads(r["detail_json"], {}),
                }
                for r in rows
            ]
        finally:
            store.close()
    return _wrap(
        "ok" if events else "empty",
        {"run_id": run_id, "page": page, "page_size": cfg.activity_page, "events": events},
        events[0]["at"] if events else None,
        now,
    )


def farm(cfg: CenterConfig, run_id, now=None):
    """One run: its pins, tasks with current state and attempts, workers/panes, artifacts."""
    try:
        with CenterStore(cfg.root, readonly=True) as center:
            run = center.run(run_id)
            workers = center.workers(run_id) if run else []
    except FileNotFoundError:
        run = None
        workers = []
    except Exception as exc:
        return unavailable(f"command center store: {type(exc).__name__}", now)
    if run is None:
        return _wrap("empty", {"run_id": run_id, "reason": "unknown run"}, None, now)
    tasks = []
    if (cfg.root / "orchestration.sqlite3").is_file() and run["tasks"]:
        try:
            store = open_pods_store(cfg.root, readonly=True)
        except Exception as exc:
            return unavailable(f"orchestration store: {type(exc).__name__}", now)
        try:
            from ..pods.store import iso as pods_iso

            for binding in run["tasks"]:
                task = store.task(binding["task_id"])
                attempts = store.attempts(binding["task_id"])
                result = store.result(binding["task_id"])
                tasks.append(
                    {
                        **binding,
                        "state": task["state"] if task else "unknown",
                        "type": task["type"] if task else None,
                        "attempts": [
                            {
                                "attempt_id": a["attempt_id"],
                                "n": a["n"],
                                "worker_pid": a["worker_pid"],
                                "started_at": pods_iso(a["started_at"]),
                                "lease_expires_at": pods_iso(a["lease_expires_at"]),
                                "finished_at": pods_iso(a["finished_at"]),
                                "outcome": a["outcome"],
                                "elapsed_seconds": (
                                    (a["finished_at"] or time.time()) - a["started_at"]
                                    if a["started_at"]
                                    else None
                                ),
                            }
                            for a in attempts
                        ],
                        "result": None
                        if result is None
                        else {
                            "result_hash": result["result_hash"],
                            "ingested_at": pods_iso(result["ingested_at"]),
                            "artifacts": _artifact_refs(result),
                        },
                    }
                )
        finally:
            store.close()
    for w in workers:
        w.pop("pane_token", None)  # never shown; identity is re-verified by the observer
    return _wrap(
        "ok",
        {"run": run, "tasks": tasks, "workers": workers},
        run["finished_at"] or run["created_at"],
        now,
    )


def _artifact_refs(result):
    try:
        manifest = json.loads(result["result_json"])
    except (KeyError, TypeError, ValueError):
        return []
    refs = manifest.get("artifacts") if isinstance(manifest, dict) else None
    out = []
    for ref in refs or []:
        if isinstance(ref, dict):
            out.append({k: ref.get(k) for k in ("name", "kind", "sha256", "bytes")})
    return out


def artifact_preview(cfg: CenterConfig, run_id, sha256, limit, now=None):
    """Bounded bytes of an artifact that one of the run's results declares; text only."""
    import re

    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return unavailable("artifact id must be a sha256", now)
    f = farm(cfg, run_id, now)
    if f["state"] != "ok":
        return f
    declared = {
        a["sha256"] for t in f["tasks"] for a in (t.get("result") or {}).get("artifacts", [])
    }
    if sha256 not in declared:
        return _wrap("empty", {"reason": "artifact not declared by this run"}, None, now)
    from ..pods.artifacts import ArtifactCorrupted, ArtifactMissing, ArtifactStore

    try:
        data = ArtifactStore(cfg.root / "artifacts").get_bytes(sha256)
    except (ArtifactMissing, ArtifactCorrupted, OSError) as exc:
        return unavailable(f"artifact unreadable: {type(exc).__name__}", now)
    truncated = len(data) > limit
    text = data[:limit].decode("utf-8", errors="replace")
    return _wrap(
        "ok",
        {"sha256": sha256, "text": text, "truncated": truncated, "bytes": len(data[:limit])},
        None,
        now,
    )
