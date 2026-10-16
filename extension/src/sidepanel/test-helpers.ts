import esMessages from '../../public/_locales/es/messages.json';
import type { Translate } from './i18n';

interface MessageEntry {
  message: string;
  placeholders?: Record<string, { content: string }>;
}

const CATALOG = esMessages as Record<string, MessageEntry>;

export const getMessage = (key: string, substitutions?: string | string[]): string => {
  const entry = CATALOG[key];
  if (!entry) return '';
  const subs = substitutions === undefined ? [] : Array.isArray(substitutions) ? substitutions : [substitutions];
  return entry.message.replace(/\$([A-Za-z0-9_@]+)\$/g, (_whole, name: string) => {
    const content = entry.placeholders?.[name.toLowerCase()]?.content ?? '';
    return content.replace(/\$(\d)/g, (_m, n: string) => subs[Number(n) - 1] ?? '');
  });
};

export const tEs: Translate = (key, substitutions) => getMessage(key, substitutions) || key;

export function installChromeI18n(): void {
  (globalThis as unknown as { chrome: unknown }).chrome = { i18n: { getMessage } };
}
