import { domFactsScript } from "../src/dom_facts.ts"

const CONFIG_PATH = decodeURIComponent(new URL("../agent-browser.json", import.meta.url).pathname)

async function command(session: string, args: string[]): Promise<Record<string, unknown>> {
  const child = new Deno.Command("agent-browser", {
    args: [
      "--session",
      session,
      "--config",
      CONFIG_PATH,
      "--json",
      "--allowed-domains",
      "127.0.0.1",
      ...args,
    ],
    stdout: "piped",
    stderr: "piped",
  }).spawn()
  const timeout = setTimeout(() => {
    try {
      child.kill("SIGKILL")
    } catch {
      // The command completed between the timeout and kill.
    }
  }, 20_000)
  const output = await child.output()
  clearTimeout(timeout)
  const stdout = new TextDecoder().decode(output.stdout)
  const stderr = new TextDecoder().decode(output.stderr)
  if (!output.success) throw new Error((stderr || stdout).slice(-2_000))
  const envelope = JSON.parse(stdout)
  if (!envelope.success) throw new Error(JSON.stringify(envelope.error))
  return envelope.data
}

Deno.test("Chromium rejects stylesheet-hidden and attribute-only capability shells", async () => {
  const html = await Deno.readTextFile(
    new URL("../fixtures/dom-layout.html", import.meta.url),
  )
  const abort = new AbortController()
  const server = Deno.serve(
    {
      hostname: "127.0.0.1",
      port: 0,
      signal: abort.signal,
      onListen() {},
    },
    () => new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } }),
  )
  const session = `cfwarp-dom-${crypto.randomUUID()}`
  try {
    await command(session, ["open", `http://127.0.0.1:${server.addr.port}/`])
    const geminiData = await command(session, [
      "eval",
      domFactsScript("https://gemini.google.com/app"),
    ])
    const geminiFacts = geminiData.result as Record<string, number>
    if (geminiFacts.promptControlCount !== 1) {
      throw new Error(`Chromium Gemini facts: ${JSON.stringify(geminiFacts)}`)
    }
    const redditData = await command(session, [
      "eval",
      domFactsScript("https://www.reddit.com/r/popular/"),
    ])
    const redditFacts = redditData.result as Record<string, number>
    if (redditFacts.publicPostTitlePermalinkCount !== 1) {
      throw new Error(`Chromium Reddit facts: ${JSON.stringify(redditFacts)}`)
    }
    const counterexampleData = await command(session, [
      "eval",
      `(() => {
        const anchor = document.querySelector('#hidden-descendant-title');
        const rect = anchor.getBoundingClientRect();
        return {
          positiveLayout: rect.width > 0 && rect.height > 0,
          rawText: anchor.textContent.trim(),
          renderedText: anchor.innerText.trim(),
        };
      })()`,
    ])
    const counterexample = counterexampleData.result as Record<string, unknown>
    if (
      counterexample.positiveLayout !== true ||
      counterexample.rawText !== "Hidden descendant only" ||
      counterexample.renderedText !== ""
    ) {
      throw new Error(`invalid Chromium counterexample: ${JSON.stringify(counterexample)}`)
    }
  } finally {
    await command(session, ["close"]).catch(() => undefined)
    abort.abort()
    await server.finished.catch(() => undefined)
  }
})
