import { PANEL_PORT_NAME, isPanelMessage, type AskKind, type LogKind, type PanelToSw, type SwToPanel } from '../shared/messages';
import { isPanelUrl } from './policy';

export type RequestKind = 'confirm' | 'ask' | 'allow';
export type CloseReason = 'denied' | 'timeout' | 'stopped' | 'cancelled';

export interface RequestPayloads {
  confirm: { title: string; lines: string[]; explanation: string };
  ask: { question: string; kind: AskKind };
  allow: { host: string; url: string };
}

export interface Resolution {
  approved: boolean;
  answer: string | null;
  reason?: CloseReason;
}

interface Pending {
  kind: RequestKind;
  message: SwToPanel;
  resolve: (resolution: Resolution) => void;
  timer: ReturnType<typeof setTimeout>;
}

const MAX_PENDING = 50;

const ports = new Set<chrome.runtime.Port>();
const pending = new Map<string, Pending>();
let badgeText: string | null = null;

export function panelCount(): number {
  return ports.size;
}

export function pendingCount(): number {
  return pending.size;
}

function post(port: chrome.runtime.Port, message: SwToPanel): void {
  try {
    port.postMessage(message);
  } catch {
    ports.delete(port);
  }
}

export function broadcast(message: SwToPanel): void {
  for (const port of [...ports]) post(port, message);
}

export function sendTo(port: chrome.runtime.Port, message: SwToPanel): void {
  post(port, message);
}

export function log(kind: LogKind, text: string, ok?: boolean): void {
  broadcast({ type: 'log', entry: { kind, text, ...(ok === undefined ? {} : { ok }), ts: Date.now() } });
}

export function refreshBadge(): void {
  const text = pending.size > 0 && ports.size === 0 ? '!' : '';
  if (text === badgeText) return;
  badgeText = text;
  try {
    const jobs: Promise<unknown>[] = [chrome.action.setBadgeText({ text })];
    if (text) jobs.push(chrome.action.setBadgeBackgroundColor({ color: '#dc2626' }));
    Promise.all(jobs).catch(() => undefined);
  } catch {
    // the badge is a hint only; the request still waits for its timeout
  }
}

function finish(requestId: string, resolution: Resolution): boolean {
  const entry = pending.get(requestId);
  if (!entry) return false;
  clearTimeout(entry.timer);
  pending.delete(requestId);
  entry.resolve(resolution);
  broadcast({ type: 'request.closed', request_id: requestId });
  refreshBadge();
  return true;
}

export function openRequest<K extends RequestKind>(kind: K, payload: RequestPayloads[K], timeoutMs: number): Promise<Resolution> {
  if (pending.size >= MAX_PENDING) {
    return Promise.resolve({ approved: false, answer: null, reason: kind === 'ask' ? 'cancelled' : 'denied' });
  }
  const requestId = crypto.randomUUID();
  const message = { type: kind, request_id: requestId, ...payload } as SwToPanel;
  return new Promise<Resolution>((resolve) => {
    const timer = setTimeout(() => finish(requestId, { approved: false, answer: null, reason: 'timeout' }), timeoutMs);
    pending.set(requestId, { kind, message, resolve, timer });
    broadcast(message);
    refreshBadge();
  });
}

export function closeAll(reason: 'stopped' | 'denied'): void {
  for (const requestId of [...pending.keys()]) finish(requestId, { approved: false, answer: null, reason });
}

function applyAnswer(message: PanelToSw): void {
  switch (message.type) {
    case 'confirm.answer':
    case 'allow.answer': {
      const entry = pending.get(message.request_id);
      if (!entry || entry.kind !== (message.type === 'confirm.answer' ? 'confirm' : 'allow')) return;
      finish(message.request_id, message.approved ? { approved: true, answer: null } : { approved: false, answer: null, reason: 'denied' });
      return;
    }
    case 'ask.answer': {
      const entry = pending.get(message.request_id);
      if (!entry || entry.kind !== 'ask') return;
      finish(message.request_id, message.answer === null ? { approved: false, answer: null, reason: 'cancelled' } : { approved: true, answer: message.answer });
      return;
    }
    default:
  }
}

export interface PanelHooks {
  onMessage: (message: PanelToSw, port: chrome.runtime.Port) => void;
  onDisconnect?: (port: chrome.runtime.Port) => void;
  greeting: () => Promise<SwToPanel[]>;
}

export function initPanels(hooks: PanelHooks): void {
  chrome.runtime.onConnect.addListener((port) => {
    const trusted = port.name === PANEL_PORT_NAME && port.sender?.id === chrome.runtime.id && isPanelUrl(port.sender?.url, chrome.runtime.getURL('sidepanel.html'));
    if (!trusted) {
      port.disconnect();
      return;
    }
    const replay = [...pending.values()].map((entry) => entry.message);
    ports.add(port);
    refreshBadge();
    port.onDisconnect.addListener(() => {
      ports.delete(port);
      refreshBadge();
      hooks.onDisconnect?.(port);
    });
    port.onMessage.addListener((raw: unknown) => {
      if (!isPanelMessage(raw)) return;
      if (raw.type === 'confirm.answer' || raw.type === 'ask.answer' || raw.type === 'allow.answer') {
        applyAnswer(raw);
        return;
      }
      hooks.onMessage(raw, port);
    });
    void hooks
      .greeting()
      .then((messages) => {
        for (const message of messages) post(port, message);
        for (const message of replay) {
          const id = 'request_id' in message ? message.request_id : '';
          if (pending.has(id)) post(port, message);
        }
      })
      .catch(() => undefined);
  });
}
