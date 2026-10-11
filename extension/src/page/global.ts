import type { PageCommand, PageResponse } from '../shared/protocol';
import { handle } from './handle';

declare global {
  interface Window {
    __babas?: { version: number; handle: (cmd: PageCommand) => Promise<PageResponse> };
  }
}

if (!window.__babas) {
  window.__babas = { version: 1, handle };
}
