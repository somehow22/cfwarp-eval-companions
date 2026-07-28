# Evaluation contracts

This directory is the language-neutral source of truth for maintained cfwarp
service and performance evaluation.

- `scenarios-v1.json` defines stable scenario identities, execution classes,
  runtime prerequisites, bounds, and whether a scenario may gate remediation.
- `classifications-v1.json` defines availability and eligibility semantics.
- `observation-v1.schema.json` defines the cross-runtime evidence envelope.
- `fixtures/` contains conformance vectors used by every implementation.

Implementations may use Python, TypeScript, Go, or another runtime. They must
emit the same scenario/version and satisfy these contracts. Target-specific
mechanics remain implementation code; result meaning does not.
