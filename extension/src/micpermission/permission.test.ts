import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import esMessages from '../../public/_locales/es/messages.json';
import { STUCK_PROMPT_MS, classifyMicrophoneError, mountPermissionPage, requestMicrophone } from './permission';

const catalog = esMessages as Record<string, { message: string }>;
const t = (key: string): string => catalog[key]?.message ?? key;

class FakeTrack {
  stopped = 0;
  stop(): void {
    this.stopped += 1;
  }
}

const fakeStream = (): { tracks: FakeTrack[]; stream: MediaStream } => {
  const tracks = [new FakeTrack(), new FakeTrack()];
  return { tracks, stream: { getTracks: () => tracks } as unknown as MediaStream };
};

const refusal = (name: string): Error => Object.assign(new Error(name), { name });
const flush = async (): Promise<void> => {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
};

describe('classifyMicrophoneError', () => {
  it('tells a refusal from a missing microphone from anything else', () => {
    expect(classifyMicrophoneError(refusal('NotAllowedError'))).toBe('denied');
    expect(classifyMicrophoneError(refusal('SecurityError'))).toBe('denied');
    expect(classifyMicrophoneError(refusal('NotFoundError'))).toBe('no_device');
    expect(classifyMicrophoneError(refusal('OverconstrainedError'))).toBe('no_device');
    expect(classifyMicrophoneError(refusal('NotReadableError'))).toBe('mic_busy');
    expect(classifyMicrophoneError(refusal('TrackStartError'))).toBe('mic_busy');
    expect(classifyMicrophoneError(refusal('AbortError'))).toBe('mic_busy');
    expect(classifyMicrophoneError(refusal('SomethingElse'))).toBe('error');
    expect(classifyMicrophoneError('boom')).toBe('error');
    expect(classifyMicrophoneError(null)).toBe('error');
  });
});

describe('requestMicrophone', () => {
  it('asks for audio only and stops every track at once', async () => {
    const { tracks, stream } = fakeStream();
    const getUserMedia = vi.fn(async () => stream);
    expect(await requestMicrophone(getUserMedia)).toBe('granted');
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(tracks.every((track) => track.stopped === 1)).toBe(true);
  });

  it('reports a refusal, a missing device and other failures', async () => {
    expect(await requestMicrophone(async () => Promise.reject(refusal('NotAllowedError')))).toBe('denied');
    expect(await requestMicrophone(async () => Promise.reject(refusal('NotFoundError')))).toBe('no_device');
    expect(await requestMicrophone(async () => Promise.reject(refusal('NotReadableError')))).toBe('mic_busy');
    expect(await requestMicrophone(async () => Promise.reject(refusal('WeirdError')))).toBe('error');
    expect(
      await requestMicrophone(() => {
        throw new TypeError('no mediaDevices');
      }),
    ).toBe('error');
    expect(await requestMicrophone(undefined)).toBe('error');
  });

  it('still reports success when a track refuses to stop', async () => {
    const stream = {
      getTracks: () => [
        {
          stop: () => {
            throw new Error('already gone');
          },
        },
      ],
    } as unknown as MediaStream;
    expect(await requestMicrophone(async () => stream)).toBe('granted');
  });
});

describe('the permission page', () => {
  beforeEach(() => {
    document.body.innerHTML = '<main id="app"></main>';
  });

  afterEach(() => {
    document.body.innerHTML = '';
  });

  const root = (): HTMLElement => document.getElementById('app') as HTMLElement;
  const button = (): HTMLButtonElement => root().querySelector('[data-testid=allow-mic]') as HTMLButtonElement;
  const result = (): HTMLElement => root().querySelector('[data-testid=result]') as HTMLElement;

  it('explains itself in Spanish and shows one big button, without asking by itself', () => {
    const getUserMedia = vi.fn();
    mountPermissionPage(root(), { t, getUserMedia });
    expect(document.title).toBe('Permiso del micrófono');
    expect(root().querySelector('h1')?.textContent).toBe('Permiso del micrófono');
    expect(root().querySelector('.intro')?.textContent).toContain('micrófono');
    expect(button().textContent).toBe('Permitir el micrófono');
    expect(root().querySelectorAll('button')).toHaveLength(1);
    expect(result().getAttribute('aria-live')).toBe('polite');
    expect(result().textContent).toBe('');
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it('tells the person, before asking, that a voice service turns the recording into text and that it is not saved', () => {
    mountPermissionPage(root(), { t, getUserMedia: vi.fn() });
    const privacy = root().querySelector('[data-testid=privacy]');
    expect(privacy?.textContent).toBe(t('micPermPrivacy'));
    expect(privacy?.textContent).toContain('servicio de voz');
    expect(privacy?.textContent).toContain('quien instaló BABAS');
    expect(privacy?.textContent).toContain('texto');
    expect(privacy?.textContent).toContain('no la guarda');
    expect([...root().children].indexOf(privacy as Element)).toBeLessThan([...root().children].indexOf(button()));
  });

  it('asks only when the button is pressed, shows a waiting hint, then the success text and hides the button', async () => {
    const { tracks, stream } = fakeStream();
    let release!: () => void;
    const getUserMedia = vi.fn(
      () =>
        new Promise<MediaStream>((resolve) => {
          release = () => resolve(stream);
        }),
    );
    mountPermissionPage(root(), { t, getUserMedia });
    button().click();
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(button().disabled).toBe(true);
    expect(result().textContent).toBe(t('micPermWaiting'));
    expect(result().textContent).not.toContain('«Permitir»');
    expect(result().textContent).toContain('barra de direcciones');
    button().click();
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    release();
    await flush();
    expect(result().textContent).toBe('Listo. Ya puede cerrar esta pestaña y volver a BABAS.');
    expect(result().classList.contains('ok')).toBe(true);
    expect(button().hidden).toBe(true);
    expect(tracks.every((track) => track.stopped === 1)).toBe(true);
  });

  it('explains how to allow the microphone in the browser settings when it is refused again, and lets the user retry', async () => {
    const getUserMedia = vi.fn(async () => Promise.reject(refusal('NotAllowedError')));
    mountPermissionPage(root(), { t, getUserMedia });
    button().click();
    await flush();
    const message = result().textContent ?? '';
    expect(message).toContain('Configuración del sitio');
    expect(message).toContain('Micrófono');
    expect(message).toContain('Permitir');
    expect(result().classList.contains('problem')).toBe(true);
    expect(button().disabled).toBe(false);
    expect(button().hidden).toBe(false);
    button().click();
    await flush();
    expect(getUserMedia).toHaveBeenCalledTimes(2);
  });

  describe('when the browser never shows the prompt', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    const pending = (): { calls: Array<{ resolve: (s: MediaStream) => void; reject: (e: unknown) => void }>; getUserMedia: () => Promise<MediaStream> } => {
      const calls: Array<{ resolve: (s: MediaStream) => void; reject: (e: unknown) => void }> = [];
      return {
        calls,
        getUserMedia: () =>
          new Promise<MediaStream>((resolve, reject) => {
            calls.push({ resolve, reject });
          }),
      };
    };

    it('offers the settings explanation and a retry after a while, and still accepts a late permission', async () => {
      const { calls, getUserMedia } = pending();
      mountPermissionPage(root(), { t, getUserMedia });
      button().click();
      vi.advanceTimersByTime(STUCK_PROMPT_MS - 1);
      expect(button().disabled).toBe(true);
      expect(result().textContent).toBe(t('micPermWaiting'));
      vi.advanceTimersByTime(1);
      expect(result().textContent).toBe(t('micPermDenied'));
      expect(result().classList.contains('problem')).toBe(true);
      expect(button().disabled).toBe(false);
      const { tracks, stream } = fakeStream();
      calls[0]?.resolve(stream);
      await flush();
      expect(result().textContent).toBe(t('micPermDone'));
      expect(button().hidden).toBe(true);
      expect(tracks.every((track) => track.stopped === 1)).toBe(true);
    });

    it('a retry after the wait supersedes an older refusal that arrives late', async () => {
      const { calls, getUserMedia } = pending();
      mountPermissionPage(root(), { t, getUserMedia });
      button().click();
      vi.advanceTimersByTime(STUCK_PROMPT_MS);
      button().click();
      expect(calls).toHaveLength(2);
      expect(result().textContent).toBe(t('micPermWaiting'));
      calls[0]?.reject(refusal('NotAllowedError'));
      await flush();
      expect(result().textContent).toBe(t('micPermWaiting'));
      expect(button().disabled).toBe(true);
      calls[1]?.resolve(fakeStream().stream);
      await flush();
      expect(result().textContent).toBe(t('micPermDone'));
    });

    it('shows no stuck message when the browser answers in time, and leaves no timer behind', async () => {
      mountPermissionPage(root(), { t, getUserMedia: async () => fakeStream().stream });
      button().click();
      await flush();
      expect(result().textContent).toBe(t('micPermDone'));
      expect(vi.getTimerCount()).toBe(0);
      vi.advanceTimersByTime(STUCK_PROMPT_MS * 2);
      expect(result().textContent).toBe(t('micPermDone'));
    });
  });

  it('says that another app may be using the microphone, like the panel does', async () => {
    for (const name of ['NotReadableError', 'AbortError', 'TrackStartError']) {
      document.body.innerHTML = '<main id="app"></main>';
      mountPermissionPage(root(), { t, getUserMedia: async () => Promise.reject(refusal(name)) });
      button().click();
      await flush();
      expect(result().textContent).toBe(t('micPermBusy'));
      expect(result().textContent).toBe(catalog.voiceErrMicBusy?.message);
      expect(result().classList.contains('problem')).toBe(true);
      expect(button().disabled).toBe(false);
    }
  });

  it('says when there is no microphone and when the browser cannot record at all', async () => {
    mountPermissionPage(root(), { t, getUserMedia: async () => Promise.reject(refusal('NotFoundError')) });
    button().click();
    await flush();
    expect(result().textContent).toBe(t('micPermNoDevice'));

    document.body.innerHTML = '<main id="app"></main>';
    mountPermissionPage(root(), { t, getUserMedia: undefined });
    button().click();
    await flush();
    expect(result().textContent).toBe(t('micPermError'));
  });

  it('renders text only, never markup', () => {
    mountPermissionPage(root(), { t: () => '<img src=x onerror="window.__pwned=1">', getUserMedia: vi.fn() });
    expect(root().querySelectorAll('img').length).toBe(0);
    expect(root().querySelector('h1')?.textContent).toContain('<img');
  });
});
