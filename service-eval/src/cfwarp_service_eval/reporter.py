from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .api import read_token


@dataclass(frozen=True)
class Observer:
    node_id: str
    deployment_origin: str
    url: str
    token_file: Path


@dataclass(frozen=True)
class ReporterConfig:
    observers: tuple[Observer, ...]
    linear_project_id: str
    linear_team_id: str
    linear_token_file: Path
    state_db: Path
    output: Path
    migration_complete: bool
    alerts_file: Path | None
    remediation_file: Path | None

    @classmethod
    def load(cls, path: Path) -> "ReporterConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported reporter config")
        observers = tuple(
            Observer(
                node_id=item["node_id"],
                deployment_origin=item["deployment_origin"],
                url=item["url"].rstrip("/"),
                token_file=Path(item["token_file"]),
            )
            for item in raw["observers"]
        )
        if not observers:
            raise ValueError("reporter requires at least one observer")
        return cls(
            observers=observers,
            linear_project_id=raw["linear_project_id"],
            linear_team_id=raw["linear_team_id"],
            linear_token_file=Path(raw["linear_token_file"]),
            state_db=Path(raw["state_db"]),
            output=Path(raw["output"]),
            migration_complete=bool(raw.get("migration_complete", False)),
            alerts_file=Path(raw["alerts_file"]) if raw.get("alerts_file") else None,
            remediation_file=(
                Path(raw["remediation_file"]) if raw.get("remediation_file") else None
            ),
        )


class ReporterStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS reports (
                  generated_at TEXT PRIMARY KEY,health TEXT NOT NULL,
                  report TEXT NOT NULL
                )
                """
            )

    def save(self, health: str, report: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO reports VALUES(?,?,?)",
                (
                    report["generated_at"],
                    health,
                    json.dumps(report, separators=(",", ":")),
                ),
            )
            cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            self.db.execute("DELETE FROM reports WHERE generated_at<?", (cutoff,))

    def rolling(self, days: int = 14) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT report FROM reports WHERE generated_at>=? ORDER BY generated_at",
            (cutoff,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]


class LinearClient:
    def __init__(self, token: str, transport: httpx.Client | None = None):
        self.token = token
        self.transport = transport or httpx.Client(
            base_url="https://api.linear.app",
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=30,
            follow_redirects=False,
        )

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self.transport.post(
            "/graphql", json={"query": query, "variables": variables}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            messages = "; ".join(str(item.get("message")) for item in payload["errors"])
            raise RuntimeError(f"Linear GraphQL failed: {messages[:300]}")
        return payload["data"]

    def status_update(self, project_id: str, body: str, health: str) -> str:
        data = self.graphql(
            """
            mutation CfwarpProjectUpdate($input: ProjectUpdateCreateInput!) {
              projectUpdateCreate(input: $input) {
                success
                projectUpdate { id }
              }
            }
            """,
            {"input": {"projectId": project_id, "body": body, "health": health}},
        )
        result = data["projectUpdateCreate"]
        if not result["success"]:
            raise RuntimeError("Linear rejected project status update")
        return str(result["projectUpdate"]["id"])

    def classified_incident(
        self, team_id: str, project_id: str, incident: dict[str, Any]
    ) -> str:
        data = self.graphql(
            """
            mutation CfwarpIncident($input: IssueCreateInput!) {
              issueCreate(input: $input) { success issue { id identifier } }
            }
            """,
            {
                "input": {
                    "teamId": team_id,
                    "projectId": project_id,
                    "title": incident["title"],
                    "description": incident["description"],
                    "priority": int(incident.get("priority", 2)),
                }
            },
        )
        result = data["issueCreate"]
        if not result["success"]:
            raise RuntimeError("Linear rejected classified incident")
        return str(result["issue"]["identifier"])


def get_json(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def collect(config: ReporterConfig) -> tuple[dict[str, Any], list[str]]:
    generated_at = datetime.now(timezone.utc).isoformat()
    sources = []
    platform = []
    egresses: list[dict[str, Any]] = []
    failures = []
    scenario_counts: dict[str, dict[str, int]] = {}
    for observer in config.observers:
        token = read_token(observer.token_file, f"observer {observer.node_id}")
        try:
            with httpx.Client(
                base_url=observer.url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                follow_redirects=False,
            ) as client:
                snapshot = get_json(client, "/v2/platform-slo")
                latest = get_json(client, "/v2/observations/latest")
                lanes = get_json(client, "/v2/lanes")
                scenarios = get_json(client, "/v2/scenarios")
                latest_by_cell = {
                    (
                        item.get("lane", {}).get("lane_id"),
                        item.get("scenario_id"),
                    ): item
                    for item in latest
                }
                for lane in lanes:
                    for scenario_id in lane.get("scenarios", []):
                        counts = scenario_counts.setdefault(
                            scenario_id,
                            {"available": 0, "unavailable": 0, "unknown": 0},
                        )
                        observation = latest_by_cell.get((lane["id"], scenario_id))
                        availability = observation_availability(
                            observation, generated_at
                        )
                        counts[availability] += 1
                for scenario in scenarios:
                    egresses.extend(
                        get_json(client, f"/v2/egresses?scenario={scenario['id']}")
                    )
        except Exception as error:
            failures.append(f"{observer.node_id}: {type(error).__name__}")
            snapshot = {
                "observer_up": False,
                "node_id": observer.node_id,
                "deployment_origin": observer.deployment_origin,
            }
            latest = []
        snapshot["node_id"] = observer.node_id
        snapshot["deployment_origin"] = observer.deployment_origin
        platform.append(snapshot)
        evaluator_builds = sorted(
            {
                str(item.get("subject", {}).get("evaluator_build"))
                for item in latest
                if item.get("subject", {}).get("evaluator_build")
            }
        )
        config_generations = sorted(
            {
                str(item.get("subject", {}).get("config_generation"))
                for item in latest
                if item.get("subject", {}).get("config_generation")
            }
        )
        sources.append(
            {
                "node_id": observer.node_id,
                "deployment_origin": observer.deployment_origin,
                "observer_build": snapshot.get("observer_build", "unavailable"),
                "observation_schema": 2,
                "evaluator_builds": evaluator_builds,
                "config_generations": config_generations,
            }
        )
    remediation = load_json_array(config.remediation_file, "remediation file")
    return (
        {
            "schema_version": 1,
            "generated_at": generated_at,
            "sources": sources,
            "platform_slo": {"nodes": platform},
            "scenarios": scenario_counts,
            "egresses": egresses,
            "remediation": remediation,
        },
        failures,
    )


def load_alerts(path: Path | None) -> list[dict[str, Any]]:
    return load_json_array(path, "alerts file")


def load_json_array(path: Path | None, name: str) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{name} must be an array")
    return raw


def observation_availability(observation: dict[str, Any] | None, now: str) -> str:
    if observation is None or observation.get("schema_version") != 2:
        return "unknown"
    try:
        fresh = datetime.fromisoformat(
            str(observation["fresh_until"]).replace("Z", "+00:00")
        ) > datetime.fromisoformat(now.replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "unknown"
    result = observation.get("result") or {}
    availability = result.get("availability")
    if not fresh or availability not in {"available", "unavailable"}:
        return "unknown"
    return availability if result.get("eligible") is True else "unknown"


def health_for(
    report: dict[str, Any],
    failures: list[str],
    alerts: list[dict[str, Any]],
    migration_complete: bool,
) -> str:
    nodes = report["platform_slo"]["nodes"]
    if failures or any(not node.get("observer_up") for node in nodes):
        return "offTrack"
    if any(node.get("hard_warp_off", 0) for node in nodes):
        return "offTrack"
    if any(node.get("deployment_inventory_mismatch", 0) for node in nodes):
        return "offTrack"
    if any(
        node.get("telemetry_export_age_seconds") is None
        or node["telemetry_export_age_seconds"] > 900
        for node in nodes
    ):
        return "offTrack"
    if any(
        node.get("last_sweep_age_seconds") is None
        or node["last_sweep_age_seconds"] > 86_400
        for node in nodes
    ):
        return "offTrack"
    if alerts or not migration_complete:
        return "atRisk"
    return "onTrack"


def render_daily(
    report: dict[str, Any],
    health: str,
    failures: list[str],
    alerts: list[dict[str, Any]],
) -> str:
    nodes = report["platform_slo"]["nodes"]
    expected = sum(int(node.get("expected_cells", 0)) for node in nodes)
    fresh = sum(int(node.get("fresh_cells", 0)) for node in nodes)
    active = sum(int(node.get("active_lane_count", 0)) for node in nodes)
    scenario_lines = [
        f"- `{scenario}`: {counts['available']} available / {counts['unavailable']} unavailable / {counts['unknown']} unknown"
        for scenario, counts in sorted(report["scenarios"].items())
    ] or ["- No scenario evidence returned."]
    alert_lines = [
        f"- {item.get('name', 'unnamed alert')}: {item.get('state', 'unknown')}"
        for item in alerts
    ]
    if failures:
        alert_lines.extend(
            f"- observer collection failure: {failure}" for failure in failures
        )
    if not alert_lines:
        alert_lines = ["- No unresolved alerts supplied by the classified alert feed."]
    versions = ", ".join(
        f"{item['node_id']} observer={item['observer_build']} evaluators={','.join(item['evaluator_builds']) or 'none'}"
        for item in report["sources"]
    )
    remediation = report.get("remediation", [])
    remediation_lines = [
        f"- {item.get('node_id', 'unknown')}/{item.get('lane_id', 'unknown')}: {item.get('outcome', 'unknown')} ({item.get('attempts_used', 0)} attempts)"
        for item in remediation[-10:]
    ] or ["- No persisted remediation activity supplied for this interval."]
    return "\n".join(
        [
            f"## cfwarp daily observation — {report['generated_at']}",
            "",
            f"Health: **{health}**",
            f"Inventory: **{active} active lanes** across {len(nodes)} observer nodes.",
            f"Platform evidence: **{fresh}/{expected} fresh cells**.",
            "",
            "### Availability by exact scenario",
            *scenario_lines,
            "",
            "### Alerts and drift",
            *alert_lines,
            "",
            "### Bounded remediation activity",
            *remediation_lines,
            "",
            f"Source builds and generations: {versions}.",
            "SQLite remains canonical; Datadog and this Linear update are projections.",
        ]
    )


def render_weekly(
    reports: list[dict[str, Any]], latest: dict[str, Any], health: str
) -> str:
    nodes = [node for report in reports for node in report["platform_slo"]["nodes"]]
    completeness = [float(node.get("completeness", 0)) for node in nodes]
    max_store = max((int(node.get("store_bytes", 0)) for node in nodes), default=0)
    max_artifacts = max(
        (int(node.get("artifact_bytes", 0)) for node in nodes), default=0
    )
    failures = sorted(
        {failure for node in nodes for failure in node.get("background_failures", [])}
    )
    first = reports[0] if reports else latest
    changes = []
    for scenario in sorted(set(first.get("scenarios", {})) | set(latest["scenarios"])):
        before = first.get("scenarios", {}).get(
            scenario, {"available": 0, "unavailable": 0, "unknown": 0}
        )
        after = latest["scenarios"].get(
            scenario, {"available": 0, "unavailable": 0, "unknown": 0}
        )
        changes.append(
            f"- `{scenario}`: available {after['available'] - before['available']:+d}, "
            f"unavailable {after['unavailable'] - before['unavailable']:+d}, "
            f"unknown {after['unknown'] - before['unknown']:+d}"
        )
    unresolved = sum(
        counts["unknown"] for counts in latest.get("scenarios", {}).values()
    )
    return "\n".join(
        [
            f"## cfwarp rolling 14-day platform SLO — {latest['generated_at']}",
            "",
            f"Health: **{health}** from {len(reports)} retained reporter samples.",
            f"Completeness range: **{min(completeness, default=0):.3f}–{max(completeness, default=0):.3f}**.",
            f"Maximum SQLite size: **{max_store} bytes**; maximum worker artifacts: **{max_artifacts} bytes**.",
            f"Recurring background failures: {', '.join(failures) if failures else 'none'}.",
            f"Current unresolved exact-scenario cells: **{unresolved}**.",
            "",
            "### Change across retained 14-day samples",
            *(changes or ["- No retained comparison sample yet."]),
            "",
            "No aggregate pass is inferred from these exact-scenario counts.",
            "A release verdict requires this rolling review; a 24-hour canary closes only its migration slice.",
        ]
    )


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate observers and report to Linear"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--classified-incident", type=Path)
    args = parser.parse_args()
    config = ReporterConfig.load(args.config)
    if args.classified_incident:
        linear = LinearClient(read_token(config.linear_token_file, "Linear reporter"))
        incident = json.loads(args.classified_incident.read_text(encoding="utf-8"))
        if incident.get("classification") != "platform_incident":
            raise ValueError(
                "incident file must be explicitly classified platform_incident"
            )
        identifier = linear.classified_incident(
            config.linear_team_id, config.linear_project_id, incident
        )
        print(identifier)
        return 0
    report, failures = collect(config)
    alerts = load_alerts(config.alerts_file)
    health = health_for(report, failures, alerts, config.migration_complete)
    store = ReporterStore(config.state_db)
    store.save(health, report)
    atomic_write(config.output, report)
    body = (
        render_weekly(store.rolling(14), report, health)
        if args.weekly
        else render_daily(report, health, failures, alerts)
    )
    if args.dry_run:
        print(body)
        return 0
    linear = LinearClient(read_token(config.linear_token_file, "Linear reporter"))
    update_id = linear.status_update(config.linear_project_id, body, health)
    print(update_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
