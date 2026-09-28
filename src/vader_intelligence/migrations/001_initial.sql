CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, provenance_json TEXT NOT NULL, summary_json TEXT
);
CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, byte_count INTEGER NOT NULL);
CREATE TABLE requests (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL,
 request_key TEXT NOT NULL, url TEXT NOT NULL, params_json TEXT NOT NULL,
 done INTEGER NOT NULL DEFAULT 0, checkpoint_json TEXT,
 UNIQUE(run_id, stage, request_key)
);
CREATE TABLE fetches (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 request_id TEXT NOT NULL REFERENCES requests(id), attempt INTEGER NOT NULL,
 started_at TEXT NOT NULL, retrieved_at TEXT, elapsed_seconds REAL,
 status INTEGER, headers_json TEXT, body_sha256 TEXT REFERENCES blobs(sha256),
 state TEXT NOT NULL, error TEXT, truncated INTEGER NOT NULL DEFAULT 0,
 envelope_version INTEGER NOT NULL DEFAULT 1, source_api_version TEXT,
 UNIQUE(request_id, attempt)
);
CREATE INDEX fetches_state ON fetches(state, seq);
CREATE TABLE entities (
 id INTEGER PRIMARY KEY, fetch_id TEXT NOT NULL REFERENCES fetches(id),
 kind TEXT NOT NULL, ticker TEXT NOT NULL, parent_ticker TEXT,
 parser_version INTEGER NOT NULL, data_json TEXT NOT NULL,
 UNIQUE(fetch_id, kind, ticker, parser_version)
);
CREATE INDEX entity_ticker ON entities(kind, ticker, id);
CREATE TABLE observations (
 id INTEGER PRIMARY KEY, fetch_id TEXT NOT NULL REFERENCES fetches(id),
 ticker TEXT NOT NULL, kind TEXT NOT NULL, parser_version INTEGER NOT NULL,
 data_json TEXT NOT NULL, UNIQUE(fetch_id, kind, ticker, parser_version)
);
CREATE INDEX observation_ticker ON observations(ticker, kind, id);
CREATE TABLE eligibility (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), ticker TEXT NOT NULL,
 checked_at TEXT NOT NULL, schedule_fetch_id TEXT REFERENCES fetches(id),
 market_fetch_id TEXT REFERENCES fetches(id), game_id INTEGER,
 eligible INTEGER NOT NULL, reason TEXT NOT NULL, cutoff TEXT,
 data_json TEXT NOT NULL
);
CREATE INDEX eligibility_run ON eligibility(run_id, id);
