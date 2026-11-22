"""Research protocol parsing and the canonical configuration hash (spec §2, R-1..R-3)."""

import pytest

from vader_intelligence.forecast.research import Protocol


def test_defaults_are_the_v1_table():
    p = Protocol()
    assert p.protocol_id == "mlb-t60-v1"
    assert p.horizon_seconds == 3600
    assert p.book_max_age_seconds == p.schedule_max_age_seconds == p.market_max_age_seconds == 300
    assert p.record_grace_seconds == 150
    assert p.log_epsilon == "0.000001"
    assert p.max_games == 100 and p.max_contracts == 200


def test_config_hash_is_stable_and_distinct_by_dataset():
    assert Protocol().config_hash == Protocol().config_hash
    assert Protocol(dataset="a").config_hash != Protocol(dataset="b").config_hash
    assert len(Protocol().config_hash) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol_id", "mlb-t30-v1"),
        ("horizon_seconds", 1800),
        ("book_max_age_seconds", 600),
        ("schedule_max_age_seconds", 200),
        ("record_grace_seconds", 120),
        ("log_epsilon", "0.001"),
        ("max_games", 0),
    ],
)
def test_out_of_contract_settings_are_rejected(field, value):
    with pytest.raises(ValueError):
        Protocol(**{field: value})


def test_load_reads_only_research_section(tmp_path):
    good = tmp_path / "research.toml"
    good.write_text('[research]\ndataset = "study-7"\nobserver_namespace = "ns-7"\n')
    p = Protocol.load(good)
    assert p.dataset == "study-7" and p.observer_namespace == "ns-7"
    assert p.config_hash == Protocol(dataset="study-7", observer_namespace="ns-7").config_hash
    # --dataset overrides the file.
    assert Protocol.load(good, dataset="cli").dataset == "cli"


def test_load_rejects_foreign_sections_and_unknown_fields(tmp_path):
    collector_leak = tmp_path / "bad.toml"
    collector_leak.write_text('[collector]\ndatabase = "x"\n')
    with pytest.raises(ValueError, match="only..research.."):
        Protocol.load(collector_leak)
    unknown = tmp_path / "unknown.toml"
    unknown.write_text("[research]\nmax_edge = 5\n")
    with pytest.raises(ValueError, match="unknown research"):
        Protocol.load(unknown)


def test_none_path_is_pure_defaults():
    assert Protocol.load(None).config_hash == Protocol().config_hash
