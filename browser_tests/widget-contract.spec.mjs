import { expect, test } from "@playwright/test";

test("publishes demo contract version 1", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  await expect.poll(() => page.evaluate(() => window.demoContractVersion)).toBe(1);
});
