import { expect, test } from "@playwright/test";

test.setTimeout(180_000);

const DIALOG_TIMEOUT = 60_000;

function watchFailures(page) {
  const failures = [];
  page.on("pageerror", (error) => failures.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(`console: ${message.text()}`);
  });
  return failures;
}

async function openDemo(page, width = 1280) {
  const failures = watchFailures(page);
  await page.setViewportSize({ width, height: 900 });
  await page.goto("/docs/index.html");
  await page.waitForFunction(() => window.demo?.ready === true, null, {
    timeout: 120_000,
  });
  await expect(page.locator("#deckarea tbody tr").first()).toBeVisible();
  return failures;
}

async function runMenuItem(page, label, submenu = null) {
  await page.locator("#ipMenuBtn").click();
  if (submenu) {
    await page.locator("#ipMenu .hasSub", { hasText: submenu }).click();
  }
  await page.getByRole("button", { name: label, exact: true }).click();
}

async function waitForDialog(page) {
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible({ timeout: DIALOG_TIMEOUT });
  await expect(dialog).toHaveAccessibleName(/\S/);
  await expect(dialog.locator(".busyline")).toHaveCount(0, {
    timeout: DIALOG_TIMEOUT,
  });
  return dialog;
}

async function maintain(page, operation) {
  return page.evaluate((name) => window.demo.request("maintainer", { operation: name }),
    operation);
}

async function shipVisibleChange(page, operation, resultText) {
  await page.locator(`[data-ship="${operation}"]`).click();
  await expect(page.locator("#shipped")).toContainText(resultText);
}

async function assertActionsReachable(page, expectOverflow,
  selector = "#overlay .dialog", expectLastVisible = true) {
  const surface = page.locator(selector);
  const result = await surface.evaluate((element) => {
    const buttons = Array.from(element.querySelectorAll("button:not([hidden])"))
      .filter((button) => !button.disabled);
    const last = buttons.at(-1);
    last?.scrollIntoView({ block: "end", inline: "nearest" });
    const rect = last?.getBoundingClientRect();
    return {
      overflow: element.scrollHeight > element.clientHeight,
      lastVisible: Boolean(rect && rect.bottom > 0 && rect.top < window.innerHeight
        && rect.left >= 0 && rect.right <= window.innerWidth),
    };
  });
  expect(result.overflow).toBe(expectOverflow);
  if (expectLastVisible) expect(result.lastVisible).toBe(true);
}

test("non-ai", async ({ page }) => {
  const failures = await openDemo(page, 1280);

  const initialCards = await page.locator("#deckarea tbody tr").count();
  expect(initialCards).toBeGreaterThan(0);
  await expect(page.locator("#deckarea .pill.interval").first()).toBeVisible();
  await expect(page.locator("#deckarea .noteCell").filter({ hasText: /\S/ }).first())
    .toBeVisible();

  await shipVisibleChange(page, "fix", "clarified");
  await shipVisibleChange(page, "reword", "reworded");
  await shipVisibleChange(page, "add", "added");
  await shipVisibleChange(page, "restyle", "restyled");

  await runMenuItem(page, "Update my decks");
  let dialog = await waitForDialog(page);
  await expect(dialog).toHaveAccessibleName(/Intern Pearls/);
  await expect(dialog).toContainText("This update also changes how some cards look");

  const cancelKeep = dialog.getByRole("button", { name: /^Keep yours:/ }).first();
  const cancelSkip = dialog.getByRole("button", { name: /^Skip:/ }).first();
  await cancelKeep.click();
  await expect(cancelKeep).toHaveAttribute("aria-pressed", "true");
  await cancelSkip.click();
  await expect(cancelSkip).toHaveAttribute("aria-pressed", "true");
  const feedback = dialog.locator('textarea[aria-label^="Feedback note:"]:visible')
    .first();
  await feedback.fill("Please clarify the generic example.");
  await page.waitForTimeout(800);
  await feedback.press("Escape");
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Nothing is sent automatically");
  await expect(dialog.locator("textarea")).toHaveValue(
    /Please clarify the generic example\./,
  );
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);

  await runMenuItem(page, "Update my decks");
  dialog = await waitForDialog(page);
  const acceptedKeep = dialog.getByRole("button", { name: /^Keep yours:/ }).first();
  const acceptedSkip = dialog.getByRole("button", { name: /^Skip:/ }).first();
  await acceptedKeep.click();
  await expect(acceptedKeep).toHaveAttribute("aria-pressed", "true");
  await acceptedSkip.click();
  await expect(acceptedSkip).toHaveAttribute("aria-pressed", "true");
  await dialog.locator('textarea[aria-label^="Feedback note:"]:visible').first()
    .fill("Please clarify the generic example.");
  await page.waitForTimeout(800);
  const acceptedImport = dialog.getByRole("button", { name: /^Import:/ }).first();
  await acceptedImport.click();
  await expect(acceptedImport).toHaveAttribute("aria-pressed", "true");
  const look = dialog.getByRole("checkbox", {
    name: /Also apply the new card look/,
  });
  await look.check();
  await expect(look).toBeChecked();
  await dialog.getByRole("button", { name: "Update", exact: true }).click();

  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Update complete");
  await expect(dialog).toContainText("Nothing is sent automatically");
  await expect(dialog.locator("textarea")).toHaveValue(
    /Please clarify the generic example\./,
  );
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);
  expect(await page.locator("#deckarea tbody tr").count()).toBeGreaterThanOrEqual(initialCards);
  await expect(page.locator("#deckarea")).toContainText(
    "Which route gets epinephrine working fastest",
  );

  await maintain(page, "history");
  await runMenuItem(page, "Update my decks");
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Changed since you kept yours");
  await expect(dialog).toContainText("coordinated revision");
  await expect(dialog).toContainText("RETIRED");
  await expect(dialog).toContainText("MOVED");
  await expect(dialog.getByRole("button", { name: /^Keep yours:/ }).first())
    .toHaveAttribute("aria-pressed", "true");
  const applyChanged = dialog.getByRole("button", { name: /^Apply:/ }).first();
  await applyChanged.click();
  await expect(applyChanged).toHaveAttribute("aria-pressed", "true");
  const historyLook = dialog.getByRole("checkbox", {
    name: /Also apply the new card look/,
  });
  await historyLook.check();
  await expect(historyLook).toBeChecked();
  await dialog.getByRole("button", { name: "Update", exact: true }).click();
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Archived");
  await expect(dialog).toContainText("Moved");
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);

  await runMenuItem(page, "Backup intern pearls deck", "Advanced");
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Backed up the Intern Pearls deck");
  await page.locator("#dbtns").getByRole("button", { name: "OK" }).click();
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);

  const noteCell = page.locator("#deckarea .noteCell").first();
  const savedNote = await noteCell.textContent();
  await noteCell.fill("Temporary session annotation");
  await noteCell.blur();
  await expect(noteCell).toHaveText("Temporary session annotation");

  await runMenuItem(page, "Restore intern pearls deck", "Advanced");
  dialog = await waitForDialog(page);
  await dialog.locator("button", { hasText: /\.apkg$/ }).first().click();
  dialog = await waitForDialog(page);
  await dialog.getByRole("button", { name: "Import", exact: true }).click();
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Imported");
  await page.locator("#dbtns").getByRole("button", { name: "OK" }).click();
  await expect(noteCell).toHaveText(savedNote || "");

  await runMenuItem(page, "Settings");
  dialog = await waitForDialog(page);
  const autoSync = dialog.getByRole("checkbox", {
    name: "Sync decks automatically when updates are available",
  });
  if (!(await autoSync.isChecked())) {
    await autoSync.press("Space");
    await expect(autoSync).toBeChecked();
    await page.waitForTimeout(500);
  }
  const interval = dialog.getByRole("spinbutton", { name: "Check every" });
  await interval.fill("1");
  await interval.press("Tab");
  await page.waitForTimeout(500);
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Deck sync checks every 1 minute");
  await page.locator("#dbtns").getByRole("button", { name: "OK" }).click();

  await maintain(page, "auto");
  await expect(page.locator("#toast")).toHaveClass(/show/, { timeout: 8_000 });
  await expect(page.locator("#toast")).toHaveAttribute("aria-live", "polite");
  await expect(page.locator("#toast")).toContainText(/auto-synced/i);

  await runMenuItem(page, "Check for add-on updates", "Advanced");
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("is up to date");
  await page.locator("#dbtns").getByRole("button", { name: "OK" }).click();

  expect(failures).toEqual([]);
});

test("keyboard", async ({ page }) => {
  const failures = await openDemo(page, 800);

  await runMenuItem(page, "Settings");
  let dialog = await waitForDialog(page);
  const autoSync = dialog.getByRole("checkbox", {
    name: "Sync decks automatically when updates are available",
  });
  await autoSync.focus();
  await autoSync.press("Space");
  const checked = await autoSync.isChecked();
  expect(checked).toBe(true);
  await autoSync.press("Tab");
  await expect(dialog.getByRole("spinbutton", { name: "Check every" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(autoSync).toBeFocused();
  await dialog.getByRole("button", { name: "Save", exact: true }).focus();
  await page.keyboard.press("Enter");
  dialog = await waitForDialog(page);
  await expect(dialog).toContainText("Settings saved");
  await page.locator("#dbtns").getByRole("button", { name: "OK" }).click();
  await expect(page.locator("#ipMenuBtn")).toBeFocused();

  await runMenuItem(page, "Night mode dimming", "Experimental");
  dialog = await waitForDialog(page);
  const enabled = dialog.getByRole("checkbox", { name: "Dim in Night Mode" });
  if (!(await enabled.isChecked())) {
    await enabled.press("Space");
    await expect(enabled).toBeChecked();
    await page.waitForTimeout(500);
  }
  const firstRadio = dialog.getByRole("radio", { name: "Bright images only" });
  await expect(firstRadio).toBeEnabled();
  await firstRadio.focus();
  await firstRadio.press("ArrowDown");
  await expect(dialog.getByRole("radio", {
    name: "Everything on cards and deck screens",
  })).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);
  await expect(page.locator("#ipMenuBtn")).toBeFocused();

  await runMenuItem(page, "Scan for duplicates", "Experimental");
  dialog = await waitForDialog(page);
  await expect(dialog).not.toContainText("Scanning...", { timeout: DIALOG_TIMEOUT });
  const combo = dialog.getByRole("combobox", { name: "Cards to scan for duplicates" });
  const optionCount = await combo.locator("option").count();
  expect(optionCount).toBeGreaterThan(1);
  const lastOption = await combo.locator("option").last().getAttribute("value");
  await combo.selectOption(lastOption);
  await expect(dialog).toContainText("Scanning...", { timeout: DIALOG_TIMEOUT });
  await expect(dialog).not.toContainText("Scanning...", { timeout: DIALOG_TIMEOUT });
  await expect(combo).toHaveValue(lastOption);
  await combo.focus();
  await combo.press("ArrowUp");
  await expect(dialog).toContainText("Scanning...", { timeout: DIALOG_TIMEOUT });
  await expect(dialog).not.toContainText("Scanning...", { timeout: DIALOG_TIMEOUT });
  await expect(combo).not.toHaveValue(lastOption);
  await page.keyboard.press("Escape");
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);

  await page.evaluate(() => { document.documentElement.style.zoom = "200%"; });
  await page.setViewportSize({ width: 800, height: 500 });
  await runMenuItem(page, "Settings");
  dialog = await waitForDialog(page);
  await assertActionsReachable(page, true, "#overlay .dialog", false);
  const zoomCancel = dialog.getByRole("button", { name: "Cancel", exact: true });
  await zoomCancel.focus();
  await expect(zoomCancel).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);

  await expect(page.locator("#toast")).toHaveAttribute("aria-live", "polite");
  expect(failures).toEqual([]);
});

test("lists", async ({ page }) => {
  const failures = await openDemo(page, 1280);
  await maintain(page, "bulk");
  await runMenuItem(page, "Update my decks");
  const dialog = await waitForDialog(page);
  const firstSkip = dialog.getByRole("button", { name: /^Skip: Bulk card 001$/ });
  await firstSkip.click();
  await expect(firstSkip).toHaveAttribute("aria-pressed", "true");

  const list = dialog.locator(".scrollbox").first();
  const listId = await list.getAttribute("data-wid");
  const counts = [await dialog.getByRole("button", { name: /^Import: Bulk card/ }).count()];
  for (let batch = 0; batch < 4; batch += 1) {
    await page.evaluate(({ id, offset }) => window.demo.dispatch([{
      type: "scroll", id, offset,
    }]), { id: listId, offset: batch + 1 });
    await expect.poll(async () => dialog.getByRole("button", {
      name: /^Import: Bulk card/,
    }).count()).toBeGreaterThan(counts.at(-1));
    counts.push(await dialog.getByRole("button", { name: /^Import: Bulk card/ }).count());
  }
  expect(counts).toHaveLength(5);
  await expect(dialog.getByRole("button", { name: /^Skip: Bulk card 001$/ }))
    .toHaveAttribute("aria-pressed", "true");
  await expect(dialog).toContainText("1 skipped for now");

  await page.setViewportSize({ width: 800, height: 900 });
  await assertActionsReachable(page, true, "#overlay .scrollbox");
  await dialog.getByRole("button", { name: "Update", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(dialog.getByRole("button", { name: "Update", exact: true })).toBeVisible();
  expect(failures).toEqual([]);
});
