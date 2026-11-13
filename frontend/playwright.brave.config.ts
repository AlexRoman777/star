import { defineConfig, devices } from "@playwright/test";

const braveExecutable = process.env.STAR_BRAVE_EXECUTABLE
  ?? "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser";
const sourceMode = process.env.STAR_PLAYWRIGHT_SOURCE === "1";
const testPort = sourceMode ? 4174 : 4173;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results/playwright-brave",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  reporter: [["line"]],
  snapshotPathTemplate: "{testDir}/__screenshots__/{projectName}/{arg}{ext}",
  use: {
    baseURL: `http://127.0.0.1:${testPort}`,
    browserName: "chromium",
    colorScheme: "dark",
    locale: "en-US",
    timezoneId: "Europe/Stockholm",
    launchOptions: { executablePath: braveExecutable },
    // Visual baselines are taken explicitly. Automatic failure artifacts stay off so
    // an opt-in local private-corpus check can never copy family media into a report.
    screenshot: "off",
    trace: "off",
  },
  webServer: {
    command: sourceMode
      ? `./node_modules/.bin/vite --host 127.0.0.1 --port ${testPort}`
      : `./node_modules/.bin/vite preview --host 127.0.0.1 --port ${testPort}`,
    url: `http://127.0.0.1:${testPort}`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [
    {
      name: "ultrawide-3440-brave",
      use: {
        ...devices["NVIDIA Chrome"],
        viewport: { width: 3440, height: 1440 },
        screen: { width: 3440, height: 1440 },
        deviceScaleFactor: 1,
      },
    },
    {
      name: "macbook-pro-16-brave",
      use: {
        ...devices["NVIDIA Chrome"],
        viewport: { width: 1728, height: 1117 },
        screen: { width: 1728, height: 1117 },
        deviceScaleFactor: 2,
      },
    },
    {
      name: "xiaomi-15-pro-brave",
      use: {
        ...devices["Pixel 7"],
        viewport: { width: 412, height: 915 },
        screen: { width: 412, height: 915 },
        deviceScaleFactor: 3.5,
        userAgent: "Mozilla/5.0 (Linux; Android 15; Xiaomi 15 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36",
      },
    },
  ],
});
