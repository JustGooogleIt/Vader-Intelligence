"""Minimal immutable storage API over explicit caller-supplied envelopes.

Production callers hold storage.writer_lock around these finite SQLite transactions.
No function selects evidence, witnesses visibility, produces a forecast, or joins an
outcome. Supplied timestamps are stored assertions, not proof of forward publication.
"""

import hashlib
import json
import time

from . import contracts
from .references import digest, reference, row_dict, validate_references
from .schema import COLUMNS, PROJECTIONS, check_schema, require_schema, transaction


class IdempotencyConflict(ValueError):
    pass


# Mode relationships for contract-2 decisions (plan §9.3). Child → parent pairs.
M9_DECISION_BINDING = {
    ("synthetic", "synthetic"),
    ("forward_shadow", "forward_shadow"),
    ("historical_reconstruction", "forward_shadow"),
    ("historical_reconstruction", "historical_reconstruction"),
}
M10_DECISION_DISCOVERY = {
    ("synthetic", "synthetic"),
    ("forward_shadow", "forward_shadow"),
    ("historical_reconstruction", "forward_shadow"),
}


def parent(db, table, row_id, mode, allowed=None):
    """Load a parent row and check the child→parent mode relationship.

    Contract 1 requires equal modes. Contract-2 callers pass the allowed pair set.
    """
    row = row_dict(db, table, {"id": row_id})
    if table in COLUMNS:
        parent_mode = json.loads(row["payload_json"])["mode"]
        if allowed is None:
            if parent_mode != mode:
                raise ValueError("incompatible parent mode")
        elif (mode, parent_mode) not in allowed:
            raise ValueError("forbidden mode relationship " + f"{mode}→{parent_mode}")
    return row


def run_configuration_digest(db, run_id):
    row = db.execute("SELECT provenance_json FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("missing run")
    return digest(json.loads(row[0])["configuration"])


def record_contract(db, table, row_id):
    """Contract declared by a stored row's own payload; never guessed from the archive."""
    row = row_dict(db, table, {"id": row_id})
    return contracts.version(table, json.loads(row["payload_json"]))


def cited_receipts(value, found=None):
    """Every typed reference to a forecast_receipts row anywhere in a payload."""
    found = [] if found is None else found
    if isinstance(value, dict):
        if value.get("table") == "forecast_receipts" and isinstance(value.get("key"), dict):
            found.append(value["key"].get("id"))
        else:
            for item in value.values():
                cited_receipts(item, found)
    elif isinstance(value, list):
        for item in value:
            cited_receipts(item, found)
    return found


def correction_lineage(db, table, payload):
    """Validate every supersedes edge, including ancestry in a malformed archive.

    Checking only the immediate parent lets a previously mislabeled correction
    launder synthetic history. No recursion or unbounded chain walk.
    """
    current = payload
    visited = set()
    while current["supersedes_id"] is not None:
        identifier = current["supersedes_id"]
        if identifier in visited:
            raise ValueError("cyclic correction lineage")
        if len(visited) >= 1000:
            raise ValueError("correction lineage exceeds 1000 ancestors")
        visited.add(identifier)
        prior = json.loads(row_dict(db, table, {"id": identifier})["payload_json"])
        if prior["mode"] == "synthetic" and current["mode"] != "synthetic":
            raise ValueError("synthetic correction lineage cannot be promoted")
        if table == "evaluation_runs" and prior["mode"] != current["mode"]:
            raise ValueError("incompatible parent mode in correction lineage")
        current = prior


def validate_links(db, table, p):
    run = db.execute("SELECT kind,provenance_json FROM runs WHERE id=?", (p["run_id"],)).fetchone()
    if run is None or (run[0] == "fixture" and p["mode"] != "synthetic"):
        raise ValueError("missing run or synthetic lineage mismatch")
    provenance = json.loads(run[1])
    tool = provenance.get("tool_configuration") or {}
    if tool.get("source_lineage") == "synthetic" and p["mode"] != "synthetic":
        # M1 on persisted lineage: a run made with an injected transport or clock can
        # only write synthetic records, whatever clock a later process runs with.
        raise ValueError("synthetic lineage mismatch: run was recorded as synthetic")
    if provenance.get("code_hash") != p["code_hash"]:
        raise ValueError("record code identity differs from writing run")
    if any(
        provenance.get(k) is not None
        for k in ("prompt_version", "model_provider", "model", "model_settings")
    ):
        raise ValueError("local forecasting storage cannot claim model calls")
    validate_references(db, p)
    if table in ("forecast_decisions", "evaluation_runs"):
        correction_lineage(db, table, p)
    if (
        "source_ceiling" in p
        and p["source_ceiling"]
        > db.execute("SELECT COALESCE(MAX(seq),0) FROM fetches").fetchone()[0]
    ):
        raise ValueError("source ceiling exceeds archive")
    if table == "forecast_bindings" and p["previous_id"] is not None:
        old = parent(db, table, p["previous_id"], p["mode"])
        before = json.loads(old["payload_json"])
        if (
            any(before[k] != p[k] for k in ("candidate_key", "policy_version", "config_hash"))
            or before["revision"] + 1 != p["revision"]
        ):
            raise ValueError("invalid binding chain")
    if table in ("forecast_decisions", "forecast_bindings"):
        if (
            p["scheduled_start"] is not None
            and p["cutoff"] is not None
            and (contracts.date(p["scheduled_start"]) - contracts.date(p["cutoff"])).total_seconds()
            != 3600
        ):
            raise ValueError("T/C do not match v1 horizon")
    if table == "forecast_decisions":
        contract = contracts.version(table, p)
        if p["binding_id"] is not None:
            allowed = M9_DECISION_BINDING if contract == 2 else None
            prior = json.loads(
                parent(db, "forecast_bindings", p["binding_id"], p["mode"], allowed)["payload_json"]
            )
            if prior["config_hash"] != p["config_hash"]:
                raise ValueError("decision parent configuration mismatch")
            if any(
                prior[k] != p[k]
                for k in ("game_id", "ticker", "yes_team_id", "scheduled_start", "cutoff")
            ):
                raise ValueError("decision differs from selected binding")
        if p["discovery_id"] is not None:
            allowed = M10_DECISION_DISCOVERY if contract == 2 else None
            row = parent(db, "discovery_passes", p["discovery_id"], p["mode"], allowed)
            prior = json.loads(row["payload_json"])
            if contract == 1:
                if prior["config_hash"] != p["config_hash"]:
                    raise ValueError("decision parent configuration mismatch")
            else:
                # Exact pin instead of equality: the collector hash is never the
                # research hash, and the pin makes cohorting checkable (plan §9.2).
                pin = p["manifest"]["discovery"]
                if pin["digest"] != row["digest"]:
                    raise ValueError("discovery pin digest mismatch")
                if pin["collector_config_hash"] != prior["config_hash"]:
                    raise ValueError("discovery pin collector configuration mismatch")
            if p["cutoff_eligible"] and not prior["complete"]:
                raise ValueError("eligible decision needs complete discovery")
        if contract == 2:
            for receipt_id in cited_receipts(p):
                if receipt_id is None or record_contract(db, "forecast_receipts", receipt_id) != 2:
                    raise ValueError("contract-2 decision cannot cite a contract-1 receipt")
        if p["supersedes_id"] is not None:
            prior = json.loads(row_dict(db, table, {"id": p["supersedes_id"]})["payload_json"])
            if prior["dataset"] == p["dataset"] or any(
                prior[k] != p[k] for k in ("game_id", "unresolved_key", "horizon")
            ):
                raise ValueError(
                    "diagnostic correction needs distinct namespace and same opportunity"
                )
            if contract == 2 and contracts.version(table, prior) != 2:
                raise ValueError("contract-2 decision cannot supersede a contract-1 decision")
    if table == "forecast_publications":
        decision = parent(db, "forecast_decisions", p["decision_id"], p["mode"])
        if decision["digest"] != p["decision_digest"]:
            raise ValueError("publication decision digest mismatch")
        if json.loads(decision["payload_json"])["config_hash"] != p["config_hash"]:
            raise ValueError("publication configuration mismatch")
    if table == "forecast_receipts":
        contract = contracts.version(table, p)
        target = {
            "fetch": ("fetches", "fetch_id"),
            "binding": ("forecast_bindings", "binding_id"),
            "publication": ("forecast_publications", "publication_id"),
            "discovery_pass": ("discovery_passes", "discovery_id"),
        }.get(p["subject_kind"])
        if (
            target is None
            or p[target[1]] is None
            or sum(
                p[k] is not None
                for k in ("fetch_id", "binding_id", "publication_id", "discovery_id")
            )
            != 1
        ):
            raise ValueError("receipt needs exactly one typed subject")
        subject = parent(db, target[0], p[target[1]], p["mode"])
        if contract == 1:
            # Frozen contract-1 rule: a fetch receipt's digest is the raw body hash.
            expected = subject.get("body_sha256") if target[0] == "fetches" else subject["digest"]
            if expected != p["subject_digest"]:
                raise ValueError("receipt subject digest mismatch")
        else:
            # Contract 2 (plan §9.1): the subject is the typed reference of the row,
            # which for a fetch fingerprints the entire completed attempt, body or not.
            subject_reference = reference(db, target[0], {"id": p[target[1]]})
            expected = subject_reference["digest"] if target[0] == "fetches" else subject["digest"]
            if expected != p["subject_digest"]:
                raise ValueError("contract-2 receipt subject digest mismatch")
            if p["references"] != [subject_reference]:
                raise ValueError("contract-2 receipt needs exactly its subject reference")
            if p["config_hash"] != run_configuration_digest(db, p["run_id"]):
                raise ValueError("receipt configuration differs from its run")
            # Operational-witness boundary (plan §4.1): contract 2 means "observed by
            # the witness". The writing run must be a forecast-witness run whose
            # provenance names this receipt's observer namespace and session, so a
            # receipt supplied by any other caller is a stored assertion at most and
            # can never occupy a witness receipt key.
            if (
                run[0] != "forecast-witness"
                or tool.get("observer_namespace") != p["observer_namespace"]
                or tool.get("session_id") != p["session_id"]
            ):
                raise ValueError(
                    "contract-2 receipt requires the operational witness run that observed it"
                )
        earliest = (
            subject.get("retrieved_at")
            if target[0] == "fetches"
            else json.loads(subject["payload_json"])["created_at"]
        )
        if p["clock_status"] == "trusted" and (
            earliest is None or contracts.date(p["observed_at"]) < contracts.date(earliest)
        ):
            raise ValueError("trusted receipt predates its subject")
        if target[0] == "fetches":
            ref = reference(db, "fetches", {"id": p["fetch_id"]})
            if ref["source_mode"] == "synthetic" and p["mode"] != "synthetic":
                raise ValueError("synthetic receipt cannot be promoted")
    if table == "evaluation_runs" and p["supersedes_id"] is not None:
        previous = json.loads(parent(db, table, p["supersedes_id"], p["mode"])["payload_json"])
        if any(
            previous[k] != p[k]
            for k in ("dataset", "protocol", "config_hash", "view", "population_manifest")
        ):
            raise ValueError("evaluation correction changed cohort")
    if table == "evaluation_items":
        evaluation = json.loads(
            parent(db, "evaluation_runs", p["evaluation_id"], p["mode"])["payload_json"]
        )
        if p["run_id"] != evaluation["run_id"] or p["config_hash"] != evaluation["config_hash"]:
            raise ValueError("evaluation item provenance mismatch")
        if p["decision_id"] is not None:
            decision = json.loads(
                parent(db, "forecast_decisions", p["decision_id"], p["mode"])["payload_json"]
            )
            if decision["game_id"] != p["game_id"] or decision["horizon"] != p["horizon"]:
                raise ValueError("evaluation opportunity mismatch")
            if any(decision[k] != evaluation[k] for k in ("dataset", "protocol", "config_hash")):
                raise ValueError("evaluation decision cohort mismatch")
            for name, score in p["scores"].items():
                if score is None:
                    continue
                # A score needs the probability the decision actually froze, and the
                # discovery provenance that places the decision in exactly one
                # collector cohort; an unpinned row is honest unscored coverage only.
                if decision["results"][name]["probability"] is None:
                    raise ValueError(f"score for {name} without a stored cutoff probability")
                if decision["discovery_id"] is None:
                    raise ValueError("scored item requires discovery provenance")
        if p["publication_id"] is not None:
            publication = parent(db, "forecast_publications", p["publication_id"], p["mode"])
            if publication["decision_id"] != p["decision_id"]:
                raise ValueError("publication is for another decision")
        if p["receipt_id"] is not None:
            receipt = parent(db, "forecast_receipts", p["receipt_id"], p["mode"])
            if receipt["publication_id"] != p["publication_id"] or p["publication_id"] is None:
                raise ValueError("receipt is for another publication")
        if p["publication_status"] == "published_timely":
            if p["decision_id"] is None or p["publication_id"] is None or p["receipt_id"] is None:
                raise ValueError("timely publication requires decision, publication and receipt")
            pub = json.loads(publication["payload_json"])
            proof = json.loads(receipt["payload_json"])
            from .policy import classify_publication

            classification = classify_publication(
                scheduled_start=contracts.date(decision["scheduled_start"]),
                attempted_at=contracts.date(pub["attempted_at"]),
                published_at=contracts.date(proof["observed_at"]),
                clock_trusted=proof["clock_status"] == "trusted",
                mode=p["mode"],
            )
            if pub["verdict"] != "allowed" or classification.status != "published_timely":
                raise ValueError("publication status contradicts supplied records")
        for field, kind in (("mapping_id", "mapping"), ("mlb_id", "mlb"), ("kalshi_id", "kalshi")):
            if (
                p[field] is not None
                and row_dict(db, "settlement_versions", {"id": p[field]})["kind"] != kind
            ):
                raise ValueError("wrong settlement evidence kind")
            if p[field] is not None:
                expected = reference(db, "settlement_versions", {"id": p[field]})
                if expected not in p["references"]:
                    raise ValueError("settlement FK requires exact typed evidence reference")
        if p["y"] is not None and (
            p["kalshi_id"] is None
            or p["payout"] is None
            or contracts.probability(p["payout"]) != p["y"]
        ):
            raise ValueError("binary outcome needs matching Kalshi payout evidence")
        if p["y"] is not None:
            from ..evaluation.settlement import finalized_binary

            # Validate a supplied reference, not an outcome search or identity join.
            data = json.loads(
                row_dict(db, "settlement_versions", {"id": p["kalshi_id"]})["data_json"]
            )
            if finalized_binary(data).label != p["y"]:
                raise ValueError("label conflicts with referenced finalized binary evidence")
        if evaluation["view"] == "forward-shadow" and any(
            v is not None for v in p["scores"].values()
        ):
            if (
                p["publication_status"] != "published_timely"
                or p["publication_id"] is None
                or p["receipt_id"] is None
            ):
                raise ValueError("forward score needs publication evidence")


def require_contract_schema(db, table, p):
    """Contract 2 needs schema 4; contract 1 is closed for new receipts/decisions there."""
    contract = contracts.version(table, p)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if contract == 2 and version < 4:
        raise ValueError(
            "contract-2 records require explicit migration: "
            "vader forecast migrate --record-contract 2"
        )
    if contract == 1 and version >= 4 and table in contracts.CONTRACT_2_TABLES:
        raise ValueError("contract 1 is closed for new receipts and decisions on schema 4")


def _insert(db, table, p):
    encoded = contracts.validate(table, p)
    validate_links(db, table, p)
    key = contracts.key(table, p)
    content_digest = hashlib.sha256(encoded.encode()).hexdigest()
    # An exact retry of an existing envelope returns its original id under either
    # contract and on either schema; only a new row is subject to the contract gate.
    if table == "evaluation_items":
        old = db.execute(
            "SELECT digest FROM evaluation_items WHERE evaluation_id=? AND opportunity_key=?",
            (p["evaluation_id"], p["opportunity_key"]),
        ).fetchone()
        if old:
            if old[0] != content_digest:
                raise IdempotencyConflict("evaluation item key reused with different content")
            return None
    else:
        old = db.execute(
            f"SELECT id,digest FROM {table} WHERE idempotency_key=?", (key,)
        ).fetchone()
        if old:
            if old[1] != content_digest:
                raise IdempotencyConflict("idempotency key reused with different content")
            return old[0]
    require_contract_schema(db, table, p)
    if table == "forecast_bindings":
        last = db.execute(
            "SELECT id,payload_json FROM forecast_bindings WHERE candidate_key=? AND policy_version=? AND config_hash=? AND mode=? ORDER BY revision DESC LIMIT 1",
            tuple(p[k] for k in ("candidate_key", "policy_version", "config_hash", "mode")),
        ).fetchone()
        if (last[0] if last else None) != p["previous_id"]:
            raise ValueError("binding must extend the latest revision")
        if last:
            ignored = {"created_at", "run_id", "revision", "previous_id"}
            before = json.loads(last[1])
            if {k: v for k, v in before.items() if k not in ignored} == {
                k: v for k, v in p.items() if k not in ignored
            }:
                return last[0]
    columns = list(PROJECTIONS[table]) + ["digest", "payload_json"]
    values = [p[k] for k in PROJECTIONS[table]] + [content_digest, encoded]
    if table != "evaluation_items":
        columns.append("idempotency_key")
        values.append(key)
    return db.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        values,
    ).lastrowid


def append(db, table, payload):
    """Atomic single record; evaluation report/items use append_evaluation instead.

    Exact retry returns original ID. Reuse the original supplied creation/observation
    fields on retry; changed content, including clocks, is a conflict, not replacement.
    """
    if table not in COLUMNS or table.startswith("evaluation_"):
        raise ValueError("unsupported single-record table")
    with transaction(db):
        require_schema(db)
        return _insert(db, table, payload)


def append_evaluation(db, payload, items):
    """Complete report/items in one transaction; input items omit evaluation_id."""
    if not isinstance(items, list) or len(items) > 1000:
        raise ValueError("evaluation item limit")
    if len(contracts.json_text(items).encode()) > 10 * 1024 * 1024:
        raise ValueError("evaluation items exceed 10 MiB")
    if len({item["opportunity_key"] for item in items}) != len(items):
        raise ValueError("duplicate evaluation opportunity")
    expected = payload.get("population_manifest")
    actual = [
        {k: item[k] for k in ("opportunity_key", "decision_id", "game_id", "horizon")}
        for item in items
    ]
    if actual != expected or payload.get("report", {}).get("items_digest") != digest(items):
        raise ValueError("incomplete evaluation population/items")
    with transaction(db) as deadline:
        require_schema(db)
        identifier = _insert(db, "evaluation_runs", payload)
        for item in items:
            if time.monotonic() >= deadline:
                raise TimeoutError("evaluation persistence deadline")
            _insert(db, "evaluation_items", item | {"evaluation_id": identifier})
        check_cohort(db, items)
        return identifier


def check_cohort(db, items):
    """One collector configuration hash per evaluation cohort (specification §5).

    Contract-2 decisions pin the collector hash of their discovery pass; every pinned
    hash in one evaluation must be identical, and an evaluation never mixes
    contract-1 and contract-2 decisions. Unpinned contract-2 decisions (no applicable
    pass) carry no collector hash and are counted but do not define the cohort.
    """
    contracts_seen = set()
    collector_hashes = set()
    unpinned = 0
    for item in items:
        if item.get("decision_id") is None:
            continue
        row = row_dict(db, "forecast_decisions", {"id": item["decision_id"]})
        decision = json.loads(row["payload_json"])
        contract = contracts.version("forecast_decisions", decision)
        contracts_seen.add(contract)
        scored = any(v is not None for v in item["scores"].values())
        if decision["discovery_id"] is None:
            if scored:
                raise ValueError(
                    "scored item cites a decision without discovery provenance; "
                    "unpinned rows are unscored coverage only"
                )
            unpinned += 1
        elif contract == 2:
            collector_hashes.add(decision["manifest"]["discovery"]["collector_config_hash"])
    if len(contracts_seen) > 1:
        raise ValueError("evaluation mixes contract-1 and contract-2 decisions")
    if len(collector_hashes) > 1:
        raise ValueError(
            "evaluation cohort spans multiple collector configuration hashes; "
            "report each collector configuration as a separate cohort"
        )
    return {
        "record_contract": contracts_seen.pop() if contracts_seen else None,
        "collector_config_hash": collector_hashes.pop() if collector_hashes else None,
        "unpinned_decisions": unpinned,
    }


def integrity(db):
    """Read-only validation of canonical payloads, nested refs, projections and items.

    Call in a caller-owned read snapshot; streamed rows keep memory bounded. This
    checks storage, not temporal selection, actual publication, or scoring accuracy.
    """
    check_schema(db)
    schema_version = db.execute("PRAGMA user_version").fetchone()[0]
    counts = {table: {"1": 0, "2": 0} for table in COLUMNS}
    for table in COLUMNS:
        cursor = db.execute(f"SELECT * FROM {table}")
        names = [d[0] for d in cursor.description]
        for values in cursor:
            row = dict(zip(names, values))
            p = json.loads(row["payload_json"])
            identity = row.get("id", row.get("opportunity_key"))
            try:
                contract = contracts.version(table, p)
            except ValueError as exc:
                raise ValueError(f"{table} row {identity}: {exc}") from exc
            if contract == 2 and schema_version < 4:
                raise ValueError(
                    f"{table} row {identity}: contract-2 record in a schema-{schema_version} "
                    "archive; not migrated automatically"
                )
            counts[table][str(contract)] += 1
            if contracts.validate(table, p) != row["payload_json"] or digest(p) != row["digest"]:
                raise ValueError("forecast payload hash/canonical mismatch")
            validate_links(db, table, p)
            if any(row[k] != p[k] for k in PROJECTIONS[table]):
                raise ValueError("forecast projection mismatch")
            if table != "evaluation_items" and row["idempotency_key"] != contracts.key(table, p):
                raise ValueError("forecast idempotency digest mismatch")
            if table == "evaluation_runs":
                items = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT payload_json FROM evaluation_items WHERE evaluation_id=? ORDER BY opportunity_key LIMIT 1001",
                        (row["id"],),
                    )
                ]
                if len(items) > 1000:
                    raise ValueError("evaluation item limit")
                by_key = {item["opportunity_key"]: item for item in items}
                ordered = []
                for entry in p["population_manifest"]:
                    item = by_key.pop(entry["opportunity_key"], None)
                    if item is None or any(
                        item[k] != entry[k] for k in ("decision_id", "game_id", "horizon")
                    ):
                        raise ValueError("evaluation missing population item")
                    ordered.append({k: v for k, v in item.items() if k != "evaluation_id"})
                if by_key or digest(ordered) != p["report"]["items_digest"]:
                    raise ValueError("evaluation item manifest mismatch")
                check_cohort(db, items)
    latest = db.execute(
        "SELECT id, payload_json FROM discovery_passes ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "integrity": "verified",
        "materialization": "not_implemented",
        "publication_verified": False,
        "record_contracts": counts,
        "latest_discovery": None
        if latest is None
        else {
            "id": latest[0],
            "complete": json.loads(latest[1])["complete"],
            "reasons": json.loads(latest[1])["reasons"],
        },
    }
