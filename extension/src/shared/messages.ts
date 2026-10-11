export const PANEL_PORT_NAME = 'sidepanel';

export type LogKind = 'user' | 'assistant' | 'step' | 'result' | 'error' | 'info';
export type RunStateName = 'running' | 'done' | 'failed' | 'stopped';
export type AskKind = 'text' | 'handoff';

export interface LogEntry {
  kind: LogKind;
  text: string;
  ok?: boolean;
  ts: number;
}

export type PanelToSw =
  | { type: 'task.start'; text: string; windowId?: number }
  | { type: 'stop' }
  | { type: 'resume' }
  | { type: 'confirm.answer'; request_id: string; approved: boolean }
  | { type: 'ask.answer'; request_id: string; answer: string | null }
  | { type: 'allow.answer'; request_id: string; approved: boolean }
  | { type: 'settings.get' }
  | { type: 'settings.set'; bridgePort?: number; removeSite?: string };

export type SwToPanel =
  | { type: 'status'; connected: boolean; halted: boolean; running: boolean }
  | { type: 'log'; entry: LogEntry }
  | { type: 'confirm'; request_id: string; title: string; lines: string[]; explanation: string }
  | { type: 'ask'; request_id: string; question: string; kind: AskKind }
  | { type: 'allow'; request_id: string; host: string; url: string }
  | { type: 'request.closed'; request_id: string }
  | { type: 'run_state'; state: RunStateName; summary?: string }
  | { type: 'settings'; bridgePort: number; sites: string[] };

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === 'string';
const isId = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 128;

export function isPanelMessage(v: unknown): v is PanelToSw {
  if (!isObject(v) || !isString(v.type)) return false;
  switch (v.type) {
    case 'task.start':
      return isString(v.text) && v.text.trim().length > 0 && v.text.length <= 4000 && (v.windowId === undefined || typeof v.windowId === 'number');
    case 'stop':
    case 'resume':
    case 'settings.get':
      return true;
    case 'confirm.answer':
    case 'allow.answer':
      return isId(v.request_id) && typeof v.approved === 'boolean';
    case 'ask.answer':
      return isId(v.request_id) && (v.answer === null || (isString(v.answer) && v.answer.length <= 4000));
    case 'settings.set':
      return (
        (v.bridgePort === undefined || (typeof v.bridgePort === 'number' && Number.isInteger(v.bridgePort) && v.bridgePort > 0 && v.bridgePort < 65536)) &&
        (v.removeSite === undefined || isString(v.removeSite))
      );
    default:
      return false;
  }
}

export function isSwMessage(v: unknown): v is SwToPanel {
  if (!isObject(v) || !isString(v.type)) return false;
  switch (v.type) {
    case 'status':
      return typeof v.connected === 'boolean' && typeof v.halted === 'boolean' && typeof v.running === 'boolean';
    case 'log':
      return isObject(v.entry) && isString(v.entry.kind) && isString(v.entry.text) && typeof v.entry.ts === 'number';
    case 'confirm':
      return isId(v.request_id) && isString(v.title) && Array.isArray(v.lines) && v.lines.every(isString) && isString(v.explanation);
    case 'ask':
      return isId(v.request_id) && isString(v.question) && (v.kind === 'text' || v.kind === 'handoff');
    case 'allow':
      return isId(v.request_id) && isString(v.host) && isString(v.url);
    case 'request.closed':
      return isId(v.request_id);
    case 'run_state':
      return ['running', 'done', 'failed', 'stopped'].includes(String(v.state)) && (v.summary === undefined || isString(v.summary));
    case 'settings':
      return typeof v.bridgePort === 'number' && Array.isArray(v.sites) && v.sites.every(isString);
    default:
      return false;
  }
}
