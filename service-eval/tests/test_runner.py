import asyncio
import sys

import pytest

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
