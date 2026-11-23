"""D-015: `last_trade_gap_c@2` means the same thing for candles and listings.

v1 read a listing's last traded price as recent whenever the listing was retrieved
recently, so a market that had not traded for hours had a value forward and none in
history. v2 counts only trades that two observations prove happened within 60 min of C.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from vader_intelligence.features import definitions
from vader_intelligence.features.quotes import Quote, QuoteSeries

C = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
V1 = definitions.DEFINITIONS["last_trade_gap_c@1"].fn
V2 = definitions.DEFINITIONS["last_trade_gap_c@2"].fn


def q(minutes_before, *, price=None, volume=None, bid="0.40", ask="0.44"):
    return Quote(
        at=C - timedelta(minutes=minutes_before),
        yes_bid=Decimal(bid),
        yes_ask=Decimal(ask),
        trade_price=None if price is None else Decimal(price),
        volume=None if volume is None else Decimal(volume),
        open_interest=None,
        ref=f"f{minutes_before}",
    )


def ctx(series):
    return definitions.FeatureContext(
        cutoff=C,
        start=C + timedelta(hours=1),
        official_date="2026-11-22",
        game_type="R",
        selected_team=1,
        opponent_team=2,
        selected_is_home=True,
        selected=series,
        other=None,
    )


def listing(*quotes):
    return QuoteSeries("T", "listing", "cumulative", tuple(quotes))


def candles(*quotes):
    return QuoteSeries("T", "candles", "period", tuple(quotes))


def test_a_quiet_listed_market_is_missing_in_v2_but_not_v1():
    # Last trade long ago: the price is listed on every retrieval, volume never moves.
    series = listing(*(q(m, price="0.50", volume="100") for m in range(120, -1, -2)))
    assert V1(ctx(series)) == pytest.approx(0.08)  # the D-015 defect: stale price as recent
    assert V2(ctx(series)) is None


def test_a_listing_trade_between_two_retrievals_within_the_hour_counts():
    series = listing(
        q(12, price="0.50", volume="100"),
        q(10, price="0.45", volume="103"),  # traded between C-12 and C-10
        q(2, price="0.45", volume="103"),
    )
    assert V2(ctx(series)) == pytest.approx(0.03)  # 0.45 - mid 0.42


@pytest.mark.parametrize(
    "earliest, counted",
    [(60, True), (61, False)],
)
def test_the_trade_must_provably_be_within_sixty_minutes(earliest, counted):
    series = listing(
        q(earliest, price="0.50", volume="100"),
        q(earliest - 2, price="0.46", volume="101"),
        q(1, price="0.46", volume="101"),
    )
    assert (V2(ctx(series)) is not None) is counted


def test_a_long_gap_between_retrievals_does_not_prove_recency():
    # Volume rose, but the earlier retrieval is 90 min old: the trade may be older than 60.
    series = listing(q(90, price="0.50", volume="100"), q(1, price="0.47", volume="105"))
    assert V2(ctx(series)) is None


def test_candles_count_only_periods_with_volume_and_a_price():
    no_volume = candles(q(5, volume="0"), q(4, price="0.48", volume="0"), q(1, volume="0"))
    assert V2(ctx(no_volume)) is None
    no_price = candles(q(5, volume="0"), q(4, volume="3"), q(1, volume="0"))
    assert V2(ctx(no_price)) is None
    traded = candles(q(5, volume="0"), q(4, price="0.48", volume="3"), q(1, volume="0"))
    assert V2(ctx(traded)) == pytest.approx(0.06)


def test_the_same_trades_give_the_same_presence_in_both_sources():
    """One trade at C-30, nothing since: both sources agree it is present; one at C-90:
    both agree it is missing."""
    for minutes, present in ((30, True), (90, False)):
        as_candles = candles(
            *(
                q(m, price="0.47" if m == minutes else None, volume="2" if m == minutes else "0")
                for m in range(120, 0, -1)
            )
        )
        as_listing = listing(
            *(q(m, price="0.47", volume="10" if m > minutes else "12") for m in range(120, 0, -2))
        )
        assert (V2(ctx(as_candles)) is not None) is present
        assert (V2(ctx(as_listing)) is not None) is present


def test_the_v2_sets_change_only_the_trade_gap_feature():
    v1, v2 = definitions.FEATURE_SETS["fs-market-v1"], definitions.FEATURE_SETS["fs-market-v2"]
    assert [k for k in v1 if k not in v2] == ["last_trade_gap_c@1"]
    assert [k for k in v2 if k not in v1] == ["last_trade_gap_c@2"]
    assert v2.index("last_trade_gap_c@2") == v1.index("last_trade_gap_c@1")
    assert definitions.describe_set("fs-market-v2")["forward_capable"] is True
    full = definitions.FEATURE_SETS["fs-full-v2"]
    assert full[: len(v2)] == v2 and full[len(v2) :] == definitions.TEAM_FEATURES
    # v1 is untouched for the models already trained on it.
    assert "last_trade_gap_c@1" in definitions.FEATURE_SETS["fs-full-v1"]
