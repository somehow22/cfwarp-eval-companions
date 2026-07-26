import json

from cfwarp_service_eval import perf
from cfwarp_service_eval.perf import PerfConfig, run_probe


def fake_curl(trace_warp="on", speeds=(6.0, 6.5, 7.0), fail_on_sample=False):
    calls = {"n": 0}

    def curl(config, url, write_out):
        if url == perf.TRACE_URL:
            return f"warp={trace_warp}\nloc=DE\ncolo=FRA\n"
        if fail_on_sample:
            raise RuntimeError("curl exit 28")
        speed = speeds[calls["n"] % len(speeds)]
        calls["n"] += 1
        return f"{speed * perf.MIB} 4.0 {config.transfer_bytes}"

    return curl


def config(tmp_path, **kwargs):
    return PerfConfig(
        proxy="socks5h://127.0.0.1:1080", output=tmp_path / "run", **kwargs
    )


def test_pass_above_floor_records_throughput_and_meets_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "curl", fake_curl())
    summary, code = run_probe(config(tmp_path, floor_mibps=5.0))
    assert code == 0
    assert summary["verdict"] == "pass"
    observation = summary["observation"]
    assert observation["scenario_id"] == "perf.throughput_sample"
    assert observation["perf"]["meets_floor"] is True
    assert observation["perf"]["throughput_mibps"] == 6.5
    assert observation["result"]["availability"] == "available"


def test_below_floor_is_eligible_unavailable_not_a_tooling_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(perf, "curl", fake_curl(speeds=(1.0, 1.2, 1.1)))
    summary, code = run_probe(config(tmp_path, floor_mibps=5.0))
    assert code == 2
    assert summary["verdict"] == "degraded_throughput"
    observation = summary["observation"]
    assert observation["perf"]["meets_floor"] is False
    assert observation["result"] == {
        "availability": "unavailable",
        "class": "degraded_throughput",
        "eligible": True,
    }


def test_substrate_lane_without_a_floor_records_evidence_and_passes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(perf, "curl", fake_curl(speeds=(0.4, 0.5, 0.6)))
    summary, code = run_probe(config(tmp_path))
    assert code == 0
    assert summary["verdict"] == "pass"
    assert summary["observation"]["perf"]["meets_floor"] is None
    assert summary["observation"]["perf"]["floor_mibps"] is None


def test_off_warp_listener_is_not_measured_as_lane_throughput(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "curl", fake_curl(trace_warp="off"))
    summary, code = run_probe(config(tmp_path, floor_mibps=5.0))
    assert code == 2
    assert summary["verdict"] == "listener_not_on_warp"
    assert summary["observation"]["failure_layer"] == "warp-core"
    assert summary["samples"] == []


def test_transfer_failure_is_ineligible_tooling_not_a_service_verdict(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(perf, "curl", fake_curl(fail_on_sample=True))
    summary, code = run_probe(config(tmp_path, floor_mibps=5.0))
    assert code == 2
    assert summary["observation"]["result"] == {
        "availability": "unknown",
        "class": "tooling_failure",
        "eligible": False,
    }


def test_artifacts_stay_within_the_declared_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "curl", fake_curl())
    run_probe(config(tmp_path, floor_mibps=5.0))
    written = sorted(item.name for item in (tmp_path / "run").iterdir())
    assert written == ["summary.json", "verdict.txt"]
    payload = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert payload["observation"]["artifacts"] == [
        {"kind": "summary", "path": "summary.json"},
        {"kind": "verdict", "path": "verdict.txt"},
    ]
