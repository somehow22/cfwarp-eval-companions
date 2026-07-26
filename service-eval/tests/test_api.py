import json
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from cfwarp_service_eval import api
from cfwarp_service_eval.runner import ProbeRunner


TOKEN = "test-token-that-is-longer-than-thirty-two-characters"


def lane():
    return {
        "id": "direct-de",
        "proxy": "socks5h://proxy-host-1:16710",
        "instance_id": "cfwarp-direct-de",
        "node_id": "proxy-host-1",
        "composition": "direct",
        "transport": "wireguard",
        "substrate_profile": None,
        "requested_region": "DE",
        "image_identity": "example@sha256:" + "a" * 64,
        "config_digest": "sha256:" + "b" * 64,
    }


def client(tmp_path, monkeypatch, lanes_payload=None, heartbeat=False, scheduler=False):
    lanes = tmp_path / "lanes.json"
    lanes.write_text(json.dumps(lanes_payload or [lane()]))
    token = tmp_path / "token"
    token.write_text(TOKEN)
    monkeypatch.setenv("SERVICE_EVAL_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SERVICE_EVAL_LANES_FILE", str(lanes))
    monkeypatch.setenv("SERVICE_EVAL_TOKEN_FILE", str(token))
    # Background loops are opt-in, and independently so: enabling the scheduler
    # would drive sweeps and muddy a heartbeat-only assertion.
    monkeypatch.setenv("SERVICE_EVAL_HEARTBEAT_ENABLED", "1" if heartbeat else "0")
    monkeypatch.setenv("SERVICE_EVAL_SCHEDULER_ENABLED", "1" if scheduler else "0")
    monkeypatch.setenv("SERVICE_EVAL_STARTUP_DELAY_SECONDS", "0")

    async def preflight(self, group_id, selected_lane):
        return {"ok": True, "checks": {"trace": {"warp": "on", "loc": "DE"}}}

    async def run(self, group_id, selected_lane, scenario_id):
        raise RuntimeError("bounded test failure")

    monkeypatch.setattr(ProbeRunner, "preflight", preflight)
    monkeypatch.setattr(ProbeRunner, "run", run)
    return TestClient(api.app)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_is_generic_and_v1_rejects_missing_bearer(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as test_client:
        assert test_client.get("/healthz").json() == {"status": "ok"}
        assert test_client.get("/v1/lanes").status_code == 401
        assert test_client.get("/docs").status_code == 401
        assert test_client.get("/openapi.json").status_code == 401


def test_lane_response_redacts_proxy_and_post_rejects_ssrf_fields(
    tmp_path, monkeypatch
):
    with client(tmp_path, monkeypatch) as test_client:
        response = test_client.get("/v1/lanes", headers=auth())
        assert response.status_code == 200
        assert "proxy" not in response.json()[0]
        response = test_client.post(
            "/v1/run-groups",
            headers=auth(),
            json={
                "lane_ids": ["direct-de"],
                "scenario_ids": ["youtube"],
                "proxy": "socks5h://attacker.invalid:1080",
                "url": "http://169.254.169.254/",
            },
        )
        assert response.status_code == 422


def test_tiers_report_unknown_before_any_evidence(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as test_client:
        assert test_client.get("/v1/tiers").status_code == 401
        body = test_client.get("/v1/tiers", headers=auth()).json()
        assert [row["lane_id"] for row in body] == ["direct-de"]
        assert body[0]["tier"] == "unknown"


def test_metrics_require_auth_and_expose_bounded_tier_series(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as test_client:
        assert test_client.get("/metrics").status_code == 401
        body = test_client.get("/metrics", headers=auth()).text
        assert 'cfwarp_probe_lane_tier{lane="direct-de"' in body
        assert 'tier="preferred"} 0' in body
        assert 'tier="unknown"} 1' in body
        # Per-attempt identifiers must never reach the backend as labels.
        assert "observation_id" not in body


def test_scheduler_chunks_due_lanes_to_the_configured_batch(tmp_path, monkeypatch):
    payload = []
    for index in range(7):
        entry = lane()
        entry["id"] = f"lane-{index}"
        payload.append(entry)
    monkeypatch.setenv("SERVICE_EVAL_LANE_CHUNK", "5")
    with client(tmp_path, monkeypatch, lanes_payload=payload) as test_client:
        assert test_client.get("/v1/lanes", headers=auth()).status_code == 200
        # No observations exist, so every lane is due for every scenario.
        created = api.runtime.enqueue_due_sweeps()
        assert created == 2
        groups = api.runtime.store.db.execute(
            "SELECT lane_ids FROM run_groups ORDER BY created_at"
        ).fetchall()
        sizes = sorted(len(json.loads(row["lane_ids"])) for row in groups)
        assert sizes == [2, 5]


def test_scheduler_skips_lanes_with_fresh_evidence(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as test_client:
        assert test_client.get("/v1/lanes", headers=auth()).status_code == 200
        store = api.runtime.store
        now = datetime.now(timezone.utc)
        group = store.create_group(["direct-de"], list(api.SCENARIOS))
        store.next_group()
        while task := store.next_task(group["id"]):
            store.start_task(task["id"])
            store.finish_task(
                task["id"],
                group["id"],
                "direct-de",
                task["scenario_id"],
                {
                    "schema_version": 1,
                    "observation_id": f"obs-{task['scenario_id']}",
                    "observed_at": now.isoformat(),
                    "fresh_until": (now + timedelta(hours=24)).isoformat(),
                    "scenario_id": task["scenario_id"],
                    "probe": {},
                    "subject": {},
                    "lane": {},
                    "egress": {},
                    "result": {
                        "availability": "available",
                        "class": "ok",
                        "eligible": True,
                    },
                    "confidence_stage": "single_observation",
                    "failure_layer": "none",
                    "latency_ms": 1,
                    "artifacts": [],
                },
            )
        store.complete_group(group["id"])
        assert api.runtime.due_scenarios("direct-de") == []
        assert api.runtime.enqueue_due_sweeps() == 0


def test_heartbeat_loop_records_samples_without_a_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_EVAL_HEARTBEAT_INTERVAL_SECONDS", "3600")
    with client(tmp_path, monkeypatch, heartbeat=True) as test_client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if api.runtime.store.heartbeat_stats("direct-de")["samples"]:
                break
            time.sleep(0.05)
        stats = api.runtime.store.heartbeat_stats("direct-de")
        assert stats["samples"] >= 1
        assert stats["ok_ratio"] == 1.0
        assert stats["latest"]["warp"] == "on"
        # Heartbeats are lane facts and must not create scenario observations.
        assert test_client.get("/v1/observations/latest", headers=auth()).json() == []


def test_unknown_lane_and_scenario_are_rejected(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as test_client:
        assert (
            test_client.post(
                "/v1/run-groups",
                headers=auth(),
                json={"lane_ids": ["unknown"], "scenario_ids": ["youtube"]},
            ).status_code
            == 422
        )
        assert (
            test_client.post(
                "/v1/run-groups",
                headers=auth(),
                json={"lane_ids": ["direct-de"], "scenario_ids": ["arbitrary-url"]},
            ).status_code
            == 422
        )


def test_warp_state_is_distinguishable_from_unreachability(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as test_client:
        store = api.runtime.store
        store.record_heartbeat(
            "direct-de", {"ok": False, "checks": {"trace": {"warp": "off"}}}
        )
        body = test_client.get("/metrics", headers=auth()).text
        # A lane serving traffic off-WARP must not look like an unreachable one.
        series = [
            line
            for line in body.splitlines()
            if line.startswith("cfwarp_probe_lane_warp_on{")
        ]
        assert len(series) == 1
        assert series[0].endswith(" 0")
        assert 'lane="direct-de"' in series[0]


def test_node_scenario_allowlist_restricts_scheduling_and_api(tmp_path, monkeypatch):
    # A memory-constrained node must not schedule browser scenarios at all,
    # rather than scheduling and failing them as tooling errors.
    monkeypatch.setenv("SERVICE_EVAL_SCENARIOS", "perf,youtube")
    with client(tmp_path, monkeypatch) as test_client:
        listed = {
            row["id"] for row in test_client.get("/v1/scenarios", headers=auth()).json()
        }
        assert listed == {"perf", "youtube"}
        assert sorted(api.runtime.due_scenarios("direct-de")) == ["perf", "youtube"]
        rejected = test_client.post(
            "/v1/run-groups",
            headers=auth(),
            json={"lane_ids": ["direct-de"], "scenario_ids": ["chatgpt"]},
        )
        assert rejected.status_code == 422
        assert "not enabled on this node" in rejected.json()["detail"]


def test_unknown_scenario_in_the_allowlist_is_rejected_at_startup(tmp_path):
    import pytest

    from cfwarp_service_eval.api import parse_scenarios

    assert set(parse_scenarios(None)) == set(api.SCENARIOS)
    with pytest.raises(ValueError, match="unknown scenario IDs"):
        parse_scenarios("perf,not-a-scenario")
