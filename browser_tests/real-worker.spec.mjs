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
