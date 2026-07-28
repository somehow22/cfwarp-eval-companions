# cfwarp-eval-companions

Supplementary evaluation containers used to observe `cfwarp` proxy lanes.

These probes answer one question: **is a given egress lane actually working
right now, and with what evidence?** They are deliberately separate from the
proxy runtime. A probe that shares a release cycle with the thing it measures
is not an independent check.

The versioned language-neutral mechanism is under [`contracts/`](contracts/).
That catalog, classification vocabulary, Observation schema, and its
conformance fixtures are authoritative across Python, Deno, and future
implementations.

## Why this repo is public

The probe image is pulled by nodes that intentionally hold no registry
credentials. Publishing the image removes the need to put a credential on a
production host to fetch a component that contains no secrets. Nothing here
embeds account material, provider profiles, or endpoint inventory; those stay
in the private runtime project.

## Containers

| Container | Purpose |
| --- | --- |
| `service-eval` | Bounded service, throughput, and challenge-reference probes behind one REST API |

More companions may land here. Each one owns its own directory and Dockerfile.

The same image also provides `cfwarp-brush`, a bounded test coordinator. It
uses the canonical scenario implementations while controlling a cfwarp
instance through its mounted local Unix socket. The evaluator API remains
probe-only and independently scheduled. Brush results include canonical
pre/candidate `perf` observations as evidence only; service acceptance is never
derived from performance.

## The observation envelope

Every probe writes its native evidence plus a common `observation` object in
`summary.json`. That envelope is the integration contract; no storage backend
is authoritative and the native summary remains the debugging evidence.

Key properties, because they are easy to get wrong:

- **Freshness is explicit.** Each observation carries `fresh_until`. After that
  instant a consumer must report `unknown` rather than reuse the last pass. A
  newer evaluator failure also stays visible as `unknown`; silently falling
  back to older evidence is forbidden.
- **Eligibility is separate from availability.** A challenge, block, or service
  failure is an eligible result: the scenario did not work through that lane.
  An evaluator or tooling failure is *ineligible* — it says nothing about the
  lane and must not be counted as a service failure.
- **Scenarios are never averaged together.** Availability is computed per
  scenario, per subject identity. Collapsing different scenarios into one
  service-wide number destroys the meaning of both.
- **Artifacts are bounded.** Each run keeps `summary.json`, `verdict.txt`, and
  at most one size-capped screenshot for browser scenarios. Exceeding the cap
  is an evaluator defect, not a tuning opportunity.
- **No secrets in the envelope.** Proxy credentials, cookies, account data,
  media URLs, query strings, raw bodies, and host paths are excluded by
  construction.

## Lane health tiers

The API derives a routing tier per lane from cheap liveness heartbeats plus
per-scenario freshness:

| Tier | Meaning |
| --- | --- |
| `preferred` | Route here first |
| `usable` | Works; below throughput floor or some scenarios failing |
| `degraded` | Repeated eligible failures across the window |
| `quarantined` | Sustained failure; expensive sweeps suspended |
| `unknown` | Evidence expired |

Tier drives probe cadence, so lane count can grow without sweep time growing
with it.

## Operating model

The API runs one persistent queue with independent loops:

- a **heartbeat** loop sampling lane liveness cheaply, so a lane going dark is
  visible immediately rather than after a long browser sweep completes;
- a **sweep** worker running the expensive scenarios;
- a **scheduler** enqueuing due work, so freshness is a property of the system
  rather than an operator chore.

The scheduler permits at most one pending task for a lane/scenario cell. A
worker or restart failure still emits `unknown`, but that row retains the same
instance, image, config, composition, transport, substrate, and canonical
scenario provenance as a successful observation.

Callers cannot register proxies or supply target URLs. Lanes are a fixed
server-side allowlist supplied by the operator, and configured proxy URLs never
appear in API responses.

## Configuration

| Variable | Meaning |
| --- | --- |
| `SERVICE_EVAL_LANES_FILE` | Read-only lane allowlist |
| `SERVICE_EVAL_TOKEN_FILE` | API bearer token, minimum 32 characters |
| `SERVICE_EVAL_METRICS_TOKEN_FILE` | Independent `/metrics` bearer token; falls back to the API token during migration |
| `SERVICE_EVAL_STATE_ROOT` | Bounded state and artifact root |
| `SERVICE_EVAL_BIND_HOST` | Defaults to `127.0.0.1` |
| `SERVICE_EVAL_SCENARIOS` | Comma-separated scenario allowlist |
| `SERVICE_EVAL_BROWSER_EXECUTION` | `disabled` (default), `local`, or `agentcore` |
| `SERVICE_EVAL_BROWSER_MIN_MEMORY_MIB` | Local-browser memory floor; defaults to `768` |
| `SERVICE_EVAL_HEARTBEAT_INTERVAL_SECONDS` | Liveness cadence |
| `SERVICE_EVAL_SWEEP_INTERVAL_SECONDS` | Scenario cadence, scaled by tier |

The API binds loopback by default. It typically runs with host networking so it
can reach lane listeners published on `127.0.0.1`, which would otherwise place
it on a public interface. Widening the bind is an explicit deployment decision.

`perf` and `youtube` are lightweight scenarios and may run in small containers
or similarly constrained function runtimes. Browser automation is optional.
Local Chromium must be explicitly enabled and meet the configured memory
prerequisite; `agentcore` delegates browser execution to an ephemeral cloud
runtime. `/v1/scenario-capabilities` reports what this evaluator can actually
run and why a scenario is disabled.

## Develop

```bash
cd service-eval
uv sync --locked --dev
uv run pytest
uv run ruff check src tests

cd browser
deno task test
deno task check
```

## Status

Pre-release. The API contract may change without notice until a tagged release.
