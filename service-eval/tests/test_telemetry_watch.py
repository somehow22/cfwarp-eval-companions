import httpx

from cfwarp_service_eval.telemetry_watch import exported_points, tick


METRICS = (
    "# TYPE otelcol_exporter_sent_metric_points_total counter\n"
    'otelcol_exporter_sent_metric_points_total{exporter="datadog",service_instance_id="one"} 12\n'
)


def test_exported_points_accepts_prometheus_counter_suffix():
    assert exported_points(METRICS) == 12


def test_heartbeat_posts_only_after_export_counter_advances(tmp_path):
    state = {"count": 12, "posts": 0}

    def collector_handler(_request):
        return httpx.Response(
            200,
            text=METRICS.replace(" 12", f" {state['count']}"),
        )

    def observer_handler(request):
        assert request.url.path == "/v2/telemetry-export-heartbeat"
        state["posts"] += 1
        return httpx.Response(202, json={"disposition": "accepted"})

    collector = httpx.Client(
        base_url="http://collector",
        transport=httpx.MockTransport(collector_handler),
    )
    observer = httpx.Client(
        base_url="http://observer",
        transport=httpx.MockTransport(observer_handler),
    )
    state_file = tmp_path / "state.json"
    assert tick(collector, observer, state_file) is False
    state["count"] = 13
    assert tick(collector, observer, state_file) is True
    assert state["posts"] == 1
