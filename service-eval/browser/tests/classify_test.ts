import { type BrowserObservation, classify } from "../src/classify.ts"
import { scenarios, type ServiceName } from "../src/scenarios.ts"

function observation(overrides: Partial<BrowserObservation> = {}): BrowserObservation {
  return {
    finalUrl: "https://example.com/",
    httpStatus: 200,
    title: "",
    bodyText: "",
    snapshot: "",
    dom: {
      promptControlCount: 0,
      searchControlCount: 0,
      searchResultCount: 0,
      publicPostCount: 0,
      loginFormCount: 0,
      turnstileWidgetCount: 0,
    },
    ...overrides,
  }
}

Deno.test("classifies supported anonymous entry and listing scenarios", () => {
  const cases: Array<[ServiceName, BrowserObservation, string, boolean]> = [
    [
      "chatgpt",
      observation({
        finalUrl: "https://chatgpt.com/",
        title: "ChatGPT",
        bodyText: "Log in",
        dom: { ...observation().dom, promptControlCount: 1 },
      }),
      "available_login_required",
      true,
    ],
    [
      "gemini",
      observation({
        finalUrl: "https://gemini.google.com/app",
        title: "Gemini",
        bodyText: "Sign in",
        dom: { ...observation().dom, promptControlCount: 1 },
      }),
      "available_login_required",
      true,
    ],
    [
      "google-search",
      observation({
        finalUrl: "https://www.google.com/search",
        title: "cfwarp connectivity check - Google Search",
        dom: { ...observation().dom, searchControlCount: 1, searchResultCount: 3 },
      }),
      "available",
      true,
    ],
    [
      "reddit",
      observation({
        finalUrl: "https://www.reddit.com/r/popular/",
        title: "Popular posts - Reddit",
        dom: { ...observation().dom, publicPostCount: 2 },
      }),
      "available",
      true,
    ],
  ]
  for (const [service, evidence, verdict, pass] of cases) {
    const result = classify(scenarios[service], evidence)
    if (result.verdict !== verdict || result.pass !== pass) {
      throw new Error(`${service}: got ${result.verdict}/${result.pass}`)
    }
  }
})

Deno.test("challenge and explicit blocks take precedence over branding", () => {
  const challenged = classify(
    scenarios["google-search"],
    observation({
      finalUrl: "https://www.google.com/sorry/index",
      httpStatus: 429,
      title: "Google Search",
      bodyText: "Our systems have detected unusual traffic from your computer network",
    }),
  )
  if (challenged.verdict !== "challenge" || challenged.pass) {
    throw new Error("Google challenge was not preserved")
  }

  const interstitial = classify(
    scenarios.chatgpt,
    observation({ httpStatus: 403, title: "Just a moment...", bodyText: "ChatGPT" }),
  )
  if (interstitial.verdict !== "challenge") {
    throw new Error("Cloudflare interstitial was not preserved")
  }

  const blocked = classify(
    scenarios.reddit,
    observation({ title: "Reddit", bodyText: "You've been blocked by network security." }),
  )
  if (blocked.verdict !== "blocked" || blocked.pass) {
    throw new Error("Reddit block was not preserved")
  }
})

Deno.test("region blocks, login walls, and unknown pages remain non-passing", () => {
  const region = classify(
    scenarios.gemini,
    observation({ title: "Gemini", bodyText: "Gemini isn't currently supported in your country" }),
  )
  if (region.verdict !== "unavailable") {
    throw new Error(`unexpected Gemini verdict: ${region.verdict}`)
  }

  const login = classify(
    scenarios.reddit,
    observation({ finalUrl: "https://www.reddit.com/login/", bodyText: "Log in" }),
  )
  if (login.verdict !== "auth_required") {
    throw new Error(`unexpected Reddit verdict: ${login.verdict}`)
  }

  const unknown = classify(
    scenarios.chatgpt,
    observation({ finalUrl: "https://chatgpt.com/", bodyText: "generic page" }),
  )
  if (unknown.verdict !== "unknown") {
    throw new Error(`unexpected unknown verdict: ${unknown.verdict}`)
  }

  const brandedSearchShell = classify(
    scenarios["google-search"],
    observation({
      finalUrl: "https://www.google.com/search",
      title: "cfwarp connectivity check - Google Search",
      dom: { ...observation().dom, searchControlCount: 1 },
    }),
  )
  if (brandedSearchShell.verdict !== "unknown") {
    throw new Error(`title-only search shell passed as ${brandedSearchShell.verdict}`)
  }

  const wrongOrigin = classify(
    scenarios.gemini,
    observation({
      finalUrl: "https://www.google.com/",
      title: "Gemini",
      dom: { ...observation().dom, promptControlCount: 1 },
    }),
  )
  if (wrongOrigin.verdict !== "unknown") {
    throw new Error(`wrong-origin page passed as ${wrongOrigin.verdict}`)
  }
})

Deno.test("HTTP 403 and 5xx are conservative failures", () => {
  const blocked = classify(scenarios.chatgpt, observation({ httpStatus: 403, title: "ChatGPT" }))
  if (blocked.verdict !== "blocked") throw new Error(`unexpected 403 verdict: ${blocked.verdict}`)
  const unavailable = classify(scenarios.chatgpt, observation({ httpStatus: 503 }))
  if (unavailable.verdict !== "service_unavailable") {
    throw new Error(`unexpected 503 verdict: ${unavailable.verdict}`)
  }
})

Deno.test("challenge references pass only with expected challenge DOM", () => {
  const turnstile = classify(
    scenarios["turnstile-reference"],
    observation({
      finalUrl: "http://127.0.0.1:12345/turnstile-interactive",
      bodyText: "For testing only. Verify you are human",
      dom: { ...observation().dom, turnstileWidgetCount: 2 },
    }),
  )
  if (turnstile.verdict !== "challenge_reference_rendered" || !turnstile.pass) {
    throw new Error(`Turnstile reference did not pass: ${turnstile.verdict}`)
  }

  const missingWidget = classify(
    scenarios["turnstile-reference"],
    observation({
      finalUrl: "http://127.0.0.1:12345/turnstile-interactive",
      bodyText: "Turnstile reference shell",
    }),
  )
  if (missingWidget.verdict !== "unknown" || missingWidget.pass) {
    throw new Error(`empty Turnstile shell passed: ${missingWidget.verdict}`)
  }
})
