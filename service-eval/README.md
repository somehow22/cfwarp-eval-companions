# Service evaluation workspace

This is the single home for service-specific verdict probes. It is intentionally
separate from `scripts/`: probes here have locked dependencies, structured
output, classification tests, and bounded artifacts so the same command can run
locally, in CI, or in an ephemeral cloud sandbox.

The individual probes remain verdict-oriented. The optional API adds a narrow,
persistent orchestration boundary for a fixed lane allowlist; callers cannot
register proxies or supply target URLs.

## Ownership and secret boundary

`cfwarp-eval-companions` owns Observation v2, scenario/classification semantics,
the observer/worker API, and evaluator images. `cfwarp-pro` owns deployment
descriptors and supplies reviewed lane metadata to this runtime. Building and
offline-testing the image requires no secret.
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
- Every stored `summary.json` carries the same backend-independent,
  freshness-aware Observation v2 envelope described in
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

`cfwarp-service-eval-api` is the slim `cfwarp-observer`: it runs the persistent
SQLite queue, scheduler, retention, and API, but no probe subprocess in normal
deployment. It reads the bearer token from
`SERVICE_EVAL_TOKEN_FILE`, the read-only lane allowlist from
`SERVICE_EVAL_LANES_FILE`, and stores bounded state below
`SERVICE_EVAL_STATE_ROOT`. The API publishes only lane metadata; configured
proxy URLs never enter responses.

`/metrics` may use a separately rotatable bearer token through
`SERVICE_EVAL_METRICS_TOKEN_FILE`. If that variable is absent, it temporarily
falls back to the API token so deployments can migrate without an outage.

The authenticated `/v1` surface remains readable during migration. `/v2/jobs`
is the leased internal worker interface; `/v2/egresses` is exact-scenario,
exact-generation discovery; and `/v2/platform-slo` is the platform-health
surface. `/healthz` is unauthenticated and generic. `/docs`, `/redoc`, and
`/openapi.json` require the same bearer token. Queue depth is bounded.
Duplicate completion is idempotent, conflicting or expired leases fail closed,
and worker loss cannot take down the observer API.

Observer retention runs independently of embedded execution. It defaults to an
hourly pass, 14 days, and 512 MiB; configure these with
`SERVICE_EVAL_RETENTION_INTERVAL_SECONDS`, `SERVICE_EVAL_RETENTION_DAYS`, and
`SERVICE_EVAL_MAX_STATE_BYTES`. Scheduler, lease-expiry, and retention failures
make `/healthz` return 503 and appear in `/v2/platform-slo`.

Run workers from the same reviewed image with `cfwarp-eval-worker`. Set
`CFWARP_WORKER_CLASS` to `light`, `perf`, or `browser`. Light and perf workers
remain node-local; browser workers run centrally and reject listener addresses
outside the Tailnet range. A declared `.ts.net` browser listener is resolved
once and pinned to its Tailnet IP before browser execution; a result that
contains any non-Tailnet address is rejected.

The worker refuses to start unless `CFWARP_WORKER_DEADLINE_SECONDS` plus the
15-second subprocess shutdown grace and
`CFWARP_WORKER_RESULT_SUBMISSION_SECONDS` fit within
`CFWARP_WORKER_LEASE_SECONDS`. Defaults are 180 + 15 + 30 seconds within a
240-second lease.

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

## YouTube transfer scenario

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

## YouTube unlock scenario

`youtube-unlock` is distinct from the historical media-transfer scenario. Its
canonical identity is `youtube.anonymous_public_video_unlock`, version `1`,
with definition digest
`sha256:eee07d148db7c8f2ee0d291609b751b3fad1bc0ce7016bfc3f2e6bf59bc56860`.
It pins yt-dlp's current primary public, non-age-restricted test video
`YE7VzlLtp-4`, which upstream selected after retiring unavailable fixture
`BaW_jenozKc`. Probe version 2 uses two target-node-local methods. An independent
HTTP client reads the anonymous watch page, parses its initial player response,
and requires matching video metadata, `OK` playability, and at least one direct
HTTP(S) audio/video format. Separately, yt-dlp performs metadata extraction with
`download=False` and requires at least one direct HTTP(S) format reference with
an explicit audio or video codec. Missing codec metadata, malformed or
non-HTTP(S) URLs, and DRM-marked extractor references cannot pass. Stored
references from both methods exclude signed media URLs and headers. The probe
sets yt-dlp `check_formats=False`,
`skip_download=True`, and `download=False`, so it neither preflights nor
downloads media bytes. It never imports browser cookies or accepts a
caller-supplied video URL.

Verdict composition is deterministic. Independent watch/player success plus
yt-dlp success is `pass`. Independent success plus a yt-dlp tool, extractor, or
service-shaped failure is `pass_with_tooling_caveat`; both are available.
yt-dlp success without independent success is `probe_dependent`, which is
unknown and ineligible. An unavailable verdict requires both methods to report
the same recognized service denial (`bot_challenge`, `consent_challenge`,
`authentication_required`, `rate_limited`, or `service_unavailable`). Mismatched
denials, one-sided denials, malformed player data, and transport/tool-only
failures remain unknown and ineligible. Admission therefore stays fail-closed
without labeling inconclusive lanes incapable.

The sanitized `youtube-unlock-extractor-regression.json` fixture records a
non-canonical LAX diagnostic: yt-dlp 2026.06.09 found 29 public formats through
one production lane where 2026.07.04 reported a bot challenge, while the older
version still reported a challenge through a legacy lane in the same region and
colo. This demonstrates both extractor-version and per-egress effects. It is a
methodology regression fixture only; its different video and one-shot result
must never be submitted as canonical admission evidence.

The locked evaluator uses yt-dlp 2026.7.4, the installed yt-dlp-ejs 0.8.0
package, and Deno 2.9.2. Exact tool versions are checked before extraction;
remote yt-dlp components and all other JavaScript runtimes are disabled. The
Deno provider runs without remote imports, a lock file, node modules, prompts,
or code-cache writes; yt-dlp's own cache is disabled. Per-operation network
timeout defaults to 25 seconds. Both the evaluator's whole-process deadline and
scheduled process-group supervision are capped at the catalog's 120 seconds.
There are at most two attempts and the catalog artifact limit is 2 MiB. Only
network failures retry; bot challenge, authentication requirement, rate limit,
geographic/service denial, unexpected content, and tooling failure stop
immediately.

```bash
uv run cfwarp-service-eval youtube-unlock \
  --proxy socks5h://127.0.0.1:1080 \
  --instance-id direct-wg-01 \
  --image-identity ghcr.io/example/cfwarp@sha256:example \
  --config-digest sha256:example
```

Observation evidence identifies probe `youtube-unlock-multisignal` version `2`,
both methods, and the Python, HTTP client, yt-dlp, EJS, and Deno identities. The
scenario catalog entry is unchanged because it already defines the service
capability, node-local execution, and bounds rather than a particular extractor
implementation; its version and definition digest therefore remain compatible.
Previously stored `youtube-unlock-yt-dlp` version `1` observations retain their
original meaning and are not rewritten. Consumers that want the corrected
methodology should require evaluator builds that emit probe version 2 and allow
old records to expire before making publication or remediation claims.

An immutable publication must pin the evaluator image by digest, retain the
locked Python dependencies, installed solver package, and exact Deno runtime, set
`CFWARP_EVALUATOR_BUILD`, and publish the exact Observation v2 scenario
provenance generated from the catalog definition. A local success is
availability evidence only; it is not permission to publish an image or claim
other videos, playback/streaming, authenticated use, lanes, or regions.

The node-local production path requires both `cfwarp-observer` and
`cfwarp-eval-worker` on the same architecture. The observer owns scheduling,
the authenticated lease API, and SQLite; the light worker owns lane heartbeats,
the pinned yt-dlp/EJS/Deno probe, Observation v1-to-v2 upgrade, and submission
back to the observer. Run both images rootless with a read-only root filesystem
and writable tmpfs storage; only the observer state directory is persistent.
The worker must reach the observer over loopback or a node-private endpoint and
must reach the lane's node-local proxy listener. Cross-host proxy access and
amd64 emulation are not production configurations.

The isolated ARM64 candidate workflow publishes commit-qualified tags pointing
to digest-addressed indexes for only those two components. Each index contains
exactly one `linux/amd64` and one `linux/arm64` runnable child; provenance
attestations are separate registry referrers. It does not update release or
`latest` aliases. Its smoke checks are offline artifact certification only:
they never execute a service scenario or produce Observation/admission
evidence. Canonical service evaluation runs node-locally on the target host
through that host's lane listener.

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
  --state-db /var/lib/cfwarp-brush/remediation.sqlite3 \
  --attempts 3 \
  --strategy auto \
  --deadline-seconds 900 \
  --cooldown-seconds 1800
```

The baseline is evaluated before mutation. An already-available scenario
returns without rotating. Unknown or expired evidence fails closed. Changed-IP
candidates are evaluated through the same canonical runner; eligible passes
commit, eligible failures roll back, and an evaluator failure gets one retry on
the same candidate before rollback.

The durable journal stores the baseline and every candidate, evaluator retry,
performance observation, rollback, and commit. Auto mode permits one reconnect
and at most two identity candidates, has a 15-minute deadline, and begins a
30-minute per-lane cooldown at the first deployment mutation. Tooling failure
never authorizes a mutation.

Central maintenance that explicitly requests a new WARP address adds
`--force-change`. This preserves the same baseline, transactional candidate,
canonical scenario evaluation, and rollback behavior, but it does not
short-circuit when the baseline is already available. Unknown baseline evidence
still fails closed.

Every brush run records `performance_before`; every changed-IP candidate also
records `performance_after`. Both use the canonical `perf` scenario, and
neither can approve, reject, commit, or roll back a candidate. `perf` cannot be
selected as the gate scenario. Browser scenarios require an explicitly enabled
local or cloud browser runtime before the command starts.
