import { describe, expect, it } from 'vitest';
import { BabasError, PROTOCOL_VERSION, isEnvelope, isResultMessage, isTarget, toErrorInfo } from './protocol';

const FIXTURES = import.meta.glob<Record<string, unknown>>('../../../tests/fixtures/protocol/*.json', {
  eager: true,
  import: 'default',
});

function fixture(name: string): Record<string, unknown> {
  const key = Object.keys(FIXTURES).find((k) => k.endsWith(`/${name}.json`));
  if (!key) throw new Error(`Missing protocol fixture ${name}.json`);
  return FIXTURES[key];
}

const ENVELOPES = ['hello', 'welcome', 'request_page_state', 'event_ping'];
const RESULTS = ['result_error', 'result_page_state'];

describe('shared protocol fixtures', () => {
  it('are all present and every file is classified', () => {
    const names = Object.keys(FIXTURES)
      .map((k) => k.split('/').pop()?.replace(/\.json$/, ''))
      .sort();
    expect(names).toEqual([...ENVELOPES, ...RESULTS].sort());
  });

  it.each(ENVELOPES)('%s is an envelope and not a result', (name) => {
    const msg = fixture(name);
    expect(isEnvelope(msg)).toBe(true);
    expect(isResultMessage(msg)).toBe(false);
  });

  it.each(RESULTS)('%s is a result and not an envelope', (name) => {
    const msg = fixture(name);
    expect(isResultMessage(msg)).toBe(true);
    expect(isEnvelope(msg)).toBe(false);
  });

  it.each([...ENVELOPES, ...RESULTS])('%s uses the current protocol version', (name) => {
    expect(fixture(name).v).toBe(PROTOCOL_VERSION);
  });

  it('hello announces the protocol version the extension speaks', () => {
    const hello = fixture('hello') as { payload: { protocol: number; ext_version: string } };
    expect(hello.payload.protocol).toBe(PROTOCOL_VERSION);
    expect(typeof hello.payload.ext_version).toBe('string');
  });

  it('request and result fixtures share the same id', () => {
    expect(fixture('request_page_state').id).toBe(fixture('result_error').id);
    expect(fixture('request_page_state').id).toBe(fixture('result_page_state').id);
  });

  it('result_error carries a code and a message', () => {
    const error = (fixture('result_error') as { error: { code: string; message: string } }).error;
    expect(error.code).toBe('STALE_SNAPSHOT');
    expect(error.message.length).toBeGreaterThan(0);
  });

  it('result_page_state yields valid targets for every element', () => {
    const payload = (fixture('result_page_state') as { payload: { snapshot_id: string; elements: Array<{ index: number }> } }).payload;
    expect(payload.elements.length).toBeGreaterThan(0);
    for (const el of payload.elements) {
      expect(isTarget({ index: el.index, snapshot_id: payload.snapshot_id })).toBe(true);
    }
  });
});

describe('isEnvelope', () => {
  const base = { v: PROTOCOL_VERSION, id: 'abc', type: 'page.state' };

  it('accepts a message with or without a payload object', () => {
    expect(isEnvelope(base)).toBe(true);
    expect(isEnvelope({ ...base, payload: {} })).toBe(true);
    expect(isEnvelope({ ...base, payload: { highlight: true } })).toBe(true);
  });

  it('rejects other protocol versions', () => {
    expect(isEnvelope({ ...base, v: PROTOCOL_VERSION + 1 })).toBe(false);
    expect(isEnvelope({ ...base, v: '1' })).toBe(false);
    expect(isEnvelope({ id: base.id, type: base.type })).toBe(false);
  });

  it('rejects a missing, empty or non-string id or type', () => {
    expect(isEnvelope({ v: PROTOCOL_VERSION, type: 'ping' })).toBe(false);
    expect(isEnvelope({ ...base, id: '' })).toBe(false);
    expect(isEnvelope({ ...base, id: 7 })).toBe(false);
    expect(isEnvelope({ v: PROTOCOL_VERSION, id: 'abc' })).toBe(false);
    expect(isEnvelope({ ...base, type: 3 })).toBe(false);
  });

  it('rejects a result and a payload that is not an object', () => {
    expect(isEnvelope({ ...base, type: 'result' })).toBe(false);
    expect(isEnvelope({ ...base, payload: [] })).toBe(false);
    expect(isEnvelope({ ...base, payload: 'x' })).toBe(false);
    expect(isEnvelope({ ...base, payload: null })).toBe(false);
  });

  it('rejects things that are not objects', () => {
    for (const value of [null, undefined, 'x', 1, true, []]) expect(isEnvelope(value)).toBe(false);
  });
});

describe('isResultMessage', () => {
  const ok = { v: PROTOCOL_VERSION, id: 'abc', type: 'result', ok: true };
  const failed = { v: PROTOCOL_VERSION, id: 'abc', type: 'result', ok: false, error: { code: 'TIMEOUT', message: 'late' } };

  it('accepts ok results with an object, null or no payload', () => {
    expect(isResultMessage({ ...ok, payload: { a: 1 } })).toBe(true);
    expect(isResultMessage({ ...ok, payload: null })).toBe(true);
    expect(isResultMessage(ok)).toBe(true);
  });

  it('accepts a failed result with a code and a message', () => {
    expect(isResultMessage(failed)).toBe(true);
  });

  it('rejects an ok result whose payload is not an object', () => {
    expect(isResultMessage({ ...ok, payload: [] })).toBe(false);
    expect(isResultMessage({ ...ok, payload: 'x' })).toBe(false);
  });

  it('rejects a failed result without a usable error', () => {
    expect(isResultMessage({ ...failed, error: undefined })).toBe(false);
    expect(isResultMessage({ ...failed, error: null })).toBe(false);
    expect(isResultMessage({ ...failed, error: { code: 'TIMEOUT' } })).toBe(false);
    expect(isResultMessage({ ...failed, error: { message: 'late' } })).toBe(false);
    expect(isResultMessage({ ...failed, error: { code: 5, message: 'late' } })).toBe(false);
    expect(isResultMessage({ ...failed, error: 'TIMEOUT' })).toBe(false);
  });

  it('rejects the wrong version, type, id or ok flag', () => {
    expect(isResultMessage({ ...ok, v: 2 })).toBe(false);
    expect(isResultMessage({ ...ok, type: 'page.state' })).toBe(false);
    expect(isResultMessage({ ...ok, id: 1 })).toBe(false);
    expect(isResultMessage({ ...ok, ok: 'true' })).toBe(false);
    expect(isResultMessage({ v: PROTOCOL_VERSION, id: 'abc', type: 'result' })).toBe(false);
  });

  it('rejects things that are not objects', () => {
    for (const value of [null, undefined, 'x', 1, true, []]) expect(isResultMessage(value)).toBe(false);
  });
});

describe('isTarget', () => {
  it('accepts an index, a selector or a text', () => {
    expect(isTarget({ index: 0 })).toBe(true);
    expect(isTarget({ index: 12, snapshot_id: 'k3j9x0qa' })).toBe(true);
    expect(isTarget({ selector: '#show' })).toBe(true);
    expect(isTarget({ text: 'Buscar' })).toBe(true);
    expect(isTarget({ selector: "button:has-text('Mostrar')", text: 'x' })).toBe(true);
  });

  it('rejects an empty target', () => {
    expect(isTarget({})).toBe(false);
    expect(isTarget({ snapshot_id: 'abc' })).toBe(false);
  });

  it('rejects an index that is not a non-negative integer', () => {
    expect(isTarget({ index: -1 })).toBe(false);
    expect(isTarget({ index: 1.5 })).toBe(false);
    expect(isTarget({ index: '1' })).toBe(false);
    expect(isTarget({ index: null })).toBe(false);
    expect(isTarget({ index: Number.NaN })).toBe(false);
  });

  it('rejects an empty or non-string selector or text', () => {
    expect(isTarget({ selector: '' })).toBe(false);
    expect(isTarget({ text: '' })).toBe(false);
    expect(isTarget({ selector: 3 })).toBe(false);
    expect(isTarget({ text: ['a'] })).toBe(false);
  });

  it('rejects a snapshot_id that is not a string', () => {
    expect(isTarget({ index: 1, snapshot_id: 5 })).toBe(false);
    expect(isTarget({ index: 1, snapshot_id: null })).toBe(false);
  });

  it('rejects things that are not objects', () => {
    for (const value of [null, undefined, 'x', 1, true, []]) expect(isTarget(value)).toBe(false);
  });
});

describe('BabasError', () => {
  it('serialises to the error info of the wire format', () => {
    expect(new BabasError('NOT_FOUND', 'gone').toInfo()).toEqual({ code: 'NOT_FOUND', message: 'gone' });
    expect(new BabasError('TIMEOUT', 'late', { ms: 5 }).toInfo()).toEqual({ code: 'TIMEOUT', message: 'late', data: { ms: 5 } });
  });

  it('produces an error info that passes the result guard', () => {
    const error = toErrorInfo(new BabasError('OBSCURED', 'covered'));
    expect(isResultMessage({ v: PROTOCOL_VERSION, id: 'abc', type: 'result', ok: false, error })).toBe(true);
  });

  it('maps unknown errors to INTERNAL', () => {
    expect(toErrorInfo(new Error('boom'))).toEqual({ code: 'INTERNAL', message: 'boom' });
    expect(toErrorInfo('plain')).toEqual({ code: 'INTERNAL', message: 'plain' });
  });
});
