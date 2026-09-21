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

test("renders every generated node kind and rejects unknown kinds", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async () => {
    const contract = await import("/docs/demo-contract.js");
    const renderer = await import("/docs/demo-renderer.js");
    const common = {
      parent_id: null, children: [], visible: true, effective_visible: true,
      enabled: true, effective_enabled: true, accessible_name: "",
      accessible_description: "", tooltip: "", focus_policy: "none",
      readonly: false, style_roles: [], actions: [],
    };
    const fields = {
      box: { margins: {}, gap: 0, stretches: [], alignment: "start" },
      button: { text: "Button", checkable: false, checked: false, role: "other",
                default: false, escape: false },
      buttons: { button_ids: [], standard_roles: [] },
      check: { text: "Check", checked: false, group_id: null, exclusive: false },
      col: { margins: {}, gap: 0, stretches: [], alignment: "start" },
      combo: { options: [], current_index: -1, current_text: "", editable: false,
               editor_value: "" },
      form: { rows: [] },
      frame: { margins: {}, gap: 0, stretches: [], alignment: "start" },
      grid: { cells: [], column_minimums: [], column_stretches: [] },
      hline: { orientation: "horizontal", size_policy: "preferred" },
      label: { format: "plain", text: "Label", wrap: false, alignment: "start",
               strike: false, selectable: false, link_actions: [] },
      line: { value: "", placeholder: "", selection_start: 0, selection_end: 0,
              password: false, max_length: 10, max_blocks: 0 },
      radio: { text: "Radio", checked: false, group_id: "group", exclusive: true },
      row: { margins: {}, gap: 0, stretches: [], alignment: "start" },
      scroll: { offset: 0, extent: 0, shown_count: 0, total_count: 0, row_ids: [] },
      spacer: { orientation: "horizontal", size_policy: "preferred" },
      spin: { value: 1, minimum: 0, maximum: 2, step: 1, suffix: "",
              special_value_text: "" },
      stack: { pages: [], current_page: null },
      textarea: { value: "", placeholder: "", selection_start: 0, selection_end: 0,
                  password: false, max_length: 10, max_blocks: 0 },
    };
    const rendered = contract.NODE_KINDS.map((kind, index) => {
      const node = { ...common, ...fields[kind], id: `node-${index}`, kind };
      return renderer.renderWidget(node, { nodes: new Map([[node.id, node]]) }).tagName;
    });
    let error;
    try {
      renderer.renderWidget({ ...common, id: "bad", kind: "unknown" }, {});
    } catch (caught) {
      error = { name: caught.name, code: caught.code, kind: caught.details.kind };
    }
    return {
      rendered,
      rendererKinds: Object.keys(renderer.RENDERERS),
      error,
    };
  });
  expect(result.rendererKinds).toEqual(NODE_KINDS);
  expect(result.rendered).toHaveLength(NODE_KINDS.length);
  expect(result.error).toEqual({
    name: "DemoContractError", code: "unknown-node-kind", kind: "unknown",
  });
});

test("rejects inherited renderer prototype keys", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const error = await page.evaluate(async () => {
    const { renderWidget } = await import("/docs/demo-renderer.js");
    try {
      renderWidget({ id: "bad", kind: "constructor" }, {});
    } catch (caught) {
      return { name: caught.name, code: caught.code, kind: caught.details.kind };
    }
    return null;
  });
  expect(error).toEqual({
    name: "DemoContractError", code: "unknown-node-kind", kind: "constructor",
  });
});

test("renders explicitly plain HTML-looking labels as text", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async () => {
    const { renderWidget } = await import("/docs/demo-renderer.js");
    const label = renderWidget({
      id: "plain", kind: "label", format: "plain", text: "<b>plain</b>",
      effective_visible: true, effective_enabled: true, style_roles: [],
      actions: [], link_actions: [],
    });
    return { text: label.textContent, html: label.innerHTML };
  });
  expect(result).toEqual({ text: "<b>plain</b>", html: "&lt;b&gt;plain&lt;/b&gt;" });
});

test("the live demo dialog context collects scroll actions", async ({ page }) => {
  const response = await page.request.get("/docs/demo.js");
  expect(response.ok()).toBeTruthy();
  const source = await response.text();
  expect(source).toContain("scrollActions: []");
  expect(source).toContain("scroll(id, offset)");
  expect(source).toContain("...ctx.scrollActions");
});

test("preserves grid form stack state and plain text semantics", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async () => {
    const { renderWidget } = await import("/docs/demo-renderer.js");
    const common = {
      parent_id: null, children: [], visible: true, effective_visible: true,
      enabled: true, effective_enabled: true, accessible_name: "",
      accessible_description: "", tooltip: "", focus_policy: "none",
      readonly: false, style_roles: [], actions: [],
    };
    const label = { ...common, id: "label", kind: "label", format: "plain",
      text: "<b>plain</b>", wrap: false, alignment: "start", strike: false,
      selectable: false, link_actions: [] };
    const field = { ...common, id: "field", kind: "line", value: "value",
      placeholder: "", selection_start: 0, selection_end: 0, password: false,
      max_length: 20, max_blocks: 0 };
    const first = { ...common, id: "first", kind: "label", format: "plain",
      text: "first", wrap: false, alignment: "start", strike: false,
      selectable: false, link_actions: [] };
    const second = { ...first, id: "second", text: "second" };
    const nodes = new Map([label, field, first, second].map((node) => [node.id, node]));
    const context = { nodes };
    const grid = renderWidget({ ...common, id: "grid", kind: "grid",
      children: ["label"], cells: [{ id: "label", row: 2, column: 3,
        row_span: 2, column_span: 4, alignment: "center" }],
      column_minimums: [0, 0, 0, 80], column_stretches: [0, 0, 0, 2] }, context);
    const form = renderWidget({ ...common, id: "form", kind: "form",
      children: ["label", "field"], rows: [{ label_id: "label", field_id: "field" }] },
      context);
    const stack = renderWidget({ ...common, id: "stack", kind: "stack",
      children: ["first", "second"], pages: ["first", "second"],
      current_page: "second" }, context);
    const disabled = renderWidget({ ...field, id: "disabled",
      effective_visible: false, effective_enabled: false,
      accessible_name: "Named field", accessible_description: "Field help",
      tooltip: "A tip", readonly: true, style_roles: ["muted"] }, context);
    return {
      gridPlacement: grid.firstElementChild.style.gridArea,
      plainText: grid.firstElementChild.textContent,
      plainHtml: grid.firstElementChild.innerHTML,
      formAssociation: form.querySelector("label").htmlFor,
      fieldId: form.querySelector("input").id,
      stackText: stack.textContent,
      commonState: {
        hidden: disabled.hidden,
        disabled: disabled.disabled,
        readonly: disabled.readOnly,
        label: disabled.getAttribute("aria-label"),
        description: disabled.getAttribute("aria-description"),
        tooltip: disabled.title,
        muted: disabled.classList.contains("demo-role-muted"),
      },
    };
  });
  expect(result).toEqual({
    gridPlacement: "3 / 4 / span 2 / span 4",
    plainText: "<b>plain</b>",
    plainHtml: "&lt;b&gt;plain&lt;/b&gt;",
    formAssociation: "field",
    fieldId: "field",
    stackText: "second",
    commonState: {
      hidden: true,
      disabled: true,
      readonly: true,
      label: "Named field",
      description: "Field help",
      tooltip: "A tip",
      muted: true,
    },
  });
});

test("emits normalized Escape and scroll actions", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const actions = await page.evaluate(async () => {
    const { renderWidget, renderWidgetTree } = await import("/docs/demo-renderer.js");
    const common = {
      parent_id: null, children: [], visible: true, effective_visible: true,
      enabled: true, effective_enabled: true, accessible_name: "",
      accessible_description: "", tooltip: "", focus_policy: "none",
      readonly: false, style_roles: [], actions: [],
    };
    const emitted = [];
    const scrollContent = { ...common, id: "scroll-content", kind: "label",
      format: "plain", text: "content", wrap: false, alignment: "start",
      strike: false, selectable: false, link_actions: [] };
    const scrollNode = { ...common, id: "scroll", kind: "scroll", offset: 0,
      extent: 10, shown_count: 1, total_count: 1, row_ids: [scrollContent.id],
      children: [scrollContent.id] };
    const scroll = renderWidget(scrollNode, {
      nodes: new Map([[scrollNode.id, scrollNode], [scrollContent.id, scrollContent]]),
      scroll: (id, offset) => emitted.push({ type: "scroll", id, offset }),
    });
    scroll.style.height = "10px";
    scroll.style.overflow = "auto";
    scroll.firstElementChild.style.height = "30px";
    document.body.appendChild(scroll);
    if (scroll.scrollHeight <= scroll.clientHeight) {
      throw new Error("scroll fixture is not scrollable");
    }
    scroll.scrollTop = 7;
    scroll.dispatchEvent(new Event("scroll"));

    const root = { ...common, id: "dialog", kind: "box",
      margins: {}, gap: 0, stretches: [], alignment: "start",
      actions: ["key", "close"] };
    const tree = renderWidgetTree({ root_id: root.id, nodes: [root] }, {
      key: (id, key, modifiers) => emitted.push({ type: "key", id, key, modifiers }),
    });
    tree.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    return emitted;
  });
  expect(actions).toEqual([
    { type: "scroll", id: "scroll", offset: 7 },
    { type: "key", id: "dialog", key: "Escape", modifiers: [] },
  ]);
});

test("puts schema accessibility on the interactive control", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async () => {
    const { renderWidget } = await import("/docs/demo-renderer.js");
    const common = {
      parent_id: null, children: [], visible: true, effective_visible: true,
      enabled: true, effective_enabled: true, accessible_name: "",
      accessible_description: "", tooltip: "", focus_policy: "strong",
      readonly: false, style_roles: [], actions: [],
    };
    const combo = renderWidget({
      ...common, id: "combo", kind: "combo",
      accessible_name: "Choose a sample", accessible_description: "Two choices",
      tooltip: "Choose one", actions: ["select-option"],
      options: [{ id: "first", label: "First" }, { id: "second", label: "Second" }],
      current_index: 0, current_text: "First", editable: false, editor_value: "",
    });
    const spin = renderWidget({
      ...common, id: "spin", kind: "spin", accessible_name: "Check every",
      value: 5, minimum: 1, maximum: 10, step: 1, suffix: " min",
      special_value_text: "", actions: ["edit-text"],
    });
    const plain = renderWidget({
      ...common, id: "plain", kind: "button", text: "Continue",
      checkable: false, checked: false, role: "accept", default: true, escape: false,
      actions: ["activate"],
    });
    const pressed = renderWidget({
      ...common, id: "pressed", kind: "button", text: "Keep",
      checkable: true, checked: true, role: "other", default: false, escape: false,
      actions: ["activate"],
    });
    return {
      combo: {
        name: combo.querySelector("select").getAttribute("aria-label"),
        description: combo.querySelector("select").getAttribute("aria-description"),
        tooltip: combo.querySelector("select").title,
      },
      spinName: spin.querySelector("input").getAttribute("aria-label"),
      plainPressed: plain.getAttribute("aria-pressed"),
      togglePressed: pressed.getAttribute("aria-pressed"),
    };
  });
  expect(result).toEqual({
    combo: { name: "Choose a sample", description: "Two choices", tooltip: "Choose one" },
    spinName: "Check every",
    plainPressed: null,
    togglePressed: "true",
  });
});

test("combo arrows select an option through the production action", async ({ page }) => {
  await page.goto("/browser_tests/contract-page.html");
  const result = await page.evaluate(async () => {
    const { renderWidget } = await import("/docs/demo-renderer.js");
    const emitted = [];
    const combo = renderWidget({
      id: "combo", kind: "combo", parent_id: null, children: [], visible: true,
      effective_visible: true, enabled: true, effective_enabled: true,
      accessible_name: "Choose", accessible_description: "", tooltip: "",
      focus_policy: "strong", readonly: false, style_roles: [],
      actions: ["select-option"],
      options: [{ id: "first", label: "First" }, { id: "second", label: "Second" }],
      current_index: 0, current_text: "First", editable: false, editor_value: "",
    }, {
      registerInput(element, action) {
        element.onchange = () => emitted.push(action(element));
      },
    });
    const select = combo.querySelector("select");
    select.dispatchEvent(new KeyboardEvent("keydown", {
      key: "ArrowDown", bubbles: true, cancelable: true,
    }));
    return { value: select.value, emitted };
  });
  expect(result).toEqual({
    value: "second",
    emitted: [{ type: "select-option", id: "combo", option_id: "second" }],
  });
});
