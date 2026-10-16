import { VOICE_ERROR_KEYS, type AskKind, type LogKind, type RunStateName, type SwToPanel, type VoiceErrorCode } from '../shared/messages';

export const MAX_LOG_ENTRIES = 500;

export interface LogItem {
  id: number;
  kind: LogKind;
  text: string;
  ok?: boolean;
  ts: number;
}

export type RequestItem =
  | { kind: 'confirm'; id: string; title: string; lines: string[]; explanation: string }
  | { kind: 'ask'; id: string; question: string; askKind: AskKind }
  | { kind: 'allow'; id: string; host: string; url: string };

export type VoicePhase = 'idle' | 'starting' | 'recording' | 'transcribing';
export type VoiceNotice = 'cancelled' | 'permission_granted';
export type VoiceFailure = VoiceErrorCode | 'permission' | 'no_device' | 'mic_busy' | 'capture';

const VOICE_NOTICE_KEYS: Record<VoiceNotice, string> = {
  cancelled: 'voiceCancelled',
  permission_granted: 'voicePermissionOk',
};

const VOICE_FAILURE_KEYS: Record<VoiceFailure, string> = {
  ...VOICE_ERROR_KEYS,
  permission: 'voiceErrPermission',
  no_device: 'voiceErrNoDevice',
  mic_busy: 'voiceErrMicBusy',
  capture: 'voiceErrCapture',
};

export interface PanelState {
  portUp: boolean;
  bridgeUp: boolean;
  halted: boolean;
  running: boolean;
  runState: RunStateName | null;
  summary: string | null;
  log: LogItem[];
  nextLogId: number;
  queue: RequestItem[];
  bridgePort: number | null;
  sites: string[];
  voice: VoicePhase;
  voiceError: VoiceFailure | null;
  voiceNotice: VoiceNotice | null;
  voiceRequestId: string | null;
}

export type PanelAction =
  | { type: 'port'; up: boolean }
  | { type: 'sw'; message: SwToPanel }
  | { type: 'user.sent'; text: string; ts: number }
  | { type: 'user.stopped' }
  | { type: 'request.answered'; request_id: string }
  | { type: 'voice.starting' }
  | { type: 'voice.recording' }
  | { type: 'voice.transcribing'; request_id: string }
  | { type: 'voice.failed'; code: VoiceFailure }
  | { type: 'voice.cancelled' }
  | { type: 'voice.permission_granted' };

export function initialState(): PanelState {
  return {
    portUp: false,
    bridgeUp: false,
    halted: false,
    running: false,
    runState: null,
    summary: null,
    log: [],
    nextLogId: 1,
    queue: [],
    bridgePort: null,
    sites: [],
    voice: 'idle',
    voiceError: null,
    voiceNotice: null,
    voiceRequestId: null,
  };
}

function voiceFailed(state: PanelState, code: VoiceFailure): PanelState {
  return { ...state, voice: 'idle', voiceError: code, voiceNotice: null, voiceRequestId: null };
}

function isCurrentTranscription(state: PanelState, requestId: string): boolean {
  return state.voice === 'transcribing' && state.voiceRequestId === requestId;
}

function appendLog(state: PanelState, entry: Omit<LogItem, 'id'>): PanelState {
  const log = [...state.log, { ...entry, id: state.nextLogId }];
  return { ...state, log: log.length > MAX_LOG_ENTRIES ? log.slice(log.length - MAX_LOG_ENTRIES) : log, nextLogId: state.nextLogId + 1 };
}

function enqueue(state: PanelState, item: RequestItem): PanelState {
  if (state.queue.some((queued) => queued.id === item.id)) return state;
  return { ...state, queue: [...state.queue, item] };
}

function dequeue(state: PanelState, requestId: string): PanelState {
  if (!state.queue.some((queued) => queued.id === requestId)) return state;
  return { ...state, queue: state.queue.filter((queued) => queued.id !== requestId) };
}

function applyStatus(state: PanelState, message: Extract<SwToPanel, { type: 'status' }>): PanelState {
  let { runState, summary } = state;
  if (message.halted) {
    if (runState !== 'stopped') {
      runState = 'stopped';
      summary = null;
    }
  } else {
    if (state.halted && runState === 'stopped') {
      runState = null;
      summary = null;
    }
    if (message.running) {
      if (runState !== 'running') summary = null;
      runState = 'running';
    } else if (runState === 'running') {
      runState = null;
      summary = null;
    }
  }
  return { ...state, bridgeUp: message.connected, halted: message.halted, running: message.running && !message.halted, runState, summary };
}

function applySw(state: PanelState, message: SwToPanel): PanelState {
  switch (message.type) {
    case 'status': {
      const next = applyStatus(state, message);
      // The microphone cannot be released from a button that is disabled, so a lost link ends the recording here.
      return !message.connected && (next.voice === 'starting' || next.voice === 'recording') ? voiceFailed(next, 'no_connection') : next;
    }
    case 'log':
      return appendLog(state, {
        kind: message.entry.kind,
        text: message.entry.text,
        ...(typeof message.entry.ok === 'boolean' ? { ok: message.entry.ok } : {}),
        ts: message.entry.ts,
      });
    case 'confirm':
      return enqueue(state, { kind: 'confirm', id: message.request_id, title: message.title, lines: message.lines, explanation: message.explanation });
    case 'ask':
      return enqueue(state, { kind: 'ask', id: message.request_id, question: message.question, askKind: message.kind });
    case 'allow':
      return enqueue(state, { kind: 'allow', id: message.request_id, host: message.host, url: message.url });
    case 'request.closed':
      return dequeue(state, message.request_id);
    case 'run_state':
      return { ...state, runState: message.state, summary: message.summary ?? null, running: message.state === 'running' };
    case 'settings':
      return { ...state, bridgePort: message.bridgePort, sites: message.sites };
    case 'voice.result':
      if (!isCurrentTranscription(state, message.request_id)) return state;
      return message.text.trim() === '' ? voiceFailed(state, 'empty') : { ...state, voice: 'idle', voiceError: null, voiceRequestId: null };
    case 'voice.error':
      return isCurrentTranscription(state, message.request_id) ? voiceFailed(state, message.code) : state;
    default:
      return state;
  }
}

export function reduce(state: PanelState, action: PanelAction): PanelState {
  switch (action.type) {
    case 'port':
      // Pending cards are dropped with the port: the worker replays every request that is still open on reconnect.
      if (action.up) return { ...state, portUp: true };
      return state.voice === 'idle' ? { ...state, portUp: false, queue: [] } : voiceFailed({ ...state, portUp: false, queue: [] }, 'no_connection');
    case 'sw':
      return applySw(state, action.message);
    case 'user.sent':
      return { ...appendLog(state, { kind: 'user', text: action.text, ts: action.ts }), runState: null, summary: null, voice: 'idle', voiceError: null, voiceNotice: null, voiceRequestId: null };
    case 'user.stopped':
      return { ...state, halted: true, running: false, runState: 'stopped', summary: null, queue: [] };
    case 'request.answered':
      return dequeue(state, action.request_id);
    case 'voice.starting':
      return { ...state, voice: 'starting', voiceError: null, voiceNotice: null, voiceRequestId: null };
    case 'voice.recording':
      return state.voice === 'starting' ? { ...state, voice: 'recording' } : state;
    case 'voice.transcribing':
      return state.voice === 'recording' ? { ...state, voice: 'transcribing', voiceRequestId: action.request_id } : state;
    case 'voice.failed':
      return voiceFailed(state, action.code);
    case 'voice.cancelled':
      return state.voice === 'starting' || state.voice === 'recording' ? { ...state, voice: 'idle', voiceError: null, voiceNotice: 'cancelled', voiceRequestId: null } : state;
    case 'voice.permission_granted':
      return state.voice === 'idle' && state.voiceError === 'permission' ? { ...state, voiceError: null, voiceNotice: 'permission_granted' } : state;
    default:
      return state;
  }
}

export type StatusKey = 'statusDisconnected' | 'statusHalted' | 'statusWorking' | 'statusConnected';

export function isDisconnected(state: PanelState): boolean {
  return !state.portUp || !state.bridgeUp;
}

export function statusKey(state: PanelState): StatusKey {
  if (isDisconnected(state)) return 'statusDisconnected';
  if (state.halted) return 'statusHalted';
  if (state.running) return 'statusWorking';
  return 'statusConnected';
}

export function runStateKey(state: PanelState): string | null {
  if (state.running) return 'statusWorking';
  switch (state.runState) {
    case 'running':
      return 'statusWorking';
    case 'done':
      return 'runDone';
    case 'stopped':
      return 'runStopped';
    case 'failed':
      return 'runFailed';
    default:
      return null;
  }
}

export function canSend(state: PanelState): boolean {
  return !state.running && !isDisconnected(state);
}

export function showStop(state: PanelState): boolean {
  return state.running || state.queue.length > 0;
}

export function showResume(state: PanelState): boolean {
  return state.halted;
}

export function activeRequest(state: PanelState): RequestItem | undefined {
  return state.queue[0];
}

// Dictation always lands in the main text box, so it must not start while a question card is waiting for its own answer.
export function micBlockedByRequest(state: PanelState): boolean {
  return state.voice === 'idle' && !isDisconnected(state) && activeRequest(state) !== undefined;
}

export function micDisabled(state: PanelState): boolean {
  return isDisconnected(state) || state.voice === 'starting' || state.voice === 'transcribing' || micBlockedByRequest(state);
}

export function micLabelKey(state: PanelState): 'voiceStart' | 'voiceFinish' | 'voiceWait' {
  switch (state.voice) {
    case 'recording':
      return 'voiceFinish';
    case 'starting':
    case 'transcribing':
      return 'voiceWait';
    default:
      return 'voiceStart';
  }
}

export function showCancel(state: PanelState): boolean {
  return state.voice === 'starting' || state.voice === 'recording';
}

export function voiceNoticeKey(notice: VoiceNotice): string {
  return VOICE_NOTICE_KEYS[notice];
}

export function voiceStatusKey(state: PanelState): string | null {
  switch (state.voice) {
    case 'starting':
      return 'voiceStarting';
    case 'recording':
      return 'voiceListening';
    case 'transcribing':
      return 'voiceTranscribing';
    default:
      return null;
  }
}

export function voiceErrorKey(code: VoiceFailure): string {
  return VOICE_FAILURE_KEYS[code];
}

export function showPermissionButton(state: PanelState): boolean {
  return state.voice === 'idle' && state.voiceError === 'permission';
}
