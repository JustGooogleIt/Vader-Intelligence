import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    database: str = "data/vader.sqlite3"
    requests_per_second: float = 2.0
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    request_budget: float = 45.0
    max_attempts: int = 3
    max_pages: int = 100
    page_size: int = 100
    max_markets: int = 100
    max_json_bytes: int = 2 * 1024 * 1024
    max_document_bytes: int = 10 * 1024 * 1024
    schedule_max_age: float = 120.0
    pregame_buffer: float = 120.0
    lookahead_days: int = 7
    health_stale_seconds: float = 180.0
    min_free_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "database":
                if not isinstance(value, str) or not value:
                    raise ValueError("database must be a nonempty local path")
                continue
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in (
            "max_attempts",
            "max_pages",
            "page_size",
            "max_markets",
            "max_json_bytes",
            "max_document_bytes",
            "lookahead_days",
            "min_free_bytes",
        ):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be an integer")
        if self.page_size > 1000 or self.lookahead_days > 14 or self.max_attempts > 3:
            raise ValueError("page_size <=1000, lookahead_days <=14, max_attempts <=3 required")
        if self.requests_per_second > 2 or self.request_budget > 45:
            raise ValueError("requests_per_second <=2 and request_budget <=45 required")
        if self.pregame_buffer < 120 or self.schedule_max_age > 120:
            raise ValueError("pregame_buffer >=120 and schedule_max_age <=120 required")

    @classmethod
    def load(cls, path: str | None = None, database: str | None = None):
        values = {}
        if path:
            p = Path(path).resolve()
            with p.open("rb") as f:
                values = tomllib.load(f)
            if set(values) - {"collector"}:
                raise ValueError("only [collector] configuration is supported")
            values = values.get("collector", {})
            if "database" in values:
                values["database"] = str(p.parent / values["database"])
        if database:
            values["database"] = database
        return cls(**values)
