import { describe, expect, it } from 'vitest';
import { detectPii, maskValue, type PiiKind } from './pii';

const POSITIVE: Array<[PiiKind, string]> = [
  ['curp', 'GOMA800101HJCRRN09'],
  ['curp', 'goma800101hjcrrn09'],
  ['curp', 'Mi CURP es GOMA800101MJCRRN05.'],
  ['curp', 'LOPE900215XDFPRS08'],
  ['rfc', 'GOMA800101AB1'],
  ['rfc', 'GOMA800101'],
  ['rfc', 'ABC800101XY2'],
  ['rfc', 'mi rfc: gama800101ab1'],
  ['rfc', 'ÑAÑA800101AB1'],
  ['nss', '12345678901'],
  ['nss', '12 34 56 7890 1'],
  ['nss', '12-34-56-7890-1'],
  ['nss', 'NSS: 48-12-90-3321-7'],
  ['email', 'ana@example.com'],
  ['email', 'ANA.GOMEZ+tramites@correo.com.mx'],
  ['email', 'escribame a ana_g@example.org por favor'],
  ['phone', '3312345678'],
  ['phone', '55 1234 5678'],
  ['phone', '55-1234-5678'],
  ['phone', '(33) 1234-5678'],
  ['phone', '+52 55 1234 5678'],
  ['phone', '+52 1 55 1234 5678'],
  ['phone', 'llame al 331 234 5678'],
  ['card', '4111111111111111'],
  ['card', '4111 1111 1111 1111'],
  ['card', '4111-1111-1111-1111'],
  ['card', '5555 5555 5555 4444'],
  ['card', '378282246310005'],
  ['card', 'mi tarjeta es 4012 8888 8888 1881 gracias'],
];

const NEGATIVE: string[] = [
  '',
  '2026-10-11',
  '11/10/2026',
  'recetas de pozole',
  'cómo sacar mi acta de nacimiento',
  '123456789',
  '12345',
  '$1,234.50',
  '1234.50',
  'Hotel Plaza, Calle 5 de Mayo 123',
  'Copyright 2026',
  'Vuelo AM1234',
  'ana arroba example punto com',
  'ana@',
  '@example.com',
];

describe('detectPii', () => {
  it.each(POSITIVE)('detects %s in %j', (kind, value) => {
    expect(detectPii(value)).toContain(kind);
  });

  it.each(NEGATIVE)('finds nothing in %j', (value) => {
    expect(detectPii(value)).toEqual([]);
  });

  it('does not flag a card number that fails the Luhn check', () => {
    expect(detectPii('4111 1111 1111 1112')).not.toContain('card');
    expect(detectPii('4111111111111112')).not.toContain('card');
    expect(detectPii('1234 5678 9012 3456')).not.toContain('card');
  });

  it('does not flag a card with too few digits even if it passes Luhn', () => {
    expect(detectPii('424242424242')).not.toContain('card');
  });

  it('reports several kinds when a value contains several', () => {
    const kinds = detectPii('ana@example.com y 55 1234 5678');
    expect(kinds).toContain('email');
    expect(kinds).toContain('phone');
  });

  it('reports each kind once', () => {
    const kinds = detectPii('ana@example.com, bob@example.com');
    expect(kinds.filter((k) => k === 'email')).toHaveLength(1);
  });

  it('is stable across repeated calls', () => {
    for (let i = 0; i < 3; i += 1) {
      expect(detectPii('GOMA800101HJCRRN09')).toContain('curp');
      expect(detectPii('4111 1111 1111 1111')).toContain('card');
    }
  });
});

describe('maskValue', () => {
  it('stars values of 4 characters or fewer', () => {
    expect(maskValue('')).toBe('');
    expect(maskValue('ab')).toBe('**');
    expect(maskValue('abcd')).toBe('****');
  });

  it('keeps the first 2 and last 2 characters', () => {
    expect(maskValue('abcde')).toBe('ab*de');
    expect(maskValue('GOMA800101HJCRRN09')).toBe('GO**************09');
    expect(maskValue('ana@example.com')).toBe('an***********om');
  });

  it('never reveals the middle of the value', () => {
    expect(maskValue('GOMA800101HJCRRN09')).not.toContain('800101');
  });
});
