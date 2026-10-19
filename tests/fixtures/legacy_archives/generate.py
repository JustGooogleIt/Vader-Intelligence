"""Generate synthetic legacy archives with OLDER implementations (roadmap A1, D-001).

Run this file with an older checkout's own source and test helpers first on the path;
it never imports this checkout's application code. Every archive is synthetic and
disposable: the shipped synthetic fixture, injected transports and clocks, plus the
adversarial records the PR #10 independent review reproduced. Some rows are labelled
`forward_shadow` although their inputs are synthetic; that is the point: they record
exactly what the older code accepted, so the current code can be tested against it.

Usage (one era per invocation; OLD is the older checkout):

    PYTHONPATH=OLD/src:OLD/tests:OLD/tests/forecast \
        .venv/bin/python tests/fixtures/legacy_archives/generate.py --era draft --out X.sql

Eras: `main` (ebb58df: schema 3, contract 1), `draft` (5811991, PR #11 head: schema 4,
contract 2 as written before the PR #12 amendment), `amended` (82665cd, PR #16 head:
schema 4, contract 2 as amended). The output is an SQL text dump plus the archive's
`user_version`, loadable with `load(path)`.
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def dump(db_path, out):
    """SQL text dump that also records PRAGMA user_version (iterdump omits it)."""
    db = sqlite3.connect(db_path)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    lines = list(db.iterdump())
    db.close()
    Path(out).write_text("\n".join(lines) + f"\nPRAGMA user_version={version};\n")
    return version


def load(sql_path, db_path):
    """Recreate an archive file from a dump (used by the tests)."""
    db = sqlite3.connect(db_path)
    db.executescript(Path(sql_path).read_text())
    db.close()


def cli(path, *args):
    from vader_intelligence.cli import execute, parser

    return execute(parser().parse_args(["--db", str(path), *args]))


def era_main(path):
    """main ebb58df: the F3.2 storage fixture (schema 3, contract-1 envelopes)."""
    from forecast_storage.fixtures import populated

    db, _ = populated(path)
    db.close()


def era_draft(path):
    """PR #11 head 5811991: pipeline plus the PR #10 review's adversarial records."""
    from _f34 import at, collect, protocol, tick_at
    from forecast_storage.fixtures import HASH

    from vader_intelligence.config import Config
    from vader_intelligence.forecast import journal
    from vader_intelligence.forecast.references import digest, reference
    from vader_intelligence.forecast.witness import witness
    from vader_intelligence.provenance import RunProvenanceV1
    from vader_intelligence.storage import Store, writer_lock

    for args in (
        ("db-init",),
        ("settlement", "migrate"),
        ("forecast", "migrate"),
        ("forecast", "migrate", "--record-contract", "2"),
    ):
        cli(path, *args)
    proto = protocol(dataset="legacy-draft")
    config = Config(database=str(path), min_free_bytes=1)
    with writer_lock(path):
        store = Store(path, min_free_bytes=1)
        # Pipeline under injected clocks (synthetic): collection, warm and due ticks.
        collect(store, config, origin=at(18, 3))
        tick_at(store, proto, at(18, 3, 30))
        tick_at(store, proto, at(18, 5, 30))
        # Review P1: a default-clock witness promotes the injected collection's fetches.
        witness(store)
        # Review P1 (second finding): contract-2 receipts supplied by an ordinary caller.
        db = store.db
        run_id = "legacy-supplied-receipts"
        store.start_run(RunProvenanceV1(run_id, "forecast-storage", HASH, {"ns": "supplied"}))
        fetch_id = db.execute(
            "SELECT f.id FROM fetches f JOIN requests r ON r.id=f.request_id"
            " WHERE r.stage='status' ORDER BY f.seq LIMIT 1"
        ).fetchone()[0]
        ref = reference(db, "fetches", {"id": fetch_id})
        observed = db.execute("SELECT retrieved_at FROM fetches WHERE id=?", (fetch_id,)).fetchone()
        payload = dict(
            schema_version=1,
            contract_version=2,
            run_id=run_id,
            created_at=observed[0],
            code_hash=json.loads(
                db.execute("SELECT provenance_json FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            )["code_hash"],
            config_hash=digest({"ns": "supplied"}),
            mode="forward_shadow",
            references=[ref],
            subject_kind="fetch",
            fetch_id=fetch_id,
            binding_id=None,
            publication_id=None,
            discovery_id=None,
            subject_digest=ref["digest"],
            observed_at=observed[0],
            session_id="supplied-session",
            observer_namespace="supplied-namespace",
            monotonic_offset="0",
            clock_status="trusted",
        )
        journal.append(db, "forecast_receipts", payload)
        store.close()


def era_amended(path):
    """PR #16 head 82665cd: the deterministic research demo archive (all synthetic)."""
    demo = None
    for entry in sys.path:
        candidate = Path(entry).parent / "tests/integration/research_demo.py"
        if candidate.exists():
            demo = candidate
            break
    if demo is None:
        raise SystemExit("research_demo.py of the older checkout not found on PYTHONPATH")
    keep = Path(tempfile.mkdtemp(prefix="legacy-amended-")) / "demo"
    result = subprocess.run(
        [sys.executable, str(demo), "--keep", str(keep)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise SystemExit("research demo failed:\n" + result.stdout[-2000:] + result.stderr[-2000:])
    archives = sorted(keep.glob("*.sqlite3"))
    if len(archives) != 1:
        raise SystemExit(f"expected one demo archive, found {archives}")
    source = sqlite3.connect(archives[0])
    target = sqlite3.connect(path)
    source.backup(target)
    source.close()
    target.close()


ERAS = {"main": era_main, "draft": era_draft, "amended": era_amended}


def main():
    options = argparse.ArgumentParser()
    options.add_argument("--era", required=True, choices=sorted(ERAS))
    options.add_argument("--out", required=True)
    args = options.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{args.era}.sqlite3"
        ERAS[args.era](path)
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=DELETE")
        db.close()
        version = dump(path, args.out)
    print(json.dumps({"era": args.era, "out": args.out, "user_version": version}))


if __name__ == "__main__":
    main()
