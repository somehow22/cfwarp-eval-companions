# Browser service scenarios

This Deno/TypeScript project is the browser-sensitive half of the dedicated `service-eval`
workspace. It owns deterministic anonymous-entry verdicts for ChatGPT, Gemini, Google Search, and
Reddit. It does not orchestrate regions or containers and does not try to bypass CAPTCHA, account,
or VPN policy.

## Runtime contract

Install Deno 2.9.2 and the currently tested `agent-browser` 0.31.2 with its managed Chromium. Pin
the browser CLI in reproducible sandboxes rather than silently accepting a newer command contract:

```bash
pnpm add --global agent-browser@0.31.2
agent-browser install
```

A clean CI or sandbox can run all deterministic checks without launching a browser:

```bash
cd service-eval/browser
deno task test
deno task check
```

Run one live scenario through an already-proven cfwarp listener:

```bash
deno task probe \
  --service chatgpt \
  --proxy socks5h://127.0.0.1:1080 \
  --instance-id direct-wg-01 \
  --image-identity ghcr.io/example/cfwarp@sha256:example \
  --config-digest sha256:example
```

Valid service names are `chatgpt`, `gemini`, `google-search`, `reddit`, and `turnstile-reference`.
Use `--output /path/to/artifact-root` when a scheduler supplies the artifact directory.

Each result contains the shared observation v1 envelope with a 24-hour freshness deadline. The probe
performs no tunnel rotation, service-verdict retry campaign, login, or challenge action. It does one
fresh-session replay when an `agent-browser` command fails so a dead daemon/socket does not discard
the run. If target navigation fails again after the WARP trace succeeded, the result is
`network_failure` at the `service-probe` layer; an inspectable Chrome navigation-error page is
recorded when available. Browser startup, CDP session, evaluation, and snapshot failures remain
`tooling_failure`; they are never reported as evidence that the target service is unavailable.

## Challenge references

The Turnstile scenario serves a fixed local HTML shell and loads Cloudflare's widget through the
selected listener using Cloudflare's documented forced-interactive test key. The local shell
bypasses the proxy; trace and all `challenges.cloudflare.com` requests do not. Success means the
test widget and its "Verify you are human" control rendered, not that a CAPTCHA was solved or that
an unrelated service is unlocked:

```bash
deno task probe \
  --service turnstile-reference \
  --proxy socks5h://127.0.0.1:1080
```

`challenge_reference_rendered` is a passing **Turnstile reference-scenario** result. It remains
distinct from the non-passing `challenge` result returned when an ordinary service scenario is
interrupted by a challenge.

## Verdict contract

Every run first opens Cloudflare trace in the same fresh browser session and requires
listener-facing `warp=on`. It then records a service-specific result:

- `available` or `available_login_required`: scenario passed;
- `challenge`, `blocked`, `unavailable`, `auth_required`, or `service_unavailable`: page was
  classified but the scenario failed;
- `unknown`: evidence was insufficient, so the scenario is not promoted;
- `challenge_reference_rendered`: the Turnstile reference rendered its expected challenge evidence;
  this is not an ordinary service-availability result;
- `tunnel_failure`, `tooling_failure`, or `probe_deadline_exceeded`: evaluation did not reach a
  trustworthy service verdict. Browser command failures are specifically `tooling_failure` at the
  `service-probe` layer after one fresh-session replay.

Exit code `0` means the named scenario's explicit purpose passed. Exit code `2` means a bounded
failing or inconclusive verdict completed. CI runs classification, type, lint, and format checks
only; live external services remain on-demand.

Artifacts are deliberately bounded:

- `summary.json` contains trace, result markers, browser/profile facts, timings, and body hash/size,
  but not raw body text, cookies, local storage, or HAR;
- `verdict.txt` is the short operator result;
- accessibility `snapshot` output and targeted DOM facts are the classification evidence;
  screenshots are not analyzed or required;
- `screenshot.jpeg` is created only with `--capture-screenshot true`, capped at 1.5 MB by default,
  and deleted when over budget.

Local managed Chromium is the default. When browser-provider location is the variable under test,
`--browser-provider agentcore` uses agent-browser's native AWS AgentCore provider with the same
structured snapshot workflow. This is an explicit escalation: the selected proxy must be reachable
from that cloud browser, and the local Turnstile fixture cannot run there. Ephemeral cloud sandboxes
should run this entire locked workspace and still emit the same observation envelope; cloud
placement is evidence metadata, not a better verdict by itself.

Each command has a timeout and the whole run has a deadline. Proxy credentials and URL query strings
are removed from structured evidence. The wrapper forces a tracked clean browser config and a small
environment allowlist so ambient profiles, auth state, proxy variables, extensions, and browser
flags cannot contaminate the anonymous session. Authenticated account testing is outside this
scenario pack.
