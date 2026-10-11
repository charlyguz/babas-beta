export type PiiKind = 'curp' | 'rfc' | 'nss' | 'email' | 'phone' | 'card';

const PATTERNS: Array<[PiiKind, RegExp]> = [
  ['curp', /(?<![A-Z0-9])[A-Z]{4}\d{6}[HMX][A-Z]{5}[A-Z0-9]\d(?![A-Z0-9])/i],
  ['rfc', /(?<![A-Z0-9Ñ&])[A-ZÑ&]{3,4}\d{6}(?:[A-Z0-9]{3})?(?![A-Z0-9])/i],
  ['email', /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i],
  ['nss', /(?<!\d)\d{2}[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d(?!\d)/],
  ['phone', /(?<![\d+])(?:\+?52[\s-]?)?(?:1[\s-]?)?(?:\(?\d{2}\)?[\s-]?\d{4}|\(?\d{3}\)?[\s-]?\d{3})[\s-]?\d{4}(?!\d)/],
];

const CARD = /(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)/g;

function luhn(digits: string): boolean {
  let sum = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i -= 1) {
    let d = digits.charCodeAt(i) - 48;
    if (double) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    double = !double;
  }
  return sum % 10 === 0;
}

export function detectPii(value: string): PiiKind[] {
  if (!value) return [];
  const found = new Set<PiiKind>();
  for (const match of value.matchAll(CARD)) {
    const digits = match[0].replace(/\D/g, '');
    if (digits.length >= 13 && luhn(digits)) found.add('card');
  }
  for (const [kind, re] of PATTERNS) {
    if (re.test(value)) found.add(kind);
  }
  return [...found];
}

export function maskValue(value: string): string {
  if (value.length <= 4) return '*'.repeat(value.length);
  return `${value.slice(0, 2)}${'*'.repeat(Math.max(value.length - 4, 1))}${value.slice(-2)}`;
}
