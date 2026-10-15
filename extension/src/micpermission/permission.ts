export type Translate = (key: string, substitutions?: string | string[]) => string;
export type PermissionResult = 'granted' | 'denied' | 'no_device' | 'error';

export const STUCK_PROMPT_MS = 20_000;

export interface PermissionPageDeps {
  t: Translate;
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  stuckMs?: number;
}

const MESSAGE_KEYS: Record<PermissionResult, string> = {
  granted: 'micPermDone',
  denied: 'micPermDenied',
  no_device: 'micPermNoDevice',
  error: 'micPermError',
};

export function classifyMicrophoneError(err: unknown): Exclude<PermissionResult, 'granted'> {
  const name = typeof err === 'object' && err !== null && 'name' in err ? String((err as { name: unknown }).name) : '';
  switch (name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return 'denied';
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return 'no_device';
    default:
      return 'error';
  }
}

export async function requestMicrophone(getUserMedia: PermissionPageDeps['getUserMedia']): Promise<PermissionResult> {
  if (!getUserMedia) return 'error';
  let stream: MediaStream;
  try {
    stream = await getUserMedia({ audio: true });
  } catch (err) {
    return classifyMicrophoneError(err);
  }
  for (const track of stream.getTracks()) {
    try {
      track.stop();
    } catch {
      // a track that cannot stop is already dead
    }
  }
  return 'granted';
}

export function mountPermissionPage(root: HTMLElement, deps: PermissionPageDeps): { request(): Promise<PermissionResult> } {
  const { t } = deps;
  document.title = t('micPermTitle');
  root.replaceChildren();

  const title = document.createElement('h1');
  title.className = 'title';
  title.textContent = t('micPermTitle');
  const intro = document.createElement('p');
  intro.className = 'intro';
  intro.textContent = t('micPermIntro');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'allow';
  button.dataset.testid = 'allow-mic';
  button.textContent = t('micPermButton');
  const status = document.createElement('p');
  status.className = 'result';
  status.dataset.testid = 'result';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  root.append(title, intro, button, status);

  const setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = deps.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  const stuckMs = deps.stuckMs ?? STUCK_PROMPT_MS;
  let latest = 0;
  let done = false;

  const showFailure = (key: string): void => {
    status.className = 'result problem';
    status.textContent = t(key);
    button.disabled = false;
  };

  // A prompt that never shows (or is ignored) leaves the browser call pending, so the page offers a way out after a while.
  const request = async (): Promise<PermissionResult> => {
    if (done) return 'granted';
    const mine = (latest += 1);
    button.disabled = true;
    status.className = 'result';
    status.textContent = t('micPermWaiting');
    const timer = setTimer(() => {
      if (mine === latest && !done) showFailure('micPermDenied');
    }, stuckMs);
    const result = await requestMicrophone(deps.getUserMedia);
    clearTimer(timer);
    if (result === 'granted') {
      done = true;
      status.className = 'result ok';
      status.textContent = t(MESSAGE_KEYS.granted);
      button.hidden = true;
    } else if (mine === latest && !done) {
      showFailure(MESSAGE_KEYS[result]);
      button.focus();
    }
    return result;
  };
  button.addEventListener('click', () => {
    void request();
  });
  return { request };
}
