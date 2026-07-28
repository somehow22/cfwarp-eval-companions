from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cfwarp_service_eval.brush import (
    BrushError,
    BrushRequest,
    BrushRunner,
    ScenarioEvaluator,
    ensure_brushable,
)
from cfwarp_service_eval.capabilities import require_scenario_capability
from cfwarp_service_eval.config import Lane


def lane() -> Lane:
    return Lane(
        id="fv-ro",
        proxy="socks5h://proxy-host-1:1080",
        instance_id="cfwarp-fv-ro",
        node_id="proxy-host-1",
        composition="warp-over-fv",
        transport="wireguard",
        substrate_profile="fv-ro",
        requested_region="RO",
        image_identity="example@sha256:" + "a" * 64,
        config_digest="sha256:" + "b" * 64,
    )


def observation(
    availability: str,
    eligible: bool = True,
    scenario_id: str = "youtube.anonymous_public_video",
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "observation_id": "example",
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(hours=1)).isoformat(),
        "scenario_id": scenario_id,
        "result": {"availability": availability, "class": "test", "eligible": eligible},
    }


class FakeControl:
    def __init__(self, trials: list[dict]):
        self.trials = list(trials)
        self.prepared: list[str] = []
        self.committed: list[str] = []
        self.rolled_back: list[str] = []

    async def prepare(self, strategy: str, lease_seconds: int) -> dict:
        self.prepared.append(strategy)
        return self.trials.pop(0)

    async def commit(self, trial_id: str) -> dict:
        self.committed.append(trial_id)
        return {"trial_id": trial_id, "status": "committed"}

    async def rollback(self, trial_id: str) -> dict:
        self.rolled_back.append(trial_id)
        return {"trial_id": trial_id, "status": "rolled_back"}


class FakeEvaluator:
    def __init__(self, observations: list[dict]):
        self.observations = list(observations)
        self.calls: list[str] = []

    async def evaluate(self, run_id: str, lane: Lane, scenario_id: str) -> dict:
        self.calls.append(f"{run_id}:{scenario_id}")
        return self.observations.pop(0)


class FailingProbeRunner:
    async def run(self, run_id: str, lane: Lane, scenario_id: str) -> dict:
        raise RuntimeError("failed through socks5h://secret.invalid:1080 safely")


def run(
    control: FakeControl,
    evaluator: FakeEvaluator,
    attempts: int = 3,
    force_change: bool = False,
) -> dict:
    return asyncio.run(
        BrushRunner(control, evaluator).run(
            BrushRequest(
                lane(),
                "youtube",
                attempts=attempts,
                force_change=force_change,
            )
        )
    )


def trial(number: int, changed: bool) -> dict:
    return {
        "trial_id": f"trial-{number}",
        "public_ip_changed": changed,
        "before_public_ip_hash": "before",
        "candidate_public_ip_hash": f"candidate-{number}",
    }


def perf_observation(availability: str = "available") -> dict:
    return observation(
        availability,
        eligible=False,
        scenario_id="perf.throughput_sample",
    )


def test_baseline_pass_does_not_mutate_egress():
    control = FakeControl([])
    result = run(
        control,
        FakeEvaluator([observation("available"), perf_observation()]),
    )
    assert result["outcome"] == "already_satisfied"
    assert control.prepared == []
    assert result["performance_before"]["scenario_id"] == "perf.throughput_sample"


def test_forced_change_rotates_and_revalidates_a_passing_baseline():
    control = FakeControl([trial(1, True)])
    evaluator = FakeEvaluator(
        [
            observation("available"),
            perf_observation(),
            observation("available"),
            perf_observation(),
        ]
    )
    result = run(control, evaluator, force_change=True)
    assert result["force_change"] is True
    assert result["outcome"] == "succeeded"
    assert control.prepared == ["reconnect"]
    assert control.committed == ["trial-1"]


def test_baseline_unknown_does_not_mutate_egress():
    control = FakeControl([])
    result = run(
        control,
        FakeEvaluator([observation("unknown", False), perf_observation()]),
    )
    assert result["outcome"] == "unknown"
    assert control.prepared == []


def test_tooling_observation_has_bounded_redacted_error():
    result = asyncio.run(
        ScenarioEvaluator(FailingProbeRunner()).evaluate("run", lane(), "youtube")
    )
    assert result["result"]["availability"] == "unknown"
    assert result["error_type"] == "RuntimeError"
    assert result["error_message"] == "failed through safely"


def test_unchanged_ip_rolls_back_without_expensive_probe():
    control = FakeControl([trial(1, False)])
    evaluator = FakeEvaluator([observation("unavailable"), perf_observation()])
    result = run(control, evaluator, attempts=1)
    assert result["outcome"] == "failed"
    assert control.rolled_back == ["trial-1"]
    assert len(evaluator.calls) == 2


def test_service_pass_commits_changed_candidate():
    control = FakeControl([trial(1, True)])
    evaluator = FakeEvaluator(
        [
            observation("unavailable"),
            perf_observation(),
            observation("available"),
            perf_observation(),
        ]
    )
    result = run(control, evaluator)
    assert result["outcome"] == "succeeded"
    assert control.committed == ["trial-1"]
    assert control.rolled_back == []
    assert (
        result["attempts"][0]["performance_after"]["scenario_id"]
        == "perf.throughput_sample"
    )


def test_performance_unknown_never_blocks_service_acceptance():
    control = FakeControl([trial(1, True)])
    evaluator = FakeEvaluator(
        [
            observation("unavailable"),
            perf_observation("unknown"),
            observation("available"),
            perf_observation("unknown"),
        ]
    )
    result = run(control, evaluator)
    assert result["outcome"] == "succeeded"
    assert control.committed == ["trial-1"]
    assert result["performance_before"]["result"]["availability"] == "unknown"
    assert (
        result["attempts"][0]["performance_after"]["result"]["availability"]
        == "unknown"
    )


def test_service_failure_rolls_back_then_tries_fresh_identity():
    control = FakeControl([trial(1, True), trial(2, True)])
    evaluator = FakeEvaluator(
        [
            observation("unavailable"),
            perf_observation(),
            observation("unavailable"),
            perf_observation(),
            observation("available"),
            perf_observation(),
        ]
    )
    result = run(control, evaluator)
    assert result["outcome"] == "succeeded"
    assert control.prepared == ["reconnect", "refresh_identity"]
    assert control.rolled_back == ["trial-1"]
    assert control.committed == ["trial-2"]


def test_tooling_failure_retries_same_candidate_once_then_stops_unknown():
    control = FakeControl([trial(1, True)])
    evaluator = FakeEvaluator(
        [
            observation("unavailable"),
            perf_observation(),
            observation("unknown", False),
            observation("unknown", False),
            perf_observation("unknown"),
        ]
    )
    result = run(control, evaluator)
    assert result["outcome"] == "unknown"
    assert result["attempts"][0]["evaluator_retried"] is True
    assert control.rolled_back == ["trial-1"]
    assert len(control.prepared) == 1


def test_expired_observation_fails_closed():
    expired = observation("available")
    expired["fresh_until"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    control = FakeControl([])
    result = run(control, FakeEvaluator([expired, perf_observation()]))
    assert result["outcome"] == "unknown"
    assert control.prepared == []


def test_performance_is_observation_only():
    with pytest.raises(BrushError, match="observation-only"):
        ensure_brushable("perf")


def test_browser_runtime_rejected_before_control_is_constructed():
    with pytest.raises(ValueError, match="optional and disabled"):
        require_scenario_capability("gemini", "disabled", 4096)
    with pytest.raises(ValueError, match="requires 768 MiB"):
        require_scenario_capability("gemini", "local", 512)
    capability = require_scenario_capability("gemini", "agentcore", 128)
    assert capability["enabled"] is True
    assert capability["execution_target"] == "agentcore"
