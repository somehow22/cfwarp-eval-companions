import { browserScenarioIds, resultForClass } from "../src/contracts.ts"
import { scenarios } from "../src/scenarios.ts"

Deno.test("browser implementations match the canonical scenario catalog", () => {
  assertEqual(Object.keys(scenarios).sort(), browserScenarioIds())
})

Deno.test("unknown classifications fail closed", () => {
  assertEqual(resultForClass("not-in-contract"), {
    availability: "unknown",
    eligible: false,
  })
})

function assertEqual(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  }
}
