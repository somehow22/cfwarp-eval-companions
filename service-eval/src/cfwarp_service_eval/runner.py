from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from .config import DIRECT_THROUGHPUT_FLOOR_MIBPS, SCENARIO_DEFINITIONS, Lane


class ProbeError(RuntimeError):
    pass


class ProbeRunner:
    def __init__(
        self,
        artifact_root: Path,
        deadline_seconds: int = 180,
        perf_transfer_bytes: int = 25 * 1024 * 1024,
        perf_runs: int = 3,
        browser_execution: str = "disabled",
    ):
        self.artifact_root = artifact_root
        self.deadline_seconds = deadline_seconds
        self.perf_transfer_bytes = perf_transfer_bytes
        self.perf_runs = perf_runs
        self.browser_execution = browser_execution

    async def preflight(self, group_id: str, lane: Lane) -> dict[str, Any]:
        output = self.artifact_root / group_id / lane.id / "preflight"
        output.mkdir(parents=True, exist_ok=True)
        checks: dict[str, Any] = {}
        commands = {
            "trace": [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--proxy",
                lane.proxy,
                "https://www.cloudflare.com/cdn-cgi/trace",
            ],
            "ipv4": [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--proxy",
                lane.proxy,
                "--connect-to",
                "one.one.one.one:443:1.1.1.1:443",
                "https://one.one.one.one/cdn-cgi/trace",
            ],
            "ipv6": [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "--proxy",
                lane.proxy,
                "--connect-to",
                "one.one.one.one:443:[2606:4700:4700::1111]:443",
                "https://one.one.one.one/cdn-cgi/trace",
            ],
        }
        for name, command in commands.items():
            stdout = await self._run(command, 40)
            fields = parse_trace(stdout)
            checks[name] = {
                "ok": fields.get("warp") == "on",
                "warp": fields.get("warp"),
                "loc": fields.get("loc"),
                "colo": fields.get("colo"),
            }
        result = {
            "schema_version": 1,
            "lane_id": lane.id,
            "checks": checks,
            "ok": all(item["ok"] for item in checks.values()),
        }
        (output / "summary.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return result

    async def run(self, group_id: str, lane: Lane, scenario_id: str) -> dict[str, Any]:
        output = self.artifact_root / group_id / lane.id / scenario_id
        output.mkdir(parents=True, exist_ok=True)
        common = [
            "--proxy",
            lane.proxy,
            "--output",
            str(output),
            "--instance-id",
            lane.instance_id,
            "--image-identity",
            lane.image_identity,
            "--config-digest",
            lane.config_digest,
            "--node-id",
            lane.node_id,
            "--runtime",
            "podman",
            "--composition",
            lane.composition,
            "--transport",
            lane.transport,
        ]
        if lane.substrate_profile:
            common += ["--substrate-profile", lane.substrate_profile]
        if lane.requested_region:
            common += ["--requested-region", lane.requested_region]
        if scenario_id == "youtube":
            command = [
                "cfwarp-service-eval",
                "youtube",
                *common,
                "--deadline-seconds",
                str(self.deadline_seconds),
            ]
        elif scenario_id == "perf":
            command = [
                "cfwarp-service-eval",
                "perf",
                *common,
                "--transfer-bytes",
                str(self.perf_transfer_bytes),
                "--runs",
                str(self.perf_runs),
            ]
            if lane.composition == "direct-warp":
                command += ["--floor-mibps", str(DIRECT_THROUGHPUT_FLOOR_MIBPS)]
        else:
            if self.browser_execution == "disabled":
                raise ProbeError("browser scenario is disabled on this runtime")
            command = [
                "deno",
                "task",
                "--config",
                "browser/deno.json",
                "probe",
                "--service",
                scenario_id,
                *common,
                "--deadline-seconds",
                str(min(self.deadline_seconds, 300)),
                "--capture-screenshot",
                "false",
            ]
            if self.browser_execution == "agentcore":
                command += ["--browser-provider", "agentcore"]
        await self._run(command, self.deadline_seconds + 15, check=False)
        enforce_artifact_limit(
            output,
            int(SCENARIO_DEFINITIONS[scenario_id]["artifact_limit_bytes"]),
        )
        summary_path = output / "summary.json"
        if not summary_path.is_file():
            raise ProbeError("probe exited without a summary")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        observation = summary.get("observation")
        if not isinstance(observation, dict) or observation.get("schema_version") != 1:
            raise ProbeError("probe summary lacks Observation v1")
        return observation

    async def _run(self, command: list[str], timeout: int, check: bool = True) -> str:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=safe_environment(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError as error:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise ProbeError("probe subprocess deadline exceeded") from error
        if check and process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace")[-300:]
            raise ProbeError(f"probe subprocess failed: {redact(message)}")
        return stdout.decode("utf-8", errors="replace")[:65536]


def safe_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "AGENT_BROWSER_EXECUTABLE_PATH",
        "CFWARP_EVAL_CONTRACTS_ROOT",
        "AWS_PROFILE",
        "AGENTCORE_REGION",
        "AGENTCORE_BROWSER_ID",
        "AGENTCORE_SESSION_TIMEOUT",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["HOME"] = os.environ.get("AGENT_BROWSER_RUNTIME_HOME", "/tmp")
    env["AGENT_BROWSER_DOWNLOADS_DISABLED"] = "1"
    return env


def parse_trace(body: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in body.splitlines() if "=" in line)


def redact(message: str) -> str:
    return " ".join(part for part in message.split() if "://" not in part)[:300]


def enforce_artifact_limit(output: Path, limit_bytes: int) -> None:
    total = sum(
        path.stat().st_size
        for path in output.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if total > limit_bytes:
        raise ProbeError(
            f"scenario artifacts exceed contract limit: {total} > {limit_bytes} bytes"
        )
