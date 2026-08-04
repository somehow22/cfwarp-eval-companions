from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import scenario_definitions

LANE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
REGION = re.compile(r"^[A-Z]{2}$")
CAPABILITY_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SCENARIO_DEFINITIONS = scenario_definitions()
SCENARIOS = {
    scenario_id: definition["scenario_id"]
    for scenario_id, definition in SCENARIO_DEFINITIONS.items()
}
BROWSER_SCENARIOS = frozenset(
    scenario_id
    for scenario_id, definition in SCENARIO_DEFINITIONS.items()
    if definition["execution_class"] == "browser"
)
LIGHTWEIGHT_SCENARIOS = frozenset(SCENARIOS) - BROWSER_SCENARIOS
SCENARIO_ALIASES = {
    **{key: key for key in SCENARIOS},
    **{value: key for key, value in SCENARIOS.items()},
}

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
    deployment_origin: str = "legacy-unattributed"
    substrate: str = "direct"
    requested_region_raw: str | None = None
    cloudflare_proto: str = "warp"
    ip_proto_stack: str = "v4"
    config_generation: str = "legacy"
    browser_proxy: str | None = None
    scenarios: tuple[str, ...] = tuple(SCENARIOS)

    def __post_init__(self) -> None:
        try:
            normalized = tuple(SCENARIO_ALIASES[value] for value in self.scenarios)
        except KeyError as error:
            raise ValueError(f"unknown lane scenario: {error.args[0]}") from error
        object.__setattr__(self, "scenarios", normalized)

    @property
    def capability_id(self) -> str:
        return "-".join(
            (
                self.substrate,
                self.requested_region or "ZZ",
                self.cloudflare_proto,
                self.ip_proto_stack,
            )
        )

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
            "deployment_origin": self.deployment_origin,
            "substrate": self.substrate,
            "requested_region_raw": self.requested_region_raw,
            "cloudflare_proto": self.cloudflare_proto,
            "ip_proto_stack": self.ip_proto_stack,
            "config_generation": self.config_generation,
            "capability_id": self.capability_id,
            "scenarios": [SCENARIOS[scenario] for scenario in self.scenarios],
        }

    def internal(self, worker_class: str = "light") -> dict[str, Any]:
        """Worker-facing descriptor. This is only returned by the worker API."""
        proxy = (
            self.browser_proxy
            if worker_class == "browser" and self.browser_proxy
            else self.proxy
        )
        return {**self.public(), "proxy": proxy}


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
            "deployment_origin",
            "substrate",
            "requested_region_raw",
            "cloudflare_proto",
            "ip_proto_stack",
            "config_generation",
            "browser_proxy",
            "scenarios",
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
        browser_proxy = optional_text(item, "browser_proxy")
        if browser_proxy is not None:
            browser_parsed = urlsplit(browser_proxy)
            if (
                browser_parsed.scheme != "socks5h"
                or not browser_parsed.hostname
                or browser_parsed.port is None
                or browser_parsed.username
                or browser_parsed.password
                or browser_parsed.path
                or browser_parsed.query
                or browser_parsed.fragment
            ):
                raise ValueError(
                    f"lane {lane_id} browser_proxy must be socks5h://host:port without credentials"
                )
        raw_scenarios = item.get("scenarios", list(SCENARIOS))
        if (
            not isinstance(raw_scenarios, list)
            or not raw_scenarios
            or not all(isinstance(value, str) and value for value in raw_scenarios)
        ):
            raise ValueError(
                f"lane {lane_id} scenarios must be a non-empty string array"
            )
        try:
            lane_scenarios = tuple(SCENARIO_ALIASES[value] for value in raw_scenarios)
        except KeyError as error:
            raise ValueError(
                f"lane {lane_id} has unknown scenario: {error.args[0]}"
            ) from error
        if len(lane_scenarios) != len(set(lane_scenarios)):
            raise ValueError(f"lane {lane_id} scenarios must be unique")
        if (
            "scenarios" in item
            and BROWSER_SCENARIOS.intersection(lane_scenarios)
            and browser_proxy is None
        ):
            raise ValueError(
                f"lane {lane_id} declares browser scenarios without browser_proxy"
            )
        requested_region_raw = optional_text(item, "requested_region_raw")
        if requested_region_raw is None:
            requested_region_raw = optional_text(item, "requested_region")
        requested_region = normalize_region(requested_region_raw)
        substrate = optional_text(item, "substrate") or infer_substrate(
            required_text(item, "composition"), optional_text(item, "substrate_profile")
        )
        if not CAPABILITY_COMPONENT.fullmatch(substrate):
            raise ValueError(
                f"lane {lane_id} substrate must be a lowercase identity component"
            )
        cloudflare_proto = optional_text(
            item, "cloudflare_proto"
        ) or infer_cloudflare_proto(required_text(item, "transport"))
        if cloudflare_proto not in {"warp", "masque"}:
            raise ValueError(f"lane {lane_id} cloudflare_proto must be warp or masque")
        ip_proto_stack = optional_text(item, "ip_proto_stack") or "v4"
        if ip_proto_stack not in {"v4", "v6"}:
            raise ValueError(f"lane {lane_id} ip_proto_stack must be v4 or v6")
        node_id = required_text(item, "node_id")
        lanes[lane_id] = Lane(
            id=lane_id,
            proxy=proxy,
            instance_id=required_text(item, "instance_id"),
            node_id=node_id,
            composition=required_text(item, "composition"),
            transport=required_text(item, "transport"),
            substrate_profile=optional_text(item, "substrate_profile"),
            requested_region=requested_region,
            image_identity=required_text(item, "image_identity"),
            config_digest=required_text(item, "config_digest"),
            # Legacy allowlists remain readable for rollback, but their origin
            # is explicit and discovery will reject observations without the
            # matching generation. Deployment templates require both fields.
            deployment_origin=optional_text(item, "deployment_origin")
            or f"legacy-{node_id}",
            substrate=substrate,
            requested_region_raw=requested_region_raw,
            cloudflare_proto=cloudflare_proto,
            ip_proto_stack=ip_proto_stack,
            config_generation=optional_text(item, "config_generation")
            or required_text(item, "config_digest"),
            browser_proxy=browser_proxy,
            scenarios=lane_scenarios,
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


def normalize_region(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if normalized == "UK":
        normalized = "GB"
    if not REGION.fullmatch(normalized):
        raise ValueError("requested_region must be a two-letter ISO-style code")
    return normalized


def infer_substrate(composition: str, substrate_profile: str | None) -> str:
    if substrate_profile:
        prefix = substrate_profile.lower().split("-", 1)[0]
        if prefix in {"fv", "ps"}:
            return prefix
    lowered = composition.lower()
    if "psiphon" in lowered:
        return "ps"
    if "fv" in lowered:
        return "fv"
    return "direct"


def infer_cloudflare_proto(transport: str) -> str:
    return "masque" if transport.lower() == "masque" else "warp"
