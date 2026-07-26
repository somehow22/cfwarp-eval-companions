from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .perf import PerfConfig
from .perf import run_probe as run_perf_probe
from .youtube import DEFAULT_SOURCE_URL, YouTubeConfig, run_probe


PROVENANCE = (
    "--instance-id",
    "--image-identity",
    "--config-digest",
    "--node-id",
    "--runtime",
    "--composition",
    "--transport",
    "--substrate-profile",
    "--requested-region",
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="cfwarp-service-eval",
        description="Run bounded, structured service verdict probes.",
    )
    subcommands = root.add_subparsers(dest="service", required=True)
    youtube = subcommands.add_parser(
        "youtube", help="evaluate anonymous public-video access through yt-dlp"
    )
    youtube.add_argument(
        "--proxy",
        help="HTTP or SOCKS proxy listener; omit only for a direct WARP host path",
    )
    youtube.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    youtube.add_argument(
        "--video-url",
        help="pin a video after deterministic discovery for repeated runs",
    )
    youtube.add_argument("--output", type=Path)
    youtube.add_argument("--attempts", type=int, default=2, choices=range(1, 4))
    youtube.add_argument("--timeout-seconds", type=float, default=25.0)
    youtube.add_argument("--deadline-seconds", type=float, default=120.0)
    youtube.add_argument("--transfer-bytes", type=int, default=262_144)
    for flag in PROVENANCE:
        youtube.add_argument(flag)

    perf = subcommands.add_parser(
        "perf", help="sample bounded lane throughput as a routine observation"
    )
    perf.add_argument("--proxy")
    perf.add_argument("--output", type=Path)
    perf.add_argument("--transfer-bytes", type=int, default=25 * 1024 * 1024)
    perf.add_argument("--runs", type=int, default=3, choices=range(1, 11))
    perf.add_argument(
        "--floor-mibps",
        type=float,
        help="omit for evidence-only lanes such as commercial substrates",
    )
    perf.add_argument("--timeout-seconds", type=float, default=60.0)
    for flag in PROVENANCE:
        perf.add_argument(flag)
    return root


def default_output(service: str = "youtube") -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("artifacts") / service / timestamp


def provenance(args) -> dict[str, str | None]:
    return {
        flag.lstrip("-").replace("-", "_"): getattr(
            args, flag.lstrip("-").replace("-", "_")
        )
        for flag in PROVENANCE
    }


def run_perf(args) -> int:
    if args.transfer_bytes < 1 or args.transfer_bytes > 512 * 1024 * 1024:
        raise SystemExit("--transfer-bytes must be between 1 and 536870912")
    if args.floor_mibps is not None and args.floor_mibps <= 0:
        raise SystemExit("--floor-mibps must be greater than 0 when supplied")
    config = PerfConfig(
        proxy=args.proxy,
        output=args.output or default_output("perf"),
        transfer_bytes=args.transfer_bytes,
        runs=args.runs,
        floor_mibps=args.floor_mibps,
        timeout_seconds=args.timeout_seconds,
        **provenance(args),
    )
    _, exit_code = run_perf_probe(config)
    print((config.output / "verdict.txt").read_text(encoding="utf-8"), end="")
    return exit_code


def main() -> int:
    args = parser().parse_args()
    if args.service == "perf":
        return run_perf(args)
    if args.timeout_seconds <= 0 or args.timeout_seconds > 120:
        raise SystemExit("--timeout-seconds must be greater than 0 and at most 120")
    if args.deadline_seconds <= 0 or args.deadline_seconds > 600:
        raise SystemExit("--deadline-seconds must be greater than 0 and at most 600")
    if args.transfer_bytes < 1 or args.transfer_bytes > 4 * 1024 * 1024:
        raise SystemExit("--transfer-bytes must be between 1 and 4194304")
    config = YouTubeConfig(
        proxy=args.proxy,
        source_url=args.source_url,
        video_url=args.video_url,
        output=args.output or default_output(),
        attempts=args.attempts,
        timeout_seconds=args.timeout_seconds,
        transfer_bytes=args.transfer_bytes,
        instance_id=args.instance_id,
        image_identity=args.image_identity,
        config_digest=args.config_digest,
        deadline_seconds=args.deadline_seconds,
        node_id=args.node_id,
        runtime=args.runtime,
        composition=args.composition,
        transport=args.transport,
        substrate_profile=args.substrate_profile,
        requested_region=args.requested_region,
    )
    summary, exit_code = run_probe(config)
    print((config.output / "verdict.txt").read_text(encoding="utf-8"), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
