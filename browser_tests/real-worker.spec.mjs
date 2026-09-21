import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial", timeout: 180000 });

async function installTrackingWorker(page) {
  await page.addInitScript(() => {
    const NativeWorker = window.Worker;
    const TrackingWorker = new Proxy(NativeWorker, {
      construct(Target, args) {
        const worker = Reflect.construct(Target, args);
        const record = { posts: [], responses: [], terminated: false };
        window.__realWorkers.push(record);
        const nativePost = worker.postMessage.bind(worker);
        const nativeTerminate = worker.terminate.bind(worker);
        record.releaseHeld = () => {
          const held = record.held;
          record.held = null;
          if (held) nativePost(held);
        };
        worker.postMessage = (message, ...rest) => {
          record.posts.push(structuredClone(message));
          if (window.__holdNextFeed && message.type === "feed") {
            window.__holdNextFeed = false;
            record.held = structuredClone(message);
            return;
          }
          nativePost(message, ...rest);
        };
        worker.terminate = () => {
          record.terminated = true;
          nativeTerminate();
        };
        worker.addEventListener("message", (event) => {
          record.responses.push(structuredClone(event.data));
        });
        return worker;
      },
    });
    window.__realWorkers = [];
    Object.defineProperty(window, "Worker", { value: TrackingWorker, configurable: true });
  });
}

async function openRealDemo(page) {
  await installTrackingWorker(page);
  await page.goto("/docs/index.html");
  await page.waitForFunction(() => window.demo?.ready === true, null, { timeout: 120000 });
}

async function menuActionId(page, labelPattern) {
  const actions = await page.evaluate(() => {
    const boot = window.__realWorkers[0].responses.find((message) => message.type === "boot");
    const flattened = [];
    const visit = (items) => {
      for (const item of items) {
        if (item.t === "menu") visit(item.items);
        else if (item.t !== "sep") flattened.push({ id: item.id, label: item.label });
      }
    };
    visit(boot.payload.menu);
    return flattened;
  });
  return actions.find((item) => labelPattern.test(item.label))?.id || null;
}

async function feedResponseCount(page) {
  return page.evaluate(() => window.__realWorkers[0].responses
    .filter((message) => message.type === "feed").length);
}

async function waitForFeedResponses(page, count) {
  await expect.poll(() => feedResponseCount(page), { timeout: 120000 }).toBe(count);
}

test("invalid public input keeps the real Worker request sequence coherent", async ({ page }) => {
  await openRealDemo(page);
  const result = await page.evaluate(async () => {
    let invalidCode;
    try {
      await window.demo.request("state", { action: "unsupported" });
    } catch (error) {
      invalidCode = error.code;
    }
    let validCode = "ok";
    try {
      await window.demo.request("menu", {});
    } catch (error) {
      validCode = error.code;
    }
    return {
      invalidCode,
      validCode,
      posts: window.__realWorkers[0].posts.map((message) => ({
        requestId: message.request_id,
        type: message.type,
      })),
    };
  });
  expect(result).toEqual({
    invalidCode: "invalid-message",
    validCode: "ok",
    posts: [
      { requestId: 1, type: "boot" },
      { requestId: 2, type: "menu" },
    ],
  });
});

test("real Worker replays an acknowledged contract error before Cancel", async ({ page }) => {
  await openRealDemo(page);
  await page.evaluate(() => window.demo.runFlow("m22"));
  const rootId = await page.locator("#dbody > [data-wid]").getAttribute("data-wid");
  const rejected = await page.evaluate((id) =>
    window.demo.dispatch([{ type: "activate", id }]), rootId);
  expect(rejected.response.status).toBe("contract-error");
  const rejectedRevision = rejected.response.render_revision;

  const cancelId = await page.getByRole("button", { name: "Cancel" }).getAttribute("id");
  await page.clock.install();
  await page.evaluate((id) => {
    window.__holdNextFeed = true;
    window.__cancelRequest = window.demo.dispatch([{ type: "activate", id }]);
  }, cancelId);
  await page.clock.runFor(5000);
  await expect.poll(() => page.evaluate(() => window.__realWorkers.length), {
    timeout: 120000,
  }).toBe(2);
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[1].responses.some((message) => message.type === "reset")), {
    timeout: 120000,
  }).toBe(true);

  const recovery = await page.evaluate(() => {
    const reset = window.__realWorkers[1].posts.find((message) => message.type === "reset");
    const response = window.__realWorkers[1].responses.find((message) => message.type === "reset");
    return {
      oldTerminated: window.__realWorkers[0].terminated,
      recovery: reset.payload.recovery,
      status: response.payload.response.status,
      overlay: document.querySelector("#overlay").classList.contains("show"),
    };
  });
  expect(recovery.oldTerminated).toBe(true);
  expect(recovery.recovery.map((entry) => entry.type)).toEqual(["start", "feed"]);
  expect(recovery.recovery[1].result.response).toMatchObject({
    status: "contract-error",
    render_revision: rejectedRevision,
  });
  expect(recovery.status).toBe("done");
  expect(recovery.overlay).toBe(false);
});

test("real Escape uses cancel recovery while its Worker request is delayed", async ({ page }) => {
  await openRealDemo(page);
  await page.evaluate(() => window.demo.runFlow("m22"));
  await page.clock.install();
  await page.evaluate(() => {
    window.__holdNextFeed = true;
    document.querySelector("#dbody > [data-wid]").dispatchEvent(new KeyboardEvent(
      "keydown", { key: "Escape", bubbles: true },
    ));
  });
  await page.clock.runFor(5000);
  await expect.poll(() => page.evaluate(() => window.__realWorkers.length), {
    timeout: 120000,
  }).toBe(2);
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[1].responses.some((message) => message.type === "reset")), {
    timeout: 120000,
  }).toBe(true);
  const result = await page.evaluate(() => {
    const held = window.__realWorkers[0].held;
    const reset = window.__realWorkers[1].responses.find((message) => message.type === "reset");
    return {
      action: held.payload.envelope.actions.at(-1),
      status: reset.payload.response.status,
      overlay: document.querySelector("#overlay").classList.contains("show"),
    };
  });
  expect(result.action).toMatchObject({ type: "key", key: "Escape", modifiers: [] });
  expect(result.action.id).toEqual(expect.any(String));
  expect(result).toMatchObject({ status: "done", overlay: false });
});

test("real Worker recovers Cancel queued behind an acknowledged response", async ({ page }) => {
  await openRealDemo(page);
  const settingsId = await menuActionId(page, /^Settings$/);
  expect(settingsId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), settingsId);
  const rootId = await page.locator("#dbody > [data-wid]").getAttribute("data-wid");
  await page.evaluate((id) => {
    window.__holdNextFeed = true;
    window.__priorRequest = window.demo.dispatch([{ type: "activate", id }]);
    document.querySelector("#dbody > [data-wid]").dispatchEvent(new KeyboardEvent(
      "keydown", { key: "Escape", bubbles: true },
    ));
    window.__realWorkers[0].releaseHeld();
  }, rootId);

  await expect.poll(() => page.evaluate(() => window.__realWorkers.length), {
    timeout: 10000,
  }).toBe(2);
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[1].responses.some((message) => message.type === "reset")), {
    timeout: 120000,
  }).toBe(true);
  const result = await page.evaluate(() => {
    const resetRequest = window.__realWorkers[1].posts.find((message) => message.type === "reset");
    const resetResponse = window.__realWorkers[1].responses.find((message) =>
      message.type === "reset");
    return {
      terminated: window.__realWorkers[0].terminated,
      recoveryStatus: resetRequest.payload.recovery.at(-1).result.response.status,
      cancel: resetRequest.payload.cancel.envelope.actions,
      status: resetResponse.payload.response.status,
      overlay: document.querySelector("#overlay").classList.contains("show"),
    };
  });
  expect(result).toMatchObject({
    terminated: true,
    recoveryStatus: "contract-error",
    cancel: [expect.objectContaining({ type: "key", key: "Escape", modifiers: [] })],
    status: "done",
    overlay: false,
  });
});

test("real Settings spin input keeps focus after edit acknowledgement", async ({ page }) => {
  await openRealDemo(page);
  const settingsId = await menuActionId(page, /^Settings$/);
  expect(settingsId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), settingsId);
  const spin = page.locator("#dbody input[type=number]").first();
  await expect(spin).toBeVisible();
  const bounds = await spin.evaluate((element) => ({
    minimum: Number(element.min),
    maximum: Number(element.max),
    step: Number(element.step) || 1,
  }));
  const firstValue = Math.min(bounds.maximum, bounds.minimum + bounds.step);
  const feedCount = await page.evaluate(() =>
    window.__realWorkers[0].responses.filter((message) => message.type === "feed").length);
  await spin.fill(String(firstValue));
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[0].responses.filter((message) => message.type === "feed").length), {
    timeout: 120000,
  }).toBe(feedCount + 1);

  await expect(spin).toBeFocused();
  expect(await spin.getAttribute("data-demo-part")).toBe("editor");
  expect(await spin.evaluate((element) => element.selectionStart)).toBeNull();
  const nextValue = firstValue + bounds.step <= bounds.maximum
    ? firstValue + bounds.step : firstValue - bounds.step;
  await page.keyboard.press(nextValue > firstValue ? "ArrowUp" : "ArrowDown");
  await expect(spin).toHaveValue(String(nextValue));
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[0].responses.filter((message) => message.type === "feed").length), {
    timeout: 120000,
  }).toBe(feedCount + 2);
  const lastAction = await page.evaluate(() =>
    window.__realWorkers[0].posts.filter((message) => message.type === "feed")
      .at(-1).payload.envelope.actions[0]);
  expect(lastAction).toMatchObject({
    type: "edit-text",
    value: String(nextValue),
  });
});

test("real Settings saves the final spin edit queued behind an acknowledgement", async ({ page }) => {
  await openRealDemo(page);
  const settingsId = await menuActionId(page, /^Settings$/);
  expect(settingsId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), settingsId);
  const checkbox = page.getByRole("checkbox", {
    name: "Sync decks automatically when updates are available",
  });
  const spin = page.locator("#dbody input[type=number]").first();
  const save = page.getByRole("button", { name: "Save", exact: true });
  await expect(spin).toBeVisible();
  if (!await checkbox.isChecked()) {
    const responseCount = await page.evaluate(() =>
      window.__realWorkers[0].responses.filter((message) => message.type === "feed").length);
    await checkbox.check();
    await expect.poll(() => page.evaluate(() =>
      window.__realWorkers[0].responses.filter((message) => message.type === "feed").length), {
      timeout: 120000,
    }).toBe(responseCount + 1);
  }
  await expect(spin).toBeEnabled();
  const feedStart = await page.evaluate(() =>
    window.__realWorkers[0].posts.filter((message) => message.type === "feed").length);
  const saveId = await save.getAttribute("id");

  await page.evaluate(() => { window.__holdNextFeed = true; });
  await spin.fill("10");
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[0].held?.payload.envelope.actions.some((action) =>
      action.type === "edit-text" && action.value === "10") || false)).toBe(true);
  await spin.fill("11");
  await page.waitForTimeout(150);
  await expect(spin).toHaveValue("11");
  await save.click();
  await page.evaluate(() => window.__realWorkers[0].releaseHeld());

  await expect(page.locator("#dbody")).toContainText("Settings saved", { timeout: 10000 });
  const result = await page.evaluate(({ feedStart, saveId }) => {
    const requests = window.__realWorkers[0].posts.filter((message) =>
      message.type === "feed").slice(feedStart);
    const responses = window.__realWorkers[0].responses.filter((message) =>
      message.type === "feed");
    return {
      actions: requests.flatMap((message) => message.payload.envelope.actions),
      savedInterval: responses.at(-1).payload.config.interval,
      saveId,
    };
  }, { feedStart, saveId });
  expect(result.actions).toContainEqual(expect.objectContaining({
    type: "edit-text", value: "10",
  }));
  expect(result.actions).toContainEqual(expect.objectContaining({
    type: "edit-text", value: "11",
  }));
  expect(result.actions).toContainEqual({ type: "activate", id: result.saveId });
  expect(result.savedInterval).toBe(11);
  await page.getByRole("button", { name: "OK" }).click();
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);
});

test("real editable Enter finishes and activates the schema default", async ({ page }) => {
  await openRealDemo(page);
  const flowId = await menuActionId(page, /^Manage /);
  expect(flowId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), flowId);
  const field = page.locator("#dbody input[type=text]").first();
  await expect(field).toBeVisible();
  const feedStart = await page.evaluate(() =>
    window.__realWorkers[0].posts.filter((message) => message.type === "feed").length);
  await field.fill(`${await field.inputValue()} sample`);
  await field.press("Enter");
  await expect.poll(() => page.evaluate((start) => {
    const request = window.__realWorkers[0].posts
      .filter((message) => message.type === "feed").slice(start)
      .find((message) => {
        const types = message.payload.envelope.actions.map((action) => action.type);
        return types.includes("edit-text") && types.includes("finish-edit")
          && types.includes("activate");
      });
    return Boolean(request && window.__realWorkers[0].responses.some((message) =>
      message.type === "feed" && message.request_id === request.request_id));
  }, feedStart), {
    timeout: 120000,
  }).toBe(true);
  const result = await page.evaluate((start) => {
    const request = window.__realWorkers[0].posts
      .filter((message) => message.type === "feed").slice(start)
      .find((message) => {
        const types = message.payload.envelope.actions.map((action) => action.type);
        return types.includes("edit-text") && types.includes("finish-edit")
          && types.includes("activate");
      });
    const response = window.__realWorkers[0].responses.find((message) =>
      message.type === "feed" && message.request_id === request.request_id);
    return {
      actions: request.payload.envelope.actions,
      status: response.payload.response.status,
    };
  }, feedStart);
  expect(result.actions.map((action) => action.type)).toEqual([
    "edit-text", "finish-edit", "activate",
  ]);
  expect(result.actions.some((action) => action.type === "key")).toBe(false);
  expect(result.status).not.toBe("contract-error");
});

test("real Manage keeps a newer edit unfinished after an older finish acknowledgement", async ({ page }) => {
  await openRealDemo(page);
  const flowId = await menuActionId(page, /^Manage /);
  expect(flowId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), flowId);
  const field = page.locator("#dbody input[type=text]").first();
  await expect(field).toBeVisible();
  const feedStart = await page.evaluate(() =>
    window.__realWorkers[0].posts.filter((message) => message.type === "feed").length);

  await page.evaluate(() => { window.__holdNextFeed = true; });
  await field.fill("First");
  await page.getByRole("button", { name: "Cancel" }).focus();
  await expect.poll(() => page.evaluate(() => Boolean(window.__realWorkers[0].held))).toBe(true);
  await field.focus();
  await field.fill("Second");
  await page.waitForTimeout(150);
  await page.evaluate(() => window.__realWorkers[0].releaseHeld());
  await expect.poll(() => page.evaluate((start) =>
    window.__realWorkers[0].responses.filter((message) => message.type === "feed").length >= start + 2,
  feedStart), { timeout: 120000 }).toBe(true);

  await page.getByRole("button", { name: "Cancel" }).focus();
  await expect.poll(() => page.evaluate((start) =>
    window.__realWorkers[0].posts.filter((message) => message.type === "feed").length >= start + 3,
  feedStart), { timeout: 10000 }).toBe(true);
  const requests = await page.evaluate((start) =>
    window.__realWorkers[0].posts.filter((message) => message.type === "feed")
      .slice(start).map((message) => message.payload.envelope.actions), feedStart);
  expect(requests[0]).toEqual([
    expect.objectContaining({ type: "edit-text", value: "First" }),
    expect.objectContaining({ type: "finish-edit" }),
  ]);
  expect(requests[1]).toEqual([
    expect.objectContaining({ type: "edit-text", value: "Second" }),
  ]);
  expect(requests[2]).toEqual([
    expect.objectContaining({ type: "finish-edit" }),
  ]);
  expect(requests.slice(1).flat().filter((action) => action.type === "finish-edit")).toHaveLength(1);
});

test("real Backup flow redacts virtual runtime paths", async ({ page }) => {
  await openRealDemo(page);
  const backupId = await menuActionId(page, /^Backup .+ deck$/);
  expect(backupId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), backupId);
  await expect(page.locator("#dbody")).toContainText("Backed up");
  const publicOutput = await page.evaluate(() => {
    const response = window.__realWorkers[0].responses.filter((message) =>
      message.type === "start").at(-1);
    return {
      body: document.querySelector("#dbody").textContent,
      payload: JSON.stringify(response.payload),
    };
  });
  expect(publicOutput.body).not.toMatch(/\/(?:app|source)\//);
  expect(publicOutput.payload).not.toMatch(/\/(?:app|source)\//);
});

test("real typed legacy dialogs retain picker choices Cancel and question text", async ({ page }) => {
  await openRealDemo(page);
  await page.evaluate(() => window.demo.runFlow("m13"));
  const picker = {
    choices: await page.locator("#dbody button").allTextContents(),
    cancel: await page.getByRole("button", { name: "Cancel" }).count(),
  };
  expect(picker.choices.some((label) => label.endsWith(".apkg"))).toBe(true);
  expect(picker.cancel).toBe(1);

  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);
  await page.evaluate(() => window.demo.runFlow("m16"));
  await expect(page.locator("#dbody")).toContainText("whole collection");
  await expect(page.getByRole("button", { name: "Choose a backup" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
});

test("a queued checkbox choice stays visible across an older Worker rerender", async ({ page }) => {
  await openRealDemo(page);
  await page.evaluate(async () => {
    await window.demo.request("maintainer", { operation: "fix" });
    await window.demo.request("maintainer", { operation: "restyle" });
  });
  const updateId = await menuActionId(page, /^Update my decks$/);
  await page.evaluate((id) => window.demo.runFlow(id), updateId);
  const decision = page.getByRole("button", {
    name: /^(?:Keep yours|Skip|Import|Apply):/,
  }).first();
  const look = page.getByRole("checkbox", {
    name: /Also apply the new card look/,
  });
  await expect(decision).toBeVisible();
  await expect(look).toBeVisible();
  const responses = await feedResponseCount(page);

  await page.evaluate(() => { window.__holdNextFeed = true; });
  await decision.click();
  await expect.poll(() => page.evaluate(() => Boolean(window.__realWorkers[0].held)))
    .toBe(true);
  await look.check();
  await expect(look).toBeChecked();
  await page.evaluate(() => {
    window.__holdNextFeed = true;
    window.__realWorkers[0].releaseHeld();
  });
  await waitForFeedResponses(page, responses + 1);

  await expect(look).toBeChecked();
  await expect.poll(() => page.evaluate(() => Boolean(window.__realWorkers[0].held)))
    .toBe(true);
  await page.evaluate(() => window.__realWorkers[0].releaseHeld());
  await waitForFeedResponses(page, responses + 2);
  await expect.poll(() => page.evaluate(() => window.demo.pendingChoices)).toBe(0);
  await expect(look).toBeChecked();
});

test("menu items activate with Enter and Space without reopening the menu", async ({ page }) => {
  await openRealDemo(page);
  const trigger = page.locator("#ipMenuBtn");

  for (const key of ["Enter", "Space"]) {
    await trigger.focus();
    await trigger.press("Enter");
    const settings = page.getByRole("button", { name: "Settings", exact: true });
    await expect(settings).toBeVisible();
    await settings.focus();
    await settings.press(key);
    await expect(page.getByRole("dialog", { name: "Settings" })).toBeVisible();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.locator("#overlay")).not.toHaveClass(/show/);
  }
});

test("Enter in the Settings interval editor activates the schema default", async ({ page }) => {
  await openRealDemo(page);
  const settingsId = await menuActionId(page, /^Settings$/);
  await page.evaluate((id) => window.demo.runFlow(id), settingsId);
  const checkbox = page.getByRole("checkbox", {
    name: "Sync decks automatically when updates are available",
  });
  if (!await checkbox.isChecked()) {
    const count = await feedResponseCount(page);
    await checkbox.check();
    await waitForFeedResponses(page, count + 1);
  }
  const interval = page.getByRole("spinbutton", { name: "Check every" });
  const responses = await feedResponseCount(page);
  await interval.fill("7");
  await interval.press("Enter");
  await expect(page.locator("#dbody")).toContainText("Settings saved", {
    timeout: 120000,
  });
  const result = await page.evaluate((start) => {
    const requests = window.__realWorkers[0].posts
      .filter((message) => message.type === "feed")
      .slice(start);
    const response = window.__realWorkers[0].responses
      .filter((message) => message.type === "feed").at(-1);
    return {
      actions: requests.flatMap((message) => message.payload.envelope.actions),
      interval: response.payload.config.interval,
    };
  }, responses);
  expect(result.actions).toContainEqual(expect.objectContaining({
    type: "edit-text", value: "7",
  }));
  expect(result.actions).toContainEqual(expect.objectContaining({ type: "activate" }));
  expect(result.interval).toBe(7);
});

test("modal focus wraps, blocks the background, cancels, and returns", async ({ page }) => {
  await openRealDemo(page);
  const trigger = page.locator("#ipMenuBtn");
  await trigger.focus();
  const settingsId = await menuActionId(page, /^Settings$/);
  await page.evaluate((id) => window.demo.runFlow(id), settingsId);
  const dialog = page.getByRole("dialog", { name: "Settings" });
  const first = dialog.getByRole("checkbox", {
    name: "Sync decks automatically when updates are available",
  });
  const last = dialog.getByRole("button", { name: "Cancel", exact: true });
  await first.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(last).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(first).toBeFocused();

  const background = page.locator("#deckarea .noteCell").first();
  await background.focus();
  await expect(dialog.locator(":focus")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);
  await expect(trigger).toBeFocused();
});

test("Escape cancels immediately behind a held valid checkbox action", async ({ page }) => {
  await openRealDemo(page);
  const settingsId = await menuActionId(page, /^Settings$/);
  await page.evaluate((id) => window.demo.runFlow(id), settingsId);
  const checkbox = page.getByRole("checkbox", {
    name: "Sync decks automatically when updates are available",
  });
  await page.evaluate(() => { window.__holdNextFeed = true; });
  await checkbox.press("Space");
  await expect.poll(() => page.evaluate(() => Boolean(window.__realWorkers[0].held)))
    .toBe(true);
  await checkbox.press("Escape");
  await page.evaluate(() => window.__realWorkers[0].releaseHeld());
  await expect.poll(() => page.evaluate(() => window.__realWorkers.length), {
    timeout: 120000,
  }).toBe(2);
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[1].responses.some((message) => message.type === "reset")), {
    timeout: 120000,
  }).toBe(true);
  await expect(page.locator("#overlay")).not.toHaveClass(/show/);
});
