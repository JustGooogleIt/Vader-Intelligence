-- Additive F3.2 foundation only. No historical backfill or visibility witnessing.
-- payload_json is the complete versioned canonical record; projected keys are checked.

CREATE TABLE discovery_passes (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
    run_id TEXT NOT NULL REFERENCES runs(id),
    session_id TEXT NOT NULL,
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(run_id IS json_extract(payload_json,'$.run_id')),
    CHECK(session_id IS json_extract(payload_json,'$.session_id')),
    UNIQUE(run_id,session_id)
);

CREATE TRIGGER discovery_passes_no_update BEFORE UPDATE ON discovery_passes BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER discovery_passes_no_delete BEFORE DELETE ON discovery_passes BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TABLE forecast_bindings (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
    run_id TEXT NOT NULL REFERENCES runs(id),
    candidate_key TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('synthetic','forward_shadow','historical_reconstruction')),
    revision INTEGER NOT NULL CHECK(revision>=1),
    previous_id INTEGER REFERENCES forecast_bindings(id),
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(run_id IS json_extract(payload_json,'$.run_id')),
    CHECK(candidate_key IS json_extract(payload_json,'$.candidate_key')),
    CHECK(policy_version IS json_extract(payload_json,'$.policy_version')),
    CHECK(config_hash IS json_extract(payload_json,'$.config_hash')),
    CHECK(mode IS json_extract(payload_json,'$.mode')),
    CHECK(revision IS json_extract(payload_json,'$.revision')),
    CHECK(previous_id IS json_extract(payload_json,'$.previous_id')),
    UNIQUE(candidate_key,policy_version,config_hash,mode,revision)
);

CREATE TRIGGER forecast_bindings_no_update BEFORE UPDATE ON forecast_bindings BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER forecast_bindings_no_delete BEFORE DELETE ON forecast_bindings BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TABLE forecast_decisions (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
    run_id TEXT NOT NULL REFERENCES runs(id),
    mode TEXT NOT NULL CHECK(mode IN ('synthetic','forward_shadow','historical_reconstruction')),
    protocol TEXT NOT NULL,
    cutoff TEXT,
    binding_id INTEGER REFERENCES forecast_bindings(id),
    discovery_id INTEGER REFERENCES discovery_passes(id),
    supersedes_id INTEGER REFERENCES forecast_decisions(id),
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(run_id IS json_extract(payload_json,'$.run_id')),
    CHECK(mode IS json_extract(payload_json,'$.mode')),
    CHECK(protocol IS json_extract(payload_json,'$.protocol')),
    CHECK(cutoff IS json_extract(payload_json,'$.cutoff')),
    CHECK(binding_id IS json_extract(payload_json,'$.binding_id')),
    CHECK(discovery_id IS json_extract(payload_json,'$.discovery_id')),
    CHECK(supersedes_id IS json_extract(payload_json,'$.supersedes_id'))
);

CREATE TRIGGER forecast_decisions_no_update BEFORE UPDATE ON forecast_decisions BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER forecast_decisions_no_delete BEFORE DELETE ON forecast_decisions BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TABLE forecast_publications (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
    run_id TEXT NOT NULL REFERENCES runs(id),
    decision_id INTEGER NOT NULL UNIQUE REFERENCES forecast_decisions(id),
    verdict TEXT NOT NULL CHECK(verdict IN ('allowed','vetoed','missed','failed')),
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(run_id IS json_extract(payload_json,'$.run_id')),
    CHECK(decision_id IS json_extract(payload_json,'$.decision_id')),
    CHECK(verdict IS json_extract(payload_json,'$.verdict'))
);

CREATE TRIGGER forecast_publications_no_update BEFORE UPDATE ON forecast_publications BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER forecast_publications_no_delete BEFORE DELETE ON forecast_publications BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TABLE forecast_receipts (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
    run_id TEXT NOT NULL REFERENCES runs(id),
    subject_kind TEXT NOT NULL CHECK(subject_kind IN ('fetch','binding','publication','discovery_pass')),
    fetch_id TEXT REFERENCES fetches(id),
    binding_id INTEGER REFERENCES forecast_bindings(id),
    publication_id INTEGER REFERENCES forecast_publications(id),
    discovery_id INTEGER REFERENCES discovery_passes(id),
    observed_at TEXT NOT NULL,
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(run_id IS json_extract(payload_json,'$.run_id')),
    CHECK(subject_kind IS json_extract(payload_json,'$.subject_kind')),
    CHECK(fetch_id IS json_extract(payload_json,'$.fetch_id')),
    CHECK(binding_id IS json_extract(payload_json,'$.binding_id')),
    CHECK(publication_id IS json_extract(payload_json,'$.publication_id')),
    CHECK(discovery_id IS json_extract(payload_json,'$.discovery_id')),
    CHECK(observed_at IS json_extract(payload_json,'$.observed_at')),
    CHECK((subject_kind='fetch' AND fetch_id IS NOT NULL AND binding_id IS NULL AND publication_id IS NULL AND discovery_id IS NULL) OR (subject_kind='binding' AND binding_id IS NOT NULL AND fetch_id IS NULL AND publication_id IS NULL AND discovery_id IS NULL) OR (subject_kind='publication' AND publication_id IS NOT NULL AND fetch_id IS NULL AND binding_id IS NULL AND discovery_id IS NULL) OR (subject_kind='discovery_pass' AND discovery_id IS NOT NULL AND fetch_id IS NULL AND binding_id IS NULL AND publication_id IS NULL))
);

CREATE TRIGGER forecast_receipts_no_update BEFORE UPDATE ON forecast_receipts BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER forecast_receipts_no_delete BEFORE DELETE ON forecast_receipts BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TABLE evaluation_runs (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
    run_id TEXT NOT NULL REFERENCES runs(id),
    mode TEXT NOT NULL CHECK(mode IN ('synthetic','forward_shadow','historical_reconstruction')),
    view TEXT NOT NULL CHECK(view IN ('cutoff-reconstruction','forward-shadow')),
    supersedes_id INTEGER REFERENCES evaluation_runs(id),
    created_at TEXT NOT NULL,
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(run_id IS json_extract(payload_json,'$.run_id')),
    CHECK(mode IS json_extract(payload_json,'$.mode')),
    CHECK(view IS json_extract(payload_json,'$.view')),
    CHECK(supersedes_id IS json_extract(payload_json,'$.supersedes_id')),
    CHECK(created_at IS json_extract(payload_json,'$.created_at'))
);

CREATE TRIGGER evaluation_runs_no_update BEFORE UPDATE ON evaluation_runs BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER evaluation_runs_no_delete BEFORE DELETE ON evaluation_runs BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TABLE evaluation_items (
    evaluation_id INTEGER NOT NULL REFERENCES evaluation_runs(id),
    opportunity_key TEXT NOT NULL,
    game_id INTEGER CHECK(game_id>0),
    horizon INTEGER NOT NULL CHECK(horizon=3600),
    decision_id INTEGER REFERENCES forecast_decisions(id),
    publication_id INTEGER REFERENCES forecast_publications(id),
    receipt_id INTEGER REFERENCES forecast_receipts(id),
    mapping_id INTEGER REFERENCES settlement_versions(id),
    mlb_id INTEGER REFERENCES settlement_versions(id),
    kalshi_id INTEGER REFERENCES settlement_versions(id),
    digest TEXT NOT NULL CHECK(length(digest)=64),
    payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))<=10485760 AND json_valid(payload_json) AND json_type(payload_json)='object' AND json_extract(payload_json,'$.schema_version') IS 1),
    CHECK(evaluation_id IS json_extract(payload_json,'$.evaluation_id')),
    CHECK(opportunity_key IS json_extract(payload_json,'$.opportunity_key')),
    CHECK(game_id IS json_extract(payload_json,'$.game_id')),
    CHECK(horizon IS json_extract(payload_json,'$.horizon')),
    CHECK(decision_id IS json_extract(payload_json,'$.decision_id')),
    CHECK(publication_id IS json_extract(payload_json,'$.publication_id')),
    CHECK(receipt_id IS json_extract(payload_json,'$.receipt_id')),
    CHECK(mapping_id IS json_extract(payload_json,'$.mapping_id')),
    CHECK(mlb_id IS json_extract(payload_json,'$.mlb_id')),
    CHECK(kalshi_id IS json_extract(payload_json,'$.kalshi_id')),
    PRIMARY KEY(evaluation_id,opportunity_key),
    UNIQUE(evaluation_id,game_id,horizon)
) WITHOUT ROWID;

CREATE TRIGGER evaluation_items_no_update BEFORE UPDATE ON evaluation_items BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE TRIGGER evaluation_items_no_delete BEFORE DELETE ON evaluation_items BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;

CREATE INDEX forecast_decisions_cohort ON forecast_decisions(mode,protocol,cutoff,id);

CREATE INDEX evaluation_runs_created ON evaluation_runs(created_at,id);

CREATE INDEX forecast_receipts_fetch_id ON forecast_receipts(fetch_id,observed_at);

CREATE INDEX forecast_receipts_binding_id ON forecast_receipts(binding_id,observed_at);

CREATE INDEX forecast_receipts_publication_id ON forecast_receipts(publication_id,observed_at);

CREATE INDEX forecast_receipts_discovery_id ON forecast_receipts(discovery_id,observed_at);

CREATE TRIGGER discovery_passes_no_replace BEFORE INSERT ON discovery_passes
WHEN EXISTS(SELECT 1 FROM discovery_passes WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key OR (run_id=NEW.run_id AND session_id=NEW.session_id))
BEGIN SELECT RAISE(ABORT, 'immutable forecast key'); END;

CREATE TRIGGER forecast_bindings_no_replace BEFORE INSERT ON forecast_bindings
WHEN EXISTS(SELECT 1 FROM forecast_bindings WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key OR (candidate_key=NEW.candidate_key AND policy_version=NEW.policy_version AND config_hash=NEW.config_hash AND mode=NEW.mode AND revision=NEW.revision))
BEGIN SELECT RAISE(ABORT, 'immutable forecast key'); END;

CREATE TRIGGER forecast_decisions_no_replace BEFORE INSERT ON forecast_decisions
WHEN EXISTS(SELECT 1 FROM forecast_decisions WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT, 'immutable forecast key'); END;

CREATE TRIGGER forecast_publications_no_replace BEFORE INSERT ON forecast_publications
WHEN EXISTS(SELECT 1 FROM forecast_publications WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key OR decision_id=NEW.decision_id)
BEGIN SELECT RAISE(ABORT, 'immutable forecast key'); END;

CREATE TRIGGER forecast_receipts_no_replace BEFORE INSERT ON forecast_receipts
WHEN EXISTS(SELECT 1 FROM forecast_receipts WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT, 'immutable forecast key'); END;

CREATE TRIGGER evaluation_runs_no_replace BEFORE INSERT ON evaluation_runs
WHEN EXISTS(SELECT 1 FROM evaluation_runs WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT, 'immutable forecast key'); END;

CREATE TRIGGER evaluation_items_no_replace BEFORE INSERT ON evaluation_items
WHEN EXISTS(SELECT 1 FROM evaluation_items WHERE evaluation_id=NEW.evaluation_id AND (opportunity_key=NEW.opportunity_key OR (game_id=NEW.game_id AND horizon=NEW.horizon)))
BEGIN SELECT RAISE(ABORT, 'immutable evaluation item'); END;
