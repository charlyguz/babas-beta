import { afterEach, describe, expect, it } from 'vitest';
import { t } from './i18n';
import { installChromeI18n } from './test-helpers';

describe('t', () => {
  afterEach(() => {
    delete (globalThis as { chrome?: unknown }).chrome;
  });

  it('reads chrome.i18n and applies placeholders', () => {
    installChromeI18n();
    expect(t('send')).toBe('Enviar');
    expect(t('allowText', ['gob.mx'])).toBe('BABAS quiere usar el sitio gob.mx.');
  });

  it('falls back to the key when the string or the API is missing', () => {
    installChromeI18n();
    expect(t('noSuchKey')).toBe('noSuchKey');
    delete (globalThis as { chrome?: unknown }).chrome;
    expect(t('send')).toBe('send');
  });
});
