import { parseHTML } from "linkedom"
import { collectDomFacts, domFactsScript, pageObservationScript } from "../src/dom_facts.ts"

async function fixture(name: string): Promise<Document> {
  const html = await Deno.readTextFile(new URL(`../fixtures/${name}`, import.meta.url))
  return parseHTML(html).document as unknown as Document
}

Deno.test("Gemini requires a visible enabled writable prompt control", async () => {
  const valid = collectDomFacts(
    await fixture("gemini-valid.html"),
    "https://gemini.google.com/app",
  )
  if (valid.promptControlCount !== 1) {
    throw new Error(`valid Gemini prompt count: ${valid.promptControlCount}`)
  }

  const invalid = collectDomFacts(
    await fixture("gemini-invalid-controls.html"),
    "https://gemini.google.com/app",
  )
  if (invalid.promptControlCount !== 0 || invalid.loginFormCount < 1) {
    throw new Error(`invalid Gemini DOM facts: ${JSON.stringify(invalid)}`)
  }
})

Deno.test("Reddit requires a nonempty title and same-site comments permalink", async () => {
  const valid = collectDomFacts(
    await fixture("reddit-valid.html"),
    "https://www.reddit.com/r/popular/",
  )
  if (valid.publicPostCount !== 1 || valid.publicPostTitlePermalinkCount !== 1) {
    throw new Error(`valid Reddit DOM facts: ${JSON.stringify(valid)}`)
  }

  const invalid = collectDomFacts(
    await fixture("reddit-invalid.html"),
    "https://www.reddit.com/r/popular/",
  )
  if (invalid.publicPostTitlePermalinkCount !== 0 || invalid.loginFormCount < 1) {
    throw new Error(`invalid Reddit DOM facts: ${JSON.stringify(invalid)}`)
  }
})

Deno.test("browser observation script remains executable JavaScript", () => {
  new Function(`return ${pageObservationScript()}`)
  new Function(`return ${domFactsScript("https://gemini.google.com/app")}`)
})
