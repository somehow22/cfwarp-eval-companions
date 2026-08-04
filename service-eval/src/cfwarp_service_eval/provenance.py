from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

from .config import (
    SCENARIO_DEFINITIONS,
    infer_cloudflare_proto,
    infer_substrate,
    normalize_region,
)


def evaluator_build() -> str:
    return os.environ.get("CFWARP_EVALUATOR_BUILD", "development")


def scenario_provenance(scenario_id: str) -> dict[str, str]:
    definition = SCENARIO_DEFINITIONS.get(scenario_id)
    if definition is None:
        definition = next(
            item
            for item in SCENARIO_DEFINITIONS.values()
            if item["scenario_id"] == scenario_id
        )
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    return {
        "catalog": "scenarios-v1",
        "scenario_id": str(definition["scenario_id"]),
        "definition_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }


def observation_v2(
    observation: Mapping[str, Any],
    lane: Mapping[str, Any],
    scenario_id: str,
    build: str | None = None,
) -> dict[str, Any]:
    """Upgrade an emitted v1 observation without changing its evidence facts."""
    upgraded = json.loads(json.dumps(observation))
    upgraded["schema_version"] = 2
    upgraded["scenario_provenance"] = scenario_provenance(scenario_id)
    node_id = str(lane["node_id"])
    requested_region_raw = lane.get("requested_region_raw") or lane.get(
        "requested_region"
    )
    requested_region = normalize_region(requested_region_raw)
    substrate = str(
        lane.get("substrate")
        or infer_substrate(
            str(lane["composition"]),
            lane.get("substrate_profile"),
        )
    )
    cloudflare_proto = str(
        lane.get("cloudflare_proto") or infer_cloudflare_proto(str(lane["transport"]))
    )
    ip_proto_stack = str(lane.get("ip_proto_stack") or "v4")
    config_generation = str(lane.get("config_generation") or lane["config_digest"])
    capability_id = str(
        lane.get("capability_id")
        or "-".join(
            (
                substrate,
                requested_region or "ZZ",
                cloudflare_proto,
                ip_proto_stack,
            )
        )
    )
    subject = upgraded.setdefault("subject", {})
    subject.update(
        {
            "deployment_origin": lane.get("deployment_origin") or f"legacy-{node_id}",
            "instance_id": lane["instance_id"],
            "node_id": node_id,
            "image_identity": lane["image_identity"],
            "config_generation": config_generation,
            "config_digest": lane["config_digest"],
            "evaluator_build": build or evaluator_build(),
        }
    )
    lane_payload = upgraded.setdefault("lane", {})
    lane_payload.update(
        {
            "lane_id": lane["id"],
            "capability_id": capability_id,
            "composition": lane["composition"],
            "transport": lane["transport"],
            "substrate": substrate,
            "substrate_profile": lane.get("substrate_profile"),
            "requested_region": requested_region,
            "requested_region_raw": requested_region_raw,
            "cloudflare_proto": cloudflare_proto,
            "ip_proto_stack": ip_proto_stack,
        }
    )
    return upgraded
