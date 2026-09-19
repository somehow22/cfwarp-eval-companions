import type { BrowserObservation } from "./classify.ts"

type DomFacts = BrowserObservation["dom"]

export function collectDomFacts(document: Document, pageUrl: string): DomFacts {
  const view = document.defaultView

  function visible(element: Element): boolean {
    for (let current: Element | null = element; current; current = current.parentElement) {
      if (
        current.hasAttribute("hidden") || current.hasAttribute("inert") ||
        current.getAttribute("aria-hidden") === "true"
      ) {
        return false
      }
      const style = (current.getAttribute("style") || "").replaceAll(/\s/g, "").toLowerCase()
      if (
        style.includes("display:none") || style.includes("visibility:hidden") ||
        style.includes("visibility:collapse") ||
        /(^|;)opacity:0(?:\.0+)?(?:!important)?(?:;|$)/.test(style)
      ) {
        return false
      }
      const computed = typeof view?.getComputedStyle === "function"
        ? view.getComputedStyle(current)
        : null
      if (
        computed?.display === "none" || computed?.visibility === "hidden" ||
        computed?.visibility === "collapse" || computed?.opacity === "0" ||
        computed?.contentVisibility === "hidden"
      ) {
        return false
      }
    }
    if (typeof element.getClientRects === "function") {
      return Array.from(element.getClientRects()).some(
        (rect) => rect.width > 0 && rect.height > 0,
      )
    }
    return true
  }

  function applicationPrompt(element: Element): boolean {
    const marker = [
      element.getAttribute("aria-label"),
      element.getAttribute("placeholder"),
      element.getAttribute("data-placeholder"),
      element.getAttribute("data-testid"),
      element.getAttribute("id"),
      element.getAttribute("name"),
    ].filter(Boolean).join(" ").toLowerCase()
    if (/\b(?:search|feedback|comment|review)\b/.test(marker)) return false
    let hostname = ""
    try {
      hostname = new URL(pageUrl).hostname.toLowerCase()
    } catch {
      return false
    }
    if (hostname === "gemini.google.com") {
      return /\bgemini\b|\bprompt\b|\bcomposer\b|\binput-area\b/.test(marker)
    }
    if (hostname === "chatgpt.com" || hostname.endsWith(".chatgpt.com")) {
      return /\bchatgpt\b|\bprompt\b|\bcomposer\b/.test(marker)
    }
    return false
  }

  function promptUsable(element: Element): boolean {
    if (!applicationPrompt(element) || !visible(element)) return false
    if (
      element.hasAttribute("disabled") || element.matches(":disabled") ||
      element.getAttribute("aria-disabled") === "true" ||
      element.hasAttribute("readonly") || element.getAttribute("aria-readonly") === "true"
    ) {
      return false
    }
    if (element.getAttribute("contenteditable") === "false") return false
    return true
  }

  function validRedditPermalink(value: string | null): boolean {
    if (!value?.trim()) return false
    try {
      const url = new URL(value, pageUrl)
      const host = url.hostname.toLowerCase()
      if (host !== "reddit.com" && !host.endsWith(".reddit.com")) return false
      return /^\/r\/[^/]+\/comments\/[^/]+(?:\/|$)/i.test(url.pathname)
    } catch {
      return false
    }
  }

  function renderedText(element: Element): string {
    const browserText = (element as HTMLElement).innerText
    if (
      typeof element.getClientRects === "function" &&
      typeof browserText === "string"
    ) {
      return browserText.trim()
    }

    function structuralText(node: Node): string {
      if (node.nodeType === 3) return node.nodeValue || ""
      if (node.nodeType !== 1) return ""
      const child = node as Element
      if (!visible(child)) return ""
      return Array.from(child.childNodes).map(structuralText).join(" ")
    }

    return Array.from(element.childNodes).map(structuralText).join(" ").trim()
  }

  const promptControlCount = Array.from(
    document.querySelectorAll(
      "textarea,[contenteditable=true]",
    ),
  ).filter(promptUsable).length
  const searchControlCount = document.querySelectorAll(
    "[role=search],input[type=search],input[name=q],textarea[name=q]",
  ).length
  const searchResultCount = document.querySelectorAll("#search a[href] h3,#rso a[href] h3").length
  const postContainers = Array.from(
    document.querySelectorAll(
      "shreddit-post,article[data-testid*=post],[data-testid=post-container]",
    ),
  )
  let publicPostTitlePermalinkCount = 0
  for (const post of postContainers) {
    if (!visible(post)) continue
    const links = Array.from(post.querySelectorAll("a[href]"))
    if (
      links.some((link) => {
        if (!visible(link)) return false
        if (!validRedditPermalink(link.getAttribute("href"))) return false
        const title = link.closest("h1,h2,h3,[slot=title],[data-testid*=title]") ??
          link.querySelector("h1,h2,h3,[slot=title],[data-testid*=title]")
        return Boolean(title && visible(title) && renderedText(title))
      })
    ) {
      publicPostTitlePermalinkCount += 1
    }
  }

  return {
    promptControlCount,
    searchControlCount,
    searchResultCount,
    publicPostCount: postContainers.length,
    publicPostTitlePermalinkCount,
    loginFormCount: document.querySelectorAll(
      "form input[type=password],form[action*=login],form[action*=signin]",
    ).length,
    turnstileWidgetCount: document.querySelectorAll(
      '[name=cf-turnstile-response],iframe[src*="challenges.cloudflare"]',
    ).length,
  }
}

export function pageObservationScript(): string {
  return `(() => {
    const collectDomFacts = ${collectDomFacts.toString()};
    return {
      status: performance.getEntriesByType('navigation')[0]?.responseStatus ?? null,
      title: document.title,
      url: location.href,
      text: document.body?.innerText?.slice(0, 12000) ?? '',
      dom: collectDomFacts(document, location.href),
      profile: {
        userAgent: navigator.userAgent,
        locale: navigator.language,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        viewport: {width: innerWidth, height: innerHeight, devicePixelRatio},
        webRtcAvailable: typeof RTCPeerConnection !== 'undefined',
        webdriver: navigator.webdriver,
      },
    };
  })()`
}

export function domFactsScript(pageUrl: string): string {
  return `(() => {
    const collectDomFacts = ${collectDomFacts.toString()};
    return collectDomFacts(document, ${JSON.stringify(pageUrl)});
  })()`
}
