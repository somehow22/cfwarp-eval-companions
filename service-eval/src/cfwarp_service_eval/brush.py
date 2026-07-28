from __future__ import annotations

import argparse
import asyncio
import json
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .capabilities import memory_limit_mib, require_scenario_capability
from .contracts import scenario_definitions
from .config import Lane, load_lanes
from .runner import ProbeRunner


class BrushError(RuntimeError):
    pass


class Control(Protocol):
    async def prepare(self, strategy: str, lease_seconds: int) -> dict[str, Any]: ...

    async def commit(self, trial_id: str) -> dict[str, Any]: ...

    async def rollback(self, trial_id: str) -> dict[str, Any]: ...


class Evaluator(Protocol):
    async def evaluate(
        self, run_id: str, lane: Lane, scenario_id: str
    ) -> dict[str, Any]: ...


class UnixControlClient:
    def __init__(self, socket_path: Path, timeout_seconds: int = 30):
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def prepare(self, strategy: str, lease_seconds: int) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._call,
            "egress.prepare",
            {"strategy": strategy, "lease_seconds": lease_seconds},
        )

    async def commit(self, trial_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._call, "egress.commit", {"trial_id": trial_id}
        )

    async def rollback(self, trial_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._call, "egress.rollback", {"trial_id": trial_id}
        )

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request = {
            "jsonrpc": "2.0",
            "id": "cfwarp-brush",
            "method": method,
            "params": {"schema_version": 1, **params},
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.socket_path))
            connection.sendall((json.dumps(request) + "\n").encode())
            reader = connection.makefile("rb")
            raw = reader.readline(1024 * 1024)
        if not raw:
            raise BrushError("cfwarp core returned an empty response")
        response = json.loads(raw)
        if response.get("error"):
            message = response["error"].get("message") or "cfwarp control error"
            raise BrushError(str(message))
        result = response.get("result")
        if not isinstance(result, dict) or not result.get("ok"):
            raise BrushError(
                str((result or {}).get("message") or "cfwarp control failed")
            )
        trial = result.get("trial")
        if not isinstance(trial, dict):
            raise BrushError("cfwarp control response omitted trial state")
        return trial


class ScenarioEvaluator:
    def __init__(self, runner: ProbeRunner):
        self.runner = runner

    async def evaluate(
        self, run_id: str, lane: Lane, scenario_id: str
    ) -> dict[str, Any]:
        try:
            return await self.runner.run(run_id, lane, scenario_id)
        except Exception as error:
            now = datetime.now(timezone.utc)
            definition = scenario_definitions()[scenario_id]
            return {
                "schema_version": 1,
                "observation_id": str(uuid.uuid4()),
                "observed_at": now.isoformat(),
                "fresh_until": (
                    now + timedelta(seconds=definition["freshness_seconds"])
                ).isoformat(),
                "scenario_id": definition["scenario_id"],
                "probe": {
                    "name": "brush-runner",
                    "version": "1",
                    "execution": "local",
                },
                "subject": lane.public(),
                "lane": {
                    "composition": lane.composition,
                    "transport": lane.transport,
                    "substrate_profile": lane.substrate_profile,
                    "requested_region": lane.requested_region,
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
                "error_type": type(error).__name__,
            }


@dataclass(frozen=True)
class BrushRequest:
    lane: Lane
    scenario_id: str
    attempts: int = 3
    strategy: str = "auto"
    lease_seconds: int = 900


class BrushRunner:
    def __init__(self, control: Control, evaluator: Evaluator):
        self.control = control
        self.evaluator = evaluator

    async def run(self, request: BrushRequest) -> dict[str, Any]:
        definition = ensure_brushable(request.scenario_id)
        if request.attempts < 1 or request.attempts > 10:
            raise BrushError("attempts must be between 1 and 10")
        if request.strategy not in {"auto", "reconnect", "refresh_identity"}:
            raise BrushError("strategy must be auto, reconnect, or refresh_identity")
        if request.lease_seconds < 30 or request.lease_seconds > 1800:
            raise BrushError("lease seconds must be between 30 and 1800")

        started = datetime.now(timezone.utc)
        run_id = "brush-" + uuid.uuid4().hex[:16]
        result: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "lane_id": request.lane.id,
            "scenario_id": definition["scenario_id"],
            "strategy": request.strategy,
            "attempts_requested": request.attempts,
            "attempts": [],
            "started_at": started.isoformat(),
            "outcome": "failed",
        }

        baseline = await self.evaluator.evaluate(
            f"{run_id}/baseline", request.lane, request.scenario_id
        )
        result["baseline"] = baseline
        baseline_state = observation_state(baseline, definition["scenario_id"])
        if baseline_state == "available":
            result["outcome"] = "already_satisfied"
            return finish(result, started)
        if baseline_state == "unknown":
            result["outcome"] = "unknown"
            result["failure_layer"] = "service-probe"
            return finish(result, started)

        for number in range(1, request.attempts + 1):
            strategy = attempt_strategy(request.strategy, number)
            attempt: dict[str, Any] = {
                "number": number,
                "strategy": strategy,
                "outcome": "failed",
                "evaluator_retried": False,
            }
            trial: dict[str, Any] | None = None
            try:
                trial = await self.control.prepare(strategy, request.lease_seconds)
                attempt["trial_id"] = trial.get("trial_id")
                attempt["public_ip_changed"] = bool(trial.get("public_ip_changed"))
                attempt["before_public_ip_hash"] = trial.get("before_public_ip_hash")
                attempt["candidate_public_ip_hash"] = trial.get(
                    "candidate_public_ip_hash"
                )
                if not attempt["public_ip_changed"]:
                    await self.control.rollback(str(trial["trial_id"]))
                    attempt["rollback"] = "succeeded"
                    attempt["failure_layer"] = "warp-core"
                    attempt["reason"] = "listener-facing public IP did not change"
                    result["attempts"].append(attempt)
                    continue

                observation = await self.evaluator.evaluate(
                    f"{run_id}/attempt-{number}",
                    request.lane,
                    request.scenario_id,
                )
                state = observation_state(observation, definition["scenario_id"])
                if state == "unknown":
                    attempt["evaluator_retried"] = True
                    observation = await self.evaluator.evaluate(
                        f"{run_id}/attempt-{number}-retry",
                        request.lane,
                        request.scenario_id,
                    )
                    state = observation_state(observation, definition["scenario_id"])
                attempt["observation"] = observation
                if state == "available":
                    committed = await self.control.commit(str(trial["trial_id"]))
                    attempt["outcome"] = "succeeded"
                    attempt["commit"] = committed.get("status", "succeeded")
                    result["attempts"].append(attempt)
                    result["outcome"] = "succeeded"
                    result["attempts_used"] = number
                    return finish(result, started)

                await self.control.rollback(str(trial["trial_id"]))
                attempt["rollback"] = "succeeded"
                if state == "unknown":
                    attempt["outcome"] = "unknown"
                    attempt["failure_layer"] = "service-probe"
                    result["attempts"].append(attempt)
                    result["outcome"] = "unknown"
                    result["failure_layer"] = "service-probe"
                    result["attempts_used"] = number
                    return finish(result, started)
                attempt["failure_layer"] = "service-probe"
                result["attempts"].append(attempt)
            except Exception as error:
                if trial is not None and trial.get("trial_id"):
                    try:
                        await self.control.rollback(str(trial["trial_id"]))
                        attempt["rollback"] = "succeeded"
                    except Exception:
                        attempt["rollback"] = "failed"
                attempt["failure_layer"] = "route-runtime"
                attempt["error_type"] = type(error).__name__
                result["attempts"].append(attempt)
                result["failure_layer"] = "route-runtime"
                result["attempts_used"] = number
                return finish(result, started)

        result["attempts_used"] = len(result["attempts"])
        result["failure_layer"] = (
            result["attempts"][-1].get("failure_layer", "unknown")
            if result["attempts"]
            else "unknown"
        )
        return finish(result, started)


def ensure_brushable(scenario_id: str) -> dict[str, Any]:
    definition = scenario_definitions().get(scenario_id)
    if definition is None:
        raise BrushError(f"unknown scenario {scenario_id}")
    if definition["remediation_role"] != "gate":
        raise BrushError(f"scenario {scenario_id} is observation-only")
    return definition


def observation_state(observation: dict[str, Any], expected_scenario: str) -> str:
    if observation.get("schema_version") != 1:
        return "unknown"
    if observation.get("scenario_id") != expected_scenario:
        return "unknown"
    try:
        fresh_until = datetime.fromisoformat(
            str(observation["fresh_until"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError):
        return "unknown"
    if fresh_until <= datetime.now(timezone.utc):
        return "unknown"
    result = observation.get("result")
    if not isinstance(result, dict) or result.get("eligible") is not True:
        return "unknown"
    availability = result.get("availability")
    if availability in {"available", "unavailable"}:
        return str(availability)
    return "unknown"


def attempt_strategy(strategy: str, number: int) -> str:
    if strategy != "auto":
        return strategy
    return "reconnect" if number == 1 else "refresh_identity"


def finish(result: dict[str, Any], started: datetime) -> dict[str, Any]:
    finished = datetime.now(timezone.utc)
    result["finished_at"] = finished.isoformat()
    result["elapsed_ms"] = round((finished - started).total_seconds() * 1000)
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="cfwarp-brush",
        description="Run bounded service-aware WARP egress brushing.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--lanes-file", type=Path, required=True)
    run.add_argument("--lane", required=True)
    run.add_argument("--scenario", required=True)
    run.add_argument("--socket", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--attempts", type=int, default=3)
    run.add_argument(
        "--strategy",
        choices=["auto", "reconnect", "refresh_identity"],
        default="auto",
    )
    run.add_argument("--lease-seconds", type=int, default=900)
    run.add_argument(
        "--browser-execution",
        choices=["disabled", "local", "agentcore"],
        default="disabled",
    )
    return root


def main() -> int:
    args = parser().parse_args()
    lanes = load_lanes(args.lanes_file)
    if args.lane not in lanes:
        raise SystemExit(f"unknown lane {args.lane}")
    definition = ensure_brushable(args.scenario)
    try:
        require_scenario_capability(
            args.scenario,
            args.browser_execution,
            memory_limit_mib(),
        )
    except ValueError as error:
        raise SystemExit(
            f"scenario {args.scenario} cannot run on this brush runtime: {error}"
        ) from error
    args.output.mkdir(parents=True, exist_ok=True)
    probe_runner = ProbeRunner(
        args.output / "artifacts",
        deadline_seconds=int(definition["deadline_seconds"]),
        browser_execution=args.browser_execution,
    )
    runner = BrushRunner(
        UnixControlClient(args.socket),
        ScenarioEvaluator(probe_runner),
    )
    started = time.monotonic()
    result = asyncio.run(
        runner.run(
            BrushRequest(
                lane=lanes[args.lane],
                scenario_id=args.scenario,
                attempts=args.attempts,
                strategy=args.strategy,
                lease_seconds=args.lease_seconds,
            )
        )
    )
    result["caller_elapsed_ms"] = round((time.monotonic() - started) * 1000)
    summary = args.output / "brush-summary.json"
    summary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["outcome"] in {"succeeded", "already_satisfied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
