export type ServiceName =
  | "chatgpt"
  | "gemini"
  | "google-search"
  | "reddit"
  | "turnstile-reference"

export interface Signal {
  name: string
  pattern: RegExp
}

export interface Scenario {
  service: ServiceName
  scenario: string
  url: string
  allowedDomains: string[]
  acceptedLocations: Array<{ hostname: string; pathPattern: RegExp }>
  requiredDom:
    | "prompt_control"
    | "search_results"
    | "public_posts"
    | "turnstile_widget"
  proxyBypass: string
  fixture: "turnstile-interactive" | null
  loginWallIsAvailable: boolean
  positiveSignals: Signal[]
  unavailableSignals: Signal[]
  blockedSignals: Signal[]
}

const regionUnavailable: Signal[] = [
  { name: "unsupported_country", pattern: /unsupported[_ ]country/i },
  { name: "country_unavailable", pattern: /not (?:currently )?available in your country/i },
  {
    name: "region_unavailable",
    pattern: /not (?:currently )?(?:available|supported) in your region/i,
  },
  {
    name: "location_unavailable",
    pattern: /not (?:currently )?(?:available|supported) in your location/i,
  },
]

export const scenarios: Record<ServiceName, Scenario> = {
  chatgpt: {
    service: "chatgpt",
    scenario: "anonymous_entry",
    url: "https://chatgpt.com/",
    allowedDomains: [
      "chatgpt.com",
      "*.chatgpt.com",
      "openai.com",
      "*.openai.com",
      "oaistatic.com",
      "*.oaistatic.com",
    ],
    acceptedLocations: [
      { hostname: "chatgpt.com", pathPattern: /^\// },
      { hostname: "auth.openai.com", pathPattern: /^\// },
    ],
    requiredDom: "prompt_control",
    proxyBypass: "",
    fixture: null,
    loginWallIsAvailable: true,
    positiveSignals: [
      { name: "chatgpt_prompt", pattern: /(?:message chatgpt|ask anything|how can i help)/i },
    ],
    unavailableSignals: regionUnavailable,
    blockedSignals: [
      { name: "chatgpt_vpn_block", pattern: /(?:disable|turn off).{0,30}(?:vpn|proxy)/i },
      { name: "chatgpt_access_denied", pattern: /access (?:is )?denied/i },
    ],
  },
  gemini: {
    service: "gemini",
    scenario: "anonymous_entry",
    url: "https://gemini.google.com/app?hl=en",
    allowedDomains: ["google.com", "*.google.com", "gstatic.com", "*.gstatic.com"],
    acceptedLocations: [
      { hostname: "gemini.google.com", pathPattern: /^\/app(?:\/|$)/ },
      { hostname: "accounts.google.com", pathPattern: /^\// },
    ],
    requiredDom: "prompt_control",
    proxyBypass: "",
    fixture: null,
    loginWallIsAvailable: true,
    positiveSignals: [
      { name: "gemini_entry", pattern: /(?:chat with gemini|meet gemini|ask gemini)/i },
    ],
    unavailableSignals: [
      ...regionUnavailable,
      {
        name: "gemini_country_unsupported",
        pattern: /gemini isn.t currently supported in your country/i,
      },
    ],
    blockedSignals: [
      { name: "google_access_denied", pattern: /(?:access denied|request (?:is )?blocked)/i },
    ],
  },
  "google-search": {
    service: "google-search",
    scenario: "anonymous_search_results",
    url: "https://www.google.com/search?q=cfwarp+connectivity+check&hl=en",
    allowedDomains: ["google.com", "*.google.com", "gstatic.com", "*.gstatic.com"],
    acceptedLocations: [{ hostname: "www.google.com", pathPattern: /^\/search$/ }],
    requiredDom: "search_results",
    proxyBypass: "",
    fixture: null,
    loginWallIsAvailable: false,
    positiveSignals: [
      {
        name: "google_search_control",
        pattern: /(?:textbox|combobox).{0,40}(?:search|cfwarp connectivity check)/i,
      },
    ],
    unavailableSignals: [],
    blockedSignals: [
      { name: "google_automated_queries", pattern: /automated quer(?:y|ies)/i },
    ],
  },
  reddit: {
    service: "reddit",
    scenario: "anonymous_public_listing",
    url: "https://www.reddit.com/r/popular/",
    allowedDomains: ["reddit.com", "*.reddit.com", "redditstatic.com", "*.redditstatic.com"],
    acceptedLocations: [{ hostname: "www.reddit.com", pathPattern: /^\/r\/popular(?:\/|$)/ }],
    requiredDom: "public_posts",
    proxyBypass: "",
    fixture: null,
    loginWallIsAvailable: false,
    positiveSignals: [
      { name: "reddit_public_listing", pattern: /(?:r\/popular|popular posts|popular on reddit)/i },
    ],
    unavailableSignals: [],
    blockedSignals: [
      {
        name: "reddit_network_block",
        pattern: /(?:blocked by network security|request (?:has been|is) blocked)/i,
      },
      { name: "reddit_whoa", pattern: /whoa there, pardner/i },
    ],
  },
  "turnstile-reference": {
    service: "turnstile-reference",
    scenario: "interactive_test_widget_render",
    url: "",
    allowedDomains: ["127.0.0.1", "challenges.cloudflare.com"],
    acceptedLocations: [{ hostname: "127.0.0.1", pathPattern: /^\/turnstile-interactive$/ }],
    requiredDom: "turnstile_widget",
    proxyBypass: "127.0.0.1",
    fixture: "turnstile-interactive",
    loginWallIsAvailable: false,
    positiveSignals: [
      { name: "turnstile_test_warning", pattern: /for testing only/i },
      { name: "turnstile_verify_human", pattern: /verify (?:that )?you are human/i },
    ],
    unavailableSignals: [],
    blockedSignals: [],
  },
}

export function isServiceName(value: string): value is ServiceName {
  return Object.hasOwn(scenarios, value)
}
