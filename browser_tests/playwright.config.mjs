import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  workers: 1,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  webServer: {
    command: "python3 -m http.server 4173 --directory .",
    cwd: process.cwd(),
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
  },
});
