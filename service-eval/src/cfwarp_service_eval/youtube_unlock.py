from __future__ import annotations

import importlib.metadata
import json
import platform
import signal
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError

from .classify import classify_failure
from .contracts import classify_result
from .youtube import (
    CapturedLogger,
    ProbeDeadlineExceeded,
    check_trace,
    command_identity,
    redact_proxy,
    redact_text,
    ydl_options,
)


FIXED_VIDEO_ID = "BaW_jenozKc"
FIXED_VIDEO_URL = f"https://www.youtube.com/watch?v={FIXED_VIDEO_ID}"
PINNED_DENO_VERSION = "2.9.2"
PINNED_EJS_VERSION = "0.8.0"
IN_FLIGHT: dict[str, Any] = {}


@dataclass(frozen=True)
class YouTubeUnlockConfig:
    proxy: str | None
    output: Path
    attempts: int
    timeout_seconds: float
    instance_id: str | None
    image_identity: str | None
    config_digest: str | None
    deadline_seconds: float = 120.0
    node_id: str | None = None
    runtime: str | None = None
    composition: str | None = None
    transport: str | None = None
    substrate_profile: str | None = None
    requested_region: str | None = None


def select_format_reference(info: dict[str, Any]) -> dict[str, Any] | None:
    def usable(item: dict[str, Any]) -> bool:
        url = item.get("url")
        if not isinstance(url, str):
            return False
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if item.get("protocol") not in {"http", "https"}:
            return False
        if item.get("has_drm") is True or item.get("drm_family"):
            return False
        if "drm" in str(item.get("format_note") or "").casefold():
            return False
        codecs = (item.get("vcodec"), item.get("acodec"))
        return any(
            isinstance(codec, str) and codec.strip() and codec.casefold() != "none"
            for codec in codecs
        )

    candidates = [
        item
        for item in info.get("formats") or []
        if item.get("format_id") and usable(item)
    ]
    if not candidates:
        return None
    selected = candidates[0]
    return {
        key: selected.get(key)
        for key in ("format_id", "protocol", "ext", "vcodec", "acodec")
    }


def youtube_unlock_options(
    config: YouTubeUnlockConfig, logger: CapturedLogger
) -> dict[str, Any]:
    options = ydl_options(config, logger)  # type: ignore[arg-type]
    deno_path = shutil.which("deno")
    return options | {
        "check_formats": False,
        "skip_download": True,
        "cachedir": False,
        "remote_components": [],
        "js_runtimes": {"deno": {"path": deno_path}} if deno_path else {},
    }


def extract_unlock_video(
    config: YouTubeUnlockConfig, logger: CapturedLogger
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    with yt_dlp.YoutubeDL(youtube_unlock_options(config, logger)) as ydl:
        info = ydl.extract_info(FIXED_VIDEO_URL, download=False)
        sanitized = ydl.sanitize_info(info)
    formats = sanitized.get("formats") or []
    metadata = {
        "id": sanitized.get("id"),
        "title": sanitized.get("title"),
        "webpage_url": sanitized.get("webpage_url"),
        "duration": sanitized.get("duration"),
        "availability": sanitized.get("availability"),
        "age_limit": sanitized.get("age_limit"),
        "live_status": sanitized.get("live_status"),
        "format_count": len(formats),
        "elapsed_ms": round((time.monotonic() - started) * 1_000),
    }
    return metadata, sanitized


def validate_unlock(
    metadata: dict[str, Any], info: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    if metadata.get("id") != FIXED_VIDEO_ID:
        return None, "unexpected_video_id"
    if not isinstance(metadata.get("title"), str) or not metadata["title"].strip():
        return None, "missing_title"
    if metadata.get("age_limit") not in {None, 0}:
        return None, "age_restricted"
    if metadata.get("availability") not in {None, "public", "unlisted"}:
        return None, "non_public_availability"
    format_reference = select_format_reference(info)
    if format_reference is None:
        return None, "missing_usable_format_reference"
    return format_reference, None


def run_probe(config: YouTubeUnlockConfig) -> tuple[dict[str, Any], int]:
    if (
        not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        return _run_probe(config)

    started_at = datetime.now(timezone.utc).isoformat()
    IN_FLIGHT.pop("summary", None)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_handler(_signum: int, _frame: Any) -> None:
        raise ProbeDeadlineExceeded

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, config.deadline_seconds)
    try:
        return _run_probe(config)
    except ProbeDeadlineExceeded:
        summary = IN_FLIGHT.get("summary") or initial_summary(config, started_at, {})
        summary["verdict"] = "probe_deadline_exceeded"
        summary["failure_layer"] = "unknown"
        return finish(config.output, summary), 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _run_probe(config: YouTubeUnlockConfig) -> tuple[dict[str, Any], int]:
    config.output.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    tools = {
        "python": {"version": platform.python_version()},
        "yt_dlp": {"version": importlib.metadata.version("yt-dlp")},
        "yt_dlp_ejs": {"version": importlib.metadata.version("yt-dlp-ejs")},
        "deno": command_identity("deno"),
    }
    summary = initial_summary(config, started_at.isoformat(), tools)
    IN_FLIGHT["summary"] = summary

    trace, trace_ok = check_trace(config)  # type: ignore[arg-type]
    summary["trace"] = trace
    if not trace_ok:
        summary["verdict"] = "tunnel_failure"
        summary["failure_layer"] = (
            "route-runtime" if trace.get("error_kind") == "warp_not_on" else "unknown"
        )
        return finish(config.output, summary), 2

    if (
        tools["yt_dlp_ejs"]["version"] != PINNED_EJS_VERSION
        or not tools["deno"]["path"]
        or tools["deno"]["version"] != f"deno {PINNED_DENO_VERSION}"
    ):
        summary["verdict"] = "tooling_failure"
        summary["failure_layer"] = "tooling"
        return finish(config.output, summary), 2

    for number in range(1, config.attempts + 1):
        logger = CapturedLogger()
        attempt: dict[str, Any] = {"number": number}
        try:
            metadata, info = extract_unlock_video(config, logger)
            attempt["metadata"] = metadata
            logged_outcome = classify_failure(
                "\n".join(logger.warnings + logger.errors)
            )
            if logged_outcome != "extractor_failure":
                raise DownloadError(f"yt-dlp reported {logged_outcome}")
            format_reference, content_error = validate_unlock(metadata, info)
            if content_error:
                attempt["content_error"] = content_error
                attempt["warnings"] = logger.warnings
                attempt["errors"] = logger.errors
                summary["attempts"].append(attempt)
                summary["verdict"] = "unexpected_content"
                summary["failure_layer"] = "service-probe"
                break
            attempt["format_reference"] = format_reference
            attempt["warnings"] = logger.warnings
            attempt["errors"] = logger.errors
            summary["attempts"].append(attempt)
            summary["verdict"] = "pass"
            summary["failure_layer"] = None
            return finish(config.output, summary), 0
        except (DownloadError, OSError, ValueError) as error:
            message = "\n".join(logger.warnings + logger.errors + [str(error)])
            outcome = classify_failure(message)
            attempt["warnings"] = logger.warnings
            attempt["errors"] = logger.errors
            attempt["exception"] = redact_text(str(error))[:1_000]
            attempt["outcome"] = outcome
            summary["attempts"].append(attempt)
            summary["verdict"] = outcome
            summary["failure_layer"] = failure_layer(outcome)
            if outcome != "network_failure":
                break

    return finish(config.output, summary), 2


def initial_summary(
    config: YouTubeUnlockConfig, started_at: str, tools: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "service": "youtube",
        "scenario": "anonymous_public_video_unlock",
        "started_at": started_at,
        "input": {
            "fixed_video_id": FIXED_VIDEO_ID,
            "proxy": redact_proxy(config.proxy),
            "attempts": config.attempts,
            "timeout_seconds": config.timeout_seconds,
            "deadline_seconds": config.deadline_seconds,
            "account_policy": "fresh-anonymous-no-cookies",
            "media_download": False,
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
        "tools": tools,
        "trace": None,
        "attempts": [],
        "verdict": "unknown",
        "failure_layer": "unknown",
    }


def failure_layer(outcome: str) -> str:
    if outcome in {
        "bot_challenge",
        "consent_challenge",
        "authentication_required",
        "rate_limited",
        "service_unavailable",
    }:
        return "service-probe"
    if outcome == "tooling_failure":
        return "tooling"
    return "unknown"


def finish(output: Path, summary: dict[str, Any]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    summary["finished_at"] = finished.isoformat()
    summary["elapsed_ms"] = round(
        (finished - datetime.fromisoformat(summary["started_at"])).total_seconds()
        * 1_000
    )
    summary["observation"] = build_observation(summary, finished)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    trace = summary.get("trace") or {}
    (output / "verdict.txt").write_text(
        "\n".join(
            (
                f"Service verdict: {summary['verdict']}",
                f"Failure layer: {summary['failure_layer'] or 'none'}",
                f"Trace: warp={trace.get('warp', 'unknown')} loc={trace.get('location', 'unknown')} colo={trace.get('colo', 'unknown')}",
                f"Attempts: {len(summary['attempts'])}",
                f"Summary: {output / 'summary.json'}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def build_observation(summary: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    verdict = str(summary["verdict"])
    availability, eligible = classify_result(verdict)
    trace = summary.get("trace") or {}
    inputs = summary["input"]
    return {
        "schema_version": 1,
        "observation_id": str(uuid.uuid4()),
        "observed_at": observed_at.isoformat(),
        "fresh_until": (observed_at + timedelta(hours=24)).isoformat(),
        "scenario_id": "youtube.anonymous_public_video_unlock",
        "probe": {
            "name": "youtube-unlock-yt-dlp",
            "version": "1",
            "execution": "local",
        },
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
        "failure_layer": summary.get("failure_layer") or "none",
        "latency_ms": summary.get("elapsed_ms"),
        "artifacts": [
            {"kind": "summary", "path": "summary.json"},
            {"kind": "verdict", "path": "verdict.txt"},
        ],
    }
