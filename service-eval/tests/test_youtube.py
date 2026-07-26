from datetime import datetime, timezone
import json
import time
from pathlib import Path

import httpx
from yt_dlp.utils import DownloadError

from cfwarp_service_eval.youtube import (
    CapturedLogger,
    YouTubeConfig,
    canonical_video_url,
    parse_trace,
    redact_proxy,
    redact_text,
    run_probe,
    validate_partial_response,
    ydl_options,
)


def test_parse_trace_keeps_only_key_value_lines() -> None:
    assert parse_trace("fl=1\nwarp=on\nloc=GB\ninvalid\n") == {
        "fl": "1",
        "warp": "on",
        "loc": "GB",
    }


def test_redact_proxy_credentials_and_query() -> None:
    assert (
        redact_proxy("socks5h://alice:secret@127.0.0.1:1080?token=secret")
        == "socks5h://127.0.0.1:1080"
    )


def test_redact_text_credentials() -> None:
    assert (
        redact_text(
            "failed through socks5://alice:secret@proxy.example:1080?token=nope"
        )
        == "failed through socks5://proxy.example:1080"
    )


def test_canonical_video_url_removes_tracking_parameters() -> None:
    assert (
        canonical_video_url("https://youtu.be/abcdefghijk?si=secret")
        == "https://www.youtube.com/watch?v=abcdefghijk"
    )


def test_partial_response_rejects_html_and_accepts_media_range() -> None:
    assert (
        validate_partial_response(
            200,
            httpx.Headers({"content-type": "text/html", "content-range": ""}),
            1024,
        )
        == "unexpected_http_status"
    )
    assert (
        validate_partial_response(
            206,
            httpx.Headers(
                {
                    "content-type": "video/webm",
                    "content-range": "bytes 0-1023/4096",
                }
            ),
            1024,
        )
        is None
    )
    assert (
        validate_partial_response(
            206,
            httpx.Headers(
                {
                    "content-type": "video/webm",
                    "content-range": "bytes 0-511/garbage",
                }
            ),
            1024,
        )
        == "invalid_content_range"
    )


def config(output: Path) -> YouTubeConfig:
    return YouTubeConfig(
        proxy="socks5h://alice:secret@127.0.0.1:1080",
        source_url="https://www.youtube.com/example/videos",
        video_url=None,
        output=output,
        attempts=2,
        timeout_seconds=25,
        transfer_bytes=1024,
        instance_id="test-instance",
        image_identity="test-image@sha256:example",
        config_digest="sha256:example",
    )


def test_direct_yt_dlp_configuration_disables_environment_proxy(tmp_path: Path) -> None:
    direct = config(tmp_path)
    direct = YouTubeConfig(**{**direct.__dict__, "proxy": None})
    assert ydl_options(direct, CapturedLogger())["proxy"] == ""


def test_probe_stops_before_service_when_trace_is_not_warp(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_trace",
        lambda _config: ({"ok": False, "warp": "off", "location": "US"}, False),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.discover_video",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not discover")),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "tunnel_failure"
    assert summary["attempts"] == []
    assert (
        json.loads((tmp_path / "summary.json").read_text())["verdict"]
        == "tunnel_failure"
    )


def test_probe_pass_requires_extraction_and_partial_transfer(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.command_identity",
        lambda command: {
            "path": "/usr/bin/node" if command == "node" else None,
            "version": "test" if command == "node" else None,
        },
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "GB"}, True),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.discover_video",
        lambda *_args: "https://www.youtube.com/watch?v=current1234",
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.extract_video",
        lambda *_args: (
            {"id": "current1234", "format_count": 3, "title": "Current video"},
            {"formats": []},
        ),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_partial_transfer",
        lambda *_args: ({"ok": True, "bytes_read": 1024}, True),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass"
    assert summary["javascript_ready"] is True
    assert summary["ffmpeg_available"] is False
    assert summary["attempts"][0]["partial_transfer"]["bytes_read"] == 1024
    assert summary["input"]["proxy"] == "socks5h://127.0.0.1:1080"
    observation = summary["observation"]
    assert observation["schema_version"] == 1
    assert observation["scenario_id"] == "youtube.anonymous_public_video"
    assert observation["result"] == {
        "availability": "available",
        "class": "pass",
        "eligible": True,
    }
    assert observation["egress"]["region"] == "GB"
    assert observation["subject"]["instance_id"] == "test-instance"
    assert observation["fresh_until"] > observation["observed_at"]


def test_bot_challenge_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def challenge(_config, logger) -> str:
        nonlocal calls
        calls += 1
        logger.error("Sign in to confirm you’re not a bot")
        raise DownloadError("challenge")

    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "GB"}, True),
    )
    monkeypatch.setattr("cfwarp_service_eval.youtube.discover_video", challenge)

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "bot_challenge"
    assert calls == 1


def test_nonfatal_logged_challenge_cannot_pass(tmp_path: Path, monkeypatch) -> None:
    def extracted(_config, _url, logger):
        logger.error("Sign in to confirm you’re not a bot")
        return (
            {"id": "current1234", "format_count": 3, "title": "Current video"},
            {"formats": []},
        )

    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "GB"}, True),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.discover_video",
        lambda *_args: "https://www.youtube.com/watch?v=current1234",
    )
    monkeypatch.setattr("cfwarp_service_eval.youtube.extract_video", extracted)
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_partial_transfer",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not transfer")),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "bot_challenge"
    assert len(summary["attempts"]) == 1


def test_semantic_transfer_failure_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    transfers = 0

    def transfer(*_args):
        nonlocal transfers
        transfers += 1
        return {"ok": False, "error_kind": "unexpected_content_type"}, False

    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "GB"}, True),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.discover_video",
        lambda *_args: "https://www.youtube.com/watch?v=current1234",
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.extract_video",
        lambda *_args: (
            {"id": "current1234", "format_count": 3, "title": "Current video"},
            {"formats": []},
        ),
    )
    monkeypatch.setattr("cfwarp_service_eval.youtube.check_partial_transfer", transfer)

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "media_transfer_failure"
    assert transfers == 1


def test_probe_has_a_hard_deadline(tmp_path: Path, monkeypatch) -> None:
    bounded = config(tmp_path)
    bounded = YouTubeConfig(**{**bounded.__dict__, "deadline_seconds": 0.01})
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube.check_trace",
        lambda _config: (time.sleep(0.1), True),
    )

    summary, exit_code = run_probe(bounded)

    assert exit_code == 2
    assert summary["verdict"] == "probe_deadline_exceeded"
    assert summary["observation"]["result"] == {
        "availability": "unknown",
        "class": "probe_deadline_exceeded",
        "eligible": False,
    }
    assert (tmp_path / "summary.json").is_file()


def test_deadline_with_transport_failure_is_an_eligible_lane_verdict():
    """A lane that visibly failed a transfer must not read as a measurement gap."""
    from cfwarp_service_eval.youtube import build_observation, deadline_verdict

    summary = {
        "attempts": [
            {
                "number": 1,
                "partial_transfer": {
                    "ok": False,
                    "bytes_read": 0,
                    "error_kind": "transport_error",
                    "error_type": "ProxyError",
                },
            }
        ]
    }
    verdict, layer = deadline_verdict(summary)
    assert verdict == "network_failure"
    assert layer == "service-probe"

    summary.update(
        {
            "verdict": verdict,
            "failure_layer": layer,
            "elapsed_ms": 1,
            "trace": {},
            "input": {},
        }
    )
    observation = build_observation(summary, datetime.now(timezone.utc))
    assert observation["result"]["availability"] == "unavailable"
    assert observation["result"]["eligible"] is True


def test_deadline_without_evidence_stays_ineligible():
    """With nothing observed, a deadline really is a measurement gap."""
    from cfwarp_service_eval.youtube import deadline_verdict

    assert deadline_verdict({"attempts": []}) == ("probe_deadline_exceeded", "unknown")
    assert deadline_verdict({}) == ("probe_deadline_exceeded", "unknown")
