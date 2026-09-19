import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from cfwarp_service_eval.contracts import (
    classification_definitions,
    contracts_root,
    scenario_definitions,
)
from cfwarp_service_eval.provenance import observation_v2, scenario_provenance


def test_contract_fixtures_and_classifications_are_conformant():
    root = contracts_root()
    schema = json.loads((root / "observation-v1.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    valid = json.loads((root / "fixtures/observation-valid.json").read_text())
    validator.validate(valid)
    invalid = json.loads(
        (root / "fixtures/observation-invalid-unknown-eligible.json").read_text()
    )
    with pytest.raises(Exception):
        validator.validate(invalid)

    classes = classification_definitions()
    for name, definition in classes.items():
        candidate = json.loads(json.dumps(valid))
        candidate["result"] = {"class": name, **definition}
        validator.validate(candidate)


def test_scenario_catalog_is_unique_and_bounded():
    definitions = scenario_definitions()
    assert len(definitions) == 8
    assert len({item["scenario_id"] for item in definitions.values()}) == 8
    assert (
        definitions["youtube"]["scenario_id"]
        != definitions["youtube-unlock"]["scenario_id"]
    )
    assert definitions["perf"]["remediation_role"] == "observe_only"
    assert scenario_provenance("youtube-unlock")["definition_digest"] == (
        "sha256:eee07d148db7c8f2ee0d291609b751b3fad1bc0ce7016bfc3f2e6bf59bc56860"
    )
    assert scenario_provenance("gemini")["definition_digest"] == (
        "sha256:a836627f65e5b7e105a303c29a6ded52c31728b2fdbdf645c2e49bb72cde8174"
    )
    assert scenario_provenance("reddit")["definition_digest"] == (
        "sha256:268ef4adfc187b9f671a0be9f83c4322666c63a4186a682c211f31ff22468d53"
    )
    for definition in definitions.values():
        assert definition["runtime_prerequisites"]["network"] is True
        assert definition["runtime_prerequisites"]["commands"]
        assert 1 <= definition["deadline_seconds"] <= 600
        assert 1 <= definition["artifact_limit_bytes"] <= 4 * 1024 * 1024


def test_observation_v2_requires_exact_provenance():
    root = contracts_root()
    schema = json.loads((root / "observation-v2.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    valid = json.loads((root / "fixtures/observation-valid.json").read_text())
    lane = {
        "id": "direct-de",
        "instance_id": "example-lane",
        "node_id": "proxy-host-1",
        "deployment_origin": "cfwarp-pro",
        "image_identity": "example@sha256:" + "a" * 64,
        "config_generation": "generation-1",
        "config_digest": "sha256:" + "b" * 64,
        "capability_id": "direct-DE-warp-v4",
        "composition": "direct-warp",
        "transport": "wireguard",
        "substrate": "direct",
        "substrate_profile": None,
        "requested_region": "DE",
        "requested_region_raw": "DE",
        "cloudflare_proto": "warp",
        "ip_proto_stack": "v4",
    }
    upgraded = observation_v2(valid, lane, "youtube", build="test-build")
    validator.validate(upgraded)
    del upgraded["subject"]["config_generation"]
    with pytest.raises(Exception):
        validator.validate(upgraded)


def test_egress_verdict_report_v1_carries_builds_generations_and_remediation():
    root = contracts_root()
    schema = json.loads((root / "egress-verdict-report-v1.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(
        {
            "schema_version": 1,
            "generated_at": "2026-08-04T01:00:00Z",
            "sources": [
                {
                    "node_id": "proxy-host-1",
                    "deployment_origin": "sibling-project",
                    "observer_build": "observer-build",
                    "observation_schema": 2,
                    "evaluator_builds": ["worker-build"],
                    "config_generations": ["generation-1"],
                }
            ],
            "platform_slo": {"nodes": []},
            "scenarios": {
                "youtube.anonymous_public_video": {
                    "available": 1,
                    "unavailable": 0,
                    "unknown": 0,
                }
            },
            "egresses": [],
            "remediation": [],
        }
    )
