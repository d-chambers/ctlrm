import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "../tests/web/browser",
  testMatch: "*.spec.mjs",
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 60_000,
  reporter: "list",
  use: {
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
