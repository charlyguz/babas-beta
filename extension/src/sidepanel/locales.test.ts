import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import enMessages from '../../public/_locales/en/messages.json';
import esMessages from '../../public/_locales/es/messages.json';
import { VOICE_ERROR_KEYS } from '../shared/messages';

interface Entry {
  message: string;
  placeholders?: Record<string, { content: string }>;
}

const es = esMessages as Record<string, Entry>;
const en = enMessages as Record<string, Entry>;
const srcDir = join(process.cwd(), 'src');

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.ts$/.test(name) && !/\.test\.ts$/.test(name) && !/test-helpers\.ts$/.test(name) ? [path] : [];
  });
}

describe('locales', () => {
  it('es and en define exactly the same keys', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(es).sort());
  });

  it('every message is a non-empty string and placeholders match between languages', () => {
    for (const [key, entry] of Object.entries(es)) {
      expect(typeof entry.message, key).toBe('string');
      expect(entry.message.trim(), key).not.toBe('');
      expect(typeof en[key]?.message, key).toBe('string');
      expect(en[key]?.message.trim(), key).not.toBe('');
      expect(Object.keys(en[key]?.placeholders ?? {}).sort(), key).toEqual(Object.keys(entry.placeholders ?? {}).sort());
      for (const lang of [entry, en[key] as Entry]) {
        const used = [...lang.message.matchAll(/\$([A-Za-z0-9_@]+)\$/g)].map((m) => (m[1] as string).toLowerCase()).sort();
        expect(used, key).toEqual(Object.keys(lang.placeholders ?? {}).sort());
      }
    }
  });

  it('has a string for every voice error the panel can show, in both languages', () => {
    const keys = [...Object.values(VOICE_ERROR_KEYS), 'voiceErrPermission', 'voiceErrNoDevice', 'voiceErrMicBusy', 'voiceErrCapture'];
    for (const key of keys) {
      expect(es[key], key).toBeDefined();
      expect(en[key], key).toBeDefined();
    }
  });

  it('has every string the voice UI and the permission page ask for', () => {
    const keys = [
      'voiceStart',
      'voiceFinish',
      'voiceStarting',
      'voiceListening',
      'voiceListeningHint',
      'voiceTranscribing',
      'voiceGrantPermission',
      'micPermTitle',
      'micPermIntro',
      'micPermButton',
      'micPermWaiting',
      'micPermDone',
      'micPermDenied',
      'micPermNoDevice',
      'micPermError',
    ];
    for (const key of keys) {
      expect(es[key], key).toBeDefined();
      expect(en[key], key).toBeDefined();
    }
    expect(es.voiceStart?.message).toBe('Hablar');
    expect(es.voiceFinish?.message).toBe('Listo');
    expect(es.voiceListening?.message).toBe('Estoy escuchando…');
    expect(es.voiceTranscribing?.message).toBe('Escribiendo lo que dijo…');
    expect(es.voiceGrantPermission?.message).toBe('Dar permiso');
    expect(es.micPermButton?.message).toBe('Permitir el micrófono');
    expect(es.micPermDone?.message).toBe('Listo. Ya puede cerrar esta pestaña y volver a BABAS.');
  });

  it('every literal t("key") in the extension sources exists', () => {
    const missing: string[] = [];
    for (const file of sourceFiles(srcDir)) {
      for (const match of readFileSync(file, 'utf8').matchAll(/\bt\(\s*'([A-Za-z0-9_]+)'/g)) {
        const key = match[1] as string;
        if (!es[key] || !en[key]) missing.push(`${file}: ${key}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it('the Spanish voice texts talk to the user as usted, without technical words', () => {
    const voiceKeys = Object.keys(es).filter((key) => key.startsWith('voice') || key.startsWith('micPerm'));
    for (const key of voiceKeys) {
      const message = es[key]?.message ?? '';
      expect(message, key).not.toMatch(/\b(tú|tu|puedes|tienes|quieres|intenta|revisa|elige|haz)\b/i);
      expect(message, key).not.toMatch(/\b(API|token|proveedor|STT|WebSocket|bridge|puerto|error \d+)\b/i);
    }
  });
});
