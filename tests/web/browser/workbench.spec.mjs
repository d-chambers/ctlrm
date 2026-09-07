import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(
  new URL("../../../frontend/package.json", import.meta.url),
);
const { test, expect } = require("@playwright/test");
const python =
  process.env.CTLRM_BROWSER_PYTHON ||
  fileURLToPath(new URL("../../../.venv/bin/python", import.meta.url));

async function freePort() {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

test("installed CLI serves the workbench and retained task text", async ({
  page,
  request,
}) => {
  const directory = await mkdtemp(join(tmpdir(), "ctlrm-browser-"));
  let server;
  let exited;
  let output = "";
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    const fixture = JSON.parse(
      execFileSync(
        python,
        ["-I", fileURLToPath(new URL("./seed.py", import.meta.url)), directory],
        { encoding: "utf8", timeout: 30_000 },
      ),
    );
    const port = await freePort();
    // Run the installed CLI outside the source tree, with Python import isolation.
    server = spawn(
      python,
      [
        "-I",
        "-m",
        "ctlrm",
        "serve",
        "--data",
        fixture.data,
        "--port",
        String(port),
      ],
      { cwd: directory },
    );
    exited = new Promise((resolve) => {
      server.once("exit", resolve);
      server.once("error", (error) => {
        output += error.message;
        resolve();
      });
    });
    for (const stream of [server.stdout, server.stderr])
      stream.on("data", (chunk) => {
        output = (output + chunk).slice(-65536);
      });
    await expect
      .poll(
        () =>
          output.match(
            /Control room: (http:\/\/127\.0\.0\.1:\d+\/#token=\S+)/,
          )?.[1],
        { timeout: 15_000 },
      )
      .toBeTruthy();
    const url = new URL(
      output.match(/Control room: (http:\/\/127\.0\.0\.1:\d+\/#token=\S+)/)[1],
    );
    const token = new URLSearchParams(url.hash.slice(1)).get("token");
    const auth = { Authorization: `Bearer ${token}` };
    await expect
      .poll(
        async () => {
          try {
            return (await request.get(url.origin)).status();
          } catch {
            return 0;
          }
        },
        { timeout: 10_000 },
      )
      .toBe(200);

    await test.step("wheel contains assets and canonical templates; APIs require authentication", async () => {
      expect((await request.get(`${url.origin}/api/snapshot`)).status()).toBe(
        401,
      );
      for (const asset of [
        "app.js",
        "app.css",
        "vendor/xterm.js",
        "vendor/xterm.css",
        "vendor/addon-fit.js",
      ]) {
        const result = await request.get(`${url.origin}/static/${asset}`);
        expect(result.status(), asset).toBe(200);
        expect((await result.body()).length, asset).toBeGreaterThan(0);
      }
      const templates = await request.get(`${url.origin}/api/templates`, {
        headers: auth,
      });
      expect(templates.status()).toBe(200);
      expect((await templates.json()).map((item) => item.name)).toEqual(
        expect.arrayContaining(["ship", "single-agent", "implement-review"]),
      );
    });

    await page.clock.install();
    await page.goto(url.toString());
    await page.locator('[data-action="open-project"]').first().click();
    await test.step("project and real workflow are rendered", async () => {
      await expect(page.locator("#main")).toContainText("Browser acceptance");
      await expect(
        page.locator('[data-action="task"][data-task="implement"]'),
      ).toBeVisible();
      expect(
        await page.locator(".workflow-node").count(),
      ).toBeGreaterThanOrEqual(3);
      expect(new URL(page.url()).hash).toBe("");
    });
    await page.locator('[data-action="task"][data-task="implement"]').click();
    const row = (kind) =>
      page.locator(`.task-artifact-row[data-kind="${kind}"]`).first();
    const editor = page.locator("#artifact-editor");
    const text = page.locator("#artifact-editor-text");
    await expect(row("commit")).toBeVisible();
    await expect(row("message")).toBeVisible();
    await expect(row("report")).toBeVisible();
    await test.step("double click opens retained commit text and restores task focus", async () => {
      await row("commit").dblclick();
      await expect(editor).toBeVisible();
      await expect(text).toHaveJSProperty("readOnly", true);
      await expect(text).toHaveValue(/Keep execution histories separate\./);
      await expect(text).toHaveValue(/\+implemented/);
      await page.locator("#artifact-editor-close").click();
      await expect(page.locator("#dialog")).toBeVisible();
      await expect(row("commit")).toBeFocused();
    });
    await test.step("messages remain literal text and Escape preserves the task", async () => {
      await row("message").dblclick();
      await expect(text).toHaveValue(/To: coordinator/);
      await expect(text).toHaveValue(/<script>window.injection=true<\/script>/);
      expect(await page.evaluate(() => window.injection)).toBeUndefined();
      expect(
        await text.evaluate((element) =>
          parseFloat(getComputedStyle(element).fontSize),
        ),
      ).toBeGreaterThanOrEqual(16);
      await page.keyboard.press("Escape");
      await expect(editor).not.toBeVisible();
      await expect(page.locator("#dialog")).toBeVisible();
      await expect(row("message")).toBeFocused();
    });
    await test.step("Enter opens the accepted outcome report", async () => {
      await row("report").focus();
      await row("report").press("Enter");
      await expect(text).toHaveValue(/Outcome: done/);
      await expect(text).toHaveValue(/Ready for the next reviewer\./);
      await page.locator("#artifact-editor-close").click();
      await page.locator('[data-action="close-dialog"]').click();
    });
    await test.step("project design, code, and artifacts remain navigable", async () => {
      await page.locator('[data-action="view"][data-view="design"]').click();
      await expect(page.locator("#view-content")).toContainText(
        "Keep task evidence readable.",
      );
      await page.locator('[data-action="view"][data-view="code"]').click();
      await expect(page.locator("#code-text")).toContainText("implemented");
      await page.locator('[data-action="view"][data-view="artifacts"]').click();
      await expect(page.locator("#view-content")).toContainText(
        "Project artifacts",
      );
    });
    await test.step("hidden tabs stop polling and refresh when shown", async () => {
      await page.clock.pauseAt(new Date(Date.now() + 1000));
      let snapshots = 0;
      page.on("request", (request) => {
        if (request.url() === `${url.origin}/api/snapshot`) snapshots++;
      });
      const polled = page.waitForResponse(`${url.origin}/api/snapshot`);
      await page.clock.runFor(2600);
      await (await polled).finished();
      expect(snapshots).toBeGreaterThan(0);
      await page.evaluate(() => {
        Object.defineProperty(document, "hidden", {
          configurable: true,
          value: true,
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      const before = snapshots;
      await page.clock.runFor(6000);
      expect(snapshots).toBe(before);
      await page.evaluate(() => {
        Object.defineProperty(document, "hidden", {
          configurable: true,
          value: false,
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await expect.poll(() => snapshots).toBeGreaterThan(before);
    });
    expect(errors).toEqual([]);
  } finally {
    await page.close();
    if (server && server.exitCode === null && server.signalCode === null) {
      server.kill("SIGTERM");
      const timer = setTimeout(() => server.kill("SIGKILL"), 5000);
      await exited;
      clearTimeout(timer);
    }
    await rm(directory, { recursive: true, force: true });
  }
});
