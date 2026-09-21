import json
import sys
import time
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from cfwarp_service_eval.youtube import CapturedLogger
from cfwarp_service_eval.contracts import contracts_root
from cfwarp_service_eval.youtube_unlock import (
    FIXED_VIDEO_ID,
    FIXED_VIDEO_URL,
    YouTubeUnlockConfig,
    check_watch_player,
    compose_signals,
    extract_unlock_video,
    player_response_from_watch_page,
    pinned_deno_identity,
    pinned_ejs_assets,
    run_probe,
    select_format_reference,
    validate_player_response,
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
            "version": (
                "deno 2.9.2 (stable, release, x86_64-unknown-linux-gnu)"
                if command == "deno"
                else None
            ),
        },
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.platform.machine", lambda: "x86_64"
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "US"}, True),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: independent_success(),
    )


def independent_success():
    return {
        "outcome": "pass",
        "metadata": {"id": FIXED_VIDEO_ID, "title_present": True},
        "format_reference": {
            "itag": 18,
            "mime_type": 'video/mp4; codecs="avc1, mp4a"',
            "content_length_present": True,
        },
    }


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


def test_unlock_uses_current_upstream_public_fixture() -> None:
    assert FIXED_VIDEO_ID == "YE7VzlLtp-4"
    assert FIXED_VIDEO_URL == "https://www.youtube.com/watch?v=YE7VzlLtp-4"


def test_unlock_pass_requires_metadata_and_sanitized_format_reference(
    tmp_path: Path, monkeypatch, ready
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: extracted_info(),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass"
    assert summary["input"]["account_policy"] == "fresh-anonymous-no-cookies"
    assert summary["input"]["media_download"] is False
    reference = summary["attempts"][0]["extractor"]["format_reference"]
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
    assert summary["observation"]["probe"]["name"] == "youtube-unlock-multisignal"
    assert summary["observation"]["probe"]["version"] == "2"
    assert summary["observation"]["probe"]["tools"]["yt_dlp"]["version"]
    assert summary["observation"]["probe"]["signals"] == {
        "watch_player": "pass",
        "yt_dlp": "pass",
    }


@pytest.mark.parametrize(
    ("message", "verdict"),
    [
        ("Sign in to confirm you’re not a bot", "bot_challenge"),
        ("Login required", "authentication_required"),
        ("HTTP Error 429: Too Many Requests", "rate_limited"),
        ("This video is not available in your country", "service_unavailable"),
    ],
)
def test_unlock_requires_matching_service_failure_from_both_methods(
    tmp_path: Path, monkeypatch, ready, message: str, verdict: str
) -> None:
    calls = 0

    def fail(_config, logger):
        nonlocal calls
        calls += 1
        logger.error(message)
        raise DownloadError("extraction failed")

    monkeypatch.setattr("cfwarp_service_eval.youtube_unlock.extract_unlock_video", fail)
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: {"outcome": verdict},
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == verdict
    assert summary["observation"]["result"]["availability"] == "unavailable"
    assert calls == 1


def test_yt_dlp_regression_does_not_override_independent_service_success(
    tmp_path: Path, monkeypatch, ready
) -> None:
    def fail(_config, logger):
        logger.error("extractor implementation changed")
        raise DownloadError("regression")

    monkeypatch.setattr("cfwarp_service_eval.youtube_unlock.extract_unlock_video", fail)

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass_with_tooling_caveat"
    assert summary["observation"]["result"] == {
        "availability": "available",
        "class": "pass_with_tooling_caveat",
        "eligible": True,
    }
    assert summary["attempts"][0]["extractor"]["outcome"] == "extractor_failure"


def test_lax_diagnostic_regression_is_context_not_canonical_admission() -> None:
    fixture = json.loads(
        (
            contracts_root() / "fixtures" / "youtube-unlock-extractor-regression.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["canonical_admission"] is False
    assert fixture["video_id"] != FIXED_VIDEO_ID
    new_lane = fixture["cases"][:2]
    assert [case["outcome"] for case in new_lane] == [
        "pass",
        "extractor_bot_challenge",
    ]
    assert {case["egress"]["colo"] for case in fixture["cases"]} == {"LAX"}
    assert compose_signals("pass", "bot_challenge") == "pass_with_tooling_caveat"


def test_yt_dlp_success_alone_is_probe_dependent_and_fail_closed(
    tmp_path: Path, monkeypatch, ready
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: {
            "outcome": "unexpected_content",
            "content_error": "malformed_player_response",
        },
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: extracted_info(),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 2
    assert summary["verdict"] == "probe_dependent"
    assert summary["observation"]["result"] == {
        "availability": "unknown",
        "class": "probe_dependent",
        "eligible": False,
    }


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
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: extracted_info(**override),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass_with_tooling_caveat"
    assert summary["observation"]["result"] == {
        "availability": "available",
        "class": "pass_with_tooling_caveat",
        "eligible": True,
    }


def test_unlock_rejects_metadata_without_usable_format() -> None:
    assert select_format_reference({"formats": [{"format_id": "storyboard"}]}) is None


def test_independent_watch_parser_requires_metadata_and_direct_usable_format() -> None:
    player = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"videoId": FIXED_VIDEO_ID, "title": "test fixture"},
        "streamingData": {
            "formats": [
                {
                    "itag": 18,
                    "url": "https://media.example/videoplayback?secret=discarded",
                    "mimeType": 'video/mp4; codecs="avc1, mp4a"',
                    "contentLength": "1234",
                }
            ]
        },
    }
    parsed = player_response_from_watch_page(
        f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
    )

    result = validate_player_response(parsed)

    assert result["outcome"] == "pass"
    assert result["metadata"] == {"id": FIXED_VIDEO_ID, "title_present": True}
    assert result["format_reference"]["itag"] == 18
    assert "url" not in result["format_reference"]


def test_independent_check_makes_one_bounded_watch_request_without_media(
    tmp_path: Path, monkeypatch
) -> None:
    player = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"videoId": FIXED_VIDEO_ID, "title": "fixture"},
        "streamingData": {
            "formats": [
                {
                    "itag": 18,
                    "url": "https://media.example/videoplayback?secret=discarded",
                    "mimeType": "video/mp4",
                }
            ]
        },
    }
    calls = []

    class Response:
        status_code = 200
        url = FIXED_VIDEO_URL
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_raw(self):
            yield f"ytInitialPlayerResponse = {json.dumps(player)};".encode()

    class Client:
        def __init__(self, **options):
            calls.append(("client", options))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url):
            calls.append((method, url))
            return Response()

    monkeypatch.setattr("cfwarp_service_eval.youtube_unlock.httpx.Client", Client)

    result = check_watch_player(config(tmp_path))

    assert result["outcome"] == "pass"
    assert calls[1:] == [("GET", FIXED_VIDEO_URL)]
    assert calls[0][1]["timeout"] == 25
    assert calls[0][1]["follow_redirects"] is False


@pytest.mark.parametrize(
    "player",
    [
        {},
        {"playabilityStatus": {"status": "OK"}, "videoDetails": "malformed"},
        {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": FIXED_VIDEO_ID, "title": "fixture"},
            "streamingData": {"formats": [{"signatureCipher": "s=opaque"}]},
        },
    ],
)
def test_independent_player_validation_rejects_malformed_responses(player) -> None:
    assert validate_player_response(player)["outcome"] == "unexpected_content"


def test_verdict_composition_requires_independent_success_or_matching_denial() -> None:
    assert compose_signals("pass", "extractor_failure") == "pass_with_tooling_caveat"
    assert compose_signals("unexpected_content", "pass") == "probe_dependent"
    assert compose_signals("bot_challenge", "bot_challenge") == "bot_challenge"
    assert compose_signals("bot_challenge", "extractor_failure") == "probe_dependent"
    assert compose_signals("network_failure", "network_failure") == (
        "tooling_or_network_failure"
    )


@pytest.mark.parametrize(
    "format_values",
    [
        {"url": "https://media.example/video", "protocol": "https"},
        {
            "url": "ftp://media.example/video",
            "protocol": "https",
            "vcodec": "avc1",
        },
        {
            "url": "https:///missing-host",
            "protocol": "https",
            "vcodec": "avc1",
        },
        {
            "url": "https://media.example/video",
            "protocol": "https",
            "vcodec": "avc1",
            "has_drm": True,
        },
        {
            "url": "https://media.example/video",
            "protocol": "https",
            "acodec": "opus",
            "drm_family": "widevine",
        },
        {
            "url": "https://media.example/video",
            "protocol": "https",
            "vcodec": "avc1",
            "format_note": "DRM protected",
        },
    ],
)
def test_unlock_rejects_missing_codec_invalid_url_and_drm_formats(
    format_values,
) -> None:
    assert (
        select_format_reference(
            {"formats": [{"format_id": "candidate", **format_values}]}
        )
        is None
    )


def test_unlock_real_yt_dlp_seam_disables_format_checks_and_downloads(
    tmp_path: Path, monkeypatch
) -> None:
    calls: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            calls["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, url, *, download):
            calls["extract_info"] = (url, download)
            return {"id": FIXED_VIDEO_ID, "title": "test", "formats": []}

        def sanitize_info(self, info):
            calls["sanitized"] = info
            return info

    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.yt_dlp.YoutubeDL", FakeYoutubeDL
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.shutil.which",
        lambda command: "/usr/bin/deno" if command == "deno" else None,
    )

    metadata, _ = extract_unlock_video(
        config(tmp_path),
        CapturedLogger(),
    )

    options = calls["options"]
    assert isinstance(options, dict)
    assert options["check_formats"] is False
    assert options["skip_download"] is True
    assert options["cachedir"] is False
    assert options["remote_components"] == []
    assert options["js_runtimes"] == {"deno": {"path": "/usr/bin/deno"}}
    assert options["cookiefile"] is None
    assert options["cookiesfrombrowser"] is None
    assert calls["extract_info"] == (FIXED_VIDEO_URL, False)
    assert metadata["id"] == FIXED_VIDEO_ID


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
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not extract")),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: independent_success(),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass_with_tooling_caveat"
    assert summary["observation"]["result"]["availability"] == "available"


@pytest.mark.parametrize(
    ("identity", "machine"),
    [
        (
            {
                "path": "/usr/bin/deno",
                "version": "deno 2.9.7 (stable, release, x86_64-unknown-linux-gnu)",
            },
            "x86_64",
        ),
        (
            {
                "path": "/usr/bin/deno",
                "version": "deno 2.9.2-rc.1 (release candidate, x86_64-unknown-linux-gnu)",
            },
            "x86_64",
        ),
        (
            {
                "path": "/usr/bin/deno",
                "version": "deno 2.9.2 (stable, release, aarch64-unknown-linux-gnu)",
            },
            "x86_64",
        ),
        ({"path": "/usr/bin/deno", "version": "deno 2.9.2"}, "x86_64"),
        ({"path": None, "version": None}, "x86_64"),
        ({"path": "/usr/bin/deno", "version": "unknown"}, "x86_64"),
    ],
)
def test_pinned_deno_identity_rejects_wrong_or_unverified_runtime(
    identity: dict[str, str | None], machine: str
) -> None:
    assert pinned_deno_identity(identity, machine) is False


def test_pinned_deno_identity_accepts_real_pinned_output() -> None:
    identity = {
        "path": "/usr/bin/deno",
        "version": "deno 2.9.2 (stable, release, x86_64-unknown-linux-gnu)",
    }

    assert pinned_deno_identity(identity, "x86_64") is True


def test_pinned_deno_identity_accepts_real_arm64_output() -> None:
    identity = {
        "path": "/usr/bin/deno",
        "version": "deno 2.9.2 (stable, release, aarch64-unknown-linux-gnu)",
    }

    assert pinned_deno_identity(identity, "aarch64") is True


def test_pinned_ejs_assets_requires_both_packaged_solver_files(monkeypatch) -> None:
    class Asset:
        def __init__(self, present: bool):
            self.present = present

        def is_file(self) -> bool:
            return self.present

        def read_bytes(self) -> bytes:
            return b"solver" if self.present else b""

    class Solver:
        def joinpath(self, name: str) -> Asset:
            return Asset(name == "core.min.js")

    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.importlib.resources.files",
        lambda _package: Solver(),
    )
    assert pinned_ejs_assets() is False


def test_installed_pinned_ejs_assets_are_complete() -> None:
    assert pinned_ejs_assets() is True


@pytest.mark.parametrize(
    ("yt_dlp_version", "ejs_version", "deno_version"),
    [
        (
            "2026.7.4",
            "0.7.0",
            "deno 2.9.2 (stable, release, x86_64-unknown-linux-gnu)",
        ),
        (
            "2026.7.4",
            "0.8.0",
            "deno 2.9.7 (stable, release, x86_64-unknown-linux-gnu)",
        ),
        (
            "2026.7.5",
            "0.8.0",
            "deno 2.9.2 (stable, release, x86_64-unknown-linux-gnu)",
        ),
    ],
)
def test_unlock_requires_exact_solver_and_runtime_versions(
    tmp_path: Path,
    monkeypatch,
    yt_dlp_version: str,
    ejs_version: str,
    deno_version: str,
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.importlib.metadata.version",
        lambda package: ejs_version if package == "yt-dlp-ejs" else yt_dlp_version,
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.command_identity",
        lambda _command: {"path": "/usr/bin/deno", "version": deno_version},
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.platform.machine", lambda: "x86_64"
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_trace",
        lambda _config: ({"ok": True, "warp": "on", "location": "US"}, True),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not extract")),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: independent_success(),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass_with_tooling_caveat"


def test_unlock_has_whole_probe_deadline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_trace",
        lambda _config: (time.sleep(0.1), True),
    )

    summary, exit_code = run_probe(config(tmp_path, deadline_seconds=0.01))

    assert exit_code == 2
    assert summary["verdict"] == "probe_deadline_exceeded"
    assert summary["observation"]["result"]["eligible"] is False


def test_unlock_cli_rejects_deadline_beyond_catalog_budget(monkeypatch) -> None:
    from cfwarp_service_eval.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        ["cfwarp-service-eval", "youtube-unlock", "--deadline-seconds", "121"],
    )
    with pytest.raises(SystemExit, match="must be at most 120"):
        main()
