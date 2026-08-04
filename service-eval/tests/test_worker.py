import asyncio

import httpx
import pytest

from cfwarp_service_eval.worker import (
    Worker,
    private_listener,
    tailnet_listener_endpoint,
)


def test_browser_worker_accepts_tailnet_and_rejects_public_listener(monkeypatch):
    assert private_listener("socks5h://100.64.10.20:1080") is True
    assert private_listener("socks5h://[fd7a:115c:a1e0::20]:1080") is True
    assert private_listener("socks5h://10.0.0.2:1080") is False
    assert private_listener("socks5h://8.8.8.8:1080") is False
    monkeypatch.setattr(
        "cfwarp_service_eval.worker.socket.getaddrinfo",
        lambda *_: [(None, None, None, None, ("8.8.8.8", 0))],
    )
    assert private_listener("socks5h://spoofed.ts.net:1080") is False
    monkeypatch.setattr(
        "cfwarp_service_eval.worker.socket.getaddrinfo",
        lambda *_: [(None, None, None, None, ("100.64.10.20", 0))],
    )
    assert private_listener("socks5h://declared.ts.net:1080") is True
    assert (
        tailnet_listener_endpoint("socks5h://declared.ts.net:1080")
        == "socks5h://100.64.10.20:1080"
    )


def test_idle_worker_fails_closed_when_heartbeat_is_rejected():
    worker = object.__new__(Worker)
    worker.worker_class = "perf"
    worker.lease_seconds = 240
    worker.identity = lambda: {"worker_id": "perf-1"}

    def handler(request):
        if request.url.path == "/v2/jobs/claim":
            return httpx.Response(200, json={"job": None})
        return httpx.Response(401, json={"detail": "bad worker token"})

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://observer", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await worker.tick(client)

    asyncio.run(run())
