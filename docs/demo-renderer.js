import { NODE_KINDS } from "./demo-contract.js";

export class DemoContractError extends Error {
  constructor(code, details = {}) {
    super(code);
    this.name = "DemoContractError";
    this.code = code;
    this.details = details;
  }
}

function nodeById(context, id) {
  const node = context.nodes instanceof Map
    ? context.nodes.get(id)
    : context.nodes?.[id];
  if (!node) {
    throw new DemoContractError("unknown-widget-id", { id });
  }
  return node;
}

function registerInput(context, element, action) {
  if (typeof context.registerInput === "function") {
    context.registerInput(element, action);
  } else if (Array.isArray(context.inputs)) {
    element.demoAction = action;
    context.inputs.push(element);
  }
}

function activate(context, node) {
  if (typeof context.activate === "function") context.activate(node.id);
  else if (typeof context.click === "function") context.click(node.id);
}

function activateLink(context, node, actionId) {
  if (typeof context.activateLink === "function") {
    context.activateLink(node.id, actionId);
  }
}

// A rich label's text is Qt rich text built from deck content the worker fetched
// from the sample source, so it reaches this page as untrusted markup. The protocol
// only bounds its length; what may become live DOM is decided here.
//
// `style` is on this list deliberately: the production About dialog ships its own
// stylesheet inside a rich label, and dropping it left that dialog unstyled. CSS
// cannot execute, so keeping it preserves the add-on's own markup without giving up
// what this guard is for (no script, no event handlers, no dangerous URLs). The
// residual is that a hostile source could restyle the page, which is defacement
// rather than code execution.
const RICH_TAGS = new Set([
  "a", "b", "big", "br", "caption", "code", "col", "colgroup", "dd", "div", "dl",
  "dt", "em", "details", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
  "img", "li", "ol", "p", "pre", "s", "small", "span", "strong", "style", "sub",
  "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
]);
// Unwrapping these would spill their source into the page as visible text, so they
// go with their contents.
const RICH_DROP_TAGS = new Set([
  "base", "embed", "form", "iframe", "link", "math", "meta", "noscript", "object",
  "script", "svg", "template",
]);
const RICH_URL_ATTRS = new Set(["href", "src", "xlink:href"]);
const RICH_URL_SCHEMES = new Set(["http", "https", "mailto"]);

function safeRichUrl(value) {
  const trimmed = String(value).replace(/[\x00-\x20]/g, "").toLowerCase();
  const scheme = /^([a-z][a-z0-9+.-]*):/.exec(trimmed);
  // No scheme at all is a relative or absolute path, which is how an extracted
  // figure is referenced.
  if (!scheme) return true;
  return RICH_URL_SCHEMES.has(scheme[1]) || trimmed.startsWith("data:image/");
}

export function sanitizeRichText(markup) {
  const template = document.createElement("template");
  template.innerHTML = String(markup);
  for (const element of Array.from(template.content.querySelectorAll("*"))) {
    const tag = element.tagName.toLowerCase();
    if (RICH_DROP_TAGS.has(tag)) {
      element.remove();
      continue;
    }
    if (!RICH_TAGS.has(tag)) {
      element.replaceWith(...element.childNodes);
      continue;
    }
    for (const name of element.getAttributeNames()) {
      const lower = name.toLowerCase();
      if (lower.startsWith("on")
          || (RICH_URL_ATTRS.has(lower) && !safeRichUrl(element.getAttribute(name)))) {
        element.removeAttribute(name);
      }
    }
  }
  return template.innerHTML;
}

function textElement(node, tagName = "div") {
  const element = document.createElement(tagName);
  if (node.format === "rich") element.innerHTML = sanitizeRichText(node.text);
  else element.textContent = node.text;
  return element;
}

function applyMargins(element, node) {
  const margins = node.margins || {};
  element.style.margin = ["top", "right", "bottom", "left"]
    .map((side) => `${margins[side] || 0}px`).join(" ");
  element.style.gap = `${node.gap || 0}px`;
  if (node.alignment) element.style.alignItems = node.alignment;
}

function renderChildren(node, context, ids = node.children) {
  const fragment = document.createDocumentFragment();
  for (const id of ids || []) {
    fragment.appendChild(renderWidget(nodeById(context, id), context));
  }
  return fragment;
}

function renderBox(node, context) {
  const element = document.createElement("div");
  element.className = node.kind === "row" ? "hstack" : "vstack";
  if (node.kind === "frame") element.classList.add("qframe");
  applyMargins(element, node);
  element.appendChild(renderChildren(node, context));
  return element;
}

function renderButton(node, context) {
  const element = document.createElement("button");
  element.className = "btn";
  element.textContent = node.text;
  element.type = "button";
  element.dataset.role = node.role;
  if (node.checkable) {
    element.setAttribute("aria-pressed", String(Boolean(node.checked)));
  }
  if (node.default) element.dataset.default = "true";
  if (node.escape) element.dataset.escape = "true";
  element.onclick = () => activate(context, node);
  return element;
}

function renderButtons(node, context) {
  const element = document.createElement("div");
  element.className = "qbuttons";
  const ids = node.button_ids?.length ? node.button_ids : node.children;
  element.appendChild(renderChildren(node, context, ids));
  return element;
}

function renderToggle(node, context, type) {
  const wrapper = document.createElement("label");
  wrapper.className = "demo-toggle";
  const input = document.createElement("input");
  input.type = type;
  input.checked = Boolean(node.checked);
  input.dataset.wid = node.id;
  if (type === "radio" && node.group_id) input.name = node.group_id;
  wrapper.append(input, document.createTextNode(node.text));
  registerInput(context, input, (control) => ({
    type: "toggle", id: node.id, checked: control.checked,
  }));
  return wrapper;
}

function renderCombo(node, context) {
  const wrapper = document.createElement("span");
  wrapper.className = "demo-combo";
  const select = document.createElement("select");
  select.dataset.wid = node.id;
  select.dataset.demoPart = "select";
  for (const option of node.options) {
    const item = document.createElement("option");
    item.value = option.id;
    item.textContent = option.label;
    select.appendChild(item);
  }
  select.selectedIndex = node.current_index;
  wrapper.appendChild(select);
  registerInput(context, select, (control) => ({
    type: "select-option", id: node.id, option_id: control.value,
  }));
  select.onkeydown = (event) => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key)
        || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    const control = event.currentTarget;
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const next = Math.max(0, Math.min(control.options.length - 1,
      control.selectedIndex + direction));
    if (next === control.selectedIndex) return;
    control.selectedIndex = next;
    control.dispatchEvent(new Event("change", { bubbles: true }));
  };
  if (node.editable) {
    const editor = document.createElement("input");
    editor.type = "text";
    editor.value = node.editor_value;
    editor.dataset.wid = node.id;
    editor.dataset.demoPart = "editor";
    wrapper.appendChild(editor);
    registerInput(context, editor, (control) => ({
      type: "edit-text", id: node.id, value: control.value,
      selection_start: control.selectionStart || 0,
      selection_end: control.selectionEnd || 0,
      composing: false,
    }));
  }
  return wrapper;
}

function renderForm(node, context) {
  const element = document.createElement("div");
  element.className = "demo-form";
  for (const row of node.rows) {
    const labelNode = nodeById(context, row.label_id);
    const fieldNode = nodeById(context, row.field_id);
    const label = document.createElement("label");
    label.appendChild(renderWidget(labelNode, context));
    const field = renderWidget(fieldNode, context);
    const control = field.matches("button, input, select, textarea")
      ? field : field.querySelector("button, input, select, textarea");
    if (control) {
      if (control !== field) control.id = `${fieldNode.id}--control`;
      label.htmlFor = control.id || fieldNode.id;
    }
    element.append(label, field);
  }
  return element;
}

function renderGrid(node, context) {
  const element = document.createElement("div");
  element.className = "demo-grid";
  element.style.display = "grid";
  const columns = Math.max(node.column_minimums.length, node.column_stretches.length);
  if (columns) {
    element.style.gridTemplateColumns = Array.from({ length: columns }, (_, index) => {
      const minimum = node.column_minimums[index] || 0;
      const stretch = node.column_stretches[index] || 0;
      return `minmax(${minimum}px, ${stretch ? `${stretch}fr` : "auto"})`;
    }).join(" ");
  }
  for (const cell of node.cells) {
    const child = renderWidget(nodeById(context, cell.id), context);
    child.style.gridArea = `${cell.row + 1} / ${cell.column + 1} / `
      + `span ${cell.row_span} / span ${cell.column_span}`;
    child.style.justifySelf = cell.alignment;
    element.appendChild(child);
  }
  return element;
}

function renderLabel(node, context) {
  const element = textElement(node);
  if (node.wrap) element.style.whiteSpace = "normal";
  element.style.textAlign = node.alignment;
  if (node.strike) element.style.textDecoration = "line-through";
  if (node.selectable) element.style.userSelect = "text";
  if (node.format === "rich" && node.link_actions.length) {
    element.onclick = (event) => {
      const link = event.target.closest("a[href]");
      if (!link) return;
      const actionId = link.getAttribute("href");
      if (!node.link_actions.includes(actionId)) return;
      event.preventDefault();
      activateLink(context, node, actionId);
    };
  }
  return element;
}

function editAction(node, element) {
  return {
    type: "edit-text", id: node.id, value: element.value,
    selection_start: element.selectionStart || 0,
    selection_end: element.selectionEnd || 0,
    composing: false,
  };
}

function renderLine(node, context) {
  const element = document.createElement("input");
  element.type = node.password ? "password" : "text";
  element.className = "dinput";
  element.value = node.value;
  element.placeholder = node.placeholder;
  element.maxLength = node.max_length;
  element.dataset.wid = node.id;
  if (!node.readonly) registerInput(context, element, (control) => editAction(node, control));
  return element;
}

function renderScroll(node, context) {
  const element = document.createElement("div");
  element.className = "scrollbox";
  element.scrollTop = node.offset;
  const ids = node.row_ids?.length ? node.row_ids : node.children;
  element.appendChild(renderChildren(node, context, ids));
  if (typeof context.scroll === "function") {
    element.onscroll = (event) => context.scroll(
      node.id, Math.max(0, Math.round(event.currentTarget.scrollTop)),
    );
  }
  return element;
}

function renderSpacer(node) {
  const element = document.createElement("div");
  element.className = "demo-spacer";
  element.dataset.orientation = node.orientation;
  element.dataset.sizePolicy = node.size_policy;
  return element;
}

function renderSpin(node, context) {
  const wrapper = document.createElement("span");
  const input = document.createElement("input");
  input.className = "dinput dspin";
  input.type = "number";
  input.min = node.minimum;
  input.max = node.maximum;
  input.step = node.step;
  input.value = node.value;
  input.dataset.wid = node.id;
  input.dataset.demoPart = "editor";
  wrapper.append(input, document.createTextNode(node.suffix));
  registerInput(context, input, (control) => editAction(node, control));
  return wrapper;
}

function renderStack(node, context) {
  const element = document.createElement("div");
  element.className = "demo-stack";
  if (node.current_page !== null) {
    element.appendChild(renderWidget(nodeById(context, node.current_page), context));
  }
  return element;
}

function renderTextarea(node, context) {
  const element = document.createElement("textarea");
  element.className = "dinput";
  element.value = node.value;
  element.placeholder = node.placeholder;
  element.dataset.wid = node.id;
  if (node.max_length) element.maxLength = node.max_length;
  if (!node.readonly) registerInput(context, element, (control) => editAction(node, control));
  return element;
}

export const RENDERERS = Object.freeze({
  box: renderBox,
  button: renderButton,
  buttons: renderButtons,
  check: (node, context) => renderToggle(node, context, "checkbox"),
  col: renderBox,
  combo: renderCombo,
  form: renderForm,
  frame: renderBox,
  grid: renderGrid,
  hline: () => document.createElement("hr"),
  label: renderLabel,
  line: renderLine,
  radio: (node, context) => renderToggle(node, context, "radio"),
  row: renderBox,
  scroll: renderScroll,
  spacer: renderSpacer,
  spin: renderSpin,
  stack: renderStack,
  textarea: renderTextarea,
});

if (Object.keys(RENDERERS).join("\0") !== NODE_KINDS.join("\0")) {
  throw new DemoContractError("unknown-node-kind", { kind: "renderer-registry" });
}

function applyCommonState(element, node, context) {
  element.id = node.id;
  element.dataset.wid = node.id;
  element.hidden = !node.effective_visible;
  if (!node.effective_visible) element.style.display = "none";
  element.setAttribute("aria-hidden", String(!node.effective_visible));
  element.setAttribute("aria-disabled", String(!node.effective_enabled));
  const controls = element.matches("button, input, select, textarea")
    ? [element]
    : Array.from(element.querySelectorAll("button, input, select, textarea"));
  for (const control of controls) {
    control.disabled = !node.effective_enabled;
    if ("readOnly" in control) control.readOnly = Boolean(node.readonly);
  }
  const accessibilityTargets = controls.length ? controls : [element];
  for (const target of accessibilityTargets) {
    if (node.accessible_name) target.setAttribute("aria-label", node.accessible_name);
    if (node.accessible_description) {
      target.setAttribute("aria-description", node.accessible_description);
    }
    if (node.tooltip) target.title = node.tooltip;
  }
  for (const role of node.style_roles) element.classList.add(`demo-role-${role}`);
  if (node.actions?.includes("key") && typeof context.key === "function") {
    element.onkeydown = (event) => {
      const modifiers = [];
      if (event.altKey) modifiers.push("Alt");
      if (event.ctrlKey) modifiers.push("Control");
      if (event.metaKey) modifiers.push("Meta");
      if (event.shiftKey) modifiers.push("Shift");
      if (context.key(node.id, event.key, modifiers, event.target)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
  }
  return element;
}

export function renderWidget(node, context = {}) {
  const kind = node?.kind;
  if (!Object.prototype.hasOwnProperty.call(RENDERERS, kind)) {
    throw new DemoContractError("unknown-node-kind", { kind: node?.kind });
  }
  const renderer = RENDERERS[kind];
  return applyCommonState(renderer(node, context), node, context);
}

export function renderWidgetTree(tree, context = {}) {
  const nodes = new Map(tree.nodes.map((node) => [node.id, node]));
  const renderContext = { ...context, nodes };
  return renderWidget(nodeById(renderContext, tree.root_id), renderContext);
}
