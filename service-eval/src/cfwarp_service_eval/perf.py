from __future__ import annotations

import json
import statistics
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SPEED_URL = "https://speed.cloudflare.com/__down"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
MIB = 1024 * 1024


@dataclass
class PerfConfig:
    proxy: str | None
    output: Path
    transfer_bytes: int = 25 * MIB
    runs: int = 3
    floor_mibps: float | None = None
    timeout_seconds: float = 60.0
    instance_id: str | None = None
    image_identity: str | None = None
    config_digest: str | None = None
    node_id: str | None = None
    runtime: str | None = None
    composition: str | None = None
    transport: str | None = None
    substrate_profile: str | None = None
    requested_region: str | None = None


def curl(config: PerfConfig, url: str, write_out: str | None) -> str:
    command = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        str(int(config.timeout_seconds)),
    ]
    if config.proxy:
        command += ["--proxy", config.proxy]
    if write_out:
        command += ["--output", "/dev/null", "--write-out", write_out]
    command.append(url)
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=config.timeout_seconds + 10
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode}")
    return result.stdout


def read_trace(config: PerfConfig) -> dict[str, str]:
    body = curl(config, TRACE_URL, None)
    return dict(line.split("=", 1) for line in body.splitlines() if "=" in line)


def sample(config: PerfConfig, index: int) -> dict[str, Any]:
    url = f"{SPEED_URL}?bytes={config.transfer_bytes}&run={index}"
    body = curl(config, url, "%{speed_download} %{time_total} %{size_download}")
    speed, elapsed, size = body.split()
    return {
        "run": index,
        "mibps": round(float(speed) / MIB, 3),
        "seconds": round(float(elapsed), 3),
        "bytes": int(float(size)),
    }


def run_probe(config: PerfConfig) -> tuple[dict[str, Any], int]:
    config.output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "input": {
            "transfer_bytes": config.transfer_bytes,
            "runs": config.runs,
            "floor_mibps": config.floor_mibps,
            "instance_id": config.instance_id,
            "image_identity": config.image_identity,
            "config_digest": config.config_digest,
            "node_id": config.node_id,
            "runtime": config.runtime,
            "composition": config.composition,
            "transport": config.transport,
            "substrate_profile": config.substrate_profile,
            "requested_region": config.requested_region,
        },
        "samples": [],
    }

    try:
        trace = read_trace(config)
    except Exception as error:
        trace = {}
        summary["trace_error"] = type(error).__name__
    summary["trace"] = {
        "warp": trace.get("warp"),
        "location": trace.get("loc"),
        "colo": trace.get("colo"),
    }

    if trace.get("warp") != "on":
        # A throughput number measured off-lane is not evidence about the lane.
        return finish(config, summary, "listener_not_on_warp", "warp-core")

    for index in range(1, config.runs + 1):
        try:
            summary["samples"].append(sample(config, index))
        except Exception as error:
            summary["sample_error"] = type(error).__name__
            return finish(config, summary, "tooling_failure", "tooling")

    speeds = [item["mibps"] for item in summary["samples"]]
    summary["throughput_mibps"] = {
        "min": min(speeds),
        "median": round(statistics.median(speeds), 3),
        "mean": round(statistics.mean(speeds), 3),
        "max": max(speeds),
    }
    if config.floor_mibps is None:
        return finish(config, summary, "pass", None)
    meets = summary["throughput_mibps"]["median"] >= config.floor_mibps
    return finish(config, summary, "pass" if meets else "degraded_throughput", None)


def finish(
    config: PerfConfig, summary: dict[str, Any], verdict: str, failure_layer: str | None
) -> tuple[dict[str, Any], int]:
    finished = datetime.now(timezone.utc)
    summary["verdict"] = verdict
    summary["failure_layer"] = failure_layer or "none"
    summary["finished_at"] = finished.isoformat()
    summary["elapsed_ms"] = round(
        (finished - datetime.fromisoformat(summary["started_at"])).total_seconds()
        * 1000
    )
    summary["observation"] = build_observation(summary, config, finished)
    summary_path = config.output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    throughput = summary.get("throughput_mibps") or {}
    trace = summary["trace"]
    lines = [
        f"Service verdict: {verdict}",
        f"Failure layer: {summary['failure_layer']}",
        f"Trace: warp={trace.get('warp') or 'unknown'} loc={trace.get('location') or 'unknown'} colo={trace.get('colo') or 'unknown'}",
        f"Throughput MiB/s: median={throughput.get('median', 'n/a')} min={throughput.get('min', 'n/a')}",
        f"Floor MiB/s: {config.floor_mibps if config.floor_mibps is not None else 'evidence-only'}",
        f"Summary: {summary_path}",
    ]
    (config.output / "verdict.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return summary, 0 if verdict == "pass" else 2


def build_observation(
    summary: dict[str, Any], config: PerfConfig, observed_at: datetime
) -> dict[str, Any]:
    verdict = summary["verdict"]
    if verdict == "pass":
        availability, eligible = "available", True
    elif verdict in {"tooling_failure", "unknown"}:
        availability, eligible = "unknown", False
    else:
        availability, eligible = "unavailable", True
    throughput = summary.get("throughput_mibps") or {}
    median = throughput.get("median")
    meets_floor = None
    if config.floor_mibps is not None and median is not None:
        meets_floor = median >= config.floor_mibps
    inputs = summary["input"]
    trace = summary["trace"]
    return {
        "schema_version": 1,
        "observation_id": str(uuid.uuid4()),
        "observed_at": observed_at.isoformat(),
        "fresh_until": (observed_at + timedelta(hours=24)).isoformat(),
        "scenario_id": "perf.throughput_sample",
        "probe": {"name": "perf-curl", "version": "1", "execution": "local"},
        "subject": {
            "instance_id": inputs.get("instance_id"),
            "node_id": inputs.get("node_id"),
            "runtime": inputs.get("runtime"),
            "image_identity": inputs.get("image_identity"),
            "config_digest": inputs.get("config_digest"),
        },
        "lane": {
            "composition": inputs.get("composition"),
            "transport": inputs.get("transport"),
            "substrate_profile": inputs.get("substrate_profile"),
            "requested_region": inputs.get("requested_region"),
        },
        "egress": {
            "warp": trace.get("warp"),
            "region": trace.get("location"),
            "colo": trace.get("colo"),
        },
        "result": {
            "availability": availability,
            "class": verdict,
            "eligible": eligible,
        },
        "confidence_stage": "single_observation",
        "failure_layer": summary["failure_layer"],
        "latency_ms": summary["elapsed_ms"],
        # Additive optional field; the contract allows these without a version
        # bump. Tier derivation reads meets_floor.
        "perf": {
            "throughput_mibps": median,
            "floor_mibps": config.floor_mibps,
            "meets_floor": meets_floor,
            "transfer_bytes": config.transfer_bytes,
            "runs": len(summary["samples"]),
        },
        "artifacts": [
            {"kind": "summary", "path": "summary.json"},
            {"kind": "verdict", "path": "verdict.txt"},
        ],
    }
