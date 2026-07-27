from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# A scheduled sweep of a 9-lane node chunks into two groups; the remaining depth
# absorbs retries and a concurrent manual submission without letting any caller
# queue unbounded work.
MAX_PENDING_GROUPS = 8

# Tier thresholds. Deliberately coarse: these classify a lane for routing, they
# are not service-level targets. See docs/slo-v2.md.
QUARANTINE_HEARTBEAT_RATIO = 0.2
DEGRADED_HEARTBEAT_RATIO = 0.8
TIER_WINDOW_HOURS = 24 * 14

# Liveness tiering reads a short recent window, not the full availability
# window. Over 14 days a lane that has recovered stays demoted for days because
# old failures still dominate the ratio, which makes the routing signal wrong in
# the one direction that matters: it hides a lane that works.
HEARTBEAT_WINDOW_HOURS = 1
# Below this many samples the ratio is noise. One failed beat must not
# quarantine a lane.
MIN_HEARTBEAT_SAMPLES = 5

# Performance bands, in MiB/s. Banding is not gating: a substrate lane is never
# failed on throughput, because its speed is the provider's property. But a
# consumer routing on tier must be able to tell 19 MiB/s from 0.8 MiB/s, and
# without this the tier is silent about a 10-20x difference across the fleet.
PERFORMANCE_BANDS = ((10.0, "fast"), (2.0, "moderate"), (0.0, "slow"))


# A single mismatched sample means nothing: observed region legitimately varies
# and the observation contract treats a mismatch as evidence, not failure. Only a
# lane that essentially never reaches its region is worth surfacing.
REGION_MATCH_FLOOR = 0.5


def performance_band(throughput_mibps: float | None) -> str | None:
    if throughput_mibps is None:
        return None
    for floor, name in PERFORMANCE_BANDS:
        if throughput_mibps >= floor:
            return name
    return "slow"


class Store:
    def __init__(self, path: Path, max_pending_groups: int = MAX_PENDING_GROUPS):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_pending_groups = max_pending_groups
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS run_groups (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
              started_at TEXT, finished_at TEXT, lane_ids TEXT NOT NULL,
              scenario_ids TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
              lane_id TEXT NOT NULL, scenario_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
              status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT,
              UNIQUE(group_id, lane_id, scenario_id),
              FOREIGN KEY(group_id) REFERENCES run_groups(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS observations (
              observation_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
              lane_id TEXT NOT NULL, scenario_id TEXT NOT NULL,
              observed_at TEXT NOT NULL, fresh_until TEXT NOT NULL,
              payload TEXT NOT NULL,
              FOREIGN KEY(group_id) REFERENCES run_groups(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_obs_filter
              ON observations(lane_id, scenario_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS heartbeats (
              id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
              observed_at TEXT NOT NULL, ok INTEGER NOT NULL,
              warp TEXT, loc TEXT, colo TEXT, error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_heartbeat_lane
              ON heartbeats(lane_id, observed_at DESC);
            """)

    def recover(
        self,
        lanes: Mapping[str, Mapping[str, Any]],
        scenario_ids: Mapping[str, str],
        runtime: str = "podman",
    ) -> None:
        with self._lock, self.db:
            running = self.db.execute(
                "SELECT group_id,lane_id,scenario_id FROM tasks WHERE status='running'"
            ).fetchall()
            now = datetime.now(timezone.utc)
            for row in running:
                observation = unknown_observation(
                    lanes[row["lane_id"]],
                    scenario_ids[row["scenario_id"]],
                    now,
                    runtime,
                )
                self._insert_observation(
                    row["group_id"], row["lane_id"], row["scenario_id"], observation
                )
            self.db.execute(
                "UPDATE tasks SET status='unknown', finished_at=?, error='service restarted during probe' WHERE status='running'",
                (now.isoformat(),),
            )
            self.db.execute(
                "UPDATE run_groups SET status='queued', started_at=NULL WHERE status='running'"
            )
            self._backfill_observation_provenance(lanes, scenario_ids, runtime)
            self._supersede_duplicate_pending_tasks(now.isoformat())

    def _backfill_observation_provenance(
        self,
        lanes: Mapping[str, Mapping[str, Any]],
        scenario_ids: Mapping[str, str],
        runtime: str,
    ) -> None:
        """Repair legacy unknown rows without changing their result or time.

        Older worker-failure observations stored the relational lane/scenario
        keys but left their Observation v1 provenance null. The lane allowlist
        is authoritative for those immutable identity fields. Preserve the
        observation ID, timestamps, result, eligibility, and failure layer.
        """
        rows = self.db.execute(
            "SELECT observation_id,lane_id,scenario_id,payload FROM observations"
        ).fetchall()
        for row in rows:
            lane = lanes.get(row["lane_id"])
            observation_scenario_id = scenario_ids.get(row["scenario_id"])
            if lane is None or observation_scenario_id is None:
                continue
            payload = json.loads(row["payload"])
            subject = payload.setdefault("subject", {})
            lane_payload = payload.setdefault("lane", {})
            changed = False
            for key, value in (
                ("instance_id", lane["instance_id"]),
                ("node_id", lane["node_id"]),
                ("runtime", runtime),
                ("image_identity", lane["image_identity"]),
                ("config_digest", lane["config_digest"]),
            ):
                if subject.get(key) in (None, ""):
                    subject[key] = value
                    changed = True
            for key in (
                "composition",
                "transport",
                "substrate_profile",
                "requested_region",
            ):
                if lane_payload.get(key) in (None, "") and lane.get(key) is not None:
                    lane_payload[key] = lane[key]
                    changed = True
            if payload.get("scenario_id") != observation_scenario_id:
                payload["scenario_id"] = observation_scenario_id
                changed = True
            if changed:
                clean = sanitize_observation(payload)
                self.db.execute(
                    "UPDATE observations SET payload=? WHERE observation_id=?",
                    (
                        json.dumps(clean, separators=(",", ":")),
                        row["observation_id"],
                    ),
                )

    def _supersede_duplicate_pending_tasks(self, now: str) -> None:
        """Keep only the oldest queued/running copy of each lane/scenario cell.

        The scheduler evaluates freshness, so a cell remains due until its
        active task finishes. Without pending-task deduplication, every
        scheduler tick can enqueue another copy and starve later lanes.
        """
        rows = self.db.execute(
            """
            SELECT t.id,t.lane_id,t.scenario_id,t.status
            FROM tasks t JOIN run_groups g ON g.id=t.group_id
            WHERE t.status IN ('queued','running')
            ORDER BY g.created_at,t.ordinal,t.id
            """
        ).fetchall()
        claimed: set[tuple[str, str]] = set()
        for row in rows:
            cell = (row["lane_id"], row["scenario_id"])
            if cell not in claimed:
                claimed.add(cell)
                continue
            if row["status"] == "queued":
                self.db.execute(
                    """
                    UPDATE tasks
                    SET status='superseded',finished_at=?,
                        error='duplicate pending scheduler cell'
                    WHERE id=?
                    """,
                    (now, row["id"]),
                )
        self.db.execute(
            """
            UPDATE run_groups
            SET status='complete',finished_at=?
            WHERE status='queued'
              AND NOT EXISTS (
                SELECT 1 FROM tasks
                WHERE tasks.group_id=run_groups.id
                  AND tasks.status IN ('queued','running')
              )
            """,
            (now,),
        )

    def pending_cells(self) -> set[tuple[str, str]]:
        with self._lock:
            rows = self.db.execute(
                """
                SELECT lane_id,scenario_id FROM tasks
                WHERE status IN ('queued','running')
                """
            ).fetchall()
        return {(row["lane_id"], row["scenario_id"]) for row in rows}

    def create_group(
        self, lane_ids: list[str], scenario_ids: list[str]
    ) -> dict[str, Any]:
        with self._lock, self.db:
            occupied = self.db.execute(
                "SELECT count(*) FROM run_groups WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if occupied >= self.max_pending_groups:
                raise QueueFull
            group_id = str(uuid.uuid4())
            now = utcnow()
            self.db.execute(
                "INSERT INTO run_groups(id,status,created_at,lane_ids,scenario_ids) VALUES(?,?,?,?,?)",
                (
                    group_id,
                    "queued",
                    now,
                    json.dumps(lane_ids),
                    json.dumps(scenario_ids),
                ),
            )
            ordinal = 0
            for lane_id in lane_ids:
                for scenario_id in scenario_ids:
                    self.db.execute(
                        "INSERT INTO tasks(group_id,lane_id,scenario_id,ordinal,status) VALUES(?,?,?,?,?)",
                        (group_id, lane_id, scenario_id, ordinal, "queued"),
                    )
                    ordinal += 1
        return self.group(group_id)

    def next_group(self) -> dict[str, Any] | None:
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT id FROM run_groups WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE run_groups SET status='running', started_at=COALESCE(started_at,?) WHERE id=?",
                (utcnow(), row["id"]),
            )
            return self.group(row["id"])

    def next_task(self, group_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE group_id=? AND status='queued' ORDER BY ordinal LIMIT 1",
                (group_id,),
            ).fetchone()
        return dict(row) if row else None

    def start_task(self, task_id: int) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE tasks SET status='running',started_at=? WHERE id=?",
                (utcnow(), task_id),
            )

    def task_is_satisfied(self, task_id: int) -> bool:
        """Whether newer evidence already satisfies this queued request.

        A task queued at T may wait behind another group that completes the
        same cell at T+1. Running the older queued copy would waste probe
        capacity. Evidence from before T never satisfies an explicit request.
        """
        with self._lock:
            row = self.db.execute(
                """
                SELECT g.created_at, max(o.observed_at) AS latest_observed_at
                FROM tasks t
                JOIN run_groups g ON g.id=t.group_id
                LEFT JOIN observations o
                  ON o.lane_id=t.lane_id AND o.scenario_id=t.scenario_id
                WHERE t.id=?
                GROUP BY g.created_at
                """,
                (task_id,),
            ).fetchone()
        return bool(
            row
            and row["latest_observed_at"]
            and row["latest_observed_at"] > row["created_at"]
        )

    def supersede_task(self, task_id: int, reason: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                """
                UPDATE tasks
                SET status='superseded',finished_at=?,error=?
                WHERE id=? AND status='queued'
                """,
                (utcnow(), reason[:300], task_id),
            )

    def finish_task(
        self,
        task_id: int,
        group_id: str,
        lane_id: str,
        scenario_id: str,
        observation: dict[str, Any],
    ) -> None:
        with self._lock, self.db:
            self._insert_observation(group_id, lane_id, scenario_id, observation)
            self.db.execute(
                "UPDATE tasks SET status='complete',finished_at=? WHERE id=?",
                (utcnow(), task_id),
            )

    def fail_task(
        self,
        task_id: int,
        group_id: str,
        lane: Mapping[str, Any],
        scenario_id: str,
        observation_scenario_id: str,
        error: str,
        runtime: str = "podman",
    ) -> None:
        now = datetime.now(timezone.utc)
        observation = unknown_observation(lane, observation_scenario_id, now, runtime)
        lane_id = str(lane["id"])
        with self._lock, self.db:
            self._insert_observation(group_id, lane_id, scenario_id, observation)
            self.db.execute(
                "UPDATE tasks SET status='unknown',finished_at=?,error=? WHERE id=?",
                (now.isoformat(), error[:300], task_id),
            )

    def complete_group(self, group_id: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE run_groups SET status='complete',finished_at=? WHERE id=?",
                (utcnow(), group_id),
            )

    def group(self, group_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM run_groups WHERE id=?", (group_id,)
            ).fetchone()
            if not row:
                raise KeyError(group_id)
            result = dict(row)
            result["tasks"] = [
                dict(item)
                for item in self.db.execute(
                    "SELECT lane_id,scenario_id,status,started_at,finished_at,error FROM tasks WHERE group_id=? ORDER BY ordinal",
                    (group_id,),
                )
            ]
        result["lane_ids"] = json.loads(result["lane_ids"])
        result["scenario_ids"] = json.loads(result["scenario_ids"])
        return result

    def observations(
        self,
        lane_id: str | None = None,
        scenario_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses, values = [], []
        for column, value, operator in (
            ("lane_id", lane_id, "="),
            ("scenario_id", scenario_id, "="),
            ("observed_at", since, ">="),
            ("observed_at", until, "<="),
        ):
            if value is not None:
                clauses.append(f"{column}{operator}?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._lock:
            rows = self.db.execute(
                f"SELECT payload FROM observations {where} ORDER BY observed_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def latest(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("""
              SELECT payload FROM observations o WHERE observed_at=(
                SELECT max(observed_at) FROM observations
                WHERE lane_id=o.lane_id AND scenario_id=o.scenario_id)
              ORDER BY lane_id,scenario_id
            """).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def record_heartbeat(self, lane_id: str, result: dict[str, Any]) -> None:
        """Store one cheap liveness sample. Heartbeats are lane facts, not
        scenario observations, and never enter availability arithmetic."""
        checks = result.get("checks") or {}
        trace = checks.get("trace") or {}
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO heartbeats(lane_id,observed_at,ok,warp,loc,colo,error) VALUES(?,?,?,?,?,?,?)",
                (
                    lane_id,
                    utcnow(),
                    1 if result.get("ok") else 0,
                    trace.get("warp"),
                    trace.get("loc"),
                    trace.get("colo"),
                    (result.get("error") or None) and str(result["error"])[:300],
                ),
            )

    def region_stats(
        self,
        lane_id: str,
        requested_region: str | None,
        window_hours: int = HEARTBEAT_WINDOW_HOURS,
    ) -> dict[str, Any]:
        """Compare where a lane was asked to exit against where it actually did.

        A provider can fall back silently when a requested region is
        unavailable: the lane stays up, reports warp=on, and serves a country
        nobody asked for. No tunnel-level signal detects that. Heartbeats
        already record the observed region, so the comparison is free.
        """
        if not requested_region:
            # Direct lanes request nothing, so there is nothing to mismatch.
            return {
                "requested": None,
                "observed": None,
                "match_ratio": None,
                "matches": None,
                "samples": 0,
            }
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        with self._lock:
            rows = self.db.execute(
                "SELECT loc FROM heartbeats WHERE lane_id=? AND observed_at>=? AND loc IS NOT NULL",
                (lane_id, since),
            ).fetchall()
        locs = [row["loc"] for row in rows if row["loc"]]
        if not locs:
            return {
                "requested": requested_region,
                "observed": None,
                "match_ratio": None,
                "matches": None,
                "samples": 0,
            }
        hits = sum(1 for loc in locs if loc.upper() == requested_region.upper())
        ratio = hits / len(locs)
        observed = max(set(locs), key=locs.count)
        return {
            "requested": requested_region,
            "observed": observed,
            "match_ratio": round(ratio, 3),
            # Do not classify one transient observation as region drift. Reuse
            # the heartbeat sample floor so the signal represents persistence.
            "matches": (
                ratio >= REGION_MATCH_FLOOR
                if len(locs) >= MIN_HEARTBEAT_SAMPLES
                else None
            ),
            "samples": len(locs),
        }

    def heartbeat_stats(
        self, lane_id: str, window_hours: int = TIER_WINDOW_HOURS
    ) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        with self._lock:
            row = self.db.execute(
                "SELECT count(*) AS total, sum(ok) AS ok_count FROM heartbeats WHERE lane_id=? AND observed_at>=?",
                (lane_id, since),
            ).fetchone()
            latest = self.db.execute(
                "SELECT observed_at,ok,warp,loc,colo FROM heartbeats WHERE lane_id=? ORDER BY observed_at DESC LIMIT 1",
                (lane_id,),
            ).fetchone()
        total = row["total"] or 0
        ok_count = row["ok_count"] or 0
        return {
            "samples": total,
            "ok_ratio": (ok_count / total) if total else None,
            "latest": dict(latest) if latest else None,
        }

    def latest_by_scenario(self, lane_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                """
                SELECT scenario_id, payload, fresh_until FROM observations o WHERE lane_id=?
                  AND observed_at=(SELECT max(observed_at) FROM observations
                    WHERE lane_id=o.lane_id AND scenario_id=o.scenario_id)
                """,
                (lane_id,),
            ).fetchall()
        return {
            row["scenario_id"]: {
                "payload": json.loads(row["payload"]),
                "fresh_until": row["fresh_until"],
            }
            for row in rows
        }

    def lane_tier(
        self,
        lane_id: str,
        window_hours: int = TIER_WINDOW_HOURS,
        requested_region: str | None = None,
    ) -> dict[str, Any]:
        """Derive a routing tier for one lane.

        Scenario results are counted, never averaged together: the observation
        contract forbids collapsing different scenarios into one number. What
        this aggregates is lane liveness (heartbeats) plus how many scenarios
        are currently unavailable.
        """
        now = datetime.now(timezone.utc)
        heartbeat = self.heartbeat_stats(lane_id, HEARTBEAT_WINDOW_HOURS)
        scenarios = self.latest_by_scenario(lane_id)

        fresh, stale, unavailable = [], [], []
        for scenario_id, record in scenarios.items():
            if parse_time(record["fresh_until"]) <= now:
                stale.append(scenario_id)
                continue
            fresh.append(scenario_id)
            result = record["payload"].get("result") or {}
            if result.get("eligible") and result.get("availability") != "available":
                unavailable.append(scenario_id)

        perf = scenarios.get("perf", {}).get("payload", {}).get("perf") or {}
        meets_floor = perf.get("meets_floor")
        throughput = perf.get("throughput_mibps")

        ratio = heartbeat["ok_ratio"]
        # Treat a thin sample as no signal rather than a bad one.
        if heartbeat["samples"] < MIN_HEARTBEAT_SAMPLES:
            ratio = None
        reason = ""
        if ratio is None and not fresh:
            tier, reason = "unknown", "no heartbeat samples and no fresh observations"
        elif ratio is not None and ratio < QUARANTINE_HEARTBEAT_RATIO:
            tier, reason = "quarantined", f"heartbeat ok ratio {ratio:.2f}"
        elif not fresh:
            tier, reason = "unknown", "every scenario observation is past fresh_until"
        elif ratio is not None and ratio < DEGRADED_HEARTBEAT_RATIO:
            tier, reason = "degraded", f"heartbeat ok ratio {ratio:.2f}"
        elif unavailable and len(unavailable) == len(fresh):
            tier, reason = "degraded", "every fresh scenario is unavailable"
        elif unavailable:
            tier, reason = (
                "usable",
                f"{len(unavailable)} of {len(fresh)} fresh scenarios unavailable",
            )
        elif meets_floor is False:
            tier, reason = "usable", "throughput below floor"
        else:
            tier, reason = (
                "preferred",
                "heartbeat healthy and fresh scenarios available",
            )

        return {
            "lane_id": lane_id,
            "tier": tier,
            "reason": reason,
            "heartbeat": heartbeat,
            "fresh_scenarios": sorted(fresh),
            "stale_scenarios": sorted(stale),
            "unavailable_scenarios": sorted(unavailable),
            "meets_throughput_floor": meets_floor,
            "throughput_mibps": throughput,
            "performance_band": performance_band(throughput),
            # Descriptive, like the performance band. A lane serving the wrong
            # region is not demoted: the contract says a mismatch matters only
            # when a scenario explicitly requires that region.
            "region": self.region_stats(lane_id, requested_region),
        }

    def prune(self, artifact_root: Path, retention_days: int, max_bytes: int) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        with self._lock, self.db:
            self.db.execute("DELETE FROM heartbeats WHERE observed_at<?", (cutoff,))
            expired = self.db.execute(
                "SELECT id FROM run_groups WHERE status='complete' AND finished_at<? ORDER BY finished_at",
                (cutoff,),
            ).fetchall()
            for row in expired:
                self._delete_group(row["id"], artifact_root)
            while tree_size(artifact_root.parent) > max_bytes:
                oldest = self.db.execute(
                    "SELECT id FROM run_groups WHERE status='complete' ORDER BY finished_at LIMIT 1"
                ).fetchone()
                if not oldest:
                    break
                self._delete_group(oldest["id"], artifact_root)

    def _delete_group(self, group_id: str, artifact_root: Path) -> None:
        import shutil

        shutil.rmtree(artifact_root / group_id, ignore_errors=True)
        self.db.execute("DELETE FROM run_groups WHERE id=?", (group_id,))

    def _insert_observation(
        self, group_id: str, lane_id: str, scenario_id: str, observation: dict[str, Any]
    ) -> None:
        clean = sanitize_observation(observation)
        self.db.execute(
            "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?)",
            (
                clean["observation_id"],
                group_id,
                lane_id,
                scenario_id,
                clean["observed_at"],
                clean["fresh_until"],
                json.dumps(clean, separators=(",", ":")),
            ),
        )


class QueueFull(Exception):
    pass


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sanitize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    clean = json.loads(json.dumps(observation))
    clean["artifacts"] = [
        {
            "kind": item.get("kind", "unknown"),
            "path": Path(str(item.get("path", ""))).name,
        }
        for item in clean.get("artifacts", [])
    ]
    return clean


def unknown_observation(
    lane: Mapping[str, Any],
    scenario_id: str,
    now: datetime,
    runtime: str = "podman",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observation_id": str(uuid.uuid4()),
        "observed_at": now.isoformat(),
        "fresh_until": (now + timedelta(hours=24)).isoformat(),
        "scenario_id": scenario_id,
        "probe": {"name": "probe-worker", "version": "1", "execution": "local"},
        "subject": {
            "instance_id": lane["instance_id"],
            "node_id": lane["node_id"],
            "runtime": runtime,
            "image_identity": lane["image_identity"],
            "config_digest": lane["config_digest"],
        },
        "lane": {
            "composition": lane["composition"],
            "transport": lane["transport"],
            "substrate_profile": lane.get("substrate_profile"),
            "requested_region": lane.get("requested_region"),
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


def tree_size(path: Path) -> int:
    return (
        sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        if path.exists()
        else 0
    )
