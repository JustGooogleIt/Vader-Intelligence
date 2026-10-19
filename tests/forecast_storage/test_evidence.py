"""Read-side availability projection (F3.3 plan §4.6): C1–C3, D1, F3, F4, J1, L2, N1, N2.

Rows are written directly through the journal so every timestamp is controlled. All
evidence is synthetic; the projection is read with mode="synthetic" accordingly.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from vader_intelligence.forecast import evidence, journal, schema
from vader_intelligence.forecast.policy import cutoff_eligibility, freshness
from vader_intelligence.forecast.references import digest, reference
from vader_intelligence.provenance import RunProvenanceV1, json_text

from .fixtures import HASH, connect, legacy

T0 = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
US = timedelta(microseconds=1)
NS = "local-witness-v1"


def iso(moment):
    return moment.isoformat()


class Archive:
    """Schema-5 synthetic archive with helpers that write two-phase runs, passes and receipts."""

    def __init__(self, path):
        self.db = legacy(path)
        schema.migrate(self.db)
        schema.migrate(self.db, record_contract=3)
        self.seq = 0
        self.witness_run("witness-run")

    def run(self, run_id, kind="collect", protocol="two-phase-v1", configuration=None):
        configuration = {"max_pages": 3} if configuration is None else configuration
        tool = {"collection_protocol": protocol} if protocol else {}
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                iso(T0),
                None,
                "running",
                RunProvenanceV1(
                    run_id, kind, HASH, configuration, tool_configuration=tool
                ).to_json(),
                None,
            ),
        )
        return digest(configuration)

    def witness_run(self, run_id, namespace=NS):
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                "forecast-witness",
                iso(T0),
                None,
                "running",
                RunProvenanceV1(
                    run_id,
                    "forecast-witness",
                    HASH,
                    {"ns": NS},
                    tool_configuration={
                        "observer_namespace": namespace,
                        "session_id": "witness-session",
                        "source_lineage": "synthetic",
                    },
                ).to_json(),
                None,
            ),
        )

    def fetch(self, run_id, stage, retrieved_at, state="ok"):
        self.seq += 1
        fetch_id = f"{run_id}-{stage}-{self.seq}"
        body = json_text({"synthetic": fetch_id}).encode()
        sha = __import__("hashlib").sha256(body).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (sha, body, len(body)))
        self.db.execute(
            "INSERT INTO requests(id,run_id,stage,request_key,url,params_json,done)"
            " VALUES (?,?,?,?,?,?,1)",
            (fetch_id + "-req", run_id, stage, fetch_id, "https://example.invalid/x", "{}"),
        )
        self.db.execute(
            "INSERT INTO fetches(id,request_id,attempt,started_at,retrieved_at,body_sha256,state,status)"
            " VALUES (?,?,1,?,?,?,?,200)",
            (
                fetch_id,
                fetch_id + "-req",
                iso(retrieved_at - timedelta(seconds=1)),
                iso(retrieved_at),
                sha,
                state,
            ),
        )
        return fetch_id, self.db.execute(
            "SELECT seq FROM fetches WHERE id=?", (fetch_id,)
        ).fetchone()[0]

    def pass_(
        self,
        run_id,
        completed_at,
        *,
        complete=True,
        reasons=(),
        through=None,
        config_hash=None,
        manifest=None,
        mode="synthetic",
        session=None,
    ):
        if config_hash is None:
            config_hash = digest({"max_pages": 3})
        if manifest is None:
            manifest = {
                "manifest_version": 1,
                "phase_reached": "done" if complete else "schedule",
                "candidates_known": True,
                "eligibility_complete": complete,
                "pages_complete": True,
                "fetch_seq_through": through if through is not None else self.seq,
            }
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": iso(completed_at),
            "code_hash": HASH,
            "config_hash": config_hash,
            "mode": mode,
            "references": [],
            "session_id": session or f"session-{self.seq}-{iso(completed_at)}",
            "phase_sequence": 1,
            "completed_at": iso(completed_at),
            "complete": complete,
            "reasons": list(reasons),
            "counts": {"games": 1, "contracts": 2},
            "eligibility_after_id": 0,
            "eligibility_through_id": 0,
            "manifest": manifest,
        }
        return journal.append(self.db, "discovery_passes", payload)

    def receipt(
        self,
        *,
        pass_id=None,
        fetch_id=None,
        observed_at,
        trusted=True,
        contract=3,
        namespace=NS,
        mode="synthetic",
    ):
        if pass_id is not None:
            row = journal.row_dict(self.db, "discovery_passes", {"id": pass_id})
            ref = reference(self.db, "discovery_passes", {"id": pass_id})
            subject = (
                "discovery_pass",
                None,
                pass_id,
                row["digest"],
            )
        else:
            ref = reference(self.db, "fetches", {"id": fetch_id})
            body = self.db.execute(
                "SELECT body_sha256 FROM fetches WHERE id=?", (fetch_id,)
            ).fetchone()[0]
            subject = ("fetch", fetch_id, None, ref["digest"] if contract >= 2 else body)
        run_id = "witness-run" if namespace == NS else f"witness-run-{namespace}"
        if self.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            self.witness_run(run_id, namespace)  # one witness run per observer namespace
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": iso(observed_at),
            "code_hash": HASH,
            "config_hash": digest({"ns": NS}),
            "mode": mode,
            "references": [ref],
            "subject_kind": subject[0],
            "fetch_id": subject[1],
            "binding_id": None,
            "publication_id": None,
            "discovery_id": subject[2],
            "subject_digest": subject[3],
            "observed_at": iso(observed_at),
            "session_id": "witness-session",
            "observer_namespace": namespace,
            "monotonic_offset": "1.5",
            "clock_status": "trusted" if trusted else "untrusted",
        }
        if contract >= 2:
            payload["contract_version"] = contract
        return journal.append(self.db, "forecast_receipts", payload)


@pytest.fixture
def archive(tmp_path):
    a = Archive(tmp_path / "evidence.sqlite3")
    try:
        yield a
    finally:
        a.db.close()


def as_of(archive, cutoff):
    return evidence.discovery_as_of(archive.db, cutoff, mode="synthetic")


# ---------------------------------------------------------------------- C1–C3


def test_receipt_after_cutoff_is_not_rescued_by_sequence_or_bytes(archive):
    archive.run("r1")
    fetch_id, _ = archive.fetch("r1", "book", T0 - timedelta(seconds=10))
    archive.receipt(fetch_id=fetch_id, observed_at=T0 + timedelta(milliseconds=1))
    availability = evidence.fetch_availability(archive.db, fetch_id, mode="synthetic")
    assert availability.status == "witnessed"
    assert freshness(availability.times, T0).reasons == ("availability_after_cutoff",)
    assert freshness(availability.times, T0 + timedelta(milliseconds=1)).eligible


def test_contract_one_receipt_contributes_nothing_whatever_time_it_claims(archive):
    archive.run("r1")
    fetch_id, _ = archive.fetch("r1", "book", T0 - timedelta(seconds=10))
    archive.db.execute("DELETE FROM schema_migrations WHERE version IN (4, 5)")
    archive.db.execute("PRAGMA user_version=3")
    archive.receipt(fetch_id=fetch_id, observed_at=T0 - timedelta(seconds=9), contract=1)
    archive.db.execute("INSERT INTO schema_migrations VALUES (4, ?), (5, ?)", (iso(T0), iso(T0)))
    archive.db.execute("PRAGMA user_version=5")
    availability = evidence.fetch_availability(archive.db, fetch_id, mode="synthetic")
    assert availability.status == "availability_unproven" and availability.receipt_id is None
    assert freshness(availability.times, T0).reasons == ("availability_unproven",)
    # N2: the witness's own contract-2 receipt at its real time is the one used.
    receipt_id = archive.receipt(fetch_id=fetch_id, observed_at=T0 + timedelta(seconds=5))
    availability = evidence.fetch_availability(archive.db, fetch_id, mode="synthetic")
    assert availability.status == "witnessed" and availability.receipt_id == receipt_id
    assert as_of(archive, T0).diagnostics["contract_1_receipts_ignored"] == 1
    assert journal.integrity(archive.db)["record_contracts"]["forecast_receipts"] == {
        "1": 1,
        "2": 0,
        "3": 1,
        "4": 0,
    }


def test_pass_first_witnessed_after_cutoff_is_not_applicable(archive):
    archive.run("r1")
    archive.fetch("r1", "status", T0 - timedelta(seconds=300))
    older = archive.pass_("r1", T0 - timedelta(seconds=290))
    archive.receipt(pass_id=older, observed_at=T0 - timedelta(seconds=200))
    archive.run("r2")
    archive.fetch("r2", "status", T0 - timedelta(seconds=100))
    newer = archive.pass_("r2", T0 - timedelta(seconds=90))
    archive.receipt(pass_id=newer, observed_at=T0 + US)
    at_cutoff = as_of(archive, T0)
    assert at_cutoff.kind == "pass" and at_cutoff.pass_id == older
    assert at_cutoff.universe_complete is True and at_cutoff.eligibility_reasons() == ()
    assert at_cutoff.diagnostics["passes_first_witnessed_after_cutoff"] == 1
    later = as_of(archive, T0 + US)
    assert later.pass_id == newer


# -------------------------------------------------------------------------- D1


def test_orphan_discovery_fetch_means_summary_missing_without_fallback(archive):
    archive.run("r1")
    archive.fetch("r1", "status", T0 - timedelta(seconds=200))
    complete = archive.pass_("r1", T0 - timedelta(seconds=190))
    archive.receipt(pass_id=complete, observed_at=T0 - timedelta(seconds=150))
    # A later two-phase run completed a status fetch and was killed before its summary.
    archive.run("r2")
    orphan, _ = archive.fetch("r2", "status", T0 - timedelta(seconds=100))
    archive.receipt(fetch_id=orphan, observed_at=T0 - timedelta(seconds=50))
    projection = as_of(archive, T0)
    assert projection.kind == "orphan" and projection.run_id == "r2"
    assert projection.universe_complete is False
    assert projection.reasons == ("discovery_summary_missing",)
    assert "universe_incomplete" in projection.eligibility_reasons()
    # Before the orphan was witnessed, the complete pass still applied.
    assert as_of(archive, T0 - timedelta(seconds=60)).pass_id == complete
    # A Phase B schedule renewal of the earlier run is not an orphan.
    renewal, _ = archive.fetch("r1", "schedule", T0 - timedelta(seconds=40))
    archive.receipt(fetch_id=renewal, observed_at=T0 - timedelta(seconds=30))
    assert as_of(archive, T0 - timedelta(seconds=60)).pass_id == complete
    assert as_of(archive, T0 - timedelta(seconds=25)).kind == "orphan"  # r2 still newest


def test_orphans_only_from_two_phase_runs(archive):
    archive.run("legacy", protocol=None)
    fetch_id, _ = archive.fetch("legacy", "status", T0 - timedelta(seconds=100))
    archive.receipt(fetch_id=fetch_id, observed_at=T0 - timedelta(seconds=50))
    assert as_of(archive, T0).reasons == ("discovery_missing",)


# ------------------------------------------------------------------- F3, F4


@pytest.mark.parametrize(
    "age,expected",
    [
        (timedelta(seconds=300), ()),
        (timedelta(seconds=300) + US, ("universe_incomplete", "discovery:input_stale")),
        (timedelta(seconds=349), ("universe_incomplete", "discovery:input_stale")),
    ],
)
def test_freshness_boundary_and_stale_pass_is_not_substituted(archive, age, expected):
    archive.run("old")
    archive.fetch("old", "status", T0 - timedelta(seconds=900))
    old = archive.pass_("old", T0 - timedelta(seconds=890))
    archive.receipt(pass_id=old, observed_at=T0 - timedelta(seconds=800))
    archive.run("r1")
    archive.fetch("r1", "status", T0 - age - timedelta(seconds=1))
    newest = archive.pass_("r1", T0 - age)
    archive.receipt(pass_id=newest, observed_at=T0 - age + timedelta(seconds=1))
    projection = as_of(archive, T0)
    assert projection.pass_id == newest  # never the older complete pass
    assert projection.universe_complete is True
    assert projection.eligibility_reasons() == expected
    common = cutoff_eligibility(
        cutoff=T0,
        binding_observed_at=T0 - timedelta(seconds=1),
        discovery=projection.times,
        schedule=projection.times,
        market=projection.times,
        universe_complete=projection.universe_complete,
        identity_proven=True,
        pregame=True,
        contract_valid=True,
        clock_trusted=True,
    )
    assert common.eligible is (expected == ())


def test_incomplete_latest_pass_fails_closed_without_falling_back(archive):
    archive.run("r1")
    archive.fetch("r1", "status", T0 - timedelta(seconds=200))
    complete = archive.pass_("r1", T0 - timedelta(seconds=190))
    archive.receipt(pass_id=complete, observed_at=T0 - timedelta(seconds=150))
    archive.run("r2")
    archive.fetch("r2", "status", T0 - timedelta(seconds=100))
    incomplete = archive.pass_(
        "r2", T0 - timedelta(seconds=90), complete=False, reasons=("event_unavailable",)
    )
    archive.receipt(pass_id=incomplete, observed_at=T0 - timedelta(seconds=50))
    projection = as_of(archive, T0)
    assert projection.pass_id == incomplete and projection.universe_complete is False
    assert projection.reasons == ("event_unavailable",)
    assert projection.eligibility_reasons()[0] == "universe_incomplete"


# ------------------------------------------------------------------- J1, L2


@pytest.mark.parametrize(
    "case,expected",
    [
        ("config", ("collector_config_mismatch",)),
        ("reasons", ("pass_inconsistent",)),
        ("flags", ("pass_inconsistent",)),
        ("manifest", ("manifest_unknown",)),
        ("legacy_run", ("run_not_two_phase",)),
        ("run_kind", ("run_kind_invalid",)),
    ],
)
def test_unverifiable_complete_pass_is_never_a_usable_universe(archive, case, expected):
    protocol = None if case == "legacy_run" else "two-phase-v1"
    kind = "forecast-storage" if case == "run_kind" else "collect"
    archive.run("r1", kind=kind, protocol=protocol)
    archive.fetch("r1", "status", T0 - timedelta(seconds=100))
    options = {}
    if case == "config":
        options["config_hash"] = "e" * 64
    if case == "reasons":
        options["reasons"] = ("interrupted",)
    if case == "flags":
        options["manifest"] = {
            "manifest_version": 1,
            "phase_reached": "events",
            "candidates_known": True,
            "eligibility_complete": False,
            "pages_complete": True,
            "fetch_seq_through": archive.seq,
        }
    if case == "manifest":
        options["manifest"] = {"synthetic": True}  # PR #7's fixture shape
    pass_id = archive.pass_("r1", T0 - timedelta(seconds=90), **options)
    archive.receipt(pass_id=pass_id, observed_at=T0 - timedelta(seconds=50))
    projection = as_of(archive, T0)
    assert projection.pass_id == pass_id and projection.universe_complete is False
    assert projection.reasons == expected
    assert "universe_incomplete" in projection.eligibility_reasons()


def test_untrusted_receipt_is_no_proof_and_is_counted(archive):
    archive.run("r1")
    archive.fetch("r1", "status", T0 - timedelta(seconds=100))
    pass_id = archive.pass_("r1", T0 - timedelta(seconds=90))
    archive.receipt(pass_id=pass_id, observed_at=T0 - timedelta(seconds=50), trusted=False)
    projection = as_of(archive, T0)
    assert projection.kind is None and projection.reasons == ("discovery_missing",)
    assert projection.diagnostics["untrusted_receipts_ignored"] == 1


def test_other_namespace_or_mode_receipts_are_separate(archive):
    archive.run("r1")
    archive.fetch("r1", "status", T0 - timedelta(seconds=100))
    pass_id = archive.pass_("r1", T0 - timedelta(seconds=90))
    archive.receipt(pass_id=pass_id, observed_at=T0 - timedelta(seconds=50), namespace="other-ns")
    assert as_of(archive, T0).kind is None
    assert evidence.discovery_as_of(
        archive.db, T0, namespace="other-ns", mode="synthetic"
    ).pass_id == (pass_id)
    assert (
        evidence.discovery_as_of(archive.db, T0, namespace="other-ns").kind is None
    )  # forward mode


def test_fetch_availability_statuses(archive):
    archive.run("r1")
    assert evidence.fetch_availability(archive.db, "nope").status == "missing"
    pending, _ = archive.fetch("r1", "book", T0, state="pending")
    archive.db.execute("UPDATE fetches SET retrieved_at=NULL WHERE id=?", (pending,))
    assert evidence.fetch_availability(archive.db, pending).status == "incomplete"
    ok, _ = archive.fetch("r1", "book", T0)
    assert evidence.fetch_availability(archive.db, ok, mode="synthetic").status == (
        "availability_unproven"
    )
    archive.receipt(fetch_id=ok, observed_at=T0 + timedelta(seconds=1))
    first = evidence.fetch_availability(archive.db, ok, mode="synthetic")
    assert first.status == "witnessed" and first.observed_at == T0 + timedelta(seconds=1)
    # Another namespace's later receipt never replaces the permanent first one here.
    archive.receipt(fetch_id=ok, observed_at=T0 + timedelta(seconds=9), namespace="other-ns")
    assert evidence.fetch_availability(archive.db, ok, mode="synthetic") == first


def test_projection_is_read_only(archive):
    archive.run("r1")
    archive.fetch("r1", "status", T0 - timedelta(seconds=100))
    pass_id = archive.pass_("r1", T0 - timedelta(seconds=90))
    archive.receipt(pass_id=pass_id, observed_at=T0 - timedelta(seconds=50))
    before = [
        tuple(r)
        for t in ("discovery_passes", "forecast_receipts", "fetches")
        for r in archive.db.execute(f"SELECT * FROM {t}")
    ]
    as_of(archive, T0)
    evidence.fetch_availability(archive.db, "r1-status-1", mode="synthetic")
    after = [
        tuple(r)
        for t in ("discovery_passes", "forecast_receipts", "fetches")
        for r in archive.db.execute(f"SELECT * FROM {t}")
    ]
    assert before == after
    assert json.loads(
        archive.db.execute("SELECT payload_json FROM discovery_passes").fetchone()[0]
    )["complete"]
    connect  # noqa: B018  (fixture import kept for parity with sibling modules)
