"""Chronological development / validation / holdout splits with an embargo (roadmap B2).

Rows are assigned by official date, so every row of one game (one group) lands in one
segment, and an embargo of whole days separates segments so that information from a day
never straddles two of them. The holdout is defined here but its rows are only ever read
through `holdout_rows`, which the training code never calls.
"""

from dataclasses import asdict, dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class SplitDefinition:
    development_through: str
    validation_through: str
    embargo_days: int = 1

    def __post_init__(self):
        dev = date.fromisoformat(self.development_through)
        val = date.fromisoformat(self.validation_through)
        if type(self.embargo_days) is not int or not 0 <= self.embargo_days <= 14:
            raise ValueError("embargo_days must be an integer in [0, 14]")
        if val <= dev + timedelta(days=self.embargo_days):
            raise ValueError("validation must end after development plus the embargo")

    @property
    def key(self):
        return (
            f"dev<={self.development_through}|val<={self.validation_through}"
            f"|embargo={self.embargo_days}"
        )

    def segment(self, official_date):
        day = date.fromisoformat(official_date)
        dev = date.fromisoformat(self.development_through)
        val = date.fromisoformat(self.validation_through)
        gap = timedelta(days=self.embargo_days)
        if day <= dev:
            return "development"
        if dev + gap < day <= val:
            return "validation"
        if day > val + gap:
            return "holdout"
        return "embargo"

    @property
    def first_holdout_date(self):
        """The first official date this split assigns to its holdout."""
        val = date.fromisoformat(self.validation_through)
        return (val + timedelta(days=self.embargo_days + 1)).isoformat()

    def to_dict(self):
        return asdict(self) | {"key": self.key}


def guard(registry, dataset_id, split, *, pin_if_missing):
    """The dataset's pinned holdout start; refuse a split whose validation reaches it.

    A dataset's final holdout is pinned once in the registry (official dates on or after
    `holdout_from`), so a later split cannot move holdout rows into development or
    validation. An unpinned dataset is pinned at this split's holdout start when
    `pin_if_missing` (a writable first use), else refused."""
    pin = registry.holdout_from(dataset_id)
    if pin is None:
        if not pin_if_missing:
            raise ValueError(
                f"dataset {dataset_id[:16]} has no pinned holdout; pin it first "
                "(vader dataset pin-holdout) or train or snapshot it with a writable registry"
            )
        pin = registry.pin_holdout(dataset_id, split.first_holdout_date, "first split " + split.key)
    if split.validation_through >= pin:
        raise ValueError(
            f"split {split.key} reaches the pinned holdout (official dates from {pin})"
        )
    return pin


def holdout_key(registry, dataset_id, split):
    """Holdout-ledger key for a final-holdout evaluation under `split`: the pinned boundary.

    The split's holdout must start exactly at the pin, so every evaluation of the same
    holdout rows shares one ledger key whatever development boundary it used."""
    pin = registry.holdout_from(dataset_id)
    if pin is None:
        pin = registry.pin_holdout(dataset_id, split.first_holdout_date, "first split " + split.key)
    if split.first_holdout_date != pin:
        raise ValueError(
            f"split {split.key} holds out from {split.first_holdout_date}, "
            f"but the dataset's holdout is pinned from {pin}"
        )
    return f"from={pin}"


def assign(rows, split):
    """Segment name per row index; a group is never split across segments."""
    segments = {}
    by_group = {}
    for index, row in enumerate(rows):
        name = split.segment(row["official_date"])
        segments[index] = name
        if by_group.setdefault(row["group"], name) != name:
            raise ValueError(f"group {row['group']} spans segments")
    return segments


def development_and_validation(rows, split):
    """(development rows, validation rows); holdout and embargo rows are not returned."""
    segments = assign(rows, split)
    dev = [r for i, r in enumerate(rows) if segments[i] == "development"]
    val = [r for i, r in enumerate(rows) if segments[i] == "validation"]
    return dev, val


def holdout_rows(rows, split):
    """Only for the one-time final holdout evaluation, recorded in the holdout ledger."""
    segments = assign(rows, split)
    return [r for i, r in enumerate(rows) if segments[i] == "holdout"]


def counts(rows, split):
    segments = assign(rows, split)
    out = {"development": 0, "validation": 0, "embargo": 0, "holdout": 0}
    for name in segments.values():
        out[name] += 1
    return out
