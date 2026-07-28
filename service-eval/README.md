# Service evaluation workspace

This is the single home for service-specific verdict probes. It is intentionally
separate from `scripts/`: probes here have locked dependencies, structured
output, classification tests, and bounded artifacts so the same command can run
locally, in CI, or in an ephemeral cloud sandbox.

The individual probes remain verdict-oriented. The optional API adds a narrow,
persistent orchestration boundary for a fixed lane allowlist; callers cannot
register proxies or supply target URLs.

## Ownership and secret boundary

`cfwarp-pro` owns this probe image, its scenarios, API contract, queue, and
result semantics. Building and offline-testing the image requires no secret.
Future deployment projects consume a reviewed image digest and project only
the runtime inputs needed by one installation; they do not become the owner of
the probe implementation.

Reusable provider credentials such as a FastestVPN WireGuard base profile are
network-substrate credentials, not SecretOps-project credentials. A probe API
bearer token is installation-specific runtime state and belongs in the owning
project/runtime secret bundle. SecretOps may deliver either value to a host,
but its normal local/operator bundle must not become the durable owner merely
because SecretOps performs the deployment.

## Why this shape

- Python probes use a standalone uv project and committed `uv.lock`.
- yt-dlp is embedded through its Python API; normal CLI output is not parsed.
- Python invocations write only `summary.json` and `verdict.txt` by default;
  browser scenarios may add one size-capped screenshot.
- Every `summary.json` carries the same backend-independent, freshness-aware
  `observation` v1 envelope described in
  [`../docs/observation-slo-contract.md`](../docs/observation-slo-contract.md).
- Raw page bodies, cookies, media URLs, and full yt-dlp metadata are not stored.
- `browser/` is the Deno project for ChatGPT, Gemini, Google Search, and Reddit;
  its live scenarios use `agent-browser` Chromium while CI remains offline and
  deterministic.

This follows the official uv CI pattern (`uv sync --locked`, then `uv run`) and
yt-dlp's embedding guidance (`YoutubeDL.extract_info` plus selected structured
fields rather than parsing human-readable stdout).

## Set up and test

```bash
cd service-eval
uv sync --locked
uv run pytest
```

Browser scenario classification is checked separately:

```bash
cd browser
deno task test
deno task check
```

## Unified REST API

`cfwarp-service-eval-api` runs one persistent SQLite queue and one probe
subprocess at a time. It reads the bearer token from
`SERVICE_EVAL_TOKEN_FILE`, the read-only lane allowlist from
`SERVICE_EVAL_LANES_FILE`, and stores bounded state below
`SERVICE_EVAL_STATE_ROOT`. The API publishes only lane metadata; configured
proxy URLs never enter responses.

`/metrics` may use a separately rotatable bearer token through
`SERVICE_EVAL_METRICS_TOKEN_FILE`. If that variable is absent, it temporarily
falls back to the API token so deployments can migrate without an outage.

The authenticated `/v1` surface lists the fixed lanes/scenarios, accepts a run
group, reports group state, and returns Observation v1 records. `/healthz` is
unauthenticated and generic. `/docs`, `/redoc`, and `/openapi.json` require the
same bearer token. Capacity is one active plus one waiting group; further
submissions return `409`.

CI or a clean sandbox should use the same locked install:

```bash
uv sync --locked --dev
uv run pytest
```

## Probe execution profiles

Lightweight service checks and browser automation are separate capabilities:

- `perf` and `youtube` run locally without a browser and are suitable for small
  containers or function-style runtimes.
- browser scenarios are optional and disabled by default.
- `SERVICE_EVAL_BROWSER_EXECUTION=local` enables bundled Chromium only when the
  detected runtime ceiling meets `SERVICE_EVAL_BROWSER_MIN_MEMORY_MIB` (768 MiB
  by default).
- `SERVICE_EVAL_BROWSER_EXECUTION=agentcore` delegates browser work to the
  existing ephemeral cloud provider integration and does not impose the local
  Chromium memory floor.

Use `SERVICE_EVAL_SCENARIOS` to select the node's scenario set.
`/v1/scenario-capabilities` returns all known scenarios with their execution
class, target, minimum memory, enabled state, and disable reason. This makes a
small evaluator a valid lightweight probe node rather than a failed browser
node.

## YouTube gold scenario

The probe first verifies that the selected listener reports `warp=on`, then
deterministically selects the first current upload from the configured channel
(or uses an explicit video URL), extracts metadata/formats, and reads a bounded
amount from one direct media format. Retries, socket timeout, artifact size, and
the transfer amount are bounded. On Linux, a whole-probe deadline (120 seconds
by default) also prevents a peer that trickles data from extending the run
indefinitely.

```bash
uv run cfwarp-service-eval youtube \
  --proxy socks5h://127.0.0.1:1080 \
  --instance-id fv-wg-ca-01 \
  --image-identity ghcr.io/example/cfwarp@sha256:example \
  --config-digest sha256:example
```

For repeated runs, pin the discovered candidate shown in `summary.json`:

```bash
uv run cfwarp-service-eval youtube \
  --proxy socks5h://127.0.0.1:1080 \
  --video-url 'https://www.youtube.com/watch?v=<id>'
```

Use `--output /path/to/artifact-root` to select a stable artifact directory.
The process exits `0` only for a passing service verdict and `2` for a completed
failing verdict. `--help` documents the bounded tuning options.

`pass_with_tooling_caveat` means the actual extraction and partial transfer
passed, but a supported JavaScript runtime was absent or yt-dlp reported a
JavaScript tooling failure. Keep such a result at experimental confidence until
the tooling caveat is removed. ffmpeg identity is recorded for reproducibility,
but ffmpeg is not required by this metadata-plus-bounded-range scenario.

## Priority browser scenarios

See [`browser/README.md`](browser/README.md) for the bounded ChatGPT, Gemini,
Google Search, and Reddit commands and verdict classes. These probes require an
already-running listener; deployment and region selection remain outside this
workspace.

## Bounded service brushing

`cfwarp-brush` is a separate test coordinator in this image. It does not add
probe logic to cfwarp and does not turn the persistent evaluator API into a
remediation controller.

```bash
cfwarp-brush run \
  --lanes-file /etc/cfwarp-service-eval/lanes.json \
  --lane fv-ro \
  --scenario youtube \
  --socket /run/cfwarp/core.sock \
  --output /var/lib/cfwarp-brush/run \
  --attempts 3 \
  --strategy auto
```

The baseline is evaluated before mutation. An already-available scenario
returns without rotating. Unknown or expired evidence fails closed. Changed-IP
candidates are evaluated through the same canonical runner; eligible passes
commit, eligible failures roll back, and an evaluator failure gets one retry on
the same candidate before rollback.

Every brush run records `performance_before`; every changed-IP candidate also
records `performance_after`. Both use the canonical `perf` scenario, and
neither can approve, reject, commit, or roll back a candidate. `perf` cannot be
selected as the gate scenario. Browser scenarios require an explicitly enabled
local or cloud browser runtime before the command starts.
