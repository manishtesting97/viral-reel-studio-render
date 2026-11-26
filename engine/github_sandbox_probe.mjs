/** Check the live Linux renderer sandbox without relying on Chrome's WebUI. */
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {readFileSync} from "node:fs";
import {networkInterfaces} from "node:os";
import {fileURLToPath} from "node:url";
import puppeteer from "puppeteer-core";

export function verifyRenderer(browser, renderer) {
  const field = (status, name) => {
    const value = status.match(new RegExp(`^${name}:[ \\t]*(.+)$`, "m"))?.[1].trim();
    assert(value, `Missing Linux process status field ${name}.`);
    return value;
  };
  assert(field(renderer, "NSpid").split(/\s+/).length > field(browser, "NSpid").split(/\s+/).length,
    "Chrome renderer has no inner PID namespace.");
  assert(field(renderer, "Seccomp") === "2" &&
    Number(field(renderer, "Seccomp_filters")) > Number(field(browser, "Seccomp_filters")),
    "Chrome renderer has no additional seccomp filter.");
}

async function main() {
  assert.deepEqual(Object.keys(networkInterfaces()), ["lo"], "Sandbox network is not loopback-only.");
  for (const name of ["GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ENV", "VRS_GITHUB_PRODUCTION_TOKEN"]) {
    assert(!(name in process.env), "Unexpected GitHub environment inside render sandbox.");
  }
  const browser = await puppeteer.launch({executablePath: "/tools/vrs-chrome", pipe: true, timeout: 30000});
  try {
    const page = await browser.newPage();
    await page.goto("data:text/html,<title>Sandbox check</title>");
    assert.equal(await page.evaluate(() => document.title), "Sandbox check");
    const session = await browser.target().createCDPSession();
    const {processInfo} = await session.send("SystemInfo.getProcessInfo");
    const browserPid = browser.process().pid;
    assert(processInfo.some(({type, id}) => type === "browser" && id === browserPid),
      "DevTools and procfs disagree on Chrome's process ID.");
    const renderers = processInfo.filter(({type}) => type === "renderer");
    assert(renderers.length > 0, "Chrome did not report a live renderer.");
    const browserStatus = readFileSync(`/proc/${browserPid}/status`, "utf8");
    for (const {id} of renderers) {
      assert(Number.isSafeInteger(id) && id > 0, "Invalid Chrome renderer PID.");
      verifyRenderer(browserStatus, readFileSync(`/proc/${id}/status`, "utf8"));
    }
  } finally {
    await browser.close();
  }
  execFileSync("/tools/ffmpeg", ["-version"]);
  console.log("Loopback-only network, clean environment, Chrome renderer PID namespace and extra seccomp filter verified.");
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await main();
}
