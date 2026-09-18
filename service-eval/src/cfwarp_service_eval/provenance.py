from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .config import (
    SCENARIO_DEFINITIONS,
    infer_cloudflare_proto,
    infer_substrate,
    normalize_region,
)
from .contracts import contracts_root


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


@lru_cache(maxsize=1)
def observation_v2_validator() -> Draft202012Validator:
    schema = json.loads(
        (contracts_root() / "observation-v2.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_observation_v2(
    observation: Mapping[str, Any],
    lane: Mapping[str, Any],
    scenario_id: str,
    evaluator: str | None = None,
) -> None:
    """Validate schema and immutable provenance against an active descriptor."""
    observation_v2_validator().validate(observation)
    expected_scenario = scenario_provenance(scenario_id)
    if observation.get("scenario_id") != expected_scenario["scenario_id"]:
        raise ValueError("scenario provenance does not match leased job")
    if observation.get("scenario_provenance") != expected_scenario:
        raise ValueError("scenario catalog provenance does not match leased job")

    subject = observation["subject"]
    expected_subject = {
        "deployment_origin": lane["deployment_origin"],
        "instance_id": lane["instance_id"],
        "node_id": lane["node_id"],
        "image_identity": lane["image_identity"],
        "config_generation": lane["config_generation"],
        "config_digest": lane["config_digest"],
    }
    if any(subject.get(key) != value for key, value in expected_subject.items()):
        raise ValueError("subject provenance does not match active deployment")
    if evaluator is not None and subject.get("evaluator_build") != evaluator:
        raise ValueError("evaluator build does not match the lease owner")

    lane_payload = observation["lane"]
    expected_lane = {
        "lane_id": lane["id"],
        "capability_id": lane["capability_id"],
        "composition": lane["composition"],
        "transport": lane["transport"],
        "substrate": lane["substrate"],
        "substrate_profile": lane.get("substrate_profile"),
        "requested_region": lane.get("requested_region"),
        "requested_region_raw": lane.get("requested_region_raw"),
        "cloudflare_proto": lane["cloudflare_proto"],
        "ip_proto_stack": lane["ip_proto_stack"],
    }
    if any(lane_payload.get(key) != value for key, value in expected_lane.items()):
        raise ValueError("lane provenance does not match active deployment")


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
