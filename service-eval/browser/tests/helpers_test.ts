import {
  BrowserCommandError,
  commandEnvironment,
  isChromeNavigationFailureUrl,
  normalizeProxy,
  parseTrace,
  proxyArguments,
  safeError,
  sanitizedEnvironment,
  sessionPolicyArguments,
  stripUrlSecrets,
} from "../src/agent_browser.ts"
import { buildObservation, type Summary } from "../src/main.ts"

Deno.test("normalizes Chromium SOCKS proxy scheme", () => {
  if (normalizeProxy("socks5h://127.0.0.1:1080") !== "socks5://127.0.0.1:1080") {
    throw new Error("socks5h proxy was not normalized")
  }
})

Deno.test("repeats proxy arguments for every browser session command", () => {
  const args = proxyArguments("socks5://proxy.internal:1080", "127.0.0.1")
  const expected = ["--proxy", "socks5://proxy.internal:1080", "--proxy-bypass", "127.0.0.1"]
  if (JSON.stringify(args) !== JSON.stringify(expected) || proxyArguments(null, "").length !== 0) {
    throw new Error(`unexpected proxy arguments: ${JSON.stringify(args)}`)
  }
})

Deno.test("repeats the complete browser session policy for every command", () => {
  const args = sessionPolicyArguments(
    "one.one.one.one,challenges.cloudflare.com",
    "socks5://proxy.internal:1080",
    "127.0.0.1",
  )
  const expected = [
    "--allowed-domains",
    "one.one.one.one,challenges.cloudflare.com",
    "--proxy",
    "socks5://proxy.internal:1080",
    "--proxy-bypass",
    "127.0.0.1",
  ]
  if (JSON.stringify(args) !== JSON.stringify(expected)) {
    throw new Error(`unexpected session policy arguments: ${JSON.stringify(args)}`)
  }
})

Deno.test("parses bounded Cloudflare trace fields", () => {
  const trace = parseTrace("ip=104.28.1.2\nloc=US\ncolo=EWR\nwarp=on\n")
  if (trace.warp !== "on" || trace.loc !== "US" || trace.colo !== "EWR") {
    throw new Error("trace fields were not parsed")
  }
})

Deno.test("strips URL credentials, query, and fragment", () => {
  const safe = stripUrlSecrets("https://alice:secret@example.com/path?token=secret#fragment")
  if (safe !== "https://example.com/path") throw new Error(`unsafe URL: ${safe}`)
})

Deno.test("error text strips URL credentials and query tokens", () => {
  const safe = safeError(
    new Error("proxy failed: https://alice:p%40ss@example.com/path?token=secret#fragment"),
  )
  if (safe !== "proxy failed: https://example.com/path") throw new Error(`unsafe error: ${safe}`)
})

Deno.test("child environment excludes browser, proxy, and unrelated secrets", () => {
  const source = {
    PATH: "/usr/bin",
    HOME: "/tmp/home",
    AGENT_BROWSER_EXECUTABLE_PATH: "/usr/local/bin/agent-browser-chrome",
    HTTP_PROXY: "http://ambient.example",
    NO_PROXY: "www.cloudflare.com",
    AGENT_BROWSER_PROFILE: "/tmp/authenticated-profile",
    API_TOKEN: "secret",
  }
  const environment = sanitizedEnvironment(source)
  const expected = JSON.stringify({
    PATH: "/usr/bin",
    HOME: "/tmp/home",
    AGENT_BROWSER_EXECUTABLE_PATH: "/usr/local/bin/agent-browser-chrome",
  })
  if (JSON.stringify(environment) !== expected) {
    throw new Error(`unexpected child environment: ${JSON.stringify(environment)}`)
  }

  const command = commandEnvironment(source, 45_000)
  if (
    command.AGENT_BROWSER_DEFAULT_TIMEOUT !== "12000" ||
    command.AGENT_BROWSER_IDLE_TIMEOUT_MS !== "90000"
  ) {
    throw new Error(`missing trusted browser timeouts: ${JSON.stringify(command)}`)
  }
})

Deno.test("builds the shared freshness-aware observation contract", () => {
  const summary: Summary = {
    schema_version: 1,
    service: "gemini",
    scenario: "anonymous_entry",
    started_at: "2026-07-15T00:00:00.000Z",
    finished_at: "2026-07-15T00:00:01.000Z",
    elapsed_ms: 1000,
    input: {
      instance_id: "test-instance",
      image_identity: "image@sha256:test",
      config_digest: "sha256:test",
    },
    tools: {},
    trace: { ok: true, warp: "on", loc: "US", colo: "LAX", ip: null, httpStatus: 200 },
    browser: null,
    verdict: "available_login_required",
    failure_layer: "none",
  }
  const observation = buildObservation(summary)
  if (observation.scenario_id !== "gemini.anonymous_entry") throw new Error("bad scenario ID")
  if (observation.result.availability !== "available" || !observation.result.eligible) {
    throw new Error(`bad result: ${JSON.stringify(observation.result)}`)
  }
  if (observation.fresh_until !== "2026-07-16T00:00:01.000Z") {
    throw new Error(`bad freshness deadline: ${observation.fresh_until}`)
  }
  if (observation.egress.region !== "US" || observation.subject.instance_id !== "test-instance") {
    throw new Error("identity or egress was not preserved")
  }
})

Deno.test("browser command failures are explicit service-probe tooling failures", () => {
  const summary: Summary = {
    schema_version: 1,
    service: "gemini",
    scenario: "anonymous_entry",
    started_at: "2026-07-15T00:00:00.000Z",
    finished_at: "2026-07-15T00:00:01.000Z",
    elapsed_ms: 1000,
    input: {},
    tools: { browser_attempts: 2, browser_session_recovered: false },
    trace: { ok: true, warp: "on", loc: "DE", colo: "FRA", ip: null, httpStatus: 200 },
    browser: { error: new BrowserCommandError("daemon socket disappeared").message },
    verdict: "tooling_failure",
    failure_layer: "service-probe",
  }
  const observation = buildObservation(summary)
  if (
    observation.result.availability !== "unknown" ||
    observation.result.class !== "tooling_failure" ||
    observation.failure_layer !== "service-probe"
  ) {
    throw new Error(`ambiguous tooling observation: ${JSON.stringify(observation)}`)
  }
})

Deno.test("inspectable Chrome navigation failures are unavailable, not unknown", () => {
  if (!isChromeNavigationFailureUrl("chrome-error://chromewebdata/")) {
    throw new Error("Chrome navigation error URL was not recognized")
  }
  if (isChromeNavigationFailureUrl("https://gemini.google.com/app")) {
    throw new Error("normal target URL was treated as a navigation failure")
  }
  const summary: Summary = {
    schema_version: 1,
    service: "gemini",
    scenario: "anonymous_entry",
    started_at: "2026-07-15T00:00:00.000Z",
    finished_at: "2026-07-15T00:00:01.000Z",
    elapsed_ms: 1000,
    input: {},
    tools: { browser_attempts: 2, browser_session_recovered: false },
    trace: { ok: true, warp: "on", loc: "CA", colo: "YUL", ip: null, httpStatus: 200 },
    browser: { final_url: "chrome-error://chromewebdata/" },
    verdict: "network_failure",
    failure_layer: "service-probe",
  }
  const observation = buildObservation(summary)
  if (
    observation.result.availability !== "unavailable" ||
    observation.result.class !== "network_failure" ||
    !observation.result.eligible
  ) {
    throw new Error(`navigation failure stayed ambiguous: ${JSON.stringify(observation)}`)
  }
})
