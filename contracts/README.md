# Evaluation contracts

This directory is the language-neutral source of truth for maintained cfwarp
service and performance evaluation.

- `scenarios-v1.json` defines stable scenario identities, execution classes,
  runtime prerequisites, bounds, and whether a scenario may gate remediation.
- `classifications-v1.json` defines availability and eligibility semantics.
- `observation-v1.schema.json` defines the cross-runtime evidence envelope.
- `observation-v2.schema.json` makes deployment origin, canonical capability
  identity, active config generation, evaluator build, and exact scenario
  provenance mandatory. V1 remains readable during migration.
- `egress-verdict-report-v1.schema.json` defines the bounded aggregate consumed
  by generated docs, Linear reporting, Nexus, and integration skills.
- `fixtures/` contains conformance vectors used by every implementation.

Implementations may use Python, TypeScript, Go, or another runtime. They must
emit the same scenario/version and satisfy these contracts. Target-specific
mechanics remain implementation code; result meaning does not.
