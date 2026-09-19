import time
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from cfwarp_service_eval.youtube_unlock import (
    FIXED_VIDEO_ID,
    YouTubeUnlockConfig,
    run_probe,
    select_format_reference,
)


def config(output: Path, deadline_seconds: float = 120.0) -> YouTubeUnlockConfig:
    return YouTubeUnlockConfig(
        proxy="socks5h://127.0.0.1:1080",
        output=output,
        attempts=2,
        timeout_seconds=25,
        instance_id="test-instance",
        image_identity="test-image@sha256:example",
        config_digest="sha256:example",
        deadline_seconds=deadline_seconds,
    )


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.command_identity",
        lambda command: {
            "path": "/usr/bin/deno" if command == "deno" else None,
            "version": "2.9.2" if command == "deno" else None,
        },
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "US"}, True),
    )


def extracted_info(**metadata):
    values = {
        "id": FIXED_VIDEO_ID,
        "title": "yt-dlp test video",
        "availability": "public",
        "age_limit": 0,
        "format_count": 1,
    }
    values.update(metadata)
    return values, {
        "formats": [
            {
                "format_id": "18",
                "url": "https://media.example/videoplayback?token=secret",
                "protocol": "https",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
            }
        ]
    }


def test_unlock_pass_requires_metadata_and_sanitized_format_reference(
    tmp_path: Path, monkeypatch, ready
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_video",
        lambda *_args: extracted_info(),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass"
    assert summary["input"]["account_policy"] == "fresh-anonymous-no-cookies"
    assert summary["input"]["media_download"] is False
    reference = summary["attempts"][0]["format_reference"]
    assert reference == {
        "format_id": "18",
        "protocol": "https",
        "ext": "mp4",
        "vcodec": "avc1",
        "acodec": "mp4a",
    }
    assert "url" not in reference
    assert summary["observation"]["scenario_id"] == (
        "youtube.anonymous_public_video_unlock"
    )


@pytest.mark.parametrize(
    ("message", "verdict"),
    [
        ("Sign in to confirm you’re not a bot", "bot_challenge"),
        ("Login required", "authentication_required"),
        ("HTTP Error 429: Too Many Requests", "rate_limited"),
        ("This video is not available in your country", "service_unavailable"),
    ],
)
def test_unlock_classifies_terminal_service_failures_without_retry(
    tmp_path: Path, monkeypatch, ready, message: str, verdict: str
) -> None:
    calls = 0

    def fail(_config, _url, logger):
        nonlocal calls
        calls += 1
        logger.error(message)
        raise DownloadError("extraction failed")

    monkeypatch.setattr("cfwarp_service_eval.youtube_unlock.extract_video", fail)

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == verdict
    assert summary["observation"]["result"]["availability"] == "unavailable"
    assert calls == 1


@pytest.mark.parametrize(
    "override",
    [
        {"id": "wrong_id_00"},
        {"title": ""},
        {"age_limit": 18},
        {"availability": "private"},
    ],
)
def test_unlock_rejects_unexpected_metadata(
    tmp_path: Path, monkeypatch, ready, override
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_video",
        lambda *_args: extracted_info(**override),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "unexpected_content"
    assert summary["observation"]["result"] == {
        "availability": "unknown",
        "class": "unexpected_content",
        "eligible": False,
    }


def test_unlock_rejects_metadata_without_usable_format() -> None:
    assert select_format_reference({"formats": [{"format_id": "storyboard"}]}) is None


def test_unlock_reports_missing_javascript_runtime_as_tooling_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.command_identity",
        lambda _command: {"path": None, "version": None},
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "US"}, True),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_video",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not extract")),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "tooling_failure"
    assert summary["observation"]["result"]["availability"] == "unknown"


def test_unlock_has_whole_probe_deadline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_trace",
        lambda _config: (time.sleep(0.1), True),
    )

    summary, exit_code = run_probe(config(tmp_path, deadline_seconds=0.01))

    assert exit_code == 2
    assert summary["verdict"] == "probe_deadline_exceeded"
    assert summary["observation"]["result"]["eligible"] is False
