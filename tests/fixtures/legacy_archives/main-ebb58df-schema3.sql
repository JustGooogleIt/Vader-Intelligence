BEGIN TRANSACTION;
CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, body BLOB NOT NULL, byte_count INTEGER NOT NULL);
INSERT INTO "blobs" VALUES('76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4',X'7B2273796E746865746963223A747275652C2276616C7565223A2241227D',30);
INSERT INTO "blobs" VALUES('bb3520659e6a7e9e3c29a8bccd5daf8415aefef04fad3cd6ef8df6293f4c0b4f',X'7B2273796E746865746963223A747275652C2276616C7565223A2242227D',30);
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
INSERT INTO "discovery_passes" VALUES(1,'c4764a4adc747251a2e02e4a66d2147436edc45b2ee627f55a45424132ee06cc','synthetic-run','synthetic-pass','2197479e85ea0a5f17c5dcaffdbd1ecf02902399f05f09760fac15fc905be19d','{"code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","complete":true,"completed_at":"2026-09-30T19:00:00+00:00","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","counts":{"contracts":2,"games":1},"created_at":"2026-09-30T19:00:00+00:00","eligibility_after_id":0,"eligibility_through_id":1,"manifest":{"synthetic":true},"mode":"synthetic","phase_sequence":1,"reasons":[],"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"}],"run_id":"synthetic-run","schema_version":1,"session_id":"synthetic-pass"}');
CREATE TABLE eligibility (
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), ticker TEXT NOT NULL,
 checked_at TEXT NOT NULL, schedule_fetch_id TEXT REFERENCES fetches(id),
 market_fetch_id TEXT REFERENCES fetches(id), game_id INTEGER,
 eligible INTEGER NOT NULL, reason TEXT NOT NULL, cutoff TEXT,
 data_json TEXT NOT NULL
);
INSERT INTO "eligibility" VALUES(1,'synthetic-run','SYNTHETIC','2026-09-30T19:00:00+00:00','fetch-1','fetch-2',900001,1,'synthetic','2026-09-30T19:00:00+00:00','{}');
CREATE TABLE entities (
 id INTEGER PRIMARY KEY, fetch_id TEXT NOT NULL REFERENCES fetches(id),
 kind TEXT NOT NULL, ticker TEXT NOT NULL, parent_ticker TEXT,
 parser_version INTEGER NOT NULL, data_json TEXT NOT NULL,
 UNIQUE(fetch_id, kind, ticker, parser_version)
);
INSERT INTO "entities" VALUES(1,'fetch-1','synthetic','SYNTHETIC',NULL,1,'{"value":"A"}');
INSERT INTO "entities" VALUES(2,'fetch-2','synthetic','SYNTHETIC',NULL,1,'{"value":"B"}');
INSERT INTO "entities" VALUES(3,'fetch-3','synthetic','SYNTHETIC',NULL,1,'{"value":"A"}');
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
INSERT INTO "evaluation_items" VALUES(1,'synthetic-game',900001,3600,1,1,1,NULL,NULL,3,'76bc78af80b1f5036daae14d95a0b0ce538344f19a3f0bbe83da9304599ddd61','{"clipped":{"constant-v1":false,"midpoint-v1":false},"code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_at":"2026-09-30T19:00:00+00:00","cutoff_reasons":[],"cutoff_status":"eligible","decision_id":1,"evaluation_id":1,"game_id":900001,"horizon":3600,"kalshi_id":3,"mapping_id":null,"mlb_id":null,"mode":"synthetic","opportunity_key":"synthetic-game","outcome_reasons":["synthetic_not_scorable"],"outcome_status":"pending","paired_delta":null,"payout":null,"publication_id":1,"publication_reasons":["not_verified"],"publication_status":"publication_unconfirmed","receipt_id":1,"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"},{"digest":"8896041d6d25dc0dedbf561994c2cfd65f06e4699b593bd48fe012647759d609","fetches":[{"id":"fetch-3","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":3,"state":"ok"}],"key":{"id":3},"kind":"kalshi","parser_version":null,"source_mode":"synthetic","table":"settlement_versions"}],"run_id":"synthetic-run","schema_version":1,"scores":{"constant-v1":null,"midpoint-v1":null},"y":null}');
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
INSERT INTO "evaluation_runs" VALUES(1,'fdbef7410052218932507254f5bcc98a24ac2dbe7c386de878053242618c4a29','synthetic-run','synthetic','cutoff-reconstruction',NULL,'2026-09-30T19:00:00+00:00','0646bccb4b23c39a9dcb151028c67ff30f1498c133a05242b0a54980f4580204','{"code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","correction_reason":null,"created_at":"2026-09-30T19:00:00+00:00","dataset":"synthetic-study","evaluator_version":"v1","mode":"synthetic","outcome_digest":"4b692f4f66321af54ee8975c8981891c579cef6ef746c5da7076e5dfb1f5f47e","outcome_manifest":[{"digest":"8896041d6d25dc0dedbf561994c2cfd65f06e4699b593bd48fe012647759d609","fetches":[{"id":"fetch-3","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":3,"state":"ok"}],"key":{"id":3},"kind":"kalshi","parser_version":null,"source_mode":"synthetic","table":"settlement_versions"}],"outcomes_as_of":"2026-09-30T19:00:00+00:00","population_manifest":[{"decision_id":1,"game_id":900001,"horizon":3600,"opportunity_key":"synthetic-game"}],"protocol":"mlb-t60-v1","publication_digest":"9db9e39f65392d813d93fd27eabe314316aff5b1ad0b11b7317c73a4c97ba78f","publication_manifest":[{"digest":"848f68066d53139e94e7366b4ae4ab3054580e6f7c3219b933599b06212e438e","fetches":[],"key":{"id":1},"kind":null,"parser_version":null,"source_mode":"synthetic","table":"forecast_publications"}],"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"}],"report":{"items_digest":"eda884801dc827ee746e72332855a2eb27fb6c7f0f1585fefdb2ea22bd98f79b","synthetic":true},"run_id":"synthetic-run","runtime_version":"decimal40","schema_version":1,"source_ceiling":3,"status":"inconclusive","supersedes_id":null,"view":"cutoff-reconstruction"}');
CREATE TABLE fetches (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 request_id TEXT NOT NULL REFERENCES requests(id), attempt INTEGER NOT NULL,
 started_at TEXT NOT NULL, retrieved_at TEXT, elapsed_seconds REAL,
 status INTEGER, headers_json TEXT, body_sha256 TEXT REFERENCES blobs(sha256),
 state TEXT NOT NULL, error TEXT, truncated INTEGER NOT NULL DEFAULT 0,
 envelope_version INTEGER NOT NULL DEFAULT 1, source_api_version TEXT,
 UNIQUE(request_id, attempt)
);
INSERT INTO "fetches" VALUES(1,'fetch-1','request-1',1,'2026-09-30T19:00:00+00:00','2026-09-30T19:00:00+00:00',NULL,200,NULL,'76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4','ok',NULL,0,1,NULL);
INSERT INTO "fetches" VALUES(2,'fetch-2','request-2',1,'2026-09-30T19:00:00+00:00','2026-09-30T19:00:00+00:00',NULL,200,NULL,'bb3520659e6a7e9e3c29a8bccd5daf8415aefef04fad3cd6ef8df6293f4c0b4f','ok',NULL,0,1,NULL);
INSERT INTO "fetches" VALUES(3,'fetch-3','request-3',1,'2026-09-30T19:00:00+00:00','2026-09-30T19:00:00+00:00',NULL,200,NULL,'76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4','ok',NULL,0,1,NULL);
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
INSERT INTO "forecast_bindings" VALUES(1,'381b1dafbfdc13240b87712f6867e2834569168df24a7a9d96165337cc2a8673','synthetic-run','synthetic-game','t60-v1','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','synthetic',1,NULL,'576b067b8d11381b4307def72f7f25e507c7962c80db1d6caa2e0dad0e56c89d','{"candidate_key":"synthetic-game","code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_at":"2026-09-30T19:00:00+00:00","cutoff":"2026-09-30T19:00:00+00:00","event_ticker":"KXMLBGAME-SYNTHETIC","game_id":900001,"game_number":1,"manifest":{"synthetic":true},"mode":"synthetic","original_date":"2026-09-30","original_start":"2026-09-30T20:00:00+00:00","policy_version":"t60-v1","previous_id":null,"reasons":[],"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"}],"revision":1,"run_id":"synthetic-run","scheduled_start":"2026-09-30T20:00:00+00:00","schema_version":1,"status":"synthetic-prepared","team_ids":[1,2],"terms_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ticker":"KXMLBGAME-SYNTHETIC-A","yes_team_id":1}');
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
INSERT INTO "forecast_decisions" VALUES(1,'cc016047436b0530e6e98ac3e4aadb5340bddd1ddda07ccf52eab3769a152f3a','synthetic-run','synthetic','mlb-t60-v1','2026-09-30T19:00:00+00:00',1,1,NULL,'4cb9dfbb8ad9c1df32b2dcf895d8e1306f140baca10abfb60116afd5a0bfe656','{"baseline_versions":["constant-v1","midpoint-v1"],"binding_id":1,"code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_at":"2026-09-30T19:00:00+00:00","cutoff":"2026-09-30T19:00:00+00:00","cutoff_eligible":true,"dataset":"synthetic-study","discovery_id":1,"game_id":900001,"horizon":3600,"manifest":{"synthetic":true},"mode":"synthetic","protocol":"mlb-t60-v1","reasons":[],"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"}],"results":{"constant-v1":{"inputs":{},"probability":"0.5","reasons":[]},"midpoint-v1":{"inputs":{},"probability":null,"reasons":["book_missing"]}},"run_id":"synthetic-run","scheduled_start":"2026-09-30T20:00:00+00:00","schema_version":1,"selection_version":"v1","source_ceiling":3,"supersedes_id":null,"ticker":"KXMLBGAME-SYNTHETIC-A","unresolved_key":null,"yes_team_id":1}');
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
INSERT INTO "forecast_publications" VALUES(1,'2c3ef71aa5900cb93e524d85233dc5d4c6080c90479f3794772063c62ba5e566','synthetic-run',1,'allowed','5829313144c5d2b2e997dd59245aa78fc96dfaf351d32b849726ba9f308c9c6f','{"attempted_at":"2026-09-30T19:00:00+00:00","code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_at":"2026-09-30T19:00:00+00:00","decision_digest":"4cb9dfbb8ad9c1df32b2dcf895d8e1306f140baca10abfb60116afd5a0bfe656","decision_id":1,"manifest":{"synthetic":true},"mode":"synthetic","publication_policy":"v1","reasons":[],"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"}],"run_id":"synthetic-run","schema_version":1,"verdict":"allowed"}');
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
INSERT INTO "forecast_receipts" VALUES(1,'711feb79f1eb90c6085b9877e119cee21e51cf3dcee39850b9bccfca4f79623c','synthetic-run','publication',NULL,NULL,1,NULL,'2026-09-30T19:00:00+00:00','6e37c5b4c8ed8be3727638694e996944c83e35983a9937a5d9986f06df37d4d6','{"binding_id":null,"clock_status":"trusted","code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","config_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","created_at":"2026-09-30T19:00:00+00:00","discovery_id":null,"fetch_id":null,"mode":"synthetic","monotonic_offset":"0.1","observed_at":"2026-09-30T19:00:00+00:00","observer_namespace":"synthetic-observer","publication_id":1,"references":[{"digest":"86f8638932303672f31cac927a50ea0d44922172f55a1a6216489ce765f61ba3","fetches":[{"id":"fetch-1","raw_sha256":"76140d171ec5fd63d79d896087bad4505be1272590779d23e569ab0f52d661f4","retrieved_at":"2026-09-30T19:00:00+00:00","seq":1,"state":"ok"}],"key":{"id":"fetch-1"},"kind":"document","parser_version":null,"source_mode":"synthetic","table":"fetches"}],"run_id":"synthetic-run","schema_version":1,"session_id":"synthetic-session","subject_digest":"5829313144c5d2b2e997dd59245aa78fc96dfaf351d32b849726ba9f308c9c6f","subject_kind":"publication"}');
CREATE TABLE observations (
 id INTEGER PRIMARY KEY, fetch_id TEXT NOT NULL REFERENCES fetches(id),
 ticker TEXT NOT NULL, kind TEXT NOT NULL, parser_version INTEGER NOT NULL,
 data_json TEXT NOT NULL, UNIQUE(fetch_id, kind, ticker, parser_version)
);
INSERT INTO "observations" VALUES(1,'fetch-1','SYNTHETIC','synthetic',1,'{"value":"A"}');
INSERT INTO "observations" VALUES(2,'fetch-2','SYNTHETIC','synthetic',1,'{"value":"B"}');
INSERT INTO "observations" VALUES(3,'fetch-3','SYNTHETIC','synthetic',1,'{"value":"A"}');
CREATE TABLE requests (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL,
 request_key TEXT NOT NULL, url TEXT NOT NULL, params_json TEXT NOT NULL,
 done INTEGER NOT NULL DEFAULT 0, checkpoint_json TEXT,
 UNIQUE(run_id, stage, request_key)
);
INSERT INTO "requests" VALUES('request-1','synthetic-run','document','1','https://example.invalid/synthetic','{}',1,NULL);
INSERT INTO "requests" VALUES('request-2','synthetic-run','document','2','https://example.invalid/synthetic','{}',1,NULL);
INSERT INTO "requests" VALUES('request-3','synthetic-run','document','3','https://example.invalid/synthetic','{}',1,NULL);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, provenance_json TEXT NOT NULL, summary_json TEXT
);
INSERT INTO "runs" VALUES('synthetic-run','fixture','2026-09-30T19:00:00+00:00','2026-09-30T19:00:00+00:00','complete','{"abstentions":[],"code_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","configuration":{},"cost":null,"evaluation_version":null,"forecasts":[],"input_snapshot_ids":[],"kind":"fixture","model":null,"model_provider":null,"model_settings":null,"outcome_ids":[],"outputs":[],"prompt_version":null,"run_id":"synthetic-run","runtime_seconds":null,"schema_version":1,"tool_configuration":{}}','{}');
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO "schema_migrations" VALUES(1,'2026-09-30T19:00:00+00:00');
INSERT INTO "schema_migrations" VALUES(2,'2026-09-30T19:00:00+00:00');
INSERT INTO "schema_migrations" VALUES(3,'2026-10-19T04:15:19.350555+00:00');
CREATE TABLE settlement_observations (
    kind TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    fetch_id TEXT NOT NULL REFERENCES fetches(id),
    version_id INTEGER NOT NULL REFERENCES settlement_versions(id),
    parser_version INTEGER NOT NULL,
    PRIMARY KEY(kind,entity_key,fetch_id,parser_version)
);
INSERT INTO "settlement_observations" VALUES('kalshi','SYNTHETIC','fetch-1',1,1);
INSERT INTO "settlement_observations" VALUES('kalshi','SYNTHETIC','fetch-2',2,1);
INSERT INTO "settlement_observations" VALUES('kalshi','SYNTHETIC','fetch-3',3,1);
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
INSERT INTO "settlement_targets" VALUES('target','synthetic-run','SYNTHETIC','fetch-1','fetch-2','fetch-2','fetch-3',NULL,'pending');
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
INSERT INTO "settlement_versions" VALUES(1,'kalshi','SYNTHETIC',1,NULL,'798640599597df7a8daa32b1132f07850a68b5e71bd295650399a38074f52804','{"synthetic":"A"}','corrected','fetch-1');
INSERT INTO "settlement_versions" VALUES(2,'kalshi','SYNTHETIC',2,1,'955cca1ceba45052d85984d3a2565f4ce25b7488602c60a165598bf80b26e472','{"synthetic":"B"}','corrected','fetch-2');
INSERT INTO "settlement_versions" VALUES(3,'kalshi','SYNTHETIC',3,2,'798640599597df7a8daa32b1132f07850a68b5e71bd295650399a38074f52804','{"synthetic":"A"}','corrected','fetch-3');
CREATE INDEX fetches_state ON fetches(state, seq);
CREATE INDEX entity_ticker ON entities(kind, ticker, id);
CREATE INDEX observation_ticker ON observations(ticker, kind, id);
CREATE INDEX eligibility_run ON eligibility(run_id, id);
CREATE INDEX settlement_observations_version ON settlement_observations(version_id);
CREATE INDEX settlement_targets_ticker ON settlement_targets(ticker);
CREATE TRIGGER discovery_passes_no_update BEFORE UPDATE ON discovery_passes BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER discovery_passes_no_delete BEFORE DELETE ON discovery_passes BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_bindings_no_update BEFORE UPDATE ON forecast_bindings BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_bindings_no_delete BEFORE DELETE ON forecast_bindings BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_decisions_no_update BEFORE UPDATE ON forecast_decisions BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_decisions_no_delete BEFORE DELETE ON forecast_decisions BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_publications_no_update BEFORE UPDATE ON forecast_publications BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_publications_no_delete BEFORE DELETE ON forecast_publications BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_receipts_no_update BEFORE UPDATE ON forecast_receipts BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER forecast_receipts_no_delete BEFORE DELETE ON forecast_receipts BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER evaluation_runs_no_update BEFORE UPDATE ON evaluation_runs BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
CREATE TRIGGER evaluation_runs_no_delete BEFORE DELETE ON evaluation_runs BEGIN SELECT RAISE(ABORT, 'immutable forecast history'); END;
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
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('fetches',3);
COMMIT;
PRAGMA user_version=3;
