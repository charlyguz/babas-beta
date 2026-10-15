import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SwToPanel } from '../shared/messages';
import { BabasError } from '../shared/protocol';
import { MAX_PENDING_VOICE, VOICE_TIMEOUT_MS, VoiceRelay } from './voice-relay';

interface FakePort {
  name: string;
}

interface Harness {
  relay: VoiceRelay<FakePort>;
  emitted: Array<{ type: string; payload: Record<string, unknown> }>;
  sent: Array<{ port: FakePort; message: SwToPanel }>;
  setUp(up: boolean): void;
  failEmit(): void;
}

function setup(): Harness {
  let up = true;
  let broken = false;
  const emitted: Harness['emitted'] = [];
  const sent: Harness['sent'] = [];
  const relay = new VoiceRelay<FakePort>({
    isBridgeUp: () => up,
    emit: (type, payload) => {
      if (broken) throw new Error('socket closed');
      emitted.push({ type, payload });
    },
    send: (port, message) => sent.push({ port, message }),
    text: (code) => `texto:${code}`,
  });
  return {
    relay,
    emitted,
    sent,
    setUp: (value) => {
      up = value;
    },
    failEmit: () => {
      broken = true;
    },
  };
}

const panel: FakePort = { name: 'panel-1' };
const other: FakePort = { name: 'panel-2' };
const audio = (id: string) => ({ request_id: id, audio_base64: 'UklGRg==', duration_ms: 1200 });

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('forwarding audio to Python', () => {
  it('emits stt.transcribe with the audio, the wav mime type and the duration', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    expect(h.emitted).toEqual([{ type: 'stt.transcribe', payload: { request_id: 'r1', audio_base64: 'UklGRg==', mime: 'audio/wav', duration_ms: 1200 } }]);
    expect(h.sent).toEqual([]);
    expect(h.relay.pendingCount).toBe(1);
  });

  it('answers no_connection at once, without emitting, when the bridge is down', () => {
    const h = setup();
    h.setUp(false);
    h.relay.request(panel, audio('r1'));
    expect(h.emitted).toEqual([]);
    expect(h.sent).toEqual([{ port: panel, message: { type: 'voice.error', request_id: 'r1', code: 'no_connection', message: 'texto:no_connection' } }]);
    expect(h.relay.pendingCount).toBe(0);
  });

  it('answers no_connection and forgets the request when the socket refuses the frame', () => {
    const h = setup();
    h.failEmit();
    h.relay.request(panel, audio('r1'));
    expect(h.sent.map((s) => s.message)).toEqual([{ type: 'voice.error', request_id: 'r1', code: 'no_connection', message: 'texto:no_connection' }]);
    expect(h.relay.pendingCount).toBe(0);
    vi.advanceTimersByTime(VOICE_TIMEOUT_MS * 2);
    expect(h.sent).toHaveLength(1);
  });

  it('ignores a request that repeats a pending id', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.request(panel, audio('r1'));
    h.relay.request(other, audio('r1'));
    expect(h.emitted).toHaveLength(1);
    expect(h.sent).toEqual([]);
  });

  it('caps the number of requests in flight with busy', () => {
    const h = setup();
    for (let i = 0; i < MAX_PENDING_VOICE; i += 1) h.relay.request(panel, audio(`r${i}`));
    expect(h.relay.pendingCount).toBe(MAX_PENDING_VOICE);
    h.relay.request(panel, audio('extra'));
    expect(h.emitted).toHaveLength(MAX_PENDING_VOICE);
    expect(h.sent.map((s) => s.message)).toEqual([{ type: 'voice.error', request_id: 'extra', code: 'busy', message: 'texto:busy' }]);
  });

  it('does not keep the audio after forwarding it', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    const pending = (h.relay as unknown as { pending: Map<string, object> }).pending;
    expect(Object.keys(pending.get('r1') ?? {}).sort()).toEqual(['port', 'timer']);
  });
});

describe('ui.transcript from Python', () => {
  it('forwards a transcript to the port that asked and answers {}', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.request(other, audio('r2'));
    expect(h.relay.handleTranscript({ request_id: 'r2', text: 'busque recetas' })).toEqual({});
    expect(h.sent).toEqual([{ port: other, message: { type: 'voice.result', request_id: 'r2', text: 'busque recetas' } }]);
    expect(h.relay.pendingCount).toBe(1);
  });

  it('forwards an error with its code and message', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', error: { code: 'not_configured', message: 'Falta configurar la voz.' } });
    expect(h.sent.map((s) => s.message)).toEqual([{ type: 'voice.error', request_id: 'r1', code: 'not_configured', message: 'Falta configurar la voz.' }]);
  });

  it('turns an unknown error code into provider and fills a missing message', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', error: { code: 'weird', message: '   ' } });
    expect(h.sent.map((s) => s.message)).toEqual([{ type: 'voice.error', request_id: 'r1', code: 'provider', message: 'texto:provider' }]);
  });

  it('bounds the message that Python sends', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', error: { code: 'provider', message: 'x'.repeat(5000) } });
    const message = h.sent[0]?.message;
    expect(message?.type === 'voice.error' && message.message.length).toBe(1000);
  });

  it('forwards an empty transcript as text so the panel can say it heard nothing', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', text: '' });
    expect(h.sent.map((s) => s.message)).toEqual([{ type: 'voice.result', request_id: 'r1', text: '' }]);
  });

  it('ignores unknown and already answered ids', () => {
    const h = setup();
    expect(h.relay.handleTranscript({ request_id: 'ghost', text: 'hola' })).toEqual({});
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', text: 'uno' });
    expect(h.relay.handleTranscript({ request_id: 'r1', text: 'dos' })).toEqual({});
    expect(h.sent).toHaveLength(1);
  });

  it('rejects a payload without a usable request_id', () => {
    const h = setup();
    for (const bad of [undefined, null, 5, '', 'x'.repeat(129)]) {
      expect(() => h.relay.handleTranscript({ request_id: bad, text: 'x' })).toThrow(BabasError);
    }
    expect(() => h.relay.handleTranscript({})).toThrow(/request_id/);
  });

  it.each([
    ['both text and error', { text: 'hola', error: { code: 'empty', message: 'x' } }],
    ['neither', {}],
    ['a text that is not a string', { text: 5 }],
    ['a text over 4000 characters', { text: 'x'.repeat(4001) }],
    ['an error that is not an object', { error: 'boom' }],
    ['an error that is an array', { error: [] }],
  ])('tells the panel provider and answers BAD_REQUEST for %s', (_name, body) => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    let thrown: unknown;
    try {
      h.relay.handleTranscript({ request_id: 'r1', ...body });
    } catch (err) {
      thrown = err;
    }
    expect(thrown).toBeInstanceOf(BabasError);
    expect((thrown as BabasError).code).toBe('BAD_REQUEST');
    expect(h.sent.map((s) => s.message)).toEqual([{ type: 'voice.error', request_id: 'r1', code: 'provider', message: 'texto:provider' }]);
    expect(h.relay.pendingCount).toBe(0);
  });
});

describe('timeout, closed ports and a dropped bridge', () => {
  it('answers timeout after 90 seconds and then ignores the late transcript', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    vi.advanceTimersByTime(VOICE_TIMEOUT_MS - 1);
    expect(h.sent).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(h.sent).toEqual([{ port: panel, message: { type: 'voice.error', request_id: 'r1', code: 'timeout', message: 'texto:timeout' } }]);
    expect(h.relay.pendingCount).toBe(0);
    expect(h.relay.handleTranscript({ request_id: 'r1', text: 'tarde' })).toEqual({});
    expect(h.sent).toHaveLength(1);
  });

  it('the timeout is 90 s by default', () => {
    expect(VOICE_TIMEOUT_MS).toBe(90_000);
  });

  it('cancels the timer once the transcript arrives', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', text: 'hola' });
    vi.advanceTimersByTime(VOICE_TIMEOUT_MS * 2);
    expect(h.sent).toHaveLength(1);
  });

  it('forgets the requests of a port that disconnected, silently', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.request(other, audio('r2'));
    h.relay.portClosed(panel);
    expect(h.relay.pendingCount).toBe(1);
    expect(h.relay.handleTranscript({ request_id: 'r1', text: 'nadie escucha' })).toEqual({});
    vi.advanceTimersByTime(VOICE_TIMEOUT_MS * 2);
    expect(h.sent.map((s) => s.port)).toEqual([other]);
    expect(h.sent[0]?.message).toMatchObject({ type: 'voice.error', request_id: 'r2', code: 'timeout' });
  });

  it('answers no_connection to every waiting port when the bridge drops', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.request(other, audio('r2'));
    h.setUp(false);
    h.relay.bridgeDown();
    expect(h.sent.map((s) => [s.port, s.message])).toEqual([
      [panel, { type: 'voice.error', request_id: 'r1', code: 'no_connection', message: 'texto:no_connection' }],
      [other, { type: 'voice.error', request_id: 'r2', code: 'no_connection', message: 'texto:no_connection' }],
    ]);
    expect(h.relay.pendingCount).toBe(0);
    vi.advanceTimersByTime(VOICE_TIMEOUT_MS * 2);
    expect(h.sent).toHaveLength(2);
  });

  it('a dropped bridge with nothing pending does nothing', () => {
    const h = setup();
    h.relay.bridgeDown();
    expect(h.sent).toEqual([]);
  });

  it('accepts a new request after the previous one finished', () => {
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', text: 'uno' });
    h.relay.request(panel, audio('r2'));
    expect(h.emitted.map((e) => e.payload.request_id)).toEqual(['r1', 'r2']);
  });
});

describe('privacy', () => {
  it('never writes audio or transcripts to the console', () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((name) => vi.spyOn(console, name).mockImplementation(() => undefined));
    const h = setup();
    h.relay.request(panel, audio('r1'));
    h.relay.handleTranscript({ request_id: 'r1', text: 'mi CURP es secreta' });
    h.relay.request(panel, audio('r2'));
    h.relay.handleTranscript({ request_id: 'r2', error: { code: 'provider', message: 'detalle' } });
    h.relay.request(panel, audio('r3'));
    vi.advanceTimersByTime(VOICE_TIMEOUT_MS);
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
