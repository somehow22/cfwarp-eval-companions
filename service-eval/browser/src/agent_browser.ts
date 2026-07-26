import type { BrowserObservation } from "./classify.ts"
import type { Scenario } from "./scenarios.ts"

interface CommandResult {
  stdout: string
  stderr: string
}

export interface TraceEvidence {
  ok: boolean
  warp: string | null
  loc: string | null
  colo: string | null
  ip: string | null
  httpStatus: number | null
}

export interface BrowserEvidence {
  observation: BrowserObservation
  profile: {
    userAgent: string
    locale: string
    timezone: string
    viewport: { width: number; height: number; devicePixelRatio: number }
    webRtcAvailable: boolean
    webdriver: boolean
  }
  screenshot: { path: string | null; bytes: number | null; omittedReason: string | null }
}

export interface NavigationFailureEvidence {
  finalUrl: string
  title: string
  bodyText: string
  readyState: string
}

export class BrowserCommandError extends Error {}
export class BrowserNavigationError extends BrowserCommandError {}
class BrowserCommandTimeoutError extends BrowserCommandError {}
export class ProbeDeadlineError extends Error {}

const TRACE_URL = "https://one.one.one.one/cdn-cgi/trace"
const CONFIG_PATH = decodeURIComponent(new URL("../agent-browser.json", import.meta.url).pathname)
export const CLEANUP_BUDGET_MS = 5_000
const MAX_BROWSER_ACTION_TIMEOUT_MS = 12_000
const PASSTHROUGH_ENV = [
  "PATH",
  "HOME",
  "USER",
  "LOGNAME",
  "TMPDIR",
  "TMP",
  "TEMP",
  "XDG_RUNTIME_DIR",
  "XDG_CACHE_HOME",
  "AGENT_BROWSER_EXECUTABLE_PATH",
  "LANG",
  "LC_ALL",
  "AWS_PROFILE",
  "AGENTCORE_REGION",
  "AGENTCORE_BROWSER_ID",
  "AGENTCORE_SESSION_TIMEOUT",
] as const

export class AgentBrowser {
  readonly session: string
  readonly version: string
  #proxy: string | null
  #commandTimeoutMs: number
  #deadlineAt: number
  #allowedDomains: string
  #proxyBypass: string
  #provider: "local" | "agentcore"

  private constructor(
    session: string,
    version: string,
    proxy: string | null,
    commandTimeoutMs: number,
    deadlineAt: number,
    allowedDomains: string,
    proxyBypass: string,
    provider: "local" | "agentcore",
  ) {
    this.session = session
    this.version = version
    this.#proxy = proxy
    this.#commandTimeoutMs = commandTimeoutMs
    this.#deadlineAt = deadlineAt
    this.#allowedDomains = allowedDomains
    this.#proxyBypass = proxyBypass
    this.#provider = provider
  }

  static async create(
    scenario: Scenario,
    proxy: string | null,
    commandTimeoutMs: number,
    deadlineAt: number,
    provider: "local" | "agentcore",
  ): Promise<AgentBrowser> {
    const remaining = deadlineAt - Date.now()
    if (remaining <= 0) throw new ProbeDeadlineError("whole-probe deadline exceeded")
    let version: string
    try {
      version = (await runCommand(
        ["--config", CONFIG_PATH, "--version"],
        Math.min(commandTimeoutMs, remaining),
      )).stdout.trim()
    } catch (error) {
      if (error instanceof BrowserCommandTimeoutError && Date.now() >= deadlineAt) {
        throw new ProbeDeadlineError("whole-probe deadline exceeded")
      }
      throw error
    }
    const session = `cfwarp-${scenario.service}-${crypto.randomUUID()}`
    const domains = [
      "www.cloudflare.com",
      "one.one.one.one",
      "challenges.cloudflare.com",
      ...scenario.allowedDomains,
    ].join(",")
    return new AgentBrowser(
      session,
      version,
      normalizeProxy(proxy),
      commandTimeoutMs,
      deadlineAt,
      domains,
      scenario.proxyBypass,
      provider,
    )
  }

  async checkTrace(): Promise<TraceEvidence> {
    await this.#open(TRACE_URL)
    await this.#command(["wait", "1000"])
    const result = await this.#eval<{ status: number | null; text: string }>(
      "({status: performance.getEntriesByType('navigation')[0]?.responseStatus ?? null, text: document.body?.innerText?.slice(0, 4096) ?? ''})",
    )
    const fields = parseTrace(result.text)
    return {
      ok: fields.warp === "on",
      warp: fields.warp ?? null,
      loc: fields.loc ?? null,
      colo: fields.colo ?? null,
      ip: fields.ip ?? null,
      httpStatus: result.status,
    }
  }

  async observe(
    scenario: Scenario,
    outputDir: string,
    maxScreenshotBytes: number,
    captureScreenshot: boolean,
  ): Promise<BrowserEvidence> {
    try {
      await this.#open(scenario.url)
    } catch (error) {
      if (error instanceof BrowserCommandError) {
        throw new BrowserNavigationError(safeError(error))
      }
      throw error
    }
    await this.#command(["wait", "1000"])
    const facts = await this.#eval<{
      status: number | null
      title: string
      url: string
      text: string
      dom: BrowserObservation["dom"]
      profile: BrowserEvidence["profile"]
    }>(
      "({status: performance.getEntriesByType('navigation')[0]?.responseStatus ?? null, title: document.title, url: location.href, text: document.body?.innerText?.slice(0, 12000) ?? '', dom: {promptControlCount: document.querySelectorAll('textarea,[contenteditable=true],[role=textbox]:not(input[type=search])').length, searchControlCount: document.querySelectorAll('[role=search],input[type=search],input[name=q],textarea[name=q]').length, searchResultCount: document.querySelectorAll('#search a[href] h3,#rso a[href] h3').length, publicPostCount: document.querySelectorAll('shreddit-post,article[data-testid*=post],[data-testid=post-container]').length, loginFormCount: document.querySelectorAll('form input[type=password],form[action*=login],form[action*=signin]').length, turnstileWidgetCount: document.querySelectorAll('[name=cf-turnstile-response],iframe[src*=\"challenges.cloudflare\"]').length}, profile: {userAgent: navigator.userAgent, locale: navigator.language, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, viewport: {width: innerWidth, height: innerHeight, devicePixelRatio}, webRtcAvailable: typeof RTCPeerConnection !== 'undefined', webdriver: navigator.webdriver}})",
    )
    const snapshotEnvelope = await this.#command(["snapshot", "-i", "-c", "-d", "4"])
    const snapshotData = envelopeData(snapshotEnvelope.stdout)
    const snapshot = isRecord(snapshotData) && typeof snapshotData.snapshot === "string"
      ? snapshotData.snapshot.slice(0, 12000)
      : ""
    const screenshot = captureScreenshot
      ? await this.#screenshot(outputDir, maxScreenshotBytes)
      : { path: null, bytes: null, omittedReason: "not_requested" }
    return {
      observation: {
        finalUrl: stripUrlSecrets(facts.url),
        httpStatus: typeof facts.status === "number" ? facts.status : null,
        title: facts.title.slice(0, 500),
        bodyText: facts.text,
        snapshot,
        dom: facts.dom,
      },
      profile: facts.profile,
      screenshot,
    }
  }

  async inspectNavigationFailure(): Promise<NavigationFailureEvidence | null> {
    const facts = await this.#eval<{
      url: string
      title: string
      text: string
      readyState: string
    }>(
      "({url: location.href, title: document.title, text: document.body?.innerText?.slice(0, 2000) ?? '', readyState: document.readyState})",
    )
    if (!isChromeNavigationFailureUrl(facts.url)) return null
    return {
      finalUrl: facts.url,
      title: facts.title.slice(0, 500),
      bodyText: facts.text,
      readyState: facts.readyState,
    }
  }

  async close(): Promise<void> {
    try {
      await runCommand(
        [
          ...(this.#provider === "agentcore" ? ["--provider", "agentcore"] : []),
          "--config",
          CONFIG_PATH,
          "--session",
          this.session,
          "--json",
          ...sessionPolicyArguments(
            this.#allowedDomains,
            this.#proxy,
            this.#proxyBypass,
          ),
          "close",
        ],
        CLEANUP_BUDGET_MS,
      )
    } catch {
      // Best-effort cleanup must not replace the service verdict.
    }
  }

  async #open(url: string): Promise<void> {
    const args = [
      ...(this.#provider === "agentcore" ? ["--provider", "agentcore"] : []),
      "--session",
      this.session,
      "--config",
      CONFIG_PATH,
      "--json",
      "--max-output",
      "20000",
    ]
    args.push(...sessionPolicyArguments(
      this.#allowedDomains,
      this.#proxy,
      this.#proxyBypass,
    ))
    args.push("open", url)
    await this.#rawCommand(args)
  }

  async #eval<T>(script: string): Promise<T> {
    const result = await this.#command(["eval", script])
    const data = envelopeData(result.stdout)
    if (!isRecord(data) || !("result" in data)) {
      throw new BrowserCommandError("agent-browser eval returned no result")
    }
    return data.result as T
  }

  async #screenshot(
    outputDir: string,
    maxBytes: number,
  ): Promise<BrowserEvidence["screenshot"]> {
    const path = `${outputDir}/screenshot.jpeg`
    try {
      try {
        await Deno.remove(path)
      } catch (error) {
        if (!(error instanceof Deno.errors.NotFound)) throw error
      }
      await this.#command([
        "screenshot",
        "--screenshot-format",
        "jpeg",
        "--screenshot-quality",
        "60",
        path,
      ])
      const stat = await Deno.stat(path)
      if (stat.size > maxBytes) {
        await Deno.remove(path)
        return { path: null, bytes: stat.size, omittedReason: "size_limit" }
      }
      const file = await Deno.open(path, { read: true })
      const magic = new Uint8Array(3)
      try {
        await file.read(magic)
      } finally {
        file.close()
      }
      if (magic[0] !== 0xff || magic[1] !== 0xd8 || magic[2] !== 0xff) {
        await Deno.remove(path)
        return { path: null, bytes: stat.size, omittedReason: "invalid_jpeg" }
      }
      return { path: "screenshot.jpeg", bytes: stat.size, omittedReason: null }
    } catch (error) {
      if (error instanceof ProbeDeadlineError || Date.now() >= this.#deadlineAt) {
        throw new ProbeDeadlineError("whole-probe deadline exceeded")
      }
      return { path: null, bytes: null, omittedReason: safeError(error) }
    }
  }

  #command(args: string[]): Promise<CommandResult> {
    return this.#rawCommand([
      ...(this.#provider === "agentcore" ? ["--provider", "agentcore"] : []),
      "--config",
      CONFIG_PATH,
      "--session",
      this.session,
      "--json",
      "--max-output",
      "20000",
      ...sessionPolicyArguments(
        this.#allowedDomains,
        this.#proxy,
        this.#proxyBypass,
      ),
      ...args,
    ])
  }

  #rawCommand(args: string[]): Promise<CommandResult> {
    const remaining = this.#deadlineAt - Date.now()
    const actionBudget = browserActionTimeout(this.#commandTimeoutMs)
    if (remaining <= actionBudget + 500) {
      throw new ProbeDeadlineError("whole-probe deadline exceeded")
    }
    return runCommand(args, Math.min(this.#commandTimeoutMs, remaining)).catch((error) => {
      if (error instanceof BrowserCommandTimeoutError && Date.now() >= this.#deadlineAt) {
        throw new ProbeDeadlineError("whole-probe deadline exceeded")
      }
      throw error
    })
  }
}

export function proxyArguments(proxy: string | null, bypass: string): string[] {
  return proxy ? ["--proxy", proxy, "--proxy-bypass", bypass] : []
}

export function sessionPolicyArguments(
  allowedDomains: string,
  proxy: string | null,
  bypass: string,
): string[] {
  return [
    "--allowed-domains",
    allowedDomains,
    ...proxyArguments(proxy, bypass),
  ]
}

async function runCommand(args: string[], timeoutMs: number): Promise<CommandResult> {
  let child: Deno.ChildProcess
  try {
    child = new Deno.Command("agent-browser", {
      args,
      stdin: "null",
      stdout: "piped",
      stderr: "piped",
      clearEnv: true,
      env: commandEnvironment(readProcessEnvironment(), timeoutMs),
    }).spawn()
  } catch (error) {
    throw new BrowserCommandError(safeError(error))
  }
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<"timeout">((resolve) => {
    timer = setTimeout(() => {
      resolve("timeout")
    }, timeoutMs)
  })
  try {
    const outputPromise = child.output()
    const outcome = await Promise.race([outputPromise, timeout])
    if (outcome === "timeout") {
      try {
        child.kill("SIGKILL")
      } catch {
        // Process may have exited between timeout and kill.
      }
      // Do not await output here: agent-browser's detached daemon can inherit a
      // pipe and delay EOF. The session-specific close in main's finally block
      // terminates that daemon within the reserved cleanup budget.
      throw new BrowserCommandTimeoutError("agent-browser command timed out")
    }
    const output = outcome
    const stdout = new TextDecoder().decode(output.stdout)
    const stderr = new TextDecoder().decode(output.stderr)
    if (!output.success) {
      throw new BrowserCommandError(
        `${stderr || stdout || "agent-browser command failed"}`.trim().slice(0, 2000),
      )
    }
    return { stdout, stderr }
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

function readProcessEnvironment(): Record<string, string> {
  const source: Record<string, string> = {}
  for (const name of PASSTHROUGH_ENV) {
    const value = Deno.env.get(name)
    if (value) source[name] = value
  }
  return source
}

export function sanitizedEnvironment(source: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    PASSTHROUGH_ENV.flatMap((name) => source[name] ? [[name, source[name]]] : []),
  )
}

export function commandEnvironment(
  source: Record<string, string>,
  timeoutMs: number,
): Record<string, string> {
  return {
    ...sanitizedEnvironment(source),
    AGENT_BROWSER_DEFAULT_TIMEOUT: String(browserActionTimeout(timeoutMs)),
    AGENT_BROWSER_IDLE_TIMEOUT_MS: String(browserIdleTimeout(timeoutMs)),
  }
}

function browserActionTimeout(timeoutMs: number): number {
  return Math.min(MAX_BROWSER_ACTION_TIMEOUT_MS, Math.max(1_000, timeoutMs - 500))
}

function browserIdleTimeout(timeoutMs: number): number {
  return Math.max(60_000, timeoutMs * 2)
}

function envelopeData(text: string): unknown {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new BrowserCommandError("agent-browser returned invalid JSON")
  }
  if (!isRecord(parsed) || parsed.success !== true) {
    throw new BrowserCommandError("agent-browser returned an unsuccessful result")
  }
  return parsed.data
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

export function parseTrace(text: string): Record<string, string> {
  const fields: Record<string, string> = {}
  for (const line of text.split("\n")) {
    const separator = line.indexOf("=")
    if (separator > 0) fields[line.slice(0, separator)] = line.slice(separator + 1).trim()
  }
  return fields
}

export function normalizeProxy(proxy: string | null): string | null {
  if (!proxy) return null
  return proxy.startsWith("socks5h://") ? `socks5://${proxy.slice("socks5h://".length)}` : proxy
}

export function isChromeNavigationFailureUrl(value: string): boolean {
  return value === "chrome-error://chromewebdata/" || value.startsWith("chrome-error://")
}

export function stripUrlSecrets(value: string): string {
  try {
    const url = new URL(value)
    url.username = ""
    url.password = ""
    return `${url.origin}${url.pathname}`
  } catch {
    return value.slice(0, 500)
  }
}

export function safeError(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error)
  return text.replace(/(?:https?|socks5h?):\/\/[^\s"'`]+/gi, stripUrlSecrets)
    .slice(0, 2000)
}
