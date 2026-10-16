import { clean } from './text';

export type FieldElement = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

const TEXT_INPUT_TYPES = new Set(['', 'text', 'search', 'email', 'tel', 'url', 'number', 'password', 'date', 'datetime-local', 'month', 'week', 'time']);

export const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  'summary',
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
  '[onclick]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export function isHTMLElement(node: unknown): node is HTMLElement {
  return typeof node === 'object' && node !== null && (node as Node).nodeType === 1 && 'style' in (node as object);
}

export function isTextField(el: Element): el is HTMLInputElement | HTMLTextAreaElement {
  if (el instanceof HTMLTextAreaElement) return true;
  return el instanceof HTMLInputElement && TEXT_INPUT_TYPES.has((el.getAttribute('type') || '').toLowerCase());
}

export function isContentEditable(el: Element): boolean {
  return isHTMLElement(el) && (el.isContentEditable || ['', 'true', 'plaintext-only'].includes(el.getAttribute('contenteditable') ?? 'x'));
}

export function isEditable(el: Element): boolean {
  return isTextField(el) || isContentEditable(el);
}

export function inputType(el: Element): string {
  if (el instanceof HTMLInputElement) return (el.getAttribute('type') || 'text').toLowerCase();
  return '';
}

export function deepQueryAll(selector: string, root: Document | ShadowRoot = document): HTMLElement[] {
  const out: HTMLElement[] = [];
  const visit = (scope: Document | ShadowRoot) => {
    scope.querySelectorAll(selector).forEach((el) => {
      if (isHTMLElement(el)) out.push(el);
    });
    scope.querySelectorAll('*').forEach((el) => {
      if (el.shadowRoot) visit(el.shadowRoot);
    });
  };
  visit(root);
  return out;
}

export function allElementsDeep(root: Document | ShadowRoot = document): HTMLElement[] {
  const out: HTMLElement[] = [];
  const visit = (scope: Document | ShadowRoot) => {
    scope.querySelectorAll('*').forEach((el) => {
      if (isHTMLElement(el)) out.push(el);
      if (el.shadowRoot) visit(el.shadowRoot);
    });
  };
  visit(root);
  return out;
}

export function isVisible(el: Element): boolean {
  if (!isHTMLElement(el) || !el.isConnected) return false;
  if (el.closest('[inert]')) return false;
  const style = getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  const check = (el as HTMLElement & { checkVisibility?: (o?: object) => boolean }).checkVisibility;
  if (typeof check === 'function' && !check.call(el, { checkVisibilityCSS: true })) return false;
  const rect = el.getBoundingClientRect();
  return rect.width >= 1 && rect.height >= 1;
}

export function visibleBox(el: HTMLElement): DOMRect | null {
  if (isVisible(el)) return el.getBoundingClientRect();
  if (el instanceof HTMLInputElement && (el.type === 'checkbox' || el.type === 'radio') && el.labels) {
    for (const label of Array.from(el.labels)) {
      if (isVisible(label)) return label.getBoundingClientRect();
    }
  }
  return null;
}

export function inViewport(rect: DOMRect): boolean {
  return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
}

export function isDisabled(el: HTMLElement): boolean {
  if ((el as HTMLButtonElement).disabled) return true;
  if (el.getAttribute('aria-disabled') === 'true') return true;
  return !!el.closest('fieldset[disabled]');
}

const IMPLICIT_ROLES: Record<string, string> = {
  a: 'link',
  button: 'button',
  select: 'combobox',
  textarea: 'textbox',
  summary: 'button',
  img: 'img',
  option: 'option',
};

export function roleOf(el: HTMLElement): string {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit.trim().split(/\s+/)[0];
  const tag = el.tagName.toLowerCase();
  if (tag === 'input') {
    const type = inputType(el);
    if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    if (type === 'search') return 'searchbox';
    return 'textbox';
  }
  if (isContentEditable(el)) return 'textbox';
  return IMPLICIT_ROLES[tag] ?? 'generic';
}

export function visibleText(el: HTMLElement): string {
  const text = typeof el.innerText === 'string' ? el.innerText : el.textContent ?? '';
  return text.replace(/\s+/g, ' ').trim();
}

function textWithoutControls(label: HTMLElement): string {
  const copy = label.cloneNode(true) as HTMLElement;
  copy.querySelectorAll('input, select, textarea, button').forEach((n) => n.remove());
  return (copy.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function nearbyText(el: HTMLElement): string {
  const short = (node: Element | null): string => {
    if (!node || !isHTMLElement(node)) return '';
    if (node.querySelector('input, select, textarea, button')) return '';
    const text = visibleText(node);
    return text.length > 0 && text.length <= 60 ? text : '';
  };
  const prev = short(el.previousElementSibling);
  if (prev) return prev;
  const cell = el.closest('td, th, div, li, p');
  if (cell && cell !== el) {
    const before = short(cell.previousElementSibling);
    if (before) return before;
  }
  return '';
}

export function labelOf(el: HTMLElement): string {
  const aria = el.getAttribute('aria-label');
  if (aria && aria.trim()) return clean(aria);
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy) {
    const root = el.getRootNode() as Document | ShadowRoot;
    const text = labelledBy
      .split(/\s+/)
      .map((id) => (root.getElementById ? root.getElementById(id) : document.getElementById(id))?.textContent ?? '')
      .join(' ');
    if (text.trim()) return clean(text);
  }
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
    const type = inputType(el);
    if (el instanceof HTMLInputElement && ['submit', 'button', 'reset'].includes(type)) {
      return clean(el.value || el.getAttribute('title') || type);
    }
    if (el instanceof HTMLInputElement && type === 'image') return clean(el.alt || el.title || 'image');
    const labels = el.labels ? Array.from(el.labels) : [];
    const fromLabels = labels.map((l) => textWithoutControls(l)).join(' ');
    if (fromLabels.trim()) return clean(fromLabels);
    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return clean(placeholder);
    const title = el.getAttribute('title');
    if (title && title.trim()) return clean(title);
    const near = nearbyText(el);
    if (near) return clean(near);
    return clean(el.getAttribute('name') || el.id || '');
  }
  if (el instanceof HTMLImageElement) return clean(el.alt || el.title);
  const text = visibleText(el);
  if (text) return clean(text);
  const title = el.getAttribute('title');
  if (title && title.trim()) return clean(title);
  const img = el.querySelector('img[alt]');
  if (img && img.getAttribute('alt')) return clean(img.getAttribute('alt'));
  const svgTitle = el.querySelector('svg title');
  if (svgTitle && svgTitle.textContent) return clean(svgTitle.textContent);
  return '';
}

export function deepElementFromPoint(x: number, y: number): Element | null {
  let el: Element | null = document.elementFromPoint(x, y);
  while (el && el.shadowRoot) {
    const inner = el.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === el) break;
    el = inner;
  }
  return el;
}

export function composedContains(ancestor: Element, node: Element): boolean {
  let cur: Node | null = node;
  while (cur) {
    if (cur === ancestor) return true;
    const parent: Node | null = (cur as Element).assignedSlot ?? cur.parentNode;
    cur = parent instanceof ShadowRoot ? parent.host : parent;
  }
  return false;
}

export function formOf(el: HTMLElement): HTMLFormElement | null {
  const owner = (el as HTMLInputElement).form;
  if (owner instanceof HTMLFormElement) return owner;
  return el.closest('form');
}
