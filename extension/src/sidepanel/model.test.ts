import { describe, expect, it } from 'vitest';
import type { SwToPanel } from '../shared/messages';
import {
  MAX_LOG_ENTRIES,
  activeRequest,
  canSend,
  initialState,
  reduce,
  runStateKey,
  showResume,
  showStop,
  statusKey,
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
