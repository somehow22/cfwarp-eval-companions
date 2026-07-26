import {
  AgentBrowser,
  BrowserCommandError,
  BrowserNavigationError,
  CLEANUP_BUDGET_MS,
  normalizeProxy,
  ProbeDeadlineError,
  safeError,
  stripUrlSecrets,
  type TraceEvidence,
} from "./agent_browser.ts"
import { classify } from "./classify.ts"
import { isServiceName, type Scenario, scenarios, type ServiceName } from "./scenarios.ts"

interface Options {
  service: ServiceName
  proxy: string | null
  output: string
  timeoutSeconds: number
  deadlineSeconds: number
  maxScreenshotBytes: number
  instanceId: string | null
  imageIdentity: string | null
  configDigest: string | null
  nodeId: string | null
  runtime: string | null
  composition: string | null
  transport: string | null
  substrateProfile: string | null
  requestedRegion: string | null
  browserProvider: "local" | "agentcore"
  captureScreenshot: boolean
}

export interface Summary {
  schema_version: 1
  service: ServiceName
  scenario: string
  started_at: string
  finished_at?: string
  elapsed_ms?: number
  input: Record<string, unknown>
  tools: Record<string, unknown>
  trace: TraceEvidence | null
  browser: Record<string, unknown> | null
  verdict: string
  failure_layer: string
  observation?: Observation
}

interface Observation {
  schema_version: 1
  observation_id: string
  observed_at: string
  fresh_until: string
  scenario_id: string
  probe: { name: string; version: string; execution: string }
  subject: Record<string, string | null>
  lane: Record<string, string | null>
  egress: Record<string, string | null>
  result: {
    availability: "available" | "unavailable" | "unknown"
    class: string
    eligible: boolean
  }
  confidence_stage: "single_observation"
  failure_layer: string
  latency_ms: number
  artifacts: Array<{ kind: string; path: string }>
}

const MAX_BROWSER_ATTEMPTS = 2

export async function main(args: string[]): Promise<number> {
  const options = parseArgs(args)
  await Deno.mkdir(options.output, { recursive: true })
  const prepared = await prepareScenario(options)
  const scenario = prepared.scenario
  const started = new Date()
  const deadlineAt = Date.now() + options.deadlineSeconds * 1000 - CLEANUP_BUDGET_MS
  const summary: Summary = {
    schema_version: 1,
    service: scenario.service,
    scenario: scenario.scenario,
    started_at: started.toISOString(),
    input: {
      requested_proxy: redactProxy(options.proxy),
      browser_proxy: redactProxy(normalizeProxy(options.proxy)),
      target_url: stripUrlSecrets(scenario.url),
      timeout_seconds: options.timeoutSeconds,
      deadline_seconds: options.deadlineSeconds,
      max_screenshot_bytes: options.maxScreenshotBytes,
      endpoint_class: options.service === "turnstile-reference"
        ? "official-test-fixture"
        : "documented-public",
      account_policy: "fresh-anonymous-session-no-credentials",
      remediation_policy: "one_fresh_session_replay_on_browser_command_failure",
      freshness_hint_seconds: 86_400,
      instance_id: options.instanceId,
      image_identity: options.imageIdentity,
      config_digest: options.configDigest,
      node_id: options.nodeId,
      runtime: options.runtime,
      composition: options.composition,
      transport: options.transport,
      substrate_profile: options.substrateProfile,
      requested_region: options.requestedRegion,
      browser_provider: options.browserProvider,
      screenshot_policy: options.captureScreenshot ? "requested" : "not_requested",
    },
    tools: { deno: Deno.version.deno },
    trace: null,
    browser: null,
    verdict: "unknown",
    failure_layer: "unknown",
  }

  let browser: AgentBrowser | null = null
  let browserAttempts = 0
  const browserErrors: string[] = []
  try {
    for (let attempt = 1; attempt <= MAX_BROWSER_ATTEMPTS; attempt++) {
      browserAttempts = attempt
      try {
        browser = await AgentBrowser.create(
          scenario,
          options.proxy,
          options.timeoutSeconds * 1000,
          deadlineAt,
          options.browserProvider,
        )
        summary.tools.agent_browser = browser.version
        const trace = await browser.checkTrace()
        summary.trace = trace
        if (!trace.ok) {
          summary.tools.browser_attempts = browserAttempts
          summary.verdict = "tunnel_failure"
          summary.failure_layer = trace.warp === null ? "unknown" : "route-runtime"
          return await finish(options.output, summary, started, 2)
        }

        const evidence = await browser.observe(
          scenario,
          options.output,
          options.maxScreenshotBytes,
          options.captureScreenshot,
        )
        const classification = classify(scenario, evidence.observation)
        const bodyBytes = new TextEncoder().encode(evidence.observation.bodyText)
        summary.tools.browser_attempts = browserAttempts
        summary.tools.browser_session_recovered = browserErrors.length > 0
        summary.browser = {
          final_url: evidence.observation.finalUrl,
          http_status: evidence.observation.httpStatus,
          title: evidence.observation.title,
          body_text_bytes: bodyBytes.byteLength,
          body_text_sha256: await sha256(bodyBytes),
          snapshot_bytes: new TextEncoder().encode(evidence.observation.snapshot).byteLength,
          dom: evidence.observation.dom,
          matched_signals: classification.matchedSignals,
          login_detected: classification.loginDetected,
          profile: evidence.profile,
          screenshot: evidence.screenshot,
        }
        summary.verdict = classification.verdict
        summary.failure_layer = classification.failureLayer
        return await finish(options.output, summary, started, classification.pass ? 0 : 2)
      } catch (error) {
        if (error instanceof BrowserNavigationError && attempt === MAX_BROWSER_ATTEMPTS) {
          const navigationFailure = browser
            ? await browser.inspectNavigationFailure().catch(() => null)
            : null
          const bodyBytes = new TextEncoder().encode(navigationFailure?.bodyText ?? "")
          summary.tools.browser_attempts = browserAttempts
          summary.tools.browser_session_recovered = false
          summary.browser = {
            error: safeError(error),
            prior_attempt_errors: browserErrors,
            ...(navigationFailure
              ? {
                final_url: navigationFailure.finalUrl,
                title: navigationFailure.title,
                ready_state: navigationFailure.readyState,
                body_text_bytes: bodyBytes.byteLength,
                body_text_sha256: await sha256(bodyBytes),
              }
              : { navigation_state: "uninspectable_after_timeout" }),
          }
          summary.verdict = "network_failure"
          summary.failure_layer = "service-probe"
          return await finish(options.output, summary, started, 2)
        }
        await browser?.close()
        browser = null
        if (
          error instanceof BrowserCommandError && attempt < MAX_BROWSER_ATTEMPTS &&
          Date.now() + options.timeoutSeconds * 1000 < deadlineAt
        ) {
          browserErrors.push(safeError(error))
          continue
        }
        throw error
      }
    }
    throw new BrowserCommandError("browser attempts exhausted")
  } catch (error) {
    summary.verdict = error instanceof ProbeDeadlineError
      ? "probe_deadline_exceeded"
      : "tooling_failure"
    summary.failure_layer = "service-probe"
    summary.tools.browser_attempts = browserAttempts
    summary.tools.browser_session_recovered = false
    summary.browser = {
      error: safeError(error),
      prior_attempt_errors: browserErrors,
    }
    return await finish(options.output, summary, started, 2)
  } finally {
    await browser?.close()
    await prepared.server?.shutdown()
  }
}

function parseArgs(args: string[]): Options {
  const allowedOptions = new Set([
    "--service",
    "--proxy",
    "--output",
    "--timeout-seconds",
    "--deadline-seconds",
    "--max-screenshot-bytes",
    "--instance-id",
    "--image-identity",
    "--config-digest",
    "--node-id",
    "--runtime",
    "--composition",
    "--transport",
    "--substrate-profile",
    "--requested-region",
    "--browser-provider",
    "--capture-screenshot",
  ])
  const values = new Map<string, string>()
  for (let index = 0; index < args.length; index += 2) {
    const name = args[index]
    const value = args[index + 1]
    if (!name?.startsWith("--") || value === undefined) usage(`invalid argument: ${name ?? ""}`)
    if (!allowedOptions.has(name)) usage(`unknown option: ${name}`)
    if (values.has(name)) usage(`duplicate option: ${name}`)
    values.set(name, value)
  }
  const rawService = values.get("--service")
  if (!rawService || !isServiceName(rawService)) {
    usage(
      "--service must be chatgpt, gemini, google-search, reddit, or turnstile-reference",
    )
  }
  const timeoutSeconds = numberOption(values, "--timeout-seconds", 45, 5, 120)
  const deadlineSeconds = numberOption(values, "--deadline-seconds", 120, 15, 300)
  const maxScreenshotBytes = numberOption(
    values,
    "--max-screenshot-bytes",
    1_500_000,
    100_000,
    5_000_000,
  )
  const browserProvider = values.get("--browser-provider") || "local"
  if (browserProvider !== "local" && browserProvider !== "agentcore") {
    usage("--browser-provider must be local or agentcore")
  }
  const captureScreenshotRaw = values.get("--capture-screenshot") || "false"
  if (captureScreenshotRaw !== "true" && captureScreenshotRaw !== "false") {
    usage("--capture-screenshot must be true or false")
  }
  const timestamp = new Date().toISOString().replaceAll(/[-:]/g, "").replace(/\.\d+Z$/, "Z")
  return {
    service: rawService,
    proxy: values.get("--proxy") || null,
    output: values.get("--output") || `artifacts/browser/${rawService}/${timestamp}`,
    timeoutSeconds,
    deadlineSeconds,
    maxScreenshotBytes,
    instanceId: values.get("--instance-id") || null,
    imageIdentity: values.get("--image-identity") || null,
    configDigest: values.get("--config-digest") || null,
    nodeId: values.get("--node-id") || null,
    runtime: values.get("--runtime") || null,
    composition: values.get("--composition") || null,
    transport: values.get("--transport") || null,
    substrateProfile: values.get("--substrate-profile") || null,
    requestedRegion: values.get("--requested-region") || null,
    browserProvider,
    captureScreenshot: captureScreenshotRaw === "true",
  }
}

async function prepareScenario(
  options: Options,
): Promise<{ scenario: Scenario; server: Deno.HttpServer<Deno.NetAddr> | null }> {
  const base = scenarios[options.service]
  if (base.fixture === "turnstile-interactive") {
    if (options.browserProvider !== "local") {
      usage("turnstile-reference requires the local browser provider because its fixture is local")
    }
    const html = await Deno.readTextFile(
      new URL("../fixtures/turnstile-interactive.html", import.meta.url),
    )
    const server = Deno.serve(
      { hostname: "127.0.0.1", port: 0, onListen() {} },
      (request) => {
        const url = new URL(request.url)
        return url.pathname === "/turnstile-interactive"
          ? new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } })
          : new Response("not found", { status: 404 })
      },
    )
    const address = server.addr
    return {
      scenario: { ...base, url: `http://127.0.0.1:${address.port}/turnstile-interactive` },
      server,
    }
  }
  return { scenario: base, server: null }
}

function numberOption(
  values: Map<string, string>,
  name: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const raw = values.get(name)
  const parsed = raw === undefined ? fallback : Number(raw)
  if (!Number.isFinite(parsed) || parsed < minimum || parsed > maximum) {
    usage(`${name} must be between ${minimum} and ${maximum}`)
  }
  return parsed
}

function usage(message: string): never {
  throw new Error(
    `${message}\nusage: deno task probe --service <name> [--proxy socks5h://127.0.0.1:1080] [--output PATH]`,
  )
}

function redactProxy(proxy: string | null): string | null {
  if (!proxy) return null
  try {
    const url = new URL(proxy)
    url.username = ""
    url.password = ""
    url.search = ""
    url.hash = ""
    return url.toString()
  } catch {
    return "invalid-proxy-url"
  }
}

async function sha256(value: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", value)
  return `sha256:${
    Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("")
  }`
}

async function finish(
  output: string,
  summary: Summary,
  started: Date,
  exitCode: number,
): Promise<number> {
  summary.finished_at = new Date().toISOString()
  summary.elapsed_ms = Date.now() - started.getTime()
  summary.observation = buildObservation(summary)
  await Deno.writeTextFile(`${output}/summary.json`, `${JSON.stringify(summary, null, 2)}\n`)
  const trace = summary.trace
  const traceText = trace
    ? `warp=${trace.warp ?? "unknown"} loc=${trace.loc ?? "unknown"} colo=${
      trace.colo ?? "unknown"
    }`
    : "not-run"
  const lines = [
    `Service verdict: ${summary.verdict}`,
    `Failure layer: ${summary.failure_layer}`,
    `Trace: ${traceText}`,
    `Summary: ${output}/summary.json`,
  ]
  await Deno.writeTextFile(`${output}/verdict.txt`, `${lines.join("\n")}\n`)
  console.log(lines.join("\n"))
  return exitCode
}

export function buildObservation(summary: Summary): Observation {
  const successful = ["available", "available_login_required", "challenge_reference_rendered"]
    .includes(summary.verdict)
  const evaluatorFailure = [
    "unknown",
    "tooling_failure",
    "tooling_or_network_failure",
    "probe_deadline_exceeded",
  ].includes(summary.verdict)
  const finished = new Date(summary.finished_at!)
  const input = summary.input
  const trace = summary.trace
  return {
    schema_version: 1,
    observation_id: crypto.randomUUID(),
    observed_at: finished.toISOString(),
    fresh_until: new Date(finished.getTime() + 86_400_000).toISOString(),
    scenario_id: `${summary.service}.${summary.scenario}`,
    probe: {
      name: "browser-scenario",
      version: "1",
      execution: typeof input.browser_provider === "string" ? input.browser_provider : "local",
    },
    subject: {
      instance_id: typeof input.instance_id === "string" ? input.instance_id : null,
      node_id: typeof input.node_id === "string" ? input.node_id : null,
      runtime: typeof input.runtime === "string" ? input.runtime : null,
      image_identity: typeof input.image_identity === "string" ? input.image_identity : null,
      config_digest: typeof input.config_digest === "string" ? input.config_digest : null,
    },
    lane: {
      composition: typeof input.composition === "string" ? input.composition : null,
      transport: typeof input.transport === "string" ? input.transport : null,
      substrate_profile: typeof input.substrate_profile === "string"
        ? input.substrate_profile
        : null,
      requested_region: typeof input.requested_region === "string" ? input.requested_region : null,
    },
    egress: {
      warp: trace?.warp ?? null,
      region: trace?.loc ?? null,
      colo: trace?.colo ?? null,
    },
    result: {
      availability: successful ? "available" : evaluatorFailure ? "unknown" : "unavailable",
      class: summary.verdict,
      eligible: !evaluatorFailure,
    },
    confidence_stage: "single_observation",
    failure_layer: summary.failure_layer === "none" ? "none" : summary.failure_layer,
    latency_ms: summary.elapsed_ms!,
    artifacts: [
      { kind: "summary", path: "summary.json" },
      { kind: "verdict", path: "verdict.txt" },
    ],
  }
}

if (import.meta.main) Deno.exit(await main(Deno.args))
