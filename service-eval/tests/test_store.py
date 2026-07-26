from datetime import datetime, timedelta, timezone

import pytest

from cfwarp_service_eval.store import QueueFull, Store


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


def test_restart_recovery_records_unknown_observation_and_resumes_group(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    group = store.create_group(["direct-de"], ["youtube"])
    store.next_group()
    task = store.next_task(group["id"])
    store.start_task(task["id"])
    store.recover()
    recovered = store.group(group["id"])
    assert recovered["status"] == "queued"
    assert recovered["tasks"][0]["status"] == "unknown"
    observation = store.latest()[0]
    assert observation["result"] == {
        "availability": "unknown",
        "class": "tooling_failure",
        "eligible": False,
    }
    assert observation["failure_layer"] == "tooling"


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
    store.fail_task(task["id"], group["id"], "direct-de", "youtube", "test")
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
