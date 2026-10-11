import { describe, expect, it } from 'vitest';
import cases from '../../../tests/fixtures/pii_cases.json';
import { detectPii, maskValue, type PiiKind } from './pii';

const POSITIVE: Array<[PiiKind, string]> = Object.entries(cases.positive).flatMap(([kind, values]) =>
  values.map((value): [PiiKind, string] => [kind as PiiKind, value]),
);

const NEGATIVE: string[] = cases.negative;

describe('detectPii', () => {
  it('has shared vectors for every kind', () => {
    expect(Object.keys(cases.positive).sort()).toEqual(['card', 'curp', 'email', 'nss', 'phone', 'rfc']);
  });

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
