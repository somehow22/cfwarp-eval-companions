import json

import pytest

from cfwarp_service_eval.config import load_lanes


def lane(proxy="socks5h://proxy-host-1:16710"):
    return {
        "id": "direct-de",
        "proxy": proxy,
        "instance_id": "cfwarp-direct-de",
        "node_id": "proxy-host-1",
        "composition": "direct",
        "transport": "wireguard",
        "substrate_profile": None,
        "requested_region": "DE",
        "image_identity": "example@sha256:" + "a" * 64,
        "config_digest": "sha256:" + "b" * 64,
    }


def test_lane_allowlist_loads_without_exposing_proxy(tmp_path):
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps([lane()]))
    loaded = load_lanes(path)["direct-de"]
    assert loaded.proxy == "socks5h://proxy-host-1:16710"
    assert "proxy" not in loaded.public()


@pytest.mark.parametrize(
    "proxy",
    [
        "http://proxy-host-1:16710",
        "socks5h://user:pass@proxy-host-1:16710",
        "socks5h://proxy-host-1:16710/path",
        "socks5h://proxy-host-1",
    ],
)
def test_lane_allowlist_rejects_unsafe_proxy_shapes(tmp_path, proxy):
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps([lane(proxy)]))
    with pytest.raises(ValueError):
        load_lanes(path)


def test_lane_allowlist_rejects_unknown_fields(tmp_path):
    value = lane()
    value["target_url"] = "http://169.254.169.254/"
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps([value]))
    with pytest.raises(ValueError, match="unknown lane fields"):
        load_lanes(path)
