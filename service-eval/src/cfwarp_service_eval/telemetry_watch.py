from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import httpx

from .api import read_token


EXPORTED = re.compile(
    r'^otelcol_exporter_sent_metric_points(?:_total)?\{[^}]*exporter(?:_name)?="datadog"[^}]*\}\s+([0-9.eE+-]+)$'
)


def exported_points(body: str) -> float:
    total = 0.0
    matched = False
    for line in body.splitlines():
        match = EXPORTED.match(line)
        if match:
            total += float(match.group(1))
            matched = True
    if not matched:
        raise ValueError("collector did not expose the Datadog exported-point counter")
    return total


def read_previous(path: Path) -> float | None:
    if not path.exists():
        return None
    return float(json.loads(path.read_text(encoding="utf-8"))["exported_points"])


def write_previous(path: Path, count: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"exported_points": count}) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def tick(
    collector: httpx.Client,
    observer: httpx.Client,
    state_file: Path,
) -> bool:
    response = collector.get("/metrics")
    response.raise_for_status()
    current = exported_points(response.text)
    previous = read_previous(state_file)
    if previous is not None and current > previous:
        submitted = observer.post("/v2/telemetry-export-heartbeat")
        submitted.raise_for_status()
        write_previous(state_file, current)
        return True
    write_previous(state_file, current)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Confirm collector export progress back to the cfwarp observer"
    )
    parser.add_argument("--collector-url", required=True)
    parser.add_argument("--observer-url", required=True)
    parser.add_argument("--metrics-token-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    token = read_token(args.metrics_token_file, "metrics")
    with (
        httpx.Client(
            base_url=args.collector_url, timeout=10, follow_redirects=False
        ) as collector,
        httpx.Client(
            base_url=args.observer_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            follow_redirects=False,
        ) as observer,
    ):
        while True:
            tick(collector, observer, args.state_file)
            if args.once:
                return 0
            time.sleep(max(10, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
