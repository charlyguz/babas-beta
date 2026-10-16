import { describe, expect, it } from 'vitest';
import type { SwToPanel } from '../shared/messages';
import {
  MAX_LOG_ENTRIES,
  activeRequest,
  canSend,
  initialState,
  micBlockedByRequest,
  micDisabled,
  micLabelKey,
  reduce,
  runStateKey,
  showCancel,
  showPermissionButton,
  showResume,
  showStop,
  statusKey,
  voiceErrorKey,
  voiceNoticeKey,
  voiceStatusKey,
  type PanelAction,
  type PanelState,
} from './model';

function run(actions: PanelAction[], from: PanelState = initialState()): PanelState {
  return actions.reduce(reduce, from);
}

const sw = (message: SwToPanel): PanelAction => ({ type: 'sw', message });
const status = (connected: boolean, halted: boolean, running: boolean): PanelAction => sw({ type: 'status', connected, halted, running });
const confirm = (id: string): PanelAction => sw({ type: 'confirm', request_id: id, title: 'T', lines: ['a', 'b', 'c'], explanation: 'x' });
const online = (): PanelState => run([{ type: 'port', up: true }, status(true, false, false)]);

describe('status pill', () => {
  it('is disconnected until both the port and the bridge are up', () => {
    expect(statusKey(initialState())).toBe('statusDisconnected');
    expect(statusKey(run([{ type: 'port', up: true }]))).toBe('statusDisconnected');
    expect(statusKey(run([status(true, false, false)]))).toBe('statusDisconnected');
    expect(statusKey(online())).toBe('statusConnected');
  });

  it('shows working, then halted over working', () => {
    expect(statusKey(run([status(true, false, true)], online()))).toBe('statusWorking');
    expect(statusKey(run([status(true, true, true)], online()))).toBe('statusHalted');
  });

  it('falls back to disconnected when the bridge drops mid-run', () => {
    expect(statusKey(run([status(true, false, true), status(false, false, true)], online()))).toBe('statusDisconnected');
  });
});

describe('run state and the send button', () => {
  it('cannot send while running or disconnected, but can while halted', () => {
    expect(canSend(initialState())).toBe(false);
    expect(canSend(online())).toBe(true);
    expect(canSend(run([sw({ type: 'run_state', state: 'running' })], online()))).toBe(false);
    expect(canSend(run([status(true, true, false)], online()))).toBe(true);
    expect(canSend(run([{ type: 'port', up: false }], online()))).toBe(false);
  });

  it('maps every run state to its locale key and carries the summary', () => {
    const s = run([sw({ type: 'run_state', state: 'done', summary: 'La respuesta' })], online());
    expect(runStateKey(s)).toBe('runDone');
    expect(s.summary).toBe('La respuesta');
    expect(runStateKey(run([sw({ type: 'run_state', state: 'failed' })], s))).toBe('runFailed');
    expect(runStateKey(run([sw({ type: 'run_state', state: 'stopped' })], s))).toBe('runStopped');
    expect(runStateKey(run([sw({ type: 'run_state', state: 'running' })], s))).toBe('statusWorking');
    expect(runStateKey(online())).toBeNull();
  });

  it('a new run clears the previous summary', () => {
    const s = run([sw({ type: 'run_state', state: 'done', summary: 'old' }), sw({ type: 'run_state', state: 'running' })], online());
    expect(s.summary).toBeNull();
    expect(s.running).toBe(true);
  });

  it('sending a task appends a user entry and clears the old run state', () => {
    const s = run([sw({ type: 'run_state', state: 'done', summary: 'old' }), { type: 'user.sent', text: 'hola', ts: 5 }], online());
    expect(s.log.at(-1)).toMatchObject({ kind: 'user', text: 'hola', ts: 5 });
    expect(s.runState).toBeNull();
    expect(s.summary).toBeNull();
  });

  it('keeps a python-side stopped run without offering resume', () => {
    const s = run([sw({ type: 'run_state', state: 'stopped' }), status(true, false, false)], online());
    expect(runStateKey(s)).toBe('runStopped');
    expect(showResume(s)).toBe(false);
  });
});

describe('stop and resume', () => {
  it('shows stop while running or while a request is pending', () => {
    expect(showStop(online())).toBe(false);
    expect(showStop(run([status(true, false, true)], online()))).toBe(true);
    expect(showStop(run([confirm('r1')], online()))).toBe(true);
  });

  it('a local stop halts at once, drops cards and offers resume', () => {
    const s = run([status(true, false, true), confirm('r1'), { type: 'user.stopped' }], online());
    expect(s.halted).toBe(true);
    expect(s.running).toBe(false);
    expect(s.queue).toEqual([]);
    expect(runStateKey(s)).toBe('runStopped');
    expect(showResume(s)).toBe(true);
    expect(showStop(s)).toBe(false);
    expect(statusKey(s)).toBe('statusHalted');
  });

  it('status.halted alone shows stopped and resume (panel opened while halted)', () => {
    const s = run([status(true, true, false)], online());
    expect(showResume(s)).toBe(true);
    expect(runStateKey(s)).toBe('runStopped');
  });

  it('clears the stopped text when the halt is lifted', () => {
    const s = run([status(true, true, false), status(true, false, false)], online());
    expect(showResume(s)).toBe(false);
    expect(runStateKey(s)).toBeNull();
  });

  it('a stale running status never wins over halted', () => {
    const s = run([status(true, true, true)], online());
    expect(s.running).toBe(false);
    expect(s.runState).toBe('stopped');
  });
});

describe('request queue', () => {
  it('shows requests one at a time in arrival order', () => {
    const s = run([confirm('a'), sw({ type: 'ask', request_id: 'b', question: 'q', kind: 'text' }), sw({ type: 'allow', request_id: 'c', host: 'x.mx', url: 'https://x.mx' })]);
    expect(s.queue.map((r) => r.id)).toEqual(['a', 'b', 'c']);
    expect(activeRequest(s)?.id).toBe('a');
    expect(activeRequest(run([{ type: 'request.answered', request_id: 'a' }], s))?.id).toBe('b');
  });

  it('request.closed removes an active or queued request without touching the others', () => {
    const s = run([confirm('a'), confirm('b'), confirm('c'), sw({ type: 'request.closed', request_id: 'b' })]);
    expect(s.queue.map((r) => r.id)).toEqual(['a', 'c']);
    expect(run([sw({ type: 'request.closed', request_id: 'a' })], s).queue.map((r) => r.id)).toEqual(['c']);
    expect(run([sw({ type: 'request.closed', request_id: 'zzz' })], s)).toBe(s);
  });

  it('ignores a duplicate request id (replay after reconnect)', () => {
    expect(run([confirm('a'), confirm('a')]).queue).toHaveLength(1);
  });

  it('drops pending cards when the port goes down, so the replay is the only source', () => {
    const s = run([confirm('a'), { type: 'port', up: false }, { type: 'port', up: true }, confirm('a')]);
    expect(s.queue.map((r) => r.id)).toEqual(['a']);
    expect(run([confirm('a'), { type: 'port', up: false }]).queue).toEqual([]);
  });

  it('keeps the ask kind', () => {
    const s = run([sw({ type: 'ask', request_id: 'h', question: 'q', kind: 'handoff' })]);
    expect(s.queue[0]).toMatchObject({ kind: 'ask', askKind: 'handoff' });
  });
});

describe('log and settings', () => {
  it('numbers entries and keeps ok only when it is a boolean', () => {
    const s = run([
      sw({ type: 'log', entry: { kind: 'step', text: 'uno', ok: true, ts: 1 } }),
      sw({ type: 'log', entry: { kind: 'step', text: 'dos', ok: false, ts: 2 } }),
      sw({ type: 'log', entry: { kind: 'info', text: 'tres', ts: 3 } }),
    ]);
    expect(s.log.map((e) => e.id)).toEqual([1, 2, 3]);
    expect(s.log.map((e) => e.ok)).toEqual([true, false, undefined]);
  });

  it('caps the log and keeps ids increasing', () => {
    let s = initialState();
    for (let i = 0; i < MAX_LOG_ENTRIES + 25; i += 1) s = reduce(s, sw({ type: 'log', entry: { kind: 'info', text: String(i), ts: i } }));
    expect(s.log).toHaveLength(MAX_LOG_ENTRIES);
    expect(s.log[0]?.text).toBe('25');
    expect(s.log.at(-1)?.id).toBe(MAX_LOG_ENTRIES + 25);
  });

  it('stores the settings message', () => {
    const s = run([sw({ type: 'settings', bridgePort: 8765, sites: ['youtube.com'] })]);
    expect(s.bridgePort).toBe(8765);
    expect(s.sites).toEqual(['youtube.com']);
  });
});

describe('voice input', () => {
  const recording = (): PanelState => run([{ type: 'voice.starting' }, { type: 'voice.recording' }], online());
  const transcribing = (id = 'v1'): PanelState => run([{ type: 'voice.transcribing', request_id: id }], recording());
  const result = (id: string, text: string): PanelAction => sw({ type: 'voice.result', request_id: id, text });
  const failure = (id: string, code: 'busy' | 'timeout' | 'provider'): PanelAction => sw({ type: 'voice.error', request_id: id, code, message: 'x' });

  it('starts idle with no error', () => {
    const s = initialState();
    expect(s.voice).toBe('idle');
    expect(s.voiceError).toBeNull();
    expect(s.voiceRequestId).toBeNull();
  });

  it('walks idle, starting, recording, transcribing and back to idle on a transcript', () => {
    let s = online();
    s = run([{ type: 'voice.starting' }], s);
    expect(s.voice).toBe('starting');
    s = run([{ type: 'voice.recording' }], s);
    expect(s.voice).toBe('recording');
    s = run([{ type: 'voice.transcribing', request_id: 'v1' }], s);
    expect(s).toMatchObject({ voice: 'transcribing', voiceRequestId: 'v1' });
    s = run([result('v1', 'busque recetas')], s);
    expect(s).toMatchObject({ voice: 'idle', voiceError: null, voiceRequestId: null });
  });

  it('only moves forward in order', () => {
    const idle = online();
    expect(run([{ type: 'voice.recording' }], idle).voice).toBe('idle');
    expect(run([{ type: 'voice.transcribing', request_id: 'v1' }], idle).voice).toBe('idle');
    const starting = run([{ type: 'voice.starting' }], idle);
    expect(run([{ type: 'voice.transcribing', request_id: 'v1' }], starting).voice).toBe('starting');
    const rec = recording();
    expect(run([{ type: 'voice.recording' }], rec)).toBe(rec);
  });

  it('a failure stores the code, returns to idle and is cleared by the next attempt', () => {
    let s = run([{ type: 'voice.failed', code: 'permission' }], online());
    expect(s).toMatchObject({ voice: 'idle', voiceError: 'permission', voiceRequestId: null });
    s = run([{ type: 'voice.starting' }], s);
    expect(s).toMatchObject({ voice: 'starting', voiceError: null });
  });

  it('shows the transcript only for the current request', () => {
    const s = transcribing('v1');
    expect(run([result('other', 'hola')], s)).toBe(s);
    expect(run([result('v1', 'hola')], online())).toMatchObject({ voice: 'idle' });
    expect(run([failure('other', 'busy')], s)).toBe(s);
  });

  it('turns a blank transcript into the empty error', () => {
    for (const text of ['', '   ', '\n\t']) {
      expect(run([result('v1', text)], transcribing())).toMatchObject({ voice: 'idle', voiceError: 'empty' });
    }
  });

  it('keeps the error code of a voice.error for the current request', () => {
    for (const code of ['busy', 'timeout', 'provider'] as const) {
      expect(run([failure('v1', code)], transcribing())).toMatchObject({ voice: 'idle', voiceError: code, voiceRequestId: null });
    }
  });

  it('ignores answers when nothing is being transcribed', () => {
    const s = online();
    expect(run([result('v1', 'hola')], s)).toBe(s);
    expect(run([failure('v1', 'busy')], s)).toBe(s);
    const rec = recording();
    expect(run([result('v1', 'hola')], rec)).toBe(rec);
  });

  it('loses the recording and the pending request when the port drops', () => {
    for (const s of [run([{ type: 'voice.starting' }], online()), recording(), transcribing()]) {
      expect(run([{ type: 'port', up: false }], s)).toMatchObject({ voice: 'idle', voiceError: 'no_connection', voiceRequestId: null, portUp: false });
    }
    const idle = run([{ type: 'voice.failed', code: 'empty' }], online());
    expect(run([{ type: 'port', up: false }], idle).voiceError).toBe('empty');
  });

  it('ends a recording when the bridge drops, but waits for the worker while transcribing', () => {
    expect(run([status(false, false, false)], recording())).toMatchObject({ voice: 'idle', voiceError: 'no_connection' });
    expect(run([status(false, false, false)], run([{ type: 'voice.starting' }], online()))).toMatchObject({ voice: 'idle', voiceError: 'no_connection' });
    expect(run([status(false, false, false)], transcribing())).toMatchObject({ voice: 'transcribing', voiceRequestId: 'v1' });
    expect(run([status(true, false, true)], recording()).voice).toBe('recording');
  });

  it('sending a task resets the voice state so a late transcript cannot appear', () => {
    const s = run([{ type: 'user.sent', text: 'hola', ts: 1 }], transcribing());
    expect(s).toMatchObject({ voice: 'idle', voiceError: null, voiceRequestId: null });
    expect(run([result('v1', 'tarde')], s)).toBe(s);
  });

  it('Stop is unrelated to voice', () => {
    for (const s of [recording(), transcribing()]) {
      const after = run([{ type: 'user.stopped' }], s);
      expect(after.voice).toBe(s.voice);
      expect(after.voiceRequestId).toBe(s.voiceRequestId);
    }
  });

  it('the microphone button is disabled while disconnected, starting or transcribing', () => {
    expect(micDisabled(initialState())).toBe(true);
    expect(micDisabled(online())).toBe(false);
    expect(micDisabled(run([{ type: 'voice.starting' }], online()))).toBe(true);
    expect(micDisabled(recording())).toBe(false);
    expect(micDisabled(transcribing())).toBe(true);
    expect(micDisabled(run([sw({ type: 'run_state', state: 'running' })], online()))).toBe(false);
  });

  it('the microphone cannot start while a card waits for an answer, but a recording in progress can still finish', () => {
    const ask = sw({ type: 'ask', request_id: 'a1', question: '¿Cómo se llama?', kind: 'text' });
    const allow = sw({ type: 'allow', request_id: 'p1', host: 'example.org', url: 'https://example.org/' });
    for (const action of [confirm('c1'), ask, allow]) {
      const waiting = run([action], online());
      expect(micBlockedByRequest(waiting)).toBe(true);
      expect(micDisabled(waiting)).toBe(true);
      expect(micDisabled(run([{ type: 'voice.starting' }, { type: 'voice.recording' }], run([action], online())))).toBe(false);
    }
    const answered = run([confirm('c1'), { type: 'request.answered', request_id: 'c1' }], online());
    expect(micBlockedByRequest(answered)).toBe(false);
    expect(micDisabled(answered)).toBe(false);
    expect(micBlockedByRequest(run([confirm('c1')]))).toBe(false);
  });

  it('only offers to cancel while preparing or recording', () => {
    expect(showCancel(online())).toBe(false);
    expect(showCancel(run([{ type: 'voice.starting' }], online()))).toBe(true);
    expect(showCancel(recording())).toBe(true);
    expect(showCancel(transcribing())).toBe(false);
  });

  it('cancelling goes back to idle with a notice and no error, and only from preparing or recording', () => {
    for (const s of [run([{ type: 'voice.starting' }], online()), recording()]) {
      expect(run([{ type: 'voice.cancelled' }], s)).toMatchObject({ voice: 'idle', voiceError: null, voiceNotice: 'cancelled', voiceRequestId: null });
    }
    const idle = online();
    expect(run([{ type: 'voice.cancelled' }], idle)).toBe(idle);
    const waiting = transcribing();
    expect(run([{ type: 'voice.cancelled' }], waiting)).toBe(waiting);
  });

  it('the cancel notice is cleared by the next attempt, a failure or a sent task', () => {
    const cancelled = run([{ type: 'voice.cancelled' }], recording());
    expect(voiceNoticeKey('cancelled')).toBe('voiceCancelled');
    expect(run([{ type: 'voice.starting' }], cancelled).voiceNotice).toBeNull();
    expect(run([{ type: 'voice.failed', code: 'empty' }], cancelled).voiceNotice).toBeNull();
    expect(run([{ type: 'user.sent', text: 'hola', ts: 1 }], cancelled).voiceNotice).toBeNull();
  });

  it('a granted permission clears only the permission error', () => {
    const refused = run([{ type: 'voice.failed', code: 'permission' }], online());
    expect(run([{ type: 'voice.permission_granted' }], refused)).toMatchObject({ voice: 'idle', voiceError: null, voiceNotice: 'permission_granted' });
    expect(voiceNoticeKey('permission_granted')).toBe('voicePermissionOk');
    const other = run([{ type: 'voice.failed', code: 'empty' }], online());
    expect(run([{ type: 'voice.permission_granted' }], other)).toBe(other);
    const idle = online();
    expect(run([{ type: 'voice.permission_granted' }], idle)).toBe(idle);
    const starting = run([{ type: 'voice.starting' }], refused);
    expect(run([{ type: 'voice.permission_granted' }], starting)).toBe(starting);
  });

  it('labels the button and the status text for each phase', () => {
    expect(micLabelKey(online())).toBe('voiceStart');
    expect(micLabelKey(recording())).toBe('voiceFinish');
    expect(micLabelKey(run([{ type: 'voice.starting' }], online()))).toBe('voiceWait');
    expect(micLabelKey(transcribing())).toBe('voiceWait');
    expect(voiceStatusKey(online())).toBeNull();
    expect(voiceStatusKey(run([{ type: 'voice.starting' }], online()))).toBe('voiceStarting');
    expect(voiceStatusKey(recording())).toBe('voiceListening');
    expect(voiceStatusKey(transcribing())).toBe('voiceTranscribing');
  });

  it('maps every failure to a locale key and offers the permission button only for permission errors', () => {
    const codes = ['no_connection', 'timeout', 'busy', 'not_configured', 'provider', 'bad_audio', 'empty', 'permission', 'no_device', 'mic_busy', 'capture'] as const;
    const keys = codes.map(voiceErrorKey);
    expect(new Set(keys).size).toBe(codes.length);
    expect(keys.every((key) => /^voiceErr[A-Z]/.test(key))).toBe(true);
    expect(showPermissionButton(run([{ type: 'voice.failed', code: 'permission' }], online()))).toBe(true);
    expect(showPermissionButton(run([{ type: 'voice.failed', code: 'empty' }], online()))).toBe(false);
    expect(showPermissionButton(run([{ type: 'voice.failed', code: 'permission' }, { type: 'voice.starting' }], online()))).toBe(false);
  });
});
