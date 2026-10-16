import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MAX_RECORDING_MS,
  MIN_SPEECH_MS,
  PROCESSOR_BLOCK,
  SILENCE_MS,
  SPEECH_RMS_THRESHOLD,
  START_TIMEOUT_MS,
  SilenceDetector,
  VoiceCaptureError,
  VoiceRecorder,
  browserRecorderDeps,
  bytesToBase64,
  classifyCaptureError,
  downmixToMono,
  encodeWav,
  floatToPcm16,
  hasMicrophoneSupport,
  levelFromRms,
  resample,
  rms,
  type AudioContextLike,
  type ProcessEventLike,
  type RecorderHooks,
} from './voice';

const constant = (length: number, value: number): Float32Array => new Float32Array(length).fill(value);
const sine = (length: number, rate: number, hz: number, amplitude = 0.5): Float32Array => {
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) out[i] = amplitude * Math.sin((2 * Math.PI * hz * i) / rate);
  return out;
};
const decode = (base64: string): Buffer => Buffer.from(base64, 'base64');

describe('rms and level', () => {
  it('is zero for nothing and for silence', () => {
    expect(rms(new Float32Array(0))).toBe(0);
    expect(rms(constant(100, 0))).toBe(0);
  });

  it('measures constants and sine waves', () => {
    expect(rms(constant(10, 0.5))).toBeCloseTo(0.5, 6);
    expect(rms(constant(10, -0.5))).toBeCloseTo(0.5, 6);
    expect(rms(sine(48_000, 48_000, 440, 1))).toBeCloseTo(Math.SQRT1_2, 3);
  });

  it('maps energy to a 0..1 meter that grows and never exceeds 1', () => {
    expect(levelFromRms(0)).toBe(0);
    expect(levelFromRms(-1)).toBe(0);
    expect(levelFromRms(NaN)).toBe(0);
    expect(levelFromRms(Infinity)).toBe(0);
    expect(levelFromRms(0.001)).toBeGreaterThan(0);
    expect(levelFromRms(0.05)).toBeGreaterThan(levelFromRms(0.005));
    expect(levelFromRms(0.2)).toBeGreaterThan(levelFromRms(0.05));
    expect(levelFromRms(1)).toBe(1);
    expect(levelFromRms(50)).toBe(1);
  });
});

describe('downmixToMono', () => {
  it('returns empty for no channels and a copy for one', () => {
    expect(downmixToMono([])).toHaveLength(0);
    const channel = Float32Array.of(0.1, 0.2, 0.3);
    const mono = downmixToMono([channel]);
    expect([...mono]).toEqual([...channel]);
    expect(mono).not.toBe(channel);
  });

  it('averages channels and uses the shortest length', () => {
    expect([...downmixToMono([Float32Array.of(1, 0, 0.5), Float32Array.of(0, 1, 0.5)])]).toEqual([0.5, 0.5, 0.5]);
    expect([...downmixToMono([Float32Array.of(1, 1, 1), Float32Array.of(-1, -1)])]).toEqual([0, 0]);
    expect([...downmixToMono([Float32Array.of(0.3), Float32Array.of(0.3), Float32Array.of(0.3)])].map((v) => Number(v.toFixed(5)))).toEqual([0.3]);
    expect(downmixToMono([new Float32Array(0), Float32Array.of(1)])).toHaveLength(0);
  });
});

describe('resample', () => {
  it('handles an empty buffer', () => {
    expect(resample(new Float32Array(0), 48_000, 16_000)).toHaveLength(0);
    expect(resample(new Float32Array(0), 16_000, 16_000)).toHaveLength(0);
  });

  it('copies the input when the rates match', () => {
    const input = Float32Array.of(0.1, -0.2, 0.3);
    const out = resample(input, 16_000, 16_000);
    expect([...out]).toEqual([...input]);
    expect(out).not.toBe(input);
  });

  it('averages exact 3:1 groups when going from 48 kHz to 16 kHz', () => {
    expect([...resample(Float32Array.of(1, 2, 3, 4, 5, 6), 48_000, 16_000)]).toEqual([2, 5]);
    const flat = resample(constant(48_000, 0.5), 48_000, 16_000);
    expect(flat).toHaveLength(16_000);
    expect(flat.every((v) => Math.abs(v - 0.5) < 1e-6)).toBe(true);
  });

  it('drops an incomplete trailing group instead of inventing samples', () => {
    expect(resample(Float32Array.of(1, 2, 3, 4), 48_000, 16_000)).toHaveLength(1);
    expect(resample(Float32Array.of(1, 2), 48_000, 16_000)).toHaveLength(0);
    expect(resample(Float32Array.of(7), 48_000, 16_000)).toHaveLength(0);
  });

  it('handles a non-integer ratio such as 44.1 kHz without gaps or NaN', () => {
    expect(resample(constant(44_100, 0.25), 44_100, 16_000)).toHaveLength(16_000);
    const odd = resample(sine(1001, 44_100, 300), 44_100, 16_000);
    expect(odd).toHaveLength(Math.floor((1001 * 16_000) / 44_100));
    expect(odd.every((v) => Number.isFinite(v))).toBe(true);
    const flat = resample(constant(1000, 0.5), 44_100, 16_000);
    expect(flat.every((v) => Math.abs(v - 0.5) < 1e-5)).toBe(true);
  });

  it('keeps the loudness of speech-band tones', () => {
    const out = resample(sine(48_000, 48_000, 440, 0.5), 48_000, 16_000);
    expect(rms(out)).toBeGreaterThan(rms(sine(48_000, 48_000, 440, 0.5)) * 0.95);
  });

  it('interpolates when it has to go up', () => {
    expect([...resample(Float32Array.of(0, 1), 8000, 16_000)]).toEqual([0, 0.5, 1, 1]);
  });

  it('rejects impossible rates', () => {
    for (const bad of [0, -1, NaN, Infinity]) {
      expect(() => resample(Float32Array.of(1), bad, 16_000)).toThrow(RangeError);
      expect(() => resample(Float32Array.of(1), 16_000, bad)).toThrow(RangeError);
    }
  });
});

describe('floatToPcm16', () => {
  it('maps the full scale and clips beyond it', () => {
    expect(floatToPcm16(0)).toBe(0);
    expect(floatToPcm16(1)).toBe(32767);
    expect(floatToPcm16(-1)).toBe(-32768);
    expect(floatToPcm16(2.5)).toBe(32767);
    expect(floatToPcm16(-2.5)).toBe(-32768);
    expect(floatToPcm16(Infinity)).toBe(32767);
    expect(floatToPcm16(-Infinity)).toBe(-32768);
    expect(floatToPcm16(0.5)).toBe(16384);
    expect(floatToPcm16(-0.5)).toBe(-16384);
  });

  it('treats NaN as silence', () => {
    expect(floatToPcm16(NaN)).toBe(0);
  });
});

describe('encodeWav', () => {
  const header = (bytes: Uint8Array) => {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const text = (offset: number, length: number): string => String.fromCharCode(...bytes.subarray(offset, offset + length));
    return {
      riff: text(0, 4),
      riffSize: view.getUint32(4, true),
      wave: text(8, 4),
      fmt: text(12, 4),
      fmtSize: view.getUint32(16, true),
      format: view.getUint16(20, true),
      channels: view.getUint16(22, true),
      rate: view.getUint32(24, true),
      byteRate: view.getUint32(28, true),
      blockAlign: view.getUint16(32, true),
      bits: view.getUint16(34, true),
      data: text(36, 4),
      dataSize: view.getUint32(40, true),
      view,
    };
  };

  it('writes a canonical 16 kHz mono PCM16 header', () => {
    const wav = encodeWav(Float32Array.of(0, 0.5, -0.5, 1), 16_000);
    const h = header(wav);
    expect(wav).toHaveLength(44 + 8);
    expect([h.riff, h.wave, h.fmt, h.data]).toEqual(['RIFF', 'WAVE', 'fmt ', 'data']);
    expect(h.riffSize).toBe(36 + 8);
    expect([h.fmtSize, h.format, h.channels, h.rate, h.byteRate, h.blockAlign, h.bits]).toEqual([16, 1, 1, 16_000, 32_000, 2, 16]);
    expect(h.dataSize).toBe(8);
    expect([h.view.getInt16(44, true), h.view.getInt16(46, true), h.view.getInt16(48, true), h.view.getInt16(50, true)]).toEqual([0, 16384, -16384, 32767]);
  });

  it('is a valid header-only file for an empty buffer', () => {
    const wav = encodeWav(new Float32Array(0));
    const h = header(wav);
    expect(wav).toHaveLength(44);
    expect(h.riffSize).toBe(36);
    expect(h.dataSize).toBe(0);
  });

  it('handles odd sample counts', () => {
    for (const count of [1, 3, 7, 4095]) {
      const wav = encodeWav(constant(count, 0.25));
      expect(wav).toHaveLength(44 + count * 2);
      expect(header(wav).dataSize).toBe(count * 2);
      expect(header(wav).riffSize).toBe(36 + count * 2);
    }
  });

  it('clips out-of-range samples instead of wrapping', () => {
    const wav = encodeWav(Float32Array.of(3, -3, NaN, 1.0001));
    const h = header(wav);
    expect([0, 1, 2, 3].map((i) => h.view.getInt16(44 + i * 2, true))).toEqual([32767, -32768, 0, 32767]);
  });

  it('a full 30 s recording is about 960 KB and fits the 1.5 M character limit as base64', () => {
    const wav = encodeWav(sine(480_000, 16_000, 300));
    expect(wav).toHaveLength(960_044);
    const base64 = bytesToBase64(wav);
    expect(base64.length).toBe(1_280_060);
    expect(base64.length).toBeLessThanOrEqual(1_500_000);
  });

  it('refuses an invalid sample rate', () => {
    for (const bad of [0, -16_000, 16_000.5, NaN]) expect(() => encodeWav(Float32Array.of(0), bad)).toThrow(RangeError);
  });
});

describe('bytesToBase64', () => {
  it('matches the reference encoder for every length around the padding boundaries', () => {
    for (const length of [0, 1, 2, 3, 4, 5, 6, 255, 256, 8191, 8192, 8193, 16_385]) {
      const bytes = new Uint8Array(length).map((_v, i) => (i * 37 + 11) % 256);
      expect(bytesToBase64(bytes)).toBe(Buffer.from(bytes).toString('base64'));
    }
  });

  it('does not overflow the call stack with a megabyte of data', () => {
    const bytes = new Uint8Array(1_500_000).map((_v, i) => (i * 131) % 256);
    const out = bytesToBase64(bytes);
    expect(out).toHaveLength(2_000_000);
    expect(Buffer.from(out, 'base64').equals(Buffer.from(bytes))).toBe(true);
  });

  it('keeps bytes above 127 intact', () => {
    expect(bytesToBase64(Uint8Array.of(0, 127, 128, 255))).toBe('AH+A/w==');
  });
});

describe('SilenceDetector', () => {
  const loud = SPEECH_RMS_THRESHOLD * 4;
  const quiet = SPEECH_RMS_THRESHOLD / 4;

  it('never asks to stop before speech was heard, however long the silence', () => {
    const d = new SilenceDetector();
    for (let i = 0; i < 1000; i += 1) expect(d.push(quiet, 100)).toBe(false);
    expect(d.speechHeard).toBe(false);
  });

  it('stops after enough speech followed by enough silence', () => {
    const d = new SilenceDetector();
    expect(d.push(loud, MIN_SPEECH_MS)).toBe(false);
    expect(d.speechHeard).toBe(true);
    expect(d.push(quiet, SILENCE_MS - 1)).toBe(false);
    expect(d.push(quiet, 1)).toBe(true);
  });

  it('does not arm on a short blip', () => {
    const d = new SilenceDetector();
    d.push(loud, MIN_SPEECH_MS - 1);
    expect(d.speechHeard).toBe(false);
    expect(d.push(quiet, SILENCE_MS * 5)).toBe(false);
  });

  it('a pause shorter than the limit does not stop, and speech resets the silence clock', () => {
    const d = new SilenceDetector();
    d.push(loud, 1000);
    expect(d.push(quiet, SILENCE_MS - 100)).toBe(false);
    expect(d.push(loud, 100)).toBe(false);
    expect(d.push(quiet, SILENCE_MS - 100)).toBe(false);
    expect(d.push(quiet, 100)).toBe(true);
  });

  it('accumulates speech over several blocks and honours custom limits', () => {
    const d = new SilenceDetector({ threshold: 0.5, minSpeechMs: 300, silenceMs: 200 });
    d.push(0.6, 100);
    d.push(0.6, 100);
    expect(d.speechHeard).toBe(false);
    d.push(0.6, 100);
    expect(d.speechHeard).toBe(true);
    expect(d.push(0.4, 199)).toBe(false);
    expect(d.push(0.4, 1)).toBe(true);
  });

  it('uses documented constants', () => {
    expect(MIN_SPEECH_MS).toBeGreaterThan(0);
    expect(SILENCE_MS).toBeGreaterThanOrEqual(1500);
    expect(MAX_RECORDING_MS).toBe(30_000);
  });
});

describe('classifyCaptureError', () => {
  const named = (name: string): Error => Object.assign(new Error('x'), { name });

  it('maps browser errors to the codes the panel understands', () => {
    expect(classifyCaptureError(named('NotAllowedError')).code).toBe('permission');
    expect(classifyCaptureError(named('SecurityError')).code).toBe('permission');
    expect(classifyCaptureError(named('PermissionDeniedError')).code).toBe('permission');
    expect(classifyCaptureError(named('NotFoundError')).code).toBe('no_device');
    expect(classifyCaptureError(named('OverconstrainedError')).code).toBe('no_device');
    expect(classifyCaptureError(named('NotReadableError')).code).toBe('mic_busy');
    expect(classifyCaptureError(named('AbortError')).code).toBe('mic_busy');
    expect(classifyCaptureError(named('TypeError')).code).toBe('capture');
    expect(classifyCaptureError('boom').code).toBe('capture');
    expect(classifyCaptureError(null).code).toBe('capture');
    expect(classifyCaptureError(undefined).code).toBe('capture');
  });

  it('keeps an error that is already classified', () => {
    const own = new VoiceCaptureError('empty', 'short');
    expect(classifyCaptureError(own)).toBe(own);
  });
});

describe('hasMicrophoneSupport', () => {
  it('needs navigator.mediaDevices.getUserMedia', () => {
    expect(hasMicrophoneSupport(undefined)).toBe(false);
    expect(hasMicrophoneSupport({})).toBe(false);
    expect(hasMicrophoneSupport({ mediaDevices: {} })).toBe(false);
    expect(hasMicrophoneSupport({ mediaDevices: { getUserMedia: 'yes' } })).toBe(false);
    expect(hasMicrophoneSupport({ mediaDevices: { getUserMedia: () => undefined } })).toBe(true);
  });
});

class FakeTrack {
  stopped = 0;
  stop(): void {
    this.stopped += 1;
  }
}

class FakeStream {
  tracks = [new FakeTrack(), new FakeTrack()];
  getTracks(): FakeTrack[] {
    return this.tracks;
  }
  get stoppedAll(): boolean {
    return this.tracks.every((track) => track.stopped > 0);
  }
}

class FakeNode {
  links: unknown[] = [];
  disconnects = 0;
  connect(destination: unknown): unknown {
    this.links.push(destination);
    return destination;
  }
  disconnect(): void {
    this.disconnects += 1;
  }
}

class FakeProcessor extends FakeNode {
  onaudioprocess: ((event: ProcessEventLike) => void) | null = null;
}

class FakeContext {
  readonly destination = { kind: 'destination' };
  readonly source = new FakeNode();
  readonly processor = new FakeProcessor();
  sourceStream: unknown = null;
  processorArgs: number[] = [];
  resumed = 0;
  closed = 0;
  constructor(readonly sampleRate: number) {}
  createMediaStreamSource(stream: unknown): FakeNode {
    this.sourceStream = stream;
    return this.source;
  }
  createScriptProcessor(...args: number[]): FakeProcessor {
    this.processorArgs = args;
    return this.processor;
  }
  resume(): Promise<void> {
    this.resumed += 1;
    return Promise.resolve();
  }
  close(): Promise<void> {
    this.closed += 1;
    return Promise.resolve();
  }
  push(...channels: Float32Array[]): void {
    this.processor.onaudioprocess?.({ inputBuffer: { numberOfChannels: channels.length, getChannelData: (index: number) => channels[index] } });
  }
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface Rig {
  recorder: VoiceRecorder;
  contexts: FakeContext[];
  streams: FakeStream[];
  gum: ReturnType<typeof vi.fn>;
  hooks: Required<RecorderHooks> & { onLevel: ReturnType<typeof vi.fn>; onAutoStop: ReturnType<typeof vi.fn> };
  context(): FakeContext;
  stream(): FakeStream;
}

function rig(options: { rate?: number; getUserMedia?: () => Promise<unknown> } = {}): Rig {
  const contexts: FakeContext[] = [];
  const streams: FakeStream[] = [];
  const gum = vi.fn(
    options.getUserMedia ??
      (async () => {
        const stream = new FakeStream();
        streams.push(stream);
        return stream;
      }),
  );
  const hooks = { onLevel: vi.fn(), onAutoStop: vi.fn() };
  const recorder = new VoiceRecorder(
    {
      getUserMedia: gum as unknown as () => Promise<MediaStream>,
      createContext: () => {
        const context = new FakeContext(options.rate ?? 16_000);
        contexts.push(context);
        return context as unknown as AudioContextLike;
      },
    },
    hooks,
  );
  return { recorder, contexts, streams, gum, hooks, context: () => contexts[contexts.length - 1] as FakeContext, stream: () => streams[streams.length - 1] as FakeStream };
}

const block = (value = 0.2, length = PROCESSOR_BLOCK): Float32Array => constant(length, value);

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('VoiceRecorder: start', () => {
  it('asks for the microphone with audio only and wires one script processor to the destination', async () => {
    const r = rig();
    await r.recorder.start();
    expect(r.gum).toHaveBeenCalledTimes(1);
    expect(r.gum).toHaveBeenCalledWith({ audio: true });
    expect(r.recorder.active).toBe(true);
    const context = r.context();
    expect(context.sourceStream).toBe(r.stream());
    expect(context.processorArgs).toEqual([PROCESSOR_BLOCK, 1, 1]);
    expect(context.source.links).toEqual([context.processor]);
    expect(context.processor.links).toEqual([context.destination]);
    expect(context.resumed).toBe(1);
  });

  it('creates the audio context before the permission prompt so it starts inside the click', async () => {
    const pending = deferred<FakeStream>();
    const r = rig({ getUserMedia: () => pending.promise });
    const starting = r.recorder.start();
    expect(r.contexts).toHaveLength(1);
    expect(r.recorder.active).toBe(true);
    pending.resolve(new FakeStream());
    await starting;
  });

  it('refuses a second start while active', async () => {
    const r = rig();
    await r.recorder.start();
    await expect(r.recorder.start()).rejects.toMatchObject({ code: 'capture' });
    expect(r.gum).toHaveBeenCalledTimes(1);
    expect(r.stream().stoppedAll).toBe(false);
    expect(r.recorder.active).toBe(true);
  });

  it.each([
    ['NotAllowedError', 'permission'],
    ['NotFoundError', 'no_device'],
    ['NotReadableError', 'mic_busy'],
    ['SomethingElse', 'capture'],
  ])('%s becomes %s and leaves nothing open', async (name, code) => {
    const r = rig({
      getUserMedia: async () => {
        throw Object.assign(new Error('nope'), { name });
      },
    });
    await expect(r.recorder.start()).rejects.toMatchObject({ code });
    expect(r.recorder.active).toBe(false);
    expect(r.context().closed).toBe(1);
  });

  it('survives getUserMedia throwing synchronously', async () => {
    const r = rig({
      getUserMedia: () => {
        throw new TypeError('no mediaDevices');
      },
    });
    await expect(r.recorder.start()).rejects.toMatchObject({ code: 'capture' });
    expect(r.recorder.active).toBe(false);
    expect(r.context().closed).toBe(1);
  });

  it('reports unsupported when no audio context can be made, without asking for the microphone', async () => {
    const gum = vi.fn();
    const recorder = new VoiceRecorder({
      getUserMedia: gum as unknown as () => Promise<MediaStream>,
      createContext: () => {
        throw new Error('no AudioContext');
      },
    });
    await expect(recorder.start()).rejects.toMatchObject({ code: 'unsupported' });
    expect(gum).not.toHaveBeenCalled();
    expect(recorder.active).toBe(false);
  });

  it('releases the stream when the audio graph cannot be built', async () => {
    const r = rig();
    const original = r.recorder as unknown as { deps: { createContext: () => AudioContextLike } };
    original.deps.createContext = () => {
      const context = new FakeContext(16_000);
      context.createScriptProcessor = () => {
        throw new Error('too many processors');
      };
      r.contexts.push(context);
      return context as unknown as AudioContextLike;
    };
    await expect(r.recorder.start()).rejects.toMatchObject({ code: 'capture' });
    expect(r.stream().stoppedAll).toBe(true);
    expect(r.context().closed).toBe(1);
    expect(r.recorder.active).toBe(false);
  });

  it('treats a permission prompt that never answers as a permission problem after the start timeout', async () => {
    const pending = deferred<FakeStream>();
    const r = rig({ getUserMedia: () => pending.promise });
    const starting = r.recorder.start();
    const outcome = starting.catch((err: unknown) => err);
    await vi.advanceTimersByTimeAsync(START_TIMEOUT_MS - 1);
    expect(r.recorder.active).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    expect(await outcome).toMatchObject({ code: 'permission' });
    expect(r.recorder.active).toBe(false);
    expect(r.context().closed).toBe(1);
    const late = new FakeStream();
    pending.resolve(late);
    await Promise.resolve();
    expect(late.stoppedAll).toBe(true);
  });
});

describe('VoiceRecorder: recording', () => {
  it('reports the level of every block', async () => {
    const r = rig();
    await r.recorder.start();
    r.context().push(block(0));
    r.context().push(block(0.3));
    expect(r.hooks.onLevel).toHaveBeenCalledTimes(2);
    expect(r.hooks.onLevel.mock.calls[0]?.[0]).toBe(0);
    const loud = r.hooks.onLevel.mock.calls[1]?.[0] as number;
    expect(loud).toBeGreaterThan(0.5);
    expect(loud).toBeLessThanOrEqual(1);
  });

  it('finish returns a 16 kHz mono PCM16 wav and its duration, and releases everything', async () => {
    const r = rig();
    await r.recorder.start();
    for (let i = 0; i < 4; i += 1) r.context().push(block(0.25));
    const context = r.context();
    const stream = r.stream();
    const audio = r.recorder.finish();
    expect(audio.durationMs).toBe(Math.round((4 * PROCESSOR_BLOCK * 1000) / 16_000));
    const wav = decode(audio.audioBase64);
    expect(wav.toString('ascii', 0, 4)).toBe('RIFF');
    expect(wav.toString('ascii', 8, 12)).toBe('WAVE');
    expect(wav.readUInt32LE(24)).toBe(16_000);
    expect(wav.readUInt16LE(22)).toBe(1);
    expect(wav.readUInt16LE(34)).toBe(16);
    expect(wav.readUInt32LE(40)).toBe(4 * PROCESSOR_BLOCK * 2);
    expect(wav.readInt16LE(44)).toBe(8192);
    expect(wav).toHaveLength(44 + 4 * PROCESSOR_BLOCK * 2);
    expect(stream.stoppedAll).toBe(true);
    expect(context.closed).toBe(1);
    expect(context.processor.onaudioprocess).toBeNull();
    expect(context.processor.disconnects).toBeGreaterThan(0);
    expect(context.source.disconnects).toBeGreaterThan(0);
    expect(r.recorder.active).toBe(false);
  });

  it('resamples a 48 kHz capture to 16 kHz', async () => {
    const r = rig({ rate: 48_000 });
    await r.recorder.start();
    for (let i = 0; i < 12; i += 1) r.context().push(block(0.5));
    const audio = r.recorder.finish();
    const wav = decode(audio.audioBase64);
    expect(wav.readUInt32LE(24)).toBe(16_000);
    const samples = (wav.length - 44) / 2;
    expect(samples).toBe(Math.floor((12 * PROCESSOR_BLOCK) / 3));
    expect(audio.durationMs).toBe(Math.round((samples * 1000) / 16_000));
    expect(wav.readInt16LE(44)).toBe(16384);
  });

  it('downmixes a stereo input', async () => {
    const r = rig();
    await r.recorder.start();
    r.context().push(block(0.5), block(-0.5));
    r.context().push(block(0.5), block(-0.5));
    const audio = r.recorder.finish();
    const wav = decode(audio.audioBase64);
    expect(wav.readInt16LE(44)).toBe(0);
    expect(wav.readInt16LE(wav.length - 2)).toBe(0);
  });

  it('keeps a recording longer than 30 s down to exactly 30 s (about 960 KB)', async () => {
    const r = rig();
    await r.recorder.start();
    for (let i = 0; i < 130; i += 1) r.context().push(block(0.1));
    const audio = r.recorder.finish();
    expect(audio.durationMs).toBe(30_000);
    const wav = decode(audio.audioBase64);
    expect(wav).toHaveLength(960_044);
    expect(audio.audioBase64.length).toBeLessThanOrEqual(1_500_000);
  });

  it('refuses a recording that is too short and still releases the microphone', async () => {
    const r = rig();
    await r.recorder.start();
    r.context().push(block(0.2, 256));
    expect(() => r.recorder.finish()).toThrowError(VoiceCaptureError);
    expect(r.stream().stoppedAll).toBe(true);
    expect(r.recorder.active).toBe(false);
    const again = rig();
    await again.recorder.start();
    expect(() => again.recorder.finish()).toThrowError(/too short/);
  });

  it('finish and cancel are safe when nothing is recording', () => {
    const r = rig();
    expect(() => r.recorder.finish()).toThrowError(VoiceCaptureError);
    expect(() => r.recorder.cancel()).not.toThrow();
    expect(r.recorder.active).toBe(false);
  });

  it('ignores blocks that arrive after finish or cancel', async () => {
    const r = rig();
    await r.recorder.start();
    const context = r.context();
    for (let i = 0; i < 4; i += 1) context.push(block());
    r.recorder.cancel();
    expect(context.processor.onaudioprocess).toBeNull();
    context.push(block());
    expect(r.hooks.onLevel).toHaveBeenCalledTimes(4);
  });

  it('can record again after finishing, with a fresh context and stream', async () => {
    const r = rig();
    await r.recorder.start();
    for (let i = 0; i < 4; i += 1) r.context().push(block());
    r.recorder.finish();
    await r.recorder.start();
    expect(r.contexts).toHaveLength(2);
    expect(r.stream().stoppedAll).toBe(false);
    r.recorder.cancel();
    expect(r.stream().stoppedAll).toBe(true);
    expect(r.streams.every((s) => s.stoppedAll)).toBe(true);
  });
});

describe('VoiceRecorder: cancel', () => {
  it('releases every track and closes the context', async () => {
    const r = rig();
    await r.recorder.start();
    r.recorder.cancel();
    expect(r.stream().stoppedAll).toBe(true);
    expect(r.context().closed).toBe(1);
    expect(r.recorder.active).toBe(false);
    r.recorder.cancel();
    expect(r.context().closed).toBe(1);
  });

  it('cancelling during the permission prompt rejects start and drops a stream that arrives later', async () => {
    const pending = deferred<FakeStream>();
    const r = rig({ getUserMedia: () => pending.promise });
    const starting = r.recorder.start();
    const outcome = starting.catch((err: unknown) => err);
    r.recorder.cancel();
    expect(await outcome).toMatchObject({ code: 'cancelled' });
    expect(r.recorder.active).toBe(false);
    expect(r.context().closed).toBe(1);
    const late = new FakeStream();
    pending.resolve(late);
    await Promise.resolve();
    await Promise.resolve();
    expect(late.stoppedAll).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('a start cancelled right after the stream arrived still releases it', async () => {
    const pending = deferred<FakeStream>();
    const r = rig({ getUserMedia: () => pending.promise });
    const outcome = r.recorder.start().catch((err: unknown) => err);
    const stream = new FakeStream();
    pending.resolve(stream);
    r.recorder.cancel();
    expect(await outcome).toMatchObject({ code: 'cancelled' });
    expect(stream.stoppedAll).toBe(true);
    expect(r.recorder.active).toBe(false);
  });

  it('a cancelled start does not break a new start that began meanwhile', async () => {
    const first = deferred<FakeStream>();
    const second = new FakeStream();
    let calls = 0;
    const r = rig({
      getUserMedia: () => {
        calls += 1;
        return calls === 1 ? first.promise : Promise.resolve(second);
      },
    });
    const firstOutcome = r.recorder.start().catch((err: unknown) => err);
    r.recorder.cancel();
    const secondStart = r.recorder.start();
    expect(await firstOutcome).toMatchObject({ code: 'cancelled' });
    await secondStart;
    expect(r.recorder.active).toBe(true);
    expect(second.stoppedAll).toBe(false);
    r.recorder.cancel();
    expect(second.stoppedAll).toBe(true);
  });
});

describe('VoiceRecorder: automatic stop', () => {
  it('asks to stop once, after speech followed by a pause', async () => {
    const r = rig();
    await r.recorder.start();
    const blockMs = (PROCESSOR_BLOCK * 1000) / 16_000;
    const speechBlocks = Math.ceil(MIN_SPEECH_MS / blockMs);
    const silenceBlocks = Math.ceil(SILENCE_MS / blockMs);
    for (let i = 0; i < speechBlocks; i += 1) r.context().push(block(0.2));
    for (let i = 0; i < silenceBlocks - 1; i += 1) r.context().push(block(0));
    expect(r.hooks.onAutoStop).not.toHaveBeenCalled();
    r.context().push(block(0));
    expect(r.hooks.onAutoStop).toHaveBeenCalledTimes(1);
    r.context().push(block(0));
    r.context().push(block(0));
    expect(r.hooks.onAutoStop).toHaveBeenCalledTimes(1);
  });

  it('does not stop on silence when no speech was heard', async () => {
    const r = rig();
    await r.recorder.start();
    for (let i = 0; i < 60; i += 1) r.context().push(block(0.001));
    expect(r.hooks.onAutoStop).not.toHaveBeenCalled();
  });

  it('stops after 30 seconds on the clock', async () => {
    const r = rig();
    await r.recorder.start();
    await vi.advanceTimersByTimeAsync(MAX_RECORDING_MS - 1);
    expect(r.hooks.onAutoStop).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(r.hooks.onAutoStop).toHaveBeenCalledTimes(1);
  });

  it('stops after 30 seconds of captured audio even without a timer tick', async () => {
    const r = rig();
    await r.recorder.start();
    const needed = Math.ceil((16_000 * 30) / PROCESSOR_BLOCK);
    for (let i = 0; i < needed - 1; i += 1) r.context().push(block(0.2));
    expect(r.hooks.onAutoStop).not.toHaveBeenCalled();
    r.context().push(block(0.2));
    expect(r.hooks.onAutoStop).toHaveBeenCalledTimes(1);
  });

  it('is safe for the stop callback to finish the recording from inside it', async () => {
    const contexts: FakeContext[] = [];
    const results: Array<ReturnType<VoiceRecorder['finish']>> = [];
    const holder: { recorder?: VoiceRecorder } = {};
    const recorder = new VoiceRecorder(
      {
        getUserMedia: (async () => new FakeStream()) as unknown as () => Promise<MediaStream>,
        createContext: () => {
          const context = new FakeContext(16_000);
          contexts.push(context);
          return context as unknown as AudioContextLike;
        },
      },
      {
        onAutoStop: () => {
          if (holder.recorder) results.push(holder.recorder.finish());
        },
      },
    );
    holder.recorder = recorder;
    await recorder.start();
    const blockMs = (PROCESSOR_BLOCK * 1000) / 16_000;
    for (let i = 0; i < Math.ceil(MIN_SPEECH_MS / blockMs); i += 1) contexts[0]?.push(block(0.2));
    for (let i = 0; i < Math.ceil(SILENCE_MS / blockMs) + 3; i += 1) contexts[0]?.push(block(0));
    expect(results).toHaveLength(1);
    expect(recorder.active).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('the max timer is cleared when the recording ends', async () => {
    const r = rig();
    await r.recorder.start();
    for (let i = 0; i < 4; i += 1) r.context().push(block());
    r.recorder.finish();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(MAX_RECORDING_MS * 2);
    expect(r.hooks.onAutoStop).not.toHaveBeenCalled();
  });
});

describe('browserRecorderDeps', () => {
  it('uses navigator.mediaDevices and a new AudioContext per recording', async () => {
    const stream = new FakeStream();
    const getUserMedia = vi.fn(async () => stream);
    const created: unknown[] = [];
    class FakeAudioContext {
      constructor() {
        created.push(this);
      }
    }
    const win = { navigator: { mediaDevices: { getUserMedia } }, AudioContext: FakeAudioContext } as unknown as Window & typeof globalThis;
    const deps = browserRecorderDeps(win);
    expect(await deps.getUserMedia({ audio: true })).toBe(stream);
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    deps.createContext();
    deps.createContext();
    expect(created).toHaveLength(2);
  });

  it('reports unsupported when there is no AudioContext', () => {
    const win = { navigator: { mediaDevices: { getUserMedia: vi.fn() } } } as unknown as Window & typeof globalThis;
    expect(() => browserRecorderDeps(win).createContext()).toThrowError(VoiceCaptureError);
  });
});
