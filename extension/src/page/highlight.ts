import { visibleBox } from './dom';

const HOST_ID = 'babas-highlight-host';
const COLORS = ['#2563eb', '#16a34a', '#d97706', '#db2777', '#7c3aed', '#0891b2'];

let root: ShadowRoot | null = null;
let clearTimer: ReturnType<typeof setTimeout> | undefined;

function layer(): ShadowRoot {
  let host = document.getElementById(HOST_ID);
  if (!host || !root) {
    host?.remove();
    host = document.createElement('div');
    host.id = HOST_ID;
    host.setAttribute(
      'style',
      'all: initial !important; position: fixed !important; inset: 0 !important; pointer-events: none !important; z-index: 2147483647 !important;',
    );
    root = host.attachShadow({ mode: 'closed' });
    (document.body ?? document.documentElement).appendChild(host);
  }
  return root;
}

export function clearHighlights(): void {
  clearTimeout(clearTimer);
  document.getElementById(HOST_ID)?.remove();
  root = null;
}

function box(rect: DOMRect, color: string, label: string, thick: boolean): HTMLElement {
  const el = document.createElement('div');
  el.setAttribute(
    'style',
    `position:fixed;left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;` +
      `border:${thick ? 4 : 2}px solid ${color};border-radius:4px;box-sizing:border-box;` +
      `background:${thick ? 'rgba(250,204,21,0.18)' : 'transparent'};`,
  );
  if (label) {
    const tag = document.createElement('span');
    tag.textContent = label;
    tag.setAttribute(
      'style',
      `position:absolute;top:-2px;left:-2px;transform:translateY(-100%);background:${color};color:#fff;` +
        `font:600 ${thick ? 14 : 11}px/1.3 system-ui,sans-serif;padding:1px 4px;border-radius:3px;white-space:nowrap;`,
    );
    el.appendChild(tag);
  }
  return el;
}

export function highlightAll(elements: Map<number, HTMLElement>, ttlMs = 6000): void {
  clearHighlights();
  const shadow = layer();
  for (const [index, el] of elements) {
    const rect = visibleBox(el);
    if (!rect || rect.bottom < 0 || rect.top > window.innerHeight) continue;
    shadow.appendChild(box(rect, COLORS[index % COLORS.length], String(index), false));
  }
  clearTimer = setTimeout(clearHighlights, ttlMs);
}

export function highlightTarget(el: HTMLElement, label = '', ttlMs = 2500): void {
  clearHighlights();
  const rect = visibleBox(el);
  if (!rect) return;
  layer().appendChild(box(rect, '#facc15', label, true));
  clearTimer = setTimeout(clearHighlights, ttlMs);
}
