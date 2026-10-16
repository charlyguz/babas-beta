export const PANEL_PORT_NAME = 'sidepanel';

export type LogKind = 'user' | 'assistant' | 'step' | 'result' | 'error' | 'info';
export type RunStateName = 'running' | 'done' | 'failed' | 'stopped';
export type AskKind = 'text' | 'handoff';

export const VOICE_ERROR_CODES = ['no_connection', 'timeout', 'busy', 'not_configured', 'provider', 'bad_audio', 'empty'] as const;
export type VoiceErrorCode = (typeof VOICE_ERROR_CODES)[number];

export const VOICE_ERROR_KEYS: Record<VoiceErrorCode, string> = {
  no_connection: 'voiceErrNoConnection',
  timeout: 'voiceErrTimeout',
  busy: 'voiceErrBusy',
  not_configured: 'voiceErrNotConfigured',
  provider: 'voiceErrProvider',
  bad_audio: 'voiceErrBadAudio',
  empty: 'voiceErrEmpty',
};

export const VOICE_LIMITS = {
  maxIdChars: 128,
  maxAudioChars: 1_500_000,
  maxDurationMs: 30_000,
  maxTextChars: 4000,
  maxMessageChars: 1000,
} as const;

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
  | { type: 'settings.set'; bridgePort?: number; removeSite?: string }
  | { type: 'voice.audio'; request_id: string; audio_base64: string; duration_ms: number };

export type SwToPanel =
  | { type: 'status'; connected: boolean; halted: boolean; running: boolean }
  | { type: 'log'; entry: LogEntry }
  | { type: 'confirm'; request_id: string; title: string; lines: string[]; explanation: string }
  | { type: 'ask'; request_id: string; question: string; kind: AskKind }
  | { type: 'allow'; request_id: string; host: string; url: string }
  | { type: 'request.closed'; request_id: string }
  | { type: 'run_state'; state: RunStateName; summary?: string }
  | { type: 'settings'; bridgePort: number; sites: string[] }
  | { type: 'voice.result'; request_id: string; text: string }
  | { type: 'voice.error'; request_id: string; code: VoiceErrorCode; message: string };

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === 'string';
const isId = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= VOICE_LIMITS.maxIdChars;

export const isVoiceErrorCode = (v: unknown): v is VoiceErrorCode => typeof v === 'string' && (VOICE_ERROR_CODES as readonly string[]).includes(v);

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
    case 'voice.audio':
      return (
        isId(v.request_id) &&
        isString(v.audio_base64) &&
        v.audio_base64.length > 0 &&
        v.audio_base64.length <= VOICE_LIMITS.maxAudioChars &&
        typeof v.duration_ms === 'number' &&
        Number.isInteger(v.duration_ms) &&
        v.duration_ms >= 1 &&
        v.duration_ms <= VOICE_LIMITS.maxDurationMs
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
    case 'voice.result':
      return isId(v.request_id) && isString(v.text) && v.text.length <= VOICE_LIMITS.maxTextChars;
    case 'voice.error':
      return isId(v.request_id) && isVoiceErrorCode(v.code) && isString(v.message) && v.message.length <= VOICE_LIMITS.maxMessageChars;
    default:
      return false;
  }
}
