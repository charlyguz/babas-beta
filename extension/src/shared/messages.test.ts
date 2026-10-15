import { describe, expect, it } from 'vitest';
import { VOICE_ERROR_CODES, VOICE_ERROR_KEYS, VOICE_LIMITS, isPanelMessage, isSwMessage, isVoiceErrorCode } from './messages';

const audio = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  type: 'voice.audio',
  request_id: 'r1',
  audio_base64: 'UklGRg==',
  duration_ms: 1500,
  ...over,
});

describe('panel messages that existed before voice input', () => {
  it('still accepts the ordinary messages and rejects unknown or malformed ones', () => {
    expect(isPanelMessage({ type: 'task.start', text: 'hola' })).toBe(true);
    expect(isPanelMessage({ type: 'stop' })).toBe(true);
    expect(isPanelMessage({ type: 'confirm.answer', request_id: 'a', approved: true })).toBe(true);
    expect(isPanelMessage({ type: 'task.start', text: '   ' })).toBe(false);
    expect(isPanelMessage({ type: 'nope' })).toBe(false);
    expect(isPanelMessage(null)).toBe(false);
    expect(isPanelMessage([])).toBe(false);
  });
});

describe('voice.audio (panel to worker)', () => {
  it('accepts a well-formed message', () => {
    expect(isPanelMessage(audio())).toBe(true);
  });

  it('bounds request_id to 1-128 characters', () => {
    expect(isPanelMessage(audio({ request_id: 'x' }))).toBe(true);
    expect(isPanelMessage(audio({ request_id: 'x'.repeat(128) }))).toBe(true);
    expect(isPanelMessage(audio({ request_id: '' }))).toBe(false);
    expect(isPanelMessage(audio({ request_id: 'x'.repeat(129) }))).toBe(false);
    expect(isPanelMessage(audio({ request_id: 7 }))).toBe(false);
    expect(isPanelMessage(audio({ request_id: undefined }))).toBe(false);
  });

  it('requires a non-empty base64 string of at most 1,500,000 characters', () => {
    expect(isPanelMessage(audio({ audio_base64: 'A'.repeat(VOICE_LIMITS.maxAudioChars) }))).toBe(true);
    expect(isPanelMessage(audio({ audio_base64: 'A'.repeat(VOICE_LIMITS.maxAudioChars + 1) }))).toBe(false);
    expect(isPanelMessage(audio({ audio_base64: '' }))).toBe(false);
    expect(isPanelMessage(audio({ audio_base64: 12 }))).toBe(false);
    expect(isPanelMessage(audio({ audio_base64: null }))).toBe(false);
    expect(isPanelMessage(audio({ audio_base64: undefined }))).toBe(false);
  });

  it('requires an integer duration from 1 to 30000 ms', () => {
    for (const ok of [1, 300, 29_999, 30_000]) expect(isPanelMessage(audio({ duration_ms: ok }))).toBe(true);
    for (const bad of [0, -1, 30_001, 1.5, NaN, Infinity, '1500', null, undefined]) expect(isPanelMessage(audio({ duration_ms: bad }))).toBe(false);
  });
});

describe('voice.result and voice.error (worker to panel)', () => {
  it('accepts a result with text up to 4000 characters and rejects the rest', () => {
    expect(isSwMessage({ type: 'voice.result', request_id: 'r1', text: 'busque recetas' })).toBe(true);
    expect(isSwMessage({ type: 'voice.result', request_id: 'r1', text: '' })).toBe(true);
    expect(isSwMessage({ type: 'voice.result', request_id: 'r1', text: 'x'.repeat(4000) })).toBe(true);
    expect(isSwMessage({ type: 'voice.result', request_id: 'r1', text: 'x'.repeat(4001) })).toBe(false);
    expect(isSwMessage({ type: 'voice.result', request_id: 'r1', text: 5 })).toBe(false);
    expect(isSwMessage({ type: 'voice.result', request_id: '', text: 'hola' })).toBe(false);
    expect(isSwMessage({ type: 'voice.result', text: 'hola' })).toBe(false);
  });

  it('accepts every code from the contract and nothing else', () => {
    expect([...VOICE_ERROR_CODES]).toEqual(['no_connection', 'timeout', 'busy', 'not_configured', 'provider', 'bad_audio', 'empty']);
    for (const code of VOICE_ERROR_CODES) {
      expect(isSwMessage({ type: 'voice.error', request_id: 'r1', code, message: 'Algo pasó.' })).toBe(true);
      expect(isVoiceErrorCode(code)).toBe(true);
      expect(VOICE_ERROR_KEYS[code]).toMatch(/^voiceErr[A-Z]/);
    }
    for (const bad of ['', 'permission', 'TIMEOUT', 'unknown', 3, null, undefined]) {
      expect(isSwMessage({ type: 'voice.error', request_id: 'r1', code: bad, message: 'x' })).toBe(false);
      expect(isVoiceErrorCode(bad)).toBe(false);
    }
  });

  it('requires a string message and a valid id', () => {
    expect(isSwMessage({ type: 'voice.error', request_id: 'r1', code: 'busy' })).toBe(false);
    expect(isSwMessage({ type: 'voice.error', request_id: 'r1', code: 'busy', message: 4 })).toBe(false);
    expect(isSwMessage({ type: 'voice.error', request_id: 'r1', code: 'busy', message: 'x'.repeat(VOICE_LIMITS.maxMessageChars + 1) })).toBe(false);
    expect(isSwMessage({ type: 'voice.error', request_id: 'x'.repeat(129), code: 'busy', message: 'x' })).toBe(false);
    expect(isSwMessage({ type: 'voice.error', code: 'busy', message: 'x' })).toBe(false);
  });

  it('does not let a worker message pass as a panel message or the other way round', () => {
    expect(isPanelMessage({ type: 'voice.result', request_id: 'r1', text: 'x' })).toBe(false);
    expect(isPanelMessage({ type: 'voice.error', request_id: 'r1', code: 'busy', message: 'x' })).toBe(false);
    expect(isSwMessage(audio())).toBe(false);
  });
});
