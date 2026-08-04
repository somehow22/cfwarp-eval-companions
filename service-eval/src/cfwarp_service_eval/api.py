from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import (
    memory_limit_mib,
    parse_browser_execution,
    parse_scenarios,
    resolve_scenario_capabilities,
)
from .config import SCENARIOS, load_lanes
from .provenance import evaluator_build
from .runner import ProbeRunner
from .store import LeaseConflict, QueueFull, Store, parse_time, tree_size

__all__ = ["parse_scenarios"]


class RunGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Lanes are a fixed server-side allowlist, so a larger batch introduces no
    # new reachable target. Flood protection is the queue depth cap instead.
    lane_ids: list[str] = Field(min_length=1, max_length=16)
    scenario_ids: list[str] = Field(min_length=1, max_length=len(SCENARIOS))

    @field_validator("lane_ids", "scenario_ids")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("IDs must be unique")
        return value


class WorkerHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=128)
    worker_class: str = Field(pattern="^(light|perf|browser)$")
    node_id: str = Field(min_length=1, max_length=128)
    evaluator_build: str = Field(min_length=1, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClaimRequest(WorkerHeartbeatRequest):
    lease_seconds: int = Field(default=240, ge=30, le=900)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=1, max_length=128)
    observation: dict[str, Any]


class FailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=1, max_length=128)
    error: str = Field(min_length=1, max_length=300)


class HeartbeatResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lane_id: str = Field(min_length=1, max_length=32)
    result: dict[str, Any]


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def read_token(path: Path, purpose: str) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError(f"{purpose} bearer token must be at least 32 characters")
    return token


def env_flag(name: str, default: bool = True) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() not in {
        "0",
        "false",
        "no",
    }


# Sweep cadence multipliers by tier. A lane nobody can reach should not consume
# the same expensive browser-scenario budget as a healthy one.
TIER_CADENCE = {
    "preferred": 1.0,
    "usable": 1.0,
    "unknown": 0.5,
    "degraded": 0.5,
    "quarantined": 4.0,
}


class Runtime:
    def __init__(self) -> None:
        self.state_root = Path(
            os.environ.get("SERVICE_EVAL_STATE_ROOT", "/var/lib/cfwarp-service-eval")
        )
        self.lanes = load_lanes(
            Path(
                os.environ.get(
                    "SERVICE_EVAL_LANES_FILE", "/etc/cfwarp-service-eval/lanes.json"
                )
            )
        )
        self.token = read_token(
            Path(
                os.environ.get(
                    "SERVICE_EVAL_TOKEN_FILE", "/run/secrets/cfwarp-probe-api-token"
                )
            ),
            "API",
        )
        metrics_token_file = os.environ.get("SERVICE_EVAL_METRICS_TOKEN_FILE")
        self.metrics_token = (
            read_token(Path(metrics_token_file), "metrics")
            if metrics_token_file
            else self.token
        )
        worker_token_file = os.environ.get("SERVICE_EVAL_WORKER_TOKEN_FILE")
        self.worker_token = (
            read_token(Path(worker_token_file), "worker")
            if worker_token_file
            else self.token
        )
        self.browser_execution = parse_browser_execution(
            os.environ.get("SERVICE_EVAL_BROWSER_EXECUTION")
        )
        self.browser_min_memory_mib = env_int(
            "SERVICE_EVAL_BROWSER_MIN_MEMORY_MIB", 768
        )
        if self.browser_min_memory_mib <= 0:
            raise ValueError("SERVICE_EVAL_BROWSER_MIN_MEMORY_MIB must be positive")
        self.memory_limit_mib = memory_limit_mib()
        self.scenarios, self.scenario_capabilities = resolve_scenario_capabilities(
            os.environ.get("SERVICE_EVAL_SCENARIOS"),
            self.browser_execution,
            self.memory_limit_mib,
            self.browser_min_memory_mib,
        )
        self.store = Store(self.state_root / "queue.sqlite3")
        self.runner = ProbeRunner(
            self.state_root / "artifacts",
            env_int("SERVICE_EVAL_DEADLINE_SECONDS", 180),
            browser_execution=self.browser_execution,
        )
        self.heartbeat_interval = env_int("SERVICE_EVAL_HEARTBEAT_INTERVAL_SECONDS", 60)
        self.sweep_interval = env_int("SERVICE_EVAL_SWEEP_INTERVAL_SECONDS", 6 * 3600)
        self.startup_delay = env_int("SERVICE_EVAL_STARTUP_DELAY_SECONDS", 5)
        self.lane_chunk = env_int("SERVICE_EVAL_LANE_CHUNK", 5)
        self.heartbeat_enabled = env_flag("SERVICE_EVAL_HEARTBEAT_ENABLED")
        self.scheduler_enabled = env_flag("SERVICE_EVAL_SCHEDULER_ENABLED")
        self.embedded_worker_enabled = env_flag(
            "SERVICE_EVAL_EMBEDDED_WORKER_ENABLED", default=False
        )
        self.expected_lane_count = env_int(
            "SERVICE_EVAL_EXPECTED_LANE_COUNT", len(self.lanes)
        )
        self.observer_build = os.environ.get("CFWARP_OBSERVER_BUILD", evaluator_build())
        self.last_sweep_at: str | None = None
        self.telemetry_export_at: str | None = None
        self.wakeup = asyncio.Event()
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []
        self.background_failures: set[str] = set()

    async def start(self) -> None:
        self.store.recover(
            {lane_id: lane.public() for lane_id, lane in self.lanes.items()},
            SCENARIOS,
        )
        if self.embedded_worker_enabled:
            self.spawn(self.sweep_worker, "probe-sweep-worker")
        if self.heartbeat_enabled:
            self.spawn(self.heartbeat_loop, "probe-heartbeat")
        if self.scheduler_enabled:
            self.spawn(self.scheduler_loop, "probe-scheduler")
        self.wakeup.set()

    def spawn(self, target: Any, name: str) -> None:
        task = asyncio.create_task(target(), name=name)
        task.add_done_callback(self._record_background_failure)
        self.tasks.append(task)

    def _record_background_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            self.background_failures.add(task.get_name())

    async def close(self) -> None:
        self.stop.set()
        self.wakeup.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def sleep_or_stop(self, seconds: float) -> bool:
        """Sleep unless shutdown arrives first. Returns True if still running."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    async def heartbeat_loop(self) -> None:
        """Cheap liveness sampling, independent of the sweep worker.

        This must not sit behind a browser sweep: a lane going dark is the
        signal an operator needs soonest, and a full sweep can run for an hour.
        """
        if not await self.sleep_or_stop(self.startup_delay):
            return
        while not self.stop.is_set():
            for lane in list(self.lanes.values()):
                if self.stop.is_set():
                    return
                try:
                    result = await self.runner.preflight("heartbeat", lane)
                except Exception as error:
                    result = {"ok": False, "error": type(error).__name__}
                self.store.record_heartbeat(lane.id, result)
            if not await self.sleep_or_stop(self.heartbeat_interval):
                return

    def due_scenarios(self, lane_id: str) -> list[str]:
        """Scenarios whose newest observation is older than this lane's cadence."""
        tier = self.store.lane_tier(
            lane_id, requested_region=self.lanes[lane_id].requested_region
        )["tier"]
        deadline = datetime.now(timezone.utc) - timedelta(
            seconds=self.sweep_interval * TIER_CADENCE.get(tier, 1.0)
        )
        latest = self.store.latest_by_scenario(lane_id)
        due = []
        for scenario_id in self.lanes[lane_id].scenarios:
            if scenario_id not in self.scenarios:
                continue
            record = latest.get(scenario_id)
            if record is None:
                due.append(scenario_id)
                continue
            observed_at = record["payload"].get("observed_at")
            if not observed_at or parse_time(observed_at) <= deadline:
                due.append(scenario_id)
        return due

    async def scheduler_loop(self) -> None:
        """Keep evidence fresh without an operator issuing run groups by hand."""
        if not await self.sleep_or_stop(self.startup_delay):
            return
        while not self.stop.is_set():
            try:
                self.store.expire_leases(
                    {lane_id: lane.public() for lane_id, lane in self.lanes.items()},
                    SCENARIOS,
                )
                self.enqueue_due_sweeps()
                self.last_sweep_at = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass
            if not await self.sleep_or_stop(max(60, self.heartbeat_interval)):
                return

    def enqueue_due_sweeps(self) -> int:
        """Group lanes by their due scenario set, then chunk to the request cap."""
        by_scenarios: dict[tuple[str, ...], list[str]] = {}
        pending = self.store.pending_cells()
        for lane_id in self.lanes:
            due = tuple(
                scenario_id
                for scenario_id in self.due_scenarios(lane_id)
                if (lane_id, scenario_id) not in pending
            )
            if due:
                by_scenarios.setdefault(due, []).append(lane_id)
        created = 0
        for scenario_ids, lane_ids in by_scenarios.items():
            for index in range(0, len(lane_ids), self.lane_chunk):
                chunk = lane_ids[index : index + self.lane_chunk]
                try:
                    self.store.create_group(chunk, list(scenario_ids))
                    pending.update(
                        (lane_id, scenario_id)
                        for lane_id in chunk
                        for scenario_id in scenario_ids
                    )
                    created += 1
                except QueueFull:
                    return created
        if created:
            self.wakeup.set()
        return created

    async def sweep_worker(self) -> None:
        while not self.stop.is_set():
            group = self.store.next_group()
            if not group:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            preflighted: set[str] = set()
            while task := self.store.next_task(group["id"]):
                lane = self.lanes.get(task["lane_id"])
                if lane is None:
                    self.store.supersede_task(
                        task["id"], "lane removed from evaluator allowlist"
                    )
                    continue
                if self.store.task_is_satisfied(task["id"]):
                    self.store.supersede_task(
                        task["id"],
                        "newer observation completed after this group was queued",
                    )
                    continue
                self.store.start_task(task["id"])
                try:
                    if lane.id not in preflighted:
                        preflighted.add(lane.id)
                        result = await self.runner.preflight(group["id"], lane)
                        self.store.record_heartbeat(lane.id, result)
                    observation = await self.runner.run(
                        group["id"], lane, task["scenario_id"]
                    )
                    self.store.finish_task(
                        task["id"],
                        group["id"],
                        lane.id,
                        task["scenario_id"],
                        observation,
                    )
                except Exception as error:
                    self.store.fail_task(
                        task["id"],
                        group["id"],
                        lane.public(),
                        task["scenario_id"],
                        self.scenarios[task["scenario_id"]],
                        type(error).__name__,
                    )
            self.store.complete_group(group["id"])
            self.store.prune(
                self.runner.artifact_root,
                retention_days=14,
                max_bytes=512 * 1024 * 1024,
            )


runtime: Runtime | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runtime
    runtime = Runtime()
    await runtime.start()
    try:
        yield
    finally:
        await runtime.close()


app = FastAPI(title="cfwarp service evaluation API", version="1", lifespan=lifespan)


@app.middleware("http")
async def protect_docs(request: Request, call_next):
    if request.url.path in {"/docs", "/redoc", "/openapi.json"}:
        try:
            require_bearer(request.headers.get("authorization"))
        except HTTPException as error:
            return JSONResponse({"detail": error.detail}, status_code=error.status_code)
    return await call_next(request)


def bearer_matches(authorization: str | None, expected: str) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token, expected)


def require_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service unavailable")
    if not bearer_matches(authorization, runtime.token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid bearer token",
            {"WWW-Authenticate": "Bearer"},
        )


Protected = Annotated[None, Depends(require_bearer)]


def require_metrics_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service unavailable")
    if not bearer_matches(authorization, runtime.metrics_token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid metrics bearer token",
            {"WWW-Authenticate": "Bearer"},
        )


MetricsProtected = Annotated[None, Depends(require_metrics_bearer)]


def require_worker_bearer(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service unavailable")
    if not bearer_matches(authorization, runtime.worker_token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid worker bearer token",
            {"WWW-Authenticate": "Bearer"},
        )


WorkerProtected = Annotated[None, Depends(require_worker_bearer)]


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    if runtime is not None and runtime.background_failures:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service unavailable")
    return {"status": "ok"}


@app.get("/v1/scenarios")
def scenarios(_: Protected) -> list[dict[str, Any]]:
    """Only the scenarios this node is configured to run."""
    assert runtime
    return [row for row in runtime.scenario_capabilities if row["enabled"]]


@app.get("/v2/scenarios")
def scenarios_v2(_: Protected) -> list[dict[str, Any]]:
    assert runtime
    return [row for row in runtime.scenario_capabilities if row["enabled"]]


@app.get("/v1/scenario-capabilities")
def scenario_capabilities(_: Protected) -> list[dict[str, Any]]:
    """All known scenarios, including optional capabilities disabled here."""
    assert runtime
    return runtime.scenario_capabilities


@app.get("/v1/lanes")
def lanes(_: Protected) -> list[dict[str, Any]]:
    assert runtime
    return [lane.public() for lane in runtime.lanes.values()]


@app.get("/v2/lanes")
def lanes_v2(_: Protected) -> list[dict[str, Any]]:
    """Return public deployment identity without worker-only listener addresses."""
    assert runtime
    return [lane.public() for lane in runtime.lanes.values()]


@app.get("/v1/tiers")
def tiers(_: Protected) -> list[dict[str, Any]]:
    """Deprecated diagnostic tier; never a consumer admission surface."""
    assert runtime
    return [
        runtime.store.lane_tier(lane_id, requested_region=lane.requested_region)
        for lane_id, lane in runtime.lanes.items()
    ]


@app.post("/v1/run-groups", status_code=status.HTTP_202_ACCEPTED)
def create_group(body: RunGroupRequest, _: Protected) -> dict[str, Any]:
    assert runtime
    if unknown := set(body.lane_ids) - set(runtime.lanes):
        raise HTTPException(422, f"unknown lane IDs: {sorted(unknown)}")
    if unknown := set(body.scenario_ids) - set(runtime.scenarios):
        raise HTTPException(
            422, f"scenario IDs not enabled on this node: {sorted(unknown)}"
        )
    undeclared = {
        (lane_id, scenario_id)
        for lane_id in body.lane_ids
        for scenario_id in body.scenario_ids
        if scenario_id not in runtime.lanes[lane_id].scenarios
    }
    if undeclared:
        raise HTTPException(422, f"undeclared lane scenarios: {sorted(undeclared)}")
    try:
        group = runtime.store.create_group(body.lane_ids, body.scenario_ids)
    except QueueFull as error:
        raise HTTPException(
            409, "one active and one waiting run group already exist"
        ) from error
    runtime.wakeup.set()
    return group


@app.get("/v1/run-groups/{group_id}")
def get_group(group_id: str, _: Protected) -> dict[str, Any]:
    assert runtime
    try:
        return runtime.store.group(group_id)
    except KeyError as error:
        raise HTTPException(404, "run group not found") from error


@app.get("/v1/observations/latest")
def latest(_: Protected) -> list[dict[str, Any]]:
    assert runtime
    return runtime.store.latest()


@app.get("/v2/observations/latest")
def latest_v2(_: Protected) -> list[dict[str, Any]]:
    """Return the newest Observation v2 cell, including unknown/unavailable."""
    assert runtime
    return [
        observation
        for observation in runtime.store.latest()
        if observation.get("schema_version") == 2
    ]


@app.get("/v1/observations")
def observations(
    _: Protected,
    lane: str | None = None,
    scenario: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    assert runtime
    if lane is not None and lane not in runtime.lanes:
        raise HTTPException(422, "unknown lane ID")
    if scenario is not None and scenario not in runtime.scenarios:
        raise HTTPException(422, "unknown scenario ID")
    for value in (since, until):
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise HTTPException(422, "time filters must be ISO 8601") from error
    return runtime.store.observations(lane, scenario, since, until, limit)


@app.get("/v2/internal/lanes")
def internal_lanes(_: WorkerProtected) -> list[dict[str, Any]]:
    assert runtime
    return [lane.internal("light") for lane in runtime.lanes.values()]


@app.post("/v2/workers/heartbeat")
def worker_heartbeat(
    body: WorkerHeartbeatRequest, _: WorkerProtected
) -> dict[str, Any]:
    assert runtime
    return runtime.store.register_worker(
        body.worker_id,
        body.worker_class,
        body.node_id,
        body.evaluator_build,
        body.metadata,
    )


@app.post("/v2/jobs/claim")
def claim_job(body: ClaimRequest, _: WorkerProtected) -> dict[str, Any]:
    assert runtime
    runtime.store.register_worker(
        body.worker_id,
        body.worker_class,
        body.node_id,
        body.evaluator_build,
        body.metadata,
    )
    runtime.store.expire_leases(
        {lane_id: lane.public() for lane_id, lane in runtime.lanes.items()}, SCENARIOS
    )
    task = runtime.store.claim_task(
        body.worker_id, body.worker_class, body.lease_seconds
    )
    if task is None:
        return {"job": None}
    lane = runtime.lanes[task["lane_id"]]
    return {
        "job": {
            "task_id": task["id"],
            "group_id": task["group_id"],
            "scenario_id": task["scenario_id"],
            "execution_class": task["execution_class"],
            "attempt": task["attempt_count"],
            "lease_token": task["lease_token"],
            "lease_expires_at": task["lease_expires_at"],
            "lane": lane.internal(body.worker_class),
        }
    }


def validate_completed_observation(
    lane_id: str, scenario_id: str, observation: dict[str, Any]
) -> None:
    assert runtime
    lane = runtime.lanes[lane_id]
    subject = observation.get("subject") or {}
    lane_payload = observation.get("lane") or {}
    result = observation.get("result") or {}
    if observation.get("schema_version") != 2:
        raise HTTPException(422, "worker completion must be Observation v2")
    if observation.get("scenario_id") != SCENARIOS[scenario_id]:
        raise HTTPException(422, "scenario provenance does not match leased job")
    expected = {
        "deployment_origin": lane.deployment_origin,
        "instance_id": lane.instance_id,
        "node_id": lane.node_id,
        "config_generation": lane.config_generation,
    }
    if any(subject.get(key) != value for key, value in expected.items()):
        raise HTTPException(422, "subject provenance does not match active deployment")
    if lane_payload.get("capability_id") != lane.capability_id:
        raise HTTPException(422, "capability identity does not match active deployment")
    availability = result.get("availability")
    eligible = result.get("eligible")
    if (availability == "unknown" and eligible is not False) or (
        availability in {"available", "unavailable"} and eligible is not True
    ):
        raise HTTPException(422, "availability and eligibility are inconsistent")


@app.post("/v2/jobs/{task_id}/complete")
def complete_job(
    task_id: int, body: CompletionRequest, _: WorkerProtected
) -> dict[str, str]:
    assert runtime
    row = runtime.store.db.execute(
        "SELECT lane_id,scenario_id FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    validate_completed_observation(row["lane_id"], row["scenario_id"], body.observation)
    try:
        disposition = runtime.store.complete_leased_task(
            task_id, body.lease_token, body.observation
        )
    except LeaseConflict as error:
        raise HTTPException(409, str(error)) from error
    return {"disposition": disposition}


@app.post("/v2/jobs/{task_id}/fail")
def fail_job(task_id: int, body: FailureRequest, _: WorkerProtected) -> dict[str, str]:
    assert runtime
    row = runtime.store.db.execute(
        "SELECT lane_id,scenario_id FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    try:
        disposition = runtime.store.fail_leased_task(
            task_id,
            body.lease_token,
            runtime.lanes[row["lane_id"]].public(),
            SCENARIOS[row["scenario_id"]],
            body.error,
        )
    except LeaseConflict as error:
        raise HTTPException(409, str(error)) from error
    return {"disposition": disposition}


@app.post("/v2/heartbeats", status_code=status.HTTP_202_ACCEPTED)
def submit_heartbeat(
    body: HeartbeatResultRequest, _: WorkerProtected
) -> dict[str, str]:
    assert runtime
    if body.lane_id not in runtime.lanes:
        raise HTTPException(422, "unknown lane ID")
    runtime.store.record_heartbeat(body.lane_id, body.result)
    return {"disposition": "accepted"}


def cell_summary(lanes: dict[str, Any], now: datetime) -> dict[str, Any]:
    assert runtime
    availability = {"available": 0, "unavailable": 0, "unknown": 0}
    evaluated = fresh = 0
    expected = 0
    for lane_id, lane in lanes.items():
        latest_by_scenario = runtime.store.latest_by_scenario(lane_id)
        for scenario_id in lane.scenarios:
            if scenario_id not in runtime.scenarios:
                continue
            expected += 1
            record = latest_by_scenario.get(scenario_id)
            if record is None:
                availability["unknown"] += 1
                continue
            evaluated += 1
            if parse_time(record["fresh_until"]) <= now:
                availability["unknown"] += 1
                continue
            fresh += 1
            result = record["payload"].get("result") or {}
            classification = result.get("availability")
            if classification not in availability or not result.get("eligible"):
                classification = "unknown"
            availability[classification] += 1
    return {
        "expected_cells": expected,
        "evaluated_cells": evaluated,
        "fresh_cells": fresh,
        "availability": availability,
    }


def platform_slo_snapshot() -> dict[str, Any]:
    assert runtime
    now = datetime.now(timezone.utc)
    cells = cell_summary(runtime.lanes, now)
    workers = runtime.store.worker_statuses()
    worker_cutoff = now - timedelta(seconds=max(180, runtime.heartbeat_interval * 3))
    worker_up = {
        worker_class: sum(
            1
            for worker in workers
            if worker["worker_class"] == worker_class
            and parse_time(worker["last_seen_at"]) >= worker_cutoff
        )
        for worker_class in ("light", "perf", "browser")
    }
    hard_warp_off = sum(
        1
        for lane_id in runtime.lanes
        if (runtime.store.heartbeat_stats(lane_id)["latest"] or {}).get("warp") == "off"
    )
    telemetry_export_age = (
        max(0, (now - parse_time(runtime.telemetry_export_at)).total_seconds())
        if runtime.telemetry_export_at
        else None
    )
    last_sweep_age = (
        max(0, (now - parse_time(runtime.last_sweep_at)).total_seconds())
        if runtime.last_sweep_at
        else None
    )
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "observer_build": runtime.observer_build,
        "observer_up": not runtime.background_failures,
        "workers_up": worker_up,
        **cells,
        "completeness": (
            cells["fresh_cells"] / cells["expected_cells"]
            if cells["expected_cells"]
            else 0
        ),
        "queue": runtime.store.queue_stats(),
        "last_sweep_at": runtime.last_sweep_at,
        "last_sweep_age_seconds": last_sweep_age,
        "store_bytes": runtime.store.path.stat().st_size,
        "artifact_bytes": tree_size(runtime.runner.artifact_root),
        "background_failures": sorted(runtime.background_failures),
        "telemetry_export_at": runtime.telemetry_export_at,
        "telemetry_export_age_seconds": telemetry_export_age,
        "active_lane_count": len(runtime.lanes),
        "hard_warp_off": hard_warp_off,
        "expected_lane_count": runtime.expected_lane_count,
        "deployment_inventory_mismatch": runtime.expected_lane_count
        - len(runtime.lanes),
    }


@app.get("/v2/platform-slo")
def platform_slo(_: Protected) -> dict[str, Any]:
    return platform_slo_snapshot()


@app.post("/v2/telemetry-export-heartbeat", status_code=status.HTTP_202_ACCEPTED)
def telemetry_export_heartbeat(_: MetricsProtected) -> dict[str, str]:
    assert runtime
    runtime.telemetry_export_at = datetime.now(timezone.utc).isoformat()
    return {"disposition": "accepted"}


@app.get("/v2/egresses")
def egresses(
    _: Protected,
    scenario: str,
    capability_id: str | None = None,
    deployment_origin: str | None = None,
) -> list[dict[str, Any]]:
    """Return only exact, fresh, eligible evidence for the active generation."""
    assert runtime
    if scenario not in runtime.scenarios:
        raise HTTPException(422, "scenario is not enabled on this observer")
    now = datetime.now(timezone.utc)
    discovered = []
    for lane_id, lane in runtime.lanes.items():
        if capability_id is not None and lane.capability_id != capability_id:
            continue
        if (
            deployment_origin is not None
            and lane.deployment_origin != deployment_origin
        ):
            continue
        record = runtime.store.latest_by_scenario(lane_id).get(scenario)
        if record is None or parse_time(record["fresh_until"]) <= now:
            continue
        observation = record["payload"]
        result = observation.get("result") or {}
        subject = observation.get("subject") or {}
        lane_payload = observation.get("lane") or {}
        if not (
            observation.get("schema_version") == 2
            and result.get("availability") == "available"
            and result.get("eligible") is True
            and subject.get("config_generation") == lane.config_generation
            and subject.get("deployment_origin") == lane.deployment_origin
            and lane_payload.get("capability_id") == lane.capability_id
        ):
            continue
        discovered.append(
            {
                **lane.public(),
                "scenario": scenario,
                "observation_id": observation["observation_id"],
                "observed_at": observation["observed_at"],
                "fresh_until": observation["fresh_until"],
                "egress": observation.get("egress") or {},
            }
        )
    return discovered


TIERS = ("preferred", "usable", "degraded", "quarantined", "unknown")


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
def metrics(_: MetricsProtected) -> str:
    """Prometheus exposition for the node-local collector to scrape.

    Tags stay bounded to lane, scenario, and lane dimensions. Per-attempt values
    such as observation_id are never emitted; they would buy unbounded
    cardinality in the observation backend.
    """
    assert runtime
    now = datetime.now(timezone.utc)
    lines = [
        "# HELP cfwarp_platform_api_up Observer API is serving requests.",
        "# TYPE cfwarp_platform_api_up gauge",
        "# HELP cfwarp_probe_lane_tier Lane routing tier, 1 for the active tier.",
        "# TYPE cfwarp_probe_lane_tier gauge",
        "# HELP cfwarp_probe_heartbeat_ok_ratio Heartbeat success ratio over the tier window.",
        "# TYPE cfwarp_probe_heartbeat_ok_ratio gauge",
        "# HELP cfwarp_probe_scenario_available Whether the newest observation is available.",
        "# TYPE cfwarp_probe_scenario_available gauge",
        "# HELP cfwarp_probe_scenario_fresh_seconds Seconds until the newest observation expires.",
        "# TYPE cfwarp_probe_scenario_fresh_seconds gauge",
        "# HELP cfwarp_probe_lane_warp_on Latest listener trace explicitly reported WARP state for this lane.",
        "# TYPE cfwarp_probe_lane_warp_on gauge",
        "# HELP cfwarp_probe_lane_warp_off Latest listener trace explicitly reported warp=off for this lane.",
        "# TYPE cfwarp_probe_lane_warp_off gauge",
        "# HELP cfwarp_probe_lane_throughput_mibps Newest sampled lane throughput.",
        "# TYPE cfwarp_probe_lane_throughput_mibps gauge",
        "# HELP cfwarp_probe_lane_performance_band Lane performance band, 1 for the active band.",
        "# TYPE cfwarp_probe_lane_performance_band gauge",
        "# HELP cfwarp_probe_lane_region_match Lane reaches its requested egress region.",
        "# TYPE cfwarp_probe_lane_region_match gauge",
        "# HELP cfwarp_probe_lane_region_match_ratio Fraction of recent heartbeats in the requested region.",
        "# TYPE cfwarp_probe_lane_region_match_ratio gauge",
        "# HELP cfwarp_probe_scenario_result One-hot scenario availability classification.",
        "# TYPE cfwarp_probe_scenario_result gauge",
    ]
    for lane_id, lane in runtime.lanes.items():
        tier = runtime.store.lane_tier(lane_id, requested_region=lane.requested_region)
        common = (
            f'lane="{escape_label(lane_id)}",'
            f'node_id="{escape_label(lane.node_id)}",'
            f'deployment_origin="{escape_label(lane.deployment_origin)}",'
            f'composition="{escape_label(lane.composition)}",'
            f'transport="{escape_label(lane.transport)}",'
            f'requested_region="{escape_label(lane.requested_region or "")}"'
        )
        for name in TIERS:
            value = 1 if tier["tier"] == name else 0
            lines.append(f'cfwarp_probe_lane_tier{{{common},tier="{name}"}} {value}')
        ratio = tier["heartbeat"]["ok_ratio"]
        if ratio is not None:
            lines.append(f"cfwarp_probe_heartbeat_ok_ratio{{{common}}} {ratio}")
        # Emitted separately from the ok ratio on purpose. A lane serving
        # traffic while reporting warp=off is a correctness failure, not a
        # degradation, and it must be distinguishable from an unreachable
        # lane rather than averaged into the same signal.
        latest = tier["heartbeat"]["latest"]
        # No series is safer than a false zero when the listener was
        # unreachable and therefore produced no trace. The separate heartbeat
        # ratio owns reachability; zero here is reserved for an explicit
        # listener-facing warp=off result.
        if latest is not None and latest.get("warp") in {"on", "off"}:
            warp_on = 1 if latest["warp"] == "on" else 0
            lines.append(f"cfwarp_probe_lane_warp_on{{{common}}} {warp_on}")
        # Emit the alert signal for every lane so an unreachable sample can
        # recover an earlier explicit off result. Zero means only "no explicit
        # off result"; reachability remains owned by the heartbeat ratio.
        warp_off = 1 if latest is not None and latest.get("warp") == "off" else 0
        lines.append(f"cfwarp_probe_lane_warp_off{{{common}}} {warp_off}")
        throughput = tier.get("throughput_mibps")
        if throughput is not None:
            lines.append(f"cfwarp_probe_lane_throughput_mibps{{{common}}} {throughput}")
        # Emitted as one series per band so a dashboard can group lanes by
        # band without string-matching a label value.
        band = tier.get("performance_band")
        if band is not None:
            for name in ("fast", "moderate", "slow"):
                lines.append(
                    f'cfwarp_probe_lane_performance_band{{{common},band="{name}"}} '
                    f"{1 if band == name else 0}"
                )
        region = tier.get("region") or {}
        # Only lanes that requested a region can mismatch one. Direct lanes are
        # omitted entirely rather than reported as matching or failing.
        if region.get("matches") is not None:
            observed = escape_label(region.get("observed") or "")
            lines.append(
                f'cfwarp_probe_lane_region_match{{{common},observed="{observed}"}} '
                f"{1 if region['matches'] else 0}"
            )
            lines.append(
                f"cfwarp_probe_lane_region_match_ratio{{{common}}} {region['match_ratio']}"
            )
        latest_by_scenario = runtime.store.latest_by_scenario(lane_id)
        for scenario_id in lane.scenarios:
            if scenario_id not in runtime.scenarios:
                continue
            record = latest_by_scenario.get(scenario_id)
            result = (record["payload"].get("result") or {}) if record else {}
            fresh = bool(record and parse_time(record["fresh_until"]) > now)
            classification = result.get("availability") if result else None
            if (
                not fresh
                or classification not in {"available", "unavailable"}
                or result.get("eligible") is not True
            ):
                classification = "unknown"
            label = f'{common},scenario="{escape_label(scenario_id)}"'
            available = 1 if classification == "available" else 0
            lines.append(f"cfwarp_probe_scenario_available{{{label}}} {available}")
            for name in ("available", "unavailable", "unknown"):
                value = 1 if classification == name else 0
                lines.append(
                    f'cfwarp_probe_scenario_result{{{label},availability="{name}"}} {value}'
                )
            if record:
                remaining = (parse_time(record["fresh_until"]) - now).total_seconds()
                lines.append(
                    f"cfwarp_probe_scenario_fresh_seconds{{{label}}} {remaining:.0f}"
                )
    snapshot = platform_slo_snapshot()
    # Queue, storage, background-loop, telemetry, and worker facts belong to
    # this observer, not an individual deployment origin. Emit them once with
    # an explicit observer scope so aggregating by origin cannot overcount a
    # single SQLite queue or filesystem.
    observer_node_ids = {lane.node_id for lane in runtime.lanes.values()}
    observer_node_id = (
        next(iter(observer_node_ids)) if len(observer_node_ids) == 1 else "mixed"
    )
    observer = f'node_id="{escape_label(observer_node_id)}",scope="observer"'
    lines.append(f"cfwarp_platform_api_up{{{observer}}} 1")
    lines.append(
        f"cfwarp_platform_queue_depth{{{observer}}} {snapshot['queue']['depth']}"
    )
    lines.append(
        f"cfwarp_platform_queue_oldest_age_seconds{{{observer}}} "
        f"{snapshot['queue']['oldest_age_seconds']}"
    )
    lines.append(f"cfwarp_platform_store_bytes{{{observer}}} {snapshot['store_bytes']}")
    lines.append(
        f"cfwarp_platform_artifact_bytes{{{observer}}} {snapshot['artifact_bytes']}"
    )
    lines.append(
        f"cfwarp_platform_background_failures{{{observer}}} "
        f"{len(snapshot['background_failures'])}"
    )
    lines.append(
        f"cfwarp_platform_deployment_inventory_mismatch{{{observer}}} "
        f"{snapshot['deployment_inventory_mismatch']}"
    )
    if snapshot["last_sweep_age_seconds"] is not None:
        lines.append(
            f"cfwarp_platform_last_sweep_age_seconds{{{observer}}} "
            f"{snapshot['last_sweep_age_seconds']}"
        )
    if snapshot["telemetry_export_age_seconds"] is not None:
        lines.append(
            f"cfwarp_platform_telemetry_export_age_seconds{{{observer}}} "
            f"{snapshot['telemetry_export_age_seconds']}"
        )
    for worker_class, count in snapshot["workers_up"].items():
        lines.append(
            f'cfwarp_platform_worker_up{{{observer},worker_class="{worker_class}"}} '
            f"{1 if count else 0}"
        )
    origins = {
        (lane.node_id, lane.deployment_origin) for lane in runtime.lanes.values()
    }
    for node_id, origin in sorted(origins):
        partition_lanes = {
            lane_id: lane
            for lane_id, lane in runtime.lanes.items()
            if lane.node_id == node_id and lane.deployment_origin == origin
        }
        partition = cell_summary(partition_lanes, now)
        partition_warp_off = sum(
            1
            for lane_id in partition_lanes
            if (runtime.store.heartbeat_stats(lane_id)["latest"] or {}).get("warp")
            == "off"
        )
        platform = (
            f'node_id="{escape_label(node_id)}",'
            f'deployment_origin="{escape_label(origin)}"'
        )
        for name in (
            "expected_cells",
            "evaluated_cells",
            "fresh_cells",
        ):
            lines.append(f"cfwarp_platform_{name}{{{platform}}} {partition[name]}")
        lines.append(
            f"cfwarp_platform_active_lane_count{{{platform}}} {len(partition_lanes)}"
        )
        lines.append(
            f"cfwarp_platform_completeness{{{platform}}} "
            f"{partition['fresh_cells'] / partition['expected_cells'] if partition['expected_cells'] else 0}"
        )
        lines.append(
            f"cfwarp_platform_hard_warp_off{{{platform}}} {partition_warp_off}"
        )
        for classification, count in partition["availability"].items():
            lines.append(
                f'cfwarp_platform_scenario_cells{{{platform},availability="{classification}"}} '
                f"{count}"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    import uvicorn

    # Loopback by default. The probe runs with host networking so it can reach
    # lane listeners published on 127.0.0.1, which would otherwise put this API
    # on the node's public interface. Widening the bind is the deployment
    # system's decision, made explicitly through a tailnet address.
    uvicorn.run(
        "cfwarp_service_eval.api:app",
        host=os.environ.get("SERVICE_EVAL_BIND_HOST", "127.0.0.1"),
        port=env_int("SERVICE_EVAL_BIND_PORT", 8080),
        workers=1,
        proxy_headers=False,
    )
