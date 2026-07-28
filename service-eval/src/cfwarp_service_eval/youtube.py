from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError

from .classify import classify_failure
from .contracts import classify_result


DEFAULT_SOURCE_URL = "https://www.youtube.com/@aiexplained-official/videos"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
MAX_LOG_ENTRIES = 50
MAX_LOG_LENGTH = 1_000
MAX_TRACE_BYTES = 32 * 1024
YOUTUBE_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


@dataclass
class CapturedLogger:
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def debug(self, message: str) -> None:
        return

    def info(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        self._append(self.warnings, message)

    def error(self, message: str) -> None:
        self._append(self.errors, message)

    @staticmethod
    def _append(target: list[str], message: str) -> None:
        if len(target) < MAX_LOG_ENTRIES:
            target.append(redact_text(message)[:MAX_LOG_LENGTH])


@dataclass(frozen=True)
class YouTubeConfig:
    proxy: str | None
    source_url: str
    video_url: str | None
    output: Path
    attempts: int
    timeout_seconds: float
    transfer_bytes: int
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


IN_FLIGHT: dict[str, Any] = {}


class ProbeDeadlineExceeded(BaseException):
    pass


def redact_proxy(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    parsed = urlsplit(proxy)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def redact_text(message: str) -> str:
    redacted = re.sub(
        r"(https?://|socks5h?://)([^\s/@:]+):([^\s/@]+)@",
        r"\1<redacted>@",
        message,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?P<url>(?:https?|socks5h?)://[^\s]+)",
        lambda match: strip_url_secrets(match.group("url")),
        redacted,
        flags=re.IGNORECASE,
    )


def strip_url_secrets(url: str | None) -> str | None:
    if not url:
        return url
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def canonical_video_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        raise ValueError("video URL must use youtube.com or youtu.be")
    if not YOUTUBE_ID.fullmatch(video_id):
        raise ValueError("video URL does not contain a valid YouTube video ID")
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_trace(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in body.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            result[key] = value
    return result


def command_identity(command: str) -> dict[str, str | None]:
    path = shutil.which(command)
    if not path:
        return {"path": None, "version": None}
    try:
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        first_line = (completed.stdout or completed.stderr).splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        first_line = "unknown"
    return {"path": path, "version": first_line[:300]}


def httpx_proxy(proxy: str | None) -> str | None:
    if proxy and proxy.startswith("socks5h://"):
        return "socks5://" + proxy.removeprefix("socks5h://")
    return proxy


def check_trace(config: YouTubeConfig) -> tuple[dict[str, Any], bool]:
    started = time.monotonic()
    try:
        with httpx.Client(
            proxy=httpx_proxy(config.proxy),
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        ) as client:
            with client.stream("GET", TRACE_URL) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_raw():
                    if len(chunk) > MAX_TRACE_BYTES - len(body):
                        return {
                            "ok": False,
                            "error_kind": "oversized_response",
                            "http_status": response.status_code,
                            "elapsed_ms": round((time.monotonic() - started) * 1_000),
                        }, False
                    body.extend(chunk)
        fields = parse_trace(body.decode("utf-8", errors="replace"))
        result = {
            "ok": fields.get("warp") == "on",
            "http_status": response.status_code,
            "warp": fields.get("warp"),
            "location": fields.get("loc"),
            "colo": fields.get("colo"),
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
        }
        if not result["ok"]:
            result["error_kind"] = "warp_not_on"
        return result, bool(result["ok"])
    except (httpx.HTTPError, OSError) as error:
        return {
            "ok": False,
            "error_kind": "transport_error",
            "error_type": type(error).__name__,
            "error": redact_text(str(error))[:MAX_LOG_LENGTH],
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
        }, False


def ydl_options(config: YouTubeConfig, logger: CapturedLogger) -> dict[str, Any]:
    runtime_commands = {
        "deno": "deno",
        "node": "node",
        "bun": "bun",
        "quickjs": "qjs",
    }
    js_runtimes = {
        runtime: {"path": path}
        for runtime, command in runtime_commands.items()
        if (path := shutil.which(command))
    }
    return {
        "logger": logger,
        "proxy": config.proxy if config.proxy is not None else "",
        "socket_timeout": config.timeout_seconds,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "quiet": True,
        "noplaylist": True,
        "cachedir": False,
        "noprogress": True,
        "js_runtimes": js_runtimes,
    }


def discover_video(config: YouTubeConfig, logger: CapturedLogger) -> str:
    if config.video_url:
        return canonical_video_url(config.video_url)
    options = ydl_options(config, logger) | {
        "extract_flat": True,
        "playlist_items": "1",
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(config.source_url, download=False)
        entries = list(info.get("entries") or []) if info else []
    if not entries or not entries[0].get("id"):
        raise DownloadError("deterministic discovery returned no current video")
    video_id = str(entries[0]["id"])
    if not YOUTUBE_ID.fullmatch(video_id):
        raise DownloadError("deterministic discovery returned an invalid video ID")
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_video(
    config: YouTubeConfig, video_url: str, logger: CapturedLogger
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    with yt_dlp.YoutubeDL(ydl_options(config, logger)) as ydl:
        info = ydl.extract_info(video_url, download=False)
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


def select_transfer_format(info: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in info.get("formats") or []
        if item.get("url") and item.get("protocol") in {"http", "https"}
    ]
    if not candidates:
        return None

    def size_key(item: dict[str, Any]) -> tuple[float, float, float]:
        size = item.get("filesize") or item.get("filesize_approx") or float("inf")
        bitrate = item.get("tbr") or float("inf")
        height = item.get("height") or float("inf")
        return float(size), float(bitrate), float(height)

    return min(candidates, key=size_key)


def validate_partial_response(
    status: int, headers: httpx.Headers, requested_bytes: int
) -> str | None:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    content_range = headers.get("content-range", "")
    allowed_type = (
        content_type.startswith("audio/")
        or content_type.startswith("video/")
        or content_type == "application/octet-stream"
    )
    if status != 206:
        return "unexpected_http_status"
    range_match = re.fullmatch(r"bytes 0-(\d+)/(\d+|\*)", content_range)
    if not range_match:
        return "invalid_content_range"
    range_end = int(range_match.group(1))
    total = range_match.group(2)
    if range_end != requested_bytes - 1:
        return "invalid_content_range"
    if total != "*" and int(total) <= range_end:
        return "invalid_content_range"
    if not allowed_type:
        return "unexpected_content_type"
    return None


def check_partial_transfer(
    config: YouTubeConfig, info: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    selected = select_transfer_format(info)
    if not selected:
        return {"ok": False, "error": "no direct HTTP media format available"}, False

    headers = {
        str(key): str(value)
        for key, value in (selected.get("http_headers") or {}).items()
    }
    headers["Range"] = f"bytes=0-{config.transfer_bytes - 1}"
    started = time.monotonic()
    bytes_read = 0
    status: int | None = None
    try:
        with httpx.Client(
            proxy=httpx_proxy(config.proxy),
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers=headers,
            trust_env=False,
        ) as client:
            with client.stream("GET", selected["url"]) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if response_error := validate_partial_response(
                    status, response.headers, config.transfer_bytes
                ):
                    return {
                        "ok": False,
                        "error_kind": response_error,
                        "http_status": status,
                        "content_type": content_type,
                    }, False
                for chunk in response.iter_bytes():
                    bytes_read += min(len(chunk), config.transfer_bytes - bytes_read)
                    if bytes_read >= config.transfer_bytes:
                        break
        ok = bytes_read >= config.transfer_bytes
        return {
            "ok": ok,
            "error_kind": None if ok else "short_read",
            "http_status": status,
            "bytes_read": bytes_read,
            "required_bytes": config.transfer_bytes,
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
            "format_id": selected.get("format_id"),
            "protocol": selected.get("protocol"),
        }, ok
    except (httpx.HTTPError, OSError) as error:
        return {
            "ok": False,
            "error_kind": "transport_error",
            "error_type": type(error).__name__,
            "http_status": status,
            "bytes_read": bytes_read,
            "required_bytes": config.transfer_bytes,
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
            "format_id": selected.get("format_id"),
            "protocol": selected.get("protocol"),
        }, False


def run_probe(config: YouTubeConfig) -> tuple[dict[str, Any], int]:
    if (
        not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        return _run_probe(config)

    deadline_started_at = datetime.now(timezone.utc).isoformat()
    IN_FLIGHT.pop("summary", None)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_handler(_signum: int, _frame: Any) -> None:
        raise ProbeDeadlineExceeded

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, config.deadline_seconds)
    try:
        return _run_probe(config)
    except ProbeDeadlineExceeded:
        config.output.mkdir(parents=True, exist_ok=True)
        summary = IN_FLIGHT.get("summary") or {
            "schema_version": 1,
            "service": "youtube",
            "scenario": "anonymous_public_video",
            "started_at": deadline_started_at,
            "input": safe_input(config),
            "tools": {},
            "javascript_ready": False,
            "ffmpeg_available": None,
            "tooling_ready": False,
            "trace": None,
            "attempts": [],
        }
        summary["verdict"], summary["failure_layer"] = deadline_verdict(summary)
        return finish(config.output, summary), 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def deadline_verdict(summary: dict[str, Any]) -> tuple[str, str]:
    """Classify a deadline expiry using whatever the probe already observed.

    A transfer that reached the media host and failed at the transport is a
    lane verdict, not a measurement gap. Reporting it as a deadline expiry
    makes it ineligible for the availability ratio, which hides a lane that
    reproducibly does not work behind an apparent gap in coverage.
    """
    for attempt in summary.get("attempts") or []:
        transfer = attempt.get("partial_transfer") or {}
        if transfer.get("ok") is False and transfer.get("error_kind") in {
            "transport_error",
            "timeout",
        }:
            return "network_failure", "service-probe"
    return "probe_deadline_exceeded", "unknown"


def safe_input(config: YouTubeConfig) -> dict[str, Any]:
    return {
        "source_url": strip_url_secrets(config.source_url),
        "explicit_video_url": (
            canonical_video_url(config.video_url) if config.video_url else None
        ),
        "proxy": redact_proxy(config.proxy),
        "attempts": config.attempts,
        "timeout_seconds": config.timeout_seconds,
        "deadline_seconds": config.deadline_seconds,
        "transfer_bytes": config.transfer_bytes,
        "instance_id": config.instance_id,
        "image_identity": config.image_identity,
        "config_digest": config.config_digest,
        "node_id": config.node_id,
        "runtime": config.runtime,
        "composition": config.composition,
        "transport": config.transport,
        "substrate_profile": config.substrate_profile,
        "requested_region": config.requested_region,
    }


def _run_probe(config: YouTubeConfig) -> tuple[dict[str, Any], int]:
    config.output.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    tools = {
        "python": {"version": platform.python_version()},
        "yt_dlp": {"version": importlib.metadata.version("yt-dlp")},
        "ffmpeg": command_identity("ffmpeg"),
        "javascript_runtimes": {
            name: command_identity(name) for name in ("deno", "node", "bun", "qjs")
        },
    }
    javascript_ready = any(
        identity["path"] for identity in tools["javascript_runtimes"].values()
    )
    ffmpeg_available = bool(tools["ffmpeg"]["path"])
    summary: dict[str, Any] = {
        "schema_version": 1,
        "service": "youtube",
        "scenario": "anonymous_public_video",
        "started_at": started_at.isoformat(),
        "input": safe_input(config),
        "tools": tools,
        "javascript_ready": javascript_ready,
        "ffmpeg_available": ffmpeg_available,
        "tooling_ready": javascript_ready,
        "trace": None,
        "attempts": [],
        "verdict": "unknown",
        "failure_layer": "unknown",
    }
    # Published so the deadline handler can salvage partial evidence. A lane
    # slow enough to exhaust the deadline is usually a lane that already failed
    # visibly; discarding what it showed turns a lane verdict into a
    # measurement gap.
    IN_FLIGHT["summary"] = summary

    trace, trace_ok = check_trace(config)
    summary["trace"] = trace
    if not trace_ok:
        summary["verdict"] = "tunnel_failure"
        summary["failure_layer"] = (
            "route-runtime" if trace.get("error_kind") == "warp_not_on" else "unknown"
        )
        return finish(config.output, summary), 2

    selected_url: str | None = (
        canonical_video_url(config.video_url) if config.video_url else None
    )
    for number in range(1, config.attempts + 1):
        logger = CapturedLogger()
        attempt: dict[str, Any] = {"number": number}
        try:
            selected_url = selected_url or discover_video(config, logger)
            attempt["selected_video_url"] = selected_url
            metadata, info = extract_video(config, selected_url, logger)
            attempt["metadata"] = metadata
            if not metadata["id"] or metadata["format_count"] < 1:
                raise DownloadError("extraction returned no playable formats")
            logged_outcome = classify_failure(
                "\n".join(logger.warnings + logger.errors)
            )
            if logged_outcome in {
                "bot_challenge",
                "consent_challenge",
                "auth_required",
                "service_unavailable",
            }:
                raise DownloadError(f"yt-dlp reported {logged_outcome}")
            transfer, transfer_ok = check_partial_transfer(config, info)
            attempt["partial_transfer"] = transfer
            attempt["warnings"] = logger.warnings
            attempt["errors"] = logger.errors
            summary["attempts"].append(attempt)
            if transfer_ok:
                summary["verdict"] = (
                    "pass"
                    if javascript_ready and logged_outcome != "tooling_failure"
                    else "pass_with_tooling_caveat"
                )
                summary["failure_layer"] = None
                return finish(config.output, summary), 0
            if transfer.get("error_kind") == "transport_error":
                summary["verdict"] = "network_failure"
                summary["failure_layer"] = "unknown"
            else:
                summary["verdict"] = "media_transfer_failure"
                summary["failure_layer"] = "service-probe"
                break
        except (DownloadError, OSError, ValueError) as error:
            message = "\n".join(logger.warnings + logger.errors + [str(error)])
            outcome = classify_failure(message)
            attempt["warnings"] = logger.warnings
            attempt["errors"] = logger.errors
            attempt["exception"] = redact_text(str(error))[:MAX_LOG_LENGTH]
            attempt["outcome"] = outcome
            summary["attempts"].append(attempt)
            summary["verdict"] = outcome
            summary["failure_layer"] = failure_layer(outcome)
            if outcome not in {"network_failure"}:
                break

    return finish(config.output, summary), 2


def failure_layer(outcome: str) -> str:
    if outcome in {
        "bot_challenge",
        "consent_challenge",
        "auth_required",
        "service_unavailable",
    }:
        return "service-probe"
    if outcome == "tooling_failure":
        return "tooling"
    return "unknown"


def finish(output: Path, summary: dict[str, Any]) -> dict[str, Any]:
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    started = datetime.fromisoformat(summary["started_at"])
    finished = datetime.fromisoformat(summary["finished_at"])
    summary["elapsed_ms"] = round((finished - started).total_seconds() * 1_000)
    summary["observation"] = build_observation(summary, finished)
    summary_path = output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    trace = summary.get("trace") or {}
    lines = [
        f"Service verdict: {summary['verdict']}",
        f"Failure layer: {summary['failure_layer'] or 'none'}",
        f"Trace: warp={trace.get('warp', 'unknown')} loc={trace.get('location', 'unknown')} colo={trace.get('colo', 'unknown')}",
        f"Attempts: {len(summary['attempts'])}",
        f"JavaScript ready: {'yes' if summary['javascript_ready'] else 'no'}",
        f"ffmpeg available: {availability(summary['ffmpeg_available'])}",
        f"Summary: {summary_path}",
    ]
    (output / "verdict.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def build_observation(summary: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    """Build the backend-independent observation contract shared by service probes."""
    verdict = str(summary["verdict"])
    availability, eligible = classify_result(verdict)
    trace = summary.get("trace") or {}
    inputs = summary.get("input") or {}
    return {
        "schema_version": 1,
        "observation_id": str(uuid.uuid4()),
        "observed_at": observed_at.isoformat(),
        "fresh_until": (observed_at + timedelta(hours=24)).isoformat(),
        "scenario_id": "youtube.anonymous_public_video",
        "probe": {"name": "youtube-yt-dlp", "version": "1", "execution": "local"},
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


def availability(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"
