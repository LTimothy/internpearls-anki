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

test("real editable Enter finishes without a contract error", async ({ page }) => {
  await openRealDemo(page);
  const flowId = await menuActionId(page, /^Manage /);
  expect(flowId).toEqual(expect.any(String));
  await page.evaluate((id) => window.demo.runFlow(id), flowId);
  const field = page.locator("#dbody input[type=text]").first();
  await expect(field).toBeVisible();
  const feedCount = await page.evaluate(() =>
    window.__realWorkers[0].responses.filter((message) => message.type === "feed").length);
  await page.clock.install();
  await field.fill(`${await field.inputValue()} sample`);
  await field.press("Enter");
  await expect.poll(() => page.evaluate(() =>
    window.__realWorkers[0].responses.filter((message) => message.type === "feed").length), {
    timeout: 120000,
  }).toBe(feedCount + 1);
  const result = await page.evaluate(() => {
    const request = window.__realWorkers[0].posts.filter((message) => message.type === "feed").at(-1);
    const response = window.__realWorkers[0].responses.filter((message) =>
      message.type === "feed").at(-1);
    return {
      actions: request.payload.envelope.actions,
      status: response.payload.response.status,
    };
  });
  expect(result.actions.map((action) => action.type)).toEqual(["edit-text", "finish-edit"]);
  expect(result.actions.some((action) => action.type === "key")).toBe(false);
  expect(result.status).not.toBe("contract-error");
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
