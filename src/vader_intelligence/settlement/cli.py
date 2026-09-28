from contextlib import nullcontext

from ..replay import replay
from ..storage import Store, writer_lock
from ..transport import Reader
from .schema import migrate, require_schema
from .service import SettlementRefresh, inspect


def register(commands):
    group = commands.add_parser("settlement", help="independent mapping and settlement evidence")
    subs = group.add_subparsers(dest="settlement_command", required=True)
    subs.add_parser("migrate", help="explicitly expand schema 1 to 2")
    sub = subs.add_parser("refresh", help="bounded public read-only provider refresh")
    sub.add_argument("--ticker", action="append", default=[], help="repeat for explicit contracts")
    sub.add_argument("--limit", type=int, default=20)
    sub.add_argument("--max-pages", type=int, default=2)
    sub.add_argument("--budget", type=float, default=120)
    sub.add_argument("--status", choices=("settled", "closed", "all"), default="settled")
    sub.add_argument("--resume")
    subs.add_parser("replay", help="replay archived collector and settlement responses")
    sub = subs.add_parser("inspect", help="read-only bounded inspection")
    sub.add_argument("--ticker")
    sub.add_argument("--limit", type=int, default=20)
    sub.add_argument("--history", action="store_true")


def execute(args, config):
    readonly = args.settlement_command == "inspect"
    with nullcontext() if readonly else writer_lock(config.database):
        store = Store(config.database, min_free_bytes=config.min_free_bytes, readonly=readonly)
        try:
            if args.settlement_command == "migrate":
                migrate(store)
                return {"status": "complete", "schema_version": 2, "database": str(store.path)}
            require_schema(store)
            if args.settlement_command == "inspect":
                return inspect(store, target=args.ticker, limit=args.limit, history=args.history)
            if args.settlement_command == "replay":
                return replay(store)
            reader = Reader(store, config)
            try:
                return SettlementRefresh(store, reader, config).run(
                    tickers=args.ticker,
                    limit=args.limit,
                    max_pages=args.max_pages,
                    budget=args.budget,
                    status=args.status,
                    resume=args.resume,
                )
            finally:
                reader.close()
        finally:
            store.close()
