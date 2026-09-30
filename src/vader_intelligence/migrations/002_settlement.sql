-- Additive evidence journal. No collector table is altered.
CREATE TABLE settlement_versions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('mapping','mlb','kalshi')),
    entity_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    previous_id INTEGER REFERENCES settlement_versions(id),
    digest TEXT NOT NULL,
    data_json TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    first_fetch_id TEXT NOT NULL REFERENCES fetches(id),
    UNIQUE(kind,entity_key,revision)
);
CREATE TABLE settlement_observations (
    kind TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    fetch_id TEXT NOT NULL REFERENCES fetches(id),
    version_id INTEGER NOT NULL REFERENCES settlement_versions(id),
    parser_version INTEGER NOT NULL,
    PRIMARY KEY(kind,entity_key,fetch_id,parser_version)
);
CREATE TABLE settlement_targets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ticker TEXT NOT NULL,
    terms_fetch_id TEXT NOT NULL REFERENCES fetches(id),
    market_fetch_id TEXT REFERENCES fetches(id),
    event_fetch_id TEXT REFERENCES fetches(id),
    schedule_fetch_id TEXT REFERENCES fetches(id),
    mapping_version_id INTEGER REFERENCES settlement_versions(id),
    state TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(run_id,ticker)
);
CREATE INDEX settlement_observations_version ON settlement_observations(version_id);
CREATE INDEX settlement_targets_ticker ON settlement_targets(ticker);
