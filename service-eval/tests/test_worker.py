from cfwarp_service_eval.worker import private_listener


def test_browser_worker_accepts_tailnet_and_rejects_public_listener():
    assert private_listener("socks5h://100.64.10.20:1080") is True
    assert private_listener("socks5h://[fd7a:115c:a1e0::20]:1080") is True
    assert private_listener("socks5h://10.0.0.2:1080") is False
    assert private_listener("socks5h://8.8.8.8:1080") is False
