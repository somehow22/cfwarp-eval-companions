import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from cfwarp_service_eval.contracts import (
    classification_definitions,
    contracts_root,
    scenario_definitions,
)


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
    assert len(definitions) == 7
    assert len({item["scenario_id"] for item in definitions.values()}) == 7
    assert definitions["perf"]["remediation_role"] == "observe_only"
    for definition in definitions.values():
        assert definition["runtime_prerequisites"]["network"] is True
        assert definition["runtime_prerequisites"]["commands"]
        assert 1 <= definition["deadline_seconds"] <= 600
        assert 1 <= definition["artifact_limit_bytes"] <= 4 * 1024 * 1024
