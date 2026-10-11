import type { ElementInfo, PageState } from '../shared/protocol';
import { CAPTCHA_SELECTOR, flagsFor, isPasswordField } from './classify';
import {
  INTERACTIVE_SELECTOR,
  allElementsDeep,
  inViewport,
  inputType,
  isContentEditable,
  isTextField,
  labelOf,
  roleOf,
  visibleBox,
  visibleText,
} from './dom';
import { clean } from './text';

const POINTER_TAGS = new Set(['div', 'span', 'li', 'img', 'svg', 'td', 'p', 'label', 'i']);
const CONTAINER_ROLES = new Set(['button', 'link', 'menuitem', 'option', 'tab', 'treeitem']);
const MAX_POINTER_CHECKS = 2500;

export interface Snapshot {
  id: string;
  map: Map<number, HTMLElement>;
}

let current: Snapshot = { id: '', map: new Map() };

export function currentSnapshot(): Snapshot {
  return current;
}

function newSnapshotId(): string {
  return Math.random().toString(36).slice(2, 10);
}

function isCandidate(el: HTMLElement): boolean {
  if (el.matches(INTERACTIVE_SELECTOR)) {
    if (el.matches('[tabindex]') && !el.matches('a[href],button,input,select,textarea,summary,[role],[onclick],[contenteditable]')) {
      return getComputedStyle(el).cursor === 'pointer' && visibleText(el).length > 0;
    }
    return true;
  }
  return false;
}

function isPointerCandidate(el: HTMLElement): boolean {
  if (!POINTER_TAGS.has(el.tagName.toLowerCase())) return false;
  if (getComputedStyle(el).cursor !== 'pointer') return false;
  const parent = el.parentElement;
  if (parent && getComputedStyle(parent).cursor === 'pointer') return false;
  return visibleText(el).length > 0 || el.tagName.toLowerCase() === 'img';
}

function nestedDuplicate(el: HTMLElement, chosen: Set<HTMLElement>): boolean {
  if (isTextField(el) || el instanceof HTMLSelectElement || isContentEditable(el)) return false;
  let parent = el.parentElement;
  for (let depth = 0; parent && depth < 6; depth += 1) {
    if (chosen.has(parent) && CONTAINER_ROLES.has(roleOf(parent))) return true;
    parent = parent.parentElement;
  }
  return false;
}

export function describeElement(el: HTMLElement, index: number): ElementInfo {
  const label = labelOf(el);
  const role = roleOf(el);
  const tag = el.tagName.toLowerCase();
  const info: ElementInfo = {
    index,
    tag,
    role,
    label,
    text: '',
    flags: flagsFor(el, label),
    in_viewport: (() => {
      const box = visibleBox(el);
      return box ? inViewport(box) : false;
    })(),
  };
  const text = clean(visibleText(el), 80);
  if (text && text !== label && !isTextField(el)) info.text = text;
  const type = inputType(el);
  if (type) info.type = type;
  if (el instanceof HTMLAnchorElement && el.href) info.href = clean(el.getAttribute('href'), 100);
  if (el instanceof HTMLInputElement && (type === 'checkbox' || type === 'radio')) info.checked = el.checked;
  else if (el.getAttribute('aria-checked')) info.checked = el.getAttribute('aria-checked') === 'true';
  if (el instanceof HTMLSelectElement) {
    info.options = Array.from(el.options)
      .slice(0, 25)
      .map((o) => clean(o.text, 40));
    const selected = el.selectedOptions[0];
    if (selected) info.value = clean(selected.text, 60);
  } else if (isTextField(el) && !isPasswordField(el)) {
    info.value = clean((el as HTMLInputElement).value, 80);
  } else if (isContentEditable(el)) {
    info.value = clean(visibleText(el), 80);
  }
  return info;
}

export function collectInteractive(): HTMLElement[] {
  const chosen = new Set<HTMLElement>();
  const ordered: HTMLElement[] = [];
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

function pageText(maxChars: number): string {
  const main = document.querySelector('main, [role="main"], article');
  const source = main && visibleText(main as HTMLElement).length > 200 ? (main as HTMLElement) : document.body;
  if (!source) return '';
  const text = visibleText(source);
  return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text;
}

export function takeSnapshot(maxElements = 250, maxText = 2000): PageState {
  const all = collectInteractive();
  let picked = all;
  let truncated = false;
  if (all.length > maxElements) {
    truncated = true;
    const visibleNow = new Set(
      all.filter((el) => {
        const box = visibleBox(el);
        return box ? inViewport(box) : false;
      }),
    );
    const rest = all.filter((el) => !visibleNow.has(el));
    const keep = new Set([...visibleNow].slice(0, maxElements));
    for (const el of rest) {
      if (keep.size >= maxElements) break;
      keep.add(el);
    }
    picked = all.filter((el) => keep.has(el));
  }
  const map = new Map<number, HTMLElement>();
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
    has_password: elements.some((e) => e.flags.includes('password')),
    truncated,
  };
}
