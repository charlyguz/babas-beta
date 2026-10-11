import { vi } from 'vitest';

export function stubLayout(): void {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 0, 100, 20));
  if ('checkVisibility' in HTMLElement.prototype) {
    vi.spyOn(HTMLElement.prototype, 'checkVisibility').mockReturnValue(true);
  }
}

export function mount(html: string): void {
  document.body.innerHTML = html;
}

export function byId<T extends HTMLElement = HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`Test fixture is missing #${id}`);
  return el as T;
}
