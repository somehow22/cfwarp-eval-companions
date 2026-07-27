from datetime import datetime, timedelta, timezone

import pytest

from cfwarp_service_eval.store import QueueFull, Store


def lane():
    return {
        "id": "direct-de",
        "instance_id": "cfwarp-direct-de",
        "node_id": "proxy-host-1",
        "composition": "direct-warp",
        "transport": "wireguard",
        "substrate_profile": None,
        "requested_region": "DE",
        "image_identity": "example@sha256:" + "a" * 64,
        "config_digest": "sha256:" + "b" * 64,
    }


def test_queue_depth_admits_a_chunked_sweep_then_rejects(tmp_path):
    # A 9-lane node chunks into more than the old one-active-plus-one-waiting
    # capacity, so depth must exceed 2 while staying bounded.
    store = Store(tmp_path / "state.sqlite3", max_pending_groups=3)
    first = store.create_group(["direct-de"], ["youtube"])
    store.next_group()
    store.create_group(["direct-de"], ["chatgpt"])
    store.create_group(["direct-de"], ["gemini"])
    assert first["status"] == "queued"
    with pytest.raises(QueueFull):
        store.create_group(["direct-de"], ["reddit"])


def test_restart_recovery_records_unknown_observation_and_completes_group(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    group = store.create_group(["direct-de"], ["youtube"])
    store.next_group()
    task = store.next_task(group["id"])
    store.start_task(task["id"])
    store.recover(
        {"direct-de": lane()},
        {"youtube": "youtube.anonymous_public_video"},
    )
    recovered = store.group(group["id"])
    assert recovered["status"] == "complete"
    assert recovered["tasks"][0]["status"] == "unknown"
    observation = store.latest()[0]
    assert observation["result"] == {
        "availability": "unknown",
        "class": "tooling_failure",
        "eligible": False,
    }
    assert observation["failure_layer"] == "tooling"
    assert observation["scenario_id"] == "youtube.anonymous_public_video"
    assert observation["subject"] == {
        "instance_id": "cfwarp-direct-de",
        "node_id": "proxy-host-1",
        "runtime": "podman",
        "image_identity": "example@sha256:" + "a" * 64,
        "config_digest": "sha256:" + "b" * 64,
    }
    assert observation["lane"] == {
        "composition": "direct-warp",
        "transport": "wireguard",
        "substrate_profile": None,
        "requested_region": "DE",
    }


def test_recovery_supersedes_duplicate_pending_cells(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = store.create_group(["direct-de"], ["youtube"])
    duplicate = store.create_group(["direct-de"], ["youtube"])

    store.recover(
        {"direct-de": lane()},
        {"youtube": "youtube.anonymous_public_video"},
    )

    assert store.pending_cells() == {("direct-de", "youtube")}
    assert store.group(first["id"])["tasks"][0]["status"] == "queued"
    duplicate_group = store.group(duplicate["id"])
    assert duplicate_group["status"] == "complete"
    assert duplicate_group["tasks"][0]["status"] == "superseded"


def test_recovery_backfills_legacy_unknown_provenance_without_changing_result_time(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite3")
    group = store.create_group(["direct-de"], ["youtube"])
    now = datetime.now(timezone.utc)
    legacy = {
        "schema_version": 1,
        "observation_id": "legacy-unknown",
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(hours=24)).isoformat(),
        "scenario_id": "youtube",
        "probe": {"name": "probe-worker", "version": "1", "execution": "local"},
        "subject": {
            "instance_id": "direct-de",
            "node_id": None,
            "runtime": None,
            "image_identity": None,
            "config_digest": None,
        },
        "lane": {
            "composition": None,
            "transport": None,
            "substrate_profile": None,
            "requested_region": None,
        },
        "egress": {"warp": None, "region": None, "colo": None},
        "result": {
            "availability": "unknown",
            "class": "tooling_failure",
            "eligible": False,
        },
        "confidence_stage": "single_observation",
        "failure_layer": "tooling",
        "latency_ms": 0,
        "artifacts": [],
    }
    store._insert_observation(group["id"], "direct-de", "youtube", legacy)

    store.recover(
        {"direct-de": lane()},
        {"youtube": "youtube.anonymous_public_video"},
    )

    repaired = store.latest()[0]
    assert repaired["observation_id"] == "legacy-unknown"
    assert repaired["observed_at"] == legacy["observed_at"]
    assert repaired["fresh_until"] == legacy["fresh_until"]
    assert repaired["result"] == legacy["result"]
    assert repaired["failure_layer"] == "tooling"
    assert repaired["scenario_id"] == "youtube.anonymous_public_video"
    assert repaired["subject"]["config_digest"] == lane()["config_digest"]
    assert repaired["lane"]["composition"] == "direct-warp"


def test_observation_artifact_path_is_reduced_to_basename(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    group = store.create_group(["direct-de"], ["youtube"])
    store.next_group()
    task = store.next_task(group["id"])
    store.start_task(task["id"])
    now = datetime.now(timezone.utc)
    observation = {
        "schema_version": 1,
        "observation_id": "obs-1",
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(days=1)).isoformat(),
        "scenario_id": "youtube.anonymous_public_video",
        "probe": {},
        "subject": {},
        "lane": {},
        "egress": {},
        "result": {},
        "confidence_stage": "single_observation",
        "failure_layer": "none",
        "latency_ms": 1,
        "artifacts": [{"kind": "summary", "path": "/private/state/group/summary.json"}],
    }
    store.finish_task(task["id"], group["id"], "direct-de", "youtube", observation)
    assert store.latest()[0]["artifacts"][0]["path"] == "summary.json"


def observation(scenario_id, availability, *, hours_fresh=24, extra=None):
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "observation_id": f"obs-{scenario_id}-{availability}",
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(hours=hours_fresh)).isoformat(),
        "scenario_id": scenario_id,
        "probe": {},
        "subject": {},
        "lane": {},
        "egress": {},
        "result": {"availability": availability, "class": "ok", "eligible": True},
        "confidence_stage": "single_observation",
        "failure_layer": "none",
        "latency_ms": 1,
        "artifacts": [],
    }
    payload.update(extra or {})
    return payload


def record(store, lane, scenario_id, availability, **kwargs):
    group = store.create_group([lane], [scenario_id])
    store.next_group()
    task = store.next_task(group["id"])
    store.start_task(task["id"])
    store.finish_task(
        task["id"],
        group["id"],
        lane,
        scenario_id,
        observation(scenario_id, availability, **kwargs),
    )
    store.complete_group(group["id"])


def beat(store, lane, ok, count=1):
    for _ in range(count):
        store.record_heartbeat(
            lane, {"ok": ok, "checks": {"trace": {"warp": "on" if ok else "off"}}}
        )


def test_tier_is_preferred_when_heartbeat_healthy_and_scenarios_available(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", True, count=10)
    record(store, "direct-de", "youtube", "available")
    tier = store.lane_tier("direct-de")
    assert tier["tier"] == "preferred"
    assert tier["unavailable_scenarios"] == []


def test_tier_is_unknown_when_evidence_expired(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", True, count=10)
    record(store, "direct-de", "youtube", "available", hours_fresh=-1)
    tier = store.lane_tier("direct-de")
    assert tier["tier"] == "unknown"
    assert tier["stale_scenarios"] == ["youtube"]


def test_tier_quarantines_a_lane_whose_heartbeat_mostly_fails(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", False, count=9)
    beat(store, "direct-de", True, count=1)
    record(store, "direct-de", "youtube", "available")
    assert store.lane_tier("direct-de")["tier"] == "quarantined"


def test_tier_is_usable_when_some_but_not_all_scenarios_fail(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", True, count=10)
    record(store, "direct-de", "youtube", "available")
    record(store, "direct-de", "reddit", "unavailable")
    tier = store.lane_tier("direct-de")
    assert tier["tier"] == "usable"
    assert tier["unavailable_scenarios"] == ["reddit"]


def test_tier_is_degraded_when_every_fresh_scenario_is_unavailable(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", True, count=10)
    record(store, "direct-de", "youtube", "unavailable")
    assert store.lane_tier("direct-de")["tier"] == "degraded"


def test_tier_is_usable_when_throughput_is_below_floor(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", True, count=10)
    record(store, "direct-de", "youtube", "available")
    record(
        store,
        "direct-de",
        "perf",
        "available",
        extra={"perf": {"throughput_mibps": 1.2, "meets_floor": False}},
    )
    tier = store.lane_tier("direct-de")
    assert tier["tier"] == "usable"
    assert tier["meets_throughput_floor"] is False


def test_heartbeats_are_pruned_with_the_retention_window(tmp_path):
    store = Store(tmp_path / "state" / "queue.sqlite3")
    beat(store, "direct-de", True, count=3)
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    with store.db:
        store.db.execute("UPDATE heartbeats SET observed_at=?", (old,))
    store.prune(tmp_path / "state" / "artifacts", 14, 512 * 1024 * 1024)
    assert store.heartbeat_stats("direct-de")["samples"] == 0


def test_retention_removes_oldest_completed_group_and_artifacts(tmp_path):
    store = Store(tmp_path / "state" / "queue.sqlite3")
    group = store.create_group(["direct-de"], ["youtube"])
    store.next_group()
    task = store.next_task(group["id"])
    store.fail_task(
        task["id"],
        group["id"],
        lane(),
        "youtube",
        "youtube.anonymous_public_video",
        "test",
    )
    store.complete_group(group["id"])
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    with store.db:
        store.db.execute(
            "UPDATE run_groups SET finished_at=? WHERE id=?", (old, group["id"])
        )
    artifact_root = tmp_path / "state" / "artifacts"
    artifact = artifact_root / group["id"] / "direct-de" / "youtube" / "summary.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("evidence")
    store.prune(artifact_root, retention_days=14, max_bytes=512 * 1024 * 1024)
    with pytest.raises(KeyError):
        store.group(group["id"])
    assert not (artifact_root / group["id"]).exists()


def test_one_failed_beat_does_not_quarantine_a_lane(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", False, count=1)
    record(store, "direct-de", "youtube", "available")
    # A single sample is noise, not evidence of sustained failure.
    assert store.lane_tier("direct-de")["tier"] != "quarantined"


def test_a_recovered_lane_is_not_held_down_by_old_failures(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "direct-de", False, count=40)
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    with store.db:
        store.db.execute("UPDATE heartbeats SET observed_at=?", (old,))
    beat(store, "direct-de", True, count=10)
    record(store, "direct-de", "youtube", "available")
    tier = store.lane_tier("direct-de")
    assert tier["tier"] == "preferred", tier["reason"]


def test_performance_band_reflects_measured_throughput(tmp_path):
    from cfwarp_service_eval.store import performance_band

    assert performance_band(19.7) == "fast"
    assert performance_band(13.5) == "fast"
    assert performance_band(5.0) == "moderate"
    assert performance_band(0.78) == "slow"
    assert performance_band(None) is None


def test_a_slow_substrate_lane_is_banded_but_never_failed(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat(store, "sub-lane", True, count=10)
    record(store, "sub-lane", "youtube", "available")
    # Substrate lanes carry no floor, so meets_floor is None and the lane passes.
    record(
        store,
        "sub-lane",
        "perf",
        "available",
        extra={"perf": {"throughput_mibps": 0.78, "meets_floor": None}},
    )
    tier = store.lane_tier("sub-lane")
    assert tier["performance_band"] == "slow"
    assert tier["throughput_mibps"] == 0.78
    # Banding must not demote the lane: it is not a gate.
    assert tier["tier"] == "preferred"


def beat_loc(store, lane, loc, count=1):
    for _ in range(count):
        store.record_heartbeat(
            lane, {"ok": True, "checks": {"trace": {"warp": "on", "loc": loc}}}
        )


def test_region_mismatch_is_detected_but_never_demotes(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    # Asked for Austria, provider silently served Germany every time.
    beat_loc(store, "ps-at", "DE", count=10)
    record(store, "ps-at", "youtube", "available")
    tier = store.lane_tier("ps-at", requested_region="AT")
    region = tier["region"]
    assert region["matches"] is False
    assert region["observed"] == "DE"
    assert region["match_ratio"] == 0.0
    # A mismatch is evidence, not failure. The lane must not be demoted.
    assert tier["tier"] == "preferred"


def test_one_region_mismatch_is_not_persistent_drift(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat_loc(store, "ps-at", "DE")
    region = store.lane_tier("ps-at", requested_region="AT")["region"]
    assert region["matches"] is None
    assert region["samples"] == 1


def test_region_match_is_case_insensitive_and_tolerates_variance(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat_loc(store, "fv-ch", "ch", count=8)
    beat_loc(store, "fv-ch", "DE", count=2)
    region = store.lane_tier("fv-ch", requested_region="CH")["region"]
    # Occasional drift should not read as a broken lane.
    assert region["matches"] is True
    assert region["match_ratio"] == 0.8


def test_a_lane_requesting_no_region_cannot_mismatch(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    beat_loc(store, "direct-wg", "US", count=5)
    region = store.lane_tier("direct-wg", requested_region=None)["region"]
    assert region["matches"] is None
    assert region["requested"] is None
