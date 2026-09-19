import type { BrowserObservation } from "./classify.ts"

type DomFacts = BrowserObservation["dom"]

export function collectDomFacts(document: Document, pageUrl: string): DomFacts {
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
    }
    const view = document.defaultView
    const computed = typeof view?.getComputedStyle === "function"
      ? view.getComputedStyle(element)
      : null
    return computed?.display !== "none" && computed?.visibility !== "hidden" &&
      computed?.visibility !== "collapse" && computed?.opacity !== "0"
  }

  function promptUsable(element: Element): boolean {
    if (!visible(element)) return false
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
    const attributeTitle = post.getAttribute("post-title")?.trim() || ""
    const attributePermalink = post.getAttribute("permalink")
    if (attributeTitle && validRedditPermalink(attributePermalink)) {
      publicPostTitlePermalinkCount += 1
      continue
    }
    const links = Array.from(post.querySelectorAll("a[href]"))
    if (
      links.some((link) => {
        if (!visible(link)) return false
        if (!validRedditPermalink(link.getAttribute("href"))) return false
        const heading = link.matches("h1 a,h2 a,h3 a")
          ? link
          : link.querySelector("h1,h2,h3,[slot=title],[data-testid*=title]")
        return Boolean(heading && visible(heading) && (heading.textContent || "").trim())
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
