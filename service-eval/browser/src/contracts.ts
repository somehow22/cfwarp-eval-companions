import classifications from "../../../contracts/classifications-v1.json" with { type: "json" }
import scenarioCatalog from "../../../contracts/scenarios-v1.json" with { type: "json" }

export interface ContractResult {
  availability: "available" | "unavailable" | "unknown"
  eligible: boolean
}

export function resultForClass(verdict: string): ContractResult {
  const classes = classifications.classes as Record<string, ContractResult>
  return classes[verdict] ?? { availability: "unknown", eligible: false }
}

export function browserScenarioIds(): string[] {
  return scenarioCatalog.scenarios
    .filter((scenario) => scenario.execution_class === "browser")
    .map((scenario) => scenario.id)
    .sort()
}
