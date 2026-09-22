// The AI wizard driven through the real Pyodide Worker.
//
// Supported surface in the browser: the wizard's setup page and the AI Backends
// window it opens (detection rows, ignore/prefer, model and effort dropdowns, the
// executable path field, Test connection). Generation itself is NOT supported and
// cannot be: it shells out to an assistant CLI, and there is no subprocess here.
// The limitation tests at the bottom pin that the demo says so rather than
// offering a path that dead-ends.
import { expect, test } from "@playwright/test";

test.setTimeout(180_000);
const BOOT_TIMEOUT = 120_000;

async function openDemo(page) {
  const failures = [];
  page.on("pageerror", (error) => failures.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(`console: ${message.text()}`);
  });
  await page.goto("/docs/index.html");
  await page.waitForFunction(() => window.demo?.ready === true, null,
    { timeout: BOOT_TIMEOUT });
  return failures;
}

// Every response rebuilds the tree and renumbers widget ids, so nothing may be
// held across a step: each of these re-resolves against the current tree.
async function tree(page) {
  return page.evaluate(async () => {
    const r = await window.demo.dispatch([]);
    return r?.response?.payload?.contract_tree ?? null;
  });
}

async function openWizard(page) {
  await page.evaluate(async () => {
    const menu = (await window.demo.request("menu", {})).menu;
    const flat = [];
    (function walk(items) {
      for (const item of items) {
        if (item.t === "menu") walk(item.items);
        else if (item.id) flat.push(item);
      }
    })(menu);
    await window.demo.runFlow(flat.find((i) => /Generate cards/i.test(i.label)).id);
  });
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.locator("#overlay .busyline")).toHaveCount(0, { timeout: 60_000 });
}

// Clicks the real rendered control, resolved by its current label.
async function clickLabelled(page, label) {
  const button = page.locator("#overlay button").filter({ hasText: label }).first();
  await expect(button).toBeEnabled();
  await button.click();
  await page.waitForTimeout(700);
}

async function openBackends(page) {
  await openWizard(page);
  await clickLabelled(page, "Set up an assistant");
  await expect(page.locator("#dtitle")).toHaveText(/AI Backends/);
}

function chipsOf(contract) {
  return contract.nodes
    .filter((n) => n.effective_visible && n.kind === "label"
      && /^(NOT FOUND|FOUND|IGNORED|PREFERRED|NOT RESPONDING)$/.test(n.text.trim()))
    .map((n) => n.text.trim());
}

test("the wizard opens on setup, because no assistant can be found here", async ({ page }) => {
  const failures = await openDemo(page);
  await openWizard(page);

  await expect(page.locator("#dtitle")).toHaveText(/Generate cards with AI/);
  await expect(page.locator("#dbody")).toContainText("Set up an AI assistant");
  // The wizard offers setup rather than a generate button it could not honour.
  const contract = await tree(page);
  const buttons = contract.nodes
    .filter((n) => n.kind === "button" && n.effective_visible).map((n) => n.text);
  expect(buttons).toEqual(["Set up an assistant", "Close"]);
  expect(failures).toEqual([]);
});

test("setup opens the AI Backends window with a row per assistant", async ({ page }) => {
  // This is the click that used to hang past the request watchdog and stop the
  // whole session: one combo option carried an id the protocol rejects.
  const failures = await openDemo(page);
  const started = Date.now();
  await openBackends(page);
  expect(Date.now() - started).toBeLessThan(60_000);

  const body = page.locator("#dbody");
  for (const name of ["Claude Code", "Codex CLI", "Antigravity CLI"]) {
    await expect(body).toContainText(name);
  }
  expect(chipsOf(await tree(page)).length).toBeGreaterThanOrEqual(3);
  expect(failures).toEqual([]);
});

test("ignoring an assistant marks its row and moves the preferred one", async ({ page }) => {
  await openDemo(page);
  await openBackends(page);

  const before = chipsOf(await tree(page));
  expect(before).toContain("PREFERRED");
  expect(before).not.toContain("IGNORED");

  await clickLabelled(page, "ignore");

  const after = chipsOf(await tree(page));
  expect(after).toContain("IGNORED");
  expect(after).toContain("PREFERRED");
  // The preference moved rather than being left on the ignored row.
  expect(after.indexOf("IGNORED")).toBeLessThan(after.indexOf("PREFERRED"));
});

test("the effort dropdown round trips, including its default row", async ({ page }) => {
  // The default row carries empty item data in Qt. Its protocol id is the thing
  // that was empty and unrepresentable, so both directions are pinned here.
  await openDemo(page);
  await openBackends(page);

  const effortOf = (contract) => contract.nodes.find((n) => n.kind === "combo"
    && n.effective_visible && n.options.some((o) => /Default/.test(o.label)));

  let effort = effortOf(await tree(page));
  expect(effort).toBeTruthy();
  expect(effort.options.every((o) => typeof o.id === "string" && o.id.length > 0)).toBe(true);
  expect(effort.current_text).toMatch(/Default/);

  const pick = async (optionId) => {
    await page.evaluate(([id, option]) => window.demo.dispatch([
      { type: "select-option", id, option_id: option }]), [effort.id, optionId]);
    await page.waitForTimeout(700);
    effort = effortOf(await tree(page));
  };

  await pick("high");
  expect(effort.current_text).toBe("high");

  const fallbackId = effort.options.find((o) => /Default/.test(o.label)).id;
  await pick(fallbackId);
  expect(effort.current_text).toMatch(/Default/);
});

test("closing AI Backends returns to the wizard's setup page", async ({ page }) => {
  await openDemo(page);
  await openBackends(page);

  const closes = page.locator("#overlay button").filter({ hasText: /^Close$/ });
  await closes.last().click();
  await page.waitForTimeout(1200);

  await expect(page.locator("#dtitle")).toHaveText(/Generate cards with AI/);
  const contract = await tree(page);
  const stack = contract.nodes.find((n) => n.kind === "stack");
  expect(stack.current_page).toBe(stack.pages[0]);
});

test("Test connection stays disabled while no assistant is runnable", async ({ page }) => {
  // The honest limitation: a path can be typed, but nothing here is executable,
  // so the demo leaves the control disabled instead of offering a test that could
  // only fail. This pins that limitation and that the DOM agrees with the tree; a
  // container re-enabling a disabled child is a different fault, covered by
  // widget-contract's "a container does not re-enable a control the protocol
  // disabled", which this case does not discriminate.
  await openDemo(page);
  await openBackends(page);

  const contract = await tree(page);
  const line = contract.nodes.find((n) => n.kind === "line" && n.effective_visible);
  expect(line).toBeTruthy();
  await page.evaluate((id) => window.demo.dispatch([{
    type: "edit-text", id, value: "/usr/local/bin/claude",
    selection_start: 21, selection_end: 21, composing: false }]), line.id);
  await page.waitForTimeout(900);

  const after = await tree(page);
  expect(after.nodes.find((n) => n.kind === "line" && n.effective_visible).value)
    .toBe("/usr/local/bin/claude");
  const testNode = after.nodes.find((n) => /^Test connection$/.test(n.text || ""));
  expect(testNode.effective_enabled).toBe(false);
  const testButton = page.locator("#overlay button").filter({ hasText: /^Test connection$/ }).first();
  await expect(testButton).toBeDisabled();
});
