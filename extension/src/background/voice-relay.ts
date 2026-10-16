import { VOICE_LIMITS, isVoiceErrorCode, type SwToPanel, type VoiceErrorCode } from '../shared/messages';
import { BabasError } from '../shared/protocol';

export const VOICE_TIMEOUT_MS = 90_000;
export const MAX_PENDING_VOICE = 8;

export interface VoiceAudioMessage {
  request_id: string;
  audio_base64: string;
  duration_ms: number;
}

export interface VoiceRelayDeps<P> {
  isBridgeUp: () => boolean;
  emit: (type: string, payload: Record<string, unknown>) => void;
  send: (port: P, message: SwToPanel) => void;
  text: (code: VoiceErrorCode) => string;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  timeoutMs?: number;
}

interface Pending<P> {
  port: P;
  timer: unknown;
}

type Outcome = { text: string } | { code: VoiceErrorCode; message: string };

// Audio and transcripts only travel through here: nothing is stored, and nothing is logged.
export class VoiceRelay<P> {
  private readonly pending = new Map<string, Pending<P>>();
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;
  private readonly timeoutMs: number;

  constructor(private readonly deps: VoiceRelayDeps<P>) {
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = deps.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
    this.timeoutMs = deps.timeoutMs ?? VOICE_TIMEOUT_MS;
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  request(port: P, message: VoiceAudioMessage): void {
    const id = message.request_id;
    if (this.pending.has(id)) return;
    if (!this.deps.isBridgeUp()) {
      this.fail(port, id, 'no_connection');
      return;
    }
    if (this.pending.size >= MAX_PENDING_VOICE) {
      this.fail(port, id, 'busy');
      return;
    }
    this.pending.set(id, { port, timer: this.setTimer(() => this.expire(id), this.timeoutMs) });
    try {
      this.deps.emit('stt.transcribe', { request_id: id, audio_base64: message.audio_base64, mime: 'audio/wav', duration_ms: message.duration_ms });
    } catch {
      this.settle(id);
      this.fail(port, id, 'no_connection');
    }
  }

  handleTranscript(payload: Record<string, unknown>): Record<string, unknown> {
    const id = payload.request_id;
    if (typeof id !== 'string' || id.length === 0 || id.length > VOICE_LIMITS.maxIdChars) {
      throw new BabasError('BAD_REQUEST', `request_id must be a string of 1-${VOICE_LIMITS.maxIdChars} characters`);
    }
    if (!this.pending.has(id)) return {};
    const outcome = this.parseOutcome(payload);
    const entry = this.settle(id);
    if (!entry) return {};
    if (outcome === null) {
      this.fail(entry.port, id, 'provider');
      throw new BabasError('BAD_REQUEST', 'ui.transcript needs exactly one of text or error');
    }
    if ('text' in outcome) this.deps.send(entry.port, { type: 'voice.result', request_id: id, text: outcome.text });
    else this.deps.send(entry.port, { type: 'voice.error', request_id: id, code: outcome.code, message: outcome.message });
    return {};
  }

  portClosed(port: P): void {
    for (const [id, entry] of [...this.pending]) {
      if (entry.port !== port) continue;
      this.clearTimer(entry.timer);
      this.pending.delete(id);
    }
  }

  bridgeDown(): void {
    for (const id of [...this.pending.keys()]) {
      const entry = this.settle(id);
      if (entry) this.fail(entry.port, id, 'no_connection');
    }
  }

  private expire(id: string): void {
    const entry = this.pending.get(id);
    if (!entry) return;
    this.pending.delete(id);
    this.fail(entry.port, id, 'timeout');
  }

  private settle(id: string): Pending<P> | undefined {
    const entry = this.pending.get(id);
    if (!entry) return undefined;
    this.clearTimer(entry.timer);
    this.pending.delete(id);
    return entry;
  }

  private fail(port: P, id: string, code: VoiceErrorCode): void {
    this.deps.send(port, { type: 'voice.error', request_id: id, code, message: this.deps.text(code) });
  }

  private parseOutcome(payload: Record<string, unknown>): Outcome | null {
    const hasText = payload.text !== undefined && payload.text !== null;
    const hasError = payload.error !== undefined && payload.error !== null;
    if (hasText === hasError) return null;
    if (hasText) {
      const text = payload.text;
      return typeof text === 'string' && text.length <= VOICE_LIMITS.maxTextChars ? { text } : null;
    }
    const error = payload.error;
    if (typeof error !== 'object' || error === null || Array.isArray(error)) return null;
    const { code, message } = error as Record<string, unknown>;
    const known: VoiceErrorCode = isVoiceErrorCode(code) ? code : 'provider';
    const readable = typeof message === 'string' && message.trim() !== '' ? message.slice(0, VOICE_LIMITS.maxMessageChars) : this.deps.text(known);
    return { code: known, message: readable };
  }
}
