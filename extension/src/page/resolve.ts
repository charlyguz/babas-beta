import { BabasError, type Target } from '../shared/protocol';
import { deepQueryAll, isVisible, labelOf, visibleBox, visibleText, allElementsDeep, inViewport } from './dom';
import { collectInteractive, currentSnapshot } from './snapshot';
import { fold } from './text';

interface ParsedSelector {
  css: string;
  text?: string;
  exact?: boolean;
}

function unquote(raw: string): { value: string; quoted: boolean } {
  const s = raw.trim();
  if ((s.startsWith("'") && s.endsWith("'")) || (s.startsWith('"') && s.endsWith('"'))) {
    return { value: s.slice(1, -1), quoted: true };
  }
  return { value: s, quoted: false };
}

export function parseSelector(selector: string): ParsedSelector {
  const sel = selector.trim();
  if (sel.startsWith('text=')) {
    const { value, quoted } = unquote(sel.slice(5));
    return { css: '*', text: value, exact: quoted };
  }
  const hasText = sel.match(/^(.*?):has-text\((.*)\)\s*$/);
  if (hasText) {
    const { value } = unquote(hasText[2]);
    return { css: hasText[1].trim() || '*', text: value, exact: false };
  }
  return { css: sel };
}

function deepest(elements: HTMLElement[]): HTMLElement[] {
  return elements.filter((el) => !elements.some((other) => other !== el && el.contains(other)));
}

function preferVisible(elements: HTMLElement[]): HTMLElement | null {
  const visible = elements.filter((el) => visibleBox(el));
  const inView = visible.filter((el) => {
    const box = visibleBox(el);
    return box ? inViewport(box) : false;
  });
  return inView[0] ?? visible[0] ?? elements[0] ?? null;
}

function matchesText(el: HTMLElement, wanted: string, exact: boolean): boolean {
  const own = fold(visibleText(el));
  const label = fold(labelOf(el));
  const w = fold(wanted);
  if (exact) return own === w || label === w;
  return own.includes(w) || label.includes(w);
}

export function bySelector(selector: string): HTMLElement {
  const parsed = parseSelector(selector);
  let candidates: HTMLElement[];
  try {
    candidates = deepQueryAll(parsed.css);
  } catch {
    throw new BabasError('BAD_REQUEST', `Invalid selector: ${selector}`);
  }
  if (parsed.text !== undefined) {
    candidates = candidates.filter((el) => isVisible(el) && matchesText(el, parsed.text as string, !!parsed.exact));
    if (parsed.css === '*') candidates = deepest(candidates);
  }
  const el = preferVisible(candidates);
  if (!el) throw new BabasError('NOT_FOUND', `No element matches ${selector}`);
  return el;
}

export function byText(text: string): HTMLElement {
  const wanted = fold(text);
  if (!wanted) throw new BabasError('BAD_REQUEST', 'Empty text target');
  const interactive = collectInteractive();
  const exact = interactive.filter((el) => fold(labelOf(el)) === wanted || fold(visibleText(el)) === wanted);
  const starts = interactive.filter((el) => fold(labelOf(el)).startsWith(wanted));
  const contains = interactive.filter((el) => fold(labelOf(el)).includes(wanted) || fold(visibleText(el)).includes(wanted));
  const best = preferVisible(exact) ?? preferVisible(starts) ?? preferVisible(contains);
  if (best) return best;
  const any = deepest(allElementsDeep().filter((el) => isVisible(el) && fold(visibleText(el)).includes(wanted)));
  const fallback = preferVisible(any);
  if (!fallback) throw new BabasError('NOT_FOUND', `No visible element with text "${text}"`);
  return fallback;
}

export function resolveTarget(target: Target): HTMLElement {
  if (target.index !== undefined && target.index !== null) {
    const snap = currentSnapshot();
    if (!snap.id) throw new BabasError('STALE_SNAPSHOT', 'No snapshot yet: observe the page first');
    if (target.snapshot_id && target.snapshot_id !== snap.id) {
      throw new BabasError('STALE_SNAPSHOT', 'The page changed since that snapshot: observe it again');
    }
    const el = snap.map.get(target.index);
    if (!el) throw new BabasError('NOT_FOUND', `Element [${target.index}] does not exist in the current snapshot`);
    if (!el.isConnected) throw new BabasError('STALE_SNAPSHOT', 'The page changed since that snapshot: observe it again');
    return el;
  }
  if (target.selector) return bySelector(target.selector);
  if (target.text) return byText(target.text);
  throw new BabasError('BAD_REQUEST', 'Target needs index, selector or text');
}

export function tryResolve(target: Target): HTMLElement | null {
  try {
    return resolveTarget(target);
  } catch {
    return null;
  }
}
