import json

import httpx

from cfwarp_service_eval.reporter import (
    LinearClient,
    ReporterStore,
    health_for,
    observation_availability,
    render_daily,
    render_weekly,
)


def report(observer_up=True, mismatch=0, warp_off=0):
    return {
        "schema_version": 1,
        "generated_at": "2026-08-04T01:00:00+00:00",
        "sources": [
            {
                "node_id": "proxy-host-1",
                "deployment_origin": "cfwarp-pro",
                "observer_build": "sha256:test",
                "observation_schema": 2,
                "evaluator_builds": ["eval-test"],
                "config_generations": ["generation-1"],
            }
        ],
        "platform_slo": {
            "nodes": [
                {
                    "node_id": "proxy-host-1",
                    "deployment_origin": "cfwarp-pro",
                    "observer_up": observer_up,
                    "hard_warp_off": warp_off,
                    "deployment_inventory_mismatch": mismatch,
                    "active_lane_count": 1,
                    "expected_cells": 2,
                    "fresh_cells": 2,
                    "telemetry_export_age_seconds": 60,
                    "last_sweep_age_seconds": 60,
                }
            ]
        },
        "scenarios": {
            "youtube.anonymous_public_video": {
                "available": 1,
                "unavailable": 0,
                "unknown": 0,
            }
        },
        "egresses": [],
        "remediation": [],
    }


def test_health_policy_fails_closed_and_migration_stays_at_risk():
    assert health_for(report(observer_up=False), [], [], True) == "offTrack"
    assert health_for(report(mismatch=1), [], [], True) == "offTrack"
    assert health_for(report(warp_off=1), [], [], True) == "offTrack"
    missing_telemetry = report()
    missing_telemetry["platform_slo"]["nodes"][0]["telemetry_export_age_seconds"] = None
    assert health_for(missing_telemetry, [], [], True) == "offTrack"
    assert health_for(report(), [], [], False) == "atRisk"
    assert health_for(report(), [], [{"name": "warning"}], True) == "atRisk"
    assert health_for(report(), [], [], True) == "onTrack"


def test_daily_body_states_canonical_boundary():
    body = render_daily(report(), "onTrack", [], [])
    assert "1 available / 0 unavailable / 0 unknown" in body
    assert "SQLite remains canonical" in body


def test_scenario_projection_counts_stale_and_ineligible_as_unknown():
    now = "2026-08-04T01:00:00+00:00"
    observation = {
        "schema_version": 2,
        "fresh_until": "2026-08-04T02:00:00+00:00",
        "result": {"availability": "available", "eligible": True},
    }
    assert observation_availability(observation, now) == "available"
    observation["result"]["eligible"] = False
    assert observation_availability(observation, now) == "unknown"
    observation["result"]["eligible"] = True
    observation["fresh_until"] = "2026-08-04T00:59:59+00:00"
    assert observation_availability(observation, now) == "unknown"


def test_weekly_body_contains_exact_week_over_week_deltas():
    before = report()
    latest = report()
    latest["generated_at"] = "2026-08-11T01:00:00+00:00"
    latest["scenarios"]["youtube.anonymous_public_video"] = {
        "available": 0,
        "unavailable": 0,
        "unknown": 1,
    }
    body = render_weekly([before, latest], latest, "atRisk")
    assert "available -1" in body
    assert "unknown +1" in body


def test_reporter_store_retains_machine_report(tmp_path):
    store = ReporterStore(tmp_path / "reporter.sqlite3")
    store.save("onTrack", report())
    assert store.rolling(14) == [report()]


def test_linear_status_update_uses_project_update_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "projectUpdateCreate": {
                        "success": True,
                        "projectUpdate": {"id": "update-1"},
                    }
                }
            },
        )

    transport = httpx.Client(
        base_url="https://api.linear.app",
        transport=httpx.MockTransport(handler),
    )
    client = LinearClient("x" * 40, transport)
    assert client.status_update("project-1", "body", "atRisk") == "update-1"
    assert captured["variables"]["input"] == {
        "projectId": "project-1",
        "body": "body",
        "health": "atRisk",
    }


def test_linear_incident_requires_explicit_call_and_stays_linked_to_project():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {"id": "id", "identifier": "WARP-99"},
                    }
                }
            },
        )

    transport = httpx.Client(
        base_url="https://api.linear.app",
        transport=httpx.MockTransport(handler),
    )
    client = LinearClient("x" * 40, transport)
    identifier = client.classified_incident(
        "team-1",
        "project-1",
        {"title": "Classified incident", "description": "Evidence", "priority": 1},
    )
    assert identifier == "WARP-99"
    assert captured["variables"]["input"]["projectId"] == "project-1"
