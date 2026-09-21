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
    select_player_format_evidence,
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


def independent_success(strength: str = "direct_usable"):
    return {
        "outcome": "pass",
        "metadata": {"id": FIXED_VIDEO_ID, "title_present": True},
        "format_evidence": {
            "strength": strength,
            "kind": (
                "format_url" if strength == "direct_usable" else "ciphered_format"
            ),
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
    assert summary["observation"]["probe"]["version"] == "4"
    assert summary["observation"]["probe"]["tools"]["yt_dlp"]["version"]
    assert summary["observation"]["probe"]["signals"] == {
        "watch_player": "pass",
        "watch_format_strength": "direct_usable",
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
    assert (
        compose_signals("pass", "bot_challenge", "direct_usable")
        == "pass_with_tooling_caveat"
    )


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


def test_advertised_watch_formats_plus_yt_dlp_usable_formats_pass(
    tmp_path: Path, monkeypatch, ready
) -> None:
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: independent_success("advertised_only"),
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: extracted_info(format_count=27),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert exit_code == 0
    assert summary["verdict"] == "pass"
    assert summary["observation"]["result"] == {
        "availability": "available",
        "class": "pass",
        "eligible": True,
    }
    assert summary["observation"]["probe"]["signals"] == {
        "watch_player": "pass",
        "watch_format_strength": "advertised_only",
        "yt_dlp": "pass",
    }


def test_33_descriptor_regression_plus_yt_dlp_formats_passes(
    tmp_path: Path, monkeypatch, ready
) -> None:
    descriptors = [
        {
            "itag": index + 100,
            "mimeType": "video/mp4" if index % 2 else "audio/mp4",
            "bitrate": 100_000 + index,
            "quality": "medium",
        }
        for index in range(33)
    ]
    independent = validate_player_response(
        {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": FIXED_VIDEO_ID, "title": "fixture"},
            "streamingData": {"formats": descriptors},
        }
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.check_watch_player",
        lambda _config: independent,
    )
    monkeypatch.setattr(
        "cfwarp_service_eval.youtube_unlock.extract_unlock_video",
        lambda *_args: extracted_info(format_count=27),
    )

    summary, exit_code = run_probe(config(tmp_path))

    assert independent["outcome"] == "pass"
    assert independent["format_evidence"]["strength"] == "descriptor_advertised"
    assert independent["format_evidence"]["diagnostics"] == {
        "descriptor_count": 33,
        "valid_descriptor_count": 33,
        "malformed_descriptor_count": 0,
        "audio_video_mime_count": 33,
        "direct_url_count": 0,
        "cipher_only_count": 0,
        "manifest_count": 0,
    }
    assert independent["format_evidence"]["reference"] == {
        "itag": 100,
        "mime_type": "audio/mp4",
        "content_length_present": False,
    }
    assert exit_code == 0
    assert summary["verdict"] == "pass"
    assert summary["observation"]["result"]["availability"] == "available"
    assert summary["observation"]["probe"]["signals"] == {
        "watch_player": "pass",
        "watch_format_strength": "descriptor_advertised",
        "yt_dlp": "pass",
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
    assert result["format_evidence"]["strength"] == "direct_usable"
    assert result["format_evidence"]["reference"]["itag"] == 18
    assert "url" not in result["format_evidence"]["reference"]


def test_independent_ciphered_formats_are_advertised_but_not_directly_usable() -> None:
    evidence = select_player_format_evidence(
        {
            "streamingData": {
                "adaptiveFormats": [
                    {
                        "itag": 137,
                        "mimeType": 'video/mp4; codecs="avc1"',
                        "signatureCipher": (
                            "url=https%3A%2F%2Fmedia.example%2Fvideoplayback"
                            "&sp=sig&s=opaque"
                        ),
                    }
                ]
            }
        }
    )

    assert evidence["strength"] == "advertised_only"
    assert evidence["kind"] == "ciphered_format"
    assert evidence["diagnostics"] == {
        "descriptor_count": 1,
        "valid_descriptor_count": 1,
        "malformed_descriptor_count": 0,
        "audio_video_mime_count": 1,
        "direct_url_count": 0,
        "cipher_only_count": 1,
        "manifest_count": 0,
    }
    assert "url" not in evidence["reference"]


@pytest.mark.parametrize(
    "cipher",
    [
        "s=opaque",
        "url=ftp%3A%2F%2Fmedia.example%2Fvideo&s=opaque",
        "url=https%3A%2F%2Fmedia.example%2Fvideo",
    ],
)
def test_valid_descriptor_with_malformed_cipher_is_descriptor_advertised(
    cipher: str,
) -> None:
    evidence = select_player_format_evidence(
        {
            "streamingData": {
                "formats": [
                    {
                        "itag": 18,
                        "mimeType": "video/mp4",
                        "signatureCipher": cipher,
                    }
                ]
            }
        }
    )

    assert evidence["strength"] == "descriptor_advertised"
    assert evidence["diagnostics"]["valid_descriptor_count"] == 1
    assert evidence["diagnostics"]["cipher_only_count"] == 0


@pytest.mark.parametrize(
    "formats",
    [
        "not-a-list",
        [None],
        [{"itag": "18", "mimeType": "video/mp4"}],
        [{"itag": 0, "mimeType": "video/mp4"}],
        [{"itag": True, "mimeType": "video/mp4"}],
        [{"itag": 18, "mimeType": "application/json"}],
        [{"itag": 18, "mimeType": None}],
    ],
)
def test_independent_rejects_malformed_descriptor_structure(formats) -> None:
    evidence = select_player_format_evidence({"streamingData": {"formats": formats}})

    assert evidence["strength"] == "malformed"
    assert evidence["diagnostics"]["valid_descriptor_count"] == 0
    assert evidence["diagnostics"]["malformed_descriptor_count"] >= 1


@pytest.mark.parametrize(
    ("manifest", "strength"),
    [
        ("https://manifest.example/video.m3u8", "direct_usable"),
        ("ftp://manifest.example/video.m3u8", "malformed"),
        ("https:///missing-host", "malformed"),
    ],
)
def test_independent_manifest_must_be_direct_http(manifest: str, strength: str) -> None:
    evidence = select_player_format_evidence(
        {"streamingData": {"hlsManifestUrl": manifest}}
    )

    assert evidence["strength"] == strength
    assert evidence["diagnostics"]["manifest_count"] == (
        1 if strength == "direct_usable" else 0
    )


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
        headers = {
            "content-type": "text/html; charset=utf-8",
            "content-encoding": "identity",
        }

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
    assert result["diagnostics"]["content_type"] == "text/html; charset=utf-8"
    assert result["diagnostics"]["content_encoding"] == "identity"
    assert result["diagnostics"]["body_bytes"] > 0
    assert result["diagnostics"]["body_sha256"].startswith("sha256:")
    assert result["diagnostics"]["assignment_counts"] == {
        "assignment": 1,
        "object_property": 0,
    }
    assert result["diagnostics"]["parse_branch"] == "assignment_object"
    assert result["diagnostics"]["formats"]["direct_url_count"] == 1
    assert "media.example" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


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


@pytest.mark.parametrize(
    ("strength", "extractor", "verdict"),
    [
        ("direct_usable", "pass", "pass"),
        ("direct_usable", "extractor_failure", "pass_with_tooling_caveat"),
        ("direct_usable", "tooling_failure", "pass_with_tooling_caveat"),
        ("direct_usable", "bot_challenge", "pass_with_tooling_caveat"),
        ("advertised_only", "pass", "pass"),
        ("advertised_only", "extractor_failure", "probe_dependent"),
        ("advertised_only", "tooling_failure", "probe_dependent"),
        ("advertised_only", "bot_challenge", "probe_dependent"),
        ("descriptor_advertised", "pass", "pass"),
        ("descriptor_advertised", "extractor_failure", "probe_dependent"),
        ("descriptor_advertised", "tooling_failure", "probe_dependent"),
        ("descriptor_advertised", "bot_challenge", "probe_dependent"),
        ("absent", "pass", "probe_dependent"),
        ("malformed", "pass", "probe_dependent"),
    ],
)
def test_verdict_composition_models_independent_format_strength(
    strength: str, extractor: str, verdict: str
) -> None:
    assert compose_signals("pass", extractor, strength) == verdict


def test_verdict_composition_requires_matching_service_denial() -> None:
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
