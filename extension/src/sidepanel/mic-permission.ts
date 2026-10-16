export interface TabLike {
  id?: number;
  windowId?: number;
}

export interface TabsApi {
  query(info: { url: string }): Promise<TabLike[]>;
  update(tabId: number, properties: { active: boolean }): Promise<unknown>;
  create(properties: { url: string }): Promise<unknown>;
}

export interface WindowsApi {
  update(windowId: number, info: { focused: boolean }): Promise<unknown>;
}

async function focusTab(tab: TabLike, tabs: TabsApi, windows: WindowsApi | undefined): Promise<boolean> {
  if (tab.id === undefined) return false;
  try {
    await tabs.update(tab.id, { active: true });
  } catch {
    return false;
  }
  if (tab.windowId !== undefined) await windows?.update(tab.windowId, { focused: true }).catch(() => undefined);
  return true;
}

export async function openMicPermissionTab(url: string, tabs: TabsApi, windows?: WindowsApi): Promise<void> {
  let existing: TabLike[] = [];
  try {
    existing = await tabs.query({ url });
  } catch {
    // a failed look-up only costs a second tab
  }
  for (const tab of existing) {
    if (await focusTab(tab, tabs, windows)) return;
  }
  await tabs.create({ url }).catch(() => undefined);
}

export interface PermissionStatusLike {
  readonly state: string;
  addEventListener(type: 'change', listener: () => void): void;
  removeEventListener(type: 'change', listener: () => void): void;
}

export interface WatchDeps {
  query: () => Promise<PermissionStatusLike>;
  onResume: (listener: () => void) => () => void;
}

// Calls onGranted (asynchronously) when the microphone permission turns to "granted" after the watch began.
export function watchMicrophonePermission(onGranted: () => void, deps: WatchDeps): () => void {
  let stopped = false;
  let status: PermissionStatusLike | null = null;
  let wasGranted = false;
  let unlisten: (() => void) | null = null;

  function stop(): void {
    if (stopped) return;
    stopped = true;
    status?.removeEventListener('change', check);
    unlisten?.();
    unlisten = null;
  }

  function check(): void {
    if (stopped || !status) return;
    if (status.state !== 'granted') {
      wasGranted = false;
      return;
    }
    if (wasGranted) return;
    stop();
    onGranted();
  }

  let pending: Promise<PermissionStatusLike>;
  try {
    pending = deps.query();
  } catch {
    return stop;
  }
  pending.then(
    (found) => {
      if (stopped) return;
      status = found;
      wasGranted = found.state === 'granted';
      found.addEventListener('change', check);
      unlisten = deps.onResume(check);
    },
    () => undefined,
  );
  return stop;
}
