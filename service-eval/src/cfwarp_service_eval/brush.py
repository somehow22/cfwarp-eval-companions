from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sqlite3
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
from .provenance import observation_v2


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
            observation = {
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
                "error_message": redact_error(error),
            }
            return observation_v2(
                observation, lane.public(), scenario_id, build="brush-runner-v2"
            )


class RemediationJournal:
    """Durable baseline, retry, mutation, and cooldown ledger."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS remediation_runs (
                  run_id TEXT PRIMARY KEY,lane_id TEXT NOT NULL,
                  scenario_id TEXT NOT NULL,status TEXT NOT NULL,
                  started_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                  cooldown_until TEXT,payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_remediation_lane
                  ON remediation_runs(lane_id,started_at DESC);
                CREATE TABLE IF NOT EXISTS remediation_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,
                  recorded_at TEXT NOT NULL,event_type TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES remediation_runs(run_id)
                );
                """
            )

    def assert_available(self, lane_id: str) -> None:
        row = self.db.execute(
            """
            SELECT cooldown_until,status FROM remediation_runs
            WHERE lane_id=? ORDER BY started_at DESC LIMIT 1
            """,
            (lane_id,),
        ).fetchone()
        if row is None:
            return
        cooldown_until, status = row
        if status == "running":
            raise BrushError("another remediation is already running for this lane")
        if cooldown_until and datetime.fromisoformat(cooldown_until) > datetime.now(
            timezone.utc
        ):
            raise BrushError(f"lane remediation is cooling down until {cooldown_until}")

    def begin(self, result: dict[str, Any]) -> None:
        self.assert_available(str(result["lane_id"]))
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute(
                """
                INSERT INTO remediation_runs(
                  run_id,lane_id,scenario_id,status,started_at,updated_at,payload
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    result["run_id"],
                    result["lane_id"],
                    result["scenario_id"],
                    "running",
                    result["started_at"],
                    now,
                    json.dumps(result, separators=(",", ":")),
                ),
            )

    def event(self, run_id: str, event_type: str, payload: Any) -> None:
        with self.db:
            self.db.execute(
                """
                INSERT INTO remediation_events(run_id,recorded_at,event_type,payload)
                VALUES(?,?,?,?)
                """,
                (
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                    event_type,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )

    def save(self, result: dict[str, Any]) -> None:
        status = "running" if "finished_at" not in result else str(result["outcome"])
        with self.db:
            self.db.execute(
                """
                UPDATE remediation_runs SET status=?,updated_at=?,cooldown_until=?,payload=?
                WHERE run_id=?
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    result.get("cooldown_until"),
                    json.dumps(result, separators=(",", ":")),
                    result["run_id"],
                ),
            )


@dataclass(frozen=True)
class BrushRequest:
    lane: Lane
    scenario_id: str
    attempts: int = 3
    strategy: str = "auto"
    lease_seconds: int = 900
    force_change: bool = False
    deadline_seconds: int = 900
    cooldown_seconds: int = 1800


class BrushRunner:
    def __init__(
        self,
        control: Control,
        evaluator: Evaluator,
        journal: RemediationJournal | None = None,
    ):
        self.control = control
        self.evaluator = evaluator
        self.journal = journal

    def event(self, run_id: str, event_type: str, payload: Any) -> None:
        if self.journal:
            self.journal.event(run_id, event_type, payload)

    def done(self, result: dict[str, Any], started: datetime) -> dict[str, Any]:
        completed = finish(result, started)
        if self.journal:
            self.journal.save(completed)
        return completed

    async def rollback(self, trial_id: str) -> dict[str, Any]:
        try:
            return await self.control.rollback(trial_id)
        except Exception:
            # A restored listener can be briefly unavailable while the cfwarp
            # runtime finishes settling. The core rollback is transactional and
            # safe to repeat for the same active trial, so retry once before
            # classifying the operation as a route-runtime failure.
            return await self.control.rollback(trial_id)

    async def run(self, request: BrushRequest) -> dict[str, Any]:
        definition = ensure_brushable(request.scenario_id)
        if request.attempts < 1 or request.attempts > 3:
            raise BrushError("attempts must be between 1 and 3")
        if request.strategy not in {"auto", "reconnect", "refresh_identity"}:
            raise BrushError("strategy must be auto, reconnect, or refresh_identity")
        if request.lease_seconds < 30 or request.lease_seconds > 1800:
            raise BrushError("lease seconds must be between 30 and 1800")
        if request.deadline_seconds < 60 or request.deadline_seconds > 900:
            raise BrushError("deadline seconds must be between 60 and 900")
        if request.cooldown_seconds != 1800:
            raise BrushError("cooldown seconds must be 1800")

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
            "force_change": request.force_change,
            "started_at": started.isoformat(),
            "outcome": "failed",
        }
        if self.journal:
            self.journal.begin(result)

        deadline = time.monotonic() + request.deadline_seconds

        def ensure_deadline() -> None:
            if time.monotonic() >= deadline:
                raise BrushError("remediation deadline exhausted")

        baseline = await self.evaluator.evaluate(
            f"{run_id}/baseline", request.lane, request.scenario_id
        )
        result["baseline"] = baseline
        self.event(run_id, "baseline", baseline)
        result["performance_before"] = await self.evaluator.evaluate(
            f"{run_id}/perf-before", request.lane, "perf"
        )
        self.event(run_id, "performance_before", result["performance_before"])
        baseline_state = observation_state(baseline, definition["scenario_id"])
        if baseline_state == "available" and not request.force_change:
            result["outcome"] = "already_satisfied"
            return self.done(result, started)
        if baseline_state == "unknown":
            result["outcome"] = "unknown"
            result["failure_layer"] = "service-probe"
            return self.done(result, started)

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
                ensure_deadline()
                trial = await self.control.prepare(strategy, request.lease_seconds)
                result["cooldown_until"] = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=request.cooldown_seconds)
                ).isoformat()
                self.event(run_id, "deployment_mutation", trial)
                if self.journal:
                    self.journal.save(result)
                attempt["trial_id"] = trial.get("trial_id")
                attempt["public_ip_changed"] = bool(trial.get("public_ip_changed"))
                attempt["before_public_ip_hash"] = trial.get("before_public_ip_hash")
                attempt["candidate_public_ip_hash"] = trial.get(
                    "candidate_public_ip_hash"
                )
                if not attempt["public_ip_changed"]:
                    await self.rollback(str(trial["trial_id"]))
                    self.event(run_id, "rollback", {"trial_id": trial["trial_id"]})
                    attempt["rollback"] = "succeeded"
                    attempt["failure_layer"] = "warp-core"
                    attempt["reason"] = "listener-facing public IP did not change"
                    result["attempts"].append(attempt)
                    continue

                ensure_deadline()
                observation = await self.evaluator.evaluate(
                    f"{run_id}/attempt-{number}",
                    request.lane,
                    request.scenario_id,
                )
                self.event(run_id, "candidate_observation", observation)
                state = observation_state(observation, definition["scenario_id"])
                if state == "unknown":
                    attempt["evaluator_retried"] = True
                    ensure_deadline()
                    observation = await self.evaluator.evaluate(
                        f"{run_id}/attempt-{number}-retry",
                        request.lane,
                        request.scenario_id,
                    )
                    self.event(run_id, "evaluator_retry", observation)
                    state = observation_state(observation, definition["scenario_id"])
                attempt["observation"] = observation
                attempt["performance_after"] = await self.evaluator.evaluate(
                    f"{run_id}/attempt-{number}-perf", request.lane, "perf"
                )
                self.event(run_id, "performance_after", attempt["performance_after"])
                if state == "available":
                    committed = await self.control.commit(str(trial["trial_id"]))
                    self.event(run_id, "commit", committed)
                    attempt["outcome"] = "succeeded"
                    attempt["commit"] = committed.get("status", "succeeded")
                    result["attempts"].append(attempt)
                    result["outcome"] = "succeeded"
                    result["attempts_used"] = number
                    return self.done(result, started)

                await self.rollback(str(trial["trial_id"]))
                self.event(run_id, "rollback", {"trial_id": trial["trial_id"]})
                attempt["rollback"] = "succeeded"
                if state == "unknown":
                    attempt["outcome"] = "unknown"
                    attempt["failure_layer"] = "service-probe"
                    result["attempts"].append(attempt)
                    result["outcome"] = "unknown"
                    result["failure_layer"] = "service-probe"
                    result["attempts_used"] = number
                    return self.done(result, started)
                attempt["failure_layer"] = "service-probe"
                result["attempts"].append(attempt)
            except Exception as error:
                if trial is not None and trial.get("trial_id"):
                    try:
                        await self.rollback(str(trial["trial_id"]))
                        self.event(run_id, "rollback", {"trial_id": trial["trial_id"]})
                        attempt["rollback"] = "succeeded"
                    except Exception:
                        attempt["rollback"] = "failed"
                attempt["failure_layer"] = "route-runtime"
                attempt["error_type"] = type(error).__name__
                result["attempts"].append(attempt)
                result["failure_layer"] = "route-runtime"
                result["attempts_used"] = number
                return self.done(result, started)

        result["attempts_used"] = len(result["attempts"])
        result["failure_layer"] = (
            result["attempts"][-1].get("failure_layer", "unknown")
            if result["attempts"]
            else "unknown"
        )
        return self.done(result, started)


def ensure_brushable(scenario_id: str) -> dict[str, Any]:
    definition = scenario_definitions().get(scenario_id)
    if definition is None:
        raise BrushError(f"unknown scenario {scenario_id}")
    if definition["remediation_role"] != "gate":
        raise BrushError(f"scenario {scenario_id} is observation-only")
    return definition


def observation_state(observation: dict[str, Any], expected_scenario: str) -> str:
    if observation.get("schema_version") not in {1, 2}:
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


def redact_error(error: Exception) -> str:
    return " ".join(part for part in str(error).split() if "://" not in part)[:300]


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
    run.add_argument("--deadline-seconds", type=int, default=900)
    run.add_argument("--cooldown-seconds", type=int, default=1800)
    run.add_argument(
        "--state-db",
        type=Path,
        default=Path("/var/lib/cfwarp-brush/remediation.sqlite3"),
    )
    run.add_argument(
        "--force-change",
        action="store_true",
        help="require a changed WARP IP even when the baseline service is available",
    )
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
        RemediationJournal(args.state_db),
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
                force_change=args.force_change,
                deadline_seconds=args.deadline_seconds,
                cooldown_seconds=args.cooldown_seconds,
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
