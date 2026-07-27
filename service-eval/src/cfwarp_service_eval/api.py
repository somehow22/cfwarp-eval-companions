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

from .config import BROWSER_SCENARIOS, LIGHTWEIGHT_SCENARIOS, SCENARIOS, load_lanes
from .runner import ProbeRunner
from .store import QueueFull, Store, parse_time


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


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def parse_scenarios(raw: str | None) -> dict[str, str]:
    """Resolve the node's enabled scenario set, defaulting to all of them."""
    if not raw or not raw.strip():
        return dict(SCENARIOS)
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = sorted(set(selected) - set(SCENARIOS))
    if unknown:
        raise ValueError(f"unknown scenario IDs in SERVICE_EVAL_SCENARIOS: {unknown}")
    return {key: SCENARIOS[key] for key in selected}


def parse_browser_execution(raw: str | None) -> str:
    value = (raw or "disabled").strip().lower()
    if value not in {"disabled", "local", "agentcore"}:
        raise ValueError(
            "SERVICE_EVAL_BROWSER_EXECUTION must be disabled, local, or agentcore"
        )
    return value


def memory_limit_mib() -> int:
    """Return the effective cgroup memory ceiling, falling back to host RAM."""
    cgroup_limit = Path("/sys/fs/cgroup/memory.max")
    if cgroup_limit.is_file():
        raw = cgroup_limit.read_text(encoding="utf-8").strip()
        if raw != "max":
            return max(1, int(raw) // (1024 * 1024))
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return max(1, int(line.split()[1]) // 1024)
    page_count = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    if page_count > 0 and page_size > 0:
        return max(1, page_count * page_size // (1024 * 1024))
    raise ValueError("cannot determine runtime memory ceiling")


def resolve_scenario_capabilities(
    raw: str | None,
    browser_execution: str,
    available_memory_mib: int,
    browser_min_memory_mib: int,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    requested = parse_scenarios(raw)
    enabled: dict[str, str] = {}
    capabilities: list[dict[str, Any]] = []
    for scenario_id, observation_id in SCENARIOS.items():
        execution_class = (
            "browser" if scenario_id in BROWSER_SCENARIOS else "lightweight"
        )
        selected = scenario_id in requested
        reason = "not selected by SERVICE_EVAL_SCENARIOS"
        execution_target = "local"
        scenario_enabled = selected
        minimum_memory_mib: int | None = None

        if scenario_id in LIGHTWEIGHT_SCENARIOS:
            reason = "enabled" if selected else reason
        elif not selected:
            execution_target = "none"
        elif browser_execution == "disabled":
            scenario_enabled = False
            execution_target = "none"
            reason = "browser automation is optional and disabled"
        elif browser_execution == "local":
            minimum_memory_mib = browser_min_memory_mib
            if available_memory_mib < browser_min_memory_mib:
                scenario_enabled = False
                execution_target = "none"
                reason = (
                    f"local browser requires {browser_min_memory_mib} MiB; "
                    f"runtime ceiling is {available_memory_mib} MiB"
                )
            else:
                reason = "enabled with local Chromium"
        else:
            execution_target = "agentcore"
            reason = "enabled with cloud browser execution"

        if scenario_enabled:
            enabled[scenario_id] = observation_id
        capabilities.append(
            {
                "id": scenario_id,
                "observation_scenario_id": observation_id,
                "execution_class": execution_class,
                "enabled": scenario_enabled,
                "execution_target": execution_target,
                "minimum_memory_mib": minimum_memory_mib,
                "reason": reason,
            }
        )
    return enabled, capabilities


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
        self.wakeup = asyncio.Event()
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.store.recover(
            {lane_id: lane.public() for lane_id, lane in self.lanes.items()},
            SCENARIOS,
        )
        self.spawn(self.sweep_worker, "probe-sweep-worker")
        if self.heartbeat_enabled:
            self.spawn(self.heartbeat_loop, "probe-heartbeat")
        if self.scheduler_enabled:
            self.spawn(self.scheduler_loop, "probe-scheduler")
        self.wakeup.set()

    def spawn(self, target: Any, name: str) -> None:
        self.tasks.append(asyncio.create_task(target(), name=name))

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
        for scenario_id in self.scenarios:
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
                self.enqueue_due_sweeps()
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
                lane = self.lanes[task["lane_id"]]
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


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/scenarios")
def scenarios(_: Protected) -> list[dict[str, Any]]:
    """Only the scenarios this node is configured to run."""
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


@app.get("/v1/tiers")
def tiers(_: Protected) -> list[dict[str, Any]]:
    """Derived routing tier per lane. This is the consumer-facing surface:
    callers route on tier, not on raw availability arithmetic."""
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
        "# HELP cfwarp_probe_lane_tier Lane routing tier, 1 for the active tier.",
        "# TYPE cfwarp_probe_lane_tier gauge",
        "# HELP cfwarp_probe_heartbeat_ok_ratio Heartbeat success ratio over the tier window.",
        "# TYPE cfwarp_probe_heartbeat_ok_ratio gauge",
        "# HELP cfwarp_probe_scenario_available Whether the newest observation is available.",
        "# TYPE cfwarp_probe_scenario_available gauge",
        "# HELP cfwarp_probe_scenario_fresh_seconds Seconds until the newest observation expires.",
        "# TYPE cfwarp_probe_scenario_fresh_seconds gauge",
        "# HELP cfwarp_probe_lane_warp_on Latest heartbeat reported warp=on for this lane.",
        "# TYPE cfwarp_probe_lane_warp_on gauge",
        "# HELP cfwarp_probe_lane_throughput_mibps Newest sampled lane throughput.",
        "# TYPE cfwarp_probe_lane_throughput_mibps gauge",
        "# HELP cfwarp_probe_lane_performance_band Lane performance band, 1 for the active band.",
        "# TYPE cfwarp_probe_lane_performance_band gauge",
        "# HELP cfwarp_probe_lane_region_match Lane reaches its requested egress region.",
        "# TYPE cfwarp_probe_lane_region_match gauge",
        "# HELP cfwarp_probe_lane_region_match_ratio Fraction of recent heartbeats in the requested region.",
        "# TYPE cfwarp_probe_lane_region_match_ratio gauge",
    ]
    for lane_id, lane in runtime.lanes.items():
        tier = runtime.store.lane_tier(lane_id, requested_region=lane.requested_region)
        common = (
            f'lane="{escape_label(lane_id)}",'
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
        if latest is not None:
            warp_on = 1 if latest.get("warp") == "on" else 0
            lines.append(f"cfwarp_probe_lane_warp_on{{{common}}} {warp_on}")
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
        for scenario_id, record in runtime.store.latest_by_scenario(lane_id).items():
            result = record["payload"].get("result") or {}
            label = f'{common},scenario="{escape_label(scenario_id)}"'
            available = 1 if result.get("availability") == "available" else 0
            lines.append(f"cfwarp_probe_scenario_available{{{label}}} {available}")
            remaining = (parse_time(record["fresh_until"]) - now).total_seconds()
            lines.append(
                f"cfwarp_probe_scenario_fresh_seconds{{{label}}} {remaining:.0f}"
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
