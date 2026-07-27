from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


LANE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SCENARIOS = {
    "turnstile-reference": "turnstile-reference.interactive_test_widget_render",
    "youtube": "youtube.anonymous_public_video",
    "gemini": "gemini.anonymous_entry",
    "chatgpt": "chatgpt.anonymous_entry",
    "google-search": "google-search.anonymous_search_results",
    "reddit": "reddit.anonymous_public_listing",
    "perf": "perf.throughput_sample",
}
BROWSER_SCENARIOS = frozenset(
    {
        "turnstile-reference",
        "gemini",
        "chatgpt",
        "google-search",
        "reddit",
    }
)
LIGHTWEIGHT_SCENARIOS = frozenset(SCENARIOS) - BROWSER_SCENARIOS

# Direct lanes carry the repeat-gate floor. Substrate throughput is a property
# of the provider, so those lanes record evidence without a pass/fail floor.
DIRECT_THROUGHPUT_FLOOR_MIBPS = 5.0


@dataclass(frozen=True)
class Lane:
    id: str
    proxy: str
    instance_id: str
    node_id: str
    composition: str
    transport: str
    substrate_profile: str | None
    requested_region: str | None
    image_identity: str
    config_digest: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instance_id": self.instance_id,
            "node_id": self.node_id,
            "composition": self.composition,
            "transport": self.transport,
            "substrate_profile": self.substrate_profile,
            "requested_region": self.requested_region,
            "image_identity": self.image_identity,
            "config_digest": self.config_digest,
        }


def load_lanes(path: Path) -> dict[str, Lane]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("lane allowlist must be a non-empty JSON array")
    lanes: dict[str, Lane] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each lane must be an object")
        unknown = set(item) - {
            "id",
            "proxy",
            "instance_id",
            "node_id",
            "composition",
            "transport",
            "substrate_profile",
            "requested_region",
            "image_identity",
            "config_digest",
        }
        if unknown:
            raise ValueError(f"unknown lane fields: {sorted(unknown)}")
        lane_id = required_text(item, "id")
        if not LANE_ID.fullmatch(lane_id) or lane_id in lanes:
            raise ValueError(f"invalid or duplicate lane id: {lane_id}")
        proxy = required_text(item, "proxy")
        parsed = urlsplit(proxy)
        if parsed.scheme != "socks5h" or not parsed.hostname or parsed.port is None:
            raise ValueError(f"lane {lane_id} proxy must be socks5h://host:port")
        if (
            parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"lane {lane_id} proxy must not contain credentials or URL extras"
            )
        lanes[lane_id] = Lane(
            id=lane_id,
            proxy=proxy,
            instance_id=required_text(item, "instance_id"),
            node_id=required_text(item, "node_id"),
            composition=required_text(item, "composition"),
            transport=required_text(item, "transport"),
            substrate_profile=optional_text(item, "substrate_profile"),
            requested_region=optional_text(item, "requested_region"),
            image_identity=required_text(item, "image_identity"),
            config_digest=required_text(item, "config_digest"),
        )
    return lanes


def config_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def required_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"lane field {key} must be a non-empty string")
    return value


def optional_text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"lane field {key} must be null or a non-empty string")
    return value
