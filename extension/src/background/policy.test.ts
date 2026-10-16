import { describe, expect, it } from 'vitest';
import { BabasError } from '../shared/protocol';
import {
  DEFAULT_SITES,
  actionDigest,
  confirmLine,
  confirmTitle,
  hostAllowed,
  hostCheckFor,
  isHttpUrl,
  isPanelUrl,
  normalizeHost,
  parseConfirmPayload,
  plausibleSiteHost,
  timeoutMsFrom,
  type Translate,
} from './policy';

const t: Translate = (key, subs = []) => `${key}[${subs.join('|')}]`;

describe('normalizeHost', () => {
  it.each([
    ['gob.mx', 'gob.mx'],
    ['WWW.Gob.MX', 'gob.mx'],
    ['https://www.google.com/search?q=1#x', 'google.com'],
    ['https://user:secret@www.GOOGLE.com:8443/a', 'google.com'],
    ['gob.mx:8080/tramites', 'gob.mx'],
    ['gob.mx.', 'gob.mx'],
    ['http://127.0.0.1:8000/form.html', '127.0.0.1'],
    ['localhost:3000', 'localhost'],
    ['http://[::1]:80/', '[::1]'],
    ['bücher.de', 'xn--bcher-kva.de'],
    ['https://BÜCHER.de/x', 'xn--bcher-kva.de'],
    ['gob.mx/redirect?u=http://evil.com', 'gob.mx'],
    ['https://gob.mx@evil.com/', 'evil.com'],
    ['https://evil.com\\@gob.mx/', 'evil.com'],
  ])('%s -> %s', (input, expected) => {
    expect(normalizeHost(input)).toBe(expected);
  });

  it.each(['', '   ', 'http://', 'http://%'])('returns an empty string for %j', (input) => {
    expect(normalizeHost(input)).toBe('');
  });
});

describe('hostAllowed', () => {
  const sites = [...DEFAULT_SITES, '127.0.0.1'];

  it.each([
    'gob.mx',
    'www.gob.mx',
    'WWW.GOB.MX',
    'sat.gob.mx',
    'citas.imss.gob.mx',
    'https://www.youtube.com/watch?v=1',
    'google.com.mx',
    'mail.google.com',
    'es.wikipedia.org',
    'web.whatsapp.com',
    '127.0.0.1',
    'http://127.0.0.1:9000/x',
  ])('allows %s', (host) => {
    expect(hostAllowed(host, sites)).toBe(true);
  });

  it.each([
    'gob.mx.evil.com',
    'evilgob.mx',
    'notgob.mx',
    'evil-gob.mx',
    'mx',
    'com',
    'google.com.evil.com',
    'google.com.mx.evil.com',
    'youtube.com.attacker.mx',
    'xgoogle.com',
    '1.127.0.0.1',
    'localhost',
    'evil.com',
    'https://gob.mx@evil.com/',
    'https://evil.com/?u=gob.mx',
    '',
  ])('refuses %j', (host) => {
    expect(hostAllowed(host, sites)).toBe(false);
  });

  it('never matches a wildcard or empty entry', () => {
    expect(hostAllowed('anything.com', ['*'])).toBe(false);
    expect(hostAllowed('anything.com', ['*.com', '', '  '])).toBe(false);
    expect(hostAllowed('anything.com', [])).toBe(false);
  });

  it('normalizes the stored entries too', () => {
    expect(hostAllowed('sat.gob.mx', ['WWW.GOB.MX'])).toBe(true);
    expect(hostAllowed('gob.mx', ['https://gob.mx/'])).toBe(true);
  });

  it('keeps the default list short and curated', () => {
    expect([...DEFAULT_SITES]).toEqual(['gob.mx', 'google.com', 'google.com.mx', 'youtube.com', 'whatsapp.com', 'wikipedia.org']);
  });
});

describe('plausibleSiteHost', () => {
  it('rejects bare single-label names such as top-level domains', () => {
    expect(plausibleSiteHost('com')).toBe(false);
    expect(plausibleSiteHost('mx')).toBe(false);
    expect(plausibleSiteHost('intranet')).toBe(false);
  });

  it('rejects names with odd characters', () => {
    expect(plausibleSiteHost('a%20b.com')).toBe(false);
    expect(plausibleSiteHost('a b.com')).toBe(false);
    expect(plausibleSiteHost('example.com/path')).toBe(false);
    expect(plausibleSiteHost('example.com:8080')).toBe(false);
    expect(plausibleSiteHost('')).toBe(false);
    expect(plausibleSiteHost('[::1')).toBe(false);
  });

  it('accepts dotted names, localhost and IP literals', () => {
    expect(plausibleSiteHost('example.com')).toBe(true);
    expect(plausibleSiteHost('localhost')).toBe(true);
    expect(plausibleSiteHost('127.0.0.1')).toBe(true);
    expect(plausibleSiteHost('[::1]')).toBe(true);
    expect(plausibleSiteHost('xn--bcher-kva.de')).toBe(true);
    expect(plausibleSiteHost('mi_sitio.example.com')).toBe(true);
  });
});

describe('hostCheckFor', () => {
  it.each(['', undefined, 'about:blank', 'chrome://newtab/', 'chrome-extension://abc/sidepanel.html', 'chrome-error://chromewebstore/', 'data:text/html,hi'])(
    'has nothing to check for %j',
    (url) => {
      expect(hostCheckFor(url)).toEqual({ check: false });
    },
  );

  it('checks the host of http and https pages', () => {
    expect(hostCheckFor('https://www.gob.mx/curp?a=1')).toEqual({ check: true, host: 'gob.mx' });
    expect(hostCheckFor('http://127.0.0.1:8000/form.html')).toEqual({ check: true, host: '127.0.0.1' });
  });

  it('checks the origin that a blob page inherits', () => {
    expect(hostCheckFor('blob:https://evil.com/0f1c')).toEqual({ check: true, host: 'evil.com' });
    expect(hostCheckFor('blob:https://gob.mx/0f1c')).toEqual({ check: true, host: 'gob.mx' });
  });

  it('fails closed for local files, ftp and unparsable addresses', () => {
    expect(hostCheckFor('file:///etc/passwd')).toEqual({ check: true, host: '' });
    expect(hostCheckFor('ftp://files.example.com/a')).toEqual({ check: true, host: '' });
    expect(hostCheckFor('not a url')).toEqual({ check: true, host: '' });
    expect(hostAllowed('', DEFAULT_SITES)).toBe(false);
  });
});

describe('isHttpUrl', () => {
  it('accepts only http and https', () => {
    expect(isHttpUrl('https://gob.mx')).toBe(true);
    expect(isHttpUrl('HTTP://gob.mx')).toBe(true);
    expect(isHttpUrl('javascript:alert(1)')).toBe(false);
    expect(isHttpUrl('file:///x')).toBe(false);
    expect(isHttpUrl('gob.mx')).toBe(false);
  });
});

describe('isPanelUrl', () => {
  const base = 'chrome-extension://abc/sidepanel.html';

  it('accepts the panel page and nothing else', () => {
    expect(isPanelUrl(base, base)).toBe(true);
    expect(isPanelUrl(`${base}?x=1`, base)).toBe(true);
    expect(isPanelUrl(`${base}#top`, base)).toBe(true);
    expect(isPanelUrl('https://gob.mx/sidepanel.html', base)).toBe(false);
    expect(isPanelUrl('chrome-extension://abc/other.html', base)).toBe(false);
    expect(isPanelUrl(`${base}x`, base)).toBe(false);
    expect(isPanelUrl(undefined, base)).toBe(false);
    expect(isPanelUrl('', base)).toBe(false);
  });
});

describe('actionDigest', () => {
  const base = {
    op: 'type',
    target: { snapshot_id: 's1', index: 4, selector: '#correo', text: 'Correo' },
    text: 'ana@example.com',
    value: 'v',
    key: 'Enter',
  };

  it('joins the fields in the PROTOCOL order with the unit separator', () => {
    expect(actionDigest(7, base)).toBe(['7', 'type', 's1', '4', '#correo', 'Correo', 'ana@example.com', 'v', 'Enter'].join('␟'));
    expect(actionDigest(7, { op: 'press' })).toBe(['7', 'press', '', '', '', '', '', '', ''].join('␟'));
  });

  it('is deterministic', () => {
    expect(actionDigest(7, base)).toBe(actionDigest(7, structuredClone(base)));
  });

  it('ignores fields outside the digest', () => {
    expect(actionDigest(7, { ...base, confirmation_id: 'a', direction: 'up', amount: 5 } as typeof base)).toBe(actionDigest(7, base));
  });

  it.each([
    ['tab', (a: typeof base) => ({ tab: 8, a })],
    ['op', (a: typeof base) => ({ tab: 7, a: { ...a, op: 'select' } })],
    ['snapshot_id', (a: typeof base) => ({ tab: 7, a: { ...a, target: { ...a.target, snapshot_id: 's2' } } })],
    ['index', (a: typeof base) => ({ tab: 7, a: { ...a, target: { ...a.target, index: 5 } } })],
    ['selector', (a: typeof base) => ({ tab: 7, a: { ...a, target: { ...a.target, selector: '#telefono' } } })],
    ['target text', (a: typeof base) => ({ tab: 7, a: { ...a, target: { ...a.target, text: 'Teléfono' } } })],
    ['text', (a: typeof base) => ({ tab: 7, a: { ...a, text: 'otro@example.com' } })],
    ['value', (a: typeof base) => ({ tab: 7, a: { ...a, value: 'w' } })],
    ['key', (a: typeof base) => ({ tab: 7, a: { ...a, key: 'Tab' } })],
  ])('changes when the %s changes', (_name, change) => {
    const { tab, a } = change(base);
    expect(actionDigest(tab, a)).not.toBe(actionDigest(7, base));
  });

  it('tells an index of 0 from no index', () => {
    expect(actionDigest(1, { op: 'click', target: { index: 0 } })).not.toBe(actionDigest(1, { op: 'click', target: {} }));
  });

  it('cannot be confused by a separator or a backslash inside a field', () => {
    const sep = '\u241f';
    const parts = ['', 'a', '\\', sep, `\\${sep}`];
    const seen = new Set<string>();
    for (const text of parts) {
      for (const value of parts) {
        for (const key of parts) seen.add(actionDigest(1, { op: 'type', target: { index: 1 }, text, value, key }));
      }
    }
    expect(seen.size).toBe(parts.length ** 3);
    const shifted = actionDigest(1, { op: 'type', target: { index: 1 }, text: `x${sep}y`, value: '', key: 'z' });
    const split = actionDigest(1, { op: 'type', target: { index: 1 }, text: 'x', value: 'y', key: `${sep}z` });
    expect(shifted).not.toBe(split);
  });
});

describe('confirmTitle', () => {
  it.each([
    [['payment'], 'titlePayment[]'],
    [['pii_field', 'payment'], 'titlePayment[]'],
    [['submit_form'], 'titleSubmit[]'],
    [['enter_submits_form'], 'titleSubmit[]'],
    [['pii_field', 'submit_form'], 'titleSubmit[]'],
    [['pii_field'], 'titlePii[]'],
    [['pii_value'], 'titlePii[]'],
    [['consequential'], 'titleImportant[]'],
    [['macro', 'llm_consequential'], 'titleImportant[]'],
    [['enter_in_editor'], 'titleImportant[]'],
    [[], 'titleImportant[]'],
  ])('%j -> %s', (reasons, expected) => {
    expect(confirmTitle(reasons, t)).toBe(expected);
  });
});

describe('confirmLine', () => {
  it('passes the substitutions in the order the locale placeholders declare', () => {
    expect(confirmLine('click', 'Enviar', 'gob.mx', {}, t)).toBe('lineClick[Enviar|gob.mx]');
    expect(confirmLine('type', 'Correo', 'gob.mx', { text: 'ana@example.com' }, t)).toBe('lineType[ana@example.com|Correo|gob.mx]');
    expect(confirmLine('select', 'Estado', 'gob.mx', { value: 'Jalisco' }, t)).toBe('lineSelect[Jalisco|Estado|gob.mx]');
    expect(confirmLine('press', 'Buscar', 'gob.mx', { key: 'Enter' }, t)).toBe('linePress[Enter|Buscar|gob.mx]');
  });

  it('falls back to the page label when the label is empty', () => {
    const fallback: Translate = (key, subs = []) => (key === 'pageFallbackLabel' ? 'la página' : `${key}[${subs.join('|')}]`);
    expect(confirmLine('click', '', 'gob.mx', {}, fallback)).toBe('lineClick[la página|gob.mx]');
    expect(confirmLine('press', '   ', 'gob.mx', { key: 'Enter' }, fallback)).toBe('linePress[Enter|la página|gob.mx]');
  });

  it('shows the value in full and defaults the pressed key to Enter', () => {
    const long = 'x'.repeat(500);
    expect(confirmLine('type', 'Campo', 'a.mx', { text: long }, t)).toContain(long);
    expect(confirmLine('press', 'Campo', 'a.mx', {}, t)).toBe('linePress[Enter|Campo|a.mx]');
  });

  it('refuses an op it has no sentence for', () => {
    expect(() => confirmLine('scroll', 'x', 'a.mx', {}, t)).toThrow();
  });
});

describe('timeoutMsFrom', () => {
  it('uses the default and clamps to 1..3600 seconds', () => {
    expect(timeoutMsFrom(undefined, 300)).toBe(300_000);
    expect(timeoutMsFrom('soon', 600)).toBe(600_000);
    expect(timeoutMsFrom(Number.NaN, 300)).toBe(300_000);
    expect(timeoutMsFrom(0, 300)).toBe(1000);
    expect(timeoutMsFrom(-5, 300)).toBe(1000);
    expect(timeoutMsFrom(99_999, 300)).toBe(3_600_000);
    expect(timeoutMsFrom(2.5, 300)).toBe(2500);
  });
});

describe('parseConfirmPayload', () => {
  const action = { op: 'type', target: { text: 'Correo' }, text: 'ana@example.com' };

  it('accepts a valid request and keeps only the known fields', () => {
    const request = parseConfirmPayload({
      actions: [{ ...action, confirmation_id: 'x', junk: 1 }, { op: 'press', key: 'Enter' }],
      reasons: ['pii_value'],
      explanation: 'Voy a escribir su correo',
    });
    expect(request.actions).toEqual([action, { op: 'press', key: 'Enter' }]);
    expect(request.reasons).toEqual(['pii_value']);
    expect(request.explanation).toBe('Voy a escribir su correo');
  });

  it('treats a missing explanation and missing reasons as empty', () => {
    const request = parseConfirmPayload({ actions: [{ op: 'click', target: { index: 1, snapshot_id: 's' } }] });
    expect(request.explanation).toBe('');
    expect(request.reasons).toEqual([]);
  });

  it.each([
    ['no actions', { actions: [] }],
    ['actions not a list', { actions: 'click' }],
    ['more than 20 actions', { actions: Array.from({ length: 21 }, () => ({ op: 'click', target: { index: 1 } })) }],
    ['unknown op', { actions: [{ op: 'scroll', target: { index: 1 } }] }],
    ['op missing', { actions: [{ target: { index: 1 } }] }],
    ['click without target', { actions: [{ op: 'click' }] }],
    ['type with an empty target', { actions: [{ op: 'type', target: {}, text: 'x' }] }],
    ['press with an invalid target', { actions: [{ op: 'press', target: { index: -1 } }] }],
    ['text not a string', { actions: [{ op: 'type', target: { index: 1 }, text: 5 }] }],
    ['text too long', { actions: [{ op: 'type', target: { index: 1 }, text: 'x'.repeat(4001) }] }],
    ['target text too long', { actions: [{ op: 'click', target: { text: 'x'.repeat(4001) } }] }],
    ['action not an object', { actions: ['click'] }],
    ['reasons not strings', { actions: [{ op: 'click', target: { index: 1 } }], reasons: [1] }],
    ['explanation not a string', { actions: [{ op: 'click', target: { index: 1 } }], explanation: 5 }],
    ['explanation too long', { actions: [{ op: 'click', target: { index: 1 } }], explanation: 'x'.repeat(4001) }],
  ])('rejects %s', (_name, payload) => {
    try {
      parseConfirmPayload(payload as Record<string, unknown>);
      throw new Error('expected a BAD_REQUEST');
    } catch (err) {
      expect(err).toBeInstanceOf(BabasError);
      expect((err as BabasError).code).toBe('BAD_REQUEST');
    }
  });

  it('accepts exactly 20 actions and a 4000 character value', () => {
    const request = parseConfirmPayload({
      actions: Array.from({ length: 20 }, () => ({ op: 'type', target: { index: 1 }, text: 'x'.repeat(4000) })),
    });
    expect(request.actions).toHaveLength(20);
  });
});
