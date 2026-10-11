export const PROTOCOL_VERSION = 1;
export const DEFAULT_BRIDGE_PORT = 8765;

export type Flag =
  | 'submit'
  | 'search_form'
  | 'consequential'
  | 'password'
  | 'pii'
  | 'payment'
  | 'captcha'
  | 'disabled';

export interface ElementInfo {
  index: number;
  tag: string;
  role: string;
  label: string;
  text: string;
  type?: string;
  value?: string;
  href?: string;
  options?: string[];
  checked?: boolean;
  flags: Flag[];
  in_viewport: boolean;
}

export interface PageState {
  snapshot_id: string;
  url: string;
  title: string;
  elements: ElementInfo[];
  text: string;
  has_captcha: boolean;
  has_password: boolean;
  truncated: boolean;
}

export interface Target {
  index?: number;
  selector?: string;
  text?: string;
  snapshot_id?: string;
}

export type ErrorCode =
  | 'STALE_SNAPSHOT'
  | 'NOT_FOUND'
  | 'NOT_VISIBLE'
  | 'OBSCURED'
  | 'DISABLED'
  | 'NOT_EDITABLE'
  | 'PASSWORD_FIELD'
  | 'CONFIRMATION_REQUIRED'
  | 'DOMAIN_NOT_ALLOWED'
  | 'HALTED'
  | 'TIMEOUT'
  | 'NO_EXTENSION'
  | 'TAB_NOT_FOUND'
  | 'UNSCRIPTABLE'
  | 'NAVIGATION_FAILED'
  | 'BAD_REQUEST'
  | 'UNSUPPORTED'
  | 'INTERNAL';

export interface ErrorInfo {
  code: ErrorCode;
  message: string;
  data?: Record<string, unknown>;
}

export interface Envelope {
  v: number;
  id: string;
  type: string;
  payload?: Record<string, unknown>;
}

export interface ResultMessage {
  v: number;
  id: string;
  type: 'result';
  ok: boolean;
  payload?: Record<string, unknown> | null;
  error?: ErrorInfo | null;
}

export type PageCommand =
  | { op: 'ping' }
  | { op: 'snapshot'; highlight?: boolean; max_elements?: number; max_text?: number }
  | { op: 'describe'; target: Target }
  | { op: 'exists'; target: Target }
  | { op: 'click'; target: Target; authorized?: boolean }
  | { op: 'type'; target: Target; text: string; authorized?: boolean }
  | { op: 'press'; key: string; target?: Target; authorized?: boolean }
  | { op: 'select'; target: Target; value: string; authorized?: boolean }
  | { op: 'scroll'; direction: 'up' | 'down'; amount?: number }
  | { op: 'read'; target?: Target; max_chars?: number }
  | { op: 'highlight'; mode: 'all' | 'off' | 'target'; target?: Target }
  | { op: 'idle'; quiet_ms?: number; timeout_ms?: number }
  | { op: 'mark'; target: Target }
  | { op: 'unmark' };

export type PageResponse = { ok: true; result?: unknown } | { ok: false; error: ErrorInfo };

export class BabasError extends Error {
  code: ErrorCode;
  data?: Record<string, unknown>;

  constructor(code: ErrorCode, message: string, data?: Record<string, unknown>) {
    super(message);
    this.code = code;
    this.data = data;
  }

  toInfo(): ErrorInfo {
    return this.data ? { code: this.code, message: this.message, data: this.data } : { code: this.code, message: this.message };
  }
}

export function toErrorInfo(err: unknown): ErrorInfo {
  if (err instanceof BabasError) return err.toInfo();
  const message = err instanceof Error ? err.message : String(err);
  return { code: 'INTERNAL', message };
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

export function isEnvelope(v: unknown): v is Envelope {
  return (
    isObject(v) &&
    v.v === PROTOCOL_VERSION &&
    typeof v.id === 'string' &&
    v.id.length > 0 &&
    typeof v.type === 'string' &&
    v.type !== 'result' &&
    (v.payload === undefined || isObject(v.payload))
  );
}

export function isResultMessage(v: unknown): v is ResultMessage {
  if (!isObject(v) || v.v !== PROTOCOL_VERSION || v.type !== 'result' || typeof v.id !== 'string') return false;
  if (typeof v.ok !== 'boolean') return false;
  if (v.ok) return v.payload === undefined || v.payload === null || isObject(v.payload);
  return isObject(v.error) && typeof v.error.code === 'string' && typeof v.error.message === 'string';
}

export function isTarget(v: unknown): v is Target {
  if (!isObject(v)) return false;
  const hasIndex = typeof v.index === 'number' && Number.isInteger(v.index) && v.index >= 0;
  const hasSelector = typeof v.selector === 'string' && v.selector.length > 0;
  const hasText = typeof v.text === 'string' && v.text.length > 0;
  if (v.snapshot_id !== undefined && typeof v.snapshot_id !== 'string') return false;
  return hasIndex || hasSelector || hasText;
}

export function newId(): string {
  return crypto.randomUUID();
}
