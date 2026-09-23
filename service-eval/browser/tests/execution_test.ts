import { AgentBrowser } from "../src/agent_browser.ts"
import { main } from "../src/main.ts"

Deno.test("browser finalizes negative verdicts atomically in worker mode", async () => {
  const originalCreate = AgentBrowser.create
  const originalRename = Deno.rename
  const output = await Deno.makeTempDir()
  let publications = 0
  try {
    AgentBrowser.create = () =>
      Promise.resolve({
        version: "offline-test",
        checkTrace: () =>
          Promise.resolve({
            ok: false,
            warp: "off",
            loc: null,
            colo: null,
            ip: null,
            httpStatus: 200,
          }),
        close: () => Promise.resolve(),
      } as unknown as AgentBrowser)
    Deno.rename = async (source, target) => {
      const summary = JSON.parse(await Deno.readTextFile(source))
      if (target === `${output}/summary.json`) {
        if (summary.observation.result.class !== "tunnel_failure") {
          throw new Error("unfinalized summary at publication")
        }
        try {
          await Deno.stat(target)
          throw new Error("summary became visible before rename")
        } catch (error) {
          if (!(error instanceof Deno.errors.NotFound)) throw error
        }
        publications++
      }
      await originalRename(source, target)
    }

    const args = ["--service", "chatgpt", "--output", output]
    const status = await main([...args, "--worker-mode", "true"])
    if (status !== 0 || publications !== 1) throw new Error("worker did not finalize")
    const summary = JSON.parse(await Deno.readTextFile(`${output}/summary.json`))
    if (summary.observation.result.availability !== "unavailable") {
      throw new Error("worker changed the negative classification")
    }
    for await (const item of Deno.readDir(output)) {
      if (item.name.startsWith(".summary-")) throw new Error("temporary summary remains")
    }

    const operatorOutput = await Deno.makeTempDir()
    try {
      const operatorStatus = await main(["--service", "chatgpt", "--output", operatorOutput])
      if (operatorStatus !== 2) throw new Error("operator exit code changed")
    } finally {
      await Deno.remove(operatorOutput, { recursive: true })
    }
  } finally {
    AgentBrowser.create = originalCreate
    Deno.rename = originalRename
    await Deno.remove(output, { recursive: true })
  }
})

Deno.test("browser worker mode keeps finalized tooling failure unknown", async () => {
  const originalCreate = AgentBrowser.create
  const output = await Deno.makeTempDir()
  try {
    AgentBrowser.create = () => Promise.reject(new Error("offline simulated startup failure"))
    const status = await main([
      "--service",
      "chatgpt",
      "--output",
      output,
      "--worker-mode",
      "true",
    ])
    const summary = JSON.parse(await Deno.readTextFile(`${output}/summary.json`))
    if (
      status !== 0 || summary.verdict !== "tooling_failure" ||
      summary.observation.result.availability !== "unknown" ||
      summary.observation.result.eligible !== false
    ) throw new Error("worker did not preserve unknown/ineligible")
  } finally {
    AgentBrowser.create = originalCreate
    await Deno.remove(output, { recursive: true })
  }
})

Deno.test("browser does not recover a one-shot summary or verdict publication failure", async () => {
  const originalCreate = AgentBrowser.create
  const originalRename = Deno.rename
  const originalWrite = Deno.writeTextFile
  try {
    AgentBrowser.create = () =>
      Promise.resolve({
        version: "offline-test",
        checkTrace: () =>
          Promise.resolve({
            ok: false,
            warp: "off",
            loc: null,
            colo: null,
            ip: null,
            httpStatus: 200,
          }),
        close: () => Promise.resolve(),
      } as unknown as AgentBrowser)

    for (const failure of ["summary", "verdict"]) {
      const output = await Deno.makeTempDir()
      let publications = 0
      let verdictWrites = 0
      try {
        Deno.rename = async (source, target) => {
          publications++
          if (failure === "summary" && publications === 1) {
            throw new Error("one-shot summary publication failure")
          }
          await originalRename(source, target)
        }
        Deno.writeTextFile = async (...args) => {
          if (
            failure === "verdict" && args[0] === `${output}/verdict.txt` &&
            ++verdictWrites === 1
          ) {
            throw new Error("one-shot verdict publication failure")
          }
          await originalWrite(...args)
        }
        let rejected = false
        try {
          await main(["--service", "chatgpt", "--output", output, "--worker-mode", "true"])
        } catch (error) {
          rejected = String(error).includes("one-shot")
        }
        if (!rejected || publications !== 1) throw new Error("finalization failure was recovered")
        if (failure === "verdict") {
          const summary = JSON.parse(await Deno.readTextFile(`${output}/summary.json`))
          if (summary.verdict !== "tunnel_failure") throw new Error("published summary changed")
        } else {
          try {
            await Deno.stat(`${output}/summary.json`)
            throw new Error("failed publication became visible")
          } catch (error) {
            if (!(error instanceof Deno.errors.NotFound)) throw error
          }
        }
      } finally {
        Deno.rename = originalRename
        Deno.writeTextFile = originalWrite
        await Deno.remove(output, { recursive: true })
      }
    }
  } finally {
    AgentBrowser.create = originalCreate
  }
})
