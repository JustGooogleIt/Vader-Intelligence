# Legacy archives written by older implementations

Synthetic, disposable SQLite archives produced by running **older code itself** (not
this checkout) and stored as SQL text dumps plus `PRAGMA user_version`. They exist to
test decision D-001 (`docs/roadmap/decisions.md`): record contract 2 was amended in place
by the draft PR #12, so the current code must open, verify, classify, back up and migrate
archives written before and after that amendment without reinterpreting any legacy row
as operational proof.

| File | Written by | Schema | Contents |
|---|---|---|---|
| `main-ebb58df-schema3.sql` | `main` `ebb58df9b89922db4828fda7d23891dd917a4b90` | 3 | The F3.2 storage fixture (`tests/forecast_storage/fixtures.populated`): contract-1 envelopes of every table |
| `draft-5811991-schema4.sql` | PR #11 head `58119912fa1098dcfd7415e15ddfcf8cb6b21566` | 4 | Injected-transport two-phase collection, warm and due ticks (contract-2 bindings/decision/publication, synthetic), a default-clock witness that promoted the injected fetches to `forward_shadow` (PR #10 review P1), and a contract-2 receipt supplied by an ordinary journal caller (review P1, second finding) |
| `amended-82665cd-schema4.sql` | PR #16 head `82665cd02a9db9694f957eca32537cca5cf71c31` | 4 | The deterministic research demo archive (`tests/integration/research_demo.py --keep`): collection, ticks, settlement, forward and reconstruction evaluations, all synthetic |

Some draft rows are labelled `forward_shadow` although their inputs are synthetic. That
is deliberate: they record exactly what the draft code accepted.

## Regenerate

From this checkout, with `OLD` a clean checkout of the writing revision (its own `src`
and `tests` first on the path; this checkout's application code is never imported):

```sh
PYTHONPATH=$OLD/src:$OLD/tests:$OLD/tests/forecast \
  .venv/bin/python tests/fixtures/legacy_archives/generate.py --era draft \
  --out tests/fixtures/legacy_archives/draft-5811991-schema4.sql
```

Eras: `main`, `draft`, `amended`. Run it from an empty temporary working directory.
Regenerated dumps differ in random identifiers and wall-clock fields (run ids, the
default-clock witness time); the tests assert structure and classification, not bytes.

## Load

`generate.load(sql_path, db_path)` recreates the archive file. The tests are in
`tests/forecast_storage/test_contract_versioning.py`; the installed-wheel smoke also
migrates the draft archive.
