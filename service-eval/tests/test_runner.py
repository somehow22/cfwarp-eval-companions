import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from jsonschema import ValidationError

from cfwarp_service_eval import cli
from cfwarp_service_eval.artifacts import write_summary
from cfwarp_service_eval.config import Lane
from cfwarp_service_eval.contracts import contracts_root
from cfwarp_service_eval.runner import (
    ProbeError,
    ProbeRunner,
    enforce_artifact_limit,
    safe_environment,
)
from cfwarp_service_eval.worker import Worker


def lane():
    return Lane(
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


def observation(identifier="current", availability="available", schema_version=1):
    payload = json.loads(
        (contracts_root() / "fixtures" / "observation-valid.json").read_text()
    )
    payload["schema_version"] = schema_version
    payload["observation_id"] = identifier
    payload["scenario_id"] = "youtube.anonymous_public_video_unlock"
    payload["result"] = {
        "class": "pass" if availability == "available" else availability,
        "availability": availability,
        "eligible": availability != "unknown",
    }
    return payload


CHILD = """
import json, os, signal, sys, time
from pathlib import Path
output, mode, payload = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
summary = json.dumps({'observation': payload})
if mode == 'temporary':
    (output / '.summary-incomplete').write_text(summary)
elif mode == 'truncated':
    (output / 'summary.json').write_text('{"observation":')
elif mode not in ('missing', 'kill_early'):
    (output / 'summary.json').write_text(summary)
if mode == 'overlap':
    time.sleep(0.05)
if mode in ('kill', 'kill_early'):
    os.kill(os.getpid(), signal.SIGKILL)
if mode == 'crash':
    sys.exit(2)
if mode == 'exit137':
    sys.exit(137)
"""


def subprocess_runner(runner, modes, observations):
    real_run = runner._run
    outputs = []

    async def invoke(command, timeout):
        output = Path(command[command.index("--output") + 1])
        assert "--worker-mode" in command
        assert output.is_dir()
        index = len(outputs)
        outputs.append(output)
        await real_run(
            [
                sys.executable,
                "-c",
                CHILD,
                str(output),
                modes[index],
                json.dumps(observations[index]),
            ],
            timeout,
        )

    runner._run = invoke
    return outputs


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

    async def capture(command, timeout):
        captured.update(command=command, timeout=timeout)
        return ""

    runner._run = capture

    with pytest.raises(ProbeError, match="without a summary"):
        asyncio.run(runner.run("test-run", lane(), "youtube-unlock"))

    assert captured["timeout"] == 120
    assert "--worker-mode" in captured["command"]
    deadline_index = captured["command"].index("--deadline-seconds")
    assert captured["command"][deadline_index + 1] == "120"


@pytest.mark.parametrize("mode", ["missing", "kill_early"])
def test_prior_positive_summary_cannot_rescue_new_child(tmp_path, mode):
    runner = ProbeRunner(tmp_path)
    parent = tmp_path / "group" / "test" / "youtube-unlock"
    parent.mkdir(parents=True)
    previous = json.dumps({"observation": observation("old-positive")})
    (parent / "summary.json").write_text(previous)
    sibling = parent / "older-invocation"
    sibling.mkdir()
    (sibling / "summary.json").write_text(previous)
    outputs = subprocess_runner(runner, [mode], [observation()])

    with pytest.raises(ProbeError, match="without a summary|subprocess failed"):
        asyncio.run(runner.run("group", lane(), "youtube-unlock"))

    assert outputs[0] != sibling
    assert not (outputs[0] / "summary.json").exists()
    assert (sibling / "summary.json").read_text() == previous


@pytest.mark.parametrize("mode", ["crash", "exit137", "kill"])
def test_new_valid_pass_followed_by_abnormal_exit_is_not_accepted(tmp_path, mode):
    runner = ProbeRunner(tmp_path)
    outputs = subprocess_runner(runner, [mode], [observation()])

    with pytest.raises(ProbeError, match="subprocess failed"):
        asyncio.run(runner.run("group", lane(), "youtube-unlock"))

    assert (
        json.loads((outputs[0] / "summary.json").read_text())["observation"]["result"][
            "availability"
        ]
        == "available"
    )


@pytest.mark.parametrize(
    "mode,error",
    [
        ("temporary", ProbeError),
        ("truncated", json.JSONDecodeError),
        ("malformed", ValidationError),
    ],
)
def test_unfinalized_or_invalid_summary_is_rejected(tmp_path, mode, error):
    runner = ProbeRunner(tmp_path)
    payload = observation() if mode != "malformed" else {"schema_version": 1}
    outputs = subprocess_runner(runner, [mode], [payload])

    with pytest.raises(error):
        asyncio.run(runner.run("group", lane(), "youtube-unlock"))

    assert len(outputs) == 1
    if mode == "temporary":
        assert not (outputs[0] / "summary.json").exists()
        assert (outputs[0] / ".summary-incomplete").is_file()


def test_overlapping_attempts_keep_exclusive_results_and_support_v1_v2(tmp_path):
    runner = ProbeRunner(tmp_path)
    outputs = subprocess_runner(
        runner,
        ["overlap", "overlap"],
        [
            observation("first", schema_version=1),
            observation("second", schema_version=2),
        ],
    )

    async def run_both():
        return await asyncio.gather(
            runner.run("group", lane(), "youtube-unlock"),
            runner.run("group", lane(), "youtube-unlock"),
        )

    first, second = asyncio.run(run_both())
    assert outputs[0] != outputs[1]
    assert first["observation_id"] == "first"
    assert second["observation_id"] == "second"
    assert first["schema_version"] == second["schema_version"] == 2
    assert first["scenario_provenance"] == second["scenario_provenance"]


def test_overlapping_positive_attempt_cannot_rescue_missing_retry(tmp_path):
    runner = ProbeRunner(tmp_path)
    outputs = subprocess_runner(
        runner, ["overlap", "missing"], [observation("old"), observation("retry")]
    )

    async def run_both():
        return await asyncio.gather(
            runner.run("group", lane(), "youtube-unlock"),
            runner.run("group", lane(), "youtube-unlock"),
            return_exceptions=True,
        )

    old, retry = asyncio.run(run_both())
    assert old["observation_id"] == "old"
    assert isinstance(retry, ProbeError)
    assert str(retry) == "probe exited without a summary"
    assert (outputs[0] / "summary.json").is_file()
    assert not (outputs[1] / "summary.json").exists()


@pytest.mark.parametrize("availability", ["unavailable", "unknown"])
def test_normal_finalized_nonpass_is_still_a_completion(tmp_path, availability):
    runner = ProbeRunner(tmp_path)
    subprocess_runner(runner, ["pass"], [observation(availability=availability)])
    result = asyncio.run(runner.run("group", lane(), "youtube-unlock"))
    assert result["result"]["availability"] == availability
    assert result["result"]["eligible"] is (availability == "unavailable")


def test_aggregate_scenario_limit_counts_old_attempts_and_temporary_files(
    tmp_path, monkeypatch
):
    from cfwarp_service_eval.config import SCENARIO_DEFINITIONS

    runner = ProbeRunner(tmp_path)
    parent = tmp_path / "group" / "test" / "youtube-unlock"
    old = parent / "failed-invocation"
    old.mkdir(parents=True)
    (old / ".summary-incomplete").write_bytes(b"x" * 2_048)
    monkeypatch.setitem(
        SCENARIO_DEFINITIONS["youtube-unlock"], "artifact_limit_bytes", 2_048
    )
    outputs = subprocess_runner(runner, ["pass"], [observation()])

    with pytest.raises(ProbeError, match="exceed contract limit"):
        asyncio.run(runner.run("group", lane(), "youtube-unlock"))

    assert (outputs[0] / "summary.json").is_file()


def test_summary_is_atomically_replaced_and_temp_is_removed_on_error(
    tmp_path, monkeypatch
):
    from cfwarp_service_eval import artifacts

    original = os.replace
    target = tmp_path / "summary.json"

    def inspect(source, destination):
        assert not target.exists()
        assert Path(source).read_text() == '{\n  "observation": {}\n}\n'
        original(source, destination)

    monkeypatch.setattr(artifacts.os, "replace", inspect)
    write_summary(tmp_path, {"observation": {}})
    assert target.is_file()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["summary.json"]

    def fail(_source, _destination):
        raise OSError("publication failed")

    monkeypatch.setattr(artifacts.os, "replace", fail)
    with pytest.raises(OSError, match="publication failed"):
        write_summary(tmp_path, {"observation": {"schema_version": 2}})
    assert json.loads(target.read_text()) == {"observation": {}}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["summary.json"]


@pytest.mark.parametrize("scenario", ["youtube", "youtube-unlock", "perf"])
@pytest.mark.parametrize("worker_mode", [False, True])
@pytest.mark.parametrize("availability", ["unavailable", "unknown"])
def test_cli_operator_nonpass_exit_two_but_worker_exit_zero(
    tmp_path, monkeypatch, capsys, scenario, worker_mode, availability
):
    probe = {
        "youtube": "run_probe",
        "youtube-unlock": "run_youtube_unlock_probe",
        "perf": "run_perf_probe",
    }[scenario]

    def finalized(config):
        config.output.mkdir(parents=True, exist_ok=True)
        (config.output / "verdict.txt").write_text(f"Service verdict: {availability}\n")
        return {"observation": observation(availability=availability)}, 2

    monkeypatch.setattr(cli, probe, finalized)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cfwarp-service-eval", scenario, "--output", str(tmp_path)]
        + (["--worker-mode"] if worker_mode else []),
    )
    assert cli.main() == (0 if worker_mode else 2)
    assert capsys.readouterr().out == f"Service verdict: {availability}\n"


@pytest.mark.parametrize(
    "mode,availability,endpoint",
    [
        ("pass", "available", "complete"),
        ("pass", "unknown", "complete"),
        ("kill", "available", "fail"),
    ],
)
def test_worker_submits_only_normal_current_child_result(
    tmp_path, mode, availability, endpoint
):
    runner = ProbeRunner(tmp_path)
    subprocess_runner(runner, [mode], [observation(availability=availability)])
    worker = object.__new__(Worker)
    worker.worker_class = "perf"  # No heartbeat during this focused dispatch test.
    worker.lease_seconds = 240
    worker.runner = runner
    worker.max_artifact_bytes = 512 * 1024 * 1024
    worker.identity = lambda: {"worker_id": "test"}
    sent = []

    def handler(request):
        if request.url.path == "/v2/jobs/claim":
            return httpx.Response(
                200,
                json={
                    "job": {
                        "task_id": 1,
                        "group_id": "group",
                        "scenario_id": "youtube-unlock",
                        "lane": vars(lane()),
                        "lease_token": "lease",
                    }
                },
            )
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"disposition": "accepted"})

    async def run():
        async with httpx.AsyncClient(
            base_url="http://observer", transport=httpx.MockTransport(handler)
        ) as client:
            await worker.tick(client)

    asyncio.run(run())
    assert [path for path, _ in sent] == [f"/v2/jobs/1/{endpoint}"]
    if endpoint == "complete":
        assert sent[0][1]["observation"]["result"]["availability"] == availability
    else:
        assert sent[0][1]["error"] == "ProbeError"
