import asyncio
import sys

import pytest

from cfwarp_service_eval.config import Lane
from cfwarp_service_eval.runner import (
    ProbeError,
    ProbeRunner,
    enforce_artifact_limit,
    safe_environment,
)


def test_safe_environment_uses_ephemeral_browser_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/probe")
    monkeypatch.setenv("AGENT_BROWSER_RUNTIME_HOME", "/tmp/browser-runtime")
    monkeypatch.setenv(
        "AGENT_BROWSER_EXECUTABLE_PATH", "/usr/local/bin/agent-browser-chrome"
    )
    monkeypatch.setenv("CFWARP_EVAL_CONTRACTS_ROOT", "/app/contracts")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.invalid")

    environment = safe_environment()

    assert environment["HOME"] == "/tmp/browser-runtime"
    assert environment["AGENT_BROWSER_EXECUTABLE_PATH"] == (
        "/usr/local/bin/agent-browser-chrome"
    )
    assert environment["AGENT_BROWSER_DOWNLOADS_DISABLED"] == "1"
    assert environment["CFWARP_EVAL_CONTRACTS_ROOT"] == "/app/contracts"
    assert "HTTP_PROXY" not in environment


def test_subprocess_deadline_kills_the_process_group(tmp_path):
    runner = ProbeRunner(tmp_path)
    with pytest.raises(ProbeError, match="deadline exceeded"):
        asyncio.run(
            runner._run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=0.01,
            )
        )


def test_artifact_limit_fails_closed(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    (output / "bounded.json").write_bytes(b"x" * 32)
    enforce_artifact_limit(output, 32)
    with pytest.raises(ProbeError, match="exceed contract limit"):
        enforce_artifact_limit(output, 31)


def test_scheduled_youtube_unlock_is_capped_at_catalog_deadline(tmp_path):
    runner = ProbeRunner(tmp_path, deadline_seconds=180)
    captured = {}

    async def capture(command, timeout, check=True):
        captured.update(command=command, timeout=timeout, check=check)
        return ""

    runner._run = capture
    lane = Lane(
        id="test",
        proxy="socks5h://proxy-host-1:1080",
        instance_id="test-instance",
        node_id="proxy-host-1",
        composition="direct-warp",
        transport="wireguard",
        substrate_profile=None,
        requested_region=None,
        image_identity="example@sha256:" + "a" * 64,
        config_digest="sha256:" + "b" * 64,
    )

    with pytest.raises(ProbeError, match="without a summary"):
        asyncio.run(runner.run("test-run", lane, "youtube-unlock"))

    assert captured["timeout"] == 120
    deadline_index = captured["command"].index("--deadline-seconds")
    assert captured["command"][deadline_index + 1] == "120"
