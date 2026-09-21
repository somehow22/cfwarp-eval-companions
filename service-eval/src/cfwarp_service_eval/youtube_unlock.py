from __future__ import annotations

import importlib.metadata
import importlib.resources
import json
import platform
import re
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

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError

from .classify import classify_failure
from .contracts import classify_result
from .youtube import (
    CapturedLogger,
    ProbeDeadlineExceeded,
    check_trace,
    command_identity,
    httpx_proxy,
    redact_proxy,
    redact_text,
    ydl_options,
)


# yt-dlp's primary public, age-unrestricted extractor fixture. It replaced the
# retired BaW_jenozKc fixture in yt-dlp c102b2096525218a6918a46b415fb167786f9656.
FIXED_VIDEO_ID = "YE7VzlLtp-4"
FIXED_VIDEO_URL = f"https://www.youtube.com/watch?v={FIXED_VIDEO_ID}"
PINNED_DENO_VERSION = "2.9.2"
PINNED_EJS_VERSION = "0.8.0"
PINNED_YT_DLP_VERSION = "2026.7.4"
MAX_WATCH_BYTES = 2 * 1024 * 1024
SERVICE_FAILURES = {
    "bot_challenge",
    "consent_challenge",
    "authentication_required",
    "rate_limited",
    "service_unavailable",
}
IN_FLIGHT: dict[str, Any] = {}
DENO_VERSION_PATTERN = re.compile(
    r"^deno (?P<version>[0-9]+\.[0-9]+\.[0-9]+) "
    r"\(stable, release, (?P<target>x86_64|aarch64)-unknown-linux-gnu\)$"
)
MACHINE_TO_DENO_TARGET = {
    "AMD64": "x86_64",
    "ARM64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
}


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


def pinned_deno_identity(
    identity: dict[str, str | None], machine: str | None = None
) -> bool:
    if not identity.get("path") or not isinstance(identity.get("version"), str):
        return False
    match = DENO_VERSION_PATTERN.fullmatch(identity["version"])
    expected_target = MACHINE_TO_DENO_TARGET.get(machine or platform.machine())
    return bool(
        match
        and expected_target
        and match.group("version") == PINNED_DENO_VERSION
        and match.group("target") == expected_target
    )


def pinned_ejs_assets() -> bool:
    try:
        solver = importlib.resources.files("yt_dlp_ejs.yt.solver")
        return all(
            solver.joinpath(name).is_file() and bool(solver.joinpath(name).read_bytes())
            for name in ("core.min.js", "lib.min.js")
        )
    except (ModuleNotFoundError, OSError):
        return False


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


def player_response_from_watch_page(body: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for marker in ("ytInitialPlayerResponse =", '"ytInitialPlayerResponse":'):
        start = body.find(marker)
        if start < 0:
            continue
        candidate = body[start + len(marker) :].lstrip()
        try:
            value, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("watch page lacks a valid initial player response")


def select_player_format(player: dict[str, Any]) -> dict[str, Any] | None:
    streaming = player.get("streamingData")
    if not isinstance(streaming, dict):
        return None
    formats = [
        item
        for key in ("formats", "adaptiveFormats")
        for item in streaming.get(key) or []
        if isinstance(item, dict)
    ]
    for item in formats:
        url = item.get("url")
        mime_type = item.get("mimeType")
        if not isinstance(url, str) or not isinstance(mime_type, str):
            continue
        parsed = urlsplit(url)
        media_type = mime_type.split(";", 1)[0].strip().casefold()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not media_type.startswith(("audio/", "video/"))
        ):
            continue
        return {
            "itag": item.get("itag"),
            "mime_type": mime_type[:300],
            "content_length_present": bool(item.get("contentLength")),
        }
    return None


def validate_player_response(player: dict[str, Any]) -> dict[str, Any]:
    playability = player.get("playabilityStatus")
    if not isinstance(playability, dict):
        return {"outcome": "unexpected_content", "content_error": "missing_playability"}
    if playability.get("status") != "OK":
        reason = " ".join(
            str(value)
            for value in (playability.get("reason"), playability.get("messages"))
            if value
        )
        outcome = classify_failure(reason)
        return {
            "outcome": outcome if outcome in SERVICE_FAILURES else "unexpected_content",
            "playability_status": playability.get("status"),
        }
    details = player.get("videoDetails")
    if not isinstance(details, dict) or details.get("videoId") != FIXED_VIDEO_ID:
        return {"outcome": "unexpected_content", "content_error": "unexpected_video_id"}
    title = details.get("title")
    if not isinstance(title, str) or not title.strip():
        return {"outcome": "unexpected_content", "content_error": "missing_title"}
    format_reference = select_player_format(player)
    if format_reference is None:
        return {
            "outcome": "unexpected_content",
            "content_error": "missing_direct_usable_format",
        }
    return {
        "outcome": "pass",
        "metadata": {"id": details["videoId"], "title_present": True},
        "format_reference": format_reference,
    }


def check_watch_player(config: YouTubeUnlockConfig) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with httpx.Client(
            proxy=httpx_proxy(config.proxy),
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept-Encoding": "identity",
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": "cfwarp-service-eval/youtube-unlock-v2",
            },
        ) as client:
            with client.stream("GET", FIXED_VIDEO_URL) as response:
                body = bytearray()
                for chunk in response.iter_raw():
                    if len(chunk) > MAX_WATCH_BYTES - len(body):
                        return {
                            "outcome": "unexpected_content",
                            "content_error": "oversized_watch_page",
                            "http_status": response.status_code,
                        }
                    body.extend(chunk)
                final_url = str(response.url)
                status = response.status_code
                location = response.headers.get("location", "")
        text = body.decode("utf-8", errors="replace")
        if 200 <= status < 300:
            try:
                player = player_response_from_watch_page(text)
            except ValueError:
                classified = classify_failure(text)
                if classified in SERVICE_FAILURES:
                    result = {"outcome": classified}
                else:
                    raise
            else:
                result = validate_player_response(player)
        else:
            classified = classify_failure(
                f"status code {status} {final_url} {location} {text}"
            )
            result = {
                "outcome": (
                    classified
                    if classified in SERVICE_FAILURES
                    else "unexpected_content"
                ),
            }
        if "http_status" not in result:
            result["http_status"] = status
        result["elapsed_ms"] = round((time.monotonic() - started) * 1_000)
        return result
    except (httpx.HTTPError, OSError) as error:
        return {
            "outcome": "network_failure",
            "error_type": type(error).__name__,
            "error": redact_text(str(error))[:1_000],
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
        }
    except (json.JSONDecodeError, ValueError) as error:
        return {
            "outcome": "unexpected_content",
            "content_error": str(error)[:300],
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
        }


def compose_signals(independent: str, extractor: str) -> str:
    if independent == "pass":
        return "pass" if extractor == "pass" else "pass_with_tooling_caveat"
    if independent in SERVICE_FAILURES and independent == extractor:
        return independent
    if (
        extractor == "pass"
        or independent in SERVICE_FAILURES
        or extractor in SERVICE_FAILURES
    ):
        return "probe_dependent"
    if independent == "unexpected_content":
        return "unexpected_content"
    if independent == "network_failure" or extractor == "network_failure":
        return "tooling_or_network_failure"
    if extractor == "tooling_failure":
        return "tooling_failure"
    return "extractor_failure"


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
        "httpx": {"version": importlib.metadata.version("httpx")},
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

    extractor_ready = not (
        tools["yt_dlp"]["version"] != PINNED_YT_DLP_VERSION
        or tools["yt_dlp_ejs"]["version"] != PINNED_EJS_VERSION
        or not pinned_deno_identity(tools["deno"])
        or not pinned_ejs_assets()
    )

    for number in range(1, config.attempts + 1):
        logger = CapturedLogger()
        attempt: dict[str, Any] = {
            "number": number,
            "independent": check_watch_player(config),
        }
        extractor: dict[str, Any] = {"outcome": "tooling_failure"}
        if extractor_ready:
            try:
                metadata, info = extract_unlock_video(config, logger)
                extractor["metadata"] = metadata
                logged_outcome = classify_failure(
                    "\n".join(logger.warnings + logger.errors)
                )
                if logged_outcome != "extractor_failure":
                    raise DownloadError(f"yt-dlp reported {logged_outcome}")
                format_reference, content_error = validate_unlock(metadata, info)
                if content_error:
                    extractor["outcome"] = "unexpected_content"
                    extractor["content_error"] = content_error
                else:
                    extractor["outcome"] = "pass"
                    extractor["format_reference"] = format_reference
            except (DownloadError, OSError, ValueError) as error:
                message = "\n".join(logger.warnings + logger.errors + [str(error)])
                extractor["outcome"] = classify_failure(message)
                extractor["exception"] = redact_text(str(error))[:1_000]
        extractor["warnings"] = logger.warnings
        extractor["errors"] = logger.errors
        attempt["extractor"] = extractor
        summary["attempts"].append(attempt)
        summary["verdict"] = compose_signals(
            str(attempt["independent"]["outcome"]), str(extractor["outcome"])
        )
        summary["failure_layer"] = failure_layer(summary["verdict"])
        if summary["verdict"] in {"pass", "pass_with_tooling_caveat"}:
            return finish(config.output, summary), 0
        if summary["verdict"] in SERVICE_FAILURES:
            break
        if (
            attempt["independent"]["outcome"] != "network_failure"
            and extractor["outcome"] != "network_failure"
        ):
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
    if outcome in SERVICE_FAILURES:
        return "service-probe"
    if outcome == "tooling_failure":
        return "tooling"
    if outcome in {"pass", "pass_with_tooling_caveat"}:
        return "none"
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
    attempts = summary.get("attempts") or []
    last_attempt = attempts[-1] if attempts else {}
    return {
        "schema_version": 1,
        "observation_id": str(uuid.uuid4()),
        "observed_at": observed_at.isoformat(),
        "fresh_until": (observed_at + timedelta(hours=24)).isoformat(),
        "scenario_id": "youtube.anonymous_public_video_unlock",
        "probe": {
            "name": "youtube-unlock-multisignal",
            "version": "2",
            "execution": "local",
            "methods": ["watch-player-response-v1", "yt-dlp-extractor"],
            "tools": summary.get("tools") or {},
            "signals": {
                "watch_player": (last_attempt.get("independent") or {}).get("outcome"),
                "yt_dlp": (last_attempt.get("extractor") or {}).get("outcome"),
            },
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
