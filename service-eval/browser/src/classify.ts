import type { Scenario, Signal } from "./scenarios.ts"

export interface BrowserObservation {
  finalUrl: string
  httpStatus: number | null
  title: string
  bodyText: string
  snapshot: string
  dom: {
    promptControlCount: number
    searchControlCount: number
    searchResultCount: number
    publicPostCount: number
    loginFormCount: number
    turnstileWidgetCount: number
  }
}

export interface Classification {
  verdict:
    | "available"
    | "available_login_required"
    | "challenge"
    | "blocked"
    | "unavailable"
    | "auth_required"
    | "service_unavailable"
    | "challenge_reference_rendered"
    | "unknown"
  pass: boolean
  failureLayer: "none" | "service-probe"
  matchedSignals: string[]
  loginDetected: boolean
}

const challengeSignals: Signal[] = [
  { name: "captcha", pattern: /captcha|recaptcha/i },
  { name: "human_verification", pattern: /verify (?:that )?you are (?:a )?human/i },
  { name: "unusual_traffic", pattern: /our systems have detected unusual traffic/i },
  { name: "google_sorry", pattern: /\/sorry\//i },
  { name: "challenge_platform", pattern: /(?:challenge-platform|cf-chl-)/i },
]

const loginSignals: Signal[] = [
  { name: "login_url", pattern: /\/(?:login|signin|sign-in)(?:[/?#]|$)/i },
  { name: "login_text", pattern: /\b(?:log in|sign in)\b/i },
]

function matches(signals: Signal[], evidence: string): string[] {
  return signals.filter((signal) => signal.pattern.test(evidence)).map((signal) => signal.name)
}

export function classify(scenario: Scenario, observation: BrowserObservation): Classification {
  const evidence = [
    observation.finalUrl,
    observation.title,
    observation.bodyText,
    observation.snapshot,
  ].join("\n")
  const pageEvidence = [observation.title, observation.bodyText, observation.snapshot].join("\n")
  const login = matches(loginSignals, evidence)
  const challenge = matches(challengeSignals, evidence)
  if (/^just a moment(?:\.{3})?$/i.test(observation.title.trim())) {
    challenge.push("cloudflare_interstitial")
  }
  const acceptedLocation = isAcceptedLocation(scenario, observation.finalUrl)
  const referenceStatus = observation.httpStatus !== null &&
    ((observation.httpStatus >= 200 && observation.httpStatus < 400) ||
      observation.httpStatus === 405)
  const referenceSignals = matches(scenario.positiveSignals, pageEvidence)
  if (
    scenario.requiredDom === "turnstile_widget" && acceptedLocation && referenceStatus &&
    observation.dom.turnstileWidgetCount > 0 && referenceSignals.length > 0
  ) {
    return result(
      "challenge_reference_rendered",
      [...referenceSignals, "turnstile_widget"],
      login,
      true,
    )
  }
  if (observation.httpStatus === 429 || challenge.length > 0) {
    return result("challenge", [
      ...challenge,
      ...(observation.httpStatus === 429 ? ["http_429"] : []),
    ], login)
  }

  const unavailable = matches(scenario.unavailableSignals, evidence)
  if (unavailable.length > 0) {
    return result("unavailable", unavailable, login)
  }

  const blocked = matches(scenario.blockedSignals, evidence)
  if (observation.httpStatus === 403 || blocked.length > 0) {
    return result(
      "blocked",
      [...blocked, ...(observation.httpStatus === 403 ? ["http_403"] : [])],
      login,
    )
  }

  if (observation.httpStatus !== null && observation.httpStatus >= 500) {
    return result("service_unavailable", ["http_5xx"], login)
  }

  const positive = matches(scenario.positiveSignals, pageEvidence)
  const reachable = observation.httpStatus !== null && observation.httpStatus >= 200 &&
    observation.httpStatus < 400
  const requiredDomPresent = hasRequiredDom(scenario, observation)
  if (
    reachable && acceptedLocation && scenario.loginWallIsAvailable &&
    observation.dom.loginFormCount > 0
  ) {
    return result("available_login_required", [...positive, ...login, "login_form"], login, true)
  }
  if (
    reachable &&
    login.length > 0 &&
    !scenario.loginWallIsAvailable &&
    (positive.length === 0 || login.includes("login_url"))
  ) {
    return result("auth_required", login, login)
  }
  if (reachable && acceptedLocation && requiredDomPresent) {
    const verdict = login.length > 0 && scenario.loginWallIsAvailable
      ? "available_login_required"
      : "available"
    return result(verdict, [...positive, scenario.requiredDom], login, true)
  }
  return result("unknown", positive, login)
}

function isAcceptedLocation(scenario: Scenario, value: string): boolean {
  try {
    const url = new URL(value)
    return scenario.acceptedLocations.some((location) =>
      url.hostname === location.hostname && location.pathPattern.test(url.pathname)
    )
  } catch {
    return false
  }
}

function hasRequiredDom(scenario: Scenario, observation: BrowserObservation): boolean {
  switch (scenario.requiredDom) {
    case "prompt_control":
      return observation.dom.promptControlCount > 0
    case "search_results":
      return observation.dom.searchControlCount > 0 && observation.dom.searchResultCount > 0
    case "public_posts":
      return observation.dom.publicPostCount > 0
    case "turnstile_widget":
      return observation.dom.turnstileWidgetCount > 0
  }
}

function result(
  verdict: Classification["verdict"],
  matchedSignals: string[],
  loginSignals: string[],
  pass = false,
): Classification {
  return {
    verdict,
    pass,
    failureLayer: pass ? "none" : "service-probe",
    matchedSignals: [...new Set(matchedSignals)].sort(),
    loginDetected: loginSignals.length > 0,
  }
}
