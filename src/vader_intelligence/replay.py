import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .normalize import normalizer
from .provenance import RunProvenanceV1, json_text, source_hash
from .transport import validate_source


def key_for(stage, url):
    if stage == "book":
        return url.split("/markets/", 1)[1].split("/", 1)[0]
    return url if stage == "document" else ""


def replay(store):
    after = 0
    processed = 0
    errors = []
    while True:
        rows = store.db.execute(
            "SELECT f.seq,f.id,f.body_sha256,b.body,r.stage,r.url,r.request_key FROM fetches f "
            "JOIN requests r ON r.id=f.request_id JOIN blobs b ON b.sha256=f.body_sha256 "
            "WHERE f.seq>? AND f.status=200 AND f.truncated=0 AND f.state IN ('ok','parse_error') "
            "ORDER BY f.seq LIMIT 10",
            (after,),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            after = row["seq"]
            if hashlib.sha256(row["body"]).hexdigest() != row["body_sha256"]:
                raise ValueError("archive body hash mismatch")
            try:
                with store.transaction():
                    if row["stage"].startswith("settlement-"):
                        from .settlement.journal import normalizer as settlement_normalizer

                        apply = settlement_normalizer(row["stage"], row["request_key"])
                    else:
                        apply = normalizer(row["stage"], key_for(row["stage"], row["url"]))
                    apply(store.db, row["id"], row["body"])
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
                if len(errors) < 100:
                    errors.append({"fetch_id": row["id"], "error": str(exc)[:300]})
            processed += 1
    return {"processed": processed, "errors": errors, "status": "partial" if errors else "complete"}


def import_fixture(store, path, config):
    p = Path(path)
    with p.open("rb") as f:
        raw = f.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("fixture exceeds 10 MiB")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1 or len(manifest["responses"]) > 1000:
        raise ValueError("invalid fixture manifest")
    run_id = manifest["run_id"]
    row = store.db.execute("SELECT id,kind FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is not None and row["kind"] != "fixture":
        raise ValueError("fixture cannot modify a live run")
    if row is None:
        store.start_run(
            RunProvenanceV1(run_id, "fixture", source_hash(), asdict(config)),
            manifest["started_at"],
        )
    for index, entry in enumerate(manifest["responses"]):
        source = validate_source(entry["url"], entry.get("params", {}))
        body = entry["body"].encode()
        existing = store.db.execute(
            "SELECT f.body_sha256,f.state,f.started_at,f.retrieved_at,r.run_id,r.stage,r.url,"
            "r.params_json FROM fetches f JOIN requests r ON r.id=f.request_id WHERE f.id=?",
            (entry["id"],),
        ).fetchone()
        if existing:
            expected = (
                hashlib.sha256(body).hexdigest(),
                entry["started_at"],
                entry["retrieved_at"],
                run_id,
                entry["stage"],
                entry["url"],
                json_text(entry.get("params", {})),
            )
            actual = tuple(
                existing[k]
                for k in (
                    "body_sha256",
                    "started_at",
                    "retrieved_at",
                    "run_id",
                    "stage",
                    "url",
                    "params_json",
                )
            )
            if actual != expected:
                raise ValueError("fixture ID reused with different bytes or provenance")
            if existing["state"] != "ok":
                raise ValueError(
                    "fixture retrieval previously failed; inspect archive and use replay"
                )
            continue
        request = store.request(
            run_id, entry["stage"], str(index), entry["url"], entry.get("params", {})
        )
        if "eligibility" in entry:
            proof = entry["eligibility"]
            with store.transaction():
                store.db.execute(
                    "INSERT INTO eligibility(run_id,ticker,checked_at,schedule_fetch_id,market_fetch_id,"
                    "game_id,eligible,reason,cutoff,data_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        proof["ticker"],
                        entry["started_at"],
                        proof["schedule_fetch_id"],
                        proof["market_fetch_id"],
                        proof["game_id"],
                        1,
                        "eligible",
                        proof["cutoff"],
                        json_text(proof),
                    ),
                )
        fetch_id = store.begin_fetch(request["id"], entry["started_at"], entry["id"])
        error = store.complete_fetch(
            fetch_id,
            body=body,
            status=200,
            headers={"source": source},
            retrieved_at=entry["retrieved_at"],
            elapsed=0,
            normalizer=normalizer(entry["stage"], key_for(entry["stage"], entry["url"])),
        )
        if error:
            raise ValueError(error)
    summary = store.summary(run_id)
    summary["status"] = "complete"
    store.finish_run(run_id, "complete", summary)
    return summary
