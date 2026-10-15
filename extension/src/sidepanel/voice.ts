export const TARGET_SAMPLE_RATE = 16_000;
export const MAX_RECORDING_MS = 30_000;
export const MIN_RECORDING_MS = 300;
export const SPEECH_RMS_THRESHOLD = 0.02;
export const MIN_SPEECH_MS = 400;
export const SILENCE_MS = 2000;
export const START_TIMEOUT_MS = 20_000;
export const PROCESSOR_BLOCK = 4096;

const MAX_OUTPUT_SAMPLES = (TARGET_SAMPLE_RATE * MAX_RECORDING_MS) / 1000;
const WAV_HEADER_BYTES = 44;
const BASE64_CHUNK = 0x2000;

export function rms(samples: ArrayLike<number>): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

export function levelFromRms(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.min(1, Math.sqrt(value) * 2.2);
}

export function downmixToMono(channels: readonly Float32Array[]): Float32Array {
  if (channels.length === 0) return new Float32Array(0);
  if (channels.length === 1) return channels[0].slice();
  let length = channels[0].length;
  for (const channel of channels) length = Math.min(length, channel.length);
  const out = new Float32Array(length);
  for (const channel of channels) {
    for (let i = 0; i < length; i += 1) out[i] += channel[i];
  }
  const scale = 1 / channels.length;
  for (let i = 0; i < length; i += 1) out[i] *= scale;
  return out;
}

export function resample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (!Number.isFinite(fromRate) || !Number.isFinite(toRate) || fromRate <= 0 || toRate <= 0) {
    throw new RangeError('Sample rates must be positive numbers');
  }
  if (input.length === 0) return new Float32Array(0);
  if (fromRate === toRate) return input.slice();
  const outLength = Math.floor((input.length * toRate) / fromRate);
  const out = new Float32Array(outLength);
  const ratio = fromRate / toRate;
  if (ratio > 1) {
    // Each output sample is the average of the source span it covers: a cheap low-pass that avoids aliasing.
    for (let i = 0; i < outLength; i += 1) {
      const start = i * ratio;
      const end = Math.min(start + ratio, input.length);
      const last = Math.min(Math.ceil(end), input.length);
      let sum = 0;
      let weight = 0;
      for (let j = Math.floor(start); j < last; j += 1) {
        const w = Math.min(j + 1, end) - Math.max(j, start);
        sum += input[j] * w;
        weight += w;
      }
      out[i] = weight > 0 ? sum / weight : 0;
    }
    return out;
  }
  for (let i = 0; i < outLength; i += 1) {
    const position = i * ratio;
    const index = Math.floor(position);
    const a = input[index];
    const b = input[Math.min(index + 1, input.length - 1)];
    out[i] = a + (b - a) * (position - index);
  }
  return out;
}

export function floatToPcm16(sample: number): number {
  if (Number.isNaN(sample)) return 0;
  const clamped = Math.max(-1, Math.min(1, sample));
  return clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff);
}

export function encodeWav(samples: Float32Array, sampleRate: number = TARGET_SAMPLE_RATE): Uint8Array {
  if (!Number.isInteger(sampleRate) || sampleRate <= 0) throw new RangeError('The sample rate must be a positive integer');
  const dataBytes = samples.length * 2;
  const buffer = new ArrayBuffer(WAV_HEADER_BYTES + dataBytes);
  const view = new DataView(buffer);
  const ascii = (offset: number, text: string): void => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  ascii(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  ascii(8, 'WAVE');
  ascii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, 'data');
  view.setUint32(40, dataBytes, true);
  for (let i = 0; i < samples.length; i += 1) view.setInt16(WAV_HEADER_BYTES + i * 2, floatToPcm16(samples[i]), true);
  return new Uint8Array(buffer);
}

export function bytesToBase64(bytes: Uint8Array): string {
  const parts: string[] = [];
  for (let i = 0; i < bytes.length; i += BASE64_CHUNK) parts.push(String.fromCharCode(...bytes.subarray(i, i + BASE64_CHUNK)));
  return btoa(parts.join(''));
}

function concatChunks(chunks: readonly Float32Array[], total: number): Float32Array {
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

export interface SilenceOptions {
  threshold?: number;
  minSpeechMs?: number;
  silenceMs?: number;
}

export class SilenceDetector {
  private speechMs = 0;
  private silentMs = 0;
  private readonly threshold: number;
  private readonly minSpeechMs: number;
  private readonly silenceMs: number;

  constructor(options: SilenceOptions = {}) {
    this.threshold = options.threshold ?? SPEECH_RMS_THRESHOLD;
    this.minSpeechMs = options.minSpeechMs ?? MIN_SPEECH_MS;
    this.silenceMs = options.silenceMs ?? SILENCE_MS;
  }

  get speechHeard(): boolean {
    return this.speechMs >= this.minSpeechMs;
  }

  push(energy: number, blockMs: number): boolean {
    if (energy >= this.threshold) {
      this.speechMs += blockMs;
      this.silentMs = 0;
    } else if (this.speechHeard) {
      this.silentMs += blockMs;
    }
    return this.speechHeard && this.silentMs >= this.silenceMs;
  }
}

export type CaptureErrorCode = 'permission' | 'no_device' | 'mic_busy' | 'unsupported' | 'capture' | 'empty' | 'cancelled';

export class VoiceCaptureError extends Error {
  constructor(
    readonly code: CaptureErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'VoiceCaptureError';
  }
}

export function classifyCaptureError(err: unknown): VoiceCaptureError {
  if (err instanceof VoiceCaptureError) return err;
  const name = typeof err === 'object' && err !== null && 'name' in err ? String((err as { name: unknown }).name) : '';
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return new VoiceCaptureError('permission', `Microphone access was refused (${name})`);
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return new VoiceCaptureError('no_device', `No microphone found (${name})`);
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return new VoiceCaptureError('mic_busy', `The microphone could not be opened (${name})`);
    default:
      return new VoiceCaptureError('capture', `Recording failed (${name || 'unknown error'})`);
  }
}

export function hasMicrophoneSupport(nav: { mediaDevices?: { getUserMedia?: unknown } } | undefined): boolean {
  return typeof nav?.mediaDevices?.getUserMedia === 'function';
}

export interface ProcessEventLike {
  inputBuffer: { numberOfChannels: number; getChannelData(channel: number): Float32Array };
}

export interface AudioNodeLike {
  connect(destination: unknown): unknown;
  disconnect(): void;
}

export interface ProcessorLike extends AudioNodeLike {
  onaudioprocess: ((event: ProcessEventLike) => void) | null;
}

export interface AudioContextLike {
  readonly sampleRate: number;
  readonly destination: unknown;
  createMediaStreamSource(stream: MediaStream): AudioNodeLike;
  createScriptProcessor(bufferSize: number, inputChannels: number, outputChannels: number): ProcessorLike;
  resume(): Promise<void>;
  close(): Promise<void>;
}

export interface RecorderDeps {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createContext: () => AudioContextLike;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

export interface RecorderHooks {
  onLevel?: (level: number) => void;
  onAutoStop?: () => void;
}

export interface RecordedAudio {
  audioBase64: string;
  durationMs: number;
}

export interface VoiceRecorderLike {
  readonly active: boolean;
  start(): Promise<void>;
  finish(): RecordedAudio;
  cancel(): void;
}

export function browserRecorderDeps(win: Window & typeof globalThis = window): RecorderDeps {
  return {
    getUserMedia: (constraints) => win.navigator.mediaDevices.getUserMedia(constraints),
    createContext: () => {
      if (typeof win.AudioContext !== 'function') throw new VoiceCaptureError('unsupported', 'AudioContext is not available');
      return new win.AudioContext() as unknown as AudioContextLike;
    },
  };
}

type Phase = 'idle' | 'starting' | 'recording';

// ScriptProcessorNode is deprecated but needs no worker file, which the extension CSP would force us to ship separately.
export class VoiceRecorder implements VoiceRecorderLike {
  private phase: Phase = 'idle';
  private generation = 0;
  private context: AudioContextLike | null = null;
  private stream: MediaStream | null = null;
  private source: AudioNodeLike | null = null;
  private processor: ProcessorLike | null = null;
  private abortStart: (() => void) | null = null;
  private maxTimer: unknown = null;
  private chunks: Float32Array[] = [];
  private total = 0;
  private sampleRate = TARGET_SAMPLE_RATE;
  private stopSignalled = false;
  private detector = new SilenceDetector();
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(
    private readonly deps: RecorderDeps,
    private readonly hooks: RecorderHooks = {},
  ) {
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = deps.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  get active(): boolean {
    return this.phase !== 'idle';
  }

  async start(): Promise<void> {
    if (this.phase !== 'idle') throw new VoiceCaptureError('capture', 'The recorder is already in use');
    this.phase = 'starting';
    const generation = ++this.generation;
    // The context is created before the permission prompt so it is born inside the click that started the recording.
    let context: AudioContextLike;
    try {
      context = this.deps.createContext();
    } catch (err) {
      this.phase = 'idle';
      throw err instanceof VoiceCaptureError ? err : new VoiceCaptureError('unsupported', 'AudioContext could not be created');
    }
    this.context = context;
    let stream: MediaStream;
    try {
      stream = await this.acquire();
    } catch (err) {
      if (generation === this.generation) this.release();
      throw classifyCaptureError(err);
    }
    if (generation !== this.generation) {
      stopStream(stream);
      throw new VoiceCaptureError('cancelled', 'The recording was cancelled');
    }
    this.stream = stream;
    try {
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(PROCESSOR_BLOCK, 1, 1);
      processor.onaudioprocess = (event) => this.onBlock(event);
      source.connect(processor);
      processor.connect(context.destination);
      this.source = source;
      this.processor = processor;
      Promise.resolve(context.resume()).catch(() => undefined);
    } catch {
      this.release();
      throw new VoiceCaptureError('capture', 'The audio pipeline could not be built');
    }
    this.sampleRate = context.sampleRate;
    this.detector = new SilenceDetector();
    this.stopSignalled = false;
    this.phase = 'recording';
    this.maxTimer = this.setTimer(() => this.signalStop(), MAX_RECORDING_MS);
  }

  finish(): RecordedAudio {
    if (this.phase !== 'recording') throw new VoiceCaptureError('capture', 'The recorder is not recording');
    const samples = concatChunks(this.chunks, this.total);
    const rate = this.sampleRate;
    this.release();
    const resampled = resample(samples, rate, TARGET_SAMPLE_RATE);
    const trimmed = resampled.length > MAX_OUTPUT_SAMPLES ? resampled.subarray(0, MAX_OUTPUT_SAMPLES) : resampled;
    const durationMs = Math.round((trimmed.length / TARGET_SAMPLE_RATE) * 1000);
    if (durationMs < MIN_RECORDING_MS) throw new VoiceCaptureError('empty', 'The recording is too short');
    return { audioBase64: bytesToBase64(encodeWav(trimmed, TARGET_SAMPLE_RATE)), durationMs };
  }

  cancel(): void {
    this.release();
  }

  private acquire(): Promise<MediaStream> {
    return new Promise<MediaStream>((resolve, reject) => {
      let settled = false;
      const timer = this.setTimer(() => {
        if (settled) return;
        settled = true;
        this.abortStart = null;
        reject(new VoiceCaptureError('permission', 'The microphone permission prompt never answered'));
      }, START_TIMEOUT_MS);
      this.abortStart = () => {
        if (settled) return;
        settled = true;
        this.abortStart = null;
        this.clearTimer(timer);
        reject(new VoiceCaptureError('cancelled', 'The recording was cancelled'));
      };
      let pending: Promise<MediaStream>;
      try {
        pending = this.deps.getUserMedia({ audio: true });
      } catch (err) {
        settled = true;
        this.abortStart = null;
        this.clearTimer(timer);
        reject(err);
        return;
      }
      pending.then(
        (stream) => {
          this.clearTimer(timer);
          if (settled) {
            stopStream(stream);
            return;
          }
          settled = true;
          this.abortStart = null;
          resolve(stream);
        },
        (err: unknown) => {
          this.clearTimer(timer);
          if (settled) return;
          settled = true;
          this.abortStart = null;
          reject(err);
        },
      );
    });
  }

  private onBlock(event: ProcessEventLike): void {
    if (this.phase !== 'recording') return;
    const buffer = event.inputBuffer;
    const channels: Float32Array[] = [];
    for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) channels.push(buffer.getChannelData(channel));
    const block = downmixToMono(channels);
    if (block.length === 0) return;
    const stopAt = Math.round((this.sampleRate * MAX_RECORDING_MS) / 1000);
    if (this.total < stopAt + this.sampleRate) {
      this.chunks.push(block);
      this.total += block.length;
    }
    const energy = rms(block);
    this.hooks.onLevel?.(levelFromRms(energy));
    const quiet = this.detector.push(energy, (block.length / this.sampleRate) * 1000);
    if (quiet || this.total >= stopAt) this.signalStop();
  }

  private signalStop(): void {
    if (this.phase !== 'recording' || this.stopSignalled) return;
    this.stopSignalled = true;
    this.hooks.onAutoStop?.();
  }

  private release(): void {
    this.generation += 1;
    this.abortStart?.();
    this.abortStart = null;
    if (this.maxTimer !== null) this.clearTimer(this.maxTimer);
    this.maxTimer = null;
    if (this.processor) this.processor.onaudioprocess = null;
    for (const node of [this.processor, this.source]) {
      try {
        node?.disconnect();
      } catch {
        // the node is already disconnected
      }
    }
    if (this.stream) stopStream(this.stream);
    const context = this.context;
    if (context) {
      try {
        Promise.resolve(context.close()).catch(() => undefined);
      } catch {
        // the context is already closed
      }
    }
    this.context = null;
    this.stream = null;
    this.source = null;
    this.processor = null;
    this.chunks = [];
    this.total = 0;
    this.stopSignalled = false;
    this.phase = 'idle';
  }
}

function stopStream(stream: MediaStream): void {
  for (const track of stream.getTracks()) {
    try {
      track.stop();
    } catch {
      // a track that cannot stop is already dead
    }
  }
}
