from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import BROWSER_SCENARIOS, SCENARIO_DEFINITIONS, SCENARIOS


def parse_scenarios(raw: str | None) -> dict[str, str]:
    """Resolve the node's enabled scenario set, defaulting to all of them."""
    if not raw or not raw.strip():
        return dict(SCENARIOS)
    selected = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = sorted(set(selected) - set(SCENARIOS))
    if unknown:
        raise ValueError(f"unknown scenario IDs in SERVICE_EVAL_SCENARIOS: {unknown}")
    return {key: SCENARIOS[key] for key in selected}


def parse_browser_execution(raw: str | None) -> str:
    value = (raw or "disabled").strip().lower()
    if value not in {"disabled", "local", "agentcore"}:
        raise ValueError(
            "SERVICE_EVAL_BROWSER_EXECUTION must be disabled, local, or agentcore"
        )
    return value


def memory_limit_mib() -> int:
    """Return the effective cgroup memory ceiling, falling back to host RAM."""
    cgroup_limit = Path("/sys/fs/cgroup/memory.max")
    if cgroup_limit.is_file():
        raw = cgroup_limit.read_text(encoding="utf-8").strip()
        if raw != "max":
            return max(1, int(raw) // (1024 * 1024))
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return max(1, int(line.split()[1]) // 1024)
    page_count = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    if page_count > 0 and page_size > 0:
        return max(1, page_count * page_size // (1024 * 1024))
    raise ValueError("cannot determine runtime memory ceiling")


def resolve_scenario_capabilities(
    raw: str | None,
    browser_execution: str,
    available_memory_mib: int,
    browser_min_memory_mib: int,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    requested = parse_scenarios(raw)
    enabled: dict[str, str] = {}
    capabilities: list[dict[str, Any]] = []
    for scenario_id, observation_id in SCENARIOS.items():
        definition = SCENARIO_DEFINITIONS[scenario_id]
        execution_class = definition["execution_class"]
        selected = scenario_id in requested
        reason = "not selected by SERVICE_EVAL_SCENARIOS"
        execution_target = "local"
        scenario_enabled = selected
        minimum_memory_mib = definition["minimum_memory_mib"]

        if scenario_id not in BROWSER_SCENARIOS:
            reason = "enabled" if selected else reason
        elif not selected:
            execution_target = "none"
        elif browser_execution == "disabled":
            scenario_enabled = False
            execution_target = "none"
            reason = "browser automation is optional and disabled"
        elif browser_execution == "local":
            minimum_memory_mib = max(
                int(minimum_memory_mib or 0), browser_min_memory_mib
            )
            if available_memory_mib < minimum_memory_mib:
                scenario_enabled = False
                execution_target = "none"
                reason = (
                    f"local browser requires {minimum_memory_mib} MiB; "
                    f"runtime ceiling is {available_memory_mib} MiB"
                )
            else:
                reason = "enabled with local Chromium"
        else:
            execution_target = "agentcore"
            minimum_memory_mib = None
            reason = "enabled with cloud browser execution"

        if scenario_enabled:
            enabled[scenario_id] = observation_id
        capabilities.append(
            {
                "id": scenario_id,
                "observation_scenario_id": observation_id,
                "execution_class": execution_class,
                "enabled": scenario_enabled,
                "execution_target": execution_target,
                "minimum_memory_mib": minimum_memory_mib,
                "reason": reason,
            }
        )
    return enabled, capabilities


def require_scenario_capability(
    scenario_id: str,
    browser_execution: str,
    available_memory_mib: int,
    browser_min_memory_mib: int = 768,
) -> dict[str, Any]:
    _, capabilities = resolve_scenario_capabilities(
        scenario_id,
        browser_execution,
        available_memory_mib,
        browser_min_memory_mib,
    )
    capability = next(item for item in capabilities if item["id"] == scenario_id)
    if not capability["enabled"]:
        raise ValueError(str(capability["reason"]))
    return capability
