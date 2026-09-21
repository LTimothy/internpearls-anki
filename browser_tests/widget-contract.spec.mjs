import { expect, test } from "@playwright/test";

const NODE_KINDS = [
  "box", "button", "buttons", "check", "col", "combo", "form", "frame",
  "grid", "hline", "label", "line", "radio", "row", "scroll", "spacer",
  "spin", "stack", "textarea",
];

const ACTION_KINDS = [
  "activate", "toggle", "select-option", "edit-text", "finish-edit",
  "activate-link", "key", "scroll", "select-files", "advance", "close",
];

test("publishes the generated demo contract registries", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const contract = await page.evaluate(async () => {
    const registry = await import("/docs/demo-contract.js");
    return {
      schemaVersion: registry.SCHEMA_VERSION,
      nodeKinds: registry.NODE_KINDS,
      actionKinds: registry.ACTION_KINDS,
      requiredFields: registry.REQUIRED_FIELDS,
    };
  });
  expect(contract.schemaVersion).toBe(1);
  expect(contract.nodeKinds).toEqual(NODE_KINDS);
  expect(contract.actionKinds).toEqual(ACTION_KINDS);
  const schema = await (await page.request.get("/demo/schema-v1.json")).json();
  expect(contract.schemaVersion).toEqual(schema.schema_version);
  expect(contract.nodeKinds).toEqual(schema.node_kinds);
  expect(contract.actionKinds).toEqual(schema.action_kinds);
  expect(contract.requiredFields).toEqual({
    ...schema.required_fields,
    node_kinds: schema.node_fields,
  });
});
