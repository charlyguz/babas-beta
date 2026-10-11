import { BabasError } from '../shared/protocol';
import { clickGateReasons, enterGateReasons, isPasswordField, typeGateReasons, type GateReason } from './classify';
import {
  composedContains,
  deepElementFromPoint,
  deepQueryAll,
  formOf,
  isContentEditable,
  isDisabled,
  isHTMLElement,
  isTextField,
  isVisible,
  labelOf,
  visibleBox,
  visibleText,
} from './dom';
import { clean, fold } from './text';

const MARK_ATTR = 'data-babas-mark';

function requireAuthorization(reasons: GateReason[], authorized: boolean | undefined, el: HTMLElement | null): void {
  if (reasons.length === 0 || authorized) return;
  throw new BabasError('CONFIRMATION_REQUIRED', 'This action needs the user to confirm it first', {
    reasons,
    label: el ? labelOf(el) : '',
    host: location.hostname,
  });
}

function center(el: HTMLElement): { x: number; y: number } {
  const box = visibleBox(el) ?? el.getBoundingClientRect();
  return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
}

function hitAllowed(el: HTMLElement, hit: Element | null): boolean {
  if (!hit) return true;
  if (composedContains(el, hit) || composedContains(hit, el)) return true;
  const label = hit.closest('label');
  if (label && (label as HTMLLabelElement).control === el) return true;
  if (el instanceof HTMLInputElement && el.labels) {
    return Array.from(el.labels).some((l) => composedContains(l, hit));
  }
  return false;
}

function dispatchPointer(el: HTMLElement, type: string, x: number, y: number): void {
  const init = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, button: 0, view: window };
  if (type.startsWith('pointer') && typeof PointerEvent === 'function') {
    el.dispatchEvent(new PointerEvent(type, { ...init, pointerId: 1, pointerType: 'mouse', isPrimary: true }));
  } else {
    el.dispatchEvent(new MouseEvent(type, init));
  }
}

export function click(el: HTMLElement, authorized?: boolean): { label: string } {
  if (isDisabled(el)) throw new BabasError('DISABLED', `"${labelOf(el)}" is disabled`);
  requireAuthorization(clickGateReasons(el), authorized, el);
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' as ScrollBehavior });
  if (!visibleBox(el)) throw new BabasError('NOT_VISIBLE', `"${labelOf(el)}" is not visible`);
  const { x, y } = center(el);
  const hit = deepElementFromPoint(x, y);
  if (!hitAllowed(el, hit)) {
    const blocker = isHTMLElement(hit) ? clean(labelOf(hit) || visibleText(hit), 60) : '';
    throw new BabasError('OBSCURED', `Another element covers the target${blocker ? `: "${blocker}"` : ''}. Close it first.`, {
      blocker,
    });
  }
  for (const type of ['pointerover', 'mouseover', 'pointerdown', 'mousedown']) dispatchPointer(el, type, x, y);
  if (typeof el.focus === 'function') el.focus({ preventScroll: true });
  for (const type of ['pointerup', 'mouseup']) dispatchPointer(el, type, x, y);
  el.click();
  return { label: labelOf(el) };
}

function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
}

function normalizeForType(el: HTMLInputElement | HTMLTextAreaElement, text: string): string {
  if (el instanceof HTMLInputElement && el.type === 'date') {
    const m = text.trim().match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/);
    if (m) return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  }
  return text;
}

function key(el: HTMLElement, type: 'keydown' | 'keypress' | 'keyup', keyName: string): boolean {
  const code = keyName.length === 1 ? `Key${keyName.toUpperCase()}` : keyName;
  const keyCode = keyName === 'Enter' ? 13 : keyName === 'Escape' ? 27 : keyName === 'Tab' ? 9 : keyName.length === 1 ? keyName.toUpperCase().charCodeAt(0) : 0;
  return el.dispatchEvent(
    new KeyboardEvent(type, { key: keyName, code, keyCode, which: keyCode, bubbles: true, cancelable: true, composed: true } as KeyboardEventInit),
  );
}

export function typeText(el: HTMLElement, text: string, authorized?: boolean): { label: string } {
  if (isPasswordField(el)) {
    throw new BabasError('PASSWORD_FIELD', 'BABAS never types passwords: ask the user to type it');
  }
  if (isDisabled(el)) throw new BabasError('DISABLED', `"${labelOf(el)}" is disabled`);
  if (!isTextField(el) && !isContentEditable(el)) {
    throw new BabasError('NOT_EDITABLE', `"${labelOf(el)}" is not a text field`);
  }
  requireAuthorization(typeGateReasons(el, text), authorized, el);
  el.scrollIntoView({ block: 'center', behavior: 'instant' as ScrollBehavior });
  el.focus({ preventScroll: true });
  if (isContentEditable(el) && !isTextField(el)) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    selection?.removeAllRanges();
    selection?.addRange(range);
    const inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, text);
    if (!inserted || fold(visibleText(el)) !== fold(text)) {
      el.textContent = text;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
    }
    return { label: labelOf(el) };
  }
  const field = el as HTMLInputElement | HTMLTextAreaElement;
  const value = normalizeForType(field, text);
  key(field, 'keydown', value.slice(-1) || 'Unidentified');
  setNativeValue(field, value);
  field.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: value }));
  key(field, 'keyup', value.slice(-1) || 'Unidentified');
  field.dispatchEvent(new Event('change', { bubbles: true }));
  if (field.value !== value && !(field instanceof HTMLInputElement && field.type === 'number')) {
    throw new BabasError('NOT_EDITABLE', `The field rejected the value (expected format? got "${clean(field.value, 40)}")`);
  }
  return { label: labelOf(el) };
}

function implicitSubmit(field: HTMLElement): boolean {
  const form = formOf(field);
  if (!form) return false;
  const submitter = Array.from(form.elements).find(
    (e) =>
      (e instanceof HTMLButtonElement && (e.getAttribute('type') || 'submit').toLowerCase() === 'submit') ||
      (e instanceof HTMLInputElement && (e.type === 'submit' || e.type === 'image')),
  ) as HTMLButtonElement | HTMLInputElement | undefined;
  const textFields = Array.from(form.elements).filter((e) => isHTMLElement(e) && isTextField(e));
  if (!submitter && textFields.length > 1) return false;
  if (submitter && (submitter as HTMLButtonElement).disabled) return false;
  if (typeof form.requestSubmit === 'function') form.requestSubmit(submitter);
  else form.submit();
  return true;
}

function focusables(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>('a[href], button, input:not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"]), [contenteditable="true"]'),
  ).filter((el) => isVisible(el) && !isDisabled(el));
}

export function press(keyName: string, el: HTMLElement | null, authorized?: boolean): { submitted: boolean } {
  if (el) {
    el.scrollIntoView({ block: 'center', behavior: 'instant' as ScrollBehavior });
    el.focus({ preventScroll: true });
  }
  const active = (el ?? (document.activeElement as HTMLElement | null) ?? document.body) as HTMLElement;
  const name = keyName === 'Return' ? 'Enter' : keyName === 'Esc' ? 'Escape' : keyName;
  const activates = active instanceof HTMLAnchorElement || active instanceof HTMLButtonElement;
  if (name === 'Enter') requireAuthorization(activates ? clickGateReasons(active) : enterGateReasons(active), authorized, active);
  const notCancelled = key(active, 'keydown', name);
  if (name === 'Enter' || name.length === 1) key(active, 'keypress', name);
  let submitted = false;
  if (notCancelled) {
    if (name === 'Enter' && isTextField(active) && !(active instanceof HTMLTextAreaElement)) {
      submitted = implicitSubmit(active);
    } else if (name === 'Enter' && activates) {
      active.click();
    } else if (name === 'Tab') {
      const list = focusables();
      const next = list[(list.indexOf(active) + 1) % Math.max(list.length, 1)];
      next?.focus();
    } else if (name === 'Backspace' && isTextField(active)) {
      const field = active as HTMLInputElement;
      setNativeValue(field, field.value.slice(0, -1));
      field.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
    } else if (name === 'PageDown' || name === 'PageUp') {
      window.scrollBy({ top: (name === 'PageDown' ? 1 : -1) * window.innerHeight * 0.85, behavior: 'instant' as ScrollBehavior });
    }
  }
  key(active, 'keyup', name);
  return { submitted };
}

export function selectOption(el: HTMLElement, value: string, authorized?: boolean): { selected: string } {
  if (!(el instanceof HTMLSelectElement)) {
    throw new BabasError('UNSUPPORTED', `"${labelOf(el)}" is not a <select>: click it and then click the option`);
  }
  if (isDisabled(el)) throw new BabasError('DISABLED', `"${labelOf(el)}" is disabled`);
  requireAuthorization(clickGateReasons(el), authorized, el);
  const wanted = fold(value);
  const options = Array.from(el.options);
  const match =
    options.find((o) => fold(o.value) === wanted) ??
    options.find((o) => fold(o.text) === wanted) ??
    options.find((o) => fold(o.text).startsWith(wanted)) ??
    options.find((o) => fold(o.text).includes(wanted));
  if (!match) {
    const names = options.slice(0, 15).map((o) => clean(o.text, 30)).join(', ');
    throw new BabasError('NOT_FOUND', `No option "${value}". Options: ${names}`);
  }
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  if (setter) setter.call(el, match.value);
  else el.value = match.value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { selected: clean(match.text, 60) };
}

function scrollableAtCenter(): HTMLElement | null {
  let el = deepElementFromPoint(window.innerWidth / 2, window.innerHeight / 2) as HTMLElement | null;
  while (el && el !== document.body && el !== document.documentElement) {
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 10) return el;
    el = el.parentElement;
  }
  return null;
}

export function scroll(direction: 'up' | 'down', amount = 500): { scrolled: boolean; y: number } {
  const delta = (direction === 'up' ? -1 : 1) * Math.max(amount, 50);
  const before = window.scrollY;
  window.scrollBy({ top: delta, behavior: 'instant' as ScrollBehavior });
  if (window.scrollY !== before) return { scrolled: true, y: window.scrollY };
  const inner = scrollableAtCenter();
  if (inner) {
    const prev = inner.scrollTop;
    inner.scrollBy({ top: delta, behavior: 'instant' as ScrollBehavior });
    return { scrolled: inner.scrollTop !== prev, y: inner.scrollTop };
  }
  return { scrolled: false, y: window.scrollY };
}

export function readText(el: HTMLElement | null, maxChars = 6000): { text: string; truncated: boolean } {
  let source: HTMLElement | null = el;
  if (!source) {
    const main = document.querySelector('main, [role="main"], article') as HTMLElement | null;
    source = main && visibleText(main).length > 200 ? main : document.body;
  }
  const text = source ? visibleText(source) : '';
  const truncated = text.length > maxChars;
  return { text: truncated ? text.slice(0, maxChars) : text, truncated };
}

export function waitIdle(quietMs = 400, timeoutMs = 3000): Promise<{ idle: boolean }> {
  return new Promise((resolve) => {
    let quietTimer: ReturnType<typeof setTimeout> | undefined;
    const done = (idle: boolean) => {
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

export function mark(el: HTMLElement): { mark: string } {
  unmark();
  const id = Math.random().toString(36).slice(2, 10);
  el.setAttribute(MARK_ATTR, id);
  return { mark: id };
}

export function unmark(): void {
  for (const el of deepQueryAll(`[${MARK_ATTR}]`)) el.removeAttribute(MARK_ATTR);
}
