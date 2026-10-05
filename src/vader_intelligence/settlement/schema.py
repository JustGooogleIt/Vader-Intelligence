from importlib.resources import files

from ..storage import timestamp


def require_schema(store):
    if store.db.execute("PRAGMA user_version").fetchone()[0] not in (2, 3):
        raise ValueError("settlement requires explicit migration: vader settlement migrate")


def migrate(store):
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    if version in (2, 3):
        return
    if version != 1:
        raise ValueError("settlement migration requires baseline schema 1")
    script = files("vader_intelligence").joinpath("migrations/002_settlement.sql").read_text()
    try:
        store.db.executescript("BEGIN IMMEDIATE;\n" + script)
        store.db.execute("INSERT INTO schema_migrations VALUES (2,?)", (timestamp(),))
        store.db.execute("PRAGMA user_version=2")
        store.db.execute("COMMIT")
    except BaseException:
        if store.db.in_transaction:
            store.db.execute("ROLLBACK")
        raise
