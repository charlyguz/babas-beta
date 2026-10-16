import { BabasError, isTarget, type Target } from '../shared/protocol';

export const DEFAULT_SITES: readonly string[] = ['gob.mx', 'google.com', 'google.com.mx', 'youtube.com', 'whatsapp.com', 'wikipedia.org'];

export type Translate = (key: string, substitutions?: string[]) => string;

const SEP = '␟';
const SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;
const MAX_FIELD = 4000;
const MAX_ACTIONS = 20;

export function normalizeHost(input: string): string {
  const raw = String(input ?? '').trim();
  if (!raw) return '';
  try {
    const host = new URL(SCHEME.test(raw) ? raw : `http://${raw}`).hostname.toLowerCase().replace(/\.$/, '');
    return host.replace(/^www\./, '');
  } catch {
    return '';
  }
}

export function hostAllowed(host: string, sites: readonly string[]): boolean {
  const h = normalizeHost(host);
  if (!h) return false;
  return sites.some((entry) => {
    if (entry.includes('*')) return false;
    const site = normalizeHost(entry);
    return site !== '' && (h === site || h.endsWith(`.${site}`));
  });
}

// Chromium percent-encodes odd characters in hosts instead of rejecting them, so the shape is checked here.
export function plausibleSiteHost(host: string): boolean {
  if (/^\[[0-9a-f:.]+\]$/.test(host)) return true;
  return /^[a-z0-9._-]+$/.test(host) && (host === 'localhost' || host.includes('.'));
}

export type HostCheck = { check: false } | { check: true; host: string };

// Only schemes the content script can be injected into get a check; blob: and filesystem: inherit the origin of their inner URL.
export function hostCheckFor(url: string | undefined): HostCheck {
  if (!url) return { check: false };
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { check: true, host: '' };
  }
  switch (parsed.protocol) {
    case 'http:':
    case 'https:':
      return { check: true, host: normalizeHost(url) };
    case 'blob:':
    case 'filesystem:':
      return { check: true, host: normalizeHost(parsed.pathname) };
    case 'file:':
    case 'ftp:':
      return { check: true, host: '' };
    default:
      return { check: false };
  }
}

export function isHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

export function isPanelUrl(url: string | undefined, base: string): boolean {
  if (!url || !base) return false;
  return url === base || url.startsWith(`${base}?`) || url.startsWith(`${base}#`);
}

export interface DigestAction {
  op?: unknown;
  target?: unknown;
  text?: unknown;
  value?: unknown;
  key?: unknown;
}

const part = (v: unknown): string => (v === undefined || v === null ? '' : String(v));
// Escaping keeps the join injective: a text containing the separator must not look like two fields.
const escapePart = (s: string): string => s.replace(/\\/g, '\\\\').replace(/␟/g, '\\␟');

export function actionDigest(tabId: number, action: DigestAction): string {
  const t = typeof action.target === 'object' && action.target !== null ? (action.target as Record<string, unknown>) : {};
  return [tabId, action.op, t.snapshot_id, t.index, t.selector, t.text, action.text, action.value, action.key]
    .map((v) => escapePart(part(v)))
    .join(SEP);
}

export function confirmTitle(reasons: readonly string[], t: Translate): string {
  const has = (reason: string): boolean => reasons.includes(reason);
  if (has('payment')) return t('titlePayment');
  if (has('submit_form') || has('enter_submits_form')) return t('titleSubmit');
  if (has('pii_field') || has('pii_value')) return t('titlePii');
  return t('titleImportant');
}

export interface LineValues {
  text?: string;
  value?: string;
  key?: string;
}

export function confirmLine(op: string, label: string, host: string, values: LineValues, t: Translate): string {
  const shown = label.trim() || t('pageFallbackLabel');
  switch (op) {
    case 'click':
      return t('lineClick', [shown, host]);
    case 'type':
      return t('lineType', [values.text ?? '', shown, host]);
    case 'select':
      return t('lineSelect', [values.value ?? '', shown, host]);
    case 'press':
      return t('linePress', [values.key || 'Enter', shown, host]);
    default:
      throw new Error(`No confirmation line for op "${op}"`);
  }
}

export function timeoutMsFrom(value: unknown, defaultSeconds: number): number {
  const seconds = typeof value === 'number' && Number.isFinite(value) ? value : defaultSeconds;
  return Math.round(Math.min(3600, Math.max(1, seconds)) * 1000);
}

export type ConfirmOp = 'click' | 'type' | 'select' | 'press';

export interface ConfirmAction {
  op: ConfirmOp;
  target?: Target;
  text?: string;
  value?: string;
  key?: string;
}

export interface ConfirmRequest {
  actions: ConfirmAction[];
  reasons: string[];
  explanation: string;
}

const CONFIRM_OPS: readonly string[] = ['click', 'type', 'select', 'press'];
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

function boundedString(value: unknown, what: string): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || value.length > MAX_FIELD) {
    throw new BabasError('BAD_REQUEST', `${what} must be a string of at most ${MAX_FIELD} characters`);
  }
  return value;
}

function parseTarget(value: unknown, required: boolean): Target | undefined {
  if (value === undefined || value === null) {
    if (required) throw new BabasError('BAD_REQUEST', 'This action needs a target');
    return undefined;
  }
  if (!isTarget(value)) throw new BabasError('BAD_REQUEST', 'A valid target (index, selector or text) is required');
  const target: Target = {};
  if (value.index !== undefined) target.index = value.index;
  const selector = boundedString(value.selector, 'target.selector');
  if (selector !== undefined) target.selector = selector;
  const text = boundedString(value.text, 'target.text');
  if (text !== undefined) target.text = text;
  const snapshot = boundedString(value.snapshot_id, 'target.snapshot_id');
  if (snapshot !== undefined) target.snapshot_id = snapshot;
  return target;
}

export function parseConfirmPayload(payload: Record<string, unknown>): ConfirmRequest {
  const rawActions = payload.actions;
  if (!Array.isArray(rawActions) || rawActions.length < 1 || rawActions.length > MAX_ACTIONS) {
    throw new BabasError('BAD_REQUEST', `actions must be a list of 1 to ${MAX_ACTIONS} items`);
  }
  const actions = rawActions.map((raw): ConfirmAction => {
    if (!isRecord(raw) || typeof raw.op !== 'string' || !CONFIRM_OPS.includes(raw.op)) {
      throw new BabasError('BAD_REQUEST', 'Each action needs an op: click, type, select or press');
    }
    const op = raw.op as ConfirmOp;
    const action: ConfirmAction = { op };
    const target = parseTarget(raw.target, op !== 'press');
    if (target) action.target = target;
    const text = boundedString(raw.text, 'text');
    if (text !== undefined) action.text = text;
    const value = boundedString(raw.value, 'value');
    if (value !== undefined) action.value = value;
    const key = boundedString(raw.key, 'key');
    if (key !== undefined) action.key = key;
    return action;
  });
  const rawReasons = payload.reasons ?? [];
  if (!Array.isArray(rawReasons) || rawReasons.length > 50 || !rawReasons.every((r) => typeof r === 'string' && r.length <= 100)) {
    throw new BabasError('BAD_REQUEST', 'reasons must be a list of short strings');
  }
  const explanation = boundedString(payload.explanation, 'explanation') ?? '';
  return { actions, reasons: rawReasons as string[], explanation };
}
