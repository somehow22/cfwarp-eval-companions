from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def contracts_root() -> Path:
    override = os.environ.get("CFWARP_EVAL_CONTRACTS_ROOT")
    if override:
        return Path(override)
    source_root = Path(__file__).resolve().parents[3] / "contracts"
    if source_root.is_dir():
        return source_root
    packaged = Path(__file__).resolve().parent / "contracts"
    if packaged.is_dir():
        return packaged
    raise RuntimeError("cannot locate cfwarp evaluation contracts")


@lru_cache(maxsize=1)
def scenario_definitions() -> dict[str, dict[str, Any]]:
    raw = json.loads(
        (contracts_root() / "scenarios-v1.json").read_text(encoding="utf-8")
    )
    if raw.get("schema_version") != 1 or not isinstance(raw.get("scenarios"), list):
        raise RuntimeError("unsupported scenario contract")
    definitions: dict[str, dict[str, Any]] = {}
    for item in raw["scenarios"]:
        scenario_id = item.get("id")
        if (
            not isinstance(scenario_id, str)
            or not scenario_id
            or scenario_id in definitions
        ):
            raise RuntimeError("scenario contract contains an invalid or duplicate id")
        definitions[scenario_id] = item
    return definitions


@lru_cache(maxsize=1)
def classification_definitions() -> dict[str, dict[str, Any]]:
    raw = json.loads(
        (contracts_root() / "classifications-v1.json").read_text(encoding="utf-8")
    )
    classes = raw.get("classes")
    if raw.get("schema_version") != 1 or not isinstance(classes, dict):
        raise RuntimeError("unsupported classification contract")
    return classes


def classify_result(verdict: str) -> tuple[str, bool]:
    definition = classification_definitions().get(verdict)
    if definition is None:
        return "unknown", False
    return str(definition["availability"]), bool(definition["eligible"])
