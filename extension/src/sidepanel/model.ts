import type { AskKind, LogKind, RunStateName, SwToPanel } from '../shared/messages';

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
}

export type PanelAction =
  | { type: 'port'; up: boolean }
  | { type: 'sw'; message: SwToPanel }
  | { type: 'user.sent'; text: string; ts: number }
  | { type: 'user.stopped' }
  | { type: 'request.answered'; request_id: string };

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
  };
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
    case 'status':
      return applyStatus(state, message);
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
    default:
      return state;
  }
}

export function reduce(state: PanelState, action: PanelAction): PanelState {
  switch (action.type) {
    case 'port':
      // Pending cards are dropped with the port: the worker replays every request that is still open on reconnect.
      return action.up ? { ...state, portUp: true } : { ...state, portUp: false, queue: [] };
    case 'sw':
      return applySw(state, action.message);
    case 'user.sent':
      return { ...appendLog(state, { kind: 'user', text: action.text, ts: action.ts }), runState: null, summary: null };
    case 'user.stopped':
      return { ...state, halted: true, running: false, runState: 'stopped', summary: null, queue: [] };
    case 'request.answered':
      return dequeue(state, action.request_id);
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
