from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.modules.data_pipeline.normalizers.traffic_network import network_feature
from jobs.import_topis_geometry import read_standard_mapping, read_vertices

NOW = datetime(2026, 10, 22, tzinfo=UTC)
FEATURE = {
    "id": "1000000300",
    "geometry": {"type": "LineString", "coordinates": [[126.97, 37.56], [126.98, 37.57]]},
    "properties": {"service_link_id": "1000000300"},
}


def observation(link="1000000300", age=0, speed="24"):
    return SimpleNamespace(
        collected_at=NOW - timedelta(minutes=age),
        data={"rows": [{"link_id": link, "prcs_spd": speed, "prcs_trv_time": "45"}]},
    )


def test_official_mapping_is_used_instead_of_matching_similar_ids():
    mapping = read_standard_mapping()
    assert "1000022501" in mapping["1000000100"]
    assert "1000022502" in mapping["1000000100"]
    assert (
        network_feature(FEATURE, observation(link="1000000301"), now=NOW)["properties"]["speed_kmh"]
        is None
    )


def test_service_speed_and_zero_speed_are_valid_but_stale_not_exposed():
    assert network_feature(FEATURE, observation(), now=NOW)["properties"]["speed_kmh"] == 24
    assert network_feature(FEATURE, observation(speed="0"), now=NOW)["properties"]["speed_kmh"] == 0
    stale = network_feature(FEATURE, observation(age=16), now=NOW)["properties"]
    assert stale["speed_kmh"] is None and stale["observation_status"] == "stale"
    assert (
        network_feature(FEATURE, observation(speed="NaN"), now=NOW)["properties"]["speed_kmh"]
        is None
    )


def test_failed_new_attempt_preserves_history_without_claiming_current_speed():
    attempt = SimpleNamespace(collected_at=NOW, data={"status": "failed"})
    result = network_feature(FEATURE, observation(age=1), attempt, now=NOW)["properties"]
    assert result["observation_status"] == "latest_attempt_failed"
    assert result["speed_kmh"] is None


def test_official_vertices_keep_distinct_order_and_valid_ids():
    rows = read_vertices()
    assert len({r["link_id"] for r in rows}) == 5093
    assert all(len(r["link_id"]) == 10 for r in rows)
    assert len({(r["link_id"], r["seq"]) for r in rows}) == len(rows)


def test_provider_missing_observation_is_distinct_from_not_collected():
    attempt = SimpleNamespace(
        collected_at=NOW, data={"status": "failed", "error_code": "provider_no_observation"}
    )
    result = network_feature(FEATURE, None, attempt, now=NOW)["properties"]
    assert result["observation_status"] == "no_observation"
    assert result["speed_kmh"] is None
