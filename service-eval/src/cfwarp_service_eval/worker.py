from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import shutil
import socket
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .api import read_token
from .config import Lane
from .provenance import evaluator_build
from .runner import ProbeRunner
from .store import tree_size

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def tailnet_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address in (TAILNET_V4 if address.version == 4 else TAILNET_V6)


def tailnet_listener_endpoint(proxy: str) -> str | None:
    """Pin a declared Tailnet proxy to an address safe for the browser worker.

    A host name may resolve to a Tailnet address while it is checked and then
    resolve to a public address when the browser process connects. Resolve it
    once here, require every returned address to be Tailnet-private, and pass
    the selected literal address to the runner instead of the host name.
    """
    try:
        parsed = urlsplit(proxy)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "socks5h"
        or host is None
        or port is None
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None)
            }
        except (OSError, ValueError):
            return None
        if not addresses or not all(tailnet_address(item) for item in addresses):
            return None
        address = min(addresses, key=lambda item: (item.version, int(item)))
    if not tailnet_address(address):
        return None
    literal = str(address)
    formatted_host = f"[{literal}]" if address.version == 6 else literal
    return f"socks5h://{formatted_host}:{port}"


def private_listener(proxy: str) -> bool:
    """Compatibility predicate for Tailnet listener validation."""
    return tailnet_listener_endpoint(proxy) is not None


class Worker:
    def __init__(self) -> None:
        self.worker_class = os.environ.get("CFWARP_WORKER_CLASS", "light")
        if self.worker_class not in {"light", "perf", "browser"}:
            raise ValueError("CFWARP_WORKER_CLASS must be light, perf, or browser")
        self.worker_id = os.environ.get(
            "CFWARP_WORKER_ID", f"{self.worker_class}-{uuid.uuid4()}"
        )
        self.node_id = os.environ.get("CFWARP_WORKER_NODE_ID", socket.gethostname())
        self.api_base = os.environ.get("CFWARP_OBSERVER_URL", "http://127.0.0.1:8080")
        token_file = Path(
            os.environ.get(
                "CFWARP_WORKER_TOKEN_FILE", "/run/secrets/cfwarp-probe-worker-token"
            )
        )
        self.token = read_token(token_file, "worker")
        self.interval = int(os.environ.get("CFWARP_WORKER_POLL_SECONDS", "15"))
        self.heartbeat_interval = int(
            os.environ.get("CFWARP_WORKER_HEARTBEAT_SECONDS", "60")
        )
        self.lease_seconds = int(os.environ.get("CFWARP_WORKER_LEASE_SECONDS", "240"))
        self.max_artifact_bytes = int(
            os.environ.get("CFWARP_WORKER_MAX_ARTIFACT_BYTES", str(512 * 1024 * 1024))
        )
        artifact_root = Path(
            os.environ.get(
                "CFWARP_WORKER_ARTIFACT_ROOT",
                f"/var/lib/cfwarp-eval-{self.worker_class}",
            )
        )
        self.runner = ProbeRunner(
            artifact_root,
            int(os.environ.get("CFWARP_WORKER_DEADLINE_SECONDS", "180")),
            browser_execution=(
                os.environ.get("SERVICE_EVAL_BROWSER_EXECUTION", "local")
                if self.worker_class == "browser"
                else "disabled"
            ),
        )
        self._last_heartbeat = 0.0

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def identity(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worker_class": self.worker_class,
            "node_id": self.node_id,
            "evaluator_build": evaluator_build(),
            "metadata": {"artifact_limit_bytes": self.max_artifact_bytes},
        }

    async def run_forever(self) -> None:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(
            base_url=self.api_base,
            headers=self.headers,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            while True:
                await self.tick(client)
                await asyncio.sleep(self.interval)

    async def tick(self, client: httpx.AsyncClient) -> None:
        now = asyncio.get_running_loop().time()
        if (
            self.worker_class == "light"
            and now - self._last_heartbeat >= self.heartbeat_interval
        ):
            await self.submit_heartbeats(client)
            self._last_heartbeat = now
        response = await client.post(
            "/v2/jobs/claim",
            json={**self.identity(), "lease_seconds": self.lease_seconds},
        )
        response.raise_for_status()
        job = response.json()["job"]
        if job is None:
            heartbeat = await client.post("/v2/workers/heartbeat", json=self.identity())
            heartbeat.raise_for_status()
            return
        lane_payload = dict(job["lane"])
        lane_payload.pop("capability_id", None)
        lane = Lane(**lane_payload)
        if self.worker_class == "browser":
            pinned_proxy = tailnet_listener_endpoint(lane.proxy)
            if pinned_proxy is None:
                await self.fail(
                    client, job, "browser worker rejected non-private listener"
                )
                return
            lane = replace(lane, proxy=pinned_proxy)
        try:
            observation = await self.runner.run(
                job["group_id"], lane, job["scenario_id"]
            )
            result = await client.post(
                f"/v2/jobs/{job['task_id']}/complete",
                json={
                    "lease_token": job["lease_token"],
                    "observation": observation,
                },
            )
            result.raise_for_status()
        except Exception as error:
            await self.fail(client, job, type(error).__name__)
        finally:
            prune_artifacts(self.runner.artifact_root, self.max_artifact_bytes)

    async def submit_heartbeats(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v2/internal/lanes")
        response.raise_for_status()
        for payload in response.json():
            payload.pop("capability_id", None)
            lane = Lane(**payload)
            try:
                result = await self.runner.preflight("heartbeat", lane)
            except Exception as error:
                result = {"ok": False, "error": type(error).__name__}
            submitted = await client.post(
                "/v2/heartbeats", json={"lane_id": lane.id, "result": result}
            )
            submitted.raise_for_status()

    async def fail(
        self, client: httpx.AsyncClient, job: dict[str, Any], error: str
    ) -> None:
        response = await client.post(
            f"/v2/jobs/{job['task_id']}/fail",
            json={"lease_token": job["lease_token"], "error": error[:300]},
        )
        if response.status_code not in {200, 409}:
            response.raise_for_status()


def prune_artifacts(root: Path, max_bytes: int) -> None:
    if not root.exists() or tree_size(root) <= max_bytes:
        return
    directories = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    for directory in directories:
        if tree_size(root) <= max_bytes:
            break
        shutil.rmtree(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description="cfwarp leased evaluation worker")
    parser.add_argument(
        "--once", action="store_true", help="claim at most one job and exit"
    )
    args = parser.parse_args()
    worker = Worker()
    if not args.once:
        asyncio.run(worker.run_forever())
        return

    async def once() -> None:
        async with httpx.AsyncClient(
            base_url=worker.api_base,
            headers=worker.headers,
            timeout=30,
            follow_redirects=False,
        ) as client:
            await worker.tick(client)

    asyncio.run(once())
