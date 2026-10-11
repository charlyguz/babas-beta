(function() {
  "use strict";
  class BabasError extends Error {
    code;
    data;
    constructor(code, message, data) {
      super(message);
      this.code = code;
      this.data = data;
    }
    toInfo() {
      return this.data ? { code: this.code, message: this.message, data: this.data } : { code: this.code, message: this.message };
    }
  }
  function toErrorInfo(err) {
    if (err instanceof BabasError) return err.toInfo();
    const message = err instanceof Error ? err.message : String(err);
    return { code: "INTERNAL", message };
  }
  function clean(value, max = 120) {
    if (!value) return "";
    const text = value.replace(/\s+/g, " ").trim();
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }
  function fold(value) {
    if (!value) return "";
    return value.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
  }
  function wordCount(value) {
    const text = value.trim();
    return text ? text.split(/\s+/).length : 0;
  }
  const TEXT_INPUT_TYPES = /* @__PURE__ */ new Set(["", "text", "search", "email", "tel", "url", "number", "password", "date", "datetime-local", "month", "week", "time"]);
  const INTERACTIVE_SELECTOR = [
    "a[href]",
    "button",
    'input:not([type="hidden"])',
    "select",
    "textarea",
    "summary",
    '[role="button"]',
    '[role="link"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[role="menuitemcheckbox"]',
    '[role="menuitemradio"]',
    '[role="option"]',
    '[role="combobox"]',
    '[role="textbox"]',
    '[role="searchbox"]',
    '[role="slider"]',
    '[role="spinbutton"]',
    '[role="treeitem"]',
    '[contenteditable=""]',
    '[contenteditable="true"]',
    '[contenteditable="plaintext-only"]',
    "[onclick]",
    '[tabindex]:not([tabindex="-1"])'
  ].join(",");
  function isHTMLElement(node) {
    return typeof node === "object" && node !== null && node.nodeType === 1 && "style" in node;
  }
  function isTextField(el) {
    if (el instanceof HTMLTextAreaElement) return true;
    return el instanceof HTMLInputElement && TEXT_INPUT_TYPES.has((el.getAttribute("type") || "").toLowerCase());
  }
  function isContentEditable(el) {
    return isHTMLElement(el) && (el.isContentEditable || ["", "true", "plaintext-only"].includes(el.getAttribute("contenteditable") ?? "x"));
  }
  function inputType(el) {
    if (el instanceof HTMLInputElement) return (el.getAttribute("type") || "text").toLowerCase();
    return "";
  }
  function deepQueryAll(selector, root2 = document) {
    const out = [];
    const visit = (scope) => {
      scope.querySelectorAll(selector).forEach((el) => {
        if (isHTMLElement(el)) out.push(el);
      });
      scope.querySelectorAll("*").forEach((el) => {
        if (el.shadowRoot) visit(el.shadowRoot);
      });
    };
    visit(root2);
    return out;
  }
  function allElementsDeep(root2 = document) {
    const out = [];
    const visit = (scope) => {
      scope.querySelectorAll("*").forEach((el) => {
        if (isHTMLElement(el)) out.push(el);
        if (el.shadowRoot) visit(el.shadowRoot);
      });
    };
    visit(root2);
    return out;
  }
  function isVisible(el) {
    if (!isHTMLElement(el) || !el.isConnected) return false;
    if (el.closest("[inert]")) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
    const check = el.checkVisibility;
    if (typeof check === "function" && !check.call(el, { checkVisibilityCSS: true })) return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= 1 && rect.height >= 1;
  }
  function visibleBox(el) {
    if (isVisible(el)) return el.getBoundingClientRect();
    if (el instanceof HTMLInputElement && (el.type === "checkbox" || el.type === "radio") && el.labels) {
      for (const label of Array.from(el.labels)) {
        if (isVisible(label)) return label.getBoundingClientRect();
      }
    }
    return null;
  }
  function inViewport(rect) {
    return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  }
  function isDisabled(el) {
    if (el.disabled) return true;
    if (el.getAttribute("aria-disabled") === "true") return true;
    return !!el.closest("fieldset[disabled]");
  }
  const IMPLICIT_ROLES = {
    a: "link",
    button: "button",
    select: "combobox",
    textarea: "textbox",
    summary: "button",
    img: "img",
    option: "option"
  };
  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.trim().split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    if (tag === "input") {
      const type = inputType(el);
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type === "search") return "searchbox";
      return "textbox";
    }
    if (isContentEditable(el)) return "textbox";
    return IMPLICIT_ROLES[tag] ?? "generic";
  }
  function visibleText(el) {
    const text = typeof el.innerText === "string" ? el.innerText : el.textContent ?? "";
    return text.replace(/\s+/g, " ").trim();
  }
  function textWithoutControls(label) {
    const copy = label.cloneNode(true);
    copy.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
    return (copy.textContent ?? "").replace(/\s+/g, " ").trim();
  }
  function nearbyText(el) {
    const short = (node) => {
      if (!node || !isHTMLElement(node)) return "";
      if (node.querySelector("input, select, textarea, button")) return "";
      const text = visibleText(node);
      return text.length > 0 && text.length <= 60 ? text : "";
    };
    const prev = short(el.previousElementSibling);
    if (prev) return prev;
    const cell = el.closest("td, th, div, li, p");
    if (cell && cell !== el) {
      const before = short(cell.previousElementSibling);
      if (before) return before;
    }
    return "";
  }
  function labelOf(el) {
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return clean(aria);
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const root2 = el.getRootNode();
      const text2 = labelledBy.split(/\s+/).map((id) => (root2.getElementById ? root2.getElementById(id) : document.getElementById(id))?.textContent ?? "").join(" ");
      if (text2.trim()) return clean(text2);
    }
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
      const type = inputType(el);
      if (el instanceof HTMLInputElement && ["submit", "button", "reset"].includes(type)) {
        return clean(el.value || el.getAttribute("title") || type);
      }
      if (el instanceof HTMLInputElement && type === "image") return clean(el.alt || el.title || "image");
      const labels = el.labels ? Array.from(el.labels) : [];
      const fromLabels = labels.map((l) => textWithoutControls(l)).join(" ");
      if (fromLabels.trim()) return clean(fromLabels);
      const placeholder = el.getAttribute("placeholder");
      if (placeholder && placeholder.trim()) return clean(placeholder);
      const title2 = el.getAttribute("title");
      if (title2 && title2.trim()) return clean(title2);
      const near = nearbyText(el);
      if (near) return clean(near);
      return clean(el.getAttribute("name") || el.id || "");
    }
    if (el instanceof HTMLImageElement) return clean(el.alt || el.title);
    const text = visibleText(el);
    if (text) return clean(text);
    const title = el.getAttribute("title");
    if (title && title.trim()) return clean(title);
    const img = el.querySelector("img[alt]");
    if (img && img.getAttribute("alt")) return clean(img.getAttribute("alt"));
    const svgTitle = el.querySelector("svg title");
    if (svgTitle && svgTitle.textContent) return clean(svgTitle.textContent);
    return "";
  }
  function deepElementFromPoint(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }
  function composedContains(ancestor, node) {
    let cur = node;
    while (cur) {
      if (cur === ancestor) return true;
      const parent = cur.assignedSlot ?? cur.parentNode;
      cur = parent instanceof ShadowRoot ? parent.host : parent;
    }
    return false;
  }
  function formOf(el) {
    const owner = el.form;
    if (owner instanceof HTMLFormElement) return owner;
    return el.closest("form");
  }
  const PATTERNS = [
    ["curp", /(?<![A-Z0-9])[A-Z]{4}\d{6}[HMX][A-Z]{5}[A-Z0-9]\d(?![A-Z0-9])/i],
    ["rfc", /(?<![A-Z0-9Ñ&])[A-ZÑ&]{3,4}\d{6}(?:[A-Z0-9]{3})?(?![A-Z0-9])/i],
    ["email", /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i],
    ["nss", /(?<!\d)\d{2}[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d(?!\d)/],
    ["phone", /(?<![\d+])(?:\+?52[\s-]?)?(?:1[\s-]?)?(?:\(?\d{2}\)?[\s-]?\d{4}|\(?\d{3}\)?[\s-]?\d{3})[\s-]?\d{4}(?!\d)/]
  ];
  const CARD = /(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)/g;
  function luhn(digits) {
    let sum = 0;
    let double = false;
    for (let i = digits.length - 1; i >= 0; i -= 1) {
      let d = digits.charCodeAt(i) - 48;
      if (double) {
        d *= 2;
        if (d > 9) d -= 9;
      }
      sum += d;
      double = !double;
    }
    return sum % 10 === 0;
  }
  function detectPii(value) {
    if (!value) return [];
    const found = /* @__PURE__ */ new Set();
    for (const match of value.matchAll(CARD)) {
      const digits = match[0].replace(/\D/g, "");
      if (digits.length >= 13 && luhn(digits)) found.add("card");
    }
    for (const [kind, re] of PATTERNS) {
      if (re.test(value)) found.add(kind);
    }
    return [...found];
  }
  const CONSEQUENTIAL_VERBS = /\b(enviar|envia|send|mandar|submit|entregar|turn in|hand in|pagar|pay|comprar|buy|purchase|checkout|finalizar compra|realizar pedido|place order|eliminar|borrar|delete|publicar|post|publish|firmar|sign|agendar|reservar|book|schedule|confirmar|confirm|transferir|transfer|solicitar|registrarme|registrarse|sign up|crear cuenta|create account|darse de baja|unsubscribe)\b/;
  const NOT_CONSEQUENTIAL = /\b(sign in|sign out|log in|log out|login|logout|iniciar sesion|cerrar sesion|acceder)\b/;
  const PII_FIELD = /\b(curp|rfc|nss|numero de seguridad social|seguridad social|social security|correo|e-?mail|email address|telefono|tel|phone|celular|cel|movil|whatsapp)\b/;
  const PAYMENT_FIELD = /\b(tarjeta|card number|numero de tarjeta|cvv|cvc|cvv2|codigo de seguridad|security code|vencimiento|expiracion|expiration)\b/;
  const SEARCH_NAMES = /* @__PURE__ */ new Set(["q", "s", "query", "search", "search_query", "keyword", "keywords", "buscar", "busqueda", "term", "k"]);
  const SEARCH_HINT = /(search|buscar|busca|busqueda|find)/;
  const CLICKABLE_ROLES = /* @__PURE__ */ new Set(["button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "option", "treeitem"]);
  const CAPTCHA_SELECTOR = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[title*="captcha" i]',
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[data-sitekey]"
  ].join(",");
  function attrHint(el) {
    return fold(
      [
        el.getAttribute("name"),
        el.id,
        el.getAttribute("placeholder"),
        el.getAttribute("aria-label"),
        el.getAttribute("autocomplete"),
        el.getAttribute("title")
      ].filter(Boolean).join(" ").replace(/[_-]+/g, " ")
    );
  }
  function isPasswordField(el) {
    if (inputType(el) === "password") return true;
    const autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();
    return autocomplete.includes("password");
  }
  function isSearchField(el) {
    const role = roleOf(el);
    if (role === "searchbox" || inputType(el) === "search") return true;
    if (el.closest('[role="search"]')) return true;
    const name = (el.getAttribute("name") || "").toLowerCase();
    if (SEARCH_NAMES.has(name)) return true;
    const hint = attrHint(el);
    if (SEARCH_HINT.test(hint) && !PII_FIELD.test(hint)) return true;
    return role === "combobox" && SEARCH_HINT.test(fold(labelOf(el)));
  }
  function isSearchForm(form) {
    if (form.getAttribute("role") === "search" || form.closest('[role="search"]')) return true;
    const fields = Array.from(form.elements).filter(
      (f) => isHTMLElement(f) && isTextField(f) && isVisible(f)
    );
    if (fields.some((f) => isPasswordField(f))) return false;
    if (fields.length !== 1) return false;
    return isSearchField(fields[0]);
  }
  function isSubmitControl(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "button") {
      const type = (el.getAttribute("type") || "submit").toLowerCase();
      return type === "submit" && !!formOf(el);
    }
    if (tag === "input") {
      const type = inputType(el);
      return (type === "submit" || type === "image") && !!formOf(el);
    }
    return false;
  }
  function isConsequentialLabel(label) {
    const text = fold(label);
    if (!text || NOT_CONSEQUENTIAL.test(text)) return false;
    if (wordCount(text) <= 6) return CONSEQUENTIAL_VERBS.test(text);
    const firstWords = text.split(" ").slice(0, 3).join(" ");
    return CONSEQUENTIAL_VERBS.test(firstWords);
  }
  function fieldPiiKind(el, label) {
    const type = inputType(el);
    const autocomplete = (el.getAttribute("autocomplete") || "").toLowerCase();
    const payment = autocomplete.startsWith("cc-") || PAYMENT_FIELD.test(fold(label)) || PAYMENT_FIELD.test(attrHint(el));
    if (payment) return { pii: true, payment: true };
    if (isSearchField(el)) return { pii: false, payment: false };
    if (type === "email" || type === "tel") return { pii: true, payment: false };
    if (autocomplete === "email" || autocomplete.startsWith("tel")) return { pii: true, payment: false };
    const hint = `${fold(label)} ${attrHint(el)}`;
    return { pii: PII_FIELD.test(hint), payment: false };
  }
  function flagsFor(el, label) {
    const flags = [];
    const role = roleOf(el);
    const form = formOf(el);
    const searchForm = form ? isSearchForm(form) : false;
    if (isDisabled(el)) flags.push("disabled");
    if (isSubmitControl(el)) flags.push("submit");
    if (searchForm || (isTextField(el) || isContentEditable(el)) && isSearchField(el)) flags.push("search_form");
    if (isPasswordField(el)) flags.push("password");
    if (isTextField(el) || isContentEditable(el) || el instanceof HTMLSelectElement) {
      const kind = fieldPiiKind(el, label);
      if (kind.pii) flags.push("pii");
      if (kind.payment) flags.push("payment");
    }
    const clickable = CLICKABLE_ROLES.has(role) || flags.includes("submit");
    if (clickable && isConsequentialLabel(label)) flags.push("consequential");
    if (clickable && /\b(pagar|pay|pago|checkout)\b/.test(fold(label))) flags.push("payment");
    if (el.closest(CAPTCHA_SELECTOR) || el.matches(CAPTCHA_SELECTOR)) flags.push("captcha");
    return [...new Set(flags)];
  }
  function clickGateReasons(el) {
    const flags = flagsFor(el, labelOf(el));
    const reasons = [];
    if (flags.includes("submit") && !flags.includes("search_form")) reasons.push("submit_form");
    if (flags.includes("consequential")) reasons.push("consequential");
    if (flags.includes("payment") && !flags.includes("pii")) reasons.push("payment");
    return reasons;
  }
  function typeGateReasons(el, value) {
    const flags = flagsFor(el, labelOf(el));
    const reasons = [];
    if (flags.includes("pii")) reasons.push("pii_field");
    if (flags.includes("payment")) reasons.push("payment");
    if (detectPii(value).length > 0) reasons.push("pii_value");
    return reasons;
  }
  function enterGateReasons(el) {
    if (!el) return [];
    if (isContentEditable(el) || el instanceof HTMLTextAreaElement) {
      return isSearchField(el) ? [] : ["enter_in_editor"];
    }
    const form = formOf(el);
    if (form && isTextField(el)) return isSearchForm(form) ? [] : ["enter_submits_form"];
    return [];
  }
  const MARK_ATTR = "data-babas-mark";
  function requireAuthorization(reasons, authorized, el) {
    if (reasons.length === 0 || authorized) return;
    throw new BabasError("CONFIRMATION_REQUIRED", "This action needs the user to confirm it first", {
      reasons,
      label: el ? labelOf(el) : "",
      host: location.hostname
    });
  }
  function center(el) {
    const box2 = visibleBox(el) ?? el.getBoundingClientRect();
    return { x: box2.left + box2.width / 2, y: box2.top + box2.height / 2 };
  }
  function hitAllowed(el, hit) {
    if (!hit) return true;
    if (composedContains(el, hit) || composedContains(hit, el)) return true;
    const label = hit.closest("label");
    if (label && label.control === el) return true;
    if (el instanceof HTMLInputElement && el.labels) {
      return Array.from(el.labels).some((l) => composedContains(l, hit));
    }
    return false;
  }
  function dispatchPointer(el, type, x, y) {
    const init = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, button: 0, view: window };
    if (type.startsWith("pointer") && typeof PointerEvent === "function") {
      el.dispatchEvent(new PointerEvent(type, { ...init, pointerId: 1, pointerType: "mouse", isPrimary: true }));
    } else {
      el.dispatchEvent(new MouseEvent(type, init));
    }
  }
  function click(el, authorized) {
    if (isDisabled(el)) throw new BabasError("DISABLED", `"${labelOf(el)}" is disabled`);
    requireAuthorization(clickGateReasons(el), authorized, el);
    el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    if (!visibleBox(el)) throw new BabasError("NOT_VISIBLE", `"${labelOf(el)}" is not visible`);
    const { x, y } = center(el);
    const hit = deepElementFromPoint(x, y);
    if (!hitAllowed(el, hit)) {
      const blocker = isHTMLElement(hit) ? clean(labelOf(hit) || visibleText(hit), 60) : "";
      throw new BabasError("OBSCURED", `Another element covers the target${blocker ? `: "${blocker}"` : ""}. Close it first.`, {
        blocker
      });
    }
    for (const type of ["pointerover", "mouseover", "pointerdown", "mousedown"]) dispatchPointer(el, type, x, y);
    if (typeof el.focus === "function") el.focus({ preventScroll: true });
    for (const type of ["pointerup", "mouseup"]) dispatchPointer(el, type, x, y);
    el.click();
    return { label: labelOf(el) };
  }
  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }
  function normalizeForType(el, text) {
    if (el instanceof HTMLInputElement && el.type === "date") {
      const m = text.trim().match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/);
      if (m) return `${m[3]}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
    }
    return text;
  }
  function key(el, type, keyName) {
    const code = keyName.length === 1 ? `Key${keyName.toUpperCase()}` : keyName;
    const keyCode = keyName === "Enter" ? 13 : keyName === "Escape" ? 27 : keyName === "Tab" ? 9 : keyName.length === 1 ? keyName.toUpperCase().charCodeAt(0) : 0;
    return el.dispatchEvent(
      new KeyboardEvent(type, { key: keyName, code, keyCode, which: keyCode, bubbles: true, cancelable: true, composed: true })
    );
  }
  function typeText(el, text, authorized) {
    if (isPasswordField(el)) {
      throw new BabasError("PASSWORD_FIELD", "BABAS never types passwords: ask the user to type it");
    }
    if (isDisabled(el)) throw new BabasError("DISABLED", `"${labelOf(el)}" is disabled`);
    if (!isTextField(el) && !isContentEditable(el)) {
      throw new BabasError("NOT_EDITABLE", `"${labelOf(el)}" is not a text field`);
    }
    requireAuthorization(typeGateReasons(el, text), authorized, el);
    el.scrollIntoView({ block: "center", behavior: "instant" });
    el.focus({ preventScroll: true });
    if (isContentEditable(el) && !isTextField(el)) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      selection?.removeAllRanges();
      selection?.addRange(range);
      const inserted = typeof document.execCommand === "function" && document.execCommand("insertText", false, text);
      if (!inserted || fold(visibleText(el)) !== fold(text)) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: text }));
      }
      return { label: labelOf(el) };
    }
    const field = el;
    const value = normalizeForType(field, text);
    key(field, "keydown", value.slice(-1) || "Unidentified");
    setNativeValue(field, value);
    field.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText", data: value }));
    key(field, "keyup", value.slice(-1) || "Unidentified");
    field.dispatchEvent(new Event("change", { bubbles: true }));
    if (field.value !== value && !(field instanceof HTMLInputElement && field.type === "number")) {
      throw new BabasError("NOT_EDITABLE", `The field rejected the value (expected format? got "${clean(field.value, 40)}")`);
    }
    return { label: labelOf(el) };
  }
  function implicitSubmit(field) {
    const form = formOf(field);
    if (!form) return false;
    const submitter = Array.from(form.elements).find(
      (e) => e instanceof HTMLButtonElement && (e.getAttribute("type") || "submit").toLowerCase() === "submit" || e instanceof HTMLInputElement && (e.type === "submit" || e.type === "image")
    );
    const textFields = Array.from(form.elements).filter((e) => isHTMLElement(e) && isTextField(e));
    if (!submitter && textFields.length > 1) return false;
    if (submitter && submitter.disabled) return false;
    if (typeof form.requestSubmit === "function") form.requestSubmit(submitter);
    else form.submit();
    return true;
  }
  function focusables() {
    return Array.from(
      document.querySelectorAll('a[href], button, input:not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"]), [contenteditable="true"]')
    ).filter((el) => isVisible(el) && !isDisabled(el));
  }
  function press(keyName, el, authorized) {
    if (el) {
      el.scrollIntoView({ block: "center", behavior: "instant" });
      el.focus({ preventScroll: true });
    }
    const active = el ?? document.activeElement ?? document.body;
    const name = keyName === "Return" ? "Enter" : keyName === "Esc" ? "Escape" : keyName;
    const activates = active instanceof HTMLAnchorElement || active instanceof HTMLButtonElement;
    if (name === "Enter") requireAuthorization(activates ? clickGateReasons(active) : enterGateReasons(active), authorized, active);
    const notCancelled = key(active, "keydown", name);
    if (name === "Enter" || name.length === 1) key(active, "keypress", name);
    let submitted = false;
    if (notCancelled) {
      if (name === "Enter" && isTextField(active) && !(active instanceof HTMLTextAreaElement)) {
        submitted = implicitSubmit(active);
      } else if (name === "Enter" && activates) {
        active.click();
      } else if (name === "Tab") {
        const list = focusables();
        const next = list[(list.indexOf(active) + 1) % Math.max(list.length, 1)];
        next?.focus();
      } else if (name === "Backspace" && isTextField(active)) {
        const field = active;
        setNativeValue(field, field.value.slice(0, -1));
        field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
      } else if (name === "PageDown" || name === "PageUp") {
        window.scrollBy({ top: (name === "PageDown" ? 1 : -1) * window.innerHeight * 0.85, behavior: "instant" });
      }
    }
    key(active, "keyup", name);
    return { submitted };
  }
  function selectOption(el, value, authorized) {
    if (!(el instanceof HTMLSelectElement)) {
      throw new BabasError("UNSUPPORTED", `"${labelOf(el)}" is not a <select>: click it and then click the option`);
    }
    if (isDisabled(el)) throw new BabasError("DISABLED", `"${labelOf(el)}" is disabled`);
    requireAuthorization(clickGateReasons(el), authorized, el);
    const wanted = fold(value);
    const options = Array.from(el.options);
    const match = options.find((o) => fold(o.value) === wanted) ?? options.find((o) => fold(o.text) === wanted) ?? options.find((o) => fold(o.text).startsWith(wanted)) ?? options.find((o) => fold(o.text).includes(wanted));
    if (!match) {
      const names = options.slice(0, 15).map((o) => clean(o.text, 30)).join(", ");
      throw new BabasError("NOT_FOUND", `No option "${value}". Options: ${names}`);
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    if (setter) setter.call(el, match.value);
    else el.value = match.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { selected: clean(match.text, 60) };
  }
  function scrollableAtCenter() {
    let el = deepElementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
    while (el && el !== document.body && el !== document.documentElement) {
      const style = getComputedStyle(el);
      if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 10) return el;
      el = el.parentElement;
    }
    return null;
  }
  function scroll(direction, amount = 500) {
    const delta = (direction === "up" ? -1 : 1) * Math.max(amount, 50);
    const before = window.scrollY;
    window.scrollBy({ top: delta, behavior: "instant" });
    if (window.scrollY !== before) return { scrolled: true, y: window.scrollY };
    const inner = scrollableAtCenter();
    if (inner) {
      const prev = inner.scrollTop;
      inner.scrollBy({ top: delta, behavior: "instant" });
      return { scrolled: inner.scrollTop !== prev, y: inner.scrollTop };
    }
    return { scrolled: false, y: window.scrollY };
  }
  function readText(el, maxChars = 6e3) {
    let source = el;
    if (!source) {
      const main = document.querySelector('main, [role="main"], article');
      source = main && visibleText(main).length > 200 ? main : document.body;
    }
    const text = source ? visibleText(source) : "";
    const truncated = text.length > maxChars;
    return { text: truncated ? text.slice(0, maxChars) : text, truncated };
  }
  function waitIdle(quietMs = 400, timeoutMs = 3e3) {
    return new Promise((resolve) => {
      let quietTimer;
      const done = (idle) => {
        observer.disconnect();
        clearTimeout(quietTimer);
        clearTimeout(hardTimer);
        resolve({ idle });
      };
      const observer = new MutationObserver(() => {
        clearTimeout(quietTimer);
        quietTimer = setTimeout(() => done(true), quietMs);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, characterData: true });
      quietTimer = setTimeout(() => done(true), quietMs);
      const hardTimer = setTimeout(() => done(false), timeoutMs);
    });
  }
  function mark(el) {
    unmark();
    const id = Math.random().toString(36).slice(2, 10);
    el.setAttribute(MARK_ATTR, id);
    return { mark: id };
  }
  function unmark() {
    for (const el of deepQueryAll(`[${MARK_ATTR}]`)) el.removeAttribute(MARK_ATTR);
  }
  const HOST_ID = "babas-highlight-host";
  const COLORS = ["#2563eb", "#16a34a", "#d97706", "#db2777", "#7c3aed", "#0891b2"];
  let root = null;
  let clearTimer;
  function layer() {
    let host = document.getElementById(HOST_ID);
    if (!host || !root) {
      host?.remove();
      host = document.createElement("div");
      host.id = HOST_ID;
      host.setAttribute(
        "style",
        "all: initial !important; position: fixed !important; inset: 0 !important; pointer-events: none !important; z-index: 2147483647 !important;"
      );
      root = host.attachShadow({ mode: "closed" });
      (document.body ?? document.documentElement).appendChild(host);
    }
    return root;
  }
  function clearHighlights() {
    clearTimeout(clearTimer);
    document.getElementById(HOST_ID)?.remove();
    root = null;
  }
  function box(rect, color, label, thick) {
    const el = document.createElement("div");
    el.setAttribute(
      "style",
      `position:fixed;left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;border:${thick ? 4 : 2}px solid ${color};border-radius:4px;box-sizing:border-box;background:${thick ? "rgba(250,204,21,0.18)" : "transparent"};`
    );
    if (label) {
      const tag = document.createElement("span");
      tag.textContent = label;
      tag.setAttribute(
        "style",
        `position:absolute;top:-2px;left:-2px;transform:translateY(-100%);background:${color};color:#fff;font:600 ${thick ? 14 : 11}px/1.3 system-ui,sans-serif;padding:1px 4px;border-radius:3px;white-space:nowrap;`
      );
      el.appendChild(tag);
    }
    return el;
  }
  function highlightAll(elements, ttlMs = 6e3) {
    clearHighlights();
    const shadow = layer();
    for (const [index, el] of elements) {
      const rect = visibleBox(el);
      if (!rect || rect.bottom < 0 || rect.top > window.innerHeight) continue;
      shadow.appendChild(box(rect, COLORS[index % COLORS.length], String(index), false));
    }
    clearTimer = setTimeout(clearHighlights, ttlMs);
  }
  function highlightTarget(el, label = "", ttlMs = 2500) {
    clearHighlights();
    const rect = visibleBox(el);
    if (!rect) return;
    layer().appendChild(box(rect, "#facc15", label, true));
    clearTimer = setTimeout(clearHighlights, ttlMs);
  }
  const POINTER_TAGS = /* @__PURE__ */ new Set(["div", "span", "li", "img", "svg", "td", "p", "label", "i"]);
  const CONTAINER_ROLES = /* @__PURE__ */ new Set(["button", "link", "menuitem", "option", "tab", "treeitem"]);
  const MAX_POINTER_CHECKS = 2500;
  let current = { id: "", map: /* @__PURE__ */ new Map() };
  function currentSnapshot() {
    return current;
  }
  function newSnapshotId() {
    return Math.random().toString(36).slice(2, 10);
  }
  function isCandidate(el) {
    if (el.matches(INTERACTIVE_SELECTOR)) {
      if (el.matches("[tabindex]") && !el.matches("a[href],button,input,select,textarea,summary,[role],[onclick],[contenteditable]")) {
        return getComputedStyle(el).cursor === "pointer" && visibleText(el).length > 0;
      }
      return true;
    }
    return false;
  }
  function isPointerCandidate(el) {
    if (!POINTER_TAGS.has(el.tagName.toLowerCase())) return false;
    if (getComputedStyle(el).cursor !== "pointer") return false;
    const parent = el.parentElement;
    if (parent && getComputedStyle(parent).cursor === "pointer") return false;
    return visibleText(el).length > 0 || el.tagName.toLowerCase() === "img";
  }
  function nestedDuplicate(el, chosen) {
    if (isTextField(el) || el instanceof HTMLSelectElement || isContentEditable(el)) return false;
    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 6; depth += 1) {
      if (chosen.has(parent) && CONTAINER_ROLES.has(roleOf(parent))) return true;
      parent = parent.parentElement;
    }
    return false;
  }
  function describeElement(el, index) {
    const label = labelOf(el);
    const role = roleOf(el);
    const tag = el.tagName.toLowerCase();
    const info = {
      index,
      tag,
      role,
      label,
      text: "",
      flags: flagsFor(el, label),
      in_viewport: (() => {
        const box2 = visibleBox(el);
        return box2 ? inViewport(box2) : false;
      })()
    };
    const text = clean(visibleText(el), 80);
    if (text && text !== label && !isTextField(el)) info.text = text;
    const type = inputType(el);
    if (type) info.type = type;
    if (el instanceof HTMLAnchorElement && el.href) info.href = clean(el.getAttribute("href"), 100);
    if (el instanceof HTMLInputElement && (type === "checkbox" || type === "radio")) info.checked = el.checked;
    else if (el.getAttribute("aria-checked")) info.checked = el.getAttribute("aria-checked") === "true";
    if (el instanceof HTMLSelectElement) {
      info.options = Array.from(el.options).slice(0, 25).map((o) => clean(o.text, 40));
      const selected = el.selectedOptions[0];
      if (selected) info.value = clean(selected.text, 60);
    } else if (isTextField(el) && !isPasswordField(el)) {
      info.value = clean(el.value, 80);
    } else if (isContentEditable(el)) {
      info.value = clean(visibleText(el), 80);
    }
    return info;
  }
  function collectInteractive() {
    const chosen = /* @__PURE__ */ new Set();
    const ordered = [];
    let pointerChecks = 0;
    for (const el of allElementsDeep()) {
      let take = false;
      if (isCandidate(el)) {
        take = true;
      } else if (pointerChecks < MAX_POINTER_CHECKS && POINTER_TAGS.has(el.tagName.toLowerCase())) {
        pointerChecks += 1;
        take = isPointerCandidate(el);
      }
      if (!take || !visibleBox(el)) continue;
      if (nestedDuplicate(el, chosen)) continue;
      chosen.add(el);
      ordered.push(el);
    }
    return ordered;
  }
  function pageText(maxChars) {
    const main = document.querySelector('main, [role="main"], article');
    const source = main && visibleText(main).length > 200 ? main : document.body;
    if (!source) return "";
    const text = visibleText(source);
    return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text;
  }
  function takeSnapshot(maxElements = 250, maxText = 2e3) {
    const all = collectInteractive();
    let picked = all;
    let truncated = false;
    if (all.length > maxElements) {
      truncated = true;
      const visibleNow = new Set(
        all.filter((el) => {
          const box2 = visibleBox(el);
          return box2 ? inViewport(box2) : false;
        })
      );
      const rest = all.filter((el) => !visibleNow.has(el));
      const keep = new Set([...visibleNow].slice(0, maxElements));
      for (const el of rest) {
        if (keep.size >= maxElements) break;
        keep.add(el);
      }
      picked = all.filter((el) => keep.has(el));
    }
    const map = /* @__PURE__ */ new Map();
    const elements = picked.map((el, i) => {
      map.set(i, el);
      return describeElement(el, i);
    });
    current = { id: newSnapshotId(), map };
    return {
      snapshot_id: current.id,
      url: location.href,
      title: document.title,
      elements,
      text: pageText(maxText),
      has_captcha: !!document.querySelector(CAPTCHA_SELECTOR),
      has_password: elements.some((e) => e.flags.includes("password")),
      truncated
    };
  }
  function unquote(raw) {
    const s = raw.trim();
    if (s.startsWith("'") && s.endsWith("'") || s.startsWith('"') && s.endsWith('"')) {
      return { value: s.slice(1, -1), quoted: true };
    }
    return { value: s, quoted: false };
  }
  function parseSelector(selector) {
    const sel = selector.trim();
    if (sel.startsWith("text=")) {
      const { value, quoted } = unquote(sel.slice(5));
      return { css: "*", text: value, exact: quoted };
    }
    const hasText = sel.match(/^(.*?):has-text\((.*)\)\s*$/);
    if (hasText) {
      const { value } = unquote(hasText[2]);
      return { css: hasText[1].trim() || "*", text: value, exact: false };
    }
    return { css: sel };
  }
  function deepest(elements) {
    return elements.filter((el) => !elements.some((other) => other !== el && el.contains(other)));
  }
  function preferVisible(elements) {
    const visible = elements.filter((el) => visibleBox(el));
    const inView = visible.filter((el) => {
      const box2 = visibleBox(el);
      return box2 ? inViewport(box2) : false;
    });
    return inView[0] ?? visible[0] ?? elements[0] ?? null;
  }
  function matchesText(el, wanted, exact) {
    const own = fold(visibleText(el));
    const label = fold(labelOf(el));
    const w = fold(wanted);
    if (exact) return own === w || label === w;
    return own.includes(w) || label.includes(w);
  }
  function bySelector(selector) {
    const parsed = parseSelector(selector);
    let candidates;
    try {
      candidates = deepQueryAll(parsed.css);
    } catch {
      throw new BabasError("BAD_REQUEST", `Invalid selector: ${selector}`);
    }
    if (parsed.text !== void 0) {
      candidates = candidates.filter((el2) => isVisible(el2) && matchesText(el2, parsed.text, !!parsed.exact));
      if (parsed.css === "*") candidates = deepest(candidates);
    }
    const el = preferVisible(candidates);
    if (!el) throw new BabasError("NOT_FOUND", `No element matches ${selector}`);
    return el;
  }
  function byText(text) {
    const wanted = fold(text);
    if (!wanted) throw new BabasError("BAD_REQUEST", "Empty text target");
    const interactive = collectInteractive();
    const exact = interactive.filter((el) => fold(labelOf(el)) === wanted || fold(visibleText(el)) === wanted);
    const starts = interactive.filter((el) => fold(labelOf(el)).startsWith(wanted));
    const contains = interactive.filter((el) => fold(labelOf(el)).includes(wanted) || fold(visibleText(el)).includes(wanted));
    const best = preferVisible(exact) ?? preferVisible(starts) ?? preferVisible(contains);
    if (best) return best;
    const any = deepest(allElementsDeep().filter((el) => isVisible(el) && fold(visibleText(el)).includes(wanted)));
    const fallback = preferVisible(any);
    if (!fallback) throw new BabasError("NOT_FOUND", `No visible element with text "${text}"`);
    return fallback;
  }
  function resolveTarget(target) {
    if (target.index !== void 0 && target.index !== null) {
      const snap = currentSnapshot();
      if (!snap.id) throw new BabasError("STALE_SNAPSHOT", "No snapshot yet: observe the page first");
      if (target.snapshot_id && target.snapshot_id !== snap.id) {
        throw new BabasError("STALE_SNAPSHOT", "The page changed since that snapshot: observe it again");
      }
      const el = snap.map.get(target.index);
      if (!el) throw new BabasError("NOT_FOUND", `Element [${target.index}] does not exist in the current snapshot`);
      if (!el.isConnected) throw new BabasError("STALE_SNAPSHOT", "The page changed since that snapshot: observe it again");
      return el;
    }
    if (target.selector) return bySelector(target.selector);
    if (target.text) return byText(target.text);
    throw new BabasError("BAD_REQUEST", "Target needs index, selector or text");
  }
  function tryResolve(target) {
    try {
      return resolveTarget(target);
    } catch {
      return null;
    }
  }
  async function run(cmd) {
    switch (cmd.op) {
      case "ping":
        return { pong: true, url: location.href };
      case "snapshot": {
        const state = takeSnapshot(cmd.max_elements ?? 250, cmd.max_text ?? 2e3);
        if (cmd.highlight) highlightAll(currentSnapshot().map);
        return state;
      }
      case "describe": {
        const el = resolveTarget(cmd.target);
        const index = cmd.target.index ?? -1;
        return { element: describeElement(el, index), host: location.hostname, url: location.href };
      }
      case "exists": {
        const el = tryResolve(cmd.target);
        return { exists: !!el, visible: !!(el && visibleBox(el)) };
      }
      case "click": {
        const el = resolveTarget(cmd.target);
        highlightTarget(el);
        return click(el, cmd.authorized);
      }
      case "type": {
        const el = resolveTarget(cmd.target);
        highlightTarget(el);
        return typeText(el, cmd.text, cmd.authorized);
      }
      case "press": {
        const el = cmd.target ? resolveTarget(cmd.target) : null;
        return press(cmd.key, el, cmd.authorized);
      }
      case "select": {
        const el = resolveTarget(cmd.target);
        highlightTarget(el);
        return selectOption(el, cmd.value, cmd.authorized);
      }
      case "scroll":
        return scroll(cmd.direction, cmd.amount);
      case "read":
        return readText(cmd.target ? resolveTarget(cmd.target) : null, cmd.max_chars);
      case "highlight":
        if (cmd.mode === "off") clearHighlights();
        else if (cmd.mode === "all") highlightAll(currentSnapshot().map);
        else if (cmd.target) highlightTarget(resolveTarget(cmd.target));
        return { mode: cmd.mode };
      case "idle":
        return waitIdle(cmd.quiet_ms, cmd.timeout_ms);
      case "mark":
        return mark(resolveTarget(cmd.target));
      case "unmark":
        unmark();
        return {};
      default:
        throw new BabasError("BAD_REQUEST", `Unknown page op: ${cmd.op}`);
    }
  }
  async function handle(cmd) {
    try {
      return { ok: true, result: await run(cmd) };
    } catch (err) {
      return { ok: false, error: toErrorInfo(err) };
    }
  }
  if (!window.__babas) {
    window.__babas = { version: 1, handle };
  }
})();
